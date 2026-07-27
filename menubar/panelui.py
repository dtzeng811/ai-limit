"""详情面板的绘制层：环形进度原语 + NSPopover 卡片视图。

跟 ai-limit-app.py 的分工：这里只管画，不碰数据获取、不碰 i18n、不碰状态
持久化。App 把已经格式化好的 payload（纯 str/int）递进来，本模块负责变成
像素。好处是面板布局能单独跑起来预览（见 scratchpad/panel_preview.py），
不需要 cookie 登录态，也不用起整个 App。

为什么不用 NSMenu：菜单项只能是「一行文字 + 可选图标」，排不出卡片分组、
大号数字、环形进度这种层次。NSPopover 给一块自由画布，代价是设置项要另找
地方——现在挪到右键菜单（见 app 里的 _wire_status_button）。
"""
import AppKit
import objc
from Foundation import NSMakeRect, NSMakePoint, NSMakeSize

# ── 布局常量 ─────────────────────────────────────────────────────────────────
# 跟 App 的 _MENU_MIN_WIDTH 对齐：菜单宽度 = 最宽那一项，面板窄于它就会
# 左对齐、右边留一条空白。两边取同一个数，面板正好铺满。
PANEL_W      = 290.0
PAD          = 12.0
CARD_RADIUS  = 8.0
CARD_PAD     = 10.0
CARD_GAP     = 8.0
HEADER_H     = 19.0
ROW_H        = 23.0
FOOTER_H     = 26.0

RING_R       = 7.0    # 行内小环半径
RING_LW      = 2.2
TRACK_ALPHA  = 0.28

# IP 安全卡片：行比额度行矮（没有大号数字，纯文字），标签列定宽让四行的值
# 对齐成一列——不定宽就会随「出口 IP」「WebRTC」的字宽左右错位。
IP_ROW_H     = 18.0
IP_LABEL_W   = 46.0
IP_TEXT_DY   = 3.0    # 行内文字相对行顶的偏移
TAG_H        = 13.0   # 小圆角标签（[机房 IP] / [安全]）
TAG_DY       = 2.5
TAG_PAD_X    = 4.5
TAG_RADIUS   = 3.0
PART_GAP     = 6.0    # 同一行内相邻片段的间距

# ── 配色 ─────────────────────────────────────────────────────────────────────
# 环用品牌色（跟菜单栏一致，由 App 传 hex 进来），数字在告警档变色。
# 分工同菜单栏：环管「这是哪个服务」，数字管「还剩多少、慌不慌」。
def _num_color(level):
    return {
        "warn": AppKit.NSColor.systemYellowColor(),
        "crit": AppKit.NSColor.systemRedColor(),
    }.get(level, AppKit.NSColor.labelColor())


def _tone_color(tone):
    """标签/文字的语义色。tone 由 App 判定后传进来（本模块不认识业务语义），
    None = 中性，只是个事实注记而非告警——「机房 IP」就属于这一档。"""
    return {
        "ok":   AppKit.NSColor.systemGreenColor(),
        "warn": AppKit.NSColor.systemYellowColor(),
        "crit": AppKit.NSColor.systemRedColor(),
    }.get(tone, AppKit.NSColor.secondaryLabelColor())


def color_from_hex(hex_color):
    """状态点配色由 App 传 hex 进来（来源是 status.claude.com 的官方色系）。
    本模块不认识状态语义，只负责把颜色画成一个点——配色表在 App 那边保持
    单一来源，避免这里再复制一份 key 拼错（"partial" vs "partial_outage"）。
    """
    raw = str(hex_color).lstrip("#")
    try:
        r = int(raw[0:2], 16) / 255
        g = int(raw[2:4], 16) / 255
        b = int(raw[4:6], 16) / 255
    except Exception:
        return AppKit.NSColor.tertiaryLabelColor()
    return AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, 1.0)


