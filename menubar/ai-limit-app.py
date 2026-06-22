#!/usr/bin/env python3
"""ai-limit 菜单栏 App（rumps 版）

独立 macOS App，不依赖 SwiftBar，有自己的图标和进程。
py2app 打包：cd menubar && python3 setup.py py2app
"""
import datetime
import json
import pathlib
import sys
import threading
import webbrowser

import rumps
import AppKit

_REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from usage import (
    __version__,
    live_claude_plan,
    live_claude_usage,
    live_codex_web_usage,
    ClaudeWebError,
    CodexWebError,
    CodexAuthError,
    TZ_LOCAL,
    epoch_to_local,
)


def _detect_system_lang() -> str:
    """GUI App 走 Cocoa 偏好语言（NSLocale），不依赖 POSIX LANG/locale——
    py2app 打包后由 Launch Services 启动，POSIX locale 环境变量通常不反映
    「系统设置 → 语言与地区」里用户的实际选择。"""
    try:
        langs = AppKit.NSLocale.preferredLanguages()
        if langs and str(langs[0]).lower().startswith("zh"):
            return "zh"
    except Exception:
        pass
    return "en"


_SYSTEM_LANG = _detect_system_lang()

# ── 常量 ─────────────────────────────────────────────────────────────────────

_STATE_PATH   = pathlib.Path.home() / ".ai-limit-menubar.json"
_CACHE_PATH   = pathlib.Path.home() / ".ai-limit-menubar-cache.json"
_CACHE_TTL    = 55
_REFRESH_SEC  = 60               # 兜底默认（= 1 分钟）
_REFRESH_MINS = (1, 2, 3, 4, 5)  # 用户可选的刷新频率（分钟）
_DISPLAY_MODES = ("5h", "7d")
_BAR_STYLES    = ("both", "number", "battery")  # 菜单栏样式：数字+电池 / 仅数字 / 仅电池
_LANGS         = ("zh", "en", "auto")
_SERVICES      = ("claude", "codex")
_MENU_MIN_WIDTH = 290
_ZH_WEEKDAYS   = "一二三四五六日"
_EN_WEEKDAYS   = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_EN_RESET_PAD  = 8
_PROJECT_URL   = "https://github.com/zhuchenxi113/ai-limit"
_AUTHOR_URL_ZH = "https://gitee.com/zhuchenxi113"
_AUTHOR_URL_EN = "https://github.com/zhuchenxi113"
_RELEASES_API_URL  = "https://api.github.com/repos/zhuchenxi113/ai-limit/releases/latest"
_RELEASES_PAGE_URL = _PROJECT_URL + "/releases"
# Gitee 国内可直连，作为 GitHub 连不上时（常见于未配代理的用户）的兜底。
# 注意：Gitee 官方 /releases/latest 接口实测有 bug，返回的不是真正最新版
# （曾返回 v0.3.10 而实际最新是 v0.3.11）；改用列表按创建时间倒序取第一条才准确。
_GITEE_RELEASES_API_URL  = "https://gitee.com/api/v5/repos/zhuchenxi113/ai-limit/releases?per_page=1&direction=desc"
_GITEE_RELEASES_PAGE_URL = "https://gitee.com/zhuchenxi113/ai-limit/releases"
_LAUNCH_AGENT_LABEL = "com.zhuchenxi.ai-limit"
_LAUNCH_AGENT_PLIST = pathlib.Path.home() / "Library/LaunchAgents" / f"{_LAUNCH_AGENT_LABEL}.plist"
_APP_EXECUTABLE     = pathlib.Path("/Applications/ai-limit.app/Contents/MacOS/ai-limit")

# ── 工具函数 ─────────────────────────────────────────────────────────────────

def _login_item_enabled():
    return _LAUNCH_AGENT_PLIST.exists()

