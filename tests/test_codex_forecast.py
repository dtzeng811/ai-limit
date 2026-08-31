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

from usage import CODEX_FORECAST_PAGE, load_codex_forecast, map_reset_forecast  # noqa: E402

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

print("\n【window 档（概率预测）】")
WIN = {"kind": "window", "p24": 25, "p48": 45,
       "source_url": "https://x.com/u/status/9",
       "fetched_at": "2026-08-28T08:00:00+00:00"}
r = load(WIN, "win")
check("合法 window → 透传", r is not None, True)
check("45% → mid 档", r["confidence"], "mid")
check("p60 → high 档", load({**WIN, "p48": 60}, "w60")["confidence"], "high")
check("p29 → low 档", load({**WIN, "p24": 5, "p48": 29}, "w29")["confidence"], "low")
check("window 超 12h → None（比 eta 档严）",
      load({**WIN, "fetched_at": "2026-08-27T23:00:00+00:00"}, "wstale"), None)
check("p 缺失 → None", load({"kind": "window", "p24": 25,
      "fetched_at": "2026-08-28T08:00:00+00:00"}, "wnop"), None)
check("p 越界 → None", load({**WIN, "p48": 145}, "wover"), None)
check("p=True(布尔) → None", load({**WIN, "p24": True}, "wbool"), None)
check("无 url → 退到站点页", load({k: v for k, v in WIN.items()
      if k != "source_url"}, "wnourl")["source_url"], CODEX_FORECAST_PAGE)

print("\n【map_reset_forecast：真实 API 夹具（2026-08-31 实测原样）】")
real = json.loads((pathlib.Path(__file__).parent / "fixtures"
                   / "codex-reset-forecast-2026-08-31.json").read_text())
m = map_reset_forecast(real, now=NOW)
check("真实返回可映射", m is not None, True)
check("p24 取 rounded", m["p24"], 25)
check("p48 取 rounded", m["p48"], 45)
check("url 取官宣推文", "x.com/thsottiaux" in m["source_url"], True)
check("非 dict 入参不崩", map_reset_forecast(None), None)
check("缺 probabilities → None", map_reset_forecast({"mode": "x"}), None)
check("probabilities 缺档 → None",
      map_reset_forecast({"probabilities": {"rounded_24h": 5}}), None)
check("official_signal 缺失 → 退站点页", map_reset_forecast(
      {"probabilities": {"rounded_24h": 1, "rounded_48h": 2}},
      now=NOW)["source_url"], CODEX_FORECAST_PAGE)

print("\n" + ("FAILED: " + ", ".join(FAILS) if FAILS else "ALL PASS"))
sys.exit(1 if FAILS else 0)