# ── 绘制原语 ─────────────────────────────────────────────────────────────────
def draw_ring(center, radius, line_width, pct, color, track_alpha=TRACK_ALPHA):
    """底环 = 已用，实心弧 = 剩余，12 点起顺时针。"""
    track = AppKit.NSBezierPath.bezierPath()
    track.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_(
        center, radius, 0, 360
    )
    track.setLineWidth_(line_width)
    color.colorWithAlphaComponent_(track_alpha).setStroke()
    track.stroke()

    p = max(0.0, min(100.0, float(pct)))
    if p <= 0:
        return
    arc = AppKit.NSBezierPath.bezierPath()
    arc.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
        center, radius, 90.0, 90.0 - 360.0 * p / 100.0, True
    )
    arc.setLineWidth_(line_width)
    arc.setLineCapStyle_(AppKit.NSLineCapStyleRound)
    color.setStroke()
    arc.stroke()


SHIELD_ICON_W = 10.5
SHIELD_ICON_H = 12.0


def draw_shield(x, top, w, h, color):
    """卡头的小盾牌图标。跟菜单栏那个盾牌同一套比例（平顶 + 尖底），只是这里
    填实、不画内部符号——档位已经由颜色和右上角状态点表达，14pt 见方的图标
    再塞个符号只会糊成一团。

    不用 🛡 emoji：它在任何档位都是同一个偏红的彩色字形，绿灯状态下顶着个
    红盾牌，读起来像在报警。
    """
    def p(fx, fy):
        # 面板视图是 flipped 的（y 向下），比例坐标按常规 y 向上写更好读，
        # 换算集中在这一处
        return NSMakePoint(x + fx * w, top + (1.0 - fy) * h)

    path = AppKit.NSBezierPath.bezierPath()
    path.moveToPoint_(p(0.02, 0.94))
    path.lineToPoint_(p(0.98, 0.94))
    path.lineToPoint_(p(0.98, 0.44))
    path.curveToPoint_controlPoint1_controlPoint2_(
        p(0.50, 0.02), p(0.98, 0.24), p(0.76, 0.11))
    path.curveToPoint_controlPoint1_controlPoint2_(
        p(0.02, 0.44), p(0.24, 0.11), p(0.02, 0.24))
    path.closePath()
    color.setFill()
    path.fill()


def _attr(s, size, weight=AppKit.NSFontWeightRegular, color=None, mono=False):
    if mono:
        font = AppKit.NSFont.monospacedDigitSystemFontOfSize_weight_(size, weight)
    else:
        font = AppKit.NSFont.systemFontOfSize_weight_(size, weight)
    return AppKit.NSAttributedString.alloc().initWithString_attributes_(
        str(s),
        {
            AppKit.NSFontAttributeName: font,
            AppKit.NSForegroundColorAttributeName: color or AppKit.NSColor.labelColor(),
        },
    )


def _draw_right(astr, right_x, y):
    w = astr.size().width
    astr.drawAtPoint_(NSMakePoint(right_x - w, y))
    return w


def _draw_clipped(astr, x, y, max_w):
    """限宽 + 尾部截断地画一段文字，返回实际占用宽度。

    IP 卡片里的值全部来自上游接口（城市名、ISP、DNS 出口国家），长度不可控；
    drawAtPoint_ 不裁剪，长文案会横向捅出卡片——报错行早年踩过同一个坑。
    """
    if max_w <= 1:
        return 0.0
    natural = astr.size().width
    para = AppKit.NSMutableParagraphStyle.alloc().init()
    para.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
    m = AppKit.NSMutableAttributedString.alloc().initWithAttributedString_(astr)
    m.addAttribute_value_range_(
        AppKit.NSParagraphStyleAttributeName, para, (0, m.length()))
    w = min(natural, max_w)
    # 高度多给 2pt：drawInRect_ 会按矩形裁剪，贴着字高画会切掉下伸部
    m.drawInRect_(NSMakeRect(x, y, w, astr.size().height + 2))
    return w


