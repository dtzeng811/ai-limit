#!/usr/bin/env python3
"""ai-limit Windows 托盘版

设计（为什么长这样，跟 macOS 版的差异都是平台规范决定的）：

- macOS 菜单栏允许任意宽度的文本+图像，所以 Mac 版是「环+数字」排一行；
  Windows 通知区域（托盘）每个应用只有一个 16~32px 图标位，塞不下数字。
  因此 Windows 版是**双托盘图标**：Claude 橙环 + CodeX 青绿环（颜色即服务，
  与 Mac 版同一套品牌色），环的填充比例 = 剩余额度。双图标是 Windows
  监控类工具的惯例（TrafficMonitor / CoreTemp 都这么做）。
- 精确数值放**悬浮 tooltip**——tooltip 是 Windows 托盘的一等交互；
  额度告警（<20% / <10%）在图标右下角加黄/红**徽标**，替代 Mac 版的数字变色。
- 左键弹 **flyout 卡片面板**（无边框、置顶、失焦自动关、深浅色跟随系统），
  对齐 Win11 电量/音量 flyout 的体验；布局复刻 Mac 版 panelui 的卡片。
- 右键是原生托盘菜单：立即刷新 / 主窗口切换（5h/7d，影响 tooltip 首行）/ 退出。

行为层与 Mac 版同参数：默认 3 分钟刷新 + 0~20s 随机抖动；单次抓取失败沿用
上一份好数据（连败 3 次或数据老于 15 分钟才报错）；连败后指数退避（上限 30
分钟）。数据层直接复用仓库根的 usage.py。

依赖：pystray + Pillow（tkinter 为 flyout 所需，Windows 官方 Python 自带）。
打包：pyinstaller --onefile --noconsole（见 build-win.ps1）。
"""
import ctypes
import json
import locale as _locale
import os
import pathlib
import queue
import random
import sys
import threading
import time

from PIL import Image, ImageDraw

# PyInstaller onefile 打包后 __file__ 在临时解包目录（sys._MEIPASS），
# usage.py 由 build-win.ps1 的 --add-data 塞在解包根；源码运行时在仓库根。
if getattr(sys, "frozen", False):
    _REPO = pathlib.Path(sys._MEIPASS)
else:
    _REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

import usage  # noqa: E402  数据层（跨平台）
from usage import (  # noqa: E402
    ClaudeWebError, CodexAuthError, CodexWebError,
    WARN_THRESHOLD, CRIT_THRESHOLD,
    live_claude_usage, live_claude_plan, live_codex_web_usage,
    _classify_codex_windows,
    epoch_to_local, ts_to_local,
)


def _window_shorthand(window_minutes):
    """按窗口实际分钟数生成 "5h"/"7d" 短标签（移植自 menubar 版——那个文件
    import rumps，Windows 上不能作为模块引用）。"""
    if not window_minutes:
        return None
    hours = window_minutes / 60
    if hours < 24:
        return f"{round(hours) or 1}h"
    return f"{round(hours / 24)}d"

# ── 常量（与 menubar/ai-limit-app.py 对齐） ──────────────────────────────────
_STATE_PATH = pathlib.Path.home() / ".ai-limit-winbar.json"

_DEFAULT_REFRESH_MIN = 3
_JITTER_MAX_SEC      = 20
_FAIL_GRACE_N        = 3
_STALE_MAX_SEC       = 15 * 60
_BACKOFF_MAX_SEC     = 30 * 60
_PLAN_TTL_SEC        = 12 * 60 * 60

_SERVICE_COLORS = {"claude": "#D97757", "codex": "#10A37F"}
_SERVICE_TITLES = {"claude": "Claude Code", "codex": "CodeX"}
_BADGE_COLORS   = {"warn": "#F5C518", "crit": "#E04343"}

_ICON_PX     = 64      # 高分画布，Windows 按 DPI 自行缩放
_RING_LW     = 9
_TRACK_ALPHA = 72      # 0-255


