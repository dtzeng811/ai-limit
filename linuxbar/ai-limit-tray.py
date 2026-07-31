#!/usr/bin/env python3
"""ai-limit Linux 托盘版（Ubuntu GNOME / AyatanaAppIndicator3）

两个托盘图标：
  1. Claude 环形进度（橙 #D97757）：环的填充 = 剩余额度，旁边文字显示百分比；
     剩余 <20% 黄 ⚠、<10% 红 ‼（GNOME 托盘 label 不能改色，用符号代替颜色）
  2. IP 安全盾牌（数据层 ipsec.py）：绿勾 / 黄横线 / 红感叹号 / 灰圆点

数据层直接复用仓库根的 usage.py 与 ipsec.py，行为与 mac/win 版对齐：
默认 3 分钟刷新 + 0–20s 随机抖动；单次失败沿用旧数据，连败 3 次（或数据
老于 15 分钟）才报 ⚠；连败后指数退避（上限 30 分钟）。IP 检测 10 分钟一轮。
"""
import pathlib
import random
import sys
import threading
import time
import webbrowser

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
import cairo  # noqa: E402
from gi.repository import AyatanaAppIndicator3 as AppIndicator  # noqa: E402
from gi.repository import GLib, Gtk  # noqa: E402

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import ipsec  # noqa: E402
from usage import live_claude_usage, ts_to_local  # noqa: E402

# ── 常量（与 menubar / winbar 版对齐） ──────────────────────────────────────
_REFRESH_SEC     = 3 * 60
_JITTER_MAX_SEC  = 20
_FAIL_GRACE_N    = 3
_STALE_MAX_SEC   = 15 * 60
_BACKOFF_MAX_SEC = 30 * 60
_IPSEC_SEC       = 10 * 60

_CLAUDE_COLOR = (0xD9 / 255, 0x77 / 255, 0x57 / 255)
_SHIELD_COLORS = {
    ipsec.SHIELD_OK:   (0x3F / 255, 0xB9 / 255, 0x50 / 255),
    ipsec.SHIELD_WARN: (0xF5 / 255, 0xC5 / 255, 0x18 / 255),
    ipsec.SHIELD_CRIT: (0xE0 / 255, 0x43 / 255, 0x43 / 255),
    ipsec.SHIELD_IDLE: (0x8A / 255, 0x8A / 255, 0x8A / 255),
}

_ICON_DIR = pathlib.Path.home() / ".cache" / "ai-limit" / "icons"
_ICON_PX  = 64
_RING_LW  = 9.0
_SHIELD_LW = 5.0

# ── 开机自启（对齐 winbar 版：菜单开关 + 路径漂移自愈） ─────────────────────
_AUTOSTART = pathlib.Path.home() / ".config" / "autostart" / "ai-limit-tray.desktop"


def _autostart_body() -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=AI Limit Tray\n"
        "Comment=Claude 额度 + IP 安全度托盘监控\n"
        f"Exec={sys.executable} {pathlib.Path(__file__).resolve()}\n"
        "X-GNOME-Autostart-enabled=true\n"
        "X-GNOME-Autostart-Delay=10\n"
    )


def autostart_enabled() -> bool:
    return _AUTOSTART.exists()


def set_autostart(on: bool):
    if on:
        _AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
        _AUTOSTART.write_text(_autostart_body())
    else:
        _AUTOSTART.unlink(missing_ok=True)


def heal_autostart():
    """仓库被挪动/重命名后，自启条目里的旧路径会失效——启动时按当前路径重写。"""
    try:
        if _AUTOSTART.exists() and _AUTOSTART.read_text() != _autostart_body():
            _AUTOSTART.write_text(_autostart_body())
    except OSError:
        pass


# ── 图标绘制（cairo → PNG，AppIndicator 按文件名换图标） ────────────────────
def _surface():
    s = cairo.ImageSurface(cairo.FORMAT_ARGB32, _ICON_PX, _ICON_PX)
    return s, cairo.Context(s)


