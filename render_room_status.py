# -*- coding: utf-8 -*-
"""
会议室状态牌渲染脚本(reTerminal E / 800x480 墨水屏)
布局 v4:与 HTML 定稿设计(room_status_template.html)一致——
  全版面深青 #003A4A 文字;空闲块绿底白字;busy 白底深青描边;
  左右分栏竖线、门牌号下分割线保持黑色;
  "预约信息"分割线与左栏门牌号下分割线严格同高(y=231);
  温度旁天气为线性图标(Pillow 线描,几何与 HTML 的 SVG 图标表一致)。
输出两种配色:
  mode="color" -> E1002 七色彩屏(固件 online_image RGB565)。注意 E6 驱动按
                  RGB 立方体角量化:绿底用 (127,195,31) 把红通道压到 <=127,
                  保证落 GREEN 桶(原 HTML 的 143,195,31 会落 YELLOW 桶)。
  mode="bw"    -> E1001 黑白屏(固件 online_image BINARY):空闲块黑底白字,
                  其余文字/线条全黑,纯黑白两色阈值转换无歧义。
"""
import os
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont

W, H = 800, 480
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")

# ---------- 示例数据(之后由飞书 API 填充) ----------
ROOM = {
    "name": "10A · 210会议室",
    "label": "210",              # 会议室号(大字展示)
    "status": "free",            # free | busy
    "temperature": "27.1",
    "weather": "多云",
    "bookings": [                # (预约时间, 预约人, 是否当前场次)
        ("09:30–10:00", "张伟", False),
        ("13:00–14:00", "李娜", True),
        ("15:00–16:00", "招聘助手", False),
    ],
}

# 布局常量(与 HTML 800×480 面板逐像素对应)
DIVIDER_X = 420          # 左右分栏竖线(.left 宽 388 + 左 padding 32)
LEFT_X = 32              # 左栏内容左缘
LEFT_R = 396             # 左栏信息区右缘(竖线 - 24 margin)
ROOM_X = 48              # 门牌号左缘(padding 0 16)
RIGHT_X = 444            # 右栏左边界(竖线 + 24 padding)
RIGHT_R = 768            # 右栏右缘(W - 32)
ALIGN_Y = 231            # 对齐基准线:门牌号下分割线 = "预约信息"下划线
TEMP_BASE_Y = 396        # 温度数字基线
ICON_BOX = 72            # 天气图标盒(与 HTML .weather svg 同)

# 两种屏的配色(见模块 docstring)
PALETTES = {
    "color": {
        "text": (0, 58, 74),        # 深青;E6 驱动全通道 <128 落 BLACK 桶,彩屏上即黑
        "divider": (0, 0, 0),
        "free_bg": (127, 195, 31),  # 红通道 <=127 -> GREEN 桶
        "free_fg": (255, 255, 255),
        "busy_bg": (255, 255, 255),
    },
    "bw": {
        "text": (0, 0, 0),
        "divider": (0, 0, 0),
        "free_bg": (0, 0, 0),
        "free_fg": (255, 255, 255),
        "busy_bg": (255, 255, 255),
    },
}

# ---------- 字体(Windows 用自带微软雅黑,Linux 容器用项目自带 Noto Sans SC) ----------
_FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")

def font(size, bold=False):
    candidates = [
        r"C:\Windows\Fonts\msyhbd.ttc" if bold else r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\simsun.ttc",
        os.path.join(_FONT_DIR, "NotoSansSC.ttf"),   # 项目自带(可变字重),跨平台兜底
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold
        else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    ]
    for path in candidates:
        if os.path.exists(path):
            f = ImageFont.truetype(path, size)
            if os.path.basename(path) == "NotoSansSC.ttf":
                # 容器回退到这个可变字体。默认实例(400)笔画比雅黑细,
                # 墨水屏边缘吃细笔画会发虚;字重轴拨到 600 对齐本机雅黑观感(已对比验证)。
                try:
                    if bold:
                        f.set_variation_by_name("Bold")
                    else:
                        f.set_variation_by_axes([600])
                except Exception:
                    pass
            return f
    return ImageFont.load_default()


