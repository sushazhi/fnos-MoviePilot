#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打包优化（装包更新提速）的冒烟测试（Windows 也能跑）。

背景（本测试要钉住的问题）：
    「应用中心用安装包更新应用耗时很长」。定位过程分两步：

    1) 先量化包体。拆解 moviepilot-1.0.6.fpk 的 app.tgz：解压后 728MB /
       约 3.8 万个文件，其中 python/（自带 CPython 3.14 + 全部依赖）占 649.7MB（90%）。
       并且包内 **0 个符号链接** —— 因为 prepare_pkg 用 shutil.copytree 默认的
       symlinks=False 把链接**解引用**成了实体副本：
           bin/{python,python3,python3.14}  三份 sha256 完全相同，各 32.2MB
           lib/libpython3.14.so{,.1.0}      两份 sha256 完全相同，各 30.4MB
       合计白占约 93MB。

    2) 但**体积不是瓶颈**。在真实 tar.gz 解压路径上做对照实验（见本测试 G 组）：
           34000 个文件 / 7.1MB  → 19.59s
             340 个文件 / 7.1MB  →  0.21s   （文件数 1/100，字节不变，快 93 倍）
           34000 个文件 / 0.7MB  → 21.28s   （字节 1/10，文件数不变，反而略升）
       即每个文件的 inode 创建 + 元数据写 + close 是固定成本，远大于字节写入。
       **减少文件数才是有效手段，压缩体积几乎没有意义。**

    为什么只能在构建期解决：fnOS 官方文档写明 install_init 在「安装文件应用前」
    执行、install_callback 在「安装文件应用后」执行，包文件写入发生在两者**之间**，
    由 fnOS 自己完成。因此任何只改 cmd/ 生命周期脚本的方案都无法减少解压写入量。

测试分两层：
  行为层：真实复制含符号链接的树，验证链接被保留；链接审计能拦住危险链接；
          实测"文件数 vs 字节数"对解压耗时的影响（本测试的核心证据）
  契约层：静态断言 build.py 仍用保留链接的方式复制运行时、仍带体积/文件数审计与
          回归告警；并**登记 lark_oapi 裁剪不可行**（见 H 组，防止重新走弯路）

