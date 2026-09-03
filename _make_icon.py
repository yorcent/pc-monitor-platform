# -*- coding: utf-8 -*-
"""生成 PC-Monitor 应用图标：蓝青渐变圆角方块 + 白色仪表盘(金色指针) + 白色闪电"""
from PIL import Image, ImageDraw, ImageFilter
import math

S = 256
R = 56

# ---- 渐变圆角背景 ----
base = Image.new("RGB", (S, S), (0, 0, 0))
grad = Image.new("RGB", (1, S))
for y in range(S):
    t = y / S
    r = int(15 + (6 - 15) * t)
    g = int(23 + (182 - 23) * t)
    b = int(42 + (212 - 42) * t)
    grad.putpixel((0, y), (r, g, b))
grad = grad.resize((S, S))
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=R, fill=255)
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
img.paste(grad, (0, 0), mask)

# ---- 左上高光 ----
hl = Image.new("RGBA", (S, S), (0, 0, 0, 0))
ImageDraw.Draw(hl).ellipse([-70, -90, 150, 130], fill=(255, 255, 255, 42))
hl = hl.filter(ImageFilter.GaussianBlur(22))
img.alpha_composite(hl)

d = ImageDraw.Draw(img)

# ---- 仪表盘（半圆弧 + 刻度）----
cx, cy, rad = 128, 156, 74
d.arc([cx - rad, cy - rad, cx + rad, cy + rad], start=180, end=360,
      fill=(255, 255, 255, 235), width=9)
for i in range(7):
    ang = math.pi * (1 - i / 6)
    x1 = cx + math.cos(ang) * rad * 0.76
    y1 = cy + math.sin(ang) * rad * 0.76
    x2 = cx + math.cos(ang) * rad * 0.92
    y2 = cy + math.sin(ang) * rad * 0.92
    d.line([x1, y1, x2, y2], fill=(255, 255, 255, 210), width=4)

# ---- 金色指针指向 75% ----
ang = math.pi * (1 - 0.75)
px = cx + math.cos(ang) * rad * 0.6
py = cy + math.sin(ang) * rad * 0.6
d.line([cx, cy, px, py], fill=(253, 224, 71, 255), width=8)
d.ellipse([cx - 9, cy - 9, cx + 9, cy + 9], fill=(253, 224, 71, 255))

# ---- 白色闪电（性能/提速）----
bolt = [(144, 62), (104, 126), (122, 126), (110, 194),
        (168, 104), (148, 104), (160, 62)]
d.polygon(bolt, fill=(255, 255, 255, 255))

# ---- 底部品牌圆点 ----
d.ellipse([118, 214, 138, 234], fill=(255, 255, 255, 235))

img.save("icon.png")
img.save("icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                            (64, 64), (128, 128), (256, 256)])
print("icon.png / icon.ico 已生成")
