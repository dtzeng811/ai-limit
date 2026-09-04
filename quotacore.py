#!/usr/bin/env python3
"""ai-limit 三端共享的行为核心（跨平台，无 UI / 无平台依赖）。

三端（menubar / winbar / linuxbar）此前各自实现了同一套「抖动抑制 + 指数退避」
逻辑，语义应当一致却是三份代码——已经发生漂移（linuxbar 的退避公式一度多乘
一档）。本模块把这套算法和相关常量、纯函数收成单一来源，各端只做 UI。

**只放真正与平台无关的东西**：失败记账算法、刷新参数、品牌色/阈值、套餐名缓存、
窗口短标签、locale 检测。抓取（usage.py / ipsec.py）和绘制（各端）不在此列。
"""
import contextlib as _contextlib
import locale as _locale
import os
import re as _re
import threading as _threading
import time

# ── 刷新 / 抖动抑制 / 退避参数（三端共用同一组值） ────────────────────────────
DEFAULT_REFRESH_MIN = 3
REFRESH_SEC     = DEFAULT_REFRESH_MIN * 60
JITTER_MAX_SEC  = 20
FAIL_GRACE_N    = 3            # 连败到这个次数才如实报错 / 开始退避
STALE_MAX_SEC   = 15 * 60      # 好数据的沿用上限（额度：5 个刷新周期）
BACKOFF_MAX_SEC = 30 * 60
PLAN_TTL_SEC    = 12 * 60 * 60

# IP 安全检测独立节奏：IP/DNS 状态不分钟级变化，10 分钟一轮。过期上限按自己的
# 节奏放宽到 4 轮——用额度那边的 15 分钟会让第 2 次失败就因过期变灰，抢在
# 「连败 3 次」规则之前触发。
IPSEC_REFRESH_SEC   = 10 * 60
IPSEC_STALE_MAX_SEC = 4 * IPSEC_REFRESH_SEC

# ── 品牌色 / 告警阈值（UI 各端按自己的色彩 API 取用） ────────────────────────
SERVICE_COLORS = {"claude": "#D97757", "codex": "#10A37F"}
SERVICE_TITLES = {"claude": "Claude Code", "codex": "CodeX"}
WARN_THRESHOLD  = 20          # 剩余低于此值告警（与 usage.py 同值，此处不 import
CRIT_THRESHOLD  = 10          # 以避免 UI 层为拿阈值而牵入整个 usage 模块）


# ── 错误分类：不同失败该用不同的退避策略 ─────────────────────────────────────
#
# 此前所有失败一视同仁地「失败 +1」，于是登录态失效（401）会让客户端每 3 分钟
# 硬撞一次 claude.ai / chatgpt.com，撞到用户重新登录为止；被限流（429）同样继续
# 按原节奏撞回去。两者都是把自己送进风控名单的典型形态。
AUTH_BACKOFF_SEC       = 15 * 60   # 401/403：不是重试能解决的，等用户干预
RATE_LIMIT_BACKOFF_SEC = 15 * 60   # 429 且服务端没给 Retry-After 时的默认值
RETRY_AFTER_MAX_SEC    = 2 * 3600  # Retry-After 上限，防畸形头把客户端冻死


def parse_retry_after(value):
    """解析 Retry-After 响应头。返回秒数（float）或 None。

    两种合法形态（RFC 9110）：delta-seconds，或 HTTP-date。此前全库根本没读过
    这个头——服务端明确告诉我们"等 N 秒再来"，客户端却照原节奏继续撞。
    """
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        secs = float(raw)
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime
            when = parsedate_to_datetime(raw)
        except Exception:
            return None
        if when is None:
            return None
        try:
            import datetime as _dt
            if when.tzinfo is None:
                when = when.replace(tzinfo=_dt.timezone.utc)
            secs = (when - _dt.datetime.now(_dt.timezone.utc)).total_seconds()
        except Exception:
            return None
        secs = max(0.0, secs)      # 已经过去的时刻 = 不用等，不是倒计时
    if secs < 0:
        return None
    return min(float(secs), RETRY_AFTER_MAX_SEC)


def classify_http_status(status):
    """HTTP 状态码 → 退避策略档位。

    auth       401/403  登录态或权限问题，重试解决不了 → 长退避等用户干预
    rate_limit 429      明确被限流 → 尊重 Retry-After，否则长退避
    server     5xx      服务端问题 → 指数退避
    generic    其余/未知 → 沿用原有的「连败 3 次才退避」抖动吸收语义
    """
    if status in (401, 403):
        return "auth"
    if status == 429:
        return "rate_limit"
    if isinstance(status, int) and 500 <= status < 600:
        return "server"
    return "generic"


