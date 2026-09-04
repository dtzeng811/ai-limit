#!/usr/bin/env python3
"""请求预算测试：断言「一轮刷新最多打几个请求」，防止请求量悄悄涨回去。

**全程打桩，不发任何真实网络请求**——所有 live_* / fetch_* 都被替换成计数器。

跑法：python3 tests/test_request_budget.py
"""
import importlib.util
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "menubar"))

import quotacore  # noqa: E402
import usage  # noqa: E402

_spec = importlib.util.spec_from_file_location("mbapp", _ROOT / "menubar" / "ai-limit-app.py")
app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(app)

FAILS = []


def check(name, got, want):
    ok = got == want
    if not ok:
        FAILS.append(name)
    print(f"  {'✓' if ok else '✗'} {name:54} {got!r}" + ("" if ok else f"  (期望 {want!r})"))


# ── 打桩：把所有出网函数换成计数器 ──────────────────────────────────────────
N = {"usage": 0, "plan": 0, "codex": 0, "status": 0, "forecast": 0}


def _reset():
    for k in N:
        N[k] = 0


usage.live_claude_usage = lambda *a, **k: (N.__setitem__("usage", N["usage"] + 1),
                                           {"five_hour": {"utilization": 12.0, "resets_at": None},
                                            "seven_day": {"utilization": 21.0, "resets_at": None},
                                            "limits": []})[1]
usage.live_claude_plan = lambda *a, **k: (N.__setitem__("plan", N["plan"] + 1), "max")[1]
usage.live_codex_web_usage = lambda *a, **k: (N.__setitem__("codex", N["codex"] + 1),
                                              (0, {"plan_type": "pro", "primary": None, "secondary": None}))[1]
usage.fetch_status_components = lambda *a, **k: (N.__setitem__("status", N["status"] + 1),
                                                 [{"id": "x", "name": "Claude Code", "status": "operational"}])[1]
# app 模块 import 时把这些名字绑到了自己的命名空间，要一并替换
app.live_claude_usage = usage.live_claude_usage
app.live_claude_plan = usage.live_claude_plan
app.live_codex_web_usage = usage.live_codex_web_usage
app.fetch_status_components = usage.fetch_status_components


def _fresh_state():
    """清空所有跨轮缓存，模拟"冷启动的一轮"。"""
    quotacore._plan_cache.update({"plan": None, "ts": 0.0})
    quotacore._plan_retry_at = 0.0
    quotacore._plan_fail = 0
    app._STATUS_CACHES.clear()
    _reset()


# ── 一轮刷新的请求预算 ──────────────────────────────────────────────────────
print("\n【单轮刷新请求预算】")
_fresh_state()
app._fetch_claude("zh")
check("Claude 额度：1 次 usage 请求", N["usage"], 1)
check("Claude 额度：首轮附带 1 次套餐查询", N["plan"], 1)

app._fetch_claude("zh")
check("第 2 轮不再查套餐（12h 缓存）", N["plan"], 1)
check("第 2 轮仍查额度（额度必须实时）", N["usage"], 2)

for _ in range(18):                       # 凑满一小时 20 轮
    app._fetch_claude("zh")
check("1 小时 20 轮 → 20 次额度请求", N["usage"], 20)
check("1 小时 20 轮 → 仍只有 1 次套餐请求", N["plan"], 1)

print("\n【状态页 TTL：这是本次优化省下的大头】")
_fresh_state()
app._fetch_status(app.CLAUDE_STATUS_COMPONENTS_URL)
check("首次穿透", N["status"], 1)
for _ in range(19):                       # 一小时内其余 19 轮
    app._fetch_status(app.CLAUDE_STATUS_COMPONENTS_URL)
check("1 小时 20 轮内只穿透 1 次（TTL=10min）", N["status"], 1)
check("两个端点各自独立缓存",
      (app._fetch_status(app.CODEX_STATUS_COMPONENTS_URL), N["status"])[1], 2)

