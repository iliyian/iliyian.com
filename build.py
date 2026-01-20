import os
import shutil
import subprocess

def run_command(command, cwd):
    print(f"Running command: {command} in {cwd}")
    subprocess.check_call(command, shell=True, cwd=cwd)

def build_hexo(path):
    print(f"Building Hexo in {path}...")
    run_command("npx hexo clean", path)
    run_command("npx hexo generate", path)

def main():
    base_dir = os.getcwd()
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
    
    print("Build complete! The combined site is in blogxyz/public")

if __name__ == "__main__":
    main()