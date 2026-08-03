import argparse
import os
import shutil
import subprocess
import sys

def ensure_python_deps():
    """确保字体子集化依赖可用（Netlify 等 CI 环境为全新 Python，无 fonttools/brotli）"""
    try:
        import brotli  # noqa: F401
        import fontTools  # noqa: F401
    except ImportError:
        print("Installing font subsetting dependencies (fonttools, brotli)...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "fonttools", "brotli"])

def run_command(command, cwd):
    print(f"Running command: {command} in {cwd}")
    subprocess.check_call(command, shell=True, cwd=cwd)

def check_and_install_deps(path):
    node_modules_path = os.path.join(path, "node_modules")
    if not os.path.exists(node_modules_path):
        print(f"node_modules not found in {path}, installing dependencies...")
        run_command("npm install", path)
    else:
        print(f"node_modules exists in {path}, skipping npm install.")

def build_hexo(path):
    check_and_install_deps(path)
    print(f"Building Hexo in {path}...")
    run_command("npx hexo clean", path)
    run_command("npx hexo generate", path)

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--deploy", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    base_dir = os.getcwd()
    ensure_python_deps()
    cn_path = os.path.join(base_dir, "blogxyz")
    en_path = os.path.join(base_dir, "blogxyz-en")
    
    # 1. Build Chinese version (blogxyz)
    build_hexo(cn_path)
    
    # 2. Build English version (blogxyz-en)
    build_hexo(en_path)
    
    # 3. Copy English version to Chinese version's public/en
    cn_public_en = os.path.join(cn_path, "public", "en")
    en_public = os.path.join(en_path, "public")
    
    print(f"Copying {en_public} to {cn_public_en}...")
    
    if os.path.exists(cn_public_en):
        shutil.rmtree(cn_public_en)
    
    shutil.copytree(en_public, cn_public_en)
    
    # 4. 按站点实际使用字符生成精简字体（subset_fonts.py）
    print("Subsetting fonts based on used characters...")
    run_command("python subset_fonts.py", base_dir)

    # 5. Copy standalone pages to public directory
    for page in ["cn-visitors.html"]:
        src = os.path.join(base_dir, page)
        dst = os.path.join(cn_path, "public", page)
        if os.path.exists(src):
            shutil.copy2(src, dst)
            print(f"Copied {page} to public/")

    print("Build complete! The combined site is in blogxyz/public")

    if args.deploy:
        print("Deploying to GitHub Pages...")
        run_command("npx hexo deploy", cn_path)

if __name__ == "__main__":
    main()