# TTL 到期后应重新取；用假时钟避免真等 10 分钟
_c = app._status_cache(app.CLAUDE_STATUS_COMPONENTS_URL)
_c._ts -= app._STATUS_TTL_SEC + 1
app._fetch_status(app.CLAUDE_STATUS_COMPONENTS_URL)
check("TTL 过期后重新取", N["status"], 3)

print("\n【状态页失败不硬撞】")
_fresh_state()
app.fetch_status_components = lambda *a, **k: (N.__setitem__("status", N["status"] + 1), None)[1]
r = app._fetch_status(app.CLAUDE_STATUS_COMPONENTS_URL)
check("失败如实返回 unknown（不用旧值伪装）", r, "unknown")
check("失败发了 1 次", N["status"], 1)
for _ in range(19):
    app._fetch_status(app.CLAUDE_STATUS_COMPONENTS_URL)
check("失败后 1 小时内不再连发（退避）", N["status"], 1)
app.fetch_status_components = usage.fetch_status_components

print("\n【套餐查询失败不硬撞】")
_fresh_state()
_boom = {"n": 0}


def _plan_boom():
    _boom["n"] += 1
    raise RuntimeError("401")


app.live_claude_plan = _plan_boom
app._cached_claude_plan()
check("首次失败发了 1 次", _boom["n"], 1)
for _ in range(19):
    app._cached_claude_plan()
check("失败后 1 小时 20 轮内不再连发（退避）", _boom["n"], 1)
app.live_claude_plan = usage.live_claude_plan

# ── 错误分类端到端：异常 → 错误字典 → AbsorbState 退避 ─────────────────────
print("\n【错误分类端到端】")
_fresh_state()


def _raise(exc):
    def _f(*a, **k):
        raise exc
    return _f


# 401：登录态失效。旧实现会每 3 分钟继续撞到用户重新登录为止
app.live_claude_usage = _raise(usage.ClaudeWebError("登录失效", kind="auth", status=401))
d = app._fetch_claude("zh")
check("401 → 错误字典带 auth 分类", d.get("error_kind"), "auth")
st = quotacore.AbsorbState(lambda: 180)
st.absorb({"5h_left": 50})
st.absorb(d)
check("401 首次即退避（不等连败 3 次）", st.in_backoff(), True)

# 429 且服务端给了 Retry-After：必须尊重服务端说的时长
app.live_claude_usage = _raise(
    usage.ClaudeWebError("限流", kind="rate_limit", status=429, retry_after="600"))
d429 = app._fetch_claude("zh")
check("429 → 错误字典带 rate_limit 分类", d429.get("error_kind"), "rate_limit")
check("429 → Retry-After 被解析进字典", d429.get("retry_after_sec"), 600.0)
st429 = quotacore.AbsorbState(lambda: 180)
st429.absorb({"5h_left": 50})
st429.absorb(d429)
import time as _t
check("429 退避时长尊重 Retry-After",
      580 <= st429.backoff_until - _t.time() <= 620, True)

# Cloudflare 挑战归入 auth：继续撞只会被拦得更死
app.live_claude_usage = _raise(
    usage.ClaudeWebError("人机验证", kind="cloudflare", status=403))
dcf = app._fetch_claude("zh")
check("Cloudflare → 归入 auth（不硬撞）", dcf.get("error_kind"), "auth")

# 普通网络错误保持原语义：不带分类，走连败 3 次的抖动吸收
app.live_claude_usage = _raise(usage.ClaudeWebError("HTTP 500", status=500))
d500 = app._fetch_claude("zh")
check("5xx 不标 auth/rate_limit（保持抖动吸收）", d500.get("error_kind"), None)
stn = quotacore.AbsorbState(lambda: 180)
stn.absorb({"5h_left": 50})
stn.absorb(d500)
check("5xx 第 1 次不退避", stn.in_backoff(), False)

app.live_claude_usage = usage.live_claude_usage

