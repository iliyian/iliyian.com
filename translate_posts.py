#!/usr/bin/env python3
"""
博客文章翻译脚本
将 blogxyz/source/_posts 下的所有中文文章翻译成英文，
保留 frontmatter 格式，翻译正文部分，
然后复制到 blogxyz-en/source/_posts 对应位置。

支持多文件并发（-j/--workers）。
使用 --field 时只更新目标文件中指定的 frontmatter 字段，不翻译正文，
其余字段和正文保持目标文件原样。

此脚本只应运行一次。
"""

import os
import sys

import argparse
import time
import signal
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI


def signal_handler(sig, frame):
    """处理 Ctrl+C 信号（并发模式下直接退出，不等待进行中的请求）"""
    print("\n\n已中断翻译任务")
    sys.stdout.flush()
    os._exit(130)


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

# 需要翻译的 frontmatter 字段
TRANSLATE_FIELDS = ["title", "description", "message"]


def parse_markdown(content: str) -> tuple[list[str], str, dict[str, str]]:
    """
    解析 Markdown 文件，分离 frontmatter 和正文。
    返回: (frontmatter_lines, body, fields)
    frontmatter_lines 是包含 --- 分隔符的行列表
    fields 是需要翻译的字段字典 {字段名: 值}
    """
    lines = content.split('\n')

    # 第一行应该是 ---
    if lines[0] != '---':
        raise ValueError("文件格式不正确，缺少 frontmatter")

    # 找到第二个 ---
    end_index = -1
    for i in range(1, len(lines)):
        if lines[i] == '---':
            end_index = i
            break

    if end_index == -1:
        raise ValueError("文件格式不正确，frontmatter 未闭合")

    # frontmatter 行（包含两个 ---）
    frontmatter_lines = lines[:end_index + 1]

    # 正文（从 --- 后一行开始）
    body = '\n'.join(lines[end_index + 1:])

    # 提取需要翻译的字段
    fields = {}
    for line in frontmatter_lines[1:end_index]:
        for field in TRANSLATE_FIELDS:
            if line.startswith(f"{field}:"):
                value = line.split(':', 1)[1].strip()
                if value:
                    fields[field] = value
                break

    return frontmatter_lines, body, fields


def translate_field(client: OpenAI, field_name: str, value: str, filename: str) -> str:
    """
    使用 OpenAI API 翻译单个 frontmatter 字段。
    返回翻译后的字段值。
    """
    if not value.strip():
        return value

    system_prompt = """你是一位专业的翻译。请将以下中文文本翻译成英文。

重要规则：
1. **必须严格保持原文的行文风格**
2. 对于中文诗词或文学引用，务必翻译的古色古香
3. 不要添加任何解释或注释——只提供翻译内容
4. 只输出翻译结果，不要输出任何其他内容"""

    print(f"  [{filename}] 正在翻译字段 {field_name}: {value}")

    stream = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": value}
        ],
        stream=True
    )

    result = ""
    for chunk in stream:
        if hasattr(chunk.choices[0].delta, 'content') and chunk.choices[0].delta.content:
            result += chunk.choices[0].delta.content

    result = result.strip()
    print(f"  [{filename}] {field_name} 翻译结果: {result}")
    return result


def translate_body(client: OpenAI, text: str, filename: str) -> str:
    """
    使用 OpenAI API 翻译正文。
    返回翻译后的正文。
    """
    if not text.strip():
        return text

    system_prompt = """你是一位专业的翻译。请将以下中文博客文章内容翻译成英文。

重要规则：
1. 保留所有 Markdown 格式（标题、粗体、斜体、链接、图片、代码块等）
2. 保留所有换行符和段落结构
3. 保持所有英文文本、URL、代码片段和技术术语不变
4. **必须严格保持原文的行文风格**
5. 对于中文诗词或文学引用，务必翻译的古色古香
6. 不要添加任何解释或注释——只提供翻译内容"""

    print(f"  [{filename}] 正在翻译正文 ({len(text)} 字符)...")

    stream = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text}
        ],
        stream=True
    )

    result = ""

    for chunk in stream:
        if hasattr(chunk.choices[0].delta, 'content') and chunk.choices[0].delta.content:
            result += chunk.choices[0].delta.content

    return result


def update_frontmatter_fields(frontmatter_lines: list[str], translated_fields: dict[str, str]) -> str:
    """
    更新 frontmatter 中的已翻译字段并返回完整的 frontmatter 字符串。
    """
    for i, line in enumerate(frontmatter_lines):
        for field, value in translated_fields.items():
            if line.startswith(f"{field}:"):
                frontmatter_lines[i] = f"{field}: {value}"
                break
    return '\n'.join(frontmatter_lines) + '\n'