# ---------- 天气线性图标(与 HTML WEATHER_ICONS 同几何,viewBox 24×24) ----------
def _icon_helpers(d, ox, oy, s, w, col):
    """把 24×24 单位坐标映射到像素(ox,oy 为图标盒左上角,s=像素/单位)的绘图助手"""
    def P(x, y):
        return (ox + x * s, oy + y * s)

    def line(x1, y1, x2, y2):
        d.line([P(x1, y1), P(x2, y2)], fill=col, width=w, joint="curve")
        r = w / 2                                   # 圆头端点(SVG stroke-linecap=round)
        for x, y in ((x1, y1), (x2, y2)):
            cx, cy = P(x, y)
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col)

    def polyline(*pts):
        for a, b in zip(pts, pts[1:]):
            line(a[0], a[1], b[0], b[1])

    def circle(cx, cy, r):
        x0, y0 = P(cx - r, cy - r)
        x1, y1 = P(cx + r, cy + r)
        d.ellipse([x0, y0, x1, y1], outline=col, width=w)

    def arc(cx, cy, r, a0, a1):
        x0, y0 = P(cx - r, cy - r)
        x1, y1 = P(cx + r, cy + r)
        d.arc([x0, y0, x1, y1], a0, a1, fill=col, width=w)

    def cloud(tx, ty, sc):
        # feather 云路径 = 大圆(9,12,r8)长弧 + 小圆(18,15,r5)右半弧 + 底/顶两条横线;
        # 缩放后角度不变,坐标过 translate(tx,ty) scale(sc)
        def T(x, y):
            return (x * sc + tx, y * sc + ty)
        bcx, bcy = T(9, 12)
        scx, scy = T(18, 15)
        arc(bcx, bcy, 8 * sc, 90, 345.5)      # 底 -> 左 -> 顶 -> 右上
        arc(scx, scy, 5 * sc, -90, 90)        # 右半
        line(*T(9, 20), *T(18, 20))           # 底边
        line(*T(16.74, 10), *T(18, 10))       # 顶短边

    def flake(x, y):
        line(x, y - 2.2, x, y + 2.2)
        line(x - 1.9, y - 1.1, x + 1.9, y + 1.1)
        line(x + 1.9, y - 1.1, x - 1.9, y + 1.1)

    CLOUD_S = lambda: cloud(1.4, -1.5, 0.85)   # 降水类共用的小云(同 HTML CLOUD_S)
    return dict(line=line, polyline=polyline, circle=circle,
                arc=arc, cloud=cloud, flake=flake, CLOUD_S=CLOUD_S)