# ── macOS 那份独立的 _absorb_fetch 也必须吃到错误分类 ───────────────────────
# 分类逻辑最初只写进 quotacore.AbsorbState，而 macOS 主力平台走的是自己那份
# _absorb_fetch —— 等于在最重要的平台上完全不生效。现在两边共用
# quotacore.failure_backoff_sec，这里把它钉死。
print("\n【macOS _absorb_fetch 与 AbsorbState 行为一致】")


class _StubApp:
    """只带 _absorb_fetch 需要的那几个字段，不启动 rumps。"""
    def __init__(self):
        self._svc_fail = {"claude": 0, "codex": 0}
        self._svc_good_ts = {"claude": 0.0, "codex": 0.0}
        self._svc_backoff_until = {"claude": 0.0, "codex": 0.0}

    def _refresh_sec(self):
        return 180

    _absorb_fetch = app.AiLimitApp._absorb_fetch


import time as _t2
stub = _StubApp()
stub._absorb_fetch("claude", {"5h_left": 50}, None)
stub._absorb_fetch("claude", {"error": "登录失效", "error_kind": "auth"}, {"5h_left": 50})
check("_absorb_fetch：401 首次即退避",
      stub._svc_backoff_until["claude"] > _t2.time(), True)
check("_absorb_fetch：认证退避 ≥ AUTH_BACKOFF_SEC",
      stub._svc_backoff_until["claude"] - _t2.time() >= quotacore.AUTH_BACKOFF_SEC - 5, True)

stub2 = _StubApp()
stub2._absorb_fetch("codex", {"5h_left": 50}, None)
stub2._absorb_fetch("codex", {"error": "限流", "error_kind": "rate_limit",
                              "retry_after_sec": 300}, {"5h_left": 50})
check("_absorb_fetch：429 尊重 Retry-After",
      280 <= stub2._svc_backoff_until["codex"] - _t2.time() <= 320, True)

stub3 = _StubApp()
stub3._absorb_fetch("claude", {"5h_left": 50}, None)
stub3._absorb_fetch("claude", {"error": "超时"}, {"5h_left": 50})
check("_absorb_fetch：普通失败第 1 次不退避（原语义不变）",
      stub3._svc_backoff_until["claude"] <= _t2.time(), True)
stub3._absorb_fetch("claude", {"error": "超时"}, {"5h_left": 50})
stub3._absorb_fetch("claude", {"error": "超时"}, {"5h_left": 50})
check("_absorb_fetch：连败 3 次才退避（原语义不变）",
      stub3._svc_backoff_until["claude"] > _t2.time(), True)

# ── UI 渲染路径必须零网络请求 ────────────────────────────────────────────────
print("\n【UI 渲染路径零网络】")
import ipsec as _ipsec
_geo_hits = {"n": 0}
_real_geoip = _ipsec.probe_geoip


def _count_geoip(ip, *a, **k):
    _geo_hits["n"] += 1
    return {"country": "China"}


_ipsec.probe_geoip = _count_geoip
# 泄露态：这是唯一会去查国家的分支
_leaked = {
    "level": "crit", "reachable": True, "ip": "1.2.3.4", "loc": "US",
    "city": "SJ", "is_datacenter": True, "abuser_score": "0.0039 (Low)",
    "dns_ok": True, "dns_servers": ["114.114.114.114", "223.5.5.5"],
    "dns_leaked": True, "degraded": False, "error": None, "ip_changed": False,
}
_rows = app._ip_card_rows(_leaked, "zh")
check("面板渲染不发 geoip 请求（UI 线程 12s 超时会卡死界面）", _geo_hits["n"], 0)
check("仍能画出 DNS 行", any(r["label"] == "DNS" for r in _rows), True)
# 重绘 10 次也必须是 0
for _ in range(10):
    app._ip_card_rows(_leaked, "zh")
check("重绘 10 次仍为 0", _geo_hits["n"], 0)

# probe() 把国家算好放进结果，UI 直接读
_leaked_with = dict(_leaked, dns_server_countries={"114.114.114.114": "china",
                                                   "223.5.5.5": "china"})
