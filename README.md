# 检查 / 自动修复 Rust 的 `use` 语句布局

## 规则（本仓库约定）

> 使用 `use` 语句导入依赖时，所有外部 crate（包括 core 和 std）必须以
> **独立单根**形式出现且**只出现一次**。

也就是：同一个作用域里，`core` / `std` / `alloc` / `abs_art` / `crate` … 每个根
各占**一条** `use`，路径写成嵌套形式：

```rust
// 不合规：同一根散成多条
use core::fmt;
use core::time::Duration;

// 合规：一个根一条、路径嵌套
use core::{fmt, time::Duration};
```

`cargo fmt` 不管这条：rustfmt 的 import granularity 默认是 `Preserve`，它只排版、
不合并。所以需要本工具。

## 用法

```bash
python3 format_use_imports.py                 # 检查（默认；有违规则退出码 1）
python3 format_use_imports.py --check         # 同上
python3 format_use_imports.py --fix           # 自动修复，随后对改动的文件跑 rustfmt
python3 format_use_imports.py --fix --no-rustfmt
python3 format_use_imports.py PATH [PATH ...] # 只处理指定文件/目录
```

`--fix` 默认会**先要求可回退点**：若待改动的文件在 git 里有未提交改动（含未跟踪），
工具拒绝动手并提示先 `git commit` / `git stash -u`；确认要直接改时加 `--force`。
（不在 git 仓库里时这项检查自动跳过。）

不传路径时，默认扫描**本脚本所在目录**下的所有 `*.rs`（跳过 `target/` 等构建目录），
因此放在仓库根目录即可供该仓库内所有项目使用。

## 实现要点（为什么是安全的）

* 只合并**同一个 import 块**内的条目。一个「块」= 同一缩进下连续的 `use` 行与空行，
  遇到任何其它行（`}`、`fn`、注释…）即结束。作用域边界必然是「其它行」，所以
  跨作用域误合并**不可能发生**（两个函数的函数体导入之间至少隔着 `}` 与 `fn`）。
* 空行被当作**子组边界**保留：原本「系统 crate / 外部 crate / crate 内」的视觉分组
  不会被抹平，同一根的重复条目会被收拢到它**首次出现**的那个子组。
* 任一条目带有属性（`#[cfg(..)] use ..`）、重命名（`as`）或 glob（`*`）时，整个块
  跳过并报告「需手工整理」——不做可能有损的重写。
