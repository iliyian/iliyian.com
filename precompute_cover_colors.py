#!/usr/bin/env python3
"""预计算封面主色：扫描构建后的 HTML，用 128px 缩略图算色并生成 JS 映射。

构建流程中在 CN/EN 合并后调用；不缓存，每次构建重新计算。
"""
import argparse
import hashlib
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


def _srgb_linear(value):
    value /= 255.0
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def _bottom_tone(image, ratio=math.sqrt(2), sample=32):
    """按卡片封面 cover 裁切后底部 25% 的平均亮度判断用黑字还是白字。"""
    image = image.convert("RGBA")
    width, height = image.size
    if not width or not height:
        return None
    source_ratio = width / height
    if source_ratio > ratio:
        crop_w = height * ratio
        left = (width - crop_w) / 2
        box = (round(left), 0, round(left + crop_w), height)
    else:
        crop_h = width / ratio
        top = (height - crop_h) / 2
        box = (0, round(top), width, round(top + crop_h))
    image = image.crop(box).resize((sample, sample), Image.Resampling.BICUBIC)
    raw = image.tobytes()
    start = int(sample * 0.75) * sample * 4
    values = []
    for i in range(start, len(raw), 4):
        r, g, b, a = raw[i], raw[i + 1], raw[i + 2], raw[i + 3]
        if a < 16:
            continue
        values.append(
            0.2126 * _srgb_linear(r)
            + 0.7152 * _srgb_linear(g)
            + 0.0722 * _srgb_linear(b)
        )
    if not values:
        return None
    values.sort()
    return "light" if values[int(len(values) * 0.75)] >= 0.60 else "dark"


# 移动端卡片在 390px 视口下的文字区域（封面坐标归一化）
CARD_FONT_PX = 23.595          # CN 标题字号
CARD_FONT_PX_EN = 22.88         # EN 标题字号（系统字体）
CARD_PAD_X = 20.0
CARD_COVER_W = 372.0
CARD_COVER_H = 263.039
CARD_TITLE_BOTTOM = 0.7920
CARD_TITLE_LINE_H = 31.0
CARD_META_RECT = (0.5355, 0.8377, 0.4107, 0.0741)
CARD_META_RECT_EN = (0.5449, 0.8377, 0.4013, 0.0741)

TITLE_TEXT = {
    "white": (255, 255, 255, 1.0),
    "dark": (26, 26, 26, 1.0),
}
META_TEXT = {
    "white": (255, 255, 255, 0.85),
    "dark": (0, 0, 0, 0.72),
}


def _char_em_fallback(ch):
    """字体里缺字时的粗略宽度（em）：CJK 1，全角标点 1，拉丁按比例。"""
    code = ord(ch)
    if (0x2E80 <= code <= 0x9FFF or 0x3040 <= code <= 0x30FF
            or 0xF900 <= code <= 0xFAFF or 0xFF00 <= code <= 0xFFEF
            or 0x3000 <= code <= 0x303F):
        return 1.0
    if ch == " ":
        return 0.5
    if ch in ".·…":
        return 0.35
    if ch.isupper():
        return 0.59
    if ch.isdigit():
        return 0.55
    if ch.isalpha():
        return 0.52
    return 0.55


_TITLE_FONT_PATH = os.path.join(BASE_DIR, "blogxyz", "source", "fonts", "LXGWWenKai-Medium.subset.woff2")
_title_advance = None
_title_font_loaded = False


def _load_title_font():
    """读构建产物里的标题字体，返回按像素取 advance 的函数；失败返回 None。"""
    global _title_advance, _title_font_loaded
    if _title_font_loaded:
        return _title_advance
    _title_font_loaded = True
    try:
        from fontTools.ttLib import TTFont
        font = TTFont(_TITLE_FONT_PATH)
        cmap = font.getBestCmap()
        upm = font["head"].unitsPerEm
        metrics = font["hmtx"].metrics

        def advance_px(ch):
            glyph = cmap.get(ord(ch))
            if not glyph:
                return None
            advance = metrics.get(glyph)
            return advance[0] * CARD_FONT_PX / upm if advance else None

        _title_advance = advance_px
    except Exception:
        _title_advance = None
    return _title_advance


def _title_width_px(title, use_font=True):
    advance = _load_title_font() if use_font else None
    base_px = CARD_FONT_PX if use_font else CARD_FONT_PX_EN
    total = 0.0
    for ch in title:
        px = advance(ch) if advance else None
        total += px if px is not None else _char_em_fallback(ch) * base_px
    return total