def process_file(
    client: OpenAI,
    source_path: Path,
    target_path: Path,
    only_fields: list[str] | None = None,
):
    """
    处理单个文件：读取、翻译、保存。

    only_fields 不为空时，只更新目标文件中这些 frontmatter 字段，
    正文和其他字段保持目标文件原样；此时目标文件必须已存在。
    成功时返回 True，跳过时返回 False，失败时抛出异常。
    """
    print(f"\n[{source_path.name}] 开始处理")

    # 开始计时
    start_time = time.time()

    # 读取源文件
    with open(source_path, "r", encoding="utf-8") as f:
        content = f.read()

    # 分离 frontmatter 和正文
    frontmatter_lines, body, fields = parse_markdown(content)

    if only_fields:
        # 只翻译指定字段：基于已有的目标译文更新，不碰正文和其他字段
        if not target_path.exists():
            print(f"  [{source_path.name}] 跳过: 目标文件不存在，请先完整翻译")
            return False

        selected = [f for f in only_fields if f in fields]
        missing = [f for f in only_fields if f not in fields]
        if missing:
            print(f"  [{source_path.name}] 源文件缺少字段，已跳过: {', '.join(missing)}")
        if not selected:
            print(f"  [{source_path.name}] 没有可翻译的字段")
            return False

        translated_fields = {}
        for field_name in selected:
            translated_fields[field_name] = translate_field(
                client, field_name, fields[field_name], source_path.name
            )

        # 读取已有的目标文件
        with open(target_path, "r", encoding="utf-8") as f:
            target_content = f.read()

        target_frontmatter_lines, target_body, _ = parse_markdown(target_content)

        # 目标文件中没有的字段，插入到 frontmatter 末尾（闭合 --- 之前）
        for field_name, value in translated_fields.items():
            if not any(line.startswith(f"{field_name}:") for line in target_frontmatter_lines):
                target_frontmatter_lines.insert(-1, f"{field_name}: {value}")

        # 只替换指定字段，正文与其余字段逐字节保留
        updated_frontmatter = update_frontmatter_fields(target_frontmatter_lines, translated_fields)
        translated_content = updated_frontmatter + target_body
    else:
        if fields:
            print(f"  [{source_path.name}] 待翻译字段: {', '.join(f'{k}={v}' for k, v in fields.items())}")

        # 逐个翻译 frontmatter 字段
        translated_fields = {}
        for field_name, field_value in fields.items():
            translated_fields[field_name] = translate_field(client, field_name, field_value, source_path.name)

        # 翻译正文
        translated_body = translate_body(client, body, source_path.name)

        # 更新 frontmatter 中的字段
        updated_frontmatter = update_frontmatter_fields(frontmatter_lines, translated_fields)

        # 组合翻译后的内容（在 frontmatter 后加一个额外换行）
        translated_content = updated_frontmatter + "\n" + translated_body

    # 确保目标目录存在
    target_path.parent.mkdir(parents=True, exist_ok=True)

    # 保存到目标文件
    with open(target_path, "w", encoding="utf-8") as f:
        f.write(translated_content)

    # 计算耗时
    elapsed_time = time.time() - start_time

    print(f"  [{source_path.name}] 已保存到: {target_path}（耗时 {elapsed_time:.2f} 秒）")

    return True


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
    parser.add_argument(
        "--workers", "-j",
        type=int,
        default=4,
        help="并发翻译的文件数（默认 4，设为 1 即串行）"
    )
    parser.add_argument(
        "--field", "-F",
        action="append",
        default=None,
        metavar="FIELD",
        help=(
            "只翻译指定 frontmatter 字段，不翻译正文，其余字段保持目标文件原样。"
            f"可选: {', '.join(TRANSLATE_FIELDS)}；也可用逗号分隔多个字段"
        )
    )
    args = parser.parse_args()

    if args.workers < 1:
        print("错误: --workers 必须大于等于 1")
        sys.exit(1)

    only_fields = None
    if args.field:
        only_fields = []
        for item in args.field:
            for field_name in item.split(","):
                field_name = field_name.strip()
                if not field_name:
                    continue
                if field_name not in TRANSLATE_FIELDS:
                    print(f"错误: 不支持的字段 {field_name}，可选: {', '.join(TRANSLATE_FIELDS)}")
                    sys.exit(1)
                if field_name not in only_fields:
                    only_fields.append(field_name)
        if not only_fields:
            print("错误: --field 不能为空")
            sys.exit(1)
    
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
    if only_fields:
        print(f"只翻译字段: {', '.join(only_fields)}（不翻译正文，其余字段保持不变）")
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

    workers = min(args.workers, len(md_files))
    print(f"并发数: {workers}")

    # 并发处理文件
    start_time = time.time()
    success_count = 0
    skip_count = 0
    error_count = 0

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="translate") as executor:
        future_to_path = {
            executor.submit(
                process_file,
                client,
                source_path,
                TARGET_DIR / source_path.name,
                only_fields,
            ): source_path
            for source_path in md_files
        }

        for future in as_completed(future_to_path):
            source_path = future_to_path[future]
            try:
                if future.result():
                    success_count += 1
                else:
                    skip_count += 1
            except Exception as e:
                print(f"  [{source_path.name}] 错误: {e}")
                error_count += 1

    elapsed_time = time.time() - start_time

    print("\n" + "=" * 50)
    print(
        f"翻译完成！成功: {success_count}, 跳过: {skip_count}, "
        f"失败: {error_count}, 总耗时: {elapsed_time:.2f} 秒"
    )


if __name__ == "__main__":
    main()