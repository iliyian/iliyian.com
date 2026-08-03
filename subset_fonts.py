#!/usr/bin/env python3
"""subset_fonts.py — 每次构建时，根据站点实际使用的字符生成精简字体

用法（在仓库根目录）：
    python subset_fonts.py

流程：
1. 扫描 blogxyz/public、blogxyz-en/public 中渲染后的 HTML/XML/JSON，以及两个站的
   源码 _posts / _data / _config.yml，收集所有实际出现的字符；
2. 合并安全字符集（ASCII、CJK 标点、全角符号、带圈数字、Latin-1 补充等），
   为评论等 JS 动态内容兜底；
3. 用 fontTools 将原始字体（从 R2 下载到内存，不落盘）裁剪为仅含这些字符的
   woff2 子集字体；
4. 输出到 blogxyz/source/fonts/（下次 hexo generate 自动拷入 public）和
   blogxyz/public/fonts/（本次构建的 public 已生成，直接覆盖使其立即可用）。
"""

import gzip
import io
import os
import sys
import urllib.request

import brotli
from fontTools.subset import Options, Subsetter
from fontTools.ttLib import TTFont

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 需要裁剪的字体：src_url 为原始字体地址，out_name 为输出文件名
FONTS = [
    {
        "src_url": "https://r2-imgs.iliyian.com/css/LXGWWenKai-Medium.ttf",
        "out_name": "LXGWWenKai-Medium.subset.woff2",
    },
]

# 扫描的文本来源目录（public 需先由 hexo generate 生成）
SCAN_DIRS = [
    os.path.join(BASE_DIR, "blogxyz", "public"),
    os.path.join(BASE_DIR, "blogxyz-en", "public"),
    os.path.join(BASE_DIR, "blogxyz", "source", "_posts"),
    os.path.join(BASE_DIR, "blogxyz-en", "source", "_posts"),
    os.path.join(BASE_DIR, "blogxyz", "source", "_data"),
    os.path.join(BASE_DIR, "blogxyz-en", "source", "_data"),
]

# 额外的文本文件（配置等）
SCAN_FILES = [
    os.path.join(BASE_DIR, "blogxyz", "_config.yml"),
    os.path.join(BASE_DIR, "blogxyz-en", "_config.yml"),
]

# 文本文件扩展名；其余（字体/图片等二进制）一律跳过
TEXT_EXTS = {
    ".html", ".htm", ".xml", ".json", ".js", ".css",
    ".md", ".yml", ".yaml", ".txt", ".svg", ".ejs",
}

# 超过该大小的文件跳过（避免误读大二进制文件）
MAX_FILE_SIZE = 8 * 1024 * 1024

# 子集字体输出目录
OUTPUT_DIRS = [
    os.path.join(BASE_DIR, "blogxyz", "source", "fonts"),
    os.path.join(BASE_DIR, "blogxyz", "public", "fonts"),
]


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


def collect_chars():
    """收集所有扫描来源中出现过的字符（含中文字符、标题、标签、主题 UI 文案等）。"""
    chars = set()

    def read_text(path):
        try:
            if os.path.getsize(path) > MAX_FILE_SIZE:
                return
            with open(path, "r", encoding="utf-8", errors="ignore") as fp:
                chars.update(fp.read())
        except OSError:
            pass

    for d in SCAN_DIRS:
        if not os.path.isdir(d):
            continue
        for root, _dirs, files in os.walk(d):
            for f in files:
                if os.path.splitext(f)[1].lower() in TEXT_EXTS:
                    read_text(os.path.join(root, f))

    for f in SCAN_FILES:
        if os.path.isfile(f):
            read_text(f)

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


def subset_font(src_data, text):
    """用 fontTools 裁剪字体，返回生成的 TTFont。"""
    opts = Options()
    opts.hinting = False                    # 去掉 hinting 指令，显著减小体积
    opts.layout_features = ["kern", "liga", "clig", "calt"]  # 只保留排版必需特性
    ss = Subsetter(options=opts)
    ss.populate(text="".join(text))
    font = TTFont(io.BytesIO(src_data))
    ss.subset(font)
    return font


def save_font(font, out_path):
    """按输出文件扩展名保存（.woff2 需 brotli 库）。"""
    if out_path.lower().endswith(".woff2"):
        font.flavor = "woff2"
    font.save(out_path)
    font.flavor = None


def main():
    chars = collect_chars() | build_safety_charset()
    print(f"收集到 {len(chars)} 个字符（含安全字符集）")

    for info in FONTS:
        out_name = info["out_name"]
        print(f"处理字体: {out_name}")
        src = fetch_font(info["src_url"])
        src_size = len(src)

        font = subset_font(src, chars)

        # 统计字体中缺失的字符（如 emoji），仅作提示
        cmap = font.getBestCmap()
        missing = [c for c in chars if ord(c) not in cmap]
        if missing:
            print(f"  字体中不存在的字符 {len(missing)} 个（已忽略），"
                  f"如 {''.join(missing[:10])!r}")

        # 输出到 source/fonts 与 public/fonts
        for out_dir in OUTPUT_DIRS:
            os.makedirs(out_dir, exist_ok=True)
            out_path = os.path.join(out_dir, out_name)
            save_font(font, out_path)
            print(f"  已生成: {out_path} ({os.path.getsize(out_path) / 1024:.0f} KB)")

        print(f"  体积: {src_size / 1024 / 1024:.1f} MB -> "
              f"{os.path.getsize(os.path.join(OUTPUT_DIRS[0], out_name)) / 1024:.0f} KB")


if __name__ == "__main__":
    main()