def _detect_lang() -> str:
    """Windows 中文系统 locale 名是 'Chinese (Simplified)_China'，不带 'zh'
    前缀，usage._detect_lang 的 startswith('zh') 判不出来——这里补上。"""
    env = os.environ.get("AI_LIMIT_LANG", "")
    if env:
        return "zh" if env.lower().startswith("zh") else "en"
    try:
        loc = _locale.getlocale()[0] or os.environ.get("LANG", "") or ""
    except Exception:
        loc = ""
    return "zh" if (loc.lower().startswith("zh") or "chinese" in loc.lower()) else "en"


LANG = _detect_lang()


def tr(zh: str, en: str) -> str:
    return zh if LANG == "zh" else en


# ── 状态持久化 ───────────────────────────────────────────────────────────────
def load_state() -> dict:
    state = {"mode": "5h", "refresh_min": _DEFAULT_REFRESH_MIN}
    try:
        raw = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        if raw.get("mode") in ("5h", "7d"):
            state["mode"] = raw["mode"]
        if raw.get("refresh_min") in (1, 2, 3, 4, 5):
            state["refresh_min"] = raw["refresh_min"]
    except Exception:
        pass
    return state


def save_state(state: dict) -> None:
    try:
        _STATE_PATH.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


# ── 额度档位 / 抖动抑制 / 指数退避（与 Mac 版同语义） ─────────────────────────
def level_of(pct) -> str:
    if pct is None:
        return "ok"
    if pct < CRIT_THRESHOLD:
        return "crit"
    if pct < WARN_THRESHOLD:
        return "warn"
    return "ok"


class ServiceState:
    """单个服务的显示数据 + 失败记账。absorb() 的语义与 Mac 版 _absorb_fetch
    一致：瞬时失败沿用好数据，连败/过期才如实报错，连败后指数退避。"""

    def __init__(self, key: str, refresh_sec_fn):
        self.key = key
        self.data = None            # 显示用 dict（同 Mac 版 fetch 契约）
        self.fail = 0
        self.good_ts = 0.0
        self.backoff_until = 0.0
        self._refresh_sec = refresh_sec_fn

    def absorb(self, new):
        if new is None:
            return
        if "error" not in new:
            self.fail = 0
            self.good_ts = time.time()
            self.backoff_until = 0.0
            self.data = new
            return
        self.fail += 1
        over = self.fail - _FAIL_GRACE_N
        if over >= 0:
            delay = min(self._refresh_sec() * (2 ** over), _BACKOFF_MAX_SEC)
            self.backoff_until = time.time() + delay
        has_good = bool(self.data) and "error" not in self.data
        fresh = (time.time() - self.good_ts) <= _STALE_MAX_SEC
        if has_good and fresh and self.fail < _FAIL_GRACE_N:
            return                  # 吸收：沿用旧好数据
        self.data = new

    def in_backoff(self) -> bool:
        return time.time() < self.backoff_until


# ── 数据抓取（镜像 menubar 的 fetch 契约） ───────────────────────────────────
_plan_cache = {"plan": None, "ts": 0.0}


def _cached_claude_plan():
    now = time.time()
    if now - _plan_cache["ts"] < _PLAN_TTL_SEC:
        return _plan_cache["plan"]
    try:
        plan = live_claude_plan()
        _plan_cache.update({"plan": plan, "ts": now})
        return plan
    except Exception:
        return _plan_cache["plan"]


