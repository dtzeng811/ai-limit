#!/usr/bin/env python3
"""ipsec 判定逻辑离线单测——不发任何网络请求，覆盖判定表全部分支。

跑法：python3 tests/test_ipsec.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import ipsec  # noqa: E402

FAILS = []


def check(name, got, want):
    ok = got == want
    if not ok:
        FAILS.append(name)
    print(f"  {'✓' if ok else '✗'} {name:52} {got}" + ("" if ok else f"  (期望 {want})"))


US = {"country": "United States", "is_abuser": False}
# ⚠️ 接口实际返回的是**纯 IP 字符串数组**（实测 ["162.158.185.30", ...]），
# 不是带国家的 dict。早期这里用自己编的 dict 结构测，于是"测试全绿但线上
# 一遇到非空 dns_servers 就崩"。下面两组用真实形态，国家靠打桩 geoip 补查。
CN_DNS = ["114.114.114.114"]
US_DNS = ["8.8.8.8"]
_GEO = {"114.114.114.114": {"country": "China"},
        "8.8.8.8": {"country": "United States"},
        "1.1.1.1": {"country": "China"}}
_real_geoip = ipsec.probe_geoip
ipsec.probe_geoip = lambda ip, *a, **k: _GEO.get(ip)

print("\n【判定表】")
check("不可达 → 红", ipsec.decide(
    reachable=False, risk=None, dns_ok=False, dns_servers=[], ip_changed=False), ipsec.SHIELD_CRIT)

check("abuser → 红", ipsec.decide(
    reachable=True, risk={"country": "US", "is_abuser": True},
    dns_ok=True, dns_servers=[], ip_changed=False), ipsec.SHIELD_CRIT)

check("DNS 出口异国 → 红", ipsec.decide(
    reachable=True, risk=US, dns_ok=True, dns_servers=CN_DNS, ip_changed=False), ipsec.SHIELD_CRIT)

check("DNS 出口同国 → 绿", ipsec.decide(
    reachable=True, risk=US, dns_ok=True, dns_servers=US_DNS, ip_changed=False), ipsec.SHIELD_OK)

check("DNS 空(未暴露) → 绿", ipsec.decide(
    reachable=True, risk=US, dns_ok=True, dns_servers=[], ip_changed=False), ipsec.SHIELD_OK)

check("IP 变化 → 黄", ipsec.decide(
    reachable=True, risk=US, dns_ok=True, dns_servers=[], ip_changed=True), ipsec.SHIELD_OK if False else ipsec.SHIELD_WARN)

check("机房 IP 不点亮(仍绿)", ipsec.decide(
    reachable=True, risk={"country": "US", "is_datacenter": True, "is_abuser": False},
    dns_ok=True, dns_servers=[], ip_changed=False), ipsec.SHIELD_OK)

check("DNS 探针失败不误判(绿)", ipsec.decide(
    reachable=True, risk=US, dns_ok=False, dns_servers=[], ip_changed=False), ipsec.SHIELD_OK)

check("红优先于黄(泄露+IP变化)", ipsec.decide(
    reachable=True, risk=US, dns_ok=True, dns_servers=CN_DNS, ip_changed=True), ipsec.SHIELD_CRIT)

check("风险数据缺失(降级)不崩", ipsec.decide(
    reachable=True, risk=None, dns_ok=True, dns_servers=CN_DNS, ip_changed=False), ipsec.SHIELD_OK)

print("\n【dns_leaked 判定】")
check("异国 → 泄露", ipsec.is_dns_leaked(US, True, CN_DNS), True)
check("同国 → 不泄露", ipsec.is_dns_leaked(US, True, US_DNS), False)
check("空列表 → 不泄露", ipsec.is_dns_leaked(US, True, []), False)
check("探针失败 → 不泄露", ipsec.is_dns_leaked(US, False, CN_DNS), False)
check("dict 形态仍兼容(country_name)", ipsec.is_dns_leaked(
    US, True, [{"ip": "1.1.1.1", "country_name": "China"}]), True)
check("字符串形态(真实结构)→ 查 geoip 判国", ipsec.is_dns_leaked(US, True, CN_DNS), True)
check("dns_server_ip 取字符串", ipsec.dns_server_ip("8.8.8.8"), "8.8.8.8")
check("dns_server_ip 取 dict", ipsec.dns_server_ip({"ip": "1.2.3.4"}), "1.2.3.4")
check("dns_server_ip 容 None", ipsec.dns_server_ip(None), "")
check("本机国家未知 → 不判泄露(宁漏勿误)",
      ipsec.is_dns_leaked({"country": None}, True, CN_DNS), False)
check("geoip 查不到国家 → 不判泄露",
      ipsec.is_dns_leaked(US, True, ["203.0.113.9"]), False)
check("大小写/空格不敏感", ipsec.is_dns_leaked(
    {"country": " united states "}, True, [{"ip": "8.8.8.8", "country": "UNITED STATES"}]), False)
ipsec.probe_geoip = _real_geoip

print("\n【probe() 降级路径（打桩，不联网）】")
_orig = (ipsec.probe_trace, ipsec.probe_iprisk, ipsec.probe_geoip, ipsec.probe_dns)

# trace 失败
ipsec.probe_trace = lambda *a, **k: (False, {"error": "URLError: timed out"})
r = ipsec.probe()
check("trace 失败 → level=crit", r["level"], ipsec.SHIELD_CRIT)
check("trace 失败 → 带 error", bool(r["error"]), True)

# trace 成功但 ip.net.coffee 挂了
ipsec.probe_trace = lambda *a, **k: (True, {"ip": "1.2.3.4", "loc": "US", "colo": "LAX"})
ipsec.probe_iprisk = lambda *a, **k: None
ipsec.probe_geoip = lambda *a, **k: None
ipsec.probe_dns = lambda *a, **k: (False, [])
ipsec._last_ip = None
r = ipsec.probe()
check("iprisk 挂 → degraded=True", r["degraded"], True)
check("iprisk 挂 → 仍有 ip", r["ip"], "1.2.3.4")
check("iprisk 挂 → 仍 reachable", r["reachable"], True)
check("iprisk 挂 → 不误报红", r["level"], ipsec.SHIELD_OK)

# 完整成功路径 + IP 变化
ipsec.probe_iprisk = lambda *a, **k: {
    "country": "United States", "city": "Los Angeles", "asOrganization": "IT7",
    "is_datacenter": True, "is_abuser": False, "abuser_score": "0.0039 (Low)"}
ipsec.probe_dns = lambda *a, **k: (True, [])
ipsec._last_ip = None
r1 = ipsec.probe()
check("首轮无变化 → 绿", r1["level"], ipsec.SHIELD_OK)
check("机房标记透传", r1["is_datacenter"], True)
check("滥用分透传", r1["abuser_score"], "0.0039 (Low)")

ipsec.probe_trace = lambda *a, **k: (True, {"ip": "9.9.9.9", "loc": "JP", "colo": "NRT"})
r2 = ipsec.probe()
check("IP 变了 → 黄", r2["level"], ipsec.SHIELD_WARN)
check("ip_changed=True", r2["ip_changed"], True)

# DNS 泄露完整路径
ipsec.probe_dns = lambda *a, **k: (True, [{"ip": "114.114.114.114", "country": "China"}])
r3 = ipsec.probe()
check("DNS 泄露 → 红", r3["level"], ipsec.SHIELD_CRIT)
check("dns_leaked=True", r3["dns_leaked"], True)

# with_dns=False 不跑 DNS 探针
called = {"n": 0}
def _spy(*a, **k):
    called["n"] += 1
    return True, []
ipsec.probe_dns = _spy
ipsec.probe(with_dns=False)
check("with_dns=False 跳过 DNS 探针", called["n"], 0)

ipsec.probe_trace, ipsec.probe_iprisk, ipsec.probe_geoip, ipsec.probe_dns = _orig

print("\n【DNS 探针不污染全局 socket 超时（回归护栏）】")
import socket as _sock, threading as _th
_sock.setdefaulttimeout(30)                      # 模拟调用方设过别的超时
ipsec._resolve_with_timeout("no-such-host.invalid")
check("解析后还原调用方原值（不是清成 None）", _sock.getdefaulttimeout(), 30)
_sock.setdefaulttimeout(None)
ipsec._resolve_with_timeout("no-such-host.invalid")
check("原值为 None 时也正确还原", _sock.getdefaulttimeout(), None)

_errs = []
def _hammer():
    try:
        for _ in range(3):
            ipsec._resolve_with_timeout("no-such-host.invalid")
    except Exception as e:
        _errs.append(e)
_ts = [_th.Thread(target=_hammer) for _ in range(4)]
[t.start() for t in _ts]; [t.join() for t in _ts]
check("4 线程并发解析无异常", len(_errs), 0)
check("并发后无残留污染", _sock.getdefaulttimeout(), None)

print("\n" + ("FAILED: " + ", ".join(FAILS) if FAILS else "ALL PASS"))
sys.exit(1 if FAILS else 0)