def draw_ring(pct_left: int) -> str:
    """环形进度图标，返回不带扩展名的 icon name。"""
    name = f"ring-claude-{max(0, min(100, pct_left))}"
    path = _ICON_DIR / f"{name}.png"
    if not path.exists():
        s, cr = _surface()
        cx = cy = _ICON_PX / 2
        r = _ICON_PX / 2 - _RING_LW / 2 - 2
        cr.set_line_width(_RING_LW)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_source_rgba(*_CLAUDE_COLOR, 72 / 255)          # 底环
        cr.arc(cx, cy, r, 0, 2 * 3.14159265)
        cr.stroke()
        if pct_left > 0:                                       # 剩余弧，12 点起顺时针
            cr.set_source_rgba(*_CLAUDE_COLOR, 1)
            start = -3.14159265 / 2
            cr.arc(cx, cy, r, start, start + 2 * 3.14159265 * pct_left / 100)
            cr.stroke()
        s.write_to_png(str(path))
    return name


def draw_shield(level: str) -> str:
    name = f"shield-{level}"
    path = _ICON_DIR / f"{name}.png"
    if not path.exists():
        s, cr = _surface()
        c = _SHIELD_COLORS.get(level, _SHIELD_COLORS[ipsec.SHIELD_IDLE])
        cr.set_source_rgba(*c, 1)
        cr.set_line_width(_SHIELD_LW)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        # 盾形轮廓：顶部平、两肩圆、底部收尖
        cr.move_to(32, 8)
        cr.line_to(52, 15)
        cr.line_to(52, 34)
        cr.curve_to(52, 46, 42, 54, 32, 58)
        cr.curve_to(22, 54, 12, 46, 12, 34)
        cr.line_to(12, 15)
        cr.close_path()
        cr.stroke()
        cr.set_line_width(6.0)
        if level == ipsec.SHIELD_OK:            # 勾
            cr.move_to(23, 33)
            cr.line_to(30, 40)
            cr.line_to(42, 26)
            cr.stroke()
        elif level == ipsec.SHIELD_WARN:        # 横线
            cr.move_to(23, 33)
            cr.line_to(41, 33)
            cr.stroke()
        elif level == ipsec.SHIELD_CRIT:        # 感叹号
            cr.move_to(32, 22)
            cr.line_to(32, 36)
            cr.stroke()
            cr.arc(32, 45, 3.2, 0, 2 * 3.14159265)
            cr.fill()
        else:                                   # 圆点（检测中 / 没测到）
            cr.arc(32, 34, 4.5, 0, 2 * 3.14159265)
            cr.fill()
        s.write_to_png(str(path))
    return name