def fetch_claude():
    import socket, urllib.error
    try:
        data = live_claude_usage()
        five_h = data.get("five_hour") or {}
        seven_d = data.get("seven_day") or {}
        return {
            "5h_left":  int(round(100 - float(five_h.get("utilization", 0)))),
            "7d_left":  int(round(100 - float(seven_d.get("utilization", 0)))),
            "5h_reset": five_h.get("resets_at"),
            "7d_reset": seven_d.get("resets_at"),
            "5h_label": "5h", "7d_label": "7d",
            "plan":     _cached_claude_plan(),
        }
    except ClaudeWebError as e:
        kind = getattr(e, "kind", "generic")
        if kind == "cloudflare":
            return {"error": tr("被拦截，打开用量页勿关", "Blocked; open claude.ai usage page, keep it open")}
        if kind == "auth":
            return {"error": tr("需在浏览器重新登录 claude.ai", "Re-login at claude.ai in browser")}
        return {"error": str(e)}
    except (socket.timeout, TimeoutError):
        return {"error": tr("网络超时", "Network timeout")}
    except urllib.error.URLError:
        return {"error": tr("网络不可用", "Network unavailable")}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def fetch_codex():
    import socket, urllib.error
    try:
        _ts, rl = live_codex_web_usage()
        short_win, long_win = _classify_codex_windows(rl)
        return {
            "5h_left":  int(round(100 - short_win.get("used_percent", 0))) if short_win else None,
            "7d_left":  int(round(100 - long_win.get("used_percent", 0))) if long_win else None,
            "5h_reset": short_win.get("resets_at") if short_win else None,
            "7d_reset": long_win.get("resets_at") if long_win else None,
            "5h_label": _window_shorthand(short_win.get("window_minutes")) if short_win else "5h",
            "7d_label": _window_shorthand(long_win.get("window_minutes")) if long_win else "7d",
            "plan":     rl.get("plan_type") or "?",
        }
    except CodexAuthError:
        return {"error": tr("无 Codex 权限（可能未订阅或需重新登录）",
                            "No Codex access (subscription or re-login needed)")}
    except CodexWebError as e:
        msg = str(e)
        if "timed out" in msg or "urlopen" in msg:
            msg = tr("网络超时", "Network timeout")
        return {"error": msg}
    except (socket.timeout, TimeoutError):
        return {"error": tr("网络超时", "Network timeout")}
    except urllib.error.URLError:
        return {"error": tr("网络不可用", "Network unavailable")}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


_FETCHERS = {"claude": fetch_claude, "codex": fetch_codex}


# ── 托盘图标绘制 ─────────────────────────────────────────────────────────────
def _hex_rgba(hex_color: str, alpha: int = 255):
    raw = hex_color.lstrip("#")
    return (int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16), alpha)


def render_icon(service: str, pct, *, error: bool = False) -> Image.Image:
    """环形进度托盘图标。底环 = 已用（品牌色低透明），实心弧 = 剩余，12 点起
    顺时针——语义与 Mac 版一致。告警加右下角徽标；error 画灰环 + 红徽标。"""
    px = _ICON_PX
    img = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    color = _SERVICE_COLORS.get(service, "#888888")
    box = [_RING_LW // 2 + 2, _RING_LW // 2 + 2, px - _RING_LW // 2 - 2, px - _RING_LW // 2 - 2]

    if error:
        d.arc(box, 0, 360, fill=_hex_rgba(color, 90), width=_RING_LW)
        _draw_badge(d, px, _BADGE_COLORS["crit"])
        return img

    d.arc(box, 0, 360, fill=_hex_rgba(color, _TRACK_ALPHA), width=_RING_LW)
    p = max(0.0, min(100.0, float(pct if pct is not None else 0)))
    if p > 0:
        # PIL 角度：0°=3 点钟方向、顺时针增长（图像坐标 y 向下）。-90 = 12 点起
        d.arc(box, -90, -90 + 360 * p / 100.0, fill=_hex_rgba(color), width=_RING_LW)

    lvl = level_of(pct)
    if lvl in _BADGE_COLORS:
        _draw_badge(d, px, _BADGE_COLORS[lvl])
    return img


def _draw_badge(d: ImageDraw.ImageDraw, px: int, color: str):
    """右下角告警徽标：带白描边的小圆点，深浅色托盘上都能看清。"""
    r = px * 0.18
    cx, cy = px - r - 2, px - r - 2
    d.ellipse([cx - r - 2, cy - r - 2, cx + r + 2, cy + r + 2], fill=(255, 255, 255, 230))
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=_hex_rgba(color))


