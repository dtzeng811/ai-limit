#!/usr/bin/env python3
"""IP 安全度检测 — 数据层（跨平台，无 UI 依赖）

复刻 https://ip.net.coffee/claude/ 页面自身调用的接口，拿到与网页显示同源的
数据，而不是另找第三方服务自行判定。接口是从页面源码逆向出来的（该站没有
公开 API 文档），三条链路都实测复现过：

1. https://claude.ai/cdn-cgi/trace          Claude 视角的出口 IP + 可用性
2. https://ip.net.coffee/api/iprisk/<ip>    IP 风险判定（网页信任分的原始数据）
   + /api/geoip/<ip>                        地理与 ISP
3. token 机制                                DNS 泄露：向 <token>-N.d.ip.net.coffee
   发起解析（页面用 <img> 触发，这里用 socket），该站自建权威 DNS 记下"谁来
   解析的"，再轮询 /api/dns/result/<token> 取回 DNS 出口列表

**不调用 /api/session**——那是页面用于"共享分析"的 IP 上报，与检测无关。

WebRTC 泄露必须由浏览器建 RTCPeerConnection 收集 ICE candidate，纯 HTTP
客户端无法模拟，本模块不做，由 UI 层如实标注「需浏览器检测」并引导跳转。

设计见 docs/superpowers/specs/2026-07-26-ip-security-design.md
"""
import json
import socket
import threading
import time
import urllib.error
import urllib.request

SITE_URL = "https://ip.net.coffee/claude/"   # UI 点击跳转的目标

_TRACE_URL   = "https://claude.ai/cdn-cgi/trace"
_API_BASE    = "https://ip.net.coffee"
_DNS_SUFFIX  = "d.ip.net.coffee"

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

_TRACE_TIMEOUT = 8
_API_TIMEOUT   = 12
_DNS_TIMEOUT   = 6

# 盾牌档位
SHIELD_OK   = "ok"      # 绿 · 勾
SHIELD_WARN = "warn"    # 黄 · 横线
SHIELD_CRIT = "crit"    # 红 · 感叹号
SHIELD_IDLE = "idle"    # 灰 · 圆点（检测中 / 连败）


