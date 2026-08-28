#!/usr/bin/env python3
"""winbar scoped（模型限定周期额度）行的离线单测。

跑法：python3 tests/test_winbar_scoped.py
"""
import importlib.util
import os
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

spec = importlib.util.spec_from_file_location(
    "wintray", _ROOT / "winbar" / "ai-limit-tray.py")
tray = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tray)

FAILS = []


def check(name, got, want):
    ok = got == want
    if not ok:
        FAILS.append(name)
    print(f"  {'✓' if ok else '✗'} {name:48} {got}" + ("" if ok else f"  (期望 {want})"))


SC = [{"label": "Fable", "left": 67, "reset": "2026-08-28T10:00:00+00:00"}]

print("\n【svc_card_h 卡片高度】")
check("无 scoped（CodeX 形态）= 基础 96", tray.svc_card_h({"5h_left": 99}), 96)
check("data=None（读取中）= 基础 96", tray.svc_card_h(None), 96)
check("1 条 scoped = 96+30", tray.svc_card_h({"scoped": SC}), 126)
check("2 条 scoped = 96+60", tray.svc_card_h({"scoped": SC * 2}), 156)


class _St:
    def __init__(self, data, fail=0):
        self.data = data
        self.fail = fail


print("\n【tooltip 追加 scoped 行】")
d = {"5h_left": 88, "7d_left": 52, "5h_reset": None, "7d_reset": None,
     "5h_label": "5h", "7d_label": "7d", "scoped": SC}
tip = tray.make_tooltip("claude", _St(d), "5h")
check("含 Fable 行", "Fable 67%" in tip, True)
check("5h 仍是首行", tip.splitlines()[0].endswith("88%）") or "88%" in tip.splitlines()[0], True)
check("不超 Windows 上限 128", len(tip) <= 127, True)
tip7 = tray.make_tooltip("claude", _St(d), "7d")
check("7d 模式首行换 7d", "52%" in tip7.splitlines()[0], True)
check("7d 模式 Fable 仍在", "Fable 67%" in tip7, True)
no_sc = tray.make_tooltip("codex", _St({"5h_left": 99, "7d_left": 31,
                                        "5h_reset": None, "7d_reset": None}), "5h")
check("无 scoped 数据 tooltip 两行不变", len(no_sc.splitlines()), 2)

print("\n【AUTOTEST_FAKE 假数据钩子】")
os.environ["AI_LIMIT_AUTOTEST_FAKE"] = "1 "   # 故意带空格：cmd set 的坑
fc = tray.fetch_claude()
check("开关带尾随空格仍生效(strip)", fc.get("plan"), "Max")
check("夹具含 scoped 行", fc.get("scoped"), [{"label": "Fable", "left": 67,
                                            "reset": "2026-08-28T10:00:00+00:00"}])
check("codex 夹具无 scoped 键", "scoped" in tray.fetch_codex(), False)
os.environ.pop("AI_LIMIT_AUTOTEST_FAKE")

print("\n" + ("FAILED: " + ", ".join(FAILS) if FAILS else "ALL PASS"))
sys.exit(1 if FAILS else 0)