def _draw_tag(text, x, y, max_w, tone=None):
    """小圆角标签：淡底 + 描边 + 小字。返回占用宽度（放不下返回 0，调用方停画）。"""
    color = _tone_color(tone)
    label = _attr(text, 9.0, AppKit.NSFontWeightMedium, color)
    w = min(label.size().width + TAG_PAD_X * 2, max_w)
    if w < TAG_PAD_X * 2 + 6:   # 连一个字都放不下就整块不画，别留个空壳
        return 0.0
    box = NSMakeRect(x, y, w, TAG_H)
    path = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        box, TAG_RADIUS, TAG_RADIUS)
    color.colorWithAlphaComponent_(0.14).setFill()
    path.fill()
    path.setLineWidth_(1.0)
    color.colorWithAlphaComponent_(0.45).setStroke()
    path.stroke()
    _draw_clipped(label, x + TAG_PAD_X, y + 1.0, w - TAG_PAD_X * 2)
    return w


ERR_ROW_H = 19.0


def card_height(card):
    # 报错卡没有数据行，但那句说明本身要占一行——早期版本漏算，报错文字会
    # 直接溢出卡片压到 footer 上
    if card.get("error"):
        return HEADER_H + ERR_ROW_H + CARD_PAD
    if card.get("kind") == "ip":
        return HEADER_H + len(card["rows"]) * IP_ROW_H + CARD_PAD
    return HEADER_H + len(card["rows"]) * ROW_H + CARD_PAD


def panel_height(cards):
    if not cards:
        return PAD + 34 + FOOTER_H + PAD
    h = PAD
    for c in cards:
        h += card_height(c) + CARD_GAP
    return h - CARD_GAP + FOOTER_H + PAD


