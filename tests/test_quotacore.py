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
def _boom():
    raise RuntimeError("net")
check("cached_plan 查失败沿用旧值", qc.cached_claude_plan(_boom) == "pro")

# detect_lang 受环境变量控制
import os
os.environ["AI_LIMIT_LANG"] = "zh"; check("detect_lang zh", qc.detect_lang() == "zh")
os.environ["AI_LIMIT_LANG"] = "en"; check("detect_lang en", qc.detect_lang() == "en")
os.environ.pop("AI_LIMIT_LANG", None)

print("\n" + ("FAILED: " + ", ".join(FAILS) if FAILS else "ALL PASS"))
sys.exit(1 if FAILS else 0)
