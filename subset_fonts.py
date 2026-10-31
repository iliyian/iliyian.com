#!/usr/bin/env python3
"""subset_fonts.py — 每次构建时，根据站点实际使用的字符生成精简字体

用法（在仓库根目录，先跑过 hexo generate）：
    python subset_fonts.py

流程：
1. 扫描 blogxyz/public、blogxyz-en/public 中渲染后的 HTML/XML 原始文本
   （包含内联脚本里的轮播诗句、文章标题、导航、meta 等）；
2. 从 HTML 的 #page-header / #nav / #site-info / #post-info 中提取“首屏标题区”
   字符，生成一个更小的 screen 字体，并通过 CSS 优先用于导航和页头；
3. 合并安全字符集（ASCII、CJK 标点、全角符号、带圈数字、Latin-1 补充等），
   为评论、Live2D、音乐播放器等动态内容兜底；
4. 用 fontTools 将原始字体（从 R2 下载到内存，不落盘）裁剪为两个 woff2：
   - LXGWWenKai-Medium.subset.woff2：全站静态内容
   - LXGWWenKai-Screen.woff2：页头/导航/文章标题等首屏文字
5. 输出到 blogxyz/source/fonts/（下次 hexo generate 自动拷入 public）和
   blogxyz/public/fonts/（本次构建的 public 已生成，直接覆盖使其立即可用）。
"""

import gzip
import io
import json
import os
import re
import sys
import urllib.request
from html.parser import HTMLParser

import brotli
from fontTools.subset import Options, Subsetter
from fontTools.ttLib import TTFont

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

FONT_SRC_URL = "https://r2-imgs.iliyian.com/css/LXGWWenKai-Medium.ttf"

# 输出文件名
GLOBAL_FONT_NAME = "LXGWWenKai-Medium.subset.woff2"
SCREEN_FONT_NAME = "LXGWWenKai-Screen.woff2"

# 扫描的文本来源目录（public 需先由 hexo generate 生成）
SCAN_DIRS = [
    os.path.join(BASE_DIR, "blogxyz", "public"),
    os.path.join(BASE_DIR, "blogxyz-en", "public"),
]

# 只扫描渲染后的 HTML/XML 原始文本；内联脚本会带入轮播诗句等动态文案
TEXT_EXTS = {".html", ".htm", ".xml"}

# 超过该大小的文件跳过（避免误读大二进制）
MAX_FILE_SIZE = 8 * 1024 * 1024

# 子集字体输出目录
OUTPUT_DIRS = [
    os.path.join(BASE_DIR, "blogxyz", "source", "fonts"),
    os.path.join(BASE_DIR, "blogxyz", "public", "fonts"),
]

# 首屏字体额外保留的标点（页头可能出现的动态日期/数字等）
SCREEN_SAFETY = set(chr(c) for c in range(0x20, 0x7F))
SCREEN_SAFETY.update("，。！？；：、“”‘’（）《》〈〉—…·")

# HTMLParser 中不会产生结束标签的元素
VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}


def build_safety_charset():
    """安全字符集：不来自站点文本，为评论、JS 注入等动态内容兜底。

    字体里没有的码点会被 subset 自动忽略，因此整块添加没有风险。
    """
    chars = set()
    chars.update(chr(c) for c in range(0x20, 0x7F))        # ASCII 可打印字符
    chars.update(chr(c) for c in range(0xA0, 0x100))       # Latin-1 补充（é ü 等）
    chars.update(chr(c) for c in range(0x2000, 0x2070))    # 通用标点（– — … ‘ ’ “ ” 等）
    chars.update(chr(c) for c in range(0x3000, 0x3040))    # CJK 标点（、。「」《》 等）
    chars.update(chr(c) for c in range(0x2460, 0x2500))    # 带圈数字 ①②③…
    chars.update(chr(c) for c in range(0x2600, 0x2700))    # 常用杂项符号（☀ ★ …）
    chars.update(chr(c) for c in range(0xFF00, 0xFFF0))    # 全角字符（，。！？（））
    return chars


def iter_public_files():
    """遍历两个站 public 下的 HTML/XML。"""
    for d in SCAN_DIRS:
        if not os.path.isdir(d):
            continue
        for root, _dirs, files in os.walk(d):
            for f in files:
                if os.path.splitext(f)[1].lower() in TEXT_EXTS:
                    yield os.path.join(root, f)


def collect_chars():
    """收集渲染后 public HTML/XML 原始文本中出现的字符。"""
    chars = set()
    for path in iter_public_files():
        try:
            if os.path.getsize(path) > MAX_FILE_SIZE:
                continue
            with open(path, "r", encoding="utf-8", errors="ignore") as fp:
                chars.update(fp.read())
        except OSError:
            pass
    return chars


