#!/usr/bin/env python3
"""
博客文章翻译脚本
将 blogxyz/source/_posts 下的所有中文文章翻译成英文，
保留 frontmatter 格式，翻译正文部分，
然后复制到 blogxyz-en/source/_posts 对应位置。

此脚本只应运行一次。
"""

import os
import sys
import re
import argparse
import time
import signal
from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI


def signal_handler(sig, frame):
    """处理 Ctrl+C 信号"""
    print("\n\n已中断翻译任务")
    sys.exit(0)


# 注册信号处理器
signal.signal(signal.SIGINT, signal_handler)

# 加载 .env 配置
load_dotenv()

# 从环境变量获取配置
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL")

# 源目录和目标目录
SOURCE_DIR = Path("blogxyz/source/_posts")
TARGET_DIR = Path("blogxyz-en/source/_posts")


def parse_markdown(content: str) -> tuple[list[str], str, str]:
    """
    解析 Markdown 文件，分离 frontmatter 和正文。
    返回: (frontmatter_lines, body, title)
    frontmatter_lines 是包含 --- 分隔符的行列表
    """
    lines = content.split('\n')
    
    # 第一行应该是 ---
    if lines[0] != '---':
        print("错误: 文件格式不正确，缺少 frontmatter")
        sys.exit(1)
    
    # 找到第二个 ---
    end_index = -1
    for i in range(1, len(lines)):
        if lines[i] == '---':
            end_index = i
            break
    
    if end_index == -1:
        print("错误: 文件格式不正确，frontmatter 未闭合")
        sys.exit(1)
    
    # frontmatter 行（包含两个 ---）
    frontmatter_lines = lines[:end_index + 1]
    
    # 正文（从 --- 后一行开始）
    body = '\n'.join(lines[end_index + 1:])
    
    # 提取 title：第二行（索引1），第一个空格后的内容
    title_line = lines[1]
    if ' ' in title_line:
        title = title_line.split(' ', 1)[1]
    else:
        title = ""
    
    return frontmatter_lines, body, title


def translate_text(client: OpenAI, text: str, title: str, filename: str) -> tuple[str, str]:
    """
    使用 OpenAI API 翻译文本和标题。
    返回: (translated_body, translated_title)
    """
    if not text.strip():
        return text, title
    
    system_prompt = """你是一位专业的翻译。请将以下中文博客文章内容翻译成英文。

重要规则：
1. 保留所有 Markdown 格式（标题、粗体、斜体、链接、图片、代码块等）
2. 保留所有换行符和段落结构
3. 保持所有英文文本、URL、代码片段和技术术语不变
4. **必须严格保持原文的行文风格**
5. 对于中文诗词或文学引用，务必翻译的古色古香
6. 不要添加任何解释或注释——只提供翻译内容

输出格式要求：
- 第一行必须是：[TITLE] 翻译后的标题
- 第二行必须是空行
- 从第三行开始是翻译后的正文内容
"""

    # 构建用户消息，包含标题和正文
    user_message = f"文章标题：{title}\n\n文章正文：\n{text}"

    print(f"  正在翻译 {filename}...")
    
    # 使用流式输出
    stream = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message}
        ],
        # temperature=0.3,
        stream=True
    )
    
    result_content = ""
    first_content_received = False
    
    for chunk in stream:
        # 处理正文内容
        if hasattr(chunk.choices[0].delta, 'content') and chunk.choices[0].delta.content:
            if not first_content_received:
                print("  开始接收翻译结果...")
                first_content_received = True
            content = chunk.choices[0].delta.content
            result_content += content
    
    # 解析翻译结果，提取标题和正文
    lines = result_content.split('\n', 2)
    translated_title = title  # 默认使用原标题
    translated_body = result_content
    
    if lines and lines[0].startswith('[TITLE]'):
        translated_title = lines[0].replace('[TITLE]', '').strip()
        # 跳过标题行和空行，获取正文
        if len(lines) > 2:
            translated_body = lines[2]
        elif len(lines) > 1:
            translated_body = lines[1]
        else:
            translated_body = ""
    
    return translated_body, translated_title