def _fmt_reset(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        return ts_to_local(iso).strftime("%m-%d %H:%M")
    except Exception:
        return "—"


def _item(label, cb=None, sensitive=True):
    it = Gtk.MenuItem(label=label)
    if cb:
        it.connect("activate", cb)
    it.set_sensitive(sensitive and cb is not None)
    return it


def _autostart_item():
    it = Gtk.CheckMenuItem(label="开机自启")
    it.set_active(autostart_enabled())      # 先设状态再连信号，避免误触发
    it.connect("toggled", lambda w: set_autostart(w.get_active()))
    return it


# ── 主程序 ──────────────────────────────────────────────────────────────────
class Tray:
    def __init__(self):
        _ICON_DIR.mkdir(parents=True, exist_ok=True)
        heal_autostart()
        theme = str(_ICON_DIR)

        self.claude = AppIndicator.Indicator.new(
            "ai-limit-claude", draw_ring(0),
            AppIndicator.IndicatorCategory.APPLICATION_STATUS)
        self.claude.set_icon_theme_path(theme)
        self.claude.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.claude.set_label("…", "100%")

        self.shield = AppIndicator.Indicator.new(
            "ai-limit-ipsec", draw_shield(ipsec.SHIELD_IDLE),
            AppIndicator.IndicatorCategory.APPLICATION_STATUS)
        self.shield.set_icon_theme_path(theme)
        self.shield.set_status(AppIndicator.IndicatorStatus.ACTIVE)

        # 状态
        self.data = None            # 最近一次成功的 usage
        self.data_ts = 0.0
        self.fails = 0
        self.last_err = None
        self.show_7d = False        # label 默认显示 5h 窗口
        self.ip = None              # 最近一次 ipsec.probe()
        self._fetching = False
        self._probing = False

        self._rebuild_claude_menu()
        self._rebuild_shield_menu()
        GLib.idle_add(self.refresh_usage)
        GLib.idle_add(self.refresh_ip)
        GLib.timeout_add_seconds(_IPSEC_SEC, self._ip_tick)

    # ── Claude 用量 ─────────────────────────────────────────────
    def _schedule_next(self):
        delay = _REFRESH_SEC + random.randint(0, _JITTER_MAX_SEC)
        if self.fails >= _FAIL_GRACE_N:      # 连败指数退避
            delay = min(_REFRESH_SEC * (2 ** (self.fails - _FAIL_GRACE_N + 1)),
                        _BACKOFF_MAX_SEC)
        GLib.timeout_add_seconds(delay, self._usage_tick)

    def _usage_tick(self):
        self.refresh_usage()
        return False                          # 单次 timer，回调里再排下一次

    def refresh_usage(self, *_a):
        if self._fetching:
            return False
        self._fetching = True
        threading.Thread(target=self._fetch_usage, daemon=True).start()
        return False

    def _fetch_usage(self):
        try:
            raw = live_claude_usage()
            five, seven = raw.get("five_hour") or {}, raw.get("seven_day") or {}
            data = {
                "5h_left": round(100 - float(five.get("utilization", 0))),
                "7d_left": round(100 - float(seven.get("utilization", 0))),
                "5h_reset": five.get("resets_at"),
                "7d_reset": seven.get("resets_at"),
            }
            GLib.idle_add(self._usage_ok, data)
        except Exception as e:
            GLib.idle_add(self._usage_fail, f"{type(e).__name__}: {e}")

    def _usage_ok(self, data):
        self._fetching = False
        self.data, self.data_ts, self.fails, self.last_err = data, time.time(), 0, None
        self._render_claude()
        self._schedule_next()
        return False

    def _usage_fail(self, err):
        self._fetching = False
        self.fails += 1
        self.last_err = err
        self._render_claude()
        self._schedule_next()
        return False

    def _render_claude(self):
        stale = self.data_ts and (time.time() - self.data_ts > _STALE_MAX_SEC)
        bad = self.data is None or self.fails >= _FAIL_GRACE_N or stale
        if self.data:
            left = self.data["7d_left" if self.show_7d else "5h_left"]
            mark = "" if left >= 20 else ("⚠" if left >= 10 else "‼")
            self.claude.set_icon_full(draw_ring(left), "Claude")
            self.claude.set_label(f"{left}%{mark}" + ("⚠" if bad else ""), "100%⚠")
        else:
            self.claude.set_icon_full(draw_ring(0), "Claude")
            self.claude.set_label("⚠", "100%")
        self._rebuild_claude_menu()

    def _rebuild_claude_menu(self):
        m = Gtk.Menu()
        if self.data:
            d = self.data
            m.append(_item(f"Claude 5h 窗口：剩 {d['5h_left']}%  (重置 {_fmt_reset(d['5h_reset'])})"))
            m.append(_item(f"Claude 7d 窗口：剩 {d['7d_left']}%  (重置 {_fmt_reset(d['7d_reset'])})"))
            m.append(_item("更新于 " + time.strftime("%H:%M:%S", time.localtime(self.data_ts))))
        else:
            m.append(_item("暂无数据（获取中…）"))
        if self.fails:
            m.append(_item(f"连续失败 {self.fails} 次：{(self.last_err or '')[:60]}"))
        m.append(Gtk.SeparatorMenuItem())
        m.append(_item("显示 7 天窗口" if not self.show_7d else "显示 5 小时窗口",
                       self._toggle_window))
        m.append(_item("立即刷新", self.refresh_usage))
        m.append(_item("打开 Claude 用量页",
                       lambda *_: webbrowser.open("https://claude.ai/settings/usage")))
        m.append(Gtk.SeparatorMenuItem())
        m.append(_autostart_item())
        m.append(_item("退出", lambda *_: Gtk.main_quit()))
        m.show_all()
        self.claude.set_menu(m)

    def _toggle_window(self, *_a):
        self.show_7d = not self.show_7d
        self._render_claude()

    # ── IP 安全盾牌 ─────────────────────────────────────────────
    def _ip_tick(self):
        self.refresh_ip()
        return True                           # 固定周期

    def refresh_ip(self, *_a):
        if self._probing:
            return False
        self._probing = True
        threading.Thread(target=self._probe_ip, daemon=True).start()
        return False

    def _probe_ip(self):
        try:
            r = ipsec.probe()
        except Exception as e:                # probe 号称不抛，防御一层
            r = {"level": ipsec.SHIELD_IDLE, "error": f"{type(e).__name__}: {e}"}
        GLib.idle_add(self._ip_done, r)

    def _ip_done(self, r):
        self._probing = False
        self.ip = r
        level = r.get("level") or ipsec.SHIELD_IDLE
        # 机房 IP 不点亮盾牌，仅在菜单里注明（与 mac/win 版一致）
        self.shield.set_icon_full(draw_shield(level), "IP 安全度")
        self._rebuild_shield_menu()
        return False

    def _rebuild_shield_menu(self):
        m = Gtk.Menu()
        r = self.ip
        if not r:
            m.append(_item("检测中…"))
        else:
            level = r.get("level") or ipsec.SHIELD_IDLE
            head = {ipsec.SHIELD_OK: "网络环境正常",
                    ipsec.SHIELD_WARN: "需注意：出口 IP 有变化",
                    ipsec.SHIELD_CRIT: "有风险",
                    ipsec.SHIELD_IDLE: "未能完成检测"}[level]
            m.append(_item(head))
            if r.get("ip"):
                loc = " ".join(x for x in [r.get("country"), r.get("city")] if x)
                m.append(_item(f"出口 IP：{r['ip']}  {loc}"))
            if r.get("isp"):
                tags = [t for t, on in [("机房", r.get("is_datacenter")),
                                        ("VPN", r.get("is_vpn")),
                                        ("代理", r.get("is_proxy")),
                                        ("Tor", r.get("is_tor")),
                                        ("滥用源", r.get("is_abuser"))] if on]
                m.append(_item(f"ISP：{r['isp']}" + ("  [" + "/".join(tags) + "]" if tags else "")))
            if not r.get("reachable"):
                m.append(_item("claude.ai 不可达"))
            elif r.get("dns_leaked"):
                m.append(_item("DNS 泄露：解析出口与 IP 出口国家不一致"))
            elif r.get("dns_ok"):
                m.append(_item("DNS 未泄露"))
            if r.get("error"):
                m.append(_item(str(r["error"])[:60]))
            m.append(_item("WebRTC 需浏览器检测 →",
                           lambda *_: webbrowser.open(ipsec.SITE_URL)))
        m.append(Gtk.SeparatorMenuItem())
        m.append(_item("立即重新检测", self.refresh_ip))
        m.append(_item("打开完整检测页面",
                       lambda *_: webbrowser.open(ipsec.SITE_URL)))
        m.append(Gtk.SeparatorMenuItem())
        m.append(_autostart_item())
        m.show_all()
        self.shield.set_menu(m)


def main():
    Tray()
    Gtk.main()


if __name__ == "__main__":
    main()