# ── tooltip / 重置时间格式化 ─────────────────────────────────────────────────
def _fmt_reset(val) -> str:
    """5h_reset 是 ISO 字符串（Claude）或 epoch（CodeX），统一转本地 HH:MM。"""
    if val is None:
        return "?"
    try:
        dt = epoch_to_local(int(val)) if isinstance(val, (int, float)) else ts_to_local(str(val))
        days = (dt.date() - __import__("datetime").datetime.now(dt.tzinfo).date()).days
        day = {0: tr("今天", "today"), 1: tr("明天", "tomorrow")}.get(days, dt.strftime("%m-%d"))
        return f"{day} {dt.strftime('%H:%M')}"
    except Exception:
        return "?"


def make_tooltip(service: str, st: ServiceState, mode: str) -> str:
    """托盘悬浮文本（Windows 上限 128 字符）。首行 = 当前主窗口的值。"""
    title = _SERVICE_TITLES[service]
    d = st.data
    if not d:
        return f"{title} · {tr('读取中…', 'loading…')}"
    if "error" in d:
        return f"{title} ⚠ {d['error']}"[:127]
    first, second = ("5h", "7d") if mode == "5h" else ("7d", "5h")
    parts = []
    for key in (first, second):
        pct = d.get(f"{key}_left")
        label = d.get(f"{key}_label") or key
        if pct is None:
            parts.append(f"{label} ?")
        else:
            parts.append(f"{label} {pct}%（{tr('重置', 'resets')} {_fmt_reset(d.get(f'{key}_reset'))}）")
    stale = f" ·{tr('重试中', 'retrying')}" if st.fail > 0 else ""
    return f"{title} {parts[0]}\n{parts[1]}{stale}"[:127]


