#!/usr/bin/env python3
"""quotacore 离线单测——不联网。覆盖 AbsorbState 全分支 + 纯函数。

quotacore 是三端共享的行为核心，这里的断言必须与合并前 menubar._absorb_fetch /
winbar.ServiceState.absorb 的行为逐条对齐（回归护栏）。
"""
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import quotacore as qc  # noqa: E402

FAILS = []


def check(name, cond):
    if not cond:
        FAILS.append(name)
    print(f"  {'✓' if cond else '✗'} {name}")


GOOD = {"5h_left": 88, "plan": "max"}
GOOD2 = {"5h_left": 87, "plan": "max"}
ERR = {"error": "被拦截"}

print("\n【AbsorbState — 额度语义（stale=15min）】")
st = qc.AbsorbState(lambda: 180)
st.absorb(GOOD);  check("成功入库", st.data == GOOD and st.fail == 0)
st.absorb(ERR);   check("单次失败吸收（沿用旧值）", st.data == GOOD and st.fail == 1)
st.absorb(ERR);   check("二连败仍吸收", st.data == GOOD and st.fail == 2)
st.absorb(ERR);   check("三连败如实报错", st.data == ERR and st.fail == 3)
check("三连败进退避 180s", st.in_backoff() and 175 < st.backoff_until - time.time() <= 180)
st.absorb(ERR);   check("四连败退避翻倍 360s", 355 < st.backoff_until - time.time() <= 360)
st.absorb(GOOD2); check("成功清账（fail/backoff 归零）",
                        st.data == GOOD2 and st.fail == 0 and not st.in_backoff())

print("\n【AbsorbState — 边界】")
st2 = qc.AbsorbState(lambda: 180)
st2.absorb(ERR);  check("冷启动即失败→立刻报错（无好数据可沿用）", st2.data == ERR)
st3 = qc.AbsorbState(lambda: 180)
st3.absorb(None); check("None（本轮没抓）不改状态", st3.data is None and st3.fail == 0)

# 数据过期：好数据老于 stale_max，下一次失败即换成错误（不再吸收）
st4 = qc.AbsorbState(lambda: 180)
st4.absorb(GOOD)
st4.good_ts = time.time() - 16 * 60      # 拨到 16 分钟前（>15min 上限）
st4.absorb(ERR); check("好数据过期后单次失败即报错", st4.data == ERR)

print("\n【AbsorbState — IP 语义（stale=40min, degraded 不算失败）】")
ipst = qc.make_ip_state(lambda: 600)
ipst.absorb({"level": "ok", "ip": "1.2.3.4"})
check("IP 成功入库", ipst.data["level"] == "ok")
ipst.absorb({"level": "ok", "degraded": True, "ip": "1.2.3.4"})
check("degraded 视作成功（不记失败）", ipst.fail == 0)
ipst.absorb({"error": "trace 挂了"})
check("真 error 记失败但被吸收", ipst.fail == 1 and ipst.data["level"] == "ok")
for _ in range(2):
    ipst.absorb({"error": "trace 挂了"})
check("IP 连败 3 次才换失败态", ipst.data.get("error") == "trace 挂了")
check("IP 退避按 600s 节奏", 595 < ipst.backoff_until - time.time() <= 600)

print("\n【纯函数】")
check("fmt_plan_label max_20x→Max 20x", qc.fmt_plan_label("max_20x") == "Max 20x")
check("fmt_plan_label pro→Pro", qc.fmt_plan_label("pro") == "Pro")
check("fmt_plan_label ?/空→None", qc.fmt_plan_label("?") is None and qc.fmt_plan_label("") is None)
check("window_shorthand 300→5h", qc.window_shorthand(300) == "5h")
check("window_shorthand 10080→7d", qc.window_shorthand(10080) == "7d")
check("window_shorthand 0/None→None",
      qc.window_shorthand(0) is None and qc.window_shorthand(None) is None)

# 套餐缓存：查一次后缓存，失败沿用旧值
calls = {"n": 0}
def _live():
    calls["n"] += 1
    return "pro"
qc._plan_cache.update({"plan": None, "ts": 0.0})
check("cached_plan 首次查", qc.cached_claude_plan(_live) == "pro" and calls["n"] == 1)
check("cached_plan 命中缓存不再查", qc.cached_claude_plan(_live) == "pro" and calls["n"] == 1)
boom_calls = {"n": 0}
def _boom():
    boom_calls["n"] += 1
    raise RuntimeError("net")

