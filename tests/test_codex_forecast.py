#!/usr/bin/env python3
"""load_codex_forecast 契约校验离线单测（设计文档 2026-08-28 第 2 节全分支）。

跑法：python3 tests/test_codex_forecast.py
"""
import datetime
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from usage import load_codex_forecast  # noqa: E402

FAILS = []


def check(name, got, want):
    ok = got == want
    if not ok:
        FAILS.append(name)
    print(f"  {'✓' if ok else '✗'} {name:46} {got}" + ("" if ok else f"  (期望 {want})"))


# 一律用 UTC 构造：本机是什么时区都不该影响结果（开发机 EDT、CI 是 UTC，
# 第一版夹具按 +08:00 写，在 EDT 机器上「未来的 eta」变成了过去，全盘误判）
NOW = datetime.datetime(2026, 8, 28, 12, 0, tzinfo=datetime.timezone.utc)
TMP = pathlib.Path(tempfile.mkdtemp())


def load(payload, name):
    p = TMP / f"{name}.json"
    p.write_text(payload if isinstance(payload, str)
                 else json.dumps(payload), encoding="utf-8")
    return load_codex_forecast(path=p, now=NOW)


GOOD = {"eta": "2026-08-28T20:00:00+00:00", "confidence": "high",
        "source_url": "https://x.com/u/status/1", "note": "tibo 预告",
        "fetched_at": "2026-08-28T08:00:00+00:00"}

r = load(GOOD, "good")
check("合法数据 → 透传", r is not None, True)
check("confidence 保留", r["confidence"], "high")
check("source_url 保留", r["source_url"], "https://x.com/u/status/1")

check("文件不存在 → None",
      load_codex_forecast(path=TMP / "nope.json", now=NOW), None)
check("JSON 损坏 → None", load("{oops", "bad"), None)
check("非 dict → None", load([1, 2], "list"), None)
check("eta 缺失 → None", load({**GOOD, "eta": None}, "noeta"), None)
check("eta 不可解析 → None", load({**GOOD, "eta": "下周吧"}, "badeta"), None)
check("eta 已过去 → None（预告作废）",
      load({**GOOD, "eta": "2026-08-28T11:59:00+00:00"}, "past"), None)
check("fetched_at 缺失 → None", load({**GOOD, "fetched_at": None}, "nof"), None)
check("fetched_at 超 48h → None（陈旧）",
      load({**GOOD, "fetched_at": "2026-08-26T11:00:00+00:00"}, "stale"), None)
check("fetched_at 刚好 48h 内 → 有效",
      load({**GOOD, "fetched_at": "2026-08-26T13:00:00+00:00"}, "fresh")
      is not None, True)
check("confidence 非法 → 降为 low",
      load({**GOOD, "confidence": "certain"}, "badconf")["confidence"], "low")
check("confidence 缺失 → 降为 low",
      load({k: v for k, v in GOOD.items() if k != "confidence"},
           "noconf")["confidence"], "low")
check("source_url 缺失 → None（行不可点）",
      load({k: v for k, v in GOOD.items() if k != "source_url"},
           "nourl")["source_url"], None)
check("Z 后缀时间兼容",
      load({**GOOD, "eta": "2026-08-28T23:00:00Z",
            "fetched_at": "2026-08-28T02:00:00Z"}, "zulu") is not None, True)

print("\n" + ("FAILED: " + ", ".join(FAILS) if FAILS else "ALL PASS"))
sys.exit(1 if FAILS else 0)
