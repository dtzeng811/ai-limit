#!/usr/bin/env python3
"""跑全部离线测试，并守住「测试不访问外部网络」这条底线。

这些测试是可独立执行的脚本（不是 pytest 用例），pytest 收集不到；且部分文件
要 Pillow / rumps，裸系统解释器跑不起来。用法：

    .venv/bin/python tests/run_all.py            # 跑全套
    .venv/bin/python tests/run_all.py -k boardlink   # 只跑名字含 boardlink 的

退出码非 0 表示有测试失败或有外部网络访问——可以直接接进 CI。
"""
import argparse
import contextlib
import io
import pathlib
import runpy
import socket
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

_LOCAL = {"127.0.0.1", "::1", "localhost"}


class _NetGuard:
    """记录所有出站连接与域名解析。回环放行（测试会起自己的 HTTP 服务），
    任何外部访问都记下来并让整轮失败。"""

    def __init__(self):
        self.external = []
        self.loopback = 0
        self._conn = socket.socket.connect
        self._conn_ex = socket.socket.connect_ex
        self._gai = socket.getaddrinfo

    def _note(self, addr):
        host = addr[0] if isinstance(addr, tuple) and addr else str(addr)
        if host in _LOCAL:
            self.loopback += 1
        else:
            self.external.append(str(host))

    def __enter__(self):
        guard = self

        def _connect(sock, addr, *a, **k):
            guard._note(addr)
            return guard._conn(sock, addr, *a, **k)

        def _connect_ex(sock, addr, *a, **k):
            guard._note(addr)
            return guard._conn_ex(sock, addr, *a, **k)

        def _getaddrinfo(host, *a, **k):
            if host not in _LOCAL and host:
                guard.external.append(f"DNS:{host}")
            return guard._gai(host, *a, **k)

        socket.socket.connect = _connect
        socket.socket.connect_ex = _connect_ex
        socket.getaddrinfo = _getaddrinfo
        return self

    def __exit__(self, *exc):
        socket.socket.connect = self._conn
        socket.socket.connect_ex = self._conn_ex
        socket.getaddrinfo = self._gai
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", dest="pattern", default="", help="只跑名字包含该子串的测试")
    ap.add_argument("-v", dest="verbose", action="store_true", help="失败时打印完整输出")
    args = ap.parse_args()

    files = sorted(p for p in (_ROOT / "tests").glob("test_*.py")
                   if args.pattern in p.name)
    if not files:
        print("没有匹配的测试文件")
        return 1

    failed = []
    with _NetGuard() as guard:
        for path in files:
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                    runpy.run_path(str(path), run_name="__main__")
                code = 0
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 0
            except Exception as e:  # noqa: BLE001
                buf.write(f"\n{type(e).__name__}: {e}\n")
                code = 1
            out = buf.getvalue()
            n_ok = out.count("✓")
            if code == 0:
                print(f"  ✓ {path.name:28} {n_ok} 项")
            else:
                failed.append(path.name)
                print(f"  ✗ {path.name:28} 失败")
                tail = [ln for ln in out.splitlines() if "✗" in ln or "FAILED" in ln]
                for ln in (out.splitlines()[-15:] if args.verbose else tail[:8]):
                    print(f"      {ln}")

    print()
    print(f"  回环连接 {guard.loopback} 次（测试自建服务，允许）")
    if guard.external:
        uniq = sorted(set(guard.external))
        print(f"  ✗ 检测到 {len(guard.external)} 次外部网络访问: {uniq[:6]}")
        print("    测试套件必须默认离线——请为对应调用打桩。")
    else:
        print("  ✓ 零外部网络访问")

    if failed:
        print(f"\nFAILED: {', '.join(failed)}")
    else:
        print(f"\nALL PASS（{len(files)} 个文件）")
    return 1 if (failed or guard.external) else 0


if __name__ == "__main__":
    sys.exit(main())
