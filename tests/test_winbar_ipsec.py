#!/usr/bin/env python3
"""Windows 托盘版 IP 安全 UI 的离线单测——不联网、不起 GUI。

覆盖三块：
1. IPState.absorb 的抖动抑制（单次失败沿用 / 连败 3 次变灰 / 退避）
2. 盾牌图标四种形状确实画得不一样（像素比对，防止改坏成只换色）
3. 卡片/tooltip 文案的分支（降级、DNS 三态、滥用分抽取）

跑法：python3 tests/test_winbar_ipsec.py
"""
import importlib.util
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import ipsec  # noqa: E402

# 托盘脚本文件名带连字符，不能直接 import，走 importlib。
# 注意它会 import pystray 吗？——不会，pystray 只在 main() 里 import，
# 所以模块级加载在任何平台上都成立，这正是这个测试能在 Mac 上跑的前提。
_spec = importlib.util.spec_from_file_location(
    "winbar_tray", _ROOT / "winbar" / "ai-limit-tray.py")
tray = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tray)

FAILS = []


def check(name, got, want):
    ok = got == want
    if not ok:
        FAILS.append(name)
    print(f"  {'✓' if ok else '✗'} {name:52} {got}" + ("" if ok else f"  (期望 {want})"))


GOOD = {"level": ipsec.SHIELD_OK, "ip": "1.2.3.4", "error": None,
        "country": "United States", "city": "Los Angeles",
        "is_datacenter": True, "abuser_score": "0.0039 (Low)",
        "dns_ok": True, "dns_servers": [], "dns_leaked": False}
BAD = {"level": ipsec.SHIELD_CRIT, "error": "URLError: timed out"}


def new_state(refresh=600):
    return tray.IPState(lambda: refresh)


print("\n【IPState 抖动抑制】")
st = new_state()
check("初始未检测 → 灰", st.level(), ipsec.SHIELD_IDLE)

st.absorb(GOOD)
check("首次成功 → 绿", st.level(), ipsec.SHIELD_OK)
check("成功不记失败", st.fail, 0)

st.absorb(BAD)
check("失败 1 次 → 仍绿（沿用）", st.level(), ipsec.SHIELD_OK)
check("失败 1 次 → 数据没被覆盖", st.data["ip"], "1.2.3.4")
check("失败 1 次 → fail=1", st.fail, 1)

st.absorb(BAD)
check("失败 2 次 → 仍绿（沿用）", st.level(), ipsec.SHIELD_OK)
check("失败 2 次 → 不进退避", st.in_backoff(), False)

st.absorb(BAD)
check("失败 3 次 → 灰（不是红！）", st.level(), ipsec.SHIELD_IDLE)
check("失败 3 次 → 进退避", st.in_backoff(), True)
check("失败 3 次 → 数据落到失败结果", bool(st.data.get("error")), True)

st.absorb(GOOD)
check("恢复成功 → 绿", st.level(), ipsec.SHIELD_OK)
check("恢复成功 → 清空退避", st.in_backoff(), False)
check("恢复成功 → 清空失败计数", st.fail, 0)

print("\n【连败即使 probe 说 crit 也只给灰】")
st = new_state()
crit_err = {"level": ipsec.SHIELD_CRIT, "error": "URLError: unreachable"}
for _ in range(3):
    st.absorb(crit_err)
check("3 次不可达 → 灰而不是红", st.level(), ipsec.SHIELD_IDLE)

print("\n【真的测出问题才给红】")
st = new_state()
st.absorb({"level": ipsec.SHIELD_CRIT, "error": None, "dns_leaked": True,
           "dns_ok": True, "dns_servers": [{"ip": "1.1.1.1", "country": "China"}]})
check("检测成功且判定 crit → 红", st.level(), ipsec.SHIELD_CRIT)
st2 = new_state()
st2.absorb({"level": ipsec.SHIELD_WARN, "error": None, "ip_changed": True})
check("检测成功且判定 warn → 黄", st2.level(), ipsec.SHIELD_WARN)