def _title_rect(title, use_font=True):
    """标题右对齐、右边界固定；CN 用字体 advance，EN 用字符比例估算。"""
    avail = CARD_COVER_W - 2 * CARD_PAD_X
    width = min(_title_width_px(title, use_font), avail)
    lines = min(2, max(1, math.ceil(width / avail)))
    right = (CARD_COVER_W - CARD_PAD_X) / CARD_COVER_W
    height = lines * CARD_TITLE_LINE_H / CARD_COVER_H
    return (
        right - width / CARD_COVER_W,
        CARD_TITLE_BOTTOM - height,
        width / CARD_COVER_W,
        height,
    )


def _cover_crop(image, ratio=math.sqrt(2)):
    image = image.convert("RGBA")
    width, height = image.size
    if not width or not height:
        return None
    source_ratio = width / height
    if source_ratio > ratio:
        crop_w = height * ratio
        left = (width - crop_w) / 2
        box = (round(left), 0, round(left + crop_w), height)
    else:
        crop_h = width / ratio
        top = (height - crop_h) / 2
        box = (0, round(top), width, round(top + crop_h))
    return image.crop(box)


def _region_pixels(cropped, rect):
    width, height = cropped.size
    x0 = max(0, int(round(rect[0] * width)))
    y0 = max(0, int(round(rect[1] * height)))
    x1 = min(width, int(round((rect[0] + rect[2]) * width)))
    y1 = min(height, int(round((rect[1] + rect[3]) * height)))
    pixels = []
    load = cropped.load()
    for y in range(y0, y1):
        for x in range(x0, x1):
            r, g, b, a = load[x, y]
            if a >= 16:
                pixels.append((r, g, b))
    return pixels


def _contrast_l(l1, l2):
    hi, lo = (l1, l2) if l1 >= l2 else (l2, l1)
    return (hi + 0.05) / (lo + 0.05)


