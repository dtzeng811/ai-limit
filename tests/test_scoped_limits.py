#!/usr/bin/env python3
"""parse_scoped_limits 离线单测——夹具照抄 2026-08-26 真实接口返回，不凭记忆编结构。

跑法：python3 tests/test_scoped_limits.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from usage import parse_scoped_limits  # noqa: E402

FAILS = []


def check(name, got, want):
    ok = got == want
    if not ok:
        FAILS.append(name)
    print(f"  {'✓' if ok else '✗'} {name:46} {got}" + ("" if ok else f"  (期望 {want})"))


# 真实返回（2026-08-26 实测，只删了与 limits 无关的顶层键）
REAL = {
    "five_hour": {"utilization": 18.0, "resets_at": "2026-08-27T05:29:59+00:00"},
    "seven_day": {"utilization": 21.0, "resets_at": "2026-08-28T09:59:59+00:00"},
    "limits": [
        {"kind": "session", "group": "session", "percent": 18, "severity": "normal",
         "resets_at": "2026-08-27T05:29:59+00:00", "scope": None, "is_active": False},
        {"kind": "weekly_all", "group": "weekly", "percent": 21, "severity": "normal",
         "resets_at": "2026-08-28T09:59:59+00:00", "scope": None, "is_active": False},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 33, "severity": "normal",
         "resets_at": "2026-08-28T09:59:59+00:00",
         "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
         "is_active": True},
    ],
}

got = parse_scoped_limits(REAL)
check("真实结构 → 1 条", len(got), 1)
check("标签来自服务端", got[0]["label"], "Fable")
check("percent 是已用 → left=100-33", got[0]["left"], 67)
check("reset 透传", got[0]["reset"], "2026-08-28T09:59:59+00:00")

check("无 limits 键（CodeX 形态）→ 空", parse_scoped_limits({"five_hour": {}}), [])
check("limits=None → 空", parse_scoped_limits({"limits": None}), [])
check("非 dict 入参不崩", parse_scoped_limits(None), [])
check("percent 缺失 → 整条跳过", parse_scoped_limits({"limits": [
    {"kind": "weekly_scoped", "scope": {"model": {"display_name": "X"}}}]}), [])
check("percent=True(布尔) → 跳过", parse_scoped_limits({"limits": [
    {"kind": "weekly_scoped", "percent": True,
     "scope": {"model": {"display_name": "X"}}}]}), [])
check("display_name 缺失退到 id", parse_scoped_limits({"limits": [
    {"kind": "weekly_scoped", "percent": 40,
     "scope": {"model": {"id": "fable-5"}}}]})[0]["label"], "fable-5")
check("模型名全缺 → 跳过（不显示错行）", parse_scoped_limits({"limits": [
    {"kind": "weekly_scoped", "percent": 40, "scope": {}}]}), [])
check("scope=None 不崩", parse_scoped_limits({"limits": [
    {"kind": "weekly_scoped", "percent": 40, "scope": None}]}), [])
check("多条 scoped 全保留", len(parse_scoped_limits({"limits": [
    {"kind": "weekly_scoped", "percent": 10,
     "scope": {"model": {"display_name": "A"}}},
    {"kind": "weekly_scoped", "percent": 90,
     "scope": {"model": {"display_name": "B"}}}]})), 2)
check("percent>100（超发）→ 剩余夹到 0", parse_scoped_limits({"limits": [
    {"kind": "weekly_scoped", "percent": 133.4,
     "scope": {"model": {"display_name": "X"}}}]})[0]["left"], 0)

print("\n" + ("FAILED: " + ", ".join(FAILS) if FAILS else "ALL PASS"))
sys.exit(1 if FAILS else 0)
