# -*- coding: utf-8 -*-
"""opt-in 本地 pre-commit 守卫——无 git remote 的仓库里唯一接近"自动门"的东西。

只做**快、确定、工具容错**的三件事；绝不把每次 commit 变成跑全套门的重税，也绝不 fail-closed
把「本机没装某工具」变成「什么都提交不了」：
  1. 暂存内容扫**锚定 secret 值**（sk-/ghp_/AKIA/…）——真 key 绝不该进 git 历史，哪怕本地无 remote。
     只报模式名+位置、不回显值。**不**扫内部专名（作者姓名、团队内部称谓等）：那些合法存在于仓库
     （内部笔记等），只在 make_delivery 对外交付时才剥离；在这里扫会挡住合法提交。
  2. 暂存的 .py 做语法编译（ast，无导入/无副作用）。
  3. 暂存的 .js 跑 `node --check`——**仅当 node 可用**；缺 node 只告警跳过、不阻断。

装：`scripts/install-hooks.ps1`（设 core.hooksPath=.githooks）。绕过单次：`git commit --no-verify`。
"""
from __future__ import annotations

import ast
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from secret_patterns import SECRET_VALUE_PATTERNS  # noqa: E402
# 文本覆盖面与交付门同一真源（后缀集 + 无后缀整名 + UTF-8/GBK 双解码），防两套口径漂移。
from make_delivery import _is_scannable_text, _read_scannable_text  # noqa: E402


def _rel(path: Path) -> str:
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _staged_files() -> list[Path]:
    # -z：NUL 分隔且**不做路径转义**。默认（无 -z）git 会把非 ASCII（中文）文件名输出为八进制转义的
    # 双引号串（core.quotePath），`REPO_ROOT/该串` 解析不到真文件→被静默剔出扫描。本仓库大量中文路径
    # （使用教程/ 等中文路径），漏扫面极广，故必须 -z。
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "-z", "--diff-filter=ACM"],
        cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    files: list[Path] = []
    for rel in result.stdout.split("\0"):
        if not rel:
            continue
        path = REPO_ROOT / rel
        if path.is_file():
            files.append(path)
    return files


def _scan_secrets(files: list[Path]) -> list[tuple[str, int, str]]:
    hits: list[tuple[str, int, str]] = []
    for path in files:
        if not _is_scannable_text(path):
            continue
        text = _read_scannable_text(path)
        if text is None:
            # 本层容错：解码不了/含 NUL（UTF-16 等）只跳过，不阻断提交；
            # fail-closed 在 make_delivery 交付门（unscannable 计入判定）。
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for pattern_id, rx in SECRET_VALUE_PATTERNS:
                if rx.search(line):
                    hits.append((_rel(path), lineno, pattern_id))
    return hits


def _compile_py(files: list[Path]) -> list[str]:
    errors: list[str] = []
    for path in files:
        if path.suffix.lower() != ".py":
            continue
        try:
            compile(path.read_text(encoding="utf-8-sig"), str(path), "exec",
                    flags=ast.PyCF_ONLY_AST, dont_inherit=True)
        except SyntaxError as exc:
            errors.append(f"{_rel(path)}:{exc.lineno} {exc.msg}")
    return errors


def _check_js(files: list[Path]) -> tuple[list[str], bool]:
    """返回 (错误列表, node_可用)。缺 node → ([], False) 表示容错跳过、不阻断。"""
    targets = [p for p in files if p.suffix.lower() == ".js" and "/vendor/" not in p.as_posix()]
    if not targets:
        return [], True
    node = shutil.which("node") or shutil.which("node.exe")
    if not node:
        return [], False
    errors: list[str] = []
    for path in targets:
        result = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
        if result.returncode != 0:
            tail = result.stderr.strip().splitlines()
            errors.append(f"{path.relative_to(REPO_ROOT).as_posix()}: {tail[-1] if tail else 'node --check failed'}")
    return errors, True


def main() -> int:
    files = _staged_files()
    if not files:
        return 0

    problems = 0

    secrets = _scan_secrets(files)
    if secrets:
        problems += 1
        print("[pre-commit] 暂存内容命中疑似 secret 值（真 key 不该进 git；只报模式名+位置）：", file=sys.stderr)
        for path, line, pattern_id in secrets:
            print(f"  {path}:{line}  pattern={pattern_id}", file=sys.stderr)

    py_errors = _compile_py(files)
    if py_errors:
        problems += 1
        print("[pre-commit] 暂存 .py 语法错误：", file=sys.stderr)
        for err in py_errors:
            print(f"  {err}", file=sys.stderr)

    js_errors, node_available = _check_js(files)
    if js_errors:
        problems += 1
        print("[pre-commit] 暂存 .js node --check 失败：", file=sys.stderr)
        for err in js_errors:
            print(f"  {err}", file=sys.stderr)
    if not node_available:
        print("[pre-commit] 提示：未找到 node，跳过 .js 语法检查（非阻断）。", file=sys.stderr)

    if problems:
        print("\n[pre-commit] 阻止提交。修复后重试，或 `git commit --no-verify` 显式绕过（自负其责）。",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
