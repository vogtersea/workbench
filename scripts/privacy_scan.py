#!/usr/bin/env python
"""敏感词扫描：防止内部文件名称、机构名、人名、地名进入仓库。

为什么需要它：报告导出功能要照抄内部规范的排版参数，很容易顺手把规范的文件名、
部门、举例机构一起写进注释或 CHANGELOG——一旦推上公开仓库就等于泄露。

三类词来源：
  1. 脚本内置 —— 只放与具体机构无关的通用公文流转词（拟稿人、发文字号等）；
  2. 仓库根目录 .privacy-terms.txt —— 你自己的机构名/地名/人名，**已 gitignore**，
     绝不入库（否则扫描词本身成了泄露源）；
  3. 环境变量 PRIVACY_TERMS —— 多个词用逗号分隔，适合 CI。

用法：
    python scripts/privacy_scan.py              # 扫描全部已跟踪文件
    python scripts/privacy_scan.py --staged     # 只扫描暂存区（pre-commit 用）
    python scripts/privacy_scan.py --history    # 连同 git 历史一起查
    python scripts/privacy_scan.py --path dist  # 扫描任意目录
命中即返回码 1，可直接用于提交前拦截。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 只放「与任何机构无关」的通用公文流转词。
#: 机构名、部门名、地名、文件名一律写进 .privacy-terms.txt（已 gitignore）——
#: 把具体名称写进这里，等于扫描器自己成了泄露源。
DEFAULT_TERMS = (
    "拟稿人",
    "审核人",
    "发文字号",
    "主送机关",
    "上行文",
    "下行文",
    "讲话材料",
)

#: 这些目录/后缀不扫描（构建产物、虚拟环境、二进制）
SKIP_DIRS = {".git", ".venv", "dist", "build", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules"}

#: 本文件自身豁免：它必须写出这些词才能扫描它们（自检问题），
#: 其余文件一律不豁免——真正的泄露不可能长期只藏在词表里。
SELF_SKIP = {"scripts/privacy_scan.py"}
SKIP_SUFFIX = {".pyc", ".exe", ".dll", ".png", ".jpg", ".ico", ".zip", ".gz", ".db", ".docx", ".pdf", ".whl"}


def load_terms() -> list[str]:
    terms = [t for t in DEFAULT_TERMS]
    local = ROOT / ".privacy-terms.txt"
    if local.exists():
        for line in local.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                terms.append(line)
    env = os.environ.get("PRIVACY_TERMS", "")
    terms.extend(t.strip() for t in env.split(",") if t.strip())
    # 去重但保持顺序
    seen: set[str] = set()
    return [t for t in terms if not (t in seen or seen.add(t))]


def tracked_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
    return [ROOT / line for line in out.stdout.splitlines() if line.strip()]


def staged_files() -> list[Path]:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        cwd=ROOT, capture_output=True, text=True,
    )
    return [ROOT / line for line in out.stdout.splitlines() if line.strip()]


def walk(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    found = []
    for item in path.rglob("*"):
        if not item.is_file():
            continue
        if any(part in SKIP_DIRS for part in item.parts):
            continue
        if item.suffix.lower() in SKIP_SUFFIX:
            continue
        found.append(item)
    return found


def scan_files(files: list[Path], terms: list[str]) -> list[str]:
    hits = []
    for file in files:
        if file.suffix.lower() in SKIP_SUFFIX or not file.exists():
            continue
        if any(part in SKIP_DIRS for part in file.parts):
            continue
        if str(file.relative_to(ROOT)).replace("\\", "/") in SELF_SKIP:
            continue
        try:
            text = file.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for term in terms:
                if term in line:
                    rel = file.relative_to(ROOT) if file.is_relative_to(ROOT) else file
                    hits.append("%s:%d: 命中「%s」" % (rel, lineno, term))
    return hits


def scan_history(terms: list[str]) -> list[str]:
    hits = []
    for term in terms:
        out = subprocess.run(
            ["git", "log", "--all", "-S" + term, "--oneline"], cwd=ROOT, capture_output=True, text=True
        )
        commits = [line for line in out.stdout.splitlines() if line.strip()]
        if commits:
            hits.append("历史中有 %d 个提交含「%s」" % (len(commits), term))
    return hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="敏感词扫描（命中返回 1）")
    parser.add_argument("--staged", action="store_true", help="只扫描暂存区（pre-commit 用）")
    parser.add_argument("--history", action="store_true", help="连同 git 历史一起检查")
    parser.add_argument("--path", default="", help="扫描指定路径，默认全部已跟踪文件")
    parser.add_argument("--quiet", action="store_true", help="只在命中时输出")
    args = parser.parse_args(argv)

    terms = load_terms()
    if args.path:
        files = walk(Path(args.path))
    elif args.staged:
        files = staged_files()
    else:
        files = tracked_files()

    hits = scan_files(files, terms)
    if args.history:
        hits += scan_history(terms)

    if hits:
        print("发现 %d 处敏感内容：" % len(hits), file=sys.stderr)
        for line in hits:
            print("  " + line, file=sys.stderr)
        print("\n处理方式：删掉或改写；确实需要保留的词请写进 .privacy-terms.txt 之外的地方。", file=sys.stderr)
        return 1

    if not args.quiet:
        print("敏感词扫描通过：%d 个词，%d 个文件%s" % (
            len(terms), len(files), "（含历史）" if args.history else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