print("\n【degraded 不算失败】")
st = new_state()
st.absorb(GOOD)
st.absorb({"level": ipsec.SHIELD_OK, "error": None, "degraded": True, "ip": "9.9.9.9"})
check("ip.net.coffee 挂了不记失败", st.fail, 0)
check("degraded 仍采用新数据", st.data["ip"], "9.9.9.9")
check("degraded 仍是绿", st.level(), ipsec.SHIELD_OK)

print("\n【数据过期后不再沿用】")
st = new_state()
st.absorb(GOOD)
st.good_ts -= tray._IPSEC_STALE_MAX_SEC + 1     # 伪造"上次好数据太老了"
st.absorb(BAD)
check("好数据过期 → 单次失败也变灰", st.level(), ipsec.SHIELD_IDLE)

print("\n【退避时长按 IP 自己的 10 分钟节奏】")
st = new_state(refresh=600)
import time as _t
for _ in range(3):
    st.absorb(BAD)
check("第 3 次失败退避 ≈10 分钟", round(st.backoff_until - _t.time()) in range(595, 605), True)
st.absorb(BAD)
check("第 4 次失败退避 ≈20 分钟", round(st.backoff_until - _t.time()) in range(1195, 1205), True)
for _ in range(10):
    st.absorb(BAD)
check("退避封顶 30 分钟", round(st.backoff_until - _t.time()) <= tray._BACKOFF_MAX_SEC, True)

print("\n【None / 空输入不炸】")
st = new_state()
st.absorb(None)
check("absorb(None) 无副作用", (st.fail, st.data), (0, None))
check("空 data 仍是灰", st.level(), ipsec.SHIELD_IDLE)
st.absorb({"error": None})           # 没 level 字段的畸形返回
check("缺 level 字段 → 灰", st.level(), ipsec.SHIELD_IDLE)

print("\n【盾牌四种形状真的不同（像素比对）】")
imgs = {lvl: tray.render_shield(lvl).convert("L").point(lambda v: 255 if v else 0)
        for lvl in (ipsec.SHIELD_OK, ipsec.SHIELD_WARN,
                    ipsec.SHIELD_CRIT, ipsec.SHIELD_IDLE)}
# 转灰度二值化后比对：颜色被抹掉，剩下的只有形状。任意两个相同即视为回归
levels = list(imgs)
for i in range(len(levels)):
    for j in range(i + 1, len(levels)):
        a, b = levels[i], levels[j]
        same = imgs[a].tobytes() == imgs[b].tobytes()
        check(f"{a} vs {b} 形状不同", same, False)

for lvl in levels:
    ic = tray.render_shield(lvl, 16)
    check(f"{lvl} @16px 能渲染且非空", (ic.size, ic.getbbox() is not None), ((16, 16), True))

print("\n【文案分支】")
check("滥用分取括号里的等级词", tray.ip_abuse_word("0.0039 (Low)"), "Low")
check("滥用分无括号原样透传", tray.ip_abuse_word("High"), "High")
check("滥用分为空 → 空串", tray.ip_abuse_word(None), "")

chips = tray.ip_risk_chips({"is_datacenter": True, "is_vpn": True})
check("机房+VPN 两个标签", len(chips), 2)
check("无标记 → 空列表", tray.ip_risk_chips({}), [])

_txt, _chip, _bad = tray.ip_dns_text({"dns_ok": True, "dns_servers": []})
check("DNS 空 → 安全标签", (_chip != "", _bad), (True, False))
_txt, _chip, _bad = tray.ip_dns_text({"dns_ok": False, "dns_servers": []})
check("DNS 没测到 → 无标签且不告警", (_chip, _bad), ("", False))
_txt, _chip, _bad = tray.ip_dns_text(
    {"dns_ok": True, "dns_leaked": True, "dns_servers": [{"country": "China"}]})
