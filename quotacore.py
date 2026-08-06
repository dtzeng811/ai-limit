#!/usr/bin/env python3
"""ai-limit 三端共享的行为核心（跨平台，无 UI / 无平台依赖）。

三端（menubar / winbar / linuxbar）此前各自实现了同一套「抖动抑制 + 指数退避」
逻辑，语义应当一致却是三份代码——已经发生漂移（linuxbar 的退避公式一度多乘
一档）。本模块把这套算法和相关常量、纯函数收成单一来源，各端只做 UI。

**只放真正与平台无关的东西**：失败记账算法、刷新参数、品牌色/阈值、套餐名缓存、
窗口短标签、locale 检测。抓取（usage.py / ipsec.py）和绘制（各端）不在此列。
"""
import locale as _locale
import os
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
        over = self.fail - FAIL_GRACE_N
        if over >= 0:
            delay = min(self._refresh_sec() * (2 ** over), BACKOFF_MAX_SEC)
            self.backoff_until = time.time() + delay
        has_good = bool(self.data) and not self._failed(self.data)
        fresh = (time.time() - self.good_ts) <= self._stale_max
        if has_good and fresh and self.fail < FAIL_GRACE_N:
            return                          # 吸收：沿用旧好数据
        self.data = new

    def in_backoff(self) -> bool:
        return time.time() < self.backoff_until


def ip_failed(d) -> bool:
    """IP 检测的失败判定：probe() 只在整轮拿不到数据（trace 挂了）时置 error；
    ip.net.coffee 单独挂掉是 degraded=True，那仍是一次成功检测，不记失败。"""
    return bool(d) and bool(d.get("error"))


def make_ip_state(refresh_sec_fn) -> AbsorbState:
    """IP 安全检测专用的 AbsorbState（40 分钟过期 + degraded 不算失败）。"""
    return AbsorbState(refresh_sec_fn, stale_max=IPSEC_STALE_MAX_SEC, failed=ip_failed)


# ── 纯函数：套餐名缓存 / 窗口短标签 / locale ─────────────────────────────────
_plan_cache = {"plan": None, "ts": 0.0}


def cached_claude_plan(live_plan_fn):
    """Claude 套餐名缓存 12 小时。套餐几个月才变一次，每轮跟着 usage 查一次
    organizations/{org} 等于把打向 claude.ai 的请求量白翻倍。查失败沿用旧值
    （展示信息，宁可旧也不必重试制造请求）。live_plan_fn 由调用方注入
    （usage.live_claude_plan），避免本模块 import usage。"""
    now = time.time()
    if now - _plan_cache["ts"] < PLAN_TTL_SEC:
        return _plan_cache["plan"]
    try:
        plan = live_plan_fn()
        _plan_cache["plan"] = plan
        _plan_cache["ts"] = now
        return plan
    except Exception:
        return _plan_cache["plan"]


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