_rows2 = app._ip_card_rows(_leaked_with, "zh")
_dns_row = [r for r in _rows2 if r["label"] == "DNS"][0]
_txt = " ".join(part.get("s", "") for part in _dns_row["parts"])
check("有国家信息时显示国家", "China" in _txt, True)
check("读取国家也不发请求", _geo_hits["n"], 0)
_ipsec.probe_geoip = _real_geoip

# ── macOS IP 检测必须有退避（此前完全没有，与 win/linux 语义漂移）─────────
print("\n【macOS IP 检测退避】")


class _StubIP:
    """只带 _absorb_ipsec / _ip_in_backoff 需要的字段。"""
    def __init__(self):
        self._ipsec = None
        self._ipsec_fail = 0
        self._ipsec_good_ts = 0.0
        self._ipsec_backoff_until = 0.0

    _absorb_ipsec = app.AiLimitApp._absorb_ipsec
    _ip_in_backoff = app.AiLimitApp._ip_in_backoff


import time as _t3
sip = _StubIP()
sip._absorb_ipsec({"level": "ok", "error": None})
check("成功后不在退避", sip._ip_in_backoff(), False)
for _ in range(2):
    sip._absorb_ipsec({"error": "trace 挂了"})
check("连败 2 次仍不退避（抖动吸收）", sip._ip_in_backoff(), False)
sip._absorb_ipsec({"error": "trace 挂了"})
check("连败 3 次开始退避（此前永远不退避）", sip._ip_in_backoff(), True)
_left = sip._ipsec_backoff_until - _t3.time()
check("首档退避 ≈ 一个 IP 检测周期", 500 <= _left <= 700, True)
sip._absorb_ipsec({"error": "trace 挂了"})
check("再败一次退避翻倍", sip._ipsec_backoff_until - _t3.time() > _left, True)
sip._absorb_ipsec({"level": "ok", "error": None})
check("成功后清除退避与计数",
      (sip._ip_in_backoff(), sip._ipsec_fail), (False, 0))

# degraded（ip.net.coffee 挂了但 trace 通）此前被算作成功，永不退避
sip2 = _StubIP()
sip2._absorb_ipsec({"level": "ok", "error": None, "degraded": False})
for _ in range(4):
    sip2._absorb_ipsec({"level": "ok", "error": None, "degraded": True})
check("degraded 仍算成功（trace 通就是测到了，不该退避）",
      sip2._ip_in_backoff(), False)

# ── 定时器首轮不得白发一轮 ──────────────────────────────────────────────────
print("\n【rumps.Timer 首轮推迟】")
import rumps as _rumps
_t_native = _rumps.Timer(lambda _: None, 180)
_t_native.start()
_native_delay = _t_native._nstimer.fireDate().timeIntervalSinceNow()
_t_native.stop()
check("原生 start() 确实立即触发（这就是问题所在）", _native_delay < 1.0, True)

_t_fixed = _rumps.Timer(lambda _: None, 180)
app._start_timer_deferred(_t_fixed)
_fixed_delay = _t_fixed._nstimer.fireDate().timeIntervalSinceNow()
_t_fixed.stop()
check("_start_timer_deferred 把首轮推迟一个完整周期", _fixed_delay > 170, True)

# 拿不到 _nstimer 时必须退回原行为而不是崩
class _NoNsTimer:
    interval = 60
    started = False

    def start(self):
        self.started = True


_stub = _NoNsTimer()
app._start_timer_deferred(_stub)
check("拿不到 _nstimer 时仍正常 start（不崩）", _stub.started, True)

# ── boardlink 开关：关掉必须真的不再监听 ────────────────────────────────────
print("\n【boardlink 开关】")
import boardlink as _bl
import socket as _sk

_saved = {"n": 0}


class _StubBL:
    """只带 _toggle_boardlink 需要的字段；不碰 rumps 菜单。"""
    def __init__(self):
        self._state = {"boardlink": True}
        self._boardlink = _bl.BoardLinkServer(lambda: {"v": 1, "ts": 0, "services": []})
        self._boardlink.start()

    def _update_boardlink_check(self):
        pass

    _toggle_boardlink = app.AiLimitApp._toggle_boardlink