# ── 面板视图 ─────────────────────────────────────────────────────────────────
class PanelView(AppKit.NSView):
    """payload 结构（全部是已格式化好的纯量，本模块不做任何业务判断）：

    {
      "cards": [
        {"title": "Claude Code", "plan": "Pro", "status_color": "#76AD2A",
         "error": None,
         "rows": [{"label": "5h", "pct": 58, "level": "ok",
                   "reset": "今天 13:00"}, ...]},

        {"kind": "ip", "icon": "shield", "title": "IP 安全", "hint": "在网页中检测 ↗",
         "status_color": "#76AD2A", "error": None,
         "rows": [{"label": "出口 IP", "parts": [
                     {"t": "text", "s": "199.19.104.157", "mono": True},
                     {"t": "text", "s": "US · Los Angeles", "dim": True},
                     {"t": "tag",  "s": "机房 IP", "tone": None}]}, ...]},
      ],
      "footer": "1 分钟刷新 · 上次 11:52:58",
      "empty":  "面板已关闭全部服务",   # cards 为空时显示
    }
    """

    def initWithFrame_(self, frame):
        self = objc.super(PanelView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._payload = {"cards": [], "footer": "", "empty": ""}
        # IP 卡片的点击热区，每次 drawRect_ 重算：卡片可能被用户关掉，
        # 留着上一次的矩形会让空白处也能点出浏览器
        self._ip_rect = None
        self._ip_click = None
        return self

    @objc.python_method
    def set_ip_click(self, handler):
        """IP 卡片的点击动作。用 python_method 而不是 setXxx_ 选择器：这里存的
        是个 Python callable，走 ObjC 桥接没必要也更容易出意外。"""
        self._ip_click = handler

    def setPayload_(self, payload):
        self._payload = payload or {"cards": [], "footer": "", "empty": ""}
        self.setNeedsDisplay_(True)

    def isFlipped(self):
        # 从上往下排版，跟阅读顺序一致，省得每个 y 都做一次翻转换算
        return True

    def drawRect_(self, rect):
        payload = self._payload
        cards = payload.get("cards") or []
        y = PAD

        self._ip_rect = None
        if not cards:
            _attr(payload.get("empty") or "", 11.5,
                  color=AppKit.NSColor.secondaryLabelColor()).drawAtPoint_(
                NSMakePoint(PAD, y + 8))
            y += 34
        for card in cards:
            if card.get("kind") == "ip":
                self._draw_ip_card(card, y)
            else:
                self._draw_card(card, y)
            y += card_height(card) + CARD_GAP
        if cards:
            y -= CARD_GAP

        # footer：刷新信息靠左，右侧留给齿轮按钮（按钮是真控件，不在这里画）
        _attr(payload.get("footer") or "", 10,
              color=AppKit.NSColor.tertiaryLabelColor()).drawAtPoint_(
            NSMakePoint(PAD + 2, y + 8))

    def _card_chrome(self, card, top):
        """卡片外框 + 卡头（标题、副标题/提示、右上角状态点），额度卡和 IP 卡
        共用同一套，保证圆角、内边距、状态点位置严格同构。返回
        (x_l, x_r, y, box)，y 已经跳过卡头。"""
        box = NSMakeRect(PAD, top, PANEL_W - PAD * 2, card_height(card))
        AppKit.NSColor.quaternarySystemFillColor().setFill()
        AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            box, CARD_RADIUS, CARD_RADIUS
        ).fill()

        x_l = PAD + CARD_PAD
        x_r = PANEL_W - PAD - CARD_PAD
        y = top + 7

        x_title = x_l
        if card.get("icon") == "shield":
            draw_shield(x_l, y + 1.5, SHIELD_ICON_W, SHIELD_ICON_H,
                        color_from_hex(card.get("status_color") or "#B0AEA5"))
            x_title += SHIELD_ICON_W + 5

        title = _attr(card["title"], 11.5, AppKit.NSFontWeightSemibold)
        title.drawAtPoint_(NSMakePoint(x_title, y))
        if card.get("plan"):
            _attr(f"  {card['plan']}", 10.5,
                  color=AppKit.NSColor.secondaryLabelColor()).drawAtPoint_(
                NSMakePoint(x_title + title.size().width, y + 0.5))

        # 状态点：贴右上角
        if card.get("status_color"):
            color_from_hex(card["status_color"]).setFill()
            AppKit.NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(x_r - 7, y + 4, 7, 7)
            ).fill()

        # 提示语（IP 卡的「在网页中检测 ↗」）：紧靠状态点左边，说明整卡可点。
        # 面板嵌在 NSMenu 里，鼠标悬停没有可靠的 hover 态，只能常驻显示。
        if card.get("hint"):
            _draw_right(_attr(card["hint"], 9.0,
                              color=AppKit.NSColor.tertiaryLabelColor()),
                        x_r - 12, y + 2.5)

        return x_l, x_r, y + HEADER_H, box

    def _draw_card_error(self, text, x_l, x_r, y):
        """整卡退化成一行说明（没有数据可画时）。"""
        para = AppKit.NSMutableParagraphStyle.alloc().init()
        para.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
        AppKit.NSAttributedString.alloc().initWithString_attributes_(
            text,
            {
                AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(10.5),
                AppKit.NSForegroundColorAttributeName:
                    AppKit.NSColor.secondaryLabelColor(),
                AppKit.NSParagraphStyleAttributeName: para,
            },
        ).drawInRect_(NSMakeRect(x_l, y + 2, x_r - x_l, ERR_ROW_H - 4))

    def _draw_card(self, card, top):
        x_l, x_r, y, _box = self._card_chrome(card, top)

        # ── 报错：整卡退化成一行说明，不画环（没有数字可画）────────
        if card.get("error"):
            self._draw_card_error(card["error"], x_l, x_r, y)
            return

        # ── 数据行：环 · 窗口 · 大数字 · 重置 ──────────────────────
        brand = color_from_hex(card.get("brand") or "#888888")
        for row in card["rows"]:
            self._draw_row(row, x_l, x_r, y, brand)
            y += ROW_H

    def _draw_ip_card(self, card, top):
        x_l, x_r, y, box = self._card_chrome(card, top)
        # 整张卡片是点击热区（跳转检测网页），热区随每次重画更新
        self._ip_rect = box

        if card.get("error"):
            self._draw_card_error(card["error"], x_l, x_r, y)
            return

        for row in card["rows"]:
            self._draw_ip_row(row, x_l, x_r, y)
            y += IP_ROW_H

    def _draw_ip_row(self, row, x_l, x_r, y):
        """标签定宽 + 值左对齐；值由若干片段（文字 / 标签）横排组成。
        每个片段都按剩余宽度限宽绘制，排不下就停——宁可少画一段，也不能
        让机房名、城市名这类不可控文本捅出卡片。"""
        _draw_clipped(
            _attr(row["label"], 10.0, AppKit.NSFontWeightMedium,
                  AppKit.NSColor.secondaryLabelColor()),
            x_l, y + IP_TEXT_DY, IP_LABEL_W)

        x = x_l + IP_LABEL_W
        for part in row.get("parts") or []:
            avail = x_r - x
            if avail <= 2:
                break
            if part.get("t") == "tag":
                w = _draw_tag(part["s"], x, y + TAG_DY, avail, part.get("tone"))
            else:
                color = (AppKit.NSColor.secondaryLabelColor() if part.get("dim")
                         else AppKit.NSColor.labelColor())
                w = _draw_clipped(
                    _attr(part["s"], 10.5, AppKit.NSFontWeightRegular, color,
                          mono=bool(part.get("mono"))),
                    x, y + IP_TEXT_DY, avail)
            if w <= 0:
                break
            x += w + PART_GAP

    def _draw_row(self, row, x_l, x_r, y, brand):
        pct = row.get("pct")
        num_color = _num_color(row.get("level"))
        cy = y + ROW_H / 2 - 2

        # 环。无数据那档只画一圈空底环，且要比有数据的更淡——它是「这档这次
        # 没返回」的占位，不该比真实数据更抢眼
        if pct is None:
            t = AppKit.NSBezierPath.bezierPath()
            t.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_(
                NSMakePoint(x_l + RING_R, cy), RING_R, 0, 360)
            t.setLineWidth_(RING_LW)
            AppKit.NSColor.quaternaryLabelColor().setStroke()
            t.stroke()
        else:
            draw_ring(NSMakePoint(x_l + RING_R, cy), RING_R, RING_LW, pct, brand)

        # 窗口标签
        _attr(row["label"], 10.5, AppKit.NSFontWeightMedium,
              AppKit.NSColor.secondaryLabelColor(), mono=True).drawAtPoint_(
            NSMakePoint(x_l + RING_R * 2 + 7, y + 4))

        # 大号百分比：这是整个面板的主角，字号拉到 15
        txt = "—" if pct is None else str(pct)
        num = _attr(txt, 15, AppKit.NSFontWeightSemibold,
                    num_color if pct is not None else AppKit.NSColor.tertiaryLabelColor(),
                    mono=True)
        num_x = x_l + RING_R * 2 + 30
        num.drawAtPoint_(NSMakePoint(num_x, y + 0.5))
        if pct is not None:
            _attr("%", 9.5, color=num_color.colorWithAlphaComponent_(0.7)).drawAtPoint_(
                NSMakePoint(num_x + num.size().width + 1, y + 5))

        # 重置时间：靠右
        if row.get("reset"):
            _draw_right(_attr(row["reset"], 10,
                              color=AppKit.NSColor.tertiaryLabelColor()),
                        x_r, y + 5)

    # ── 点击（只有 IP 卡片有热区）─────────────────────────────────────────
    #
    # 面板是 NSMenuItem.setView_ 内嵌的自绘视图，没有子控件可挂 target/action，
    # 只能自己接 mouseDown。别再想着换 NSPopover——rumps 的事件循环跟它不兼容
    # （见 App 里 _panel_payload 上方的长注释）。

    @objc.python_method
    def ip_hit(self, x, y):
        """点 (x, y)（本视图坐标系，已翻转）是否落在 IP 卡片里。抽出来是为了
        能在离屏脚本里直接验证热区，不用真去点菜单。"""
        r = self._ip_rect
        return r is not None and AppKit.NSPointInRect(NSMakePoint(x, y), r)

    def acceptsFirstMouse_(self, event):
        # 菜单弹出时窗口不是 key window，不接第一次点击就得点两下才有反应
        return True

    def mouseDown_(self, event):
        pt = self.convertPoint_fromView_(event.locationInWindow(), None)
        if not (self._ip_click and self.ip_hit(pt.x, pt.y)):
            return
        # 先收菜单再开浏览器：菜单是模态跟踪状态，留着它会挡在新窗口前面
        try:
            item = self.enclosingMenuItem()
            if item is not None and item.menu() is not None:
                item.menu().cancelTracking()
        except Exception:
            pass
        self._ip_click()


def make_panel_view(height):
    return PanelView.alloc().initWithFrame_(NSMakeRect(0, 0, PANEL_W, height))
