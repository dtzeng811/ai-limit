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
- 第三个托盘图标是 **IP 安全盾牌**（数据层 ipsec.py）。四种状态的**形状不同**
  （勾/横线/感叹号/圆点），不靠颜色区分——托盘只有 16px，且要照顾色觉障碍。
  左键**直接跳转**检测站点而不是弹 flyout：WebRTC 泄露必须浏览器才测得出，
  面板给不出完整答案，多一次点击不如直接送到能测全的地方。

行为层与 Mac 版同参数：默认 3 分钟刷新 + 0~20s 随机抖动；单次抓取失败沿用
上一份好数据（连败 3 次或数据老于 15 分钟才报错）；连败后指数退避（上限 30
分钟）。数据层直接复用仓库根的 usage.py。

依赖：pystray + Pillow（tkinter 为 flyout 所需，Windows 官方 Python 自带）。
打包：pyinstaller --onefile --noconsole（见 build-win.ps1）。
"""
import ctypes
import json
import os
import pathlib
import queue
import random
import sys
import threading
import time
import webbrowser

from PIL import Image, ImageDraw

# PyInstaller onefile 打包后 __file__ 在临时解包目录（sys._MEIPASS），
# usage.py / ipsec.py 由 build-win.ps1 的 --add-data 塞在解包根；源码运行时在仓库根。
if getattr(sys, "frozen", False):
    _REPO = pathlib.Path(sys._MEIPASS)
else:
    _REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

import ipsec  # noqa: E402  IP 安全度数据层（跨平台）
import quotacore  # noqa: E402  三端共享的行为核心（抖动抑制/退避/常量）
import usage  # noqa: E402  数据层（跨平台）
from usage import (  # noqa: E402
    ClaudeWebError, CodexAuthError, CodexWebError,
    WARN_THRESHOLD, CRIT_THRESHOLD,
    live_claude_usage, live_claude_plan, live_codex_web_usage,
    _classify_codex_windows,
    epoch_to_local, ts_to_local,
)


# 窗口短标签、退避/刷新参数、品牌色、套餐缓存、locale——全部下沉到 quotacore，
# 三端共用同一份实现。下面保留旧的模块级名字作为别名，避免改动本文件里几十处
# 已有的 _FAIL_GRACE_N / _window_shorthand 调用点。
_window_shorthand = quotacore.window_shorthand

# ── 常量（引用 quotacore 单一来源） ──────────────────────────────────────────
_STATE_PATH = pathlib.Path.home() / ".ai-limit-winbar.json"

_DEFAULT_REFRESH_MIN = quotacore.DEFAULT_REFRESH_MIN
_JITTER_MAX_SEC      = quotacore.JITTER_MAX_SEC
_FAIL_GRACE_N        = quotacore.FAIL_GRACE_N
_STALE_MAX_SEC       = quotacore.STALE_MAX_SEC
_BACKOFF_MAX_SEC     = quotacore.BACKOFF_MAX_SEC
_PLAN_TTL_SEC        = quotacore.PLAN_TTL_SEC

_SERVICE_COLORS = quotacore.SERVICE_COLORS
_SERVICE_TITLES = quotacore.SERVICE_TITLES
_BADGE_COLORS   = {"warn": "#F5C518", "crit": "#E04343"}

_ICON_PX     = 64      # 高分画布，Windows 按 DPI 自行缩放
_RING_LW     = 9
_TRACK_ALPHA = 72      # 0-255

# IP 安全盾牌。轮廓比环细一档：盾形本身占的面积比环大，同样 9px 会糊成一块实心
_SHIELD_LW     = 7
_SHIELD_COLORS = {
    ipsec.SHIELD_OK:   "#3FB950",
    ipsec.SHIELD_WARN: "#F5C518",
    ipsec.SHIELD_CRIT: "#E04343",
    ipsec.SHIELD_IDLE: "#8A8A8A",
}
_IPSEC_REFRESH_SEC   = quotacore.IPSEC_REFRESH_SEC
_IPSEC_STALE_MAX_SEC = quotacore.IPSEC_STALE_MAX_SEC


LANG = quotacore.detect_lang()


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


# ── 开机自启（对应 Mac 版的 LaunchAgent plist） ───────────────────────────────
# 走 HKCU\...\Run 而不是「启动」文件夹的快捷方式：快捷方式要 COM/pywin32 才能
# 生成，为一个开关引入原生依赖不值当；winreg 是标准库，且 HKCU 不需要管理员权限。
_RUN_KEY_PATH   = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_VALUE_NAME = "ai-limit-tray"

# 源码运行时要拼进命令行的脚本路径。在模块级取而不是在函数里现取：PyInstaller
# 打包后 __file__ 指向 _MEIPASS 临时解包目录（每次启动都不一样），只有源码运行
# 这条分支会用到它，模块级固化下来也方便单测替换。
_SCRIPT_PATH = pathlib.Path(__file__).resolve()


def _winreg_or_none():
    """非 Windows 上 `import winreg` 直接 ImportError。集中在这一处降级，
    下面每个函数拿到 None 就走「查询=未开启、设置=静默无操作」，这样开发机
    （macOS）上加载本模块跑离线单测不会炸。"""
    if sys.platform != "win32":
        return None
    try:
        import winreg
        return winreg
    except ImportError:
        return None


def _pythonw_path() -> str:
    """源码运行时用来起进程的解释器。

    必须是 pythonw.exe 而不是 python.exe：后者带控制台子系统，开机自启会在
    桌面上弹一个黑窗口，一个常驻托盘 App 不该有这种可见副作用。同目录换名
    即可（venv / 系统 Python 的布局都是 python.exe 与 pythonw.exe 并排）；
    换不出来（比如某些精简发行版没带 pythonw）就退回原解释器——宁可弹个黑窗，
    也好过开机根本起不来。
    """
    exe = pathlib.Path(sys.executable)
    cand = exe.with_name("pythonw.exe")
    return str(cand if cand.exists() else exe)


def autostart_command() -> str:
    """Run 键该写的值 = 开机时执行的命令行。

    路径一律套双引号：默认安装位置 `C:\\Program Files\\...` 带空格，不加引号
    时 Windows 会把第一个空格前的片段当作可执行文件名，开机静默启动失败，
    而且失败得毫无痕迹——这是这个功能最容易踩的坑。
    """
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    return f'"{_pythonw_path()}" "{_SCRIPT_PATH}"'


def autostart_enabled() -> bool:
    """勾选状态**每次都实时读注册表**，不落 ~/.ai-limit-winbar.json。

    因为这个开关有 App 之外的入口：任务管理器「启动」页、msconfig、各种优化
    软件都能把它关掉。本地状态文件一旦和注册表不一致，菜单就会长期说谎，而
    注册表本身就是唯一权威——那再存一份就纯属自找麻烦。
    """
    winreg = _winreg_or_none()
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY_PATH) as key:
            value, _type = winreg.QueryValueEx(key, _RUN_VALUE_NAME)
        return bool(value)
    except Exception:
        # 值不存在（FileNotFoundError）是最常见的一支，等同于「没开」；
        # 组策略禁读之类的 OSError 也只能当没开，不能让菜单构造抛异常
        return False


def set_autostart(enabled: bool) -> bool:
    """写入 / 删除 Run 值。返回是否真的达成目标（供自愈和单测断言用）。

    注册表可能被组策略或安全软件锁死，写失败在企业环境里是常态。托盘 App
    没有可靠的弹窗位置，失败就静默返回 False：菜单下次展开时读到的仍是「关」，
    用户看得见结果，也不会因为一个开关把整个 App 崩掉。
    """
    winreg = _winreg_or_none()
    if winreg is None:
        return False
    try:
        if enabled:
            # CreateKeyEx 而不是 OpenKey：Run 键在极少数精简 / 全新用户配置里
            # 确实不存在。access 显式给 KEY_WRITE 而不用 CreateKey 的默认
            # KEY_ALL_ACCESS——只是写个值，没必要申请这个键的全部权限
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _RUN_KEY_PATH, 0,
                                    winreg.KEY_WRITE) as key:
                winreg.SetValueEx(key, _RUN_VALUE_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY_PATH, 0,
                                    winreg.KEY_SET_VALUE) as key:
                    winreg.DeleteValue(key, _RUN_VALUE_NAME)
            except FileNotFoundError:
                pass          # 键或值本来就不存在 = 已经是"关"，算达成而不是失败
        return True
    except Exception:
        return False


def toggle_autostart() -> bool:
    """菜单点击的入口：以注册表当前值为基准取反。"""
    return set_autostart(not autostart_enabled())


def sync_autostart_path() -> bool:
    """启动时的路径漂移自愈：值还在但指向的不是当前可执行路径，就地改写。

    为什么必须做：用户把 exe 从下载目录挪进 Program Files、覆盖安装到新的版本
    目录、或者换了盘符之后，Run 键里留的还是旧路径。菜单读到「值存在」照样打
    勾显示已开启，可开机时那条路径已经不在了 → 静默不启动。用户看不到任何报错，
    也不会想到去翻注册表，只会认为这个功能坏了。App 自己每次启动都知道真实路径，
    顺手校正一次是成本最低的修复点。

    只在**已开启**（值存在）时改写：值不存在说明用户没开或主动关过，绝不能因为
    跑了一次就被偷偷加上开机自启。
    """
    winreg = _winreg_or_none()
    if winreg is None:
        return False
    want = autostart_command()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY_PATH) as key:
            current, _type = winreg.QueryValueEx(key, _RUN_VALUE_NAME)
    except Exception:
        return False
    if str(current) == want:
        return False
    return set_autostart(True)


# ── 额度档位 / 抖动抑制 / 指数退避（与 Mac 版同语义） ─────────────────────────
def level_of(pct) -> str:
    if pct is None:
        return "ok"
    if pct < CRIT_THRESHOLD:
        return "crit"
    if pct < WARN_THRESHOLD:
        return "warn"
    return "ok"


# 额度与 IP 的失败记账都用 quotacore.AbsorbState（合并前是本文件的 ServiceState /
# IPState 两个类，语义完全一致，只差 stale 上限和 failed 判定）。
# 构造走 quotacore：额度用默认，IP 用 make_ip_state（40 分钟过期 + degraded 不算失败）。
def ip_level(state) -> str:
    """IP 盾牌档位。probe() 在 claude.ai 不可达时返回 level=crit，但只要 absorb
    判定成失败态（连败/过期），一律改判灰——红留给"真的测出问题"，灰表示
    "没测到"。这条是设计文档第 4 节写死的规则，从 IPState.level() 抽出来。"""
    d = state.data
    if not d or quotacore.ip_failed(d):
        return ipsec.SHIELD_IDLE
    return d.get("level") or ipsec.SHIELD_IDLE


# ── 数据抓取（镜像 menubar 的 fetch 契约） ───────────────────────────────────
def _cached_claude_plan():
    return quotacore.cached_claude_plan(live_claude_plan)


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


def fetch_ipsec():
    """跑一轮 IP 安全检测。

    ipsec.probe() 号称任何单探针失败都不抛异常，但它跑在共用的抓取线程里——
    真漏出一个异常会连额度轮询一起打死，所以这里再兜一层。
    """
    try:
        return ipsec.probe()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "level": ipsec.SHIELD_IDLE}


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


def _shield_pts(px: int):
    """盾形五边形顶点：顶边平、两侧竖下、底部收成尖。

    坐标按 64px 画布手调后按比例缩放——托盘实际尺寸随 DPI 在 16~32px 之间，
    等比缩放能保证小尺寸下轮廓仍读得出是"盾"而不是一坨。
    """
    s = px / 64.0
    return [(6 * s, 6 * s), (58 * s, 6 * s), (58 * s, 32 * s),
            (32 * s, 59 * s), (6 * s, 32 * s)]


def render_shield(level: str, px: int = _ICON_PX) -> Image.Image:
    """IP 安全盾牌托盘图标。

    四种状态**形状不同**（勾 / 横线 / 感叹号 / 圆点），不只是换色：托盘图标
    实际只有 16~32px，颜色在浅色任务栏上区分度本就有限，加上色觉障碍用户
    分不清红绿——形状是唯一可靠的语义通道。颜色只是冗余强化。
    """
    img = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    color = _hex_rgba(_SHIELD_COLORS.get(level, _SHIELD_COLORS[ipsec.SHIELD_IDLE]))
    lw = max(2, round(_SHIELD_LW * px / _ICON_PX))
    s = px / 64.0

    pts = _shield_pts(px)
    # 用 line 闭合描边而不是 polygon(outline=)：polygon 的 width 参数在旧
    # Pillow 上不生效，line + joint="curve" 各版本都有，拐角也更圆润
    d.line(pts + [pts[0]], fill=color, width=lw, joint="curve")

    # 内符号一律待在盾牌"肩部以上"的宽敞区（y<38）：底下是收尖的三角，
    # 缩到 16px 时任何伸进去的笔画都会和轮廓糊成一团
    if level == ipsec.SHIELD_OK:            # 勾
        d.line([(21 * s, 27 * s), (28 * s, 35 * s), (44 * s, 17 * s)],
               fill=color, width=lw, joint="curve")
    elif level == ipsec.SHIELD_WARN:        # 横线
        d.line([(21 * s, 26 * s), (43 * s, 26 * s)], fill=color, width=lw)
    elif level == ipsec.SHIELD_CRIT:        # 感叹号（竖线 + 点）
        # 竖线起点压到 y=19 而不是 14：盾牌顶边在 y≈8，14 在 64px 下看着没事，
        # 缩到 16px 只差 1.5px，抗锯齿一糊就跟顶边并成一条，读起来像"T"而不是
        # 感叹号——四个符号里唯独红色最不该认错。点也跟着下移保持间距。
        d.line([(32 * s, 19 * s), (32 * s, 31 * s)], fill=color, width=lw)
        r = max(lw / 2.0, 1.0)
        d.ellipse([32 * s - r, 38 * s - r, 32 * s + r, 38 * s + r], fill=color)
    else:                                    # 圆点
        r = 7 * s
        d.ellipse([32 * s - r, 25 * s - r, 32 * s + r, 25 * s + r], fill=color)
    return img


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


def make_tooltip(service: str, st, mode: str) -> str:
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


# ── IP 安全卡片文案（判定在 ipsec，i18n 与格式化留在各端 UI 层） ─────────────
def _IP_TITLE() -> str:
    return tr("IP 安全", "IP security")


def ip_level_word(level: str) -> str:
    return {
        ipsec.SHIELD_OK:   tr("正常", "OK"),
        ipsec.SHIELD_WARN: tr("出口 IP 有变化", "Exit IP changed"),
        ipsec.SHIELD_CRIT: tr("有风险", "At risk"),
    }.get(level, tr("未检测到", "No result"))


def ip_abuse_word(score) -> str:
    """abuser_score 形如 "0.0039 (Low)"。卡片一行放不下小数，只取括号里的
    等级词——用户要的是"高不高"，不是第四位小数。"""
    if not score:
        return ""
    s = str(score)
    if "(" in s and ")" in s:
        return s[s.index("(") + 1:s.index(")")].strip()
    return s


def ip_risk_chips(d) -> list:
    """风险标签。机房 IP 不点亮盾牌（长期走机房出口的人会被常亮告警淹没），
    但必须在卡片里如实标出来——它是 Claude 风控最看重的信号之一。"""
    out = []
    if d.get("is_datacenter"):
        out.append(tr("机房 IP", "Datacenter"))
    if d.get("is_vpn"):
        out.append("VPN")
    if d.get("is_proxy"):
        out.append(tr("代理", "Proxy"))
    if d.get("is_tor"):
        out.append("Tor")
    if d.get("is_abuser"):
        out.append(tr("滥用", "Abuser"))
    return out


def ip_dns_text(d):
    """DNS 行 → (正文, 标签, 标签是否告警)。三态必须分清：

    - 有出口且异国      → 真泄露
    - 有出口且同国 / 空 → 安全（Clash fake-IP 下 DNS 不出公网，恒为空，
                          这在参考站点的判定里正是"安全"，不是检测失败）
    - dns_ok=False      → 本轮没测到，既不能报安全也不能报泄露
    """
    if d.get("dns_leaked"):
        # dns_servers 实际是纯 IP 字符串数组，国家要另查——统一走 ipsec 的
        # 归一化取值（早期这里按 dict 假设写 s.get()，非空时直接抛异常）
        names = [(ipsec.dns_server_country(s) or "?").title()
                 for s in (d.get("dns_servers") or [])]
        # 只报出口在哪国——"与本机不同国"这层意思由红色 + 泄露标签承担，
        # 一行 190px 塞不下完整句子
        return (tr("出口在 ", "Resolver in ") + "/".join(names[:2]),
                tr("泄露", "Leak"), True)
    if not d.get("dns_ok"):
        return tr("本轮未测到", "No result this round"), "", False
    if d.get("dns_servers"):
        return tr("出口与本机同国", "Resolver in same country"), tr("安全", "Safe"), False
    return tr("未暴露出口（加密）", "No exposed resolver"), tr("安全", "Safe"), False


def make_ip_tooltip(ip_state) -> str:
    """盾牌托盘悬浮文本（Windows 上限 128 字符）。"""
    head = _IP_TITLE()
    d = ip_state.data
    if not d:
        return f"{head} · {tr('检测中…', 'checking…')}"
    if not d.get("error"):
        loc = " · ".join(x for x in (d.get("country") or d.get("loc"), d.get("city")) if x)
        second = f"{d.get('ip') or '?'}" + (f" · {loc}" if loc else "")
        if d.get("degraded"):
            second += tr(" · 风险数据不可用", " · risk data unavailable")
        return f"{head} {ip_level_word(ip_level(ip_state))}\n{second}"[:127]
    # 连败落到这里：说的是"没测到"，不是"测到问题"——措辞不能像告警
    return f"{head} · {tr('检测不可用', 'check unavailable')}\n{d.get('error') or ''}"[:127]


# ── flyout 卡片面板（tkinter，仅 Windows 有完整体验；懒加载可脱离测试） ───────
class Flyout:
    """无边框置顶卡片小窗：布局复刻 Mac 版 panelui（服务名+方案 / 环+大号
    百分比+重置时间 ×2），深浅色跟随系统（注册表 AppsUseLightTheme）。
    失焦自动隐藏，贴近点击位置（托盘点击时鼠标就在托盘上）。

    第三张 IP 安全卡片整块是点击热区（Canvas 上判坐标）——它给不出 WebRTC
    的答案，点进网页才是完整检测，所以卡片本身就是那个入口。
    """

    W, CARD_H, IP_CARD_H, PAD = 300, 96, 112, 10

    def __init__(self, root, states: dict, state: dict, ip_state=None):
        self.root = root
        self.states = states
        self.state = state
        self.ip_state = ip_state
        self.win = None
        self._hide_job = None
        self._ip_rect = None     # IP 卡片的点击热区（画完才知道，见 _ip_card）
        self._ip_hint = None     # 悬停提示的 canvas item id
        self._ip_hover = False

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
        if self.ip_state is not None:
            h += self.IP_CARD_H + self.PAD

        if self.win is None or not self.win.winfo_exists():
            self.win = tk.Toplevel(self.root)
            self.win.overrideredirect(True)
            self.win.attributes("-topmost", True)
            self.canvas = tk.Canvas(self.win, highlightthickness=0, bd=0)
            self.canvas.pack(fill="both", expand=True)
            self.win.bind("<Escape>", lambda e: self.win.withdraw())
            # Canvas 是自绘的，没有控件层级可以挂事件，只能整块收鼠标事件后
            # 自己判坐标落在哪张卡片上
            self.canvas.bind("<Button-1>", self._on_click)
            self.canvas.bind("<Motion>", self._on_motion)
            self.canvas.bind("<Leave>", lambda e: self._set_hover(False))
            # 不能在这里直接 bind FocusOut：Windows 禁止后台进程抢焦点，
            # focus_force() 常常拿不到（或瞬间被收回）→ FocusOut 立即触发
            # → 窗口弹出几毫秒就被自己收掉，肉眼只觉得"点了没反应"。
            # FocusOut 延迟到确认真的拿到焦点后再武装（见 show() 尾部）。

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

        self._ip_rect = None
        self._ip_hover = False
        if self.ip_state is not None:
            self._ip_card(y0, card, fg, sub, f_title, f_sub)
            y0 += self.IP_CARD_H + self.PAD

        ts = time.strftime("%H:%M:%S")
        retry = [s for s in ("claude", "codex") if self.states[s].fail > 0]
        note = f"{'/'.join(_SERVICE_TITLES[s].split()[0] for s in retry)} {tr('重试中 · ', 'retrying · ')}" if retry else ""
        self.canvas.create_text(self.PAD + 4, y0 + 2, anchor="nw", fill=sub, font=f_sub,
                                text=f"{note}{self.state['refresh_min']} {tr('分钟刷新', 'min refresh')} · {ts}")
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()

        # 关闭时机（Windows flyout 惯例是点外面即关，但 tkinter 拿不到全局
        # 鼠标钩子，用三重保底代替）：
        # 1. 拿到焦点时：失焦即关（延迟武装，见上面 bind 处注释）
        # 2. 拿不到焦点时：12 秒自动收起
        # 3. 任何时候：再点托盘图标切换收起 / Esc
        if self._hide_job is not None:
            try:
                self.root.after_cancel(self._hide_job)
            except Exception:
                pass
        self._hide_job = self.root.after(12000, self.win.withdraw)

        def _arm_focusout():
            try:
                if self.win.focus_displayof() is not None:
                    self.win.bind("<FocusOut>", lambda e: self.win.withdraw())
            except Exception:
                pass
        self.root.after(200, _arm_focusout)

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

    # ── IP 安全卡片 ──────────────────────────────────────────────────────────
    def _ip_card(self, y0, card_bg, fg, sub, f_title, f_sub):
        c = self.canvas
        x0, x1 = self.PAD, self.W - self.PAD
        y1 = y0 + self.IP_CARD_H
        c.create_rectangle(x0, y0, x1, y1, fill=card_bg, width=0)
        self._ip_rect = (x0, y0, x1, y1)

        lvl = ip_level(self.ip_state)
        accent = _SHIELD_COLORS.get(lvl, _SHIELD_COLORS[ipsec.SHIELD_IDLE])
        d = self.ip_state.data or {}

        # 标题行：小盾牌 + 标题 + 右上状态点（与 Claude/CodeX 卡片同构）
        self._shield_glyph(x0 + 12, y0 + 8, 18, lvl, accent)
        c.create_text(x0 + 36, y0 + 9, anchor="nw", text=_IP_TITLE(), fill=fg, font=f_title)
        c.create_oval(x1 - 20, y0 + 13, x1 - 12, y0 + 21, fill=accent, outline="")
        # 悬停才浮出的可点提示：常驻会和状态点抢眼球，卡片本身已经够满了
        self._ip_hint = c.create_text(
            x1 - 26, y0 + 11, anchor="ne", state="hidden", fill=sub, font=f_sub,
            text=tr("在网页中检测 ↗", "Check in browser ↗"))

        lx, vx = x0 + 12, x0 + 74      # 标签列 / 值列
        ry = y0 + 34
        step = 20

        def label(text):
            c.create_text(lx, ry + 1, anchor="nw", text=text, fill=sub, font=f_sub)

        if not d:
            label(tr("状态", "Status"))
            c.create_text(vx, ry + 1, anchor="nw", text=tr("检测中…", "checking…"),
                          fill=sub, font=f_sub)
            return
        if d.get("error"):
            # 连败才会走到这里。措辞是"检测不可用"而不是"不安全"——
            # 灰盾 + 这行字共同表达"没测到"，别让用户以为网络被判危险
            label(tr("状态", "Status"))
            c.create_text(vx, ry + 1, anchor="nw", text=tr("检测不可用", "Check unavailable"),
                          fill=sub, font=f_sub)
            ry += step
            c.create_text(lx, ry + 1, anchor="nw", text=str(d.get("error") or "")[:44],
                          fill=sub, font=f_sub)
            return

        rx = x1 - 12                   # 值区右边界，所有行共用这个预算

        # 出口 IP
        label(tr("出口 IP", "Exit IP"))
        ip_txt = d.get("ip") or "—"
        c.create_text(vx, ry + 1, anchor="nw", text=ip_txt, fill=fg, font=f_sub)
        loc = " · ".join(x for x in (d.get("country") or d.get("loc"), d.get("city")) if x)
        if loc:
            lx2 = vx + f_sub.measure(ip_txt) + 10
            c.create_text(lx2, ry + 1, anchor="nw", fill=sub, font=f_sub,
                          text=self._fit(loc, f_sub, rx - lx2))
        ry += step

        # 风险
        label(tr("风险", "Risk"))
        if d.get("degraded"):
            c.create_text(vx, ry + 1, anchor="nw", text=tr("不可用", "Unavailable"),
                          fill=sub, font=f_sub)
        else:
            cx = vx
            for chip in ip_risk_chips(d):
                w = self._chip_w(chip, f_sub)
                if cx + w > rx:
                    break              # 一行放不下就不放了：截半个胶囊比少一个更难看
                danger = chip in ("Tor", tr("滥用", "Abuser"))
                cx = self._chip(cx, ry - 1, chip, f_sub, card_bg,
                                _BADGE_COLORS["crit"] if danger else sub)
            score = ip_abuse_word(d.get("abuser_score"))
            score_txt = f"{tr('滥用分', 'Abuse')} {score}" if score else ""
            if score_txt and cx + f_sub.measure(score_txt) <= rx:
                c.create_text(cx, ry + 1, anchor="nw", text=score_txt, fill=sub, font=f_sub)
            elif cx == vx:
                c.create_text(vx, ry + 1, anchor="nw", text=tr("无标记", "No flags"),
                              fill=sub, font=f_sub)
        ry += step

        # DNS
        label("DNS")
        dns_txt, dns_chip, dns_bad = ip_dns_text(d)
        chip_w = (self._chip_w(dns_chip, f_sub) + 2) if dns_chip else 0
        dns_txt = self._fit(dns_txt, f_sub, rx - vx - chip_w - 8)
        c.create_text(vx, ry + 1, anchor="nw", text=dns_txt,
                      fill=fg if not dns_bad else _BADGE_COLORS["crit"], font=f_sub)
        if dns_chip:
            self._chip(vx + f_sub.measure(dns_txt) + 8, ry - 1, dns_chip, f_sub, card_bg,
                       _BADGE_COLORS["crit"] if dns_bad else _SHIELD_COLORS[ipsec.SHIELD_OK])
        ry += step

        # WebRTC：纯 HTTP 客户端测不了（要浏览器建 RTCPeerConnection 收 ICE），
        # 如实标注而不是伪装成已检测，箭头把用户引到能测的地方
        label("WebRTC")
        c.create_text(vx, ry + 1, anchor="nw", text=tr("需浏览器检测", "Browser needed"),
                      fill=sub, font=f_sub)
        c.create_text(x1 - 12, ry + 1, anchor="ne", text="→", fill=sub, font=f_sub)

    @staticmethod
    def _chip_w(text, font) -> int:
        return font.measure(text) + 10

    def _chip(self, x, y, text, font, card_bg, accent):
        """标签胶囊，返回下一个可用 x。tkinter Canvas 没有圆角矩形原语，用直角
        小矩形——这个尺寸下圆角肉眼看不出来，不值得拿 arc 拼一个。"""
        w = self._chip_w(text, font)
        self.canvas.create_rectangle(x, y, x + w, y + 16,
                                     fill=self._mix(card_bg, accent, 0.22), width=0)
        self.canvas.create_text(x + 5, y + 2, anchor="nw", text=text, fill=accent, font=font)
        return x + w + 6

    @staticmethod
    def _fit(text, font, max_px) -> str:
        """按**像素**宽截断而不是按字数。中英混排下 24 个字符可能是 130px 也可能
        是 260px，按字数截要么白留半张卡片、要么直接冲出卡片右边。"""
        if max_px <= 0 or font.measure(text) <= max_px:
            return text
        while text and font.measure(text + "…") > max_px:
            text = text[:-1]
        return text + "…"

    def _shield_glyph(self, x, y, size, level, color):
        """卡片标题前的小盾牌。几何直接复用托盘图标的 _shield_pts——两处形状
        必须一模一样，用户才能把这张卡片和托盘上那个图标对上号。"""
        c = self.canvas
        s = size / 64.0
        lw = 2
        pts = []
        for px_, py_ in _shield_pts(size):
            pts += [x + px_, y + py_]
        c.create_polygon(pts, outline=color, fill="", width=lw)
        if level == ipsec.SHIELD_OK:
            c.create_line(x + 21 * s, y + 27 * s, x + 28 * s, y + 35 * s,
                          x + 44 * s, y + 17 * s, fill=color, width=lw)
        elif level == ipsec.SHIELD_WARN:
            c.create_line(x + 21 * s, y + 26 * s, x + 43 * s, y + 26 * s, fill=color, width=lw)
        elif level == ipsec.SHIELD_CRIT:
            c.create_line(x + 32 * s, y + 13 * s, x + 32 * s, y + 28 * s, fill=color, width=lw)
            c.create_oval(x + 32 * s - 1, y + 34 * s - 1, x + 32 * s + 1, y + 34 * s + 1,
                          fill=color, outline=color)
        else:
            r = 6 * s
            c.create_oval(x + 32 * s - r, y + 25 * s - r, x + 32 * s + r, y + 25 * s + r,
                          fill=color, outline=color)

    # ── 卡片交互 ────────────────────────────────────────────────────────────
    def _in_ip_card(self, ev) -> bool:
        r = self._ip_rect
        return bool(r) and r[0] <= ev.x <= r[2] and r[1] <= ev.y <= r[3]

    def _on_click(self, ev):
        if self._in_ip_card(ev):
            webbrowser.open(ipsec.SITE_URL)
            # 跳走了就收起面板：浏览器会抢焦点，留个孤零零的置顶小窗很碍事
            try:
                self.win.withdraw()
            except Exception:
                pass

    def _on_motion(self, ev):
        self._set_hover(self._in_ip_card(ev))

    def _set_hover(self, on: bool):
        if on == self._ip_hover:
            return                       # Motion 每像素都触发，不去重会白刷一堆 configure
        self._ip_hover = on
        try:
            if self._ip_hint is not None:
                self.canvas.itemconfigure(self._ip_hint, state="normal" if on else "hidden")
            self.canvas.configure(cursor="hand2" if on else "")
        except Exception:
            pass

    @staticmethod
    def _mix(c1: str, c2: str, t: float) -> str:
        """两个 hex 色线性插值。深浅两套主题下都要得到"比卡片底色深一层"的
        标签底，所以混的是卡片底色而不是固定灰。"""
        a, b = _hex_rgba(c1), _hex_rgba(c2)
        return "#" + "".join(f"{int(a[i] * (1 - t) + b[i] * t):02x}" for i in range(3))

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


_LOG_PATH = pathlib.Path(os.environ.get("TEMP") or "/tmp") / "ai-limit-tray.log"


def _log_exc(where: str):
    """异常落盘。托盘 App 没有可见的 stderr（--noconsole 打包后更没有），
    tkinter 回调异常默认只往 stderr 打——不落盘用户报障时什么线索都没有。"""
    import traceback
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n[{time.strftime('%F %T')}] {where}\n{traceback.format_exc()}")
    except Exception:
        pass


def main():
    if not _single_instance_guard():
        print("ai-limit tray already running", file=sys.stderr)
        return 1
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # flyout 防糊
        except Exception:
            pass
    # 每次启动校正一次 Run 键里的路径（挪过 exe / 覆盖安装后它会指向旧位置，
    # 详见 sync_autostart_path 的注释）。函数内部已经吞掉全部异常，不再包一层
    sync_autostart_path()

    import pystray
    import tkinter as tk

    state = load_state()
    ui_queue: "queue.Queue[str]" = queue.Queue()
    stop_evt = threading.Event()
    wake_evt = threading.Event()   # 手动刷新时打断休眠
    ip_force = threading.Event()   # 盾牌菜单的「立即刷新」跳过 10 分钟节流
    refresh_sec = lambda: state["refresh_min"] * 60
    states = {k: quotacore.AbsorbState(refresh_sec) for k in ("claude", "codex")}
    ip_state = quotacore.make_ip_state(lambda: _IPSEC_REFRESH_SEC)

    root = tk.Tk()
    root.withdraw()
    # tkinter 回调里的异常默认打 stderr 就没了，统一落盘（%TEMP%\ai-limit-tray.log）
    root.report_callback_exception = lambda *a: _log_exc("tk-callback")
    flyout = Flyout(root, states, state, ip_state)

    icons: dict[str, "pystray.Icon"] = {}   # 环图标（claude/codex），盾牌另存
    shield: "pystray.Icon | None" = None

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

    def repaint_shield():
        if shield is None:
            return
        shield.icon = render_shield(ip_level(ip_state))
        shield.title = make_ip_tooltip(ip_state)

    # ── 抓取线程：jitter + 退避跳过 + absorb，然后重画图标 ──
    def fetch_loop():
        first = True
        ip_due = 0.0        # 0 = 首轮立刻测一次，之后按 _IPSEC_REFRESH_SEC 走
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

            # IP 检测挂在同一个线程但走自己的时钟：一轮探针含 DNS 轮询要好几秒，
            # 放在额度重画之后，别让盾牌拖慢用户真正天天看的额度数字
            forced = ip_force.is_set()      # 手动刷新无视节流和退避
            if forced:
                ip_force.clear()
            if forced or (time.time() >= ip_due and not ip_state.in_backoff()):
                ip_state.absorb(fetch_ipsec())
                ip_due = time.time() + _IPSEC_REFRESH_SEC + random.uniform(0, _JITTER_MAX_SEC)
                repaint_shield()

    # ── 托盘回调（pystray 线程）→ 主线程队列 ──
    def on_flyout(icon, item):
        ui_queue.put("flyout")

    def on_refresh(icon, item):
        for st in states.values():
            st.backoff_until = 0.0
        wake_evt.set()

    def on_ip_open(icon, item):
        # pystray 回调在它自己的线程里，一律经 ui_queue 交给主线程——
        # webbrowser.open 在 Windows 上会起子进程，跨线程调用不值得赌
        ui_queue.put("ip-open")

    def on_ip_refresh(icon, item):
        ip_state.backoff_until = 0.0
        ip_force.set()
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

    def on_autostart(icon, item):
        # 写失败（组策略锁注册表）只返回 False，不弹窗也不抛：托盘回调里抛异常
        # 会被 pystray 吞掉或打死消息循环，而勾选状态下次展开菜单自会照实反映
        toggle_autostart()

    def mode_checked(m):
        return lambda item: state["mode"] == m

    # checked 每次展开菜单都会被调用一次 → 直接读注册表，所以外部改动（任务
    # 管理器「启动」页）能立刻反映在勾选上，不需要 App 自己维护一份状态
    def autostart_item():
        return pystray.MenuItem(tr("开机自启", "Launch at login"), on_autostart,
                                checked=lambda item: autostart_enabled())

    menu = pystray.Menu(
        pystray.MenuItem(tr("详情", "Details"), on_flyout, default=True),
        pystray.MenuItem(tr("立即刷新", "Refresh now"), on_refresh),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(tr("主显示 5 小时", "Primary window 5h"),
                         make_mode_setter("5h"), radio=True, checked=mode_checked("5h")),
        pystray.MenuItem(tr("主显示 7 天", "Primary window 7d"),
                         make_mode_setter("7d"), radio=True, checked=mode_checked("7d")),
        pystray.Menu.SEPARATOR,
        autostart_item(),
        pystray.MenuItem(tr("退出", "Quit"), on_quit),
    )

    # 盾牌的默认动作是直接跳转，不弹 flyout：面板给不出 WebRTC 的答案，
    # 网页才是完整检测，中间插一层面板只是多一次点击
    # 开机自启在两个菜单里都放一份（各自 new 一个 MenuItem，不共用实例）：用户
    # 常常只让其中一个图标常显、另一个折进溢出区，只放一边等于一半用户找不到
    shield_menu = pystray.Menu(
        pystray.MenuItem(tr("在网页中检测", "Check in browser"), on_ip_open, default=True),
        pystray.MenuItem(tr("立即刷新", "Refresh now"), on_ip_refresh),
        pystray.Menu.SEPARATOR,
        autostart_item(),
        pystray.MenuItem(tr("退出", "Quit"), on_quit),
    )

    for svc in ("claude", "codex"):
        icons[svc] = pystray.Icon(
            f"ai-limit-{svc}", render_icon(svc, 0),
            f"{_SERVICE_TITLES[svc]} · {tr('读取中…', 'loading…')}", menu)
    shield = pystray.Icon(
        "ai-limit-ipsec", render_shield(ipsec.SHIELD_IDLE),
        f"{_IP_TITLE()} · {tr('检测中…', 'checking…')}", shield_menu)

    threading.Thread(target=fetch_loop, daemon=True).start()
    for icon in list(icons.values()) + [shield]:
        icon.run_detached()

    # ── 主线程：tkinter loop + 队列轮询 ──
    def poll():
        try:
            while True:
                msg = ui_queue.get_nowait()
                if msg == "flyout":
                    flyout.toggle()
                elif msg == "ip-open":
                    webbrowser.open(ipsec.SITE_URL)
                elif msg == "quit":
                    for icon in list(icons.values()) + [shield]:
                        if icon is not None:
                            icon.stop()
                    root.destroy()
                    return
        except queue.Empty:
            pass
        root.after(120, poll)

    root.after(120, poll)
    # 仅测试用：无人值守环境点不了托盘，这个开关让 flyout 每 3 秒自动弹出
    # 供截屏验证——失焦自动收起的行为会让单次弹出在抓屏前消失，所以是循环弹
    # （与 macOS 版 AI_LIMIT_AUTOTEST_* 同一模式，生产不设置则无行为差异）
    # strip()：cmd 的 `set X=1 && ...` 会把 && 前的空格算进值（"1 "），
    # 不 strip 的话计划任务/批处理里设的开关会静默失效
    if os.environ.get("AI_LIMIT_AUTOTEST_FLYOUT", "").strip() == "1":
        def _autotest_loop():
            try:
                flyout.show()
                # 心跳探针：窗口到底映射没有、映射在哪、指针在哪——
                # 无人值守排障的地面真相，比隔空猜测强
                with open(_LOG_PATH, "a", encoding="utf-8") as f:
                    w = flyout.win
                    f.write(f"[{time.strftime('%T')}] hb state={w.state()} "
                            f"geo={w.winfo_geometry()} viewable={w.winfo_viewable()} "
                            f"ptr=({root.winfo_pointerx()},{root.winfo_pointery()})\n")
            except Exception:
                _log_exc("autotest-flyout")
            root.after(3000, _autotest_loop)
        root.after(3000, _autotest_loop)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