_orig_save = app._save_state
app._save_state = lambda st: _saved.__setitem__("n", _saved["n"] + 1)
sb = _StubBL()
_p = sb._boardlink.port
check("默认开：端口在监听", _p is not None and _p > 0, True)

sb._toggle_boardlink(None)
check("关掉后状态置 False", sb._state["boardlink"], False)
check("关掉后状态被持久化", _saved["n"] >= 1, True)
_probe = _sk.socket()
_probe.settimeout(1)
_refused = _probe.connect_ex(("127.0.0.1", _p)) != 0
_probe.close()
check("关掉后端口不再可连（一个字节都不对外）", _refused, True)

sb._toggle_boardlink(None)
check("再开回来状态置 True", sb._state["boardlink"], True)
check("再开回来重新监听", sb._boardlink.port is not None, True)
sb._boardlink.stop()
app._save_state = _orig_save

# ── 更新提醒：指向 fork、每天最多一次、每版只通知一次 ───────────────────────
print("\n【更新提醒】")
check("更新源指向 fork 仓库而不是上游",
      "dtzeng811/ai-limit" in app._RELEASES_API_URL, True)
check("下载页也指向 fork", "dtzeng811/ai-limit" in app._RELEASES_PAGE_URL, True)
check("Gitee 兜底已移除（它指向上游）", app._GITEE_RELEASES_API_URL, None)
check("未公证的包不走一键安装", app._AUTO_INSTALL_ENABLED, False)


class _StubUpd:
    """只带更新检查需要的字段；不启动 rumps。"""
    def __init__(self, **st):
        self._state = {"update_check": True, "last_update_check": 0.0,
                       "update_seen": "", "update_notified": "",
                       "lang": "zh", **st}
        self._updating = False
        self._update_checking = False
        self._update_lock = __import__("threading").Lock()
        self._update_pending = None
        self._update_gate = quotacore.SingleFlight(0)
        self.kicked = []
        self.notified = []
        self.title = ""

    def _lang(self):
        return "zh"

    def _kick_update_check(self, manual=False):
        self.kicked.append(manual)

    def _notify_update(self, latest):
        self.notified.append(latest)

    class _Item:
        title = ""
    _check_update_item = _Item()

    _maybe_auto_check_update = app.AiLimitApp._maybe_auto_check_update
    _show_update_result = app.AiLimitApp._show_update_result
    _update_check_item_title = app.AiLimitApp._update_check_item_title


_saved_upd = {"n": 0}
_orig_save2 = app._save_state
app._save_state = lambda st: _saved_upd.__setitem__("n", _saved_upd["n"] + 1)

# 关掉开关 → 一个请求都不发
u = _StubUpd(update_check=False)
u._maybe_auto_check_update()
check("关掉自动检查 → 不发请求", u.kicked, [])

# 打开且从未查过 → 查
u = _StubUpd()
u._maybe_auto_check_update()
check("从未查过 → 触发一次自动检查", u.kicked, [False])

# 刚查过 → 不再查（每天最多一次）
import time as _t4
u = _StubUpd(last_update_check=_t4.time())
u._maybe_auto_check_update()
check("24 小时内不再查", u.kicked, [])

# 结果处理：有新版 → 记状态 + 通知一次 + 菜单变红点
u = _StubUpd()
u._show_update_result({"latest": "0.3.99-fork.1", "manual": False})
check("记下已知最新版", u._state["update_seen"], "0.3.99-fork.1")
check("记下检查时刻（失败/成功都记，避免每轮重试）",
      u._state["last_update_check"] > 0, True)
check("自动检查弹一次通知", u.notified, ["0.3.99-fork.1"])
u._update_check_item_title()
check("菜单标题变成醒目提醒", "🔴" in u._check_update_item.title
      and "0.3.99-fork.1" in u._check_update_item.title, True)

