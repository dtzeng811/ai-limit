#!/usr/bin/env python3
"""boardlink 契约 / 并发 / 异常 / 启停 / 资源释放测试。

只打本机回环到我们自己起的服务，**不访问任何外部网络**。

跑法：python3 tests/test_boardlink.py
"""
import json
import os
import pathlib
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import boardlink  # noqa: E402

FAILS = []


def check(name, got, want):
    ok = got == want
    if not ok:
        FAILS.append(name)
    print(f"  {'✓' if ok else '✗'} {name:52} {got!r}" + ("" if ok else f"  (期望 {want!r})"))


def check_true(name, cond, detail=""):
    check(name, bool(cond), True) if not detail or cond else None
    if detail and not cond:
        print(f"      详情: {detail}")


def _fd_count():
    """当前进程打开的 fd 数。macOS/Linux 都有 /dev/fd。"""
    try:
        return len(os.listdir("/dev/fd"))
    except OSError:
        return -1


def _get(port, path="/quota.json", timeout=5):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
        return r.status, r.read().decode("utf-8")


# ── 契约：字节截断与百分比归一 ───────────────────────────────────────────────
print("\n【契约：_clip 按 UTF-8 字节截断】")
check("短串原样返回", boardlink._clip("Claude Code", "name"), "Claude Code")
check("None → 空串", boardlink._clip(None, "name"), "")
check("空串 → 空串", boardlink._clip("", "plan"), "")
# plan 上限 15 字节：中文 3 字节/字 → 5 个字正好 15
check("中文正好卡满不截", boardlink._clip("一二三四五", "plan"), "一二三四五")
check("中文超限按字截，不切碎", boardlink._clip("一二三四五六", "plan"), "一二三四五")
_clipped = boardlink._clip("一二三四五六", "plan")
check("截断结果可正常编码", len(_clipped.encode("utf-8")) <= 15, True)
check("ASCII 超限按字节截", boardlink._clip("A" * 20, "plan"), "A" * 15)
check("混合中英不切碎", boardlink._clip("Max一二三四五", "plan").encode("utf-8").decode("utf-8") is not None, True)
check("footer 上限 47", len(boardlink._clip("重" * 30, "footer").encode("utf-8")) <= 47, True)

print("\n【契约：_pct 百分比归一】")
check("正常值", boardlink._pct(72), 72)
check("浮点四舍五入", boardlink._pct(66.6), 67)
check("字符串数字可解析", boardlink._pct("45"), 45)
check("None → -1（板端画空条）", boardlink._pct(None), -1)
check("非数字 → -1", boardlink._pct("abc"), -1)
check("负数夹到 0", boardlink._pct(-5), 0)
check("超 100 夹到 100", boardlink._pct(150), 100)

print("\n【契约：build_service_entry】")
_e = boardlink.build_service_entry(
    "Claude Code", "Max 5x",
    [("5小时", 72), ("7 天", 45), ("Fable", 67), ("多余行", 1)],
    "重置 16:00")
check("rows 最多 3 条", len(_e["rows"]), 3)
check("第 4 行被丢弃", [r["label"] for r in _e["rows"]], ["5小时", "7 天", "Fable"])
check("plan=None → 空串", boardlink.build_service_entry("X", None, [], None)["plan"], "")
check("footer=None → 空串", boardlink.build_service_entry("X", None, [], None)["footer"], "")
check("空 rows 不崩", boardlink.build_service_entry("X", "P", [], "F")["rows"], [])
_e2 = boardlink.build_service_entry("X", "P", [("a", None)], "F")
check("row 内 None 余量 → -1", _e2["rows"][0]["left"], -1)

# ── HTTP 行为 ────────────────────────────────────────────────────────────────
print("\n【HTTP：路由与响应】")
SNAP = {"v": 1, "ts": 1234567890, "services": [
    boardlink.build_service_entry("Claude Code", "Max", [("5小时", 72)], "重置 16:00")]}