ICON_DRAWERS = {
    "晴": lambda h: (h["circle"](12, 12, 4.5),
                     h["line"](12, 2.5, 12, 5), h["line"](12, 19, 12, 21.5),
                     h["line"](2.5, 12, 5, 12), h["line"](19, 12, 21.5, 12),
                     h["line"](4.6, 4.6, 6.4, 6.4), h["line"](17.6, 17.6, 19.4, 19.4),
                     h["line"](19.4, 4.6, 17.6, 6.4), h["line"](6.4, 17.6, 4.6, 19.4)),
    "多云": lambda h: (h["circle"](6, 6, 2.6),
                       h["line"](6, 1.4, 6, 3), h["line"](1.4, 6, 3, 6),
                       h["line"](2.7, 2.7, 3.9, 3.9), h["line"](9.3, 2.7, 8.1, 3.9),
                       h["cloud"](4.5, 4.5, 0.75)),
    "阴": lambda h: h["cloud"](0, 0, 1),
    "雾": lambda h: (h["CLOUD_S"](), h["line"](5, 18.5, 19, 18.5),
                     h["line"](7.5, 21.5, 16.5, 21.5)),
    "毛毛雨": lambda h: (h["CLOUD_S"](), h["line"](8, 18.5, 8, 20.3),
                         h["line"](12, 20, 12, 21.8), h["line"](16, 18.5, 16, 20.3)),
    "冻雨": lambda h: (h["CLOUD_S"](), h["line"](8, 18.5, 8, 21.5),
                       h["line"](16, 18.5, 16, 21.5), h["flake"](12, 19.8)),
    "小雨": lambda h: (h["CLOUD_S"](), h["line"](9, 18.5, 9, 21.5),
                       h["line"](14.5, 18.5, 14.5, 21.5)),
    "中雨": lambda h: (h["CLOUD_S"](), h["line"](8, 18.5, 8, 21.5),
                       h["line"](12, 19.5, 12, 22.5), h["line"](16, 18.5, 16, 21.5)),
    "大雨": lambda h: (h["CLOUD_S"](), h["line"](7.5, 18, 7.5, 21.5),
                       h["line"](11, 19, 11, 22.5), h["line"](14.5, 18, 14.5, 21.5),
                       h["line"](18, 19, 18, 22.5)),
    "阵雨": lambda h: (h["CLOUD_S"](), h["line"](8, 18.5, 8, 21.5),
                       h["line"](12, 19.5, 12, 22.5), h["line"](16, 18.5, 16, 21.5)),
    "暴雨": lambda h: (h["CLOUD_S"](), h["line"](6.5, 18, 6.5, 22),
                       h["line"](9.5, 19.5, 9.5, 23.5), h["line"](12.5, 18, 12.5, 22),
                       h["line"](15.5, 19.5, 15.5, 23.5), h["line"](18.5, 18, 18.5, 22)),
    "小雪": lambda h: (h["CLOUD_S"](), h["flake"](12, 19.5)),
    "中雪": lambda h: (h["CLOUD_S"](), h["flake"](9.5, 19.5), h["flake"](14.5, 19.5)),
    "大雪": lambda h: (h["CLOUD_S"](), h["flake"](8, 19.5), h["flake"](12, 19.5),
                       h["flake"](16, 19.5)),
    "雪": lambda h: (h["CLOUD_S"](), h["flake"](12, 19.5)),
    "阵雪": lambda h: (h["CLOUD_S"](), h["flake"](9.5, 19.5), h["flake"](14.5, 19.5)),
    "雷暴": lambda h: (h["CLOUD_S"](), h["polyline"]((13, 14.5), (9.5, 19),
                                                     (14, 19), (10.5, 23))),
    "冰雹": lambda h: (h["CLOUD_S"](), h["circle"](8.5, 19, 1.1),
                       h["circle"](12.5, 20.5, 1.1), h["circle"](16, 19, 1.1)),
}


def draw_weather_icon(d, name, ox, oy, box, col):
    """在 (ox,oy) 处画 box×box 的天气图标;未收录的名字返回 False"""
    drawer = ICON_DRAWERS.get(name)
    if not drawer:
        return False
    s = box / 24.0
    w = max(2, round(1.8 * s))                  # 与 SVG stroke-width 1.8 同比例
    drawer(_icon_helpers(d, ox, oy, s, w, col))
    return True


