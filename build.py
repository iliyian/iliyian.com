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

def read_deploy_config(site_dir):
    """从站点 _config.yml 读出 deploy 的 repo 与 branch（hexo-deployer-git 的目标）"""
    config_path = os.path.join(site_dir, "_config.yml")
    with open(config_path, encoding="utf-8") as fp:
        lines = fp.read().splitlines()

    repo = branch = None
    in_deploy = False
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith((" ", "\t")):
            in_deploy = line.strip() == "deploy:"
            continue
        if not in_deploy:
            continue
        key, _, value = line.strip().partition(":")
        value = value.strip().strip('"\'')
        if key == "repo" and value:
            repo = value
        elif key == "branch" and value:
            branch = value
    return repo, branch

def ensure_deploy_history(site_dir):
    """确保 .deploy_git 带着远端 gh-pages 历史。

    hexo-deployer-git 在 .deploy_git 不存在时会 git init 一个全新仓库，再
    `push --force` 推上去——远端 gh-pages 的历史会被整个覆盖掉。
    .deploy_git 被 gitignore，换机器或干净 CI 上必然缺失，所以这里先从远端
    克隆部署分支；克隆失败（离线/无权限）就放弃续写历史并明确警告。
    """
    deploy_dir = os.path.join(site_dir, ".deploy_git")
    if os.path.isdir(os.path.join(deploy_dir, ".git")):
        return True

    repo, branch = read_deploy_config(site_dir)
    if not repo:
        print(f"Warning: no deploy repo configured in {site_dir}/_config.yml; "
              "gh-pages history cannot be continued")
        return False

    # 远端还没有该分支（首次部署）时无历史可续写，让 deployer 照常新建即可
    probe = subprocess.run(
        ["git", "ls-remote", "--heads", repo, f"refs/heads/{branch}"],
        capture_output=True, text=True,
    )
    if probe.returncode != 0:
        print(f"Warning: could not reach {repo}: {probe.stderr.strip()}")
        return False
    if not probe.stdout.strip():
        print(f"No {branch} branch on {repo} yet; starting a fresh deployment history")
        return True

    if os.path.isdir(deploy_dir):
        shutil.rmtree(deploy_dir)

    print(f"Cloning {branch} history from {repo} into {deploy_dir}")
    result = subprocess.run(
        ["git", "clone", "--branch", branch, "--single-branch", "--no-tags", repo, deploy_dir],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(
            "Warning: could not clone existing deployment history; "
            "hexo-deployer-git would overwrite it with a fresh repo.\n"
            f"  {result.stderr.strip()}\n"
            "  Abort this run, or fix access and re-run."
        )
        return False

    # 克隆下来的内容随后会被 deployer 用 public 目录整体覆盖，这里只需要 .git
    for entry in os.listdir(deploy_dir):
        if entry == ".git":
            continue
        target = os.path.join(deploy_dir, entry)
        shutil.rmtree(target) if os.path.isdir(target) else os.remove(target)
    return True

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
            # 持久化到真实站点目录，避免临时工作区销毁时丢失部署历史。
            # 缺失时先补齐，否则 deployer 会新建仓库并用 --force 覆盖远端历史。
            deploy_git = os.path.join(src, ".deploy_git")
            if not ensure_deploy_history(src):
                raise SystemExit(f"Refusing to deploy {site}: deployment history is missing and could not be restored")
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