srv = boardlink.BoardLinkServer(lambda: SNAP)
port = srv.start()
check("start() 返回端口", isinstance(port, int) and port > 0, True)
try:
    status, body = _get(port)
    check("/quota.json → 200", status, 200)
    _parsed = json.loads(body)
    check("返回契约 v1", _parsed["v"], 1)
    check("服务条目透传", _parsed["services"][0]["name"], "Claude Code")
    check("中文不转义（ensure_ascii=False）", "重置" in body, True)
    status_q, _ = _get(port, "/quota.json?cache=0")
    check("带 query 仍命中路由", status_q, 200)

    code = None
    try:
        _get(port, "/etc/passwd")
    except urllib.error.HTTPError as e:
        code = e.code
    check("未知路径 → 404", code, 404)

    code = None
    try:
        _get(port, "/../../etc/passwd")
    except urllib.error.HTTPError as e:
        code = e.code
    check("路径穿越 → 404", code, 404)

    # 【异常】快照函数抛异常时必须给空态而不是 500/挂死
    boom = boardlink.BoardLinkServer(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    bport = boom.start()
    try:
        bstatus, bbody = _get(bport)
        check("快照抛异常仍 200", bstatus, 200)
        check("异常时给空 services", json.loads(bbody)["services"], [])
    finally:
        boom.stop()

    # 【并发】多线程同时打，全部成功且响应完整
    print("\n【并发】")
    results, errors = [], []

    def _hammer():
        try:
            s, b = _get(port)
            results.append((s, json.loads(b)["v"]))
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    ts = [threading.Thread(target=_hammer) for _ in range(12)]
    [t.start() for t in ts]
    [t.join(timeout=10) for t in ts]
    check("12 并发请求无异常", errors, [])
    check("12 并发全部 200", results.count((200, 1)), 12)
finally:
    srv.stop()

print("\n【只服务局域网私有地址：非私网来源一律拒绝】")
# 监听 0.0.0.0 是板子发现所必需的（板端靠 mDNS 拿 IP+端口），但不该把额度数据
# 端给任何能路由到本机的来源。这里在连接层就挡掉非私网地址——纵深防御，
# 不改板端契约（板子在同一局域网，地址必然是私网）。
check("私网 10.x 放行", boardlink.is_allowed_client(("10.0.1.23", 5000)), True)
check("私网 192.168.x 放行", boardlink.is_allowed_client(("192.168.1.7", 5000)), True)
check("私网 172.16-31 放行", boardlink.is_allowed_client(("172.20.3.4", 5000)), True)
check("回环放行", boardlink.is_allowed_client(("127.0.0.1", 5000)), True)
check("链路本地放行（自组网）", boardlink.is_allowed_client(("169.254.9.9", 5000)), True)
check("IPv6 回环放行", boardlink.is_allowed_client(("::1", 5000)), True)
check("IPv6 唯一本地地址放行", boardlink.is_allowed_client(("fd00::1", 5000)), True)
check("公网地址拒绝", boardlink.is_allowed_client(("8.8.8.8", 5000)), False)
check("公网 IPv6 拒绝", boardlink.is_allowed_client(("2001:4860::1", 5000)), False)
check("畸形地址拒绝（不放行未知）", boardlink.is_allowed_client(("not-an-ip", 5000)), False)
check("空地址拒绝", boardlink.is_allowed_client(None), False)

print("\n【连接数上限与超时：防同网设备耗尽线程】")
# 旧实现：ThreadingHTTPServer 每条 TCP 连接开一个线程，handler 无超时，
# 连接不发数据就永远占着。同网任何设备开几千条连接即可耗尽整个菜单栏 App
# 的线程（macOS kern.num_taskthreads = 8192），App 直接瘫。
_slow = boardlink.BoardLinkServer(lambda: SNAP)
_sport = _slow.start()
try:
    check("有连接上限常量", isinstance(boardlink.MAX_CONNECTIONS, int)
          and 0 < boardlink.MAX_CONNECTIONS <= 64, True)
    check("有连接超时常量", isinstance(boardlink.CONNECTION_TIMEOUT, (int, float))
          and 0 < boardlink.CONNECTION_TIMEOUT <= 60, True)

    th0 = threading.active_count()
    hogs = []
    # 开 MAX+8 条连接且**一个字节都不发**，模拟慢速攻击
    for _ in range(boardlink.MAX_CONNECTIONS + 8):
        try:
            c = socket.create_connection(("127.0.0.1", _sport), timeout=2)
            hogs.append(c)
        except OSError:
            pass
    time.sleep(0.6)
    grew = threading.active_count() - th0
    print(f"      开了 {len(hogs)} 条静默连接 → 线程增长 {grew}")
    check("线程增长受上限约束（不随连接数线性增长）",
          grew <= boardlink.MAX_CONNECTIONS + 2, True)

    # 上限之内的正常请求仍必须能服务
    for c in hogs[:boardlink.MAX_CONNECTIONS]:
        try:
            c.close()
        except OSError:
            pass
    time.sleep(0.5)
    st, body = _get(_sport, timeout=6)
    check("清掉占用后服务照常可用", st, 200)
    for c in hogs:
        try:
            c.close()
        except OSError:
            pass
finally:
    _slow.stop()

print("\n【孤儿 dns-sd 回收：只认 PPID=1，绝不误杀活实例的子进程】")
# App 被 SIGKILL / pkill 时 atexit 不执行，dns-sd 子进程被 launchd 收养，
# 继续广播一个已经死掉的端口——板子会发现这个僵尸服务却连不上。实测复现过。
_PS = [
    (58830, 1,     "/usr/bin/dns-sd -R ai-limit _ailimit._tcp . 60772"),   # 孤儿
    (70309, 70306, "/usr/bin/dns-sd -R ai-limit _ailimit._tcp . 61652"),   # 活实例的子进程
    (12345, 1,     "/usr/bin/dns-sd -R other-app _other._tcp . 5000"),     # 别人的服务
    (23456, 1,     "/usr/bin/dns-sd -B _ailimit._tcp"),                    # 浏览不是注册
]
check("只挑出孤儿的注册进程", boardlink._stale_dnssd_pids(_PS), [58830])
check("空输入不崩", boardlink._stale_dnssd_pids([]), [])
check("只有活实例时不回收任何东西",
      boardlink._stale_dnssd_pids([(70309, 70306, "/usr/bin/dns-sd -R ai-limit _ailimit._tcp . 1")]), [])
check("不碰其他应用的 Bonjour 注册",
      boardlink._stale_dnssd_pids([(1, 1, "/usr/bin/dns-sd -R foo _foo._tcp . 1")]), [])

print("\n【启停与幂等】")
# 关键回归：stop() 必须**主动关闭监听 socket**，不能指望 GC。
# 旧实现只调 shutdown()（停 serve_forever 循环）就把 _httpd 置 None，socket 的
# 关闭全靠引用计数回收——一旦有别处持有引用（或换个 GC 行为的解释器），fd 就泄漏。
_s3 = boardlink.BoardLinkServer(lambda: SNAP)
_s3.start()
_held = _s3._httpd.socket          # 故意持有引用，挡住 GC 兜底
_s3.stop()
check("stop() 主动关闭监听 socket（不靠 GC）", _held.fileno(), -1)
check("stop() 后 _httpd 置空", _s3._httpd, None)
_s3.stop()
check("未启动/已停止时再 stop 不抛", True, True)
_s4 = boardlink.BoardLinkServer(lambda: SNAP)
_s4.stop()
check("从未 start 就 stop 不抛", True, True)


check("stop() 后端口不再可连", _port_closed := (lambda p: (lambda s: (s.settimeout(1),
      s.connect_ex(("127.0.0.1", p)) != 0, s.close())[1])(socket.socket()))(port), True)
srv.stop()  # 二次 stop 不应抛异常
check("stop() 可重复调用不抛", True, True)

_s2 = boardlink.BoardLinkServer(lambda: SNAP)
_p2 = _s2.start()
check("新实例可再次启动", isinstance(_p2, int) and _p2 > 0, True)
_s2.stop()

# ── 资源释放：反复启停不泄漏 fd ──────────────────────────────────────────────
print("\n【资源释放：反复启停不泄漏 fd / 线程】")
for _ in range(3):                       # 预热，排除首轮的一次性分配
    _w = boardlink.BoardLinkServer(lambda: SNAP)
    _w.start()
    _w.stop()
time.sleep(0.3)
fd_before = _fd_count()
th_before = threading.active_count()
for _ in range(10):
    s = boardlink.BoardLinkServer(lambda: SNAP)
    s.start()
    s.stop()
time.sleep(0.5)
fd_after = _fd_count()
th_after = threading.active_count()
print(f"      fd: {fd_before} → {fd_after} | 线程: {th_before} → {th_after}")
check("10 轮启停后 fd 不增长（允许 ±2 抖动）", fd_after - fd_before <= 2, True)
check("10 轮启停后线程不增长（允许 +1）", th_after - th_before <= 1, True)

print("\n" + ("FAILED: " + ", ".join(FAILS) if FAILS else "ALL PASS"))
sys.exit(1 if FAILS else 0)