# 同一版本再来一轮 → 不重复通知
u._show_update_result({"latest": "0.3.99-fork.1", "manual": False})
check("同一版本不重复通知（装着不升的人不该天天被弹）", u.notified, ["0.3.99-fork.1"])

# 更新的版本出现 → 再通知一次
u._show_update_result({"latest": "0.3.99-fork.2", "manual": False})
check("出现更新的版本 → 再通知一次", u.notified,
      ["0.3.99-fork.1", "0.3.99-fork.2"])

# 已是最新 → 不通知、标题回落
u2 = _StubUpd()
u2._show_update_result({"latest": app.__version__.replace("+", "-"), "manual": False})
check("已是最新不通知", u2.notified, [])
u2._update_check_item_title()
check("已是最新时标题不带红点", "🔴" not in u2._check_update_item.title, True)

# 旧版本（降级）不该提示
u3 = _StubUpd()
u3._show_update_result({"latest": "0.0.1", "manual": False})
check("远端是旧版时不提示", u3.notified, [])

# 检查失败也要记时间戳，否则会每轮重试
u4 = _StubUpd()
u4._show_update_result({"error": True, "manual": False})
check("检查失败也记时间戳（不每轮硬撞）", u4._state["last_update_check"] > 0, True)
check("检查失败不通知", u4.notified, [])

app._save_state = _orig_save2

# ── 全天请求量对照（假时钟推进真实时间，否则 TTL 永不过期，数字是假的）──────
print("\n【默认配置每天请求量（假时钟推进 24 小时）】")
ROUNDS_PER_DAY = 24 * 20                  # 3 分钟一轮
_fresh_state()
fake = {"t": 0.0}
# 给两个状态页缓存换上假时钟，让 TTL 真的会过期
app._STATUS_CACHES.clear()
for _u in (app.CLAUDE_STATUS_COMPONENTS_URL, app.CODEX_STATUS_COMPONENTS_URL):
    app._STATUS_CACHES[_u] = quotacore.TTLCache(
        app._STATUS_TTL_SEC, fail_backoff_sec=app._STATUS_TTL_SEC,
        failed=lambda v: v == "unknown", clock=lambda: fake["t"])

for _ in range(ROUNDS_PER_DAY):
    app._fetch_claude("zh")
    app._fetch_status(app.CLAUDE_STATUS_COMPONENTS_URL)
    app._fetch_status(app.CODEX_STATUS_COMPONENTS_URL)
    fake["t"] += quotacore.REFRESH_SEC        # 每轮推进 3 分钟

status_day = N["status"]
plan_day = N["plan"]
print(f"      480 轮/天 → 额度 {N['usage']} 次 · 套餐 {plan_day} 次 · 状态页 {status_day} 次")
check("额度每天 480 次（每轮必抓，实时性要求高，不该省）", N["usage"], 480)
check("套餐每天 ≤ 3 次（12h TTL，旧实现最坏 480）", plan_day <= 3, True)

_OLD_STATUS_PER_DAY = 480 * 2                 # 旧实现：每轮 × 2 端点
check("状态页每天 ≤ 300 次（旧实现 960）", status_day <= 300, True)
saved = _OLD_STATUS_PER_DAY - status_day
print(f"      状态页：{_OLD_STATUS_PER_DAY} → {status_day} 次/天，"
      f"省 {saved} 次（{saved * 100 // _OLD_STATUS_PER_DAY}%）")
_OLD_TOTAL = 480 + 480 + _OLD_STATUS_PER_DAY  # 额度 + 套餐(最坏每轮) + 状态页
_NEW_TOTAL = N["usage"] + plan_day + status_day
print(f"      claude.ai + statuspage 合计：{_OLD_TOTAL} → {_NEW_TOTAL} 次/天，"
      f"省 {_OLD_TOTAL - _NEW_TOTAL} 次（{(_OLD_TOTAL - _NEW_TOTAL) * 100 // _OLD_TOTAL}%）")

print("\n" + ("FAILED: " + ", ".join(FAILS) if FAILS else "ALL PASS"))
sys.exit(1 if FAILS else 0)
