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
import quotacore  # noqa: E402  三端共享的行为核心（退避/刷新参数、品牌色）
import usage  # noqa: E402  数据层（跨平台）
from usage import live_claude_usage, ts_to_local  # noqa: E402

# ── 常量（引用 quotacore 单一来源，与 menubar / winbar 对齐） ────────────────
# 连败/过期/退避阈值都封装在 AbsorbState 里了，本文件只需刷新间隔和抖动
_REFRESH_SEC     = quotacore.REFRESH_SEC
_JITTER_MAX_SEC  = quotacore.JITTER_MAX_SEC
_IPSEC_SEC       = quotacore.IPSEC_REFRESH_SEC

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
        # 路径必须套引号：用户目录带空格/中文时，裸拼的 Exec 会在第一个空格处
        # 截断，开机静默启动失败且毫无痕迹——winbar 版在注册表 Run 键上踩过
        # 同一个坑（见其 autostart_command 注释）
        f'Exec="{sys.executable}" "{pathlib.Path(__file__).resolve()}"\n'
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


# i18n：与 winbar 同名同签名的 tr()，语言判定共用 quotacore.detect_lang
# （原先 linuxbar 界面全是硬编码中文，英文环境的用户会看到一整套中文菜单）
LANG = quotacore.detect_lang()
usage.set_lang(LANG)   # 数据层的 POSIX locale 判定在 GUI 进程里不可靠，同步过去


def tr(zh: str, en: str) -> str:
    return zh if LANG == "zh" else en


