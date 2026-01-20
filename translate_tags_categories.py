#!/usr/bin/env python3
"""
脚本功能：
1. 扫描 blogxyz-en/source/_posts 目录下所有 md 文件
2. 提取所有 tags 和 categories
3. 将中文翻译成英文
4. 批量替换
"""

import os
import re
from pathlib import Path

# 中英文翻译映射表
TRANSLATION_MAP = {
    # Tags
    "音乐": "Music",
    "自叙": "Autobiography",
    "法学": "Law",
    "法理学": "Jurisprudence",
    "学习": "Study",
    "生物": "Biology",
    "算法": "Algorithm",
    "文学": "Literature",
    "哲学": "Philosophy",
    "宪法学": "Constitutional Law",
    "导数": "Derivatives",
    "数学": "Mathematics",
    "无意义": "Meaningless",
    "消极": "Negative",
    "讽刺": "Satire",
    "读书报告": "Book Report",
    "随笔": "Essay",
    "ICPC": "ICPC",  # 保持不变
    
    # Categories
    "无用之谈": "Idle Talk",
}

def extract_frontmatter(content):
    """提取 frontmatter 内容"""
    match = re.match(r'^---\n(.*?)\n---', content, re.DOTALL)
    if match:
        return match.group(1), match.start(), match.end()
    return None, None, None

def parse_tags_categories(frontmatter_str):
    """解析 frontmatter 中的 tags 和 categories"""
    result = {'tags': [], 'categories': []}
    
    lines = frontmatter_str.split('\n')
    current_field = None
    
    for line in lines:
        # 检查是否是 tags: 或 categories: 开头
        if line.startswith('tags:'):
            current_field = 'tags'
            # 检查是否是单行格式 tags: xxx
            value = line[5:].strip()
            if value:
                result['tags'] = [value]
                current_field = None
        elif line.startswith('categories:'):
            current_field = 'categories'
            # 检查是否是单行格式 categories: xxx
            value = line[11:].strip()
            if value:
                result['categories'] = [value]
                current_field = None
        elif current_field and line.strip().startswith('- '):
            # 列表项
            item = line.strip()[2:].strip()
            result[current_field].append(item)
        elif current_field and not line.strip().startswith('-') and line.strip() and not line.startswith(' '):
            # 遇到新字段，结束当前字段
            current_field = None
    
    return result

def collect_all_tags_categories(posts_dir):
    """收集所有文件中的 tags 和 categories"""
    all_tags = set()
    all_categories = set()
    
    for md_file in Path(posts_dir).glob("*.md"):
        with open(md_file, 'r', encoding='utf-8') as f:
            content = f.read()
        
        frontmatter_str, _, _ = extract_frontmatter(content)
        if frontmatter_str:
            data = parse_tags_categories(frontmatter_str)
            all_tags.update(data['tags'])
            all_categories.update(data['categories'])
    
    return all_tags, all_categories

def translate_in_frontmatter(frontmatter_str):
    """在 frontmatter 中翻译 tags 和 categories"""
    lines = frontmatter_str.split('\n')
    new_lines = []
    modified = False
    
    for line in lines:
        new_line = line
        
        # 检查是否是单行格式 tags: xxx 或 categories: xxx
        for prefix in ['tags:', 'categories:']:
            if line.startswith(prefix):
                value = line[len(prefix):].strip()
                if value and value in TRANSLATION_MAP:
                    new_line = f"{prefix} {TRANSLATION_MAP[value]}"
                    modified = True
                break
        
        # 检查是否是列表项 - xxx
        if line.strip().startswith('- '):
            # 提取缩进
            indent = len(line) - len(line.lstrip())
            item = line.strip()[2:].strip()
            if item in TRANSLATION_MAP:
                new_line = ' ' * indent + '- ' + TRANSLATION_MAP[item]
                modified = True
        
        new_lines.append(new_line)
    
    return '\n'.join(new_lines), modified

def process_file(file_path, dry_run=False):
    """处理单个文件"""
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    frontmatter_str, start, end = extract_frontmatter(content)
    if not frontmatter_str:
        print(f"  跳过 (无 frontmatter): {file_path}")
        return False
    
    # 翻译 frontmatter
    new_frontmatter, modified = translate_in_frontmatter(frontmatter_str)
    
    if not modified:
        return False
    
    # 显示变化
    data_before = parse_tags_categories(frontmatter_str)
    data_after = parse_tags_categories(new_frontmatter)
    
    if data_before['tags'] != data_after['tags']:
        print(f"  tags: {data_before['tags']} -> {data_after['tags']}")
    if data_before['categories'] != data_after['categories']:
        print(f"  categories: {data_before['categories']} -> {data_after['categories']}")
    
    if dry_run:
        return True
    
    # 重建文件内容
    new_content = f"---\n{new_frontmatter}\n---{content[end:]}"
    
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(new_content)
    
    return True

def main():
    # 设置路径
    script_dir = Path(__file__).parent
    posts_dir = script_dir / "blogxyz-en" / "source" / "_posts"
    
    print(f"扫描目录: {posts_dir}")
    print()
    
    # 首先收集所有 tags 和 categories
    print("=" * 50)
    print("第一步：收集所有 tags 和 categories")
    print("=" * 50)
    
    all_tags, all_categories = collect_all_tags_categories(posts_dir)
    
    print(f"\n发现的 tags ({len(all_tags)} 个):")
    for tag in sorted(all_tags):
        if tag:
            translation = TRANSLATION_MAP.get(tag, "[未翻译]")
            print(f"  - {tag} -> {translation}")
    
    print(f"\n发现的 categories ({len(all_categories)} 个):")
    for cat in sorted(all_categories):
        if cat:
            translation = TRANSLATION_MAP.get(cat, "[未翻译]")
            print(f"  - {cat} -> {translation}")
    
    # 检查是否有未翻译的项
    untranslated = set()
    for tag in all_tags:
        if tag and tag not in TRANSLATION_MAP:
            untranslated.add(tag)
    for cat in all_categories:
        if cat and cat not in TRANSLATION_MAP:
            untranslated.add(cat)
    
    if untranslated:
        print(f"\n警告：以下项目没有翻译映射:")
        for item in sorted(untranslated):
            print(f"  - {item}")
    
    print()
    print("=" * 50)
    print("第二步：处理文件")
    print("=" * 50)
    
    # 处理所有文件
    modified_count = 0
    for md_file in sorted(Path(posts_dir).glob("*.md")):
        print(f"\n处理: {md_file.name}")
        if process_file(md_file, dry_run=False):
            modified_count += 1
    
    print()
    print("=" * 50)
    print(f"完成！共修改 {modified_count} 个文件")
    print("=" * 50)

if __name__ == "__main__":
    main()