def failure_backoff_sec(new, fail, refresh_sec):
    """一次失败该退避多久（秒；0 = 不退避）。

    AbsorbState 与 menubar 自己那份 _absorb_fetch **共用这一个函数**——分类逻辑
    只写在其中一处的话，另一处就是漏网之鼠（错误分类刚加进 AbsorbState 时，
    macOS 主力平台走的恰恰是 _absorb_fetch，等于完全不生效）。

    - 通用失败：连败达 FAIL_GRACE_N 后指数退避，封顶 BACKOFF_MAX_SEC
    - 429：不等宽限，首次即退避；服务端给了 Retry-After 就听它的
    - 401/403/Cloudflare：不等宽限，首次即长退避（重试解决不了，等用户干预）
    """
    over = fail - FAIL_GRACE_N
    delay = min(refresh_sec * (2 ** over), BACKOFF_MAX_SEC) if over >= 0 else 0.0
    if isinstance(new, dict):
        kind = new.get("error_kind")
        if kind == "rate_limit":
            hinted = new.get("retry_after_sec")
            delay = max(delay, float(hinted) if hinted else RATE_LIMIT_BACKOFF_SEC)
        elif kind == "auth":
            delay = max(delay, AUTH_BACKOFF_SEC)
    return delay


# ── 抖动抑制 + 指数退避（三端合一） ──────────────────────────────────────────
class AbsorbState:
    """一个被监控目标（某个服务的额度、或 IP 安全检测）的失败记账 + 数据吸收。

    合并自 menubar._absorb_fetch、winbar.ServiceState/IPState、linuxbar 手写计数
    ——它们曾是同一算法的四种写法。语义：

    - 成功：清零失败计数与退避，记录成功时刻，替换数据。
    - 失败：计数 +1。若手里还有一份足够新的好数据、且连败未到 grace，就沿用旧值
      （瞬时抖动被吸收，UI 不闪告警）；连败到阈值或数据过老才如实换成失败态。
    - 连败达 grace 后进入指数退避（跳过 1、2、4… 个刷新周期，上限封顶）。

    额度与 IP 的差异只有两点，用构造参数区分，算法本体不分叉：
    - `stale_max`：好数据沿用上限（额度 15 分钟 / IP 40 分钟）。
    - `failed`：怎样算「这份数据是失败态」。额度看 `"error" in d`；IP 看
      `d.get("error")`（因为 degraded=True 仍是一次成功检测，不能记失败）。
    """

    def __init__(self, refresh_sec_fn, *, stale_max=STALE_MAX_SEC, failed=None):
        self.data = None
        self.fail = 0
        self.good_ts = 0.0
        self.backoff_until = 0.0
        self._refresh_sec = refresh_sec_fn
        self._stale_max = stale_max
        self._failed = failed or (lambda d: bool(d) and "error" in d)

    def absorb(self, new):
        """吃进一次抓取结果，更新 self.data。new 为 None（本轮没抓）时不动。"""
        if new is None:
            return
        if not self._failed(new):
            self.fail = 0
            self.good_ts = time.time()
            self.backoff_until = 0.0
            self.data = new
            return
        self.fail += 1
        delay = failure_backoff_sec(new, self.fail, self._refresh_sec())
        if delay > 0:
            self.backoff_until = time.time() + delay
        has_good = bool(self.data) and not self._failed(self.data)
        fresh = (time.time() - self.good_ts) <= self._stale_max
        if has_good and fresh and self.fail < FAIL_GRACE_N:
            return                          # 吸收：沿用旧好数据
        self.data = new

    def in_backoff(self) -> bool:
        return time.time() < self.backoff_until


class SingleFlight:
    """同一目标的并发去重 + 最小触发间隔（冷却）。

    没有它的时候，「立即刷新」每点一次就无条件起一个后台线程：用户连点 5 下
    就是 5 组并发抓取打向同一批端点——这正是被判为异常自动化流量的典型形态。
    自动刷新与手动刷新、设置变更触发的刷新之间也会重叠。

    冷却对自动触发生效；用户显式操作可 force=True 跳过冷却，但**并发保护永远
    生效**（正在抓就是正在抓，再点也不会多发一组请求）。

    clock 可注入，便于用假时钟做确定性测试。
    """

    def __init__(self, cooldown_sec=0.0, clock=time.time):
        self.cooldown_sec = cooldown_sec
        self._clock = clock
        self._busy = False
        self._last_begin = float("-inf")
        self._lock = _threading.Lock()

    def try_begin(self, *, force=False) -> bool:
        """拿到执行权返回 True；被并发或冷却挡下返回 False（调用方直接跳过）。"""
        with self._lock:
            if self._busy:
                return False
            if not force and (self._clock() - self._last_begin) < self.cooldown_sec:
                return False
            self._busy = True
            self._last_begin = self._clock()
            return True

    def end(self):
        with self._lock:
            self._busy = False

    @_contextlib.contextmanager
    def guard(self, *, force=False):
        """with sf.guard() as ok: ... —— 异常路径也保证 end()，不会永久卡死。"""
        ok = self.try_begin(force=force)
        try:
            yield ok
        finally:
            if ok:
                self.end()


