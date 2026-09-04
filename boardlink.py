#!/usr/bin/env python3
"""boardlink：把 ai-limit 的额度快照通过局域网暴露给桌面小屏（ES3C28P）。

设计约定（与板端固件的契约，改动需两头同步）：
- HTTP GET /quota.json 返回显示级 JSON：所有中文标签、重置行、预测行都在
  Mac 端算好，板子只负责画。这样文案与格式可以随 ai-limit 迭代而不必重烧固件。
- 端口用系统分配的临时端口，通过 Bonjour 服务 ``_ailimit._tcp`` 广播（名称
  ``ai-limit``），板子用 mDNS 服务发现拿 IP+端口，不存在写死 IP 的问题。
- 绑 0.0.0.0（板子靠 mDNS 拿 IP+端口，绑回环就发现不到），但**只受理私网 /
  回环 / 链路本地来源**（is_allowed_client），公网可路由的来源在连接层就被拒。
  数据是只读的额度百分比与套餐档位，不含任何凭据。仍无鉴权——信任边界是
  "同一局域网"，所以宿主端提供了一键关闭的开关（菜单「桌面小屏供数」），
  在咖啡厅/公司网这类不可信网络下可以整个停掉：停服务、关监听、注销 Bonjour。

契约（v1）：
    {"v":1, "ts":<epoch秒>,
     "services":[
       {"name":"Claude Code","plan":"Max 5x",
        "rows":[{"label":"5小时","left":72},...],   # left: 0-100，未知为 -1
        "footer":"重置 16:00 · 周四 08:00"},
       {"name":"CodeX", ...}]}
板端上限：services≤2、rows≤3、name≤23B、plan≤15B、label≤15B、footer≤47B（UTF-8）。
"""
from __future__ import annotations

import ipaddress
import json
import os
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONTRACT_VERSION = 1
SERVICE_TYPE = "_ailimit._tcp."
SERVICE_NAME = "ai-limit"

# 服务对象只有桌面小屏一台（每分钟拉一次），但监听的是 0.0.0.0——同网任何设备
# 都能连。没有上限时每条 TCP 连接开一个线程且 handler 无超时，连上不发数据就
# 永远占着；几千条连接即可耗尽整个菜单栏 App 的线程（macOS 上限 8192），
# 把 App 整个拖死。这两个常量就是那道闸。
MAX_CONNECTIONS = 8       # 并发连接上限，超出直接断开（板子只有一台，8 足够富余）
CONNECTION_TIMEOUT = 10   # 单连接读写超时（秒）：慢客户端不许一直占着线程


def is_allowed_client(client_address):
    """只服务私网 / 回环 / 链路本地来源。

    绑 0.0.0.0 是板子发现所必需的（板端靠 mDNS 拿 IP + 端口，绑回环就找不到），
    但那不等于该把额度数据端给任何能路由到本机的来源。在连接层按来源地址挡一道，
    是纵深防御，且不动板端契约——板子和 Mac 在同一局域网，地址必然是私网。

    解析不出来的地址一律拒绝：拿不准就不给，比放行未知安全。
    """
    if not client_address:
        return False
    host = client_address[0] if isinstance(client_address, (tuple, list)) else client_address
    try:
        ip = ipaddress.ip_address(str(host).split("%", 1)[0])   # 去掉 IPv6 的 %en0 作用域
    except ValueError:
        return False
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local)

# 板端 char 数组的字节上限（UTF-8，含结尾 NUL 之前的净荷）
_LIMITS = {"name": 23, "plan": 15, "label": 15, "footer": 47}


def _clip(text, limit_key):
    """按 UTF-8 字节数截断到板端上限，绝不切碎多字节字符。"""
    if not text:
        return ""
    limit = _LIMITS[limit_key]
    raw = str(text).encode("utf-8")
    if len(raw) <= limit:
        return str(text)
    while limit > 0 and (raw[limit] & 0xC0) == 0x80:
        limit -= 1
    return raw[:limit].decode("utf-8", "ignore")


def _pct(value):
    """余量百分比归一：None/异常 → -1（板端画空条），其余压进 0-100。"""
    try:
        v = int(round(float(value)))
    except (TypeError, ValueError):
        return -1
    return max(0, min(100, v))


def build_service_entry(name, plan, rows, footer):
    """一条服务卡片的契约条目；rows 为 (label, left) 序列，最多取 3 条。"""
    return {
        "name": _clip(name, "name"),
        "plan": _clip(plan or "", "plan"),
        "rows": [
            {"label": _clip(label, "label"), "left": _pct(left)}
            for label, left in list(rows)[:3]
        ],
        "footer": _clip(footer or "", "footer"),
    }


_DNSSD_BIN = "/usr/bin/dns-sd"


def _stale_dnssd_pids(entries):
    """从 (pid, ppid, command) 列表里挑出**我们自己留下的孤儿** dns-sd 注册进程。

    App 被 SIGKILL / pkill / 强制退出时 atexit 不会执行，dns-sd 子进程被 launchd
    收养（PPID 变成 1）后继续广播一个已经随进程消失的端口——板子通过 mDNS 发现
    这个僵尸服务却连不上。实测复现：kill 掉 App 后 `dns-sd -B _ailimit._tcp`
    仍能看到条目。

    判据故意收得很紧，只回收同时满足三条的：
    1. PPID == 1（已成孤儿）——活实例的子进程 PPID 指向那个实例，绝不误杀
    2. 是 `-R`（注册）而不是 `-B`（浏览）
    3. 服务名与类型都是我们自己的
    """
    out = []
    for pid, ppid, cmd in entries or []:
        if ppid != 1:
            continue
        parts = str(cmd).split()
        if len(parts) < 4 or not parts[0].endswith("dns-sd"):
            continue
        if parts[1] != "-R" or parts[2] != SERVICE_NAME:
            continue
        if not parts[3].startswith(SERVICE_TYPE.rstrip(".")):
            continue
        out.append(pid)
    return out