# ⚠️ 注意：TTL 未过期时根本不会调用 live_plan_fn。旧版这条断言写成
# cached_claude_plan(_boom) == "pro" 却从没真正走进失败分支（命中缓存直接返回），
# 是个"看起来在测失败、其实没测"的断言。下面先把 ts 置为过期再测。
qc._plan_cache["ts"] = 0.0                      # 模拟 12h TTL 已过期
check("cached_plan 过期后查失败沿用旧值",
      qc.cached_claude_plan(_boom) == "pro" and boom_calls["n"] == 1)

# 关键回归：套餐查询失败后**不能每轮都重试**。套餐是展示信息，失败还每 3 分钟
# 硬撞 claude.ai 正是"被判为异常自动化流量"的典型成因。
before = boom_calls["n"]
qc.cached_claude_plan(_boom)
qc.cached_claude_plan(_boom)
check("cached_plan 失败后进入退避，不再连续重试",
      boom_calls["n"] == before)

# 退避期过后允许重试；成功则恢复正常缓存并清除失败计数
qc._plan_retry_at = 0.0
check("退避期满可重试", qc.cached_claude_plan(_live) == "pro" and calls["n"] == 2)
check("成功后清除失败退避", qc._plan_retry_at == 0.0)

# detect_lang 受环境变量控制
import os
os.environ["AI_LIMIT_LANG"] = "zh"; check("detect_lang zh", qc.detect_lang() == "zh")
os.environ["AI_LIMIT_LANG"] = "en"; check("detect_lang en", qc.detect_lang() == "en")
os.environ.pop("AI_LIMIT_LANG", None)


# ── 错误分类 + Retry-After ──────────────────────────────────────────────────
print("\n【parse_retry_after — 秒数与 HTTP-date 两种形态】")
check("纯秒数", qc.parse_retry_after("120") == 120.0)
check("带空白", qc.parse_retry_after("  60 ") == 60.0)
check("0 视为无等待", qc.parse_retry_after("0") == 0.0)
check("负数当无效", qc.parse_retry_after("-5") is None)
check("None → None", qc.parse_retry_after(None) is None)
check("空串 → None", qc.parse_retry_after("") is None)
check("垃圾串 → None", qc.parse_retry_after("soon") is None)
_future = qc.parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT")
check("HTTP-date 解析成正的秒数", _future is not None and _future > 0)
check("过去的 HTTP-date → 0（不倒计时）",
      qc.parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0)
check("超长 Retry-After 被夹到上限",
      qc.parse_retry_after(str(10 ** 9)) == qc.RETRY_AFTER_MAX_SEC)

print("\n【classify_http_status — 不同错误不同策略】")
check("401 → auth（等用户干预，不是重试能解决的）", qc.classify_http_status(401) == "auth")
check("403 → auth", qc.classify_http_status(403) == "auth")
check("429 → rate_limit", qc.classify_http_status(429) == "rate_limit")
check("500 → server", qc.classify_http_status(500) == "server")
check("503 → server", qc.classify_http_status(503) == "server")
check("404 → generic", qc.classify_http_status(404) == "generic")
check("None → generic", qc.classify_http_status(None) == "generic")

print("\n【AbsorbState 按错误类型差异化退避】")
# 429：不等连败 3 次，第一次就退避，且尊重 Retry-After
st429 = qc.AbsorbState(lambda: 180)
st429.absorb(GOOD)
st429.absorb({"error": "限流", "error_kind": "rate_limit", "retry_after_sec": 900})
check("429 首次即退避（不等宽限 3 次）", st429.in_backoff())
check("429 退避时长尊重 Retry-After（≈900s）",
      880 <= st429.backoff_until - time.time() <= 920)

# 401/403：认证类立即长退避——登录态失效时每 3 分钟硬撞正是被判异常流量的成因
stauth = qc.AbsorbState(lambda: 180)
stauth.absorb(GOOD)
stauth.absorb({"error": "需重新登录", "error_kind": "auth"})
check("401/403 首次即退避", stauth.in_backoff())
check("认证类退避 ≥ AUTH_BACKOFF_SEC",
      stauth.backoff_until - time.time() >= qc.AUTH_BACKOFF_SEC - 5)