class TTLCache:
    """低频变化数据的单值缓存：TTL 内直接返回缓存，失败后指数退避。

    为状态页（Statuspage components.json）这类数据准备：它以十分钟计变化，
    却曾跟额度一样每 3 分钟抓一次——两个端点 × 20 次/小时 = 960 次/天纯浪费。

    **不用旧值伪装新值**：失败时如实返回失败值（沿用调用方原有语义，例如状态页
    的 "unknown" 必须显示❓），只是不再立刻重试，避免失败后持续硬撞。
    """

    def __init__(self, ttl_sec, *, fail_backoff_sec=60.0,
                 max_backoff_sec=BACKOFF_MAX_SEC, failed=None, clock=time.time):
        self.ttl_sec = ttl_sec
        self.fail_backoff_sec = fail_backoff_sec
        self.max_backoff_sec = max_backoff_sec
        self._failed = failed or (lambda v: v is None)
        self._clock = clock
        self._value = None
        self._ts = float("-inf")
        self._last = None          # 上一次的结果（含失败值），退避期内回放
        self._fail = 0
        self._retry_at = 0.0
        self._lock = _threading.Lock()

    def get(self, fetch_fn):
        """命中缓存或退避期内都不会调用 fetch_fn（即：不发请求）。"""
        with self._lock:
            now = self._clock()
            if self._value is not None and (now - self._ts) < self.ttl_sec:
                return self._value
            if now < self._retry_at:
                return self._last
        value = fetch_fn()
        with self._lock:
            now = self._clock()
            self._last = value
            if self._failed(value):
                self._fail += 1
                self._retry_at = now + min(
                    self.fail_backoff_sec * (2 ** (self._fail - 1)),
                    self.max_backoff_sec)
            else:
                self._fail = 0
                self._retry_at = 0.0
                self._value = value
                self._ts = now
            return value

    def invalidate(self):
        """用户显式刷新时清空，让下一次 get 真的去取。"""
        with self._lock:
            self._value = None
            self._ts = float("-inf")
            self._fail = 0
            self._retry_at = 0.0


def ip_failed(d) -> bool:
    """IP 检测的失败判定：probe() 只在整轮拿不到数据（trace 挂了）时置 error；
    ip.net.coffee 单独挂掉是 degraded=True，那仍是一次成功检测，不记失败。"""
    return bool(d) and bool(d.get("error"))


def make_ip_state(refresh_sec_fn) -> AbsorbState:
    """IP 安全检测专用的 AbsorbState（40 分钟过期 + degraded 不算失败）。"""
    return AbsorbState(refresh_sec_fn, stale_max=IPSEC_STALE_MAX_SEC, failed=ip_failed)


# ── 更新检查：版本比较 + 节流 ────────────────────────────────────────────────
#
# fork 版有自己的发版节奏（tag 形如 v0.3.23-fork.14，__version__ 形如
# 0.3.23+fork.14），装了 fork 的人要跟的是 fork 仓库而不是上游。
UPDATE_CHECK_TTL_SEC = 24 * 3600   # 每天最多查一次 Release（GitHub 匿名 API 60 次/小时）


def version_tuple(v):
    """版本字符串 → 可比较的整数元组。

    每段只取前导数字，取不到记 0。这样 "0.3.23-fork.14"（tag 写法）与
    "0.3.23+fork.14"（__version__ 写法）都会得到 (0, 3, 23, 14)——两种写法
    必须等值，否则永远比不出新版。预发布后缀（-rc1 / -dev）同理不会抛异常。
    """
    out = []
    for part in str(v or "").lstrip("vV").split("."):
        m = _re.match(r"\d+", part)
        out.append(int(m.group()) if m else 0)
    return tuple(out) if out else (0,)


def is_newer_version(latest, current):
    """远端版本是否严格新于当前版本。空/无效的 latest 一律判为「不新」——
    拿不准就不提示，比误报一个不存在的更新好。"""
    if not latest:
        return False
    return version_tuple(latest) > version_tuple(current)