def _get(url, timeout, as_json=True):
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept": "*/*",
        "Referer": SITE_URL,
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", "replace")
    return json.loads(body) if as_json else body


# ── 探针 1：Claude 视角出口 IP + 可用性 ──────────────────────────────────────
def probe_trace(timeout=_TRACE_TIMEOUT):
    """GET claude.ai/cdn-cgi/trace，返回 (reachable, info_dict)。

    Cloudflare 的 trace 端点返回 `k=v` 纯文本。一个请求同时回答两件事：
    claude.ai 通不通、以及 Claude 那边看到的是哪个出口 IP。
    """
    try:
        txt = _get(_TRACE_URL, timeout, as_json=False)
    except Exception as e:
        return False, {"error": f"{type(e).__name__}: {e}"}
    info = {}
    for line in txt.strip().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            info[k.strip()] = v.strip()
    if not info.get("ip"):
        return False, {"error": "trace: no ip field"}
    return True, info


# ── 探针 2：IP 风险 + 地理 ───────────────────────────────────────────────────
def probe_iprisk(ip, timeout=_API_TIMEOUT):
    """GET /api/iprisk/<ip>，网页那个信任评分就是基于这份数据算的。"""
    try:
        return _get(f"{_API_BASE}/api/iprisk/{ip}", timeout)
    except Exception:
        return None


def probe_geoip(ip, timeout=_API_TIMEOUT):
    try:
        return _get(f"{_API_BASE}/api/geoip/{ip}", timeout)
    except Exception:
        return None


# ── 探针 3：DNS 泄露 ─────────────────────────────────────────────────────────
# socket.setdefaulttimeout() 改的是**整个进程**的默认超时，不是当前线程的。
# menubar 同时跑着额度线程（期望 15s 超时）和 IP 线程，DNS 探针最坏要跑 50 秒——
# 这段时间里额度请求会被意外掐成 6 秒，慢网络下误判超时 → 记失败 → 最终误报 ⚠️，
# 正好击穿这个项目整套抖动抑制的设计目的。（getaddrinfo 不接受 timeout 参数，
# 全局开关是标准库唯一的入口，所以只能把污染窗口压到单次解析，并用锁串行化。）
_dns_lock = threading.Lock()


def _resolve_with_timeout(host, timeout=_DNS_TIMEOUT):
    """带超时地解析一个域名，解析完立刻把全局超时**还原成原值**（不是 None——
    调用方可能本来就设过别的值，无条件清零会顺手改坏它）。"""
    with _dns_lock:
        prev = socket.getdefaulttimeout()
        try:
            socket.setdefaulttimeout(timeout)
            socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        except Exception:
            pass                      # 无 A 记录必然抛错，查询已到达权威 DNS 即达目的
        finally:
            socket.setdefaulttimeout(prev)
def probe_dns(token=None, rounds=2, polls=3, poll_gap=1.5):
    """复刻页面的 token 机制，返回 (ok, dns_servers)。

    ok=False 表示这轮没测成（网络异常/接口挂了），与"测了但没有出口"是两回事：
    - ok=True,  servers=[]  → DNS 未暴露出口（加密 DNS 或走代理）= **安全**
    - ok=True,  servers=[…] → 拿到 DNS 出口，交给判定层比对国家
    - ok=False             → 本轮不可用，不参与判定（不能当成安全，也不算泄露）

    注意：本机开 Clash fake-IP 时 DNS 查询不出公网，servers 恒为空——这在
    该站判定里正是"安全"。早期用 bash.ws 把这种情况误判成"检测不可用"。
    """
    if token is None:
        token = f"{int(time.time() * 1000) % 10**10:010d}"
    # 触发解析：这些子域没有 A 记录，getaddrinfo 必然抛错，但查询已经到达
    # 该站的权威 DNS——这正是检测所需，异常要吞掉
    for i in range(1, rounds + 1):
        _resolve_with_timeout(f"{token}-{i}.{_DNS_SUFFIX}")

    got_response = False
    for attempt in range(polls):
        time.sleep(poll_gap)
        try:
            data = _get(f"{_API_BASE}/api/dns/result/{token}", _API_TIMEOUT)
        except Exception:
            continue
        got_response = True
        servers = data.get("dns_servers") or []
        if servers:
            return True, servers
    # 轮询通了但一直是空 → 确实没有 DNS 出口（安全）；一次都没通 → 不可用
    return (True, []) if got_response else (False, [])


# ── 判定 ─────────────────────────────────────────────────────────────────────
def _norm_country(v):
    return (v or "").strip().lower()


def decide(*, reachable, risk, dns_ok, dns_servers, ip_changed):
    """把探针结果合成盾牌档位。合并取最差。

    红：claude.ai 不可达 / DNS 真泄露 / IP 被标记 abuser
    黄：出口 IP 相比上次变化
    绿：其余

    机房 IP（is_datacenter）**不点亮盾牌**——用户长期走机房出口，若据此常亮
    黄灯则告警失去意义；它只在卡片里以标签注明。
    """
    if not reachable:
        return SHIELD_CRIT
    if risk and risk.get("is_abuser"):
        return SHIELD_CRIT
    # DNS 泄露：拿到了 DNS 出口，且国家与出口 IP 不一致
    if dns_ok and dns_servers:
        home = _norm_country(risk.get("country") if risk else None)
        for s in dns_servers:
            c = _norm_country(s.get("country") or s.get("country_name"))
            if home and c and c != home:
                return SHIELD_CRIT
    if ip_changed:
        return SHIELD_WARN
    return SHIELD_OK


def is_dns_leaked(risk, dns_ok, dns_servers):
    if not (dns_ok and dns_servers):
        return False
    home = _norm_country(risk.get("country") if risk else None)
    for s in dns_servers:
        c = _norm_country(s.get("country") or s.get("country_name"))
        if home and c and c != home:
            return True
    return False


# ── 对外主入口 ───────────────────────────────────────────────────────────────
_last_ip = None   # 进程内记忆，用于判断出口 IP 是否变化


def probe(*, with_dns=True):
    """跑一轮完整检测，返回 UI 可直接消费的扁平 dict。

    任何单个探针失败都不抛异常：
    - trace 失败        → level=crit（claude.ai 不可达是真问题）
    - ip.net.coffee 失败 → degraded=True，仍有 ip/可用性，风险与 DNS 显示不可用
    - DNS 探针失败       → dns_ok=False，不参与判定
    """
    global _last_ip
    out = {
        "level": SHIELD_IDLE, "reachable": False, "degraded": False, "error": None,
        "ip": None, "loc": None, "colo": None,
        "country": None, "city": None, "isp": None,
        "is_datacenter": False, "is_vpn": False, "is_proxy": False,
        "is_tor": False, "is_abuser": False, "abuser_score": None,
        "dns_ok": False, "dns_servers": [], "dns_leaked": False,
        "ip_changed": False, "checked_at": time.time(),
    }

    reachable, info = probe_trace()
    out["reachable"] = reachable
    if not reachable:
        out["error"] = info.get("error")
        out["level"] = SHIELD_CRIT
        return out

    ip = info.get("ip")
    out.update(ip=ip, loc=info.get("loc"), colo=info.get("colo"))
    out["ip_changed"] = bool(_last_ip and ip and _last_ip != ip)
    _last_ip = ip or _last_ip

    risk = probe_iprisk(ip)
    if risk is None:
        out["degraded"] = True
    else:
        out.update(
            country=risk.get("country"), city=risk.get("city"),
            isp=risk.get("asOrganization") or risk.get("company_name"),
            is_datacenter=bool(risk.get("is_datacenter")),
            is_vpn=bool(risk.get("is_vpn")), is_proxy=bool(risk.get("is_proxy")),
            is_tor=bool(risk.get("is_tor")), is_abuser=bool(risk.get("is_abuser")),
            abuser_score=risk.get("abuser_score"),
        )
        if not out["city"]:
            geo = probe_geoip(ip)
            if geo:
                out.update(country=out["country"] or geo.get("country"),
                           city=geo.get("city"), isp=out["isp"] or geo.get("isp"))

    if with_dns:
        dns_ok, servers = probe_dns()
        out["dns_ok"], out["dns_servers"] = dns_ok, servers
        out["dns_leaked"] = is_dns_leaked(risk, dns_ok, servers)

    out["level"] = decide(
        reachable=reachable, risk=risk,
        dns_ok=out["dns_ok"], dns_servers=out["dns_servers"],
        ip_changed=out["ip_changed"],
    )
    return out


if __name__ == "__main__":
    import pprint
    pprint.pprint(probe())