def _reap_stale_dnssd():
    """启动时清掉上一轮留下的僵尸广播。任何失败都静默——这是清理，不是主线。"""
    import subprocess
    try:
        raw = subprocess.run(["/bin/ps", "-eo", "pid=,ppid=,command="],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return []
    entries = []
    for line in raw.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            entries.append((int(parts[0]), int(parts[1]), parts[2]))
    killed = []
    for pid in _stale_dnssd_pids(entries):
        try:
            os.kill(pid, signal.SIGTERM)
            killed.append(pid)
        except Exception:
            pass
    return killed


class _BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """带并发连接上限的 ThreadingHTTPServer。

    超过 MAX_CONNECTIONS 的连接**立刻断开**而不是排队——排队只是把线程耗尽
    换成内存耗尽，而且板子每分钟才拉一次，正常情况永远碰不到这个上限。
    """

    daemon_threads = True          # 显式写出来：不能让残留连接线程挡住进程退出
    block_on_close = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._slots = threading.Semaphore(MAX_CONNECTIONS)

    def verify_request(self, request, client_address):
        """socketserver 在 process_request 之前调用：非私网来源直接不受理。"""
        return is_allowed_client(client_address)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.close_request(request)       # 满了就断，不排队
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()             # 线程没起来也要还回名额
            raise

    def shutdown_request(self, request):
        try:
            super().shutdown_request(request)
        finally:
            self._slots.release()


class BoardLinkServer:
    """后台 HTTP 服务 + Bonjour 广播。snapshot_fn 由宿主注入，须线程安全可调。"""

    def __init__(self, snapshot_fn):
        self._snapshot_fn = snapshot_fn
        self._httpd = None
        self._dnssd = None
        self._atexit_done = False
        self.port = None

    def start(self):
        snapshot_fn = self._snapshot_fn

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 (http.server 命名约定)
                if self.path.split("?", 1)[0] != "/quota.json":
                    self.send_error(404)
                    return
                try:
                    payload = json.dumps(
                        snapshot_fn(), ensure_ascii=False,
                        separators=(",", ":")).encode("utf-8")
                except Exception:  # 快照失败不拖垮服务：给板子一个空态
                    payload = json.dumps(
                        {"v": CONTRACT_VERSION, "ts": 0, "services": []},
                        separators=(",", ":")).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type",
                                 "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args):  # 板子每分钟拉一次，别刷日志
                pass

        Handler.timeout = CONNECTION_TIMEOUT      # StreamRequestHandler.setup() 会用它 settimeout
        self._httpd = _BoundedThreadingHTTPServer(("0.0.0.0", 0), Handler)
        self.port = self._httpd.server_address[1]
        threading.Thread(target=self._httpd.serve_forever,
                         name="boardlink-http", daemon=True).start()
        self._publish()
        return self.port

    def _publish(self):
        """Bonjour 广播：直接用系统自带 dns-sd 子进程。

        为什么不用 NSNetService：它依赖调用线程的 NSRunLoop 调度，在 py2app
        bundle 里（__init__ 先于 runloop 启动执行）实测发布不出去；dns-sd -R
        是 mDNSResponder 官方客户端，进程活着注册就活着，退出即注销，行为
        完全确定。失败不致命——板子会报 error=discover，宿主可查。"""
        import atexit
        import subprocess
        _reap_stale_dnssd()      # 先清掉上一轮被 SIGKILL 留下的僵尸广播
        try:
            self._dnssd = subprocess.Popen(
                [_DNSSD_BIN, "-R", SERVICE_NAME, "_ailimit._tcp", ".",
                 str(self.port)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            # 只注册一次：start/stop 反复调用时重复 register 会在退出时把 stop
            # 执行 N 遍（本身幂等，但白白堆积退出钩子）
            if not self._atexit_done:
                atexit.register(self.stop)
                self._atexit_done = True
        except Exception:
            self._dnssd = None

    def stop(self):
        """幂等：未启动、已停止、重复调用都安全。"""
        if getattr(self, "_dnssd", None) is not None:
            proc = self._dnssd
            self._dnssd = None
            try:
                proc.terminate()
                proc.wait(timeout=2)   # 回收子进程，否则留一个僵尸直到父进程退出
            except Exception:
                pass
        if self._httpd is not None:
            httpd = self._httpd
            self._httpd = None
            try:
                httpd.shutdown()       # 停 serve_forever 循环
            finally:
                # 必须显式关闭监听 socket：shutdown() 不关 fd，旧实现靠把
                # _httpd 置 None 后的引用计数回收兜底——一旦别处还持有引用
                # （或解释器 GC 行为不同）就是实打实的 fd 泄漏。
                httpd.server_close()
        self.port = None


if __name__ == "__main__":
    # 演示模式：无需菜单栏 app 即可给板子供数（联调用）。
    import time

    def _demo_snapshot():
        return {
            "v": CONTRACT_VERSION, "ts": int(time.time()),
            "services": [
                build_service_entry(
                    "Claude Code", "Max 5x",
                    [("5小时", 72), ("7 天", 45), ("Fable", 67)],
                    "重置 16:00 · 周四 08:00"),
                build_service_entry(
                    "CodeX", "Pro", [("5小时", 88), ("7 天", 31)],
                    "重置预测 24h 62% · 48h 91%"),
            ],
        }

    server = BoardLinkServer(_demo_snapshot)
    port = server.start()
    print(f"boardlink demo on http://0.0.0.0:{port}/quota.json")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.stop()
