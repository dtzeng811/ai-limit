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

# ── 配色 ─────────────────────────────────────────────────────────────────────
# 环用品牌色（跟菜单栏一致，由 App 传 hex 进来），数字在告警档变色。
# 分工同菜单栏：环管「这是哪个服务」，数字管「还剩多少、慌不慌」。
def _num_color(level):
    return {
        "warn": AppKit.NSColor.systemYellowColor(),
        "crit": AppKit.NSColor.systemRedColor(),
    }.get(level, AppKit.NSColor.labelColor())


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


ERR_ROW_H = 19.0


def card_height(card):
    # 报错卡没有数据行，但那句说明本身要占一行——早期版本漏算，报错文字会
    # 直接溢出卡片压到 footer 上
    if card.get("error"):
        return HEADER_H + ERR_ROW_H + CARD_PAD
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
        return self

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

        if not cards:
            _attr(payload.get("empty") or "", 11.5,
                  color=AppKit.NSColor.secondaryLabelColor()).drawAtPoint_(
                NSMakePoint(PAD, y + 8))
            y += 34
        for card in cards:
            self._draw_card(card, y)
            y += card_height(card) + CARD_GAP
        if cards:
            y -= CARD_GAP

        # footer：刷新信息靠左，右侧留给齿轮按钮（按钮是真控件，不在这里画）
        _attr(payload.get("footer") or "", 10,
              color=AppKit.NSColor.tertiaryLabelColor()).drawAtPoint_(
            NSMakePoint(PAD + 2, y + 8))

    def _draw_card(self, card, top):
        h = card_height(card)
        box = NSMakeRect(PAD, top, PANEL_W - PAD * 2, h)
        AppKit.NSColor.quaternarySystemFillColor().setFill()
        AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            box, CARD_RADIUS, CARD_RADIUS
        ).fill()

        x_l = PAD + CARD_PAD
        x_r = PANEL_W - PAD - CARD_PAD
        y = top + 7

        # ── 卡头：服务名 · 方案 ────────────────────────────────────
        title = _attr(card["title"], 11.5, AppKit.NSFontWeightSemibold)
        title.drawAtPoint_(NSMakePoint(x_l, y))
        if card.get("plan"):
            _attr(f"  {card['plan']}", 10.5,
                  color=AppKit.NSColor.secondaryLabelColor()).drawAtPoint_(
                NSMakePoint(x_l + title.size().width, y + 0.5))

        # 状态点：贴右上角
        if card.get("status_color"):
            color_from_hex(card["status_color"]).setFill()
            AppKit.NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(x_r - 7, y + 4, 7, 7)
            ).fill()

        y += HEADER_H

        # ── 报错：整卡退化成一行说明，不画环（没有数字可画）────────
        if card.get("error"):
            # 用 drawInRect_ 限宽 + 尾部截断：错误文案来自上游接口，长度不可控，
            # drawAtPoint_ 不裁剪，长文案会横向捅出卡片
            para = AppKit.NSMutableParagraphStyle.alloc().init()
            para.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
            AppKit.NSAttributedString.alloc().initWithString_attributes_(
                card["error"],
                {
                    AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(10.5),
                    AppKit.NSForegroundColorAttributeName:
                        AppKit.NSColor.secondaryLabelColor(),
                    AppKit.NSParagraphStyleAttributeName: para,
                },
            ).drawInRect_(NSMakeRect(x_l, y + 2, x_r - x_l, ERR_ROW_H - 4))
            return

        # ── 数据行：环 · 窗口 · 大数字 · 重置 ──────────────────────
        brand = color_from_hex(card.get("brand") or "#888888")
        for row in card["rows"]:
            self._draw_row(row, x_l, x_r, y, brand)
            y += ROW_H

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


def make_panel_view(height):
    return PanelView.alloc().initWithFrame_(NSMakeRect(0, 0, PANEL_W, height))
