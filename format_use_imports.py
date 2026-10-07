#!/usr/bin/env python3
"""检查 / 自动修复 Rust 的 `use` 语句布局（AGENTS.md §5）。

规则（本仓库约定）
------------------

> 使用 `use` 语句导入依赖时，所有外部 crate（包括 core 和 std）必须以
> **独立单根**形式出现且**只出现一次**。

也就是：同一个作用域里，`core` / `std` / `alloc` / `abs_art` / `crate` … 每个根
各占**一条** `use`，路径写成嵌套形式：

    // 不合规：同一根散成多条
    use core::fmt;
    use core::time::Duration;

    // 合规：一个根一条、路径嵌套
    use core::{fmt, time::Duration};

`cargo fmt` 不管这条：rustfmt 的 import granularity 默认是 `Preserve`，它只排版、
不合并。所以需要本工具。

用法
----

    python3 format_use_imports.py                 # 检查（默认；有违规则退出码 1）
    python3 format_use_imports.py --check         # 同上
    python3 format_use_imports.py --fix           # 自动修复，随后对改动的文件跑 rustfmt
    python3 format_use_imports.py --fix --no-rustfmt
    python3 format_use_imports.py PATH [PATH ...] # 只处理指定文件/目录

`--fix` 默认会**先要求可回退点**：若待改动的文件在 git 里有未提交改动（含未跟踪），
工具拒绝动手并提示先 `git commit` / `git stash -u`；确认要直接改时加 `--force`。
（不在 git 仓库里时这项检查自动跳过。）

不传路径时，默认扫描**本脚本所在目录**下的所有 `*.rs`（跳过 `target/` 等构建目录），
因此放在仓库根目录即可供该仓库内所有项目使用。

实现要点（为什么是安全的）
--------------------------

* 只合并**同一个 import 块**内的条目。一个「块」= 同一缩进下连续的 `use` 行与空行，
  遇到任何其它行（`}`、`fn`、注释…）即结束。作用域边界必然是「其它行」，所以
  跨作用域误合并**不可能发生**（两个函数的函数体导入之间至少隔着 `}` 与 `fn`）。
* 空行被当作**子组边界**保留：原本「系统 crate / 外部 crate / crate 内」的视觉分组
  不会被抹平，同一根的重复条目会被收拢到它**首次出现**的那个子组。
* 任一条目带有属性（`#[cfg(..)] use ..`）、重命名（`as`）或 glob（`*`）时，整个块
  跳过并报告「需手工整理」——不做可能有损的重写。
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

# 扫描时跳过的目录（构建产物、依赖缓存等）。
SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "target",
    "vendor",
    "node_modules",
    "build",
    "dist",
    ".cargo",
    ".idea",
    ".vscode",
}

USE_LINE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<vis>pub(?:\s*\([^)]*\))?\s+)?use\s+(?P<path>[^;]+);\s*$"
)
ATTR_LINE = re.compile(r"^[ \t]*#\s*\[")
BLANK_LINE = re.compile(r"^[ \t]*$")


# --------------------------------------------------------------------------- #
# 路径解析与树合并
# --------------------------------------------------------------------------- #

def split_commas(text: str) -> list[str]:
    """按顶层逗号切分 `a, b::{c, d}, e`。"""
    parts: list[str] = []
    depth = 0
    cur = ""
    for ch in text:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur)
    return [part.strip() for part in parts if part.strip()]


def split_path(text: str) -> list[str]:
    """只在**顶层**按 `::` 切分（`{...}` 内部的 `::` 不算）。"""
    parts: list[str] = []
    depth = 0
    cur = ""
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        if text.startswith("::", i) and depth == 0:
            parts.append(cur)
            cur = ""
            i += 2
            continue
        cur += ch
        i += 1
    if cur:
        parts.append(cur)
    return [part for part in parts if part]


def expand(path: str) -> list[list[str]]:
    """把 `core::task::{A, B}` 展开成 `[['core','task','A'], ['core','task','B']]`。"""
    result: list[list[str]] = [[]]
    for seg in split_path(path):
        if seg.startswith("{") and seg.endswith("}"):
            nxt: list[list[str]] = []
            for prefix in result:
                for leaf in split_commas(seg[1:-1]):
                    for sub in expand(leaf):
                        nxt.append(prefix + sub)
            result = nxt
        else:
            for prefix in result:
                prefix.append(seg)
    return result


def build_tree(paths: list[list[str]]) -> dict:
    tree: dict = {}
    for path in paths:
        node = tree
        for seg in path:
            node = node.setdefault(seg, {})
        node[""] = {}
    return tree


def render_tree(node: dict) -> str:
    keys = sorted(key for key in node if key != "")
    items = []
    for key in keys:
        sub = render_tree(node[key])
        items.append(key if sub is None else f"{key}::{sub}")
    if not items:
        return None  # type: ignore[return-value]
    if len(items) == 1 and "" not in node:
        return items[0]
    return "{" + ", ".join(items) + "}"


def merge_items(items: list[tuple[str, str, str]]) -> list[str]:
    """把 `(vis, root, path)` 列表合并成每条 `vis use root::{...}` 的路径体。

    返回 [(vis, root, body)]，按 root 首次出现顺序（同名 root 只保留一条）。
    """
    groups: dict[tuple[str, str], list[list[str]]] = {}
    order: list[tuple[str, str]] = []
    for vis, root, paths in items:
        key = (vis, root)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].extend(paths)
    out = []
    for vis, root in order:
        body = render_tree(build_tree(groups[(vis, root)]))
        out.append((vis, root, body))
    return out


# --------------------------------------------------------------------------- #
# 逐文件处理
# --------------------------------------------------------------------------- #

class Violation:
    def __init__(self, path: Path, line: int, message: str):
        self.path = path
        self.line = line
        self.message = message

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


def collect_block(lines: list[str], start: int) -> tuple[list[list[tuple[int, re.Match]]], int]:
    """从 `start` 起收集一个 import 块，按空行切成子组。

    返回 (sub_runs, next_index)；sub_runs 的每一项是该子组内的 (行号, 匹配对象)。
    只有 `use` 行与空行会被吞掉，遇到任何其它行立刻结束。
    """
    sub_runs: list[list[tuple[int, re.Match]]] = []
    current: list[tuple[int, re.Match]] = []
    i = start
    tail = start  # 最后一条 use 之后的位置：块尾的空行要原样留下
    while i < len(lines):
        line = lines[i]
        match = USE_LINE.match(line)
        if match:
            current.append((i, match))
            i += 1
            tail = i
            continue
        if BLANK_LINE.match(line):
            if current:
                sub_runs.append(current)
                current = []
            i += 1
            continue
        break
    if current:
        sub_runs.append(current)
    return sub_runs, tail


def block_has_attribute(lines: list[str], sub_runs) -> bool:
    """块内是否有 `use` 条目被属性修饰（含 `#[cfg]`）。"""
    for run in sub_runs:
        for line_no, _ in run:
            j = line_no - 1
            while j >= 0 and ATTR_LINE.match(lines[j]):
                return True
    return False


def is_plain(path_text: str) -> bool:
    """条目是否「朴素」（可安全合并）：无 ` as `、无 glob。"""
    return " as " not in path_text and "*" not in path_text


def process_file(
    path: Path, fix: bool, skipped: list[Violation] | None = None
) -> tuple[list[Violation], bool]:
    """处理一个文件；返回 (违规列表, 是否改动)。

    `skipped` 非 `None` 时，把「因含属性/重命名/glob 而跳过」的块记进去（仅信息用途）。
    """
    text = path.read_text(encoding="utf-8")
    if not text:
        return [], False
    had_trailing_newline = text.endswith("\n")
    lines = text.split("\n")
    violations: list[Violation] = []
    changed = False
    i = 0
    while i < len(lines):
        if not USE_LINE.match(lines[i]):
            i += 1
            continue
        sub_runs, next_i = collect_block(lines, i)
        # 块内条目
        items: list[tuple[str, str, str]] = []  # (vis, root, path)
        indexed: list[tuple[int, str, str, str]] = []  # (行号, vis, root, path)
        plain = True
        for run in sub_runs:
            for line_no, match in run:
                vis = (match.group("vis") or "").strip()
                raw = match.group("path").strip()
                paths = expand(raw)
                root = paths[0][0]
                if not is_plain(raw):
                    plain = False
                items.append((vis, root, raw))
                indexed.append((line_no, vis, root, raw))
        if not plain or block_has_attribute(lines, sub_runs):
            # 含属性 / `as` / glob 的条目**本来就不能**与同根它项合成一条
            # （属性只作用于它自己那一行，重命名/glob 同理），报它们只会制造噪音。
            # 因此整块跳过；`--verbose` 时列出，便于人工确认。
            if skipped is not None:
                skipped.append(
                    Violation(
                        path,
                        indexed[0][0] + 1,
                        f"跳过（含属性/重命名/glob）：{len(indexed)} 条",
                    )
                )
            i = next_i
            continue

        roots = [root for _, _, root, _ in indexed]
        duplicated = sorted({r for r in roots if roots.count(r) > 1})
        if not duplicated:
            i = next_i
            continue

        # 需要修复：按「根」合并，并保留子组（空行）分组
        groups: dict[tuple[str, str], list[list[str]]] = {}
        order: list[tuple[str, str]] = []
        group_sub: dict[tuple[str, str], int] = {}
        for sub_index, run in enumerate(sub_runs):
            for _, match in run:
                vis = (match.group("vis") or "").strip()
                raw = match.group("path").strip()
                paths = expand(raw)
                root = paths[0][0]
                key = (vis, root)
                if key not in groups:
                    groups[key] = []
                    order.append(key)
                    group_sub[key] = sub_index
                groups[key].extend([path[1:] for path in paths])

        if not fix:
            violations.append(
                Violation(
                    path,
                    indexed[0][0] + 1,
                    f"同一块内重复的 crate 根 {duplicated}（可用 --fix 自动合并）",
                )
            )
            i = next_i
            continue

        # 生成新块：按子组归位，保持「系统 / 外部 / crate 内」的视觉分组
        by_sub: dict[int, list[str]] = {}
        for vis, root in order:
            key = (vis, root)
            body = render_tree(build_tree(groups[key]))
            prefix = f"{vis} " if vis else ""
            by_sub.setdefault(group_sub[key], []).append(f"{prefix}use {root}::{body};")
        new_lines: list[str] = []
        sub_indices = sorted(by_sub)
        for position, sub_index in enumerate(sub_indices):
            if position:
                new_lines.append("")
            new_lines.extend(by_sub[sub_index])
        indent = USE_LINE.match(lines[indexed[0][0]]).group("indent")
        new_lines = [indent + line.lstrip() if line else line for line in new_lines]

        lines[i:next_i] = new_lines
        changed = True
        violations.append(
            Violation(path, indexed[0][0] + 1, f"已合并重复的 crate 根 {duplicated}")
        )
        i = i + len(new_lines)

    if changed and fix:
        new_text = "\n".join(lines)
        if had_trailing_newline and not new_text.endswith("\n"):
            new_text += "\n"
        path.write_text(new_text, encoding="utf-8")
    return violations, changed


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def iter_rust_files(roots: list[Path]):
    for root in roots:
        if root.is_file() and root.suffix == ".rs":
            yield root
            continue
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.rs")):
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            yield path


def git_dirty(paths: list[Path]) -> list[str]:
    """返回这些路径里「有未提交改动（含未跟踪）」的 `git status` 行；不适用时返回空。

    用途：`--fix` 前确认存在可回退点（见 AGENTS.md §5）。不在 git 仓库 / 没装 git
    时返回空列表，即跳过这项检查。
    """
    if not paths or shutil.which("git") is None:
        return []
    try:
        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=paths[0].parent,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        return []
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain", "--", *[str(path) for path in paths]],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (subprocess.CalledProcessError, OSError):
        return []
    return [line for line in out.splitlines() if line.strip()]


def rustfmt(files: list[Path]) -> None:
    if not files or shutil.which("rustfmt") is None:
        return
    for path in files:
        subprocess.run(
            ["rustfmt", "--edition", "2024", str(path)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="检查/自动修复 Rust `use` 语句的单根嵌套写法（AGENTS.md §5）",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="只检查（默认）")
    mode.add_argument("--fix", action="store_true", help="自动修复")
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="同时列出因含属性/重命名/glob 而被跳过的块（不影响退出码）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="即使待改文件有未提交改动也继续修复（默认拒绝，见 AGENTS.md §5）",
    )
    parser.add_argument(
        "--no-rustfmt",
        action="store_true",
        help="修复后不调用 rustfmt（默认会调用，用于排版）",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="要处理的文件或目录；默认 = 本脚本所在目录",
    )
    args = parser.parse_args(argv)

    roots = [Path(p) for p in args.paths] if args.paths else [here]
    fix = bool(args.fix)
    files = [p for p in iter_rust_files(roots)]

    if fix:
        # 先干跑一遍，找出「确实会被改动」的文件——只对它们要求可回退点。
        pending: list[Path] = []
        for path in files:
            try:
                violations, _ = process_file(path, False)
            except OSError:
                continue
            if violations:
                pending.append(path)
        dirty = git_dirty(pending)
        if dirty and not args.force:
            print("拒绝修复：以下文件存在未提交改动（含未跟踪），先建立可回退点再动手：")
            for line in dirty:
                print(f"  {line}")
            print(
                "\n请先 `git commit` 或 `git stash -u`（见 AGENTS.md §5），"
                "确认要直接改再加 `--force`。"
            )
            return 2

    all_violations: list[Violation] = []
    skipped_blocks: list[Violation] = []
    changed_files: list[Path] = []
    for path in files:
        try:
            violations, changed = process_file(
                path, fix, skipped_blocks if args.verbose else None
            )
        except OSError as error:  # 读不了就报出来，别静默跳过
            print(f"无法处理 {path}: {error}", file=sys.stderr)
            continue
        all_violations.extend(violations)
        if changed:
            changed_files.append(path)

    for violation in all_violations:
        print(violation)

    if args.verbose and skipped_blocks:
        print(f"\n跳过 {len(skipped_blocks)} 个块（含属性/重命名/glob，本工具不动它们）：")
        for block in skipped_blocks:
            print(f"  {block}")

    if fix:
        if changed_files and not args.no_rustfmt:
            rustfmt(changed_files)
        print(
            f"\n已处理 {len(files)} 个文件，改动 {len(changed_files)} 个"
            + ("（并已 rustfmt）" if changed_files and not args.no_rustfmt else "")
        )
        return 0

    if all_violations:
        print(
            f"\n共 {len(all_violations)} 处不合规（扫描 {len(files)} 个文件）；"
            f"用 `--fix` 自动修复"
        )
        return 1
    print(f"全部合规（扫描 {len(files)} 个文件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