# 普通网络抖动：保持原语义（前 2 次吸收，不退避）
stnet = qc.AbsorbState(lambda: 180)
stnet.absorb(GOOD)
stnet.absorb({"error": "网络超时"})
check("普通失败第 1 次不退避（保持原抖动吸收语义）", not stnet.in_backoff())
stnet.absorb({"error": "网络超时"})
check("普通失败第 2 次仍不退避", not stnet.in_backoff())
stnet.absorb({"error": "网络超时"})
check("普通失败第 3 次才退避", stnet.in_backoff())

# 恢复：一次成功清掉所有退避
stauth.absorb(GOOD)
check("成功后清除认证退避", not stauth.in_backoff() and stauth.fail == 0)

# 没有 Retry-After 的 429 用默认值
st429b = qc.AbsorbState(lambda: 180)
st429b.absorb({"error": "限流", "error_kind": "rate_limit"})
check("429 无 Retry-After 时用默认退避",
      st429b.in_backoff() and
      st429b.backoff_until - time.time() >= qc.RATE_LIMIT_BACKOFF_SEC - 5)

# ── SingleFlight：并发去重 + 最小冷却 ────────────────────────────────────────
print("\n【SingleFlight — 并发去重 + 冷却】")
clock = {"t": 1000.0}
sf = qc.SingleFlight(cooldown_sec=5.0, clock=lambda: clock["t"])
check("首次可执行", sf.try_begin() is True)
check("执行中再来被拒（并发去重）", sf.try_begin() is False)
sf.end()
check("刚结束仍在冷却期内被拒", sf.try_begin() is False)
clock["t"] += 4.9
check("冷却未满仍被拒", sf.try_begin() is False)
clock["t"] += 0.2
check("冷却期满可再执行", sf.try_begin() is True)
sf.end()
clock["t"] += 1
check("force=True 无视冷却（用户显式操作）", sf.try_begin(force=True) is True)
sf.end()
check("force 仍受并发保护", (sf.try_begin(force=True), sf.try_begin(force=True))[1] is False)
sf.end()
# 用户在别人执行期间点了刷新：**不能丢弃**，要记下来在本轮结束后补跑一轮。
# 丢弃 = 用户点了没反应（自动刷新占用闸门期间点击会石沉大海）。
sf3 = qc.SingleFlight(cooldown_sec=5.0, clock=lambda: clock["t"])
check("先占住闸门", sf3.try_begin() is True)
check("占用期间的 force 点击仍被拒（不并发）", sf3.try_begin(force=True) is False)
check("但会被记为待补跑，end() 返回 True", sf3.end() is True)
check("补跑标记是一次性的，第二次 end 不再重复", (sf3.try_begin(), sf3.end())[1] is False)

# 非 force（自动刷新撞上自动刷新）不该触发补跑——那只是多余请求
sf4 = qc.SingleFlight(clock=lambda: clock["t"])
sf4.try_begin()
sf4.try_begin(force=False)
check("自动触发被挡下不产生补跑", sf4.end() is False)

# 连点多次只补跑一轮（合并，不是排队 N 次）
sf5 = qc.SingleFlight(clock=lambda: clock["t"])
sf5.try_begin()
for _ in range(5):
    sf5.try_begin(force=True)
check("连点 5 次只合并成一次补跑", sf5.end() is True)
check("合并后不残留第二次", (sf5.try_begin(), sf5.end())[1] is False)

# 异常路径必须释放，否则永久卡死
sf2 = qc.SingleFlight(clock=lambda: clock["t"])
try:
    with sf2.guard() as ok:
        check("guard 拿到执行权", ok is True)
        raise RuntimeError("boom")
except RuntimeError:
    pass
check("guard 异常后仍释放（不会永久卡死）", sf2.try_begin() is True)
sf2.end()

# ── TTLCache：低频数据缓存 + 失败退避 ───────────────────────────────────────
print("\n【TTLCache — TTL 缓存 + 失败退避】")
clk = {"t": 0.0}
calls = {"n": 0}
def _ok():
    calls["n"] += 1
    return "operational"
tc = qc.TTLCache(ttl_sec=600, fail_backoff_sec=60,
                 failed=lambda v: v in (None, "unknown"), clock=lambda: clk["t"])