def _set_login_item(enabled: bool):
    if enabled:
        _LAUNCH_AGENT_PLIST.parent.mkdir(parents=True, exist_ok=True)
        _LAUNCH_AGENT_PLIST.write_text(
            f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{_LAUNCH_AGENT_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{_APP_EXECUTABLE}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
</dict>
</plist>
""",
            encoding="utf-8",
        )
    else:
        try:
            _LAUNCH_AGENT_PLIST.unlink()
        except FileNotFoundError:
            pass

def _tr(lang, zh, en):
    return en if lang == "en" else zh

def _version_tuple(v: str):
    # 容忍预发布后缀（如 0.3.13-dev / 0.3.13-rc1）：每段只取前导数字，无数字记 0，
    # 避免 int("13-dev") 抛 ValueError 导致「检查更新」静默不弹窗。
    import re
    out = []
    for p in v.lstrip("v").split("."):
        m = re.match(r"\d+", p)
        out.append(int(m.group()) if m else 0)
    return tuple(out)

def _show_alert(title, message, ok, cancel=None) -> bool:
    """rumps.alert() 包的是 AppKit 已废弃的 NSAlert 便捷构造器
    （alertWithMessageText_defaultButton_alternateButton_otherButton_informativeTextWithFormat_），
    在当前 macOS 版本下静默不弹窗、直接返回——实测确认（见 lessons）。
    这里改用现代 NSAlert API（alloc/init + setMessageText_ + addButtonWithTitle_）自己拼，可正常显示。
    返回是否点了第一个按钮（ok）。"""
    alert = AppKit.NSAlert.alloc().init()
    alert.setMessageText_(title)
    alert.setInformativeText_(message)
    alert.addButtonWithTitle_(ok)
    if cancel:
        alert.addButtonWithTitle_(cancel)
    return alert.runModal() == AppKit.NSAlertFirstButtonReturn

def _fetch_latest_release_info(timeout=6) -> dict:
    """后台线程调用：查最新 Release tag。优先 GitHub；连不上（常见于未配代理
    的用户，GitHub 在国内常被墙）时退到 Gitee（国内可直连）。不抛异常，
    两边都失败才返回 {"error": True}。"""
    import urllib.request

    def _get_json(url):
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "ai-limit-menubar"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())

    try:
        data = _get_json(_RELEASES_API_URL)
        return {"latest": data["tag_name"].lstrip("v"), "source": "github"}
    except Exception:
        pass

    try:
        data = _get_json(_GITEE_RELEASES_API_URL)
        return {"latest": data[0]["tag_name"].lstrip("v"), "source": "gitee"}
    except Exception:
        return {"error": True}

def _native_bar(pct, width=4):
    filled = round(max(0, min(100, pct)) / 100 * width)
    return "▰" * filled + "▱" * (width - filled)

def _fmt_plan(plan, lang="zh"):
    if not plan or plan == "?":
        return ""
    plan = str(plan).replace("_", " ").title()
    return f" Plan: {plan}" if lang == "en" else f" 方案：{plan}"

def _fmt_reset_dt(dt, lang):
    today = datetime.datetime.now(TZ_LOCAL).date()
    target = dt.date()
    days = (target - today).days
    next_week = target.isocalendar()[:2] > today.isocalendar()[:2]
    if lang == "en":
        if days == 0:    wd = "today"
        elif days == 1:  wd = "tomorrow"
        elif days == 2:  wd = "2 days"
        elif next_week:  wd = f"next {_EN_WEEKDAYS[dt.weekday()]}"
        else:            wd = _EN_WEEKDAYS[dt.weekday()]
        return f"{dt:%H:%M}  {wd}"
    if days == 0:    wd = "今天"
    elif days == 1:  wd = "明天"
    elif days == 2:  wd = "后天"
    elif next_week:  wd = f"下周{_ZH_WEEKDAYS[dt.weekday()]}"
    else:            wd = f"周{_ZH_WEEKDAYS[dt.weekday()]}"
    if len(wd) < 3:
        wd += "　" * (3 - len(wd))
    return f"{wd} {dt:%H:%M}"

def _fmt_reset_epoch(epoch, lang="zh"):
    try:
        return _fmt_reset_dt(epoch_to_local(int(epoch)), lang)
    except Exception:
        return "?"

def _fmt_reset_iso(iso, lang="zh"):
    try:
        return _fmt_reset_dt(datetime.datetime.fromisoformat(iso).astimezone(TZ_LOCAL), lang)
    except Exception:
        return "?"

# ── 状态 / 缓存 ──────────────────────────────────────────────────────────────

def _load_state():
    # lang: "auto"（默认）= 跟随系统，每次启动按 NSLocale 实时判定；
    # "zh"/"en" = 用户在菜单里显式选过，永久优先于系统语言。
    state = {"global": "5h", "lang": "auto",
             "bar_services": list(_SERVICES),    # 菜单栏图标显示哪些（不允许全空）
             "panel_services": list(_SERVICES),  # 详情面板显示哪些（允许全空）
             "bar_style": "both",                # 菜单栏样式：both/number/battery
             "refresh_min": 1}
    try:
        raw = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            if raw.get("global") in _DISPLAY_MODES:
                state["global"] = raw["global"]
            if raw.get("lang") in _LANGS:
                state["lang"] = raw["lang"]
            # 迁移：旧版本只有单一 services 字段，同时充当菜单栏与面板
            legacy = None
            if isinstance(raw.get("services"), list):
                legacy = [s for s in raw["services"] if s in _SERVICES]
            # bar_services 不允许全空：空则忽略，回退到旧 services 或默认
            if isinstance(raw.get("bar_services"), list):
                f = [s for s in raw["bar_services"] if s in _SERVICES]
                if f:
                    state["bar_services"] = f
            elif legacy:
                state["bar_services"] = legacy
            # panel_services 允许全空（用户可只看菜单栏）
            if isinstance(raw.get("panel_services"), list):
                state["panel_services"] = [s for s in raw["panel_services"] if s in _SERVICES]
            elif legacy is not None:
                state["panel_services"] = legacy
            if raw.get("bar_style") in _BAR_STYLES:
                state["bar_style"] = raw["bar_style"]
            if raw.get("refresh_min") in _REFRESH_MINS:
                state["refresh_min"] = raw["refresh_min"]
    except Exception:
        pass
    return state

def _save_state(state):
    try:
        _STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception:
        pass

def _load_cache():
    try:
        raw = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        age = datetime.datetime.now().timestamp() - float(raw.get("cached_at", 0))
        if age <= _CACHE_TTL:
            return raw.get("claude"), raw.get("codex")
    except Exception:
        pass
    return None, None

def _save_cache(claude, codex):
    try:
        _CACHE_PATH.write_text(
            json.dumps({
                "cached_at": datetime.datetime.now().timestamp(),
                "claude": claude,
                "codex": codex,
            }, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass

# ── 数据获取 ─────────────────────────────────────────────────────────────────

def _fetch_claude(lang):
    import socket, urllib.error
    try:
        data = live_claude_usage()
        five_h = data.get("five_hour") or {}
        seven_d = data.get("seven_day") or {}
        try:
            plan = live_claude_plan()
        except Exception:
            plan = None
        return {
            "5h_left":  int(round(100 - float(five_h.get("utilization", 0)))),
            "7d_left":  int(round(100 - float(seven_d.get("utilization", 0)))),
            "5h_reset": five_h.get("resets_at"),
            "7d_reset": seven_d.get("resets_at"),
            "plan":     plan,
        }
    except ClaudeWebError as e:
        kind = getattr(e, "kind", "generic")
        if kind == "cloudflare":
            msg = _tr(lang, "被拦截，打开用量页勿关", "Blocked, open Claude usage, keep open")
        elif kind == "auth":
            msg = _tr(lang, "需在浏览器重新登录 claude.ai", "Re-login at claude.ai in browser")
        else:
            msg = str(e)
            if "JSON" in msg or "DOCTYPE" in msg or "html" in msg.lower():
                msg = _tr(lang, "网络不可用或需重新登录 claude.ai", "Network error or re-login at claude.ai required")
        return {"error": msg}
    except (socket.timeout, TimeoutError):
        return {"error": _tr(lang, "网络超时，请稍后重试", "Network timeout, please retry later")}
    except urllib.error.URLError:
        return {"error": _tr(lang, "网络不可用", "Network unavailable")}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}

def _fetch_codex(lang):
    import socket, urllib.error
    try:
        _ts, rl = live_codex_web_usage()
        primary   = rl.get("primary") or {}
        secondary = rl.get("secondary") or {}
        return {
            "5h_left":  int(round(100 - primary.get("used_percent", 0))),
            "7d_left":  int(round(100 - secondary.get("used_percent", 0))),
            "5h_reset": primary.get("resets_at"),
            "7d_reset": secondary.get("resets_at"),
            "plan":     rl.get("plan_type") or "?",
        }
    except CodexAuthError:
        return {"error": _tr(lang,
            "无 Codex 权限（可能未订阅或需重新登录）",
            "No Codex access (subscription required or re-login needed)")}
    except CodexWebError as e:
        msg = str(e)
        if "timed out" in msg or "urlopen" in msg:
            msg = _tr(lang, "网络超时，请稍后重试", "Network timeout, please retry later")
        return {"error": msg}
    except (socket.timeout, TimeoutError):
        return {"error": _tr(lang, "网络超时，请稍后重试", "Network timeout, please retry later")}
    except urllib.error.URLError:
        return {"error": _tr(lang, "网络不可用", "Network unavailable")}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}

# ── AppKit 辅助 ───────────────────────────────────────────────────────────────

def _status_button(app):
    """返回 NSStatusItem.button()；rumps 在不同版本里把它存在不同属性下。"""
    # 已知 rumps 0.4 在 _nsapp.nsstatusitem，但版本间不一致；做一次探测
    candidates = ("_status_item", "_status_bar_item", "_nsstatusitem")
    for attr in candidates:
        item = getattr(app, attr, None)
        if item and hasattr(item, "button"):
            return item.button()
    # rumps 0.4.x 路径：app._nsapp.nsstatusitem
    nsapp = getattr(app, "_nsapp", None)
    if nsapp is not None:
        item = getattr(nsapp, "nsstatusitem", None)
        if item and hasattr(item, "button"):
            return item.button()
    # 兜底：扫一遍 app 所有属性，找一个 .button() 看起来对的
    for name in dir(app):
        if name.startswith("__"):
            continue
        try:
            item = getattr(app, name)
        except Exception:
            continue
        if item is not None and hasattr(item, "button") and callable(getattr(item, "button", None)):
            try:
                btn = item.button()
                if hasattr(btn, "setTitle_") and hasattr(btn, "setImage_"):
                    return btn
            except Exception:
                continue
    return None


def _set_bar_title(app, text):
    """纯文字标题（用作 SF Symbol 不可用时的兜底）。"""
    btn = _status_button(app)
    if btn is not None:
        btn.setImage_(None)
        btn.setAttributedTitle_(AppKit.NSAttributedString.alloc().initWithString_(""))
        btn.setTitle_(text)
        btn.setImagePosition_(0)  # NSNoImage
        return
    app.title = text


def _sf_battery_image(pct, point_size=14):
    """返回对应百分比的 SF Symbol 电池 NSImage（5 档量化）。

    粒度：0(<13) / 25 / 50 / 75 / 100(≥88)。
    不在这里上色——会作为 template 一起整合进 composite，由 AppKit 在状态
    栏上下文里和系统 Wi-Fi、电池等一起决定实际颜色（vibrancy/明暗自适应）。
    """
    if pct >= 88:
        name = "battery.100"
    elif pct >= 63:
        name = "battery.75"
    elif pct >= 38:
        name = "battery.50"
    elif pct >= 13:
        name = "battery.25"
    else:
        name = "battery.0"
    img = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
    if img is None:
        return None
    cfg = AppKit.NSImageSymbolConfiguration.configurationWithPointSize_weight_(
        point_size, AppKit.NSFontWeightMedium
    )
    return img.imageWithSymbolConfiguration_(cfg)


def _battery_attachment(pct, font):
    """SF Symbol 电池包成 NSTextAttachment，可塞进 NSAttributedString 里跟文字一行排。

    image 设 template，菜单栏会把它当系统图标处理（vibrancy + 亮暗自适应），
    跟 Wi-Fi / 系统电池图标在同一渲染通道。
    """
    bat = _sf_battery_image(pct)
    if bat is None:
        return None
    bat.setTemplate_(True)
    attach = AppKit.NSTextAttachment.alloc().init()
    attach.setImage_(bat)
    sz = bat.size()
    # 垂直微调：让电池中线大致对齐文字中线
    y_offset = (font.capHeight() - sz.height) / 2
    attach.setBounds_(AppKit.NSMakeRect(0, y_offset, sz.width, sz.height))
    return AppKit.NSAttributedString.attributedStringWithAttachment_(attach)


def _render_attributed_title(items, style="both"):
    """构建状态栏 attributed title：文字交给 NSStatusBarButton 原生渲染（拿到
    系统 vibrancy 和亮暗自适应），电池作为内联 template image 附件。

    旧方案是把整条画成位图（NSImage.lockFocus + labelColor），但 bitmap 里
    的文字是一次性栅格化的灰度，拿不到状态栏文字的 vibrancy，视觉上比系统
    时钟、菜单文字偏暗。
    """
    font = AppKit.NSFont.menuBarFontOfSize_(0)
    text_attrs = {AppKit.NSFontAttributeName: font}
    mas = AppKit.NSMutableAttributedString.alloc().init()

    def append_text(s):
        mas.appendAttributedString_(
            AppKit.NSAttributedString.alloc().initWithString_attributes_(s, text_attrs)
        )

    for i, (label, pct, err) in enumerate(items):
        prefix = "  " if i > 0 else ""
        if err:
            append_text(f"{prefix}{label} ⚠️")
            continue
        if style == "number":
            append_text(f"{prefix}{label} {pct}%")
        elif style == "battery":
            append_text(f"{prefix}{label} ")
            bat_attach = _battery_attachment(pct, font)
            if bat_attach is not None:
                mas.appendAttributedString_(bat_attach)
        else:  # both：数字 + 电池
            append_text(f"{prefix}{label} {pct}% ")
            bat_attach = _battery_attachment(pct, font)
            if bat_attach is not None:
                mas.appendAttributedString_(bat_attach)

    if mas.length() == 0:
        append_text("ai-limit ⚠️")
    return mas


def _set_bar_with_batteries(app, items, style="both"):
    """把 attributed title（文字 + 电池附件）安到状态栏按钮上。"""
    btn = _status_button(app)
    if btn is None:
        raise RuntimeError("no status button")
    btn.setImage_(None)
    btn.setTitle_("")
    btn.setAttributedTitle_(_render_attributed_title(items, style))

def _noop(_):
    """无副作用 callback，仅用于让 macOS 把无动作菜单项也按常规文字色渲染。
    AppKit 会把 NSMenuItem.target=nil 的项自动灰化，setEnabled_(True) 也救不了；
    挂一个真实 callback（哪怕什么都不做）才会让 macOS 视为正常项。"""
    pass


def _disable(menu_item):
    """让菜单项显式灰色（仅用于'上次刷新'这种刻意的次要信息）。"""
    menu_item._menuitem.setEnabled_(False)
    return menu_item


def _inert(menu_item):
    """挂 no-op callback，让 macOS 按常规文字色渲染（不灰），点击无效果。"""
    menu_item.set_callback(_noop)
    return menu_item

def _detail_text(mode, pct, reset, lang):
    if lang == "en":
        return f"  {mode}\t{pct:>3}% left   \t↻ {reset}"
    return f"  {mode}\t{pct:>3}% 剩余\t↻ {reset}"

# ── 主 App ────────────────────────────────────────────────────────────────────

class AiLimitApp(rumps.App):
    def __init__(self):
        super().__init__("…", quit_button=None)
        self._state = _load_state()
        self._claude = None
        self._codex  = None
        # 后台线程把抓取结果放这里，由主线程的 _apply_pending 定时器接力
        self._pending = None
        self._pending_lock = threading.Lock()
        # 检查更新：同样模式，后台线程查完放这里，_apply_pending 接力弹窗
        self._update_checking = False
        self._update_pending = None
        self._update_lock = threading.Lock()
        self._last_refresh_str = "…"   # 折进刷新频率子菜单标题，无单独菜单行
        self._build_menu()
        # 自动刷新定时器手动管理（间隔可在运行时按用户选择的频率重建），
        # 不用 @rumps.timer 装饰器——那是静态绑定，改不了间隔。
        self._auto_timer = rumps.Timer(self._auto_refresh, self._refresh_sec())
        self._auto_timer.start()

    def _refresh_sec(self):
        return self._state.get("refresh_min", 1) * 60

    def _lang(self):
        """当前生效语言：菜单里选了"中文"/"English"就用该选择（持久化覆盖），
        选"跟随系统"（或旧状态文件没有该字段）则每次启动按 NSLocale 实时判定——
        不把检测结果写回 state，避免被其他偏好的保存操作连带固化成"伪用户选择"。"""
        choice = self._state["lang"]
        return choice if choice in ("zh", "en") else _SYSTEM_LANG

    # ── 菜单构建 ──────────────────────────────────────────────────────────────

    def _build_menu(self):
        lang = self._lang()

        # Claude 区块（段头 + 详情都挂 no-op callback 避免 macOS 自动灰化）
        self._claude_header = _inert(rumps.MenuItem("Claude Code"))
        self._claude_5h     = _inert(rumps.MenuItem("  5h  …"))
        self._claude_7d     = _inert(rumps.MenuItem("  7d  …"))

        # CodeX 区块
        self._codex_header = _inert(rumps.MenuItem("CodeX"))
        self._codex_5h     = _inert(rumps.MenuItem("  5h  …"))
        self._codex_7d     = _inert(rumps.MenuItem("  7d  …"))

        # 刷新频率子菜单（1–5 分钟单选）。标题同时携带"上次刷新"时间，
        # 二者合一行——NSMenu 每个 item 自占一行，无法两项共行。
        self._rate_items = {}
        self._rate_menu = rumps.MenuItem("")
        for m in _REFRESH_MINS:
            it = rumps.MenuItem("", callback=self._make_set_rate(m))
            self._rate_items[m] = it
            self._rate_menu.add(it)

        # 菜单栏显示子菜单
        self._mode_5h = rumps.MenuItem("5 小时" if lang == "zh" else "5 hours",
                                       callback=self._set_mode_5h)
        self._mode_7d = rumps.MenuItem("7 天" if lang == "zh" else "7 days",
                                       callback=self._set_mode_7d)
        mode_label = "菜单栏显示" if lang == "zh" else "Menu bar display"
        self._mode_menu = rumps.MenuItem(mode_label)
        self._mode_menu.add(self._mode_5h)
        self._mode_menu.add(self._mode_7d)

        # 语言子菜单
        self._lang_auto = rumps.MenuItem(_tr(lang, "跟随系统", "Follow System"), callback=self._set_lang_auto)
        self._lang_zh   = rumps.MenuItem("中文", callback=self._set_lang_zh)
        self._lang_en   = rumps.MenuItem("English", callback=self._set_lang_en)
        lang_label = "语言" if lang == "zh" else "Language"
        self._lang_menu = rumps.MenuItem(lang_label)
        self._lang_menu.add(self._lang_auto)
        self._lang_menu.add(self._lang_zh)
        self._lang_menu.add(self._lang_en)

        # 菜单栏图标子菜单：显示哪些服务 + 样式（数字+电池 / 仅数字 / 仅电池）
        self._bar_claude = rumps.MenuItem("Claude Code", callback=self._toggle_bar_claude)
        self._bar_codex  = rumps.MenuItem("CodeX",       callback=self._toggle_bar_codex)
        self._bar_style_both = rumps.MenuItem("", callback=self._make_set_bar_style("both"))
        self._bar_style_num  = rumps.MenuItem("", callback=self._make_set_bar_style("number"))
        self._bar_style_bat  = rumps.MenuItem("", callback=self._make_set_bar_style("battery"))
        self._bar_menu = rumps.MenuItem("")
        self._bar_menu.add(self._bar_claude)
        self._bar_menu.add(self._bar_codex)
        self._bar_menu.add(None)
        self._bar_menu.add(self._bar_style_both)
        self._bar_menu.add(self._bar_style_num)
        self._bar_menu.add(self._bar_style_bat)

        # 详情面板子菜单：显示哪些服务（允许全空，只看菜单栏）
        self._panel_claude = rumps.MenuItem("Claude Code", callback=self._toggle_panel_claude)
        self._panel_codex  = rumps.MenuItem("CodeX",       callback=self._toggle_panel_codex)
        self._panel_menu = rumps.MenuItem("")
        self._panel_menu.add(self._panel_claude)
        self._panel_menu.add(self._panel_codex)

        # 开机自启
        self._login_item = rumps.MenuItem(
            "开机自启" if lang == "zh" else "Launch at Login",
            callback=self._toggle_login_item,
        )
        self._update_login_item_check()

        # 操作项
        self._refresh_item = rumps.MenuItem(
            "立即刷新" if lang == "zh" else "Refresh now",
            callback=self._force_refresh,
        )
        self._codex_dash = rumps.MenuItem(
            "打开 CodeX 分析页" if lang == "zh" else "Open CodeX analytics",
            callback=lambda _: webbrowser.open("https://chatgpt.com/codex/cloud/settings/analytics"),
        )
        self._claude_dash = rumps.MenuItem(
            "打开 Claude 用量页" if lang == "zh" else "Open Claude usage",
            callback=lambda _: webbrowser.open("https://claude.ai/settings/usage"),
        )

        # 关于子菜单
        about_label = f"关于（ai-limit {__version__}）" if lang == "zh" else f"About (ai-limit {__version__})"
        self._about_menu   = rumps.MenuItem(about_label)
        self._about_ver    = rumps.MenuItem(f"ai-limit {__version__}",
                                            callback=lambda _: webbrowser.open(_PROJECT_URL))
        self._about_author = rumps.MenuItem(
            "作者：zhuchenxi" if lang == "zh" else "Author: zhuchenxi",
            callback=lambda _: webbrowser.open(_AUTHOR_URL_ZH if self._lang() == "zh" else _AUTHOR_URL_EN),
        )
        self._about_desc   = _disable(rumps.MenuItem(
            "Claude Code / CodeX 额度监控" if lang == "zh" else "Claude Code / CodeX quota monitor"
        ))
        self._about_src    = _disable(rumps.MenuItem(
            "数据来源：本地日志 + 官方网页接口" if lang == "zh" else "Source: local logs + official web endpoints"
        ))
        self._check_update_item = rumps.MenuItem(
            "检查更新" if lang == "zh" else "Check for Updates",
            callback=self._check_for_updates,
        )
        self._about_menu.add(self._about_ver)
        self._about_menu.add(self._about_author)
        self._about_menu.add(self._check_update_item)

        # Star on GitHub（放在关于子菜单里，_about_menu 之后才 add）
        self._star_item = rumps.MenuItem(
            "⭐ 给个 Star，鼓励作者" if lang == "zh" else "⭐ Star on GitHub — support the author",
            callback=lambda _: webbrowser.open(_PROJECT_URL),
        )
        self._about_menu.add(self._star_item)
        self._about_menu.add(self._about_desc)
        self._about_menu.add(self._about_src)

        # 退出
        self._quit_item = rumps.MenuItem(
            "退出" if lang == "zh" else "Quit",
            callback=rumps.quit_application,
        )

        self.menu = [
            self._claude_header,
            self._claude_5h,
            self._claude_7d,
            None,
            self._codex_header,
            self._codex_5h,
            self._codex_7d,
            None,
            self._rate_menu,
            self._refresh_item,
            None,
            self._mode_menu,
            self._bar_menu,
            self._panel_menu,
            self._lang_menu,
            self._login_item,
            None,
            self._codex_dash,
            self._claude_dash,
            None,
            self._about_menu,
            None,
            self._quit_item,
        ]
        # NSMenu otherwise shrinks to the longest localized label, so the
        # Chinese and English panels visibly jump between different widths.
        self.menu._menu.setMinimumWidth_(_MENU_MIN_WIDTH)
        self._update_mode_checks()
        self._update_lang_checks()
        self._update_bar_checks()
        self._update_panel_checks()
        self._update_rate_checks()

    # ── 数据更新 ──────────────────────────────────────────────────────────────
    #
    # 原则：网络抓取一律在后台线程跑，绝对不阻塞主 UI 线程，否则切换菜单时
    # macOS 会显示转圈光标。
    # 流程：
    #   主线程触发    → 立即用 _load_cache() 重画一次（瞬时响应）
    #                → 启动后台线程 _async_refresh()
    #   后台线程     → 调 _fetch_claude / _fetch_codex（耗时几秒）
    #                → 把结果塞进 self._pending（加锁）
    #   主线程定时器 → _apply_pending 每 0.4s 检查 _pending，有就 apply + 重画

    @rumps.timer(0.3)
    def _init_render(self, sender):
        """启动后立即用缓存重画 + 后台拉一次最新数据。"""
        self._refresh_from_cache()
        self._kick_background_fetch()
        sender.stop()

    def _auto_refresh(self, _):
        """按用户选择的频率后台拉一次（由 self._auto_timer 驱动，间隔可调）。"""
        self._kick_background_fetch()

    @rumps.timer(0.4)
    def _apply_pending(self, _):
        """主线程接力点：把后台线程取到的数据 apply 到 UI。

        重点：服务被禁用时不要清空内存里的旧数据。后台线程对禁用服务返回
        None 表示"没拉新的"，不是"清空"——保留上次的值，重新启用时菜单栏
        瞬间显示该服务的最近一次缓存，避免 1-2s 网络抓取的等待感。
        """
        with self._pending_lock:
            pending = self._pending
            self._pending = None
        if pending is not None:
            claude, codex = pending
            if claude is not None:
                self._claude = claude
            if codex is not None:
                self._codex = codex
            _save_cache(self._claude, self._codex)
            self._render()

        with self._update_lock:
            update_result = self._update_pending
            self._update_pending = None
        if update_result is not None:
            self._update_checking = False
            self._check_update_item.title = _tr(self._lang(), "检查更新", "Check for Updates")
            self._show_update_result(update_result)

    def _refresh_from_cache(self):
        """主线程瞬时操作：读短缓存重画，不碰网络。"""
        claude, codex = _load_cache()
        # 不按 services 过滤——内存里保留两份数据，UI 显示由 _render 控
        if claude is not None:
            self._claude = claude
        if codex is not None:
            self._codex = codex
        self._render()

    def _kick_background_fetch(self):
        """启动后台线程抓数据；线程内不要碰任何 UI 对象。"""
        t = threading.Thread(target=self._async_refresh, daemon=True)
        t.start()

    def _async_refresh(self):
        """后台线程：抓数据 → 写共享变量。不能调任何 rumps/AppKit UI。"""
        lang = self._lang()
        need = set(self._state.get("bar_services") or []) | set(self._state.get("panel_services") or [])
        claude = _fetch_claude(lang) if "claude" in need else None
        codex  = _fetch_codex(lang)  if "codex"  in need else None
        with self._pending_lock:
            self._pending = (claude, codex)

    def _render(self):
        lang     = self._lang()
        mode     = self._state["global"]
        bar_svc   = self._state.get("bar_services") or list(_SERVICES)
        panel_svc = self._state.get("panel_services") or []
        style     = self._state.get("bar_style", "both")
        show_claude = "claude" in panel_svc   # 面板维度（下方区块沿用此变量）
        show_codex  = "codex"  in panel_svc
        claude = self._claude or {}
        codex  = self._codex  or {}

        # 菜单栏标题：[Claude 68% ⌬]  [CodeX 99% ⌬]
        # 电池是原生 SF Symbol，Apple 亲手画的 iPhone 风格，向量永不糊
        bar_items = []
        if "claude" in bar_svc:
            if "error" in claude:
                bar_items.append(("Claude", 0, True))
            elif claude:
                pct = claude["5h_left"] if mode == "5h" else claude["7d_left"]
                bar_items.append(("Claude", pct, False))
        if "codex" in bar_svc:
            if "error" in codex:
                bar_items.append(("CodeX", 0, True))
            elif codex:
                pct = codex["5h_left"] if mode == "5h" else codex["7d_left"]
                bar_items.append(("CodeX", pct, False))
        try:
            _set_bar_with_batteries(self, bar_items, style)
        except Exception:
            # SF Symbol 不可用时（很老的 macOS）回退到 ▰▱ 文字版
            parts = []
            for lbl, pct, err in bar_items:
                if err:
                    parts.append(f"{lbl} ⚠️")
                elif style == "number":
                    parts.append(f"{lbl} {pct}%")
                elif style == "battery":
                    parts.append(f"{lbl} {_native_bar(pct)}")
                else:
                    parts.append(f"{lbl} {pct}% {_native_bar(pct)}")
            _set_bar_title(self, "  ".join(parts) if parts else "ai-limit ⚠️")

        # Claude 区块 —— 服务被关时整段隐藏
        self._claude_header._menuitem.setHidden_(not show_claude)
        self._claude_5h._menuitem.setHidden_(not show_claude)
        self._claude_7d._menuitem.setHidden_(not show_claude)
        if show_claude:
            if "error" in claude:
                self._claude_header.title = "Claude Code ⚠️"
                self._claude_5h.title = f"  {claude['error'][:60]}"
                self._claude_7d._menuitem.setHidden_(True)
            elif claude:
                plan = _fmt_plan(claude.get("plan"), lang)
                self._claude_header.title = f"Claude Code{plan}"
                c5_reset = _fmt_reset_iso(claude["5h_reset"], lang)
                c7_reset = _fmt_reset_iso(claude["7d_reset"], lang)
                self._claude_5h.title = _detail_text("5h", claude["5h_left"], c5_reset, lang)
                self._claude_7d.title = _detail_text("7d", claude["7d_left"], c7_reset, lang)

        # CodeX 区块
        self._codex_header._menuitem.setHidden_(not show_codex)
        self._codex_5h._menuitem.setHidden_(not show_codex)
        self._codex_7d._menuitem.setHidden_(not show_codex)
        if show_codex:
            if "error" in codex:
                self._codex_header.title = "CodeX ⚠️"
                self._codex_5h.title = f"  {codex['error'][:60]}"
                self._codex_7d._menuitem.setHidden_(True)
            elif codex:
                plan = _fmt_plan(codex.get("plan"), lang)
                self._codex_header.title = f"CodeX{plan}"
                x5_reset = _fmt_reset_epoch(codex["5h_reset"], lang)
                x7_reset = _fmt_reset_epoch(codex["7d_reset"], lang)
                self._codex_5h.title = _detail_text("5h", codex["5h_left"], x5_reset, lang)
                self._codex_7d.title = _detail_text("7d", codex["7d_left"], x7_reset, lang)

        # 刷新时间：折进刷新频率子菜单标题（见 _update_rate_checks）
        self._last_refresh_str = datetime.datetime.now(TZ_LOCAL).strftime("%H:%M:%S")
        self._update_rate_checks()

    # ── 模式 / 语言切换 ──────────────────────────────────────────────────────

    def _set_mode_5h(self, _):
        self._state["global"] = "5h"
        _save_state(self._state)
        self._update_mode_checks()
        self._render()  # 只换显示窗口，数据没变，直接重画

    def _set_mode_7d(self, _):
        self._state["global"] = "7d"
        _save_state(self._state)
        self._update_mode_checks()
        self._render()

    def _update_mode_checks(self):
        lang = self._lang()
        mode = self._state["global"]
        self._mode_5h.title = ("✓ " if mode == "5h" else "  ") + _tr(lang, "5 小时", "5 hours")
        self._mode_7d.title = ("✓ " if mode == "7d" else "  ") + _tr(lang, "7 天", "7 days")
        self._mode_menu.title = _tr(lang,
            f"菜单栏显示（{_tr(lang, '5 小时', '5 hours') if mode == '5h' else _tr(lang, '7 天', '7 days')}）",
            f"Menu bar display ({_tr(lang, '5 hours', '5 hours') if mode == '5h' else '7 days'})",
        )

    # ── 刷新频率 ────────────────────────────────────────────────────────────
    def _make_set_rate(self, minutes):
        return lambda _: self._set_refresh_rate(minutes)

    def _set_refresh_rate(self, minutes):
        if minutes not in _REFRESH_MINS or minutes == self._state.get("refresh_min"):
            self._update_rate_checks()  # 重画勾选，即使没变也保持一致
            return
        self._state["refresh_min"] = minutes
        _save_state(self._state)
        # 重建定时器间隔：stop → 改 interval → start（rumps 在 start 时才读取 interval）
        self._auto_timer.stop()
        self._auto_timer.interval = self._refresh_sec()
        self._auto_timer.start()
        self._update_rate_checks()

    def _update_rate_checks(self):
        lang = self._lang()
        cur = self._state.get("refresh_min", 1)
        for m, it in self._rate_items.items():
            it.title = ("✓ " if m == cur else "  ") + _tr(lang, f"{m} 分钟", f"{m} min")
        ts = self._last_refresh_str
        self._rate_menu.title = _tr(lang,
            f"刷新频率（{cur} 分钟）   上次: {ts}",
            f"Refresh interval ({cur} min)   last: {ts}")

    def _set_lang_auto(self, _):
        self._state["lang"] = "auto"
        _save_state(self._state)
        self._update_lang_checks()
        self._update_mode_checks()
        self._update_bar_checks()
        self._update_panel_checks()
        self._refresh_static_labels()
        self._render()

    def _set_lang_zh(self, _):
        self._state["lang"] = "zh"
        _save_state(self._state)
        self._update_lang_checks()
        # 重画所有 i18n 文本（详情行 / 段头 / "上次刷新" 等）
        self._update_mode_checks()
        self._update_bar_checks()
        self._update_panel_checks()
        self._refresh_static_labels()
        self._render()

    def _set_lang_en(self, _):
        self._state["lang"] = "en"
        _save_state(self._state)
        self._update_lang_checks()
        self._update_mode_checks()
        self._update_bar_checks()
        self._update_panel_checks()
        self._refresh_static_labels()
        self._render()

    def _refresh_static_labels(self):
        """语言切换后，更新所有不依赖数据的菜单文字。"""
        lang = self._lang()
        self._refresh_item.title = _tr(lang, "立即刷新", "Refresh now")
        self._codex_dash.title  = _tr(lang, "打开 CodeX 分析页", "Open CodeX analytics")
        self._claude_dash.title = _tr(lang, "打开 Claude 用量页", "Open Claude usage")
        self._about_menu.title  = _tr(lang,
            f"关于（ai-limit {__version__}）",
            f"About (ai-limit {__version__})",
        )
        self._about_author.title = _tr(lang, "作者：zhuchenxi", "Author: zhuchenxi")
        self._about_desc.title   = _tr(lang,
            "Claude Code / CodeX 额度监控",
            "Claude Code / CodeX quota monitor",
        )
        self._about_src.title    = _tr(lang,
            "数据来源：本地日志 + 官方网页接口",
            "Source: local logs + official web endpoints",
        )
        if not self._update_checking:
            self._check_update_item.title = _tr(lang, "检查更新", "Check for Updates")
        self._update_login_item_check()
        self._update_rate_checks()
        self._star_item.title    = _tr(lang, "⭐ 给个 Star，鼓励作者", "⭐ Star on GitHub — support the author")
        self._quit_item.title    = _tr(lang, "退出", "Quit")

    def _update_lang_checks(self):
        choice = self._state["lang"]
        lang = self._lang()
        self._lang_auto.title = ("✓ " if choice == "auto" else "  ") + _tr(lang, "跟随系统", "Follow System")
        self._lang_zh.title   = ("✓ " if choice == "zh"   else "  ") + "中文"
        self._lang_en.title   = ("✓ " if choice == "en"   else "  ") + "English"
        sel_zh = {"zh": "中文", "en": "English"}.get(choice, "跟随系统")
        sel_en = {"zh": "中文", "en": "English"}.get(choice, "Follow System")
        self._lang_menu.title = _tr(lang, f"语言（{sel_zh}）", f"Language ({sel_en})")

    # ── 菜单栏图标 / 详情面板 切换 ──────────────────────────────────────────

    def _toggle_bar_claude(self, _):
        self._toggle_bar("claude")

    def _toggle_bar_codex(self, _):
        self._toggle_bar("codex")

    def _toggle_bar(self, service):
        svc = list(self._state.get("bar_services") or list(_SERVICES))
        if service in svc:
            svc.remove(service)
        else:
            svc.append(service)
        if not svc:
            # 菜单栏不允许全空（否则只剩空白点击区，找不到入口），回退保留
            svc = [service]
        self._state["bar_services"] = svc
        _save_state(self._state)
        self._update_bar_checks()
        self._render()
        self._kick_background_fetch()

    def _toggle_panel_claude(self, _):
        self._toggle_panel("claude")

    def _toggle_panel_codex(self, _):
        self._toggle_panel("codex")

    def _toggle_panel(self, service):
        svc = list(self._state.get("panel_services") or [])
        if service in svc:
            svc.remove(service)
        else:
            svc.append(service)
        # 面板允许全空（用户可只看菜单栏）
        self._state["panel_services"] = svc
        _save_state(self._state)
        self._update_panel_checks()
        self._render()
        self._kick_background_fetch()

    def _make_set_bar_style(self, style):
        return lambda _: self._set_bar_style(style)

    def _set_bar_style(self, style):
        if style not in _BAR_STYLES:
            return
        self._state["bar_style"] = style
        _save_state(self._state)
        self._update_bar_checks()
        self._render()

    def _toggle_login_item(self, _):
        _set_login_item(not _login_item_enabled())
        self._update_login_item_check()

    def _update_login_item_check(self):
        lang = self._lang()
        enabled = _login_item_enabled()
        suffix = " ✓" if enabled else ""
        self._login_item.title = _tr(lang, "开机自启", "Launch at Login") + suffix

    def _update_bar_checks(self):
        lang = self._lang()
        bar = self._state.get("bar_services") or list(_SERVICES)
        self._bar_claude.title = ("✓ " if "claude" in bar else "  ") + "Claude Code"
        self._bar_codex.title  = ("✓ " if "codex"  in bar else "  ") + "CodeX"
        style = self._state.get("bar_style", "both")
        self._bar_style_both.title = ("✓ " if style == "both"    else "  ") + _tr(lang, "数字 + 电池", "Number + battery")
        self._bar_style_num.title  = ("✓ " if style == "number"  else "  ") + _tr(lang, "仅数字", "Number only")
        self._bar_style_bat.title  = ("✓ " if style == "battery" else "  ") + _tr(lang, "仅电池", "Battery only")
        summary = _tr(lang, "全部", "All") if len(bar) == 2 else (
            "Claude Code" if "claude" in bar else "CodeX"
        )
        self._bar_menu.title = _tr(lang, f"菜单栏图标（{summary}）", f"Menu bar icons ({summary})")

    def _update_panel_checks(self):
        lang = self._lang()
        panel = self._state.get("panel_services") or []
        self._panel_claude.title = ("✓ " if "claude" in panel else "  ") + "Claude Code"
        self._panel_codex.title  = ("✓ " if "codex"  in panel else "  ") + "CodeX"
        if not panel:
            summary = _tr(lang, "无", "None")
        elif len(panel) == 2:
            summary = _tr(lang, "全部", "All")
        else:
            summary = "Claude Code" if "claude" in panel else "CodeX"
        self._panel_menu.title = _tr(lang, f"详情面板（{summary}）", f"Detail panel ({summary})")

    # ── 立即刷新 ──────────────────────────────────────────────────────────────

    def _force_refresh(self, _):
        try:
            _CACHE_PATH.unlink()
        except Exception:
            pass
        # 后台拉，不卡 UI；新数据 ≤几秒内通过 _apply_pending 落到菜单上
        self._kick_background_fetch()

    # ── 检查更新 ──────────────────────────────────────────────────────────────

    def _check_for_updates(self, _):
        if self._update_checking:
            return
        self._update_checking = True
        self._check_update_item.title = _tr(self._lang(), "检查更新…", "Checking for updates…")
        threading.Thread(target=self._async_check_update, daemon=True).start()

    def _async_check_update(self):
        """后台线程：查 GitHub 最新 Release。不能调任何 rumps/AppKit UI。"""
        result = _fetch_latest_release_info()
        with self._update_lock:
            self._update_pending = result

    def _show_update_result(self, result):
        lang = self._lang()
        if result.get("error"):
            _show_alert(
                _tr(lang, "检查更新失败", "Update Check Failed"),
                _tr(lang,
                    "无法连接 GitHub，请检查网络后重试。",
                    "Could not reach GitHub. Check your network and try again.",
                ),
                ok=_tr(lang, "好", "OK"),
            )
            return
        latest = result["latest"]
        if _version_tuple(latest) <= _version_tuple(__version__):
            _show_alert(
                _tr(lang, "已是最新版本", "You're Up to Date"),
                _tr(lang,
                    f"当前版本 {__version__} 已是最新。",
                    f"Current version {__version__} is the latest.",
                ),
                ok=_tr(lang, "好", "OK"),
            )
            return
        opened = _show_alert(
            _tr(lang, "发现新版本", "Update Available"),
            _tr(lang,
                f"最新版本 {latest}，当前版本 {__version__}。是否打开下载页？",
                f"Latest version {latest}, current version {__version__}. Open the download page?",
            ),
            ok=_tr(lang, "打开下载页", "Open Download Page"),
            cancel=_tr(lang, "取消", "Cancel"),
        )
        if opened:
            page_url = _GITEE_RELEASES_PAGE_URL if result.get("source") == "gitee" else _RELEASES_PAGE_URL
            webbrowser.open(page_url)


if __name__ == "__main__":
    AiLimitApp().run()
