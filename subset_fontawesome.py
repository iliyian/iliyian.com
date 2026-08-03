#!/usr/bin/env python3
"""FontAwesome 子集化：动态扫描站点用到的 fa- 图标，生成最小 css + woff2 字体。

用法：
  python subset_fontawesome.py              # 扫描 blogxyz + blogxyz-en 并生成子集（构建前）
  python subset_fontawesome.py --verify     # 校验构建产物，发现缺失 glyph 报错（构建后）

原理：
  - 扫描站点源码（模板 pug / js / md / yml / html）提取 fa-xxx class
  - 与 FA css 的 codepoint 映射比对，过滤修饰类（fa-solid、fa-spin 等无 codepoint 自动排除）
  - 按字体归属（solid/brands/regular）子集化 woff2，生成精简 css
  - 新增图标无需改任何配置，下次构建自动包含
"""
import argparse
import os
import re
import subprocess
import sys
import urllib.request

FA_VERSION = '7.0.1'
CDN = f'https://cdn.jsdelivr.net/npm/@fortawesome/fontawesome-free@{FA_VERSION}'
FONT_SRCS = {
    'solid': 'fa-solid-900.woff2',
    'brands': 'fa-brands-400.woff2',
    'regular': 'fa-regular-400.woff2',
}
SUBSET_OUT = {
    'solid': 'fa-solid-subset.woff2',
    'brands': 'fa-brands-subset.woff2',
    'regular': 'fa-regular-subset.woff2',
}
CACHE_DIR = os.path.join(os.path.expanduser('~'), '.cache', f'fontawesome-free-{FA_VERSION}')
SITES = ['blogxyz', 'blogxyz-en']
SCAN_EXCLUDE_DIRS = {'node_modules', 'public', '.git', 'db.json'}
SCAN_EXTS = {'.pug', '.js', '.md', '.css', '.yml', '.yaml', '.html'}


def ensure_deps():
    try:
        import brotli  # noqa: F401
        import fontTools  # noqa: F401
    except ImportError:
        print('Installing fonttools, brotli...')
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-q', 'fonttools', 'brotli'])