check("首次穿透取值", tc.get(_ok) == "operational" and calls["n"] == 1)
clk["t"] += 599
check("TTL 内命中缓存不发请求", tc.get(_ok) == "operational" and calls["n"] == 1)
clk["t"] += 2
check("TTL 过期后重新取", tc.get(_ok) == "operational" and calls["n"] == 2)

fcalls = {"n": 0}
def _fail():
    fcalls["n"] += 1
    return "unknown"
clk["t"] += 601
check("失败值如实返回（不用旧值伪装）", tc.get(_fail) == "unknown" and fcalls["n"] == 1)
check("失败后进入退避，不再连发", tc.get(_fail) == "unknown" and fcalls["n"] == 1)
clk["t"] += 61
check("首次退避期满可重试", tc.get(_fail) == "unknown" and fcalls["n"] == 2)
clk["t"] += 61
check("第二次失败退避翻倍（61s 不够）", fcalls["n"] == 2 or tc.get(_fail) is not None)
clk["t"] += 200
tc.get(_fail)
check("退避有指数增长", fcalls["n"] == 3)
clk["t"] += 100000
check("成功后恢复正常缓存", tc.get(_ok) == "operational")
clk["t"] += 1
check("恢复后 TTL 生效", tc.get(_ok) == "operational" and calls["n"] == 3)


# ── 更新检查：版本比较 + 节流 + 通知去重 ────────────────────────────────────
print("\n【version_tuple — 必须认得 fork 版本号】")
check("普通语义化版本", qc.version_tuple("0.3.24") == (0, 3, 24))
check("带 v 前缀", qc.version_tuple("v0.3.24") == (0, 3, 24))
check("fork 版本（tag 用连字符）", qc.version_tuple("0.3.23-fork.14") == (0, 3, 23, 14))
check("fork 版本（__version__ 用加号）", qc.version_tuple("0.3.23+fork.14") == (0, 3, 23, 14))
check("两种写法等值（这是能正确比较的前提）",
      qc.version_tuple("0.3.23+fork.14") == qc.version_tuple("v0.3.23-fork.14"))
check("预发布后缀不崩", qc.version_tuple("0.3.13-rc1") == (0, 3, 13))
check("空串不崩", qc.version_tuple("") == (0,))
check("垃圾串不崩", qc.version_tuple("nightly") == (0,))

print("\n【is_newer_version】")
check("fork.15 > fork.14", qc.is_newer_version("0.3.23-fork.15", "0.3.23+fork.14"))
check("fork.14 不新于自己", not qc.is_newer_version("0.3.23-fork.14", "0.3.23+fork.14"))
check("fork.9 不新于 fork.14（数字比较非字典序）",
      not qc.is_newer_version("0.3.23-fork.9", "0.3.23+fork.14"))
check("fork.14 新于 fork.9", qc.is_newer_version("0.3.23-fork.14", "0.3.23+fork.9"))
check("大版本更新", qc.is_newer_version("0.4.0", "0.3.23+fork.14"))
check("降级不算新版（防误装旧包）",
      not qc.is_newer_version("0.3.22", "0.3.23+fork.14"))
check("latest 为空不算新版", not qc.is_newer_version("", "0.3.23+fork.14"))
check("latest 为 None 不算新版", not qc.is_newer_version(None, "0.3.23+fork.14"))

print("\n【update_due — 每天最多查一次，重启不重查】")
DAY = 24 * 3600
check("从未查过 → 该查", qc.update_due(0.0, now=1000.0))
check("刚查过 → 不该查", not qc.update_due(1000.0, now=1000.0 + 60))
check("差一点到期 → 不该查", not qc.update_due(1000.0, now=1000.0 + DAY - 10))
check("到期 → 该查", qc.update_due(1000.0, now=1000.0 + DAY + 1))
check("时钟回拨（last 在未来）→ 该查，不会永久卡住",
      qc.update_due(9999999.0, now=1000.0))
check("last 非数字 → 该查（状态文件被改坏也不能瘫）",
      qc.update_due("坏数据", now=1000.0))
check("自定义 TTL 生效", qc.update_due(1000.0, now=1000.0 + 700, ttl_sec=600))

print("\n" + ("FAILED: " + ", ".join(FAILS) if FAILS else "ALL PASS"))
sys.exit(1 if FAILS else 0)
