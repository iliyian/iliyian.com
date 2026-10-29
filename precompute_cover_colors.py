#!/usr/bin/env python3
"""预计算封面主色：扫描构建后的 HTML，用 128px 缩略图算色并生成 JS 映射。

构建流程中在 CN/EN 合并后调用；不缓存，每次构建重新计算。
"""
import io
import json
import math
import os
import re
import sys
import urllib.parse
import urllib.request
from html import unescape

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CN_PUBLIC = os.path.join(BASE_DIR, "blogxyz", "public")
EN_PUBLIC = os.path.join(BASE_DIR, "blogxyz-en", "public")
THUMB_BASE = "https://r2-imgs.iliyian.com/cdn-cgi/image/width=128,quality=75,format=webp/"
R2_HOST = "r2-imgs.iliyian.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

try:
    from PIL import Image
except ImportError:
    print("[cover-colors] Pillow not installed; writing empty map")
    Image = None


def _round_half_up(value):
    return int(math.floor(value + 0.5))


def _rgb_to_hsl(r, g, b):
    r, g, b = r / 255.0, g / 255.0, b / 255.0
    mx, mn = max(r, g, b), min(r, g, b)
    l = (mx + mn) / 2.0
    if mx == mn:
        return 0.0, 0.0, l
    d = mx - mn
    s = d / (2.0 - mx - mn) if l > 0.5 else d / (mx + mn)
    if mx == r:
        h = (g - b) / d + (6.0 if g < b else 0.0)
    elif mx == g:
        h = (b - r) / d + 2.0
    else:
        h = (r - g) / d + 4.0
    return h / 6.0, s, l


def _hue2rgb(p, q, t):
    if t < 0:
        t += 1
    if t > 1:
        t -= 1
    if t < 1 / 6:
        return p + (q - p) * 6 * t
    if t < 1 / 2:
        return q
    if t < 2 / 3:
        return p + (q - p) * (2 / 3 - t) * 6
    return p


def _tune(r, g, b):
    h, s, l = _rgb_to_hsl(r, g, b)
    if s < 0.08:
        return None
    s = min(0.85, s * 1.25)
    l = l * 0.45 + 0.286
    q = l * (1 + s) if l < 0.5 else l + s - l * s
    p = 2 * l - q
    rgb = [_hue2rgb(p, q, h + 1 / 3), _hue2rgb(p, q, h), _hue2rgb(p, q, h - 1 / 3)]
    return [_round_half_up(min(255, max(0, v * 255))) for v in rgb]


def _dominant_color(image):
    image = image.convert("RGBA").resize((16, 16), Image.Resampling.BICUBIC)
    sr = sg = sb = sw = 0.0
    ar = ag = ab = aw = 0.0
    raw = image.tobytes()
    for i in range(0, len(raw), 4):
        r, g, b, a = raw[i], raw[i + 1], raw[i + 2], raw[i + 3]
        if a < 16:
            continue
        chroma = max(r, g, b) - min(r, g, b)
        sr += r * chroma
        sg += g * chroma
        sb += b * chroma
        sw += chroma
        ar += r
        ag += g
        ab += b
        aw += 1
    if sw > 0:
        rgb = [sr / sw, sg / sw, sb / sw]
    elif aw > 0:
        rgb = [ar / aw, ag / aw, ab / aw]
    else:
        return None
    return _tune(rgb[0], rgb[1], rgb[2])


def _extract_urls(html):
    urls = set()
    # 首页卡片封面：<img class="post-bg" data-lazy-src="...">
    for tag in re.findall(r'<img\b[^>]*>', html, re.I):
        class_m = re.search(r'\bclass\s*=\s*"([^"]*)"', tag, re.I)
        if not class_m or 'post-bg' not in class_m.group(1).split():
            continue
        src_m = re.search(r'\bdata-lazy-src\s*=\s*"([^"]*)"', tag, re.I) or re.search(r'\bsrc\s*=\s*"([^"]*)"', tag, re.I)
        if src_m:
            urls.add(unescape(src_m.group(1)).strip().strip("'\""))
    # 文章头图：#page-header 的 inline background-image
    for tag in re.findall(r'<[^>]*id\s*=\s*"page-header"[^>]*>', html, re.I):
        style_m = re.search(r'\bstyle\s*=\s*"([^"]*)"', tag, re.I)
        if not style_m:
            continue
        bg_m = re.search(r'background(?:-image)?\s*:\s*url\(([^)]+)\)', unescape(style_m.group(1)), re.I)
        if bg_m:
            urls.add(bg_m.group(1).strip().strip("'\""))
    return urls


def _is_cover(url):
    if not url.lower().startswith(("http://", "https://")):
        return False
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc.lower() != R2_HOST:
        return False
    return os.path.splitext(parsed.path.lower())[1] in ('.jpg', '.jpeg', '.png')


def _thumb_url(url):
    path = urllib.parse.urlparse(url).path
    path = urllib.parse.unquote(path).lstrip('/')
    return THUMB_BASE + urllib.parse.quote(path, safe='/')


def _fetch_thumb(url):
    req = urllib.request.Request(_thumb_url(url), headers={
        'User-Agent': UA,
        'Accept': 'image/webp,image/avif,image/*,*/*',
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read()


def _collect_urls():
    urls = set()
    if not os.path.isdir(CN_PUBLIC):
        return urls
    for root, _dirs, files in os.walk(CN_PUBLIC):
        for name in files:
            if not name.lower().endswith(('.html', '.htm')):
                continue
            try:
                with open(os.path.join(root, name), 'r', encoding='utf-8', errors='ignore') as fp:
                    urls.update(_extract_urls(fp.read()))
            except OSError:
                pass
    return {u for u in urls if _is_cover(u)}


def _write_map(colors):
    payload = 'window.__COVER_COLORS=' + json.dumps(
        {k: colors[k] for k in sorted(colors)}, ensure_ascii=False, separators=(',', ':')
    ) + ';\n'
    targets = [
        os.path.join(CN_PUBLIC, 'js', 'cover-colors.js'),
        os.path.join(CN_PUBLIC, 'en', 'js', 'cover-colors.js'),
        os.path.join(EN_PUBLIC, 'js', 'cover-colors.js'),
    ]
    for path in targets:
        if not os.path.isdir(os.path.dirname(path)):
            continue
        with open(path, 'w', encoding='utf-8') as fp:
            fp.write(payload)
        print(f"[cover-colors] wrote {path}")


def main():
    urls = _collect_urls()
    print(f"[cover-colors] {len(urls)} cover URLs")
    colors = {}
    if Image is not None:
        for i, url in enumerate(sorted(urls), 1):
            try:
                data = _fetch_thumb(url)
                rgb = _dominant_color(Image.open(io.BytesIO(data)))
            except Exception as exc:  # 单张失败不影响构建
                print(f"[cover-colors] skip {url}: {exc}")
                continue
            # 近灰/无色封面也写入 null，运行时据此跳过采样，避免多下载一次
            colors[url] = rgb
            decoded = urllib.parse.unquote(url)
            if decoded != url:
                colors[decoded] = rgb
            if i % 20 == 0:
                print(f"[cover-colors] {i}/{len(urls)}")
    _write_map(colors)
    print(f"[cover-colors] done: {len(colors)} entries")
    return 0


if __name__ == '__main__':
    sys.exit(main())
