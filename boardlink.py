#!/usr/bin/env python3
"""boardlink：把 ai-limit 的额度快照通过局域网暴露给桌面小屏（ES3C28P）。

设计约定（与板端固件的契约，改动需两头同步）：
- HTTP GET /quota.json 返回显示级 JSON：所有中文标签、重置行、预测行都在
  Mac 端算好，板子只负责画。这样文案与格式可以随 ai-limit 迭代而不必重烧固件。
- 端口用系统分配的临时端口，通过 Bonjour 服务 ``_ailimit._tcp`` 广播（名称
  ``ai-limit``），板子用 mDNS 服务发现拿 IP+端口，不存在写死 IP 的问题。
- 只监听局域网（0.0.0.0），无鉴权：数据是只读的额度百分比，不含任何凭据；
  与板子的信任边界同家庭 Wi-Fi。

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

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONTRACT_VERSION = 1
SERVICE_TYPE = "_ailimit._tcp."
SERVICE_NAME = "ai-limit"

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


class BoardLinkServer:
    """后台 HTTP 服务 + Bonjour 广播。snapshot_fn 由宿主注入，须线程安全可调。"""

    def __init__(self, snapshot_fn):
        self._snapshot_fn = snapshot_fn
        self._httpd = None
        self._netservice = None
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

        self._httpd = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
        self.port = self._httpd.server_address[1]
        threading.Thread(target=self._httpd.serve_forever,
                         name="boardlink-http", daemon=True).start()
        self._publish()
        return self.port

    def _publish(self):
        """Bonjour 广播。只在 macOS（菜单栏宿主）可用；失败不致命——
        板子会在下一次服务发现失败时报 error=discover，宿主日志可查。"""
        try:
            from Foundation import NSNetService
            self._netservice = NSNetService.alloc(
            ).initWithDomain_type_name_port_("", SERVICE_TYPE, SERVICE_NAME,
                                             self.port)
            self._netservice.publish()
        except Exception:
            self._netservice = None

    def stop(self):
        if self._netservice is not None:
            self._netservice.stop()
            self._netservice = None
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd = None


if __name__ == "__main__":
    # 演示模式：无需菜单栏 app 即可给板子供数（联调用）。
    # Bonjour 走 NSNetService；若在非 GUI 环境失败，可另开
    # `dns-sd -R ai-limit _ailimit._tcp . <port>` 手动广播。
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