def update_due(last_check_ts, now=None, ttl_sec=UPDATE_CHECK_TTL_SEC):
    """距上次检查是否已超过 TTL。

    时钟回拨或状态文件被改坏（last 是未来时刻 / 不是数字）时返回 True：
    宁可多查一次，也不能让「下次检查」永远不到来而彻底失去更新提醒。
    """
    if not last_check_ts:
        return True                    # 从未查过：与"过了多久"无关，直接该查
    now = time.time() if now is None else now
    try:
        last = float(last_check_ts)
    except (TypeError, ValueError):
        return True
    if last > now:
        return True
    return (now - last) >= ttl_sec


# ── 纯函数：套餐名缓存 / 窗口短标签 / locale ─────────────────────────────────
_plan_cache = {"plan": None, "ts": 0.0}
_plan_retry_at = 0.0       # 失败退避：早于此刻不再重试
_plan_fail = 0
_plan_lock = _threading.Lock()

PLAN_FAIL_BACKOFF_SEC = 10 * 60   # 套餐查询失败后的首次退避，逐次翻倍至 BACKOFF_MAX_SEC


def cached_claude_plan(live_plan_fn):
    """Claude 套餐名缓存 12 小时。套餐几个月才变一次，每轮跟着 usage 查一次
    organizations/{org} 等于把打向 claude.ai 的请求量白翻倍。live_plan_fn 由
    调用方注入（usage.live_claude_plan），避免本模块 import usage。

    失败退避：查失败时**不更新 ts**，所以旧实现会在下一轮（3 分钟后）再查一次，
    登录态失效这类持续性失败会变成每 3 分钟硬撞一次 claude.ai——套餐只是展示
    信息，为它持续制造失败请求正是被判为异常自动化流量的成因。现在失败后进入
    独立退避（10 分钟起，逐次翻倍，封顶 BACKOFF_MAX_SEC），期间直接沿用旧值。

    并发去重：双检锁保证多个后台线程同时未命中时只有一个真去查（single-flight），
    其余等锁后直接读到新缓存，不会各发一次请求。
    """
    global _plan_retry_at, _plan_fail
    now = time.time()
    if now - _plan_cache["ts"] < PLAN_TTL_SEC:
        return _plan_cache["plan"]
    if now < _plan_retry_at:               # 失败退避期内：沿用旧值，不发请求
        return _plan_cache["plan"]
    with _plan_lock:
        now = time.time()                  # 双检：等锁期间别的线程可能已经查好了
        if now - _plan_cache["ts"] < PLAN_TTL_SEC or now < _plan_retry_at:
            return _plan_cache["plan"]
        try:
            plan = live_plan_fn()
        except Exception:
            _plan_fail += 1
            _plan_retry_at = now + min(
                PLAN_FAIL_BACKOFF_SEC * (2 ** (_plan_fail - 1)), BACKOFF_MAX_SEC)
            return _plan_cache["plan"]
        _plan_fail = 0
        _plan_retry_at = 0.0
        _plan_cache["plan"] = plan
        _plan_cache["ts"] = now
        return plan


def window_shorthand(window_minutes):
    """按窗口实际分钟数生成 "5h"/"7d" 短标签，不硬编码 Codex 固定两档
    （2026-07 OpenAI 把 5 小时窗口并入周窗口后字段位置仍叫 primary）。"""
    if not window_minutes:
        return None
    hours = window_minutes / 60
    if hours < 24:
        return f"{round(hours) or 1}h"
    return f"{round(hours / 24)}d"


def fmt_plan_label(plan):
    """套餐名规范化（"max_20x" → "Max 20x"）。不用 str.title()——它把紧跟
    数字的字母也当词首，会得到 "Max 20X"。返回 None 表示无有效套餐。"""
    if not plan or plan == "?":
        return None
    words = str(plan).replace("_", " ").split()
    return " ".join(w[:1].upper() + w[1:] for w in words) or None


def detect_lang() -> str:
    """zh / en。AI_LIMIT_LANG 显式覆盖优先；否则看系统 locale。兼容 Windows
    中文系统的 'Chinese (Simplified)_China'（不带 zh 前缀）。"""
    env = os.environ.get("AI_LIMIT_LANG", "")
    if env:
        return "zh" if env.lower().startswith("zh") else "en"
    try:
        loc = (_locale.getlocale()[0] or os.environ.get("LANG", "") or "").lower()
    except Exception:
        loc = ""
    return "zh" if (loc.startswith("zh") or "chinese" in loc) else "en"