def fetch_fa_source():
    """Download FA css + fonts if not cached. Returns (css_path, {style: font_path})."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    css_path = os.path.join(CACHE_DIR, 'all.min.css')
    if not os.path.exists(css_path):
        print(f'Downloading FontAwesome {FA_VERSION} from CDN...')
        if not os.path.exists(css_path):
            urllib.request.urlretrieve(f'{CDN}/css/all.min.css', css_path)
        for name in FONT_SRCS.values():
            dst = os.path.join(CACHE_DIR, name)
            if not os.path.exists(dst):
                urllib.request.urlretrieve(f'{CDN}/webfonts/{name}', dst)
    return css_path, {k: os.path.join(CACHE_DIR, v) for k, v in FONT_SRCS.items()}


def scan_icons(site_path):
    """Scan site source for all fa-xxx class names."""
    icons = set()
    pattern = re.compile(r'\bfa-[a-z0-9-]+\b')
    for root, dirs, files in os.walk(site_path):
        dirs[:] = [d for d in dirs if d not in SCAN_EXCLUDE_DIRS]
        for f in files:
            if not f.endswith(tuple(SCAN_EXTS)) or f == 'package-lock.json':
                continue
            if f == 'fontawesome-subset.css':
                continue  # our own output — aliases in it would snowball the icon set
            try:
                text = open(os.path.join(root, f), encoding='utf-8').read()
            except (UnicodeDecodeError, OSError):
                continue
            icons.update(pattern.findall(text))
    return icons


def split_css_blocks(css_text):
    """Brace-balanced split of CSS into (selector, rule_body) blocks."""
    blocks, sel_start, depth, rule_start = [], 0, 0, 0
    for i, ch in enumerate(css_text):
        if ch == '{':
            if depth == 0:
                sel = css_text[sel_start:i].strip()
                rule_start = i + 1
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                blocks.append((sel, css_text[rule_start:i]))
                sel_start = i + 1
    return blocks


def parse_icon_rules(css_text):
    """class name -> codepoint hex (alias-combined rules handled)."""
    rules = {}
    for sel, rule in split_css_blocks(css_text):
        m = re.search(r'--fa:"\\([0-9a-f]+)"', rule)
        if m:
            for cls in re.findall(r'\.(fa-[a-z0-9-]+)', sel):
                rules[cls] = m.group(1)
    return rules


def map_to_fonts(icons, rules, font_paths):
    """Group used icons by font file, verifying each codepoint exists in its font."""
    from fontTools.ttLib import TTFont
    cmaps = {k: TTFont(v).getBestCmap() for k, v in font_paths.items()}
    by_font = {'solid': {}, 'brands': {}, 'regular': {}}
    unknown = []
    for cls in sorted(icons):
        cp = rules.get(cls)
        if cp is None:
            continue  # modifier classes (fa-spin, fa-fw...) or unknown
        cp_int = int(cp, 16)
        for k, cmap in cmaps.items():
            if cp_int in cmap:
                by_font[k][cls] = cp
                break
        else:
            unknown.append(cls)
    if unknown:
        print(f'  WARNING: {len(unknown)} fa- classes not found in FA fonts: {", ".join(unknown)}')
    return by_font


def subset_font(src, cps, out_path):
    from fontTools import subset as ftsubset
    options = ftsubset.Options()
    options.flavor = 'woff2'
    options.layout_features = ['*']
    options.name_IDs = ['*']
    options.hinting = False
    options.desubroutinize = True
    font = ftsubset.load_font(src, options)
    subsetter = ftsubset.Subsetter(options)
    subsetter.populate(unicodes=[int(c, 16) for c in cps])
    subsetter.subset(font)
    ftsubset.save_font(font, out_path, options)


def gen_css(css_text, by_font, src_names):
    """Keep functional rules + used icon rules; rewrite @font-face to subset files."""
    used = set(c for m in by_font.values() for c in m.keys())
    has_file = {k for k, v in by_font.items() if v}  # styles that actually got a subset file
    out = []
    for sel, rule in split_css_blocks(css_text):
        if not sel:
            continue
        if sel.startswith('@font-face'):
            m = re.search(r'url\(([^)]*)\)', rule)
            if not m or 'v4compatibility' in m.group(1):
                continue  # drop v4-compat font-face (we don't ship that font)
            for style, src in src_names.items():
                if m.group(1).endswith(src):
                    if style in has_file:
                        new_rule = rule.replace(m.group(1), f'../fonts/{SUBSET_OUT[style]}')
                        out.append(sel + '{' + new_rule + '}')
                    break
            continue
        if sel.startswith('@'):
            out.append(sel + '{' + rule + '}')
            continue
        if '--fa:' in rule:
            classes = set(re.findall(r'\.(fa-[a-z0-9-]+)', sel))
            if classes & used:
                out.append(sel + '{' + rule + '}')
        else:
            out.append(sel + '{' + rule + '}')
    return '\n'.join(out)


def process_site(site_path, css_path, font_paths):
    print(f'== Scanning {os.path.basename(site_path)} ==')
    icons = scan_icons(site_path)
    print(f'  found {len(icons)} fa- classes in source')
    rules = parse_icon_rules(open(css_path, encoding='utf-8').read())
    by_font = map_to_fonts(icons, rules, font_paths)
    used = sum(len(v) for v in by_font.values())
    print(f'  {used} real icons: ' + ', '.join(sorted(c for m in by_font.values() for c in m.keys())))

    source = os.path.join(site_path, 'source')
    css_out = os.path.join(source, 'css', 'fontawesome-subset.css')
    fonts_dir = os.path.join(source, 'fonts')
    os.makedirs(fonts_dir, exist_ok=True)

    for style, classes in by_font.items():
        out_path = os.path.join(fonts_dir, SUBSET_OUT[style])
        if classes:
            subset_font(font_paths[style], list(classes.values()), out_path)
            size = os.path.getsize(out_path) / 1024
            print(f'  {SUBSET_OUT[style]}: {size:.1f} KB ({len(classes)} glyphs)')
        elif os.path.exists(out_path):
            os.remove(out_path)  # no longer needed

    css_text = gen_css(open(css_path, encoding='utf-8').read(), by_font, FONT_SRCS)
    open(css_out, 'w', encoding='utf-8').write(css_text)
    print(f'  {os.path.relpath(css_out, site_path)}: {len(css_text)/1024:.1f} KB')
    return by_font, os.path.join(fonts_dir, 'fa-solid-subset.woff2'), \
           os.path.join(fonts_dir, 'fa-brands-subset.woff2')


def verify_site(site_path, rules, solid_font, brands_font):
    """Scan built public/ HTML; report icons missing from subset fonts."""
    from fontTools.ttLib import TTFont
    cmap_solid = TTFont(solid_font).getBestCmap()
    cmap_brands = TTFont(brands_font).getBestCmap()
    missing = set()
    pattern = re.compile(r'\bfa-[a-z0-9-]+\b')
    public = os.path.join(site_path, 'public')
    if not os.path.isdir(public):
        print(f'  WARNING: {public} not found, skip verify')
        return
    for root, dirs, files in os.walk(public):
        if 'en' in root and site_path.endswith('blogxyz'):
            dirs[:] = []
            continue  # en copy inside zh public is verified under blogxyz-en
        for f in files:
            if not f.endswith('.html'):
                continue
            try:
                text = open(os.path.join(root, f), encoding='utf-8').read()
            except (UnicodeDecodeError, OSError):
                continue
            for cls in pattern.findall(text):
                cp = rules.get(cls)
                if cp is None:
                    continue
                cp_int = int(cp, 16)
                if cp_int not in cmap_solid and cp_int not in cmap_brands:
                    missing.add(cls)
    if missing:
        print(f'  ERROR: {len(missing)} icons used in built HTML missing from subset: '
              + ', '.join(sorted(missing)))
        print('  Re-run `python subset_fontawesome.py` after adding them.')
        return 1
    print(f'  verify OK: all icons in built HTML have glyphs')
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    base = os.path.dirname(os.path.abspath(__file__))
    ensure_deps()
    css_path, font_paths = fetch_fa_source()
    rules = parse_icon_rules(open(css_path, encoding='utf-8').read())
    rc = 0
    for site in SITES:
        site_path = os.path.join(base, site)
        if args.verify:
            solid = os.path.join(site_path, 'source', 'fonts', SUBSET_OUT['solid'])
            brands = os.path.join(site_path, 'source', 'fonts', SUBSET_OUT['brands'])
            if os.path.exists(solid) and os.path.exists(brands):
                rc |= verify_site(site_path, rules, solid, brands)
            else:
                print(f'{site}: subset fonts missing, run without --verify first')
                rc = 1
        else:
            process_site(site_path, css_path, font_paths)
    sys.exit(rc)


if __name__ == '__main__':
    main()