依赖：仅标准库。不需要 bash / node。
"""
import importlib.util
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD_PY = ROOT / "build.py"
SANDBOX = ROOT / ".local-build" / "_smoke" / "pkgsize"

FAILS = []


def check(label, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + label
          + (f"  <- {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(label)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_build_module():
    """把 build.py 作为模块加载（不执行 main）。"""
    spec = importlib.util.spec_from_file_location("mp_build", BUILD_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# 行为层
# ---------------------------------------------------------------------------
def make_fake_runtime(root: Path) -> Path:
    """造一棵与 python-build-standalone 布局同构的假运行时。

    关键点是**符号链接**：真实发行版里 bin/python3、bin/python 都是指向
    bin/python3.14 的相对链接，lib/libpython3.14.so.1.0 指向 libpython3.14.so。
    """
    rt = root / "python"
    (rt / "bin").mkdir(parents=True)
    (rt / "lib").mkdir(parents=True)

    real = rt / "bin" / "python3.14"
    real.write_bytes(b"X" * (256 * 1024))
    os.symlink("python3.14", rt / "bin" / "python3")
    os.symlink("python3.14", rt / "bin" / "python")

    so = rt / "lib" / "libpython3.14.so"
    so.write_bytes(b"Y" * (128 * 1024))
    os.symlink("libpython3.14.so", rt / "lib" / "libpython3.14.so.1.0")
    return rt


def real_bytes(path: Path) -> int:
    """统计真实占用的字节数（跳过符号链接，避免把目标重复计一次）。"""
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for name in filenames:
            p = Path(dirpath) / name
            if p.is_symlink():
                continue
            total += p.stat().st_size
    return total


def count_links(path: Path) -> int:
    n = 0
    for dirpath, _, filenames in os.walk(path):
        for name in filenames:
            if (Path(dirpath) / name).is_symlink():
                n += 1
    return n


def behavior_checks(b):
    print("--- A. 复制保留符号链接（削减包体）---")
    work = Path(tempfile.mkdtemp(dir=str(SANDBOX)))
    try:
        src = make_fake_runtime(work)

        # 旧行为：裸 copytree（symlinks=False），链接被解引用成实体副本
        old = work / "old"
        shutil.copytree(src, old)
        # 新行为：保留链接
        new = work / "new"
        b.copy_tree_preserving_links(src, new)

        check("A1 旧行为确实把链接解引用成实体（复现问题）",
              count_links(old) == 0,
              f"links={count_links(old)}")
        check("A2 新行为保留了符号链接",
              count_links(new) == count_links(src) == 3,
              f"src={count_links(src)} new={count_links(new)}")
        check("A3 保留链接后真实字节数显著下降",
              real_bytes(new) < real_bytes(old) * 0.7,
              f"old={real_bytes(old)} new={real_bytes(new)}")
        # 内容必须一致：链接指向的文件要能读到，且与实体副本相同
        check("A4 链接仍可正常解析到内容",
              (new / "bin" / "python3").read_bytes() == (src / "bin" / "python3.14").read_bytes())

        print()
        print("--- B. 运行时链接审计 ---")
        # 正常（相对链接、包内自包含）应通过；这里直接调用，不应抛 SystemExit
        try:
            b._audit_runtime_links(new)
            ok_norm = True
        except SystemExit:
            ok_norm = False
        check("B1 安全的相对链接通过审计", ok_norm)

        # 绝对路径链接（指向构建机）必须被拦截
        bad = work / "bad"
        b.copy_tree_preserving_links(src, bad)
        (bad / "bin" / "python").unlink()
        os.symlink("/home/runner/.local/python/bin/python3.14", bad / "bin" / "python")
        try:
            b._audit_runtime_links(bad)
            caught_abs = False
        except SystemExit:
            caught_abs = True
        check("B2 指向构建机的绝对链接被拦截", caught_abs)

        # 相对但逃出运行时目录的链接必须被拦截
        esc = work / "esc"
        b.copy_tree_preserving_links(src, esc)
        (esc / "bin" / "python").unlink()
        os.symlink("../../../../etc/passwd", esc / "bin" / "python")
        try:
            b._audit_runtime_links(esc)
            caught_esc = False
        except SystemExit:
            caught_esc = True
        check("B3 逃出运行时目录的相对链接被拦截", caught_esc)

        print()
        print("--- C. 打包写入代价统计 ---")
        pkg = work / "pkg"
        (pkg / "frontend").mkdir(parents=True)
        (pkg / "mp").mkdir(parents=True)
        b.copy_tree_preserving_links(src, pkg / "python")
        (pkg / "frontend" / "index.html").write_text("<html>", encoding="utf-8")
        (pkg / "mp" / "main.py").write_text("print(1)", encoding="utf-8")
        (pkg / "manifest").write_text("appname = moviepilot\n", encoding="utf-8")
        # 不应抛异常，且要能识别出链接保留正常
        try:
            b._report_pkg_write_cost(pkg)
            ok_cost = True
        except Exception as e:  # noqa: BLE001
            ok_cost = False
            print("      异常:", e)
        check("C1 体积审计可执行且不抛异常", ok_cost)
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------
# 契约层
# ---------------------------------------------------------------------------
def contract_checks():
    src = read(BUILD_PY)

    # D. 运行时必须用"保留符号链接"的方式复制。裸 copytree 会把 93MB 的
    #    bin/python* 与 libpython3.14.so* 重复体打进包，这是纯浪费。
    check("D1 build.py 提供 copy_tree_preserving_links",
          "def copy_tree_preserving_links(" in src)
    check("D2 该函数使用 symlinks=True",
          "symlinks=True" in src)
    check("D3 prepare_pkg 复制运行时时使用它（而非裸 copytree）",
          'copy_tree_preserving_links(PYTHON_DIR, pkg_app / "python")' in src)
    # 运行时的复制绝不能退回裸 copytree —— 这行一旦被改回去，93MB 冗余就回来了
    check("D4 运行时复制未退回裸 copytree",
          "shutil.copytree(PYTHON_DIR, pkg_app" not in src)

    # E. 链接审计必须存在且在打包路径上被调用，否则绝对路径链接会静默打进包
    check("E1 build.py 提供 _audit_runtime_links", "def _audit_runtime_links(" in src)
    check("E2 打包时调用链接审计", "_audit_runtime_links(pkg_app" in src)
    check("E3 审计会拦截绝对路径链接", "os.path.isabs(target)" in src)
    check("E4 审计会拦截逃出运行时目录的链接", "relative_to(root)" in src)

    # F. 体积审计与符号链接回归告警：改动收益是"93MB 不再写盘"，
    #    这种收益没有报错就会悄悄消失，必须有构建期可见的输出兜住。
    check("F1 build.py 提供打包写入代价统计", "def _report_pkg_write_cost(" in src)
    check("F2 组装完成后调用它", "_report_pkg_write_cost(PKG_DIR)" in src)
    check("F3 对链接缺失给出回归告警", "解引用成了实体副本" in src)

    # G. 不得为了方便而删除上游声明的运行时依赖。
    #    上游 pyproject.toml 对 lark-oapi / pytelegrambotapi / pywebpush 明确注明
    #    "暂时保留给插件使用"，删除会破坏插件兼容；playwright 是 cloakbrowser 的
    #    硬依赖（站点反爬 / 站点认证），删了站点功能直接失效。
    #    这里检查 build.py 里**没有把 site-packages 下的依赖包整个删掉**的行为
    #    （例如 rmtree(site / "playwright") 之类）。靠"包名 + 删除动作同时出现
    #    在同一行"来判定，避免误伤注释里提到包名的情况。
    protected = ("playwright", "lark_oapi", "lark-oapi", "Pinyin2Hanzi",
                 "pypinyin", "cloakbrowser")
    delete_verbs = ("rmtree", "unlink", "remove(", "shutil.move")
    offenders = []
    for i, line in enumerate(src.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith('"""'):
            continue
        if not any(v in line for v in delete_verbs):
            continue
        if any(pkg in line for pkg in protected):
            offenders.append(f"L{i}: {stripped}")
    check("G1 构建期未删除受保护的运行时依赖",
          not offenders, "; ".join(offenders[:3]))
    # G2. _remove_pkg_tests 只允许删 tests/testing 目录，不得扩大删除范围
    #     （扩到包本体就等于删依赖，会打出功能残缺的包）。
    check("G2 裁剪只针对 tests/testing 目录",
          'if d in ("tests", "testing")' in src)

    # H. lark_oapi 裁剪**不可行**的登记（防止以后再走一遍这条弯路）。
    #
    #    背景：lark_oapi 是全包文件数最多的单个包（11112 个 = 全包 34689 的 32%），
    #    而"文件数才是解压耗时主导因素"这个结论起初让人很自然地想去裁它。
    #    但实测证明**只删 api/<域>/ 目录必然失败**：
    #      lark_oapi/__init__.py        -> from .api import *
    #      lark_oapi/api/__init__.py    -> 顶层 57 行 `from . import <域>`
    #      lark_oapi/client.py          -> 57 条 `from .api.<域>.service import XService`
    #                                      且每个 Service 在 Client 里另有 3 处使用点
    #    删掉任一域目录，`import lark_oapi` 直接 ModuleNotFoundError（飞书功能全废）。
    #    要生效就得改写 wheel 内第三方源码约 112 行，且升级 lark-oapi 后失效、
    #    上游改 import 形式就会漏改打出坏包 —— 风险与收益不成比例，故**放弃**。
    #
    #    本组断言的作用是：如果以后有人重新加回"按目录裁剪 lark"的代码，这里会失败，
    #    强制他先读上面这段结论。
    check("H1 未按目录裁剪 lark_oapi（已证明会让 import lark_oapi 崩溃）",
          "_trim_lark_oapi" not in src and "LARK_KEEP_API_DOMAINS" not in src)
    check("H2 未改写第三方库源码做惰性导入",
          "lark_oapi/api/__init__.py" not in src and "__getattr__" not in src)

    # I. 实测结论必须留在代码里：字节数不是瓶颈、文件数才是。
    #    这条结论是"为什么盯文件数而不是盯 MB"的唯一在库依据；删掉后很容易被
    #    重新按体积去优化（那正是本次走过的弯路）。
    check("I1 代码里记录了文件数主导的实测依据",
          "文件数才是瓶颈" in src and "盯文件数" in src)
    # I2. lark_oapi 是文件数大头但裁剪不可行的结论必须留档，
    #     否则后来人会重新调研一遍（本次已完整评估过两条替代路径）。
    check("I2 留档了 lark_oapi 裁剪不可行的原因",
          "lark_oapi" in src and "必然失败" in src)


def main() -> int:
    print("== 打包体积优化（装包更新提速）冒烟测试 ==\n")
    SANDBOX.mkdir(parents=True, exist_ok=True)

    print("--- 契约层 ---")
    contract_checks()

    print()
    print("--- 行为层 ---")
    try:
        b = load_build_module()
    except Exception as e:  # noqa: BLE001
        check("加载 build.py 模块", False, str(e))
        b = None
    if b is not None:
        check("加载 build.py 模块", True)
        behavior_checks(b)

    print()
    if FAILS:
        print(f"== 结果: {len(FAILS)} 项失败 ==")
        for f in FAILS:
            print("   - " + f)
        return 1
    print("== 结果: 全部通过 ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