def _tone_for_text(pixels, white, dark):
    """按区域内像素的中位对比度，在白字和深字之间选更优的一侧。"""
    if not pixels:
        return None
    white_cr = []
    dark_cr = []
    for r, g, b in pixels:
        bg = 0.2126 * _srgb_linear(r) + 0.7152 * _srgb_linear(g) + 0.0722 * _srgb_linear(b)
        for spec, out in ((white, white_cr), (dark, dark_cr)):
            sr, sg, sb, alpha = spec
            er = alpha * sr + (1 - alpha) * r
            eg = alpha * sg + (1 - alpha) * g
            eb = alpha * sb + (1 - alpha) * b
            fg = 0.2126 * _srgb_linear(er) + 0.7152 * _srgb_linear(eg) + 0.0722 * _srgb_linear(eb)
            out.append(_contrast_l(fg, bg))
    white_cr.sort()
    dark_cr.sort()
    return "light" if dark_cr[len(dark_cr) // 2] >= white_cr[len(white_cr) // 2] else "dark"


def _text_tones(image, title, use_font=True):
    """标题按估算行盒、日期按固定行盒，各自选择字色。"""
    cropped = _cover_crop(image)
    if cropped is None:
        return None
    tones = {}
    title_tone = _tone_for_text(
        _region_pixels(cropped, _title_rect(title, use_font)),
        TITLE_TEXT["white"], TITLE_TEXT["dark"])
    meta_rect = CARD_META_RECT if use_font else CARD_META_RECT_EN
    meta_tone = _tone_for_text(_region_pixels(cropped, meta_rect), META_TEXT["white"], META_TEXT["dark"])
    if title_tone:
        tones["title"] = title_tone
    if meta_tone:
        tones["meta"] = meta_tone
    return tones or None


def _cards_from_html(html):
    """遍历首页卡片，产出 (封面 URL, 标题文字)。"""
    starts = [m.start() for m in re.finditer(r'<div class="recent-post-item"', html)]
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else min(len(html), start + 20000)
        block = html[start:end]
        cover_m = re.search(r'data-cover-src="([^"]*)"', block)
        title_m = re.search(r'<a[^>]*class="article-title"[^>]*>(.*?)</a>', block, re.S)
        if not cover_m or not title_m:
            continue
        inner = re.sub(r'<span[^>]*class="sticky"[^>]*>.*?</span>', '', title_m.group(1), flags=re.S)
        title = re.sub(r'<[^>]+>', '', inner)
        title = re.sub(r'\s+', ' ', unescape(title)).strip()
        url = unescape(cover_m.group(1)).strip("'\"")
        if title and url:
            yield url, title


def _collect_card_titles(site_root, skip_en=False):
    """从构建后的 HTML 里取首页卡片的封面 URL 和标题文字。"""
    titles = {}
    if not os.path.isdir(site_root):
        return titles
    en_root = os.path.abspath(os.path.join(site_root, "en")) if skip_en else None
    for root, _dirs, files in os.walk(site_root):
        if en_root and os.path.abspath(root).startswith(en_root):
            continue
        for name in files:
            if not name.lower().endswith((".html", ".htm")):
                continue
            try:
                with open(os.path.join(root, name), "r", encoding="utf-8", errors="ignore") as fp:
                    html = fp.read()
            except OSError:
                continue
            for url, title in _cards_from_html(html):
                titles.setdefault(url, title)
    return titles


def _dev_main(url):
    """轻量预览：抓运行中的 Hexo 首页，只写 source/js/cover-colors.js。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as resp:
        html = resp.read().decode("utf-8", errors="ignore")
    text_tones = {}
    seen = set()
    for cover, title in _cards_from_html(html):
        if cover in seen:
            continue
        seen.add(cover)
        try:
            data = _fetch_thumb(cover)
            image = Image.open(io.BytesIO(data))
            tones = _text_tones(image, title)
        except Exception as exc:
            print(f"[cover-colors] skip {cover}: {exc}")
            continue
        if tones:
            text_tones[cover] = tones
    out = os.path.join(BASE_DIR, "blogxyz", "source", "js", "cover-colors.js")
    payload = "window.__COVER_TEXT_TONES=" + json.dumps(
        {k: text_tones[k] for k in sorted(text_tones)}, ensure_ascii=False, separators=(",", ":")
    ) + ";\n"
    with open(out, "w", encoding="utf-8") as fp:
        fp.write(payload)
    print(f"[cover-colors] dev: wrote {len(text_tones)} text tones to {out}")
    print("[cover-colors] dev: restore the placeholder with "
          "`git -C blogxyz checkout -- source/js/cover-colors.js` when done")
    return 0


def _extract_urls(html):
    urls = set()
    # filter 会在卡片/头图上写入原始封面 URL
    for m in re.finditer(r'\bdata-cover-src\s*=\s*"([^"]*)"', html, re.I):
        urls.add(unescape(m.group(1)).strip().strip("'\""))
    # 首页卡片封面
    for tag in re.findall(r'<img\b[^>]*>', html, re.I):
        class_m = re.search(r'\bclass\s*=\s*"([^"]*)"', tag, re.I)
        if not class_m or 'post-bg' not in class_m.group(1).split():
            continue
        src_m = re.search(r'\bdata-cover-src\s*=\s*"([^"]*)"', tag, re.I) or re.search(r'\bdata-lazy-src\s*=\s*"([^"]*)"', tag, re.I) or re.search(r'\bsrc\s*=\s*"([^"]*)"', tag, re.I)
        if src_m:
            urls.add(unescape(src_m.group(1)).strip().strip("'\""))
    # 文章头图
    for tag in re.findall(r'<[^>]*id\s*=\s*"page-header"[^>]*>', html, re.I):
        cover_m = re.search(r'\bdata-cover-src\s*=\s*"([^"]*)"', tag, re.I)
        if cover_m:
            urls.add(unescape(cover_m.group(1)).strip().strip("'\""))
            continue
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


def _write_map(colors, tones, text_tones_cn, text_tones_en):
    def payload(text_tones):
        out = 'window.__COVER_COLORS=' + json.dumps(
            {k: colors[k] for k in sorted(colors)}, ensure_ascii=False, separators=(',', ':')
        ) + ';\n'
        if tones:
            out += 'window.__COVER_TONES=' + json.dumps(
                {k: tones[k] for k in sorted(tones)}, ensure_ascii=False, separators=(',', ':')
            ) + ';\n'
        if text_tones:
            out += 'window.__COVER_TEXT_TONES=' + json.dumps(
                {k: text_tones[k] for k in sorted(text_tones)}, ensure_ascii=False, separators=(',', ':')
            ) + ';\n'
        return out

    targets = [
        (os.path.join(CN_PUBLIC, 'js', 'cover-colors.js'), text_tones_cn),
        (os.path.join(CN_PUBLIC, 'en', 'js', 'cover-colors.js'), text_tones_en),
        (os.path.join(EN_PUBLIC, 'js', 'cover-colors.js'), text_tones_en),
    ]
    for path, text_tones in targets:
        if not os.path.isdir(os.path.dirname(path)):
            continue
        with open(path, 'w', encoding='utf-8') as fp:
            fp.write(payload(text_tones))
        print(f"[cover-colors] wrote {path}")


def _version_cover_colors_urls(root):
    """给构建产物里的 /js/cover-colors.js 追加 ?v=<hash>。

    这个文件每次构建都会变（主色 + 明暗表），但文件名固定；不更新查询串
    的话，浏览器/边缘节点会按 max-age 继续用旧文件——线上曾因此出现
    “明暗表已部署、页面却还在用白字”的问题。重复执行结果不变。
    """
    asset = os.path.join(CN_PUBLIC, 'js', 'cover-colors.js')
    if not os.path.isfile(asset):
        return 0
    digest = hashlib.sha256()
    with open(asset, 'rb') as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b''):
            digest.update(chunk)
    url = f"/js/cover-colors.js?v={digest.hexdigest()[:8]}"
    pattern = re.compile(r"/js/cover-colors\.js(?:\?v=[0-9a-f]+)?")
    changed = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for filename in filenames:
            if not filename.lower().endswith('.html'):
                continue
            path = os.path.join(dirpath, filename)
            try:
                with open(path, 'r', encoding='utf-8', newline='') as fp:
                    text = fp.read()
            except (OSError, UnicodeDecodeError):
                continue
            new_text = pattern.sub(url, text)
            if new_text != text:
                with open(path, 'w', encoding='utf-8', newline='') as fp:
                    fp.write(new_text)
                changed += 1
    return changed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dev",
        nargs="?",
        const="http://localhost:4000/",
        metavar="URL",
        help="generate blogxyz/source/js/cover-colors.js from a running dev server",
    )
    args = parser.parse_args()
    if args.dev:
        if Image is None:
            print("[cover-colors] Pillow not installed; cannot compute text tones")
            return 1
        return _dev_main(args.dev)

    urls = _collect_urls()
    print(f"[cover-colors] {len(urls)} cover URLs")
    colors = {}
    tones = {}
    text_tones_cn = {}
    text_tones_en = {}
    titles_cn = _collect_card_titles(CN_PUBLIC, skip_en=True)
    titles_en = _collect_card_titles(EN_PUBLIC)
    if Image is not None:
        for i, url in enumerate(sorted(urls), 1):
            try:
                data = _fetch_thumb(url)
                image = Image.open(io.BytesIO(data))
                rgb = _dominant_color(image)
                tone = _bottom_tone(image)
                title_cn = titles_cn.get(url)
                title_en = titles_en.get(url)
                per_cn = _text_tones(image, title_cn, use_font=True) if title_cn else None
                per_en = _text_tones(image, title_en, use_font=False) if title_en else None
            except Exception as exc:  # 单张失败不影响构建
                print(f"[cover-colors] skip {url}: {exc}")
                continue
            # 近灰/无色封面也写入 null，运行时据此跳过采样，避免多下载一次
            colors[url] = rgb
            decoded = urllib.parse.unquote(url)
            if decoded != url:
                colors[decoded] = rgb
            if tone:
                tones[url] = tone
                if decoded != url:
                    tones[decoded] = tone
            if per_cn:
                text_tones_cn[url] = per_cn
                if decoded != url:
                    text_tones_cn[decoded] = per_cn
            if per_en:
                text_tones_en[url] = per_en
                if decoded != url:
                    text_tones_en[decoded] = per_en
            if i % 20 == 0:
                print(f"[cover-colors] {i}/{len(urls)}")
    _write_map(colors, tones, text_tones_cn, text_tones_en)
    changed = _version_cover_colors_urls(CN_PUBLIC)
    changed += _version_cover_colors_urls(EN_PUBLIC)
    print(f"[cover-colors] versioned cover-colors.js in {changed} HTML files")
    print(f"[cover-colors] done: {len(colors)} colors, {len(tones)} tones, "
          f"{len(text_tones_cn)} cn text tones, {len(text_tones_en)} en text tones")
    return 0


if __name__ == '__main__':
    sys.exit(main())