# ── flyout 卡片面板（tkinter，仅 Windows 有完整体验；懒加载可脱离测试） ───────
class Flyout:
    """无边框置顶卡片小窗：布局复刻 Mac 版 panelui（服务名+方案 / 环+大号
    百分比+重置时间 ×2），深浅色跟随系统（注册表 AppsUseLightTheme）。
    失焦自动隐藏，贴近点击位置（托盘点击时鼠标就在托盘上）。"""

    W, CARD_H, PAD = 300, 96, 10

    def __init__(self, root, states: dict, state: dict):
        self.root = root
        self.states = states
        self.state = state
        self.win = None

    # 主题
    @staticmethod
    def _dark_mode() -> bool:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
                return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
        except Exception:
            return False

    def toggle(self):
        if self.win is not None and self.win.winfo_exists() and self.win.state() != "withdrawn":
            self.win.withdraw()
            return
        self.show()

    def show(self):
        import tkinter as tk
        import tkinter.font as tkfont
        dark = self._dark_mode()
        bg    = "#202020" if dark else "#F3F3F3"
        card  = "#2B2B2B" if dark else "#FFFFFF"
        fg    = "#FFFFFF" if dark else "#1A1A1A"
        sub   = "#9E9E9E" if dark else "#6E6E6E"
        h = self.PAD + len(self.states) * (self.CARD_H + self.PAD) + 24

        if self.win is None or not self.win.winfo_exists():
            self.win = tk.Toplevel(self.root)
            self.win.overrideredirect(True)
            self.win.attributes("-topmost", True)
            self.canvas = tk.Canvas(self.win, highlightthickness=0, bd=0)
            self.canvas.pack(fill="both", expand=True)
            self.win.bind("<FocusOut>", lambda e: self.win.withdraw())
            self.win.bind("<Escape>", lambda e: self.win.withdraw())

        # 位置：贴近鼠标（= 托盘图标处），右下对齐并夹取到屏幕内
        mx, my = self.root.winfo_pointerx(), self.root.winfo_pointery()
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        x = min(max(mx - self.W // 2, 8), sw - self.W - 8)
        y = my - h - 12 if my > sh // 2 else my + 12
        self.win.geometry(f"{self.W}x{h}+{x}+{max(y, 8)}")
        self.canvas.configure(bg=bg, width=self.W, height=h)
        self.canvas.delete("all")

        f_title = tkfont.Font(family="Microsoft YaHei UI", size=10, weight="bold")
        f_big   = tkfont.Font(family="Segoe UI", size=14, weight="bold")
        f_sub   = tkfont.Font(family="Microsoft YaHei UI", size=8)

        y0 = self.PAD
        mode = self.state["mode"]
        for svc in ("claude", "codex"):
            st = self.states[svc]
            self._card(svc, st, mode, y0, card, fg, sub, f_title, f_big, f_sub)
            y0 += self.CARD_H + self.PAD

        ts = time.strftime("%H:%M:%S")
        retry = [s for s in ("claude", "codex") if self.states[s].fail > 0]
        note = f"{'/'.join(_SERVICE_TITLES[s].split()[0] for s in retry)} {tr('重试中 · ', 'retrying · ')}" if retry else ""
        self.canvas.create_text(self.PAD + 4, y0 + 2, anchor="nw", fill=sub, font=f_sub,
                                text=f"{note}{self.state['refresh_min']} {tr('分钟刷新', 'min refresh')} · {ts}")
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()

    def _card(self, svc, st, mode, y0, card_bg, fg, sub, f_title, f_big, f_sub):
        c = self.canvas
        x0, x1 = self.PAD, self.W - self.PAD
        y1 = y0 + self.CARD_H
        c.create_rectangle(x0, y0, x1, y1, fill=card_bg, width=0)
        color = _SERVICE_COLORS[svc]
        d = st.data or {}

        title = _SERVICE_TITLES[svc]
        plan = (d.get("plan") or "").replace("_", " ")
        plan = " ".join(w[:1].upper() + w[1:] for w in plan.split()) if plan and plan != "?" else ""
        c.create_text(x0 + 12, y0 + 10, anchor="nw", text=title, fill=fg, font=f_title)
        if plan:
            c.create_text(x0 + 16 + f_title.measure(title), y0 + 12, anchor="nw",
                          text=plan, fill=sub, font=f_sub)

        if not d:
            c.create_text(x0 + 12, y0 + 44, anchor="nw", text=tr("读取中…", "loading…"),
                          fill=sub, font=f_sub)
            return
        if "error" in d:
            c.create_text(x0 + 12, y0 + 44, anchor="nw", text=d["error"][:46],
                          fill=sub, font=f_sub)
            return

        first, second = ("5h", "7d") if mode == "5h" else ("7d", "5h")
        ry = y0 + 34
        for key in (first, second):
            pct = d.get(f"{key}_left")
            label = d.get(f"{key}_label") or key
            # 小环
            bx = [x0 + 14, ry, x0 + 14 + 22, ry + 22]
            c.create_oval(bx, outline=self._alpha(color, 0.28), width=3)
            if pct:
                c.create_arc(bx, start=90, extent=-360 * pct / 100, style="arc",
                             outline=color, width=3)
            c.create_text(x0 + 46, ry + 3, anchor="nw", text=label, fill=sub, font=f_sub)
            lvl = level_of(pct)
            num_color = {"warn": "#C8A400", "crit": "#E04343"}.get(lvl, fg)
            c.create_text(x0 + 78, ry - 2, anchor="nw",
                          text="—" if pct is None else f"{pct}%", fill=num_color, font=f_big)
            c.create_text(x1 - 12, ry + 4, anchor="ne",
                          text=_fmt_reset(d.get(f"{key}_reset")), fill=sub, font=f_sub)
            ry += 30

    @staticmethod
    def _alpha(hex_color: str, a: float) -> str:
        """tkinter 不支持 alpha，把品牌色向背景灰混合模拟低透明底环。"""
        r, g, b, _ = _hex_rgba(hex_color)
        mix = lambda v: int(v * a + 128 * (1 - a))
        return f"#{mix(r):02x}{mix(g):02x}{mix(b):02x}"


# ── 主程序 ───────────────────────────────────────────────────────────────────
def _single_instance_guard() -> bool:
    """Windows named mutex，防止双开导致托盘出现四个图标。非 Windows 直接放行。"""
    if sys.platform != "win32":
        return True
    ctypes.windll.kernel32.CreateMutexW(None, False, "ai-limit-winbar-single")
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def main():
    if not _single_instance_guard():
        print("ai-limit tray already running", file=sys.stderr)
        return 1
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # flyout 防糊
        except Exception:
            pass

    import pystray
    import tkinter as tk

    state = load_state()
    ui_queue: "queue.Queue[str]" = queue.Queue()
    stop_evt = threading.Event()
    wake_evt = threading.Event()   # 手动刷新时打断休眠
    refresh_sec = lambda: state["refresh_min"] * 60
    states = {k: ServiceState(k, refresh_sec) for k in ("claude", "codex")}

    root = tk.Tk()
    root.withdraw()
    flyout = Flyout(root, states, state)

    icons: dict[str, "pystray.Icon"] = {}

    def repaint():
        mode = state["mode"]
        for svc, icon in icons.items():
            st = states[svc]
            d = st.data or {}
            if "error" in d:
                icon.icon = render_icon(svc, 0, error=True)
            else:
                pct = d.get(f"{mode}_left")
                if pct is None:
                    pct = d.get("7d_left" if mode == "5h" else "5h_left")
                icon.icon = render_icon(svc, pct if pct is not None else 0)
            icon.title = make_tooltip(svc, st, mode)

    # ── 抓取线程：jitter + 退避跳过 + absorb，然后重画图标 ──
    def fetch_loop():
        first = True
        while not stop_evt.is_set():
            if not first:
                wake_evt.wait(timeout=refresh_sec() + random.uniform(0, _JITTER_MAX_SEC))
                wake_evt.clear()
            first = False
            if stop_evt.is_set():
                break
            for svc, st in states.items():
                if st.in_backoff():
                    continue
                st.absorb(_FETCHERS[svc]())
            repaint()

    # ── 托盘回调（pystray 线程）→ 主线程队列 ──
    def on_flyout(icon, item):
        ui_queue.put("flyout")

    def on_refresh(icon, item):
        for st in states.values():
            st.backoff_until = 0.0
        wake_evt.set()

    def make_mode_setter(m):
        def _set(icon, item):
            state["mode"] = m
            save_state(state)
            repaint()
        return _set

    def on_quit(icon, item):
        stop_evt.set()
        wake_evt.set()
        ui_queue.put("quit")

    def mode_checked(m):
        return lambda item: state["mode"] == m

    menu = pystray.Menu(
        pystray.MenuItem(tr("详情", "Details"), on_flyout, default=True),
        pystray.MenuItem(tr("立即刷新", "Refresh now"), on_refresh),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(tr("主显示 5 小时", "Primary window 5h"),
                         make_mode_setter("5h"), radio=True, checked=mode_checked("5h")),
        pystray.MenuItem(tr("主显示 7 天", "Primary window 7d"),
                         make_mode_setter("7d"), radio=True, checked=mode_checked("7d")),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(tr("退出", "Quit"), on_quit),
    )

    for svc in ("claude", "codex"):
        icons[svc] = pystray.Icon(
            f"ai-limit-{svc}", render_icon(svc, 0),
            f"{_SERVICE_TITLES[svc]} · {tr('读取中…', 'loading…')}", menu)

    threading.Thread(target=fetch_loop, daemon=True).start()
    for icon in icons.values():
        icon.run_detached()

    # ── 主线程：tkinter loop + 队列轮询 ──
    def poll():
        try:
            while True:
                msg = ui_queue.get_nowait()
                if msg == "flyout":
                    flyout.toggle()
                elif msg == "quit":
                    for icon in icons.values():
                        icon.stop()
                    root.destroy()
                    return
        except queue.Empty:
            pass
        root.after(120, poll)

    root.after(120, poll)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
