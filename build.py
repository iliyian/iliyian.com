import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tempfile

SITES = ["blogxyz", "blogxyz-en"]

# 构建脚本使用工作区中的版本（属于构建工具，而非站点内容）
BUILD_FILES = [
    "subset_fontawesome.py",
    "precompute_cover_colors.py",
    "subset_fonts.py",
]

# 独立页面属于部署内容，--deploy 时从根仓库 HEAD 取，避免带上未提交改动
STANDALONE_PAGES = ["cn-visitors.html"]

def ensure_python_deps():
    """确保字体子集化/封面取色依赖可用（Netlify 等 CI 环境为全新 Python）"""
    try:
        import brotli  # noqa: F401
        import fontTools  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        print("Installing dependencies (fonttools, brotli, pillow)...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "fonttools", "brotli", "pillow"])

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
    parser.add_argument(
        "--deploy",
        action="store_true",
        help="deploy only committed HEAD of each site (uncommitted/untracked changes are excluded)",
    )
    return parser.parse_args()

def git_status(path):
    """返回未提交/未跟踪改动列表；递归子模块，非仓库则返回空列表"""
    def status_of(repo_path):
        try:
            out = subprocess.check_output(
                ["git", "status", "--porcelain", "--untracked-files=normal"],
                cwd=repo_path, text=True, stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.CalledProcessError):
            return []
        return [line for line in out.splitlines() if line.strip()]

    entries = status_of(path)
    try:
        submodules = subprocess.check_output(
            ["git", "submodule", "status", "--recursive"],
            cwd=path, text=True, stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return entries

    for line in submodules.splitlines():
        fields = line[1:].split()
        if len(fields) < 2:
            continue
        subpath = fields[1]
        entries.extend(f"{subpath}: {entry}" for entry in status_of(os.path.join(path, subpath)))
    return entries

def warn_ignored_changes(base_dir):
    """提示哪些改动不会被部署（部署只使用已提交的 HEAD）"""
    for site in SITES:
        status = git_status(os.path.join(base_dir, site))
        if status:
            print(f"Notice: {site} has uncommitted/untracked changes; --deploy only builds the committed HEAD:")
            for line in status:
                print(f"  {line}")
            print("  These changes will NOT be included in this deployment.")

def copy_standalone_page(base_dir, name, dst_dir):
    """独立页面按根仓库 HEAD 取；非 git 环境则回退到工作区文件"""
    dst = os.path.join(dst_dir, name)
    try:
        data = subprocess.check_output(
            ["git", "show", f"HEAD:{name}"], cwd=base_dir, stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        src = os.path.join(base_dir, name)
        if os.path.exists(src):
            shutil.copy2(src, dst)
        return
    with open(dst, "wb") as fp:
        fp.write(data)

def create_deploy_workspace(base_dir):
    """在临时目录按各站点 HEAD 创建干净 worktree，未提交/未跟踪内容不会参与构建"""
    workspace = tempfile.mkdtemp(prefix="iliyian-deploy-")
    print(f"Creating deploy workspace at {workspace} (build from committed HEAD only)")

    try:
        for site in SITES:
            src = os.path.join(base_dir, site)
            dst = os.path.join(workspace, site)
            run_command(f"git worktree add --detach {shlex.quote(dst)} HEAD", src)
            run_command("git submodule update --init --recursive", dst)

            # 复用已安装的依赖，避免在临时工作区重复 npm install
            node_modules = os.path.join(src, "node_modules")
            if os.path.isdir(node_modules):
                os.symlink(node_modules, os.path.join(dst, "node_modules"))

            # hexo-deployer-git 在 base_dir/.deploy_git 里保存 gh-pages 历史，
            # 持久化到真实站点目录，避免临时工作区销毁时丢失部署历史
            deploy_git = os.path.join(src, ".deploy_git")
            if os.path.isdir(deploy_git):
                os.symlink(deploy_git, os.path.join(dst, ".deploy_git"))

        for name in BUILD_FILES:
            src = os.path.join(base_dir, name)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(workspace, name))

        for page in STANDALONE_PAGES:
            copy_standalone_page(base_dir, page, workspace)

        return workspace
    except BaseException:
        remove_deploy_workspace(base_dir, workspace)
        raise

def persist_deploy_history(base_dir, workspace):
    """把 hexo-deployer-git 新建的 .deploy_git 移回真实站点目录，供下次部署续写历史"""
    for site in SITES:
        deploy_git = os.path.join(workspace, site, ".deploy_git")
        target = os.path.join(base_dir, site, ".deploy_git")
        if os.path.isdir(deploy_git) and not os.path.lexists(target):
            shutil.move(deploy_git, target)

def remove_deploy_workspace(base_dir, workspace):
    persist_deploy_history(base_dir, workspace)
    for site in SITES:
        src = os.path.join(base_dir, site)
        dst = os.path.join(workspace, site)
        subprocess.run(
            ["git", "worktree", "remove", "--force", dst], cwd=src, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["git", "worktree", "prune"], cwd=src, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    shutil.rmtree(workspace, ignore_errors=True)

def build_site(base_dir):
    cn_path = os.path.join(base_dir, "blogxyz")
    en_path = os.path.join(base_dir, "blogxyz-en")

    # 0. Subset FontAwesome icons from scanned source (must run before hexo generate)
    run_command("python subset_fontawesome.py", base_dir)

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

    # 3.5 预计算封面主色（128px q75 缩略图，构建时生成 cover-colors.js）
    run_command("python precompute_cover_colors.py", base_dir)

    # 4. 按站点实际使用字符生成精简字体（subset_fonts.py）
    print("Subsetting fonts based on used characters...")
    run_command("python subset_fonts.py", base_dir)

    # 4.5 校验构建产物中所有 fa- 图标都有对应 glyph
    run_command("python subset_fontawesome.py --verify", base_dir)

    # 5. Copy standalone pages to public directory
    for page in ["cn-visitors.html"]:
        src = os.path.join(base_dir, page)
        dst = os.path.join(cn_path, "public", page)
        if os.path.exists(src):
            shutil.copy2(src, dst)
            print(f"Copied {page} to public/")

    print("Build complete! The combined site is in blogxyz/public")
    return cn_path

def main():
    args = parse_args()
    base_dir = os.getcwd()
    ensure_python_deps()

    if args.deploy:
        warn_ignored_changes(base_dir)
        workspace = create_deploy_workspace(base_dir)
        try:
            cn_path = build_site(workspace)
            print("Deploying committed HEAD to GitHub Pages...")
            run_command("npx hexo deploy", cn_path)
        finally:
            remove_deploy_workspace(base_dir, workspace)
    else:
        build_site(base_dir)

if __name__ == "__main__":
    main()