class ScreenTextParser(HTMLParser):
    """收集 #page-header / #nav / #site-info / #post-info 内的文本。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.depth = 0
        self.parts = []

    def _is_screen_element(self, attrs):
        values = dict(attrs)
        ident = values.get("id", "")
        classes = values.get("class", "").split()
        return (
            ident in {"page-header", "nav", "site-info", "post-info"}
            or "site-name" in classes
            or "site-info" in classes
            or "post-info" in classes
        )

    def handle_starttag(self, tag, attrs):
        if tag in VOID_TAGS:
            return
        hit = self._is_screen_element(attrs)
        self.stack.append((tag, hit))
        if hit:
            self.depth += 1

    def handle_startendtag(self, tag, attrs):
        return

    def handle_endtag(self, tag):
        while self.stack:
            open_tag, hit = self.stack.pop()
            if hit:
                self.depth = max(0, self.depth - 1)
            if open_tag == tag:
                break

    def handle_data(self, data):
        if self.depth > 0 and data.strip():
            self.parts.append(data)


SUB_RE = re.compile(r"const\s+sub\s*=\s*(\[[\s\S]*?\])(?:\.slice\(\))?", re.MULTILINE)


def collect_screen_chars():
    """收集所有页头、导航、文章标题区和 typed.js 轮播诗句的字符。"""
    chars = set()
    for path in iter_public_files():
        try:
            if os.path.getsize(path) > MAX_FILE_SIZE:
                continue
            with open(path, "r", encoding="utf-8", errors="ignore") as fp:
                raw = fp.read()
        except OSError:
            continue

        parser = ScreenTextParser()
        parser.feed(raw)
        chars.update("".join(parser.parts))

        # 轮播诗句在 index.html 的内联 script 里，不在可见文本中
        for match in SUB_RE.finditer(raw):
            try:
                for line in json.loads(match.group(1)):
                    chars.update(line)
            except (TypeError, ValueError):
                pass

    return chars


def fetch_font(src_url):
    """下载原始字体到内存（不落盘），返回字节。"""
    print(f"  下载原始字体: {src_url}")
    try:
        # R2 对默认 urllib UA 返回 403，需要带浏览器 UA；带 Accept-Encoding 省流量
        req = urllib.request.Request(src_url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            "Accept-Encoding": "br, gzip",
        })
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = resp.read()
            enc = resp.headers.get("Content-Encoding", "").lower()
            if enc == "br":
                data = brotli.decompress(data)
            elif enc == "gzip":
                data = gzip.decompress(data)
        return data
    except Exception as e:
        print(f"  下载失败: {e}")
        sys.exit(1)


def subset_font(src_data, chars):
    """用 fontTools 裁剪字体，返回生成的 TTFont。"""
    opts = Options()
    opts.hinting = False                    # 去掉 hinting 指令，显著减小体积
    opts.layout_features = ["kern", "liga", "clig", "calt"]  # 只保留排版必需特性
    ss = Subsetter(options=opts)
    ss.populate(text="".join(sorted(chars)))
    font = TTFont(io.BytesIO(src_data))
    ss.subset(font)
    return font


def save_font(font, out_path):
    """按输出文件扩展名保存（.woff2 需 brotli 库）。"""
    if out_path.lower().endswith(".woff2"):
        font.flavor = "woff2"
    font.save(out_path)
    font.flavor = None


def build_font(src_data, chars, out_name):
    print(f"处理字体: {out_name}（{len(chars)} 个字符）")
    font = subset_font(src_data, chars)

    # 统计字体中缺失的字符（如 emoji），仅作提示
    cmap = font.getBestCmap()
    missing = [c for c in chars if ord(c) not in cmap]
    if missing:
        print(f"  字体中不存在的字符 {len(missing)} 个（已忽略），"
              f"如 {''.join(missing[:10])!r}")

    size = 0
    for out_dir in OUTPUT_DIRS:
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, out_name)
        save_font(font, out_path)
        size = os.path.getsize(out_path)
        print(f"  已生成: {out_path} ({size / 1024:.0f} KB)")
    return size, len(missing)


def main():
    if not any(os.path.isdir(d) for d in SCAN_DIRS) or not any(True for _ in iter_public_files()):
        print("未找到 public HTML/XML，请先运行 hexo generate 生成站点。")
        sys.exit(1)

    global_chars = collect_chars() | build_safety_charset()
    screen_chars = collect_screen_chars() | SCREEN_SAFETY

    print(f"全站字符: {len(global_chars)}（含安全字符集）")
    print(f"首屏字符: {len(screen_chars)}（含首屏标点兜底）")

    src = fetch_font(FONT_SRC_URL)
    src_kb = len(src) / 1024

    for chars, out_name in (
        (global_chars, GLOBAL_FONT_NAME),
        (screen_chars, SCREEN_FONT_NAME),
    ):
        size, missing = build_font(src, chars, out_name)
        print(f"  体积: {src_kb / 1024:.1f} MB -> {size / 1024:.0f} KB"
              + (f"，缺字 {missing}" if missing else "，无缺字"))


if __name__ == "__main__":
    main()