check("DNS 泄露 → 告警标签", _bad, True)
_txt, _chip, _bad = tray.ip_dns_text(
    {"dns_ok": True, "dns_servers": [{"country": "United States"}]})
check("DNS 同国 → 安全不告警", _bad, False)

print("\n【tooltip 分支（Windows 128 字符硬上限）】")
st = new_state()
check("未检测 tooltip 说'检测中'", "…" in tray.make_ip_tooltip(st), True)
st.absorb(GOOD)
tip = tray.make_ip_tooltip(st)
check("成功 tooltip 含 IP", "1.2.3.4" in tip, True)
check("成功 tooltip 含地理", "Los Angeles" in tip, True)
for _ in range(3):
    st.absorb(BAD)
tip = tray.make_ip_tooltip(st)
check("连败 tooltip 说'不可用'而非'不安全'",
      ("不安全" not in tip and "unsafe" not in tip.lower()), True)
st = new_state()
st.absorb(dict(GOOD, city="X" * 200, isp="Y" * 200))
check("超长字段被截到 127", len(tray.make_ip_tooltip(st)) <= 127, True)

print("\n【卡片几何 / 点击热区】")


class StubFont:
    """tkinter.font.Font 的替身：每个字符固定 10px，让像素预算逻辑可被断言。
    真实字宽在 Windows 上才量得准，这里只测"按宽度截断"这套算法本身。"""

    @staticmethod
    def measure(text):
        return len(text) * 10


F = Flyout = tray.Flyout
fly = Flyout(None, {}, {"mode": "5h", "refresh_min": 3}, new_state())
check("放得下不截", fly._fit("abcd", StubFont, 100), "abcd")
check("放不下加省略号", fly._fit("abcdefghij", StubFont, 55), "abcd…")
check("预算为 0 原样返回（不产生半个省略号）", fly._fit("abc", StubFont, 0), "abc")
check("胶囊宽 = 字宽 + 内边距", fly._chip_w("ab", StubFont), 30)

# 4 行 × 20px 从 y0+34 起，最后一行文字底部必须留在卡片内
check("IP 卡片高度装得下 4 行", 34 + 3 * 20 + 16 <= F.IP_CARD_H, True)
h = F.PAD + 2 * (F.CARD_H + F.PAD) + F.IP_CARD_H + F.PAD + 24
check("窗口高把 IP 卡片算进去了", h, F.PAD + 2 * (F.CARD_H + F.PAD) + F.IP_CARD_H + F.PAD + 24)
check("IP 卡片底边在窗口内", F.PAD + 2 * (F.CARD_H + F.PAD) + F.IP_CARD_H <= h, True)


class Ev:
    def __init__(self, x, y):
        self.x, self.y = x, y


fly._ip_rect = (10, 222, 290, 334)
check("卡片中心命中", fly._in_ip_card(Ev(150, 280)), True)
check("卡片上方不命中", fly._in_ip_card(Ev(150, 100)), False)
check("卡片左侧不命中", fly._in_ip_card(Ev(2, 280)), False)
check("卡片下方不命中", fly._in_ip_card(Ev(150, 400)), False)
fly._ip_rect = None
check("卡片没画出来时任何点都不命中", fly._in_ip_card(Ev(150, 280)), False)

print("\n【fetch_ipsec 包住 probe 的异常】")
_orig_probe = ipsec.probe
def _boom(*a, **k):
    raise RuntimeError("probe blew up")
ipsec.probe = _boom
r = tray.fetch_ipsec()
check("probe 抛异常 → 返回失败 dict 而不是崩线程", bool(r.get("error")), True)
st = new_state()
st.absorb(r)
check("异常结果被当失败吸收", st.fail, 1)
ipsec.probe = _orig_probe

print("\n" + ("FAILED: " + ", ".join(FAILS) if FAILS else "ALL PASS"))
sys.exit(1 if FAILS else 0)