def update_frontmatter_title(frontmatter_lines: list[str], new_title: str) -> str:
    """
    更新 frontmatter 中的 title 字段并返回完整的 frontmatter 字符串。
    """
    # 第二行是 title 行，替换为新标题
    frontmatter_lines[1] = f"title: {new_title}"
    return '\n'.join(frontmatter_lines) + '\n'


def process_file(client: OpenAI, source_path: Path, target_path: Path):
    """
    处理单个文件：读取、翻译、保存。
    """
    print(f"\n处理文件: {source_path}")
    
    # 开始计时
    start_time = time.time()
    
    # 读取源文件
    with open(source_path, "r", encoding="utf-8") as f:
        content = f.read()
    
    # 分离 frontmatter 和正文
    frontmatter_lines, body, title = parse_markdown(content)
    
    print(f"  原标题: {title}")
    
    # 翻译正文和标题
    translated_body, translated_title = translate_text(client, body, title, source_path.name)
    
    print(f"  翻译后标题: {translated_title}")
    
    # 更新 frontmatter 中的标题
    updated_frontmatter = update_frontmatter_title(frontmatter_lines, translated_title)
    
    # 组合翻译后的内容（在 frontmatter 后加一个额外换行）
    translated_content = updated_frontmatter + "\n" + translated_body
    
    # 确保目标目录存在
    target_path.parent.mkdir(parents=True, exist_ok=True)
    
    # 保存到目标文件
    with open(target_path, "w", encoding="utf-8") as f:
        f.write(translated_content)
    
    # 计算耗时
    elapsed_time = time.time() - start_time
    
    print(f"  已保存到: {target_path}")
    print(f"  耗时: {elapsed_time:.2f} 秒")


def main():
    # 解析命令行参数
    parser = argparse.ArgumentParser(description="博客文章翻译脚本")
    parser.add_argument(
        "--file", "-f",
        type=str,
        default=None,
        help="只翻译指定文件名的文章（例如：凌晨几月.md）"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="强制重新翻译已存在的文件"
    )
    args = parser.parse_args()
    
    # 验证环境变量
    if not OPENAI_BASE_URL:
        print("错误:未设置 OPENAI_BASE_URL 环境变量")
        sys.exit(1)
    
    if not OPENAI_API_KEY:
        print("错误: 未设置 OPENAI_API_KEY 环境变量")
        sys.exit(1)
    
    print(f"OpenAI Base URL: {OPENAI_BASE_URL}")
    print(f"OpenAI Model: {OPENAI_MODEL}")
    print(f"源目录: {SOURCE_DIR}")
    print(f"目标目录: {TARGET_DIR}")
    if args.file:
        print(f"指定文件: {args.file}")
    print("-" * 50)
    
    # 创建 OpenAI 客户端
    client = OpenAI(
        base_url=OPENAI_BASE_URL,
        api_key=OPENAI_API_KEY
    )
    
    # 获取要翻译的文件
    if args.file:
        # 指定了文件名，只翻译该文件
        source_path = SOURCE_DIR / args.file
        if not source_path.exists():
            print(f"错误: 文件不存在 - {source_path}")
            sys.exit(1)
        md_files = [source_path]
    else:
        # 获取所有 .md 文件（排除 en 子目录）
        md_files = []
        for path in SOURCE_DIR.glob("*.md"):
            if path.is_file():
                md_files.append(path)
    
    if not md_files:
        print("未找到任何 .md 文件")
        sys.exit(0)
    
    print(f"找到 {len(md_files)} 个文件待翻译")
    
    # 逐个处理文件
    success_count = 0
    skip_count = 0
    error_count = 0
    
    for source_path in md_files:
        # 计算目标路径（保持相同的文件名）
        target_path = TARGET_DIR / source_path.name
        
        # 检查目标文件是否已存在
        if target_path.exists() and not args.force:
            print(f"\n跳过文件: {source_path.name} (已存在)")
            skip_count += 1
            continue
        
        try:
            process_file(client, source_path, target_path)
            success_count += 1
        except Exception as e:
            print(f"  错误: {e}")
            error_count += 1
    
    print("\n" + "=" * 50)
    print(f"翻译完成！成功: {success_count}, 跳过: {skip_count}, 失败: {error_count}")


if __name__ == "__main__":
    main()