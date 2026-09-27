#!/usr/bin/env python3
"""Draw the link-preview image (og.png) and the home-screen icon (apple-touch-icon.png).

Runs only when either file is missing. Uses the Archivo font from Google Fonts' GitHub repo.
"""
import io
import os
import sys

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = os.path.join(ROOT, "site")
FONT_URL = "https://raw.githubusercontent.com/google/fonts/main/ofl/archivo/Archivo%5Bwdth%2Cwght%5D.ttf"

INK, INK2, GROUND, TAB = (21, 23, 28), (84, 90, 101), (231, 233, 237), (232, 98, 42)


def font(data, size, wdth, wght):
    f = ImageFont.truetype(io.BytesIO(data), size)
    try:
        axes = f.get_variation_axes()
        values = []
        for ax in axes:
            name = ax.get("name", b"")
            name = name.decode() if isinstance(name, bytes) else str(name)
            values.append(wdth if name.lower().startswith("width") else wght)
        f.set_variation_by_axes(values)
    except Exception as e:  # static fallback is fine
        print("font variation not applied:", e)
    return f


def orb(size, top, bottom, rim=True):
    """A soft 'glass' disc with a vertical gradient and a light rim."""
    s = size * 4
    grad = Image.new("RGB", (1, s))
    for y in range(s):
        t = y / (s - 1)
        grad.putpixel((0, y), tuple(int(top[k] + (bottom[k] - top[k]) * t) for k in range(3)))
    grad = grad.resize((s, s))
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, s - 1, s - 1), fill=255)
    disc = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    disc.paste(grad, (0, 0), mask)
    if rim:
        d = ImageDraw.Draw(disc)
        d.ellipse((6, 6, s - 7, s - 7), outline=(255, 255, 255, 110), width=6)
        hi = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        ImageDraw.Draw(hi).ellipse((int(s * .18), int(s * .06), int(s * .82), int(s * .46)), fill=(255, 255, 255, 60))
        hi = hi.filter(ImageFilter.GaussianBlur(s * .04))
        disc = Image.alpha_composite(disc, Image.composite(hi, Image.new("RGBA", (s, s), (0, 0, 0, 0)), mask))
    return disc.resize((size, size), Image.LANCZOS)


def shadowed(canvas, img, x, y, blur=18, offset=10, alpha=70):
    sh = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    a = img.split()[3].point(lambda v: v * alpha // 255)
    sh.paste((0, 0, 0, 255), (x, y + offset), a)
    sh = sh.filter(ImageFilter.GaussianBlur(blur))
    canvas.alpha_composite(sh)
    canvas.alpha_composite(img, (x, y))


PALETTE = [((236, 239, 244), (196, 204, 216)), ((250, 230, 214), (236, 186, 150)), ((214, 226, 214), (160, 186, 168)),
           ((218, 230, 246), (150, 180, 222)), ((70, 76, 88), (28, 31, 38)), ((244, 238, 226), (214, 200, 176)),
           ((255, 170, 120), (232, 98, 42)), ((226, 222, 244), (176, 168, 222)), ((200, 208, 218), (120, 130, 146)),
           ((248, 242, 236), (226, 214, 204))]


def make_og(fdata, path):
    W, H = 1200, 630
    env = Image.new("RGB", (W, H), GROUND)
    d = ImageDraw.Draw(env)
    d.ellipse((-420, -420, 700, 460), fill=(247, 236, 222))
    d.ellipse((640, 200, 1500, 980), fill=(200, 211, 224))
    im = env.filter(ImageFilter.GaussianBlur(110)).convert("RGBA")

    # floating round "apps", arranged like the visionOS Home View
    r, gap = 96, 16
    rows = [3, 4, 3]
    cx0, cy0 = 950, 318
    k = 0
    for ri, n in enumerate(rows):
        y = cy0 + (ri - 1) * (r + gap - 12) - r // 2
        width = n * r + (n - 1) * gap
        for j in range(n):
            x = cx0 - width // 2 + j * (r + gap)
            top, bottom = PALETTE[k % len(PALETTE)]
            shadowed(im, orb(r, top, bottom), x, y)
            k += 1

    d = ImageDraw.Draw(im)
    d.rounded_rectangle((72, 92, 108, 104), radius=6, fill=TAB)  # the band's pull tab
    small = font(fdata, 22, 100, 600)
    d.text((124, 85), "VISIONOS APP STORE  ·  UPDATED DAILY", font=small, fill=INK2)
    big = font(fdata, 102, 125, 760)
    d.text((66, 134), "Only on", font=big, fill=INK)
    d.text((66, 242), "Vision Pro", font=big, fill=INK)
    body = font(fdata, 34, 100, 500)
    d.text((72, 408), "Every app made only for", font=body, fill=INK2)
    d.text((72, 452), "Apple Vision Pro, in one place.", font=body, fill=INK2)
    im.convert("RGB").save(path, "PNG", optimize=True)


def make_touch_icon(path):
    S = 180
    im = Image.new("RGBA", (S, S), INK + (255,))
    for (x, y), (top, bottom) in zip([(28, 34), (98, 34), (63, 94)],
                                     [((236, 239, 244), (170, 178, 190)), ((150, 158, 170), (96, 104, 116)), ((255, 170, 120), (232, 98, 42))]):
        shadowed(im, orb(54, top, bottom), x, y, blur=6, offset=4, alpha=120)
    im.convert("RGB").save(path, "PNG", optimize=True)


def main():
    og = os.path.join(SITE, "og.png")
    touch = os.path.join(SITE, "apple-touch-icon.png")
    force = "--force" in sys.argv
    if os.path.exists(og) and os.path.exists(touch) and not force:
        print("images present")
        return
    fdata = requests.get(FONT_URL, timeout=60).content
    if force or not os.path.exists(og):
        make_og(fdata, og)
        print("wrote", og)
    if force or not os.path.exists(touch):
        make_touch_icon(touch)
        print("wrote", touch)


if __name__ == "__main__":
    main()