def ip_level(state) -> str:
    """IP 盾牌档位。连败/过期一律映射成灰（红只留给真的测出问题）——与
    winbar 的同名函数、设计文档第 4 节一致。"""
    d = state.data
    if not d or quotacore.ip_failed(d):
        return ipsec.SHIELD_IDLE
    return d.get("level") or ipsec.SHIELD_IDLE


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
    it = Gtk.CheckMenuItem(label=tr("开机自启", "Launch at Login"))
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

        # 状态。额度与 IP 的失败记账都交给 quotacore.AbsorbState——单次失败沿用
        # 上次好数据、连败 3 次才如实报错、连败后指数退避，与 mac/win 版逐字节
        # 一致（此前 linuxbar 是手写 self.fails 计数，且 IP 侧根本没有吸收/退避）。
        self.usage = quotacore.AbsorbState(lambda: _REFRESH_SEC)
        self.ipst = quotacore.make_ip_state(lambda: _IPSEC_SEC)
        self.show_7d = False        # label 默认显示 5h 窗口
        self._fetching = False
        self._probing = False
        self._usage_timer = None    # 已排定的下一次刷新（GLib source id）

        self._rebuild_claude_menu()
        self._rebuild_shield_menu()
        GLib.idle_add(self.refresh_usage)
        GLib.idle_add(self.refresh_ip)
        GLib.timeout_add_seconds(_IPSEC_SEC, self._ip_tick)

    # ── Claude 用量 ─────────────────────────────────────────────
    def _schedule_next(self):
        # 先取消已挂的下一次：刷新链是「fetch 完成 → 排下一次」自续的，手动
        # 「立即刷新」会在旧链 timer 仍挂着时开出第二条链——每点一次，请求
        # 频率就永久翻一倍，与低调采集原则直接冲突。单一 pending timer 是硬约束
        if self._usage_timer is not None:
            GLib.source_remove(self._usage_timer)
        # 退避时长直接读 AbsorbState 算好的 backoff_until，不再本地重算公式
        remain = self.usage.backoff_until - time.time()
        delay = max(remain, 0) if remain > 0 else _REFRESH_SEC + random.randint(0, _JITTER_MAX_SEC)
        self._usage_timer = GLib.timeout_add_seconds(int(delay), self._usage_tick)

    def _usage_tick(self):
        self._usage_timer = None              # 该 source 触发即自动销毁，别再 remove
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
        self.usage.absorb(data)              # 成功：吸收清账
        self._render_claude()
        self._schedule_next()
        return False

    def _usage_fail(self, err):
        self._fetching = False
        self.usage.absorb({"error": err})    # 失败：AbsorbState 决定沿用还是报错
        self._render_claude()
        self._schedule_next()
        return False

    def _render_claude(self):
        d = self.usage.data
        good = d and "error" not in d
        if good:
            left = d["7d_left" if self.show_7d else "5h_left"]
            # 符号分工（避免两种含义叠成 "15%⚠⚠"）：
            #   ⚠/‼ 只表额度高低（<20% / <10%）
            #   ~   表"这个数字是上一次的，正在重试"——吸收期沿用旧数据时，
            #       不给任何提示会让用户以为看到的是刚测出来的实时值。
            #       mac/win 版用 footer 的「重试中」表达，托盘 label 没有那个
            #       位置，用一个前缀符号等价传达
            mark = "" if left >= 20 else ("⚠" if left >= 10 else "‼")
            retry = "~" if self.usage.fail else ""
            self.claude.set_icon_full(draw_ring(left), "Claude")
            self.claude.set_label(f"{retry}{left}%{mark}", "~100%‼")
        else:
            self.claude.set_icon_full(draw_ring(0), "Claude")
            self.claude.set_label("⚠", "~100%‼")
        self._rebuild_claude_menu()

    def _rebuild_claude_menu(self):
        m = Gtk.Menu()
        d = self.usage.data
        if d and "error" not in d:
            for key in ("5h", "7d"):
                m.append(_item(tr(
                    f"Claude {key} 窗口：剩 {d[key + '_left']}%  (重置 {_fmt_reset(d[key + '_reset'])})",
                    f"Claude {key}: {d[key + '_left']}% left  (resets {_fmt_reset(d[key + '_reset'])})")))
            m.append(_item(tr("更新于 ", "updated ") +
                           time.strftime("%H:%M:%S", time.localtime(self.usage.good_ts))))
        else:
            m.append(_item(tr("暂无数据（获取中…）", "No data yet (fetching…)")))
        if self.usage.fail:
            err = (d or {}).get("error", "") if d else ""
            m.append(_item(tr(f"连续失败 {self.usage.fail} 次：{err[:60]}",
                              f"{self.usage.fail} consecutive failures: {err[:60]}")))
        m.append(Gtk.SeparatorMenuItem())
        m.append(_item(tr("显示 7 天窗口", "Show 7-day window") if not self.show_7d
                       else tr("显示 5 小时窗口", "Show 5-hour window"),
                       self._toggle_window))
        m.append(_item(tr("立即刷新", "Refresh now"), self.refresh_usage))
        m.append(_item(tr("打开 Claude 用量页", "Open Claude usage page"),
                       lambda *_: webbrowser.open("https://claude.ai/settings/usage")))
        m.append(Gtk.SeparatorMenuItem())
        m.append(_autostart_item())
        m.append(_item(tr("退出", "Quit"), lambda *_: Gtk.main_quit()))
        m.show_all()
        self.claude.set_menu(m)

    def _toggle_window(self, *_a):
        self.show_7d = not self.show_7d
        self._render_claude()

    # ── IP 安全盾牌 ─────────────────────────────────────────────
    def _ip_tick(self):
        # 退避期内这一轮跳过（AbsorbState 已算好 backoff_until），不发请求
        if not self.ipst.in_backoff():
            self.refresh_ip()
        return True                           # 固定周期 timer，退避靠跳过实现

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
        self.ipst.absorb(r)                   # 吸收：单次失败沿用上次盾牌，连败才变灰
        # 机房 IP 不点亮盾牌，仅在菜单里注明（与 mac/win 版一致）；ip_level 把
        # 连败/过期统一映射成灰（红只留给真的测出问题）
        self.shield.set_icon_full(draw_shield(ip_level(self.ipst)), tr("IP 安全度", "IP Security"))
        self._rebuild_shield_menu()
        return False

    def _rebuild_shield_menu(self):
        m = Gtk.Menu()
        r = self.ipst.data
        if not r:
            m.append(_item(tr("检测中…", "Checking…")))
        else:
            # 菜单头走 ip_level（连败/过期 → 灰），与盾牌图标一致；不能直接用
            # r["level"]，否则连败时图标是灰、菜单头却还写"有风险"，自相矛盾
            level = ip_level(self.ipst)
            head = {ipsec.SHIELD_OK:   tr("网络环境正常", "Network looks fine"),
                    ipsec.SHIELD_WARN: tr("需注意：出口 IP 有变化", "Heads up: exit IP changed"),
                    ipsec.SHIELD_CRIT: tr("有风险", "At risk"),
                    ipsec.SHIELD_IDLE: tr("未能完成检测", "Check did not complete")}[level]
            m.append(_item(head))
            if r.get("ip"):
                loc = " ".join(x for x in [r.get("country"), r.get("city")] if x)
                m.append(_item(tr(f"出口 IP：{r['ip']}  {loc}", f"Exit IP: {r['ip']}  {loc}")))
            if r.get("isp"):
                tags = [t for t, on in [
                    (tr("机房", "Datacenter"), r.get("is_datacenter")),
                    ("VPN", r.get("is_vpn")),
                    (tr("代理", "Proxy"), r.get("is_proxy")),
                    ("Tor", r.get("is_tor")),
                    (tr("滥用源", "Abuser"), r.get("is_abuser"))] if on]
                m.append(_item(tr(f"ISP：{r['isp']}", f"ISP: {r['isp']}")
                               + ("  [" + "/".join(tags) + "]" if tags else "")))
            if not r.get("reachable"):
                m.append(_item(tr("claude.ai 不可达", "claude.ai unreachable")))
            elif r.get("dns_leaked"):
                m.append(_item(tr("DNS 泄露：解析出口与 IP 出口国家不一致",
                                  "DNS leak: resolver country differs from exit IP")))
            elif r.get("dns_ok"):
                m.append(_item(tr("DNS 未泄露", "No DNS leak")))
            if r.get("error"):
                m.append(_item(str(r["error"])[:60]))
            m.append(_item(tr("WebRTC 需浏览器检测 →", "WebRTC needs a browser →"),
                           lambda *_: webbrowser.open(ipsec.SITE_URL)))
        m.append(Gtk.SeparatorMenuItem())
        m.append(_item(tr("立即重新检测", "Re-check now"), self.refresh_ip))
        m.append(_item(tr("打开完整检测页面", "Open full check page"),
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