def render(room: dict, now: datetime = None, mode: str = "color") -> Image.Image:
    now = now or datetime.now()
    pal = PALETTES[mode]
    text, divider = pal["text"], pal["divider"]
    img = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)

    weekday_cn = "一二三四五六日"[now.weekday()]
    date_str = f"{now.strftime('%Y-%m-%d')}  周{weekday_cn}"

    # ---- 左右分栏竖线(黑) ----
    d.line([DIVIDER_X, 24, DIVIDER_X, H - 24], fill=divider, width=1)

    # ---- 左栏:会议室号(大字) ----
    d.text((ROOM_X, 40), room.get("label") or room["name"],
           font=font(150, bold=True), fill=text)

    # ---- 左栏下部:分割线(黑) + 日期 + 今日温度 + 天气图标 ----
    d.line([LEFT_X, ALIGN_Y, LEFT_R, ALIGN_Y], fill=divider, width=1)
    d.text((LEFT_X, 249), date_str, font=font(28), fill=text)
    d.text((LEFT_X, 293), "今日温度", font=font(24), fill=text)
    d.text((LEFT_X, TEMP_BASE_Y), room["temperature"],
           font=font(68, bold=True), fill=text, anchor="ls")
    tw = d.textbbox((0, 0), room["temperature"], font=font(68, bold=True))[2]
    ux = LEFT_X + tw + 8
    d.text((ux, TEMP_BASE_Y), "°C", font=font(30), fill=text, anchor="ls")
    uw = d.textbbox((0, 0), "°C", font=font(30))[2]
    wx = ux + uw + 16
    # 图标盒:底边比基线低 13px(同 HTML vertical-align:-13px 的对齐关系)
    if not draw_weather_icon(d, room.get("weather") or "", wx,
                             TEMP_BASE_Y + 13 - ICON_BOX, ICON_BOX, text):
        d.text((wx, TEMP_BASE_Y), room.get("weather") or "",
               font=font(30), fill=text, anchor="ls")

    # ---- 右栏:正在使用 + 预约信息 ----
    current = next((b for b in room["bookings"] if len(b) > 2 and b[2]), None)
    upcoming = [b for b in room["bookings"] if not (len(b) > 2 and b[2])]
    f_title = font(26, bold=True)
    title_h = d.textbbox((0, 0), "预约信息", font=f_title)[3]

    if current:
        # 正在使用:标题 + 深青下划线,白底描边状态框
        d.text((RIGHT_X, 34), "正在使用", font=f_title, fill=text)
        d.line([RIGHT_X, 34 + title_h + 8, RIGHT_R, 34 + title_h + 8],
               fill=text, width=1)
        d.rectangle([RIGHT_X, 87, RIGHT_R, 153], fill=pal["busy_bg"],
                    outline=text, width=2)
        d.text((RIGHT_X + 8, 120), current[0], font=font(24, bold=True),
               fill=text, anchor="lm")
        d.text((RIGHT_R - 8, 120), current[1], font=font(24, bold=True),
               fill=text, anchor="rm")
    else:
        # 空闲:整块底色 + 白字居中(标题隐藏)
        d.rectangle([RIGHT_X, 34, RIGHT_R, 152], fill=pal["free_bg"])
        d.text(((RIGHT_X + RIGHT_R) / 2, 93), "当前空闲",
               font=font(36, bold=True), fill=pal["free_fg"], anchor="mm")

    # 模块二:"预约信息"下划线严格压在 ALIGN_Y,与左栏门牌号分割线同高
    d.text((RIGHT_X, ALIGN_Y - 8 - title_h), "预约信息", font=f_title, fill=text)
    d.line([RIGHT_X, ALIGN_Y, RIGHT_R, ALIGN_Y], fill=text, width=1)
    y = ALIGN_Y + 14
    if not upcoming:
        d.text((RIGHT_X, y), "今日暂无更多预约", font=font(22), fill=text)
    else:
        for booking in upcoming[:5]:
            d.text((RIGHT_X, y), booking[0], font=font(22), fill=text)
            d.text((RIGHT_R, y), booking[1], font=font(22), fill=text, anchor="ra")
            y += 46

    return img


if __name__ == "__main__":
    os.makedirs(OUT_DIR, exist_ok=True)
    for m in ("color", "bw"):
        out_path = os.path.join(OUT_DIR, f"room_status_{m}.png")
        render(ROOM, mode=m).save(out_path)
        print("已生成:", out_path)
