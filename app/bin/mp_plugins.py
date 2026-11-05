#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MoviePilot 插件源码目录持久化与在位性修复
=========================================
用户插件装在**源码树**里（上游写死 `ROOT_PATH/app/plugins`），而这个目录会随宿主
代码一起被整体换掉：

  * fnOS 应用中心升级/重装：整个 `@appcenter/<app>` 换成新包；
  * 应用内自更新：`mp_updater.py` 的 `stage_replace()` 替换整个 `app/`。

插件记录却留在数据库（`user.db` 的 `UserInstalledPlugins`）与插件数据目录
（`config/plugins`）里，于是插件页永远显示「插件文件缺失」：前端 i18n 的
`sourceMissing`，对应后端 `PluginRuntimeStatus.SOURCE_MISSING` —— 判定条件只是
`plugin_dir.is_dir()` 为假（`app/adapters/system/plugin/dependency.py`）。

上游并非没有恢复机制，但它在原生部署下完全不会触发：`SystemChain.backup_plugins()`
/ `restore_plugins()` 与插件包的持久备份都以 `is_docker()`（存在 `/.dockerenv`）为
前提，fnOS 原生版没有这个文件，于是"备份/恢复"整条路都被跳过。

本脚本把插件源码落到应用数据目录（`TRIM_PKGVAR`，不随应用包替换），再把源码树的
`app/plugins` 做成指向它的软链：

    ${TRIM_PKGVAR}/plugins_src/             <- 真实插件源码（持久）
    <MP_SRC>/app/plugins -> .../plugins_src

此后无论 fpk 升级还是自更新换掉 `app/`，插件都还在；插件在运行时安装/更新
（写入 `app/plugins/...`）也直接落在持久目录里。

两条边界：
  * `app/plugins/__init__.py` 是宿主提供的**兼容入口**（属于源码，不是插件，
    内容随版本变化），每次都以包内那份刷新，绝不让旧版遮蔽新版 SDK 符号；
  * 其余内容一律保留在持久目录：持久目录里已有同名插件时以持久那份为准，
    绝不覆盖。

只复制不移动，任何一步失败都保持源码树原样；纯标准库、幂等、可重复执行。

用法：
    python mp_plugins.py --check      # 只检查，未持久化时退出码 1
    python mp_plugins.py              # 检查并修复

环境变量：
    MP_SRC          后端源码目录（${TRIM_APPDEST}/mp）
    CONFIG_DIR      MoviePilot 配置目录（${TRIM_PKGVAR}/config）
    MP_PLUGINS_DIR  持久插件目录（覆盖按 CONFIG_DIR 推导的默认值）
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Optional

# app/plugins 下不属于插件、也不参与迁移的名字
EXCLUDE = {"__init__.py", "__pycache__", ".DS_Store"}
# 宿主兼容入口：随宿主版本变化，必须以包内那份为准
SHIM_NAME = "__init__.py"
# 复制插件时丢弃的构建产物（版本无关，复制过去只会让排查误判）
COPY_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")


def log(msg: str) -> None:
    """输出一行日志（cmd/main 会把它并进 update.log）。

    极端 locale（POSIX/ASCII，stdout 是管道）下 `print` 会抛 UnicodeEncodeError，
    而这里崩掉等于整个修复被跳过；退回按 UTF-8 写原始字节，日志本身不能成为故障点。
    """
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(msg.encode("utf-8", "replace") + b"\n")
        sys.stdout.buffer.flush()


def plugin_root(mp_src: Path) -> Path:
    """返回源码树里的插件目录 `<MP_SRC>/app/plugins`。"""
    return Path(mp_src) / "app" / "plugins"


def resolve_persist_dir(explicit: str = "") -> Optional[Path]:
    """解析持久插件目录。

    优先级：显式参数 > `$MP_PLUGINS_DIR` > `CONFIG_DIR` 同级的 `plugins_src`。
    默认值刻意落在**应用数据目录**（`TRIM_PKGVAR`）而不是源码树里：源码树
    （`TRIM_APPDEST`）正是每次装包/自更新会被整体替换的那一半。
    无法从环境推导时返回 `None`，由调用方报错退出 —— 绝不能退回 `Path('.')`：
    那会把当前工作目录当成插件目录，往里面写东西。
    """
    if explicit:
        return Path(explicit).expanduser().resolve()
    config_dir = os.environ.get("CONFIG_DIR") or ""
    if config_dir:
        return (Path(config_dir).expanduser().resolve().parent / "plugins_src")
    return None


def is_persisted(plugins_dir: Path, persist: Path) -> bool:
    """判断 `app/plugins` 是否已经是指向**存在**的持久目录的软链。

    必须连目标目录一起验：`os.path.realpath` 对断链也会返回目标路径，只看路径相等
    会把"软链还在、目标目录已被人删掉"误判成已就位 —— 那时 `app.plugins` 兼容入口
    根本读不出来，正是最需要报出来的状态。
    """
    if not plugins_dir.is_symlink() or not persist.is_dir():
        return False
    try:
        return os.path.realpath(plugins_dir) == os.path.realpath(persist)
    except OSError:
        return False


def plugin_names(root: Path) -> list:
    """列出目录下真正的插件项（排除兼容入口与构建产物）。"""
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.name not in EXCLUDE)


def check(mp_src: Path, persist: Path) -> int:
    """只检查不改动：已持久化返回 0，需要修复返回 1。"""
    plugins_dir = plugin_root(mp_src)
    if plugins_dir.is_symlink():
        if is_persisted(plugins_dir, persist):
            if not (persist / SHIM_NAME).is_file():
                log(f"持久目录缺少 {SHIM_NAME}（宿主兼容入口），app.plugins 无法导入: {persist}")
                return 1
            names = plugin_names(persist)
            log(f"OK 插件源码已持久化: app/plugins -> {persist}"
                f"（{len(names)} 个插件）")
            return 0
        if not plugins_dir.exists():
            log(f"app/plugins 断链（软链目标不存在）: {os.path.realpath(plugins_dir)}")
        else:
            log(f"app/plugins 指向 {os.path.realpath(plugins_dir)}，"
                f"不是本应用的持久目录 {persist}（保持原样，需人工确认）")
        return 1
    if not plugins_dir.is_dir():
        log(f"app/plugins 不存在（源码树不完整？）: {plugins_dir}")
        return 1
    names = plugin_names(plugins_dir)
    log(f"app/plugins 还是源码树里的真实目录（{len(names)} 个插件），"
        "装包/自更新会把它们一起换掉")
    return 1


def _refresh_shim(plugins_dir: Path, persist: Path) -> bool:
    """用包内那份刷新宿主兼容入口，返回刷新后持久目录里是否有兼容入口。

    只有 `plugins_dir` 还是真实目录时才有"包内那份"可取；它已经是软链时，
    链接里的 `__init__.py` 就是持久目录里那份，没有第二份可比。
    """
    host_shim = plugins_dir / SHIM_NAME
    if not plugins_dir.is_symlink() and host_shim.is_file():
        try:
            shutil.copy2(str(host_shim), str(persist / SHIM_NAME))
        except OSError as e:
            log(f"错误: 刷新 {SHIM_NAME} 失败: {e}")
    return (persist / SHIM_NAME).is_file()


def _migrate(plugins_dir: Path, persist: Path) -> int:
    """把源码树里的真实插件**复制**进持久目录。

    只复制不移动：中途失败时源码树仍然完整，调用方可以直接放弃修复。
    持久目录已有同名插件时以持久那份为准（它才是上次真正跑起来的那份）。
    返回复制进来的数量；出现错误抛 OSError。
    """
    copied = 0
    for item in sorted(plugins_dir.iterdir()):
        if item.name in EXCLUDE:
            continue
        target = persist / item.name
        if target.exists() or target.is_symlink():
            log(f"  持久目录已有 {item.name}，保留持久那份（源码树里这份不迁移）")
            continue
        if item.is_dir() and not item.is_symlink():
            shutil.copytree(str(item), str(target), symlinks=True, ignore=COPY_IGNORE)
        else:
            shutil.copy2(str(item), str(target), follow_symlinks=False)
        log(f"  迁移插件源码 {item.name}")
        copied += 1
    return copied


def _link(plugins_dir: Path, persist: Path) -> int:
    """把真实目录换成指向持久目录的软链，失败时把原目录放回去。

    先改名再建链：任何一步失败都能恢复成"原来的真实目录"，绝不会留下一个
    没有 `app/plugins` 的源码树（后端 import 期的 `app.plugins` 兼容入口也在这里）。
    暂存目录名用 `mkdtemp` 生成，保证唯一：重名会让 `os.rename` 直接报错
    （Windows 上目标存在即失败），那等于把"插件位置"这件事交给时间戳碰运气。
    """
    try:
        stash = Path(tempfile.mkdtemp(dir=str(plugins_dir.parent), prefix=".plugins.old-"))
        # mkdtemp 建的是空目录，rename 需要目标不存在，先删掉占位
        os.rmdir(str(stash))
    except OSError as e:
        log(f"错误: 无法准备暂存目录: {e}")
        return 1
    try:
        os.rename(str(plugins_dir), str(stash))
    except OSError as e:
        log(f"错误: 无法移开旧插件目录 {plugins_dir}: {e}")
        return 1
    try:
        os.symlink(str(persist), str(plugins_dir), target_is_directory=True)
    except OSError as e:
        # Windows 无符号链接权限、只读文件系统等：必须回滚，宁可"插件会丢"也不能
        # 让源码树缺了 app/plugins
        try:
            os.rename(str(stash), str(plugins_dir))
        except OSError as re_:
            log(f"错误: 建立软链失败且回滚失败: {e} / {re_}")
            log(f"      请手工把 {stash} 改回 {plugins_dir}")
            return 1
        log(f"错误: 建立软链失败（已回滚，插件位置不变）: {e}")
        return 1
    # 旧目录里剩下的项只有在持久目录里都有对应副本时才敢删：漏掉一个就等于
    # 删了用户插件。判据是"同名条目在持久目录里存在"，而不是"我迁移过它"。
    unaccounted = [name for name in plugin_names(stash)
                   if not ((persist / name).is_dir() or (persist / name).is_file())]
    if unaccounted:
        log(f"警告: 旧目录里仍有未迁移项 {', '.join(unaccounted)}，保留在 {stash}")
    else:
        shutil.rmtree(str(stash), ignore_errors=True)
    return 0


def repair(mp_src: Path, persist: Path) -> int:
    """检查并修复：插件源码落到持久目录，源码树用软链指过去。"""
    plugins_dir = plugin_root(mp_src)
    if is_persisted(plugins_dir, persist):
        if not _refresh_shim(plugins_dir, persist):
            # 软链在、目标目录缺兼容入口：这时 app.plugins 根本 import 不出来，
            # 绝不能报"已就位"（这正是本脚本要消灭的那类静默失败）
            log(f"错误: 持久目录缺少 {SHIM_NAME}（宿主兼容入口），"
                f"app.plugins 无法导入: {persist}")
            return 1
        log(f"插件源码目录已就位: app/plugins -> {persist}")
        return 0
    if plugins_dir.is_symlink():
        if not plugins_dir.exists():
            log(f"错误: app/plugins 断链（软链目标 {os.path.realpath(plugins_dir)} 不存在）；"
                f"删除该软链后重新安装应用包即可恢复成源码树里的真实目录")
        else:
            log(f"警告: app/plugins 已是指向 {os.path.realpath(plugins_dir)} 的软链，"
                f"与本应用的持久目录 {persist} 不一致；保持原样，需人工确认")
        return 1
    if not plugins_dir.is_dir():
        log(f"错误: 源码树缺少 app/plugins 目录: {plugins_dir}")
        return 1

    try:
        persist.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log(f"错误: 无法创建持久插件目录 {persist}: {e}")
        return 1
    if not os.access(str(persist), os.W_OK):
        log(f"错误: 持久插件目录不可写: {persist}")
        return 1

    shim_ok = _refresh_shim(plugins_dir, persist)
    if not shim_ok and (plugins_dir / SHIM_NAME).is_file():
        # 包内有兼容入口却没能落到持久目录：迁移下去只会得到一个 import 不出来的
        # 插件目录，不如保持源码树原样（当前状态至少是能跑的）
        log("错误: 宿主兼容入口未能落到持久目录，放弃本次迁移（源码树保持原样）")
        return 1
    if not shim_ok:
        log(f"警告: 包内没有 {SHIM_NAME}（上游布局可能已变化），按命名空间包继续")
    try:
        copied = _migrate(plugins_dir, persist)
    except OSError as e:
        # 复制失败：源码树完全没动过，直接放弃本次修复
        log(f"错误: 迁移插件源码失败（源码树保持原样）: {e}")
        return 1
    if _link(plugins_dir, persist) != 0:
        return 1
    names = plugin_names(persist)
    log(f"插件源码已持久化: app/plugins -> {persist}"
        f"（本次迁入 {copied} 个，共 {len(names)} 个插件）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="MoviePilot 插件源码目录检查/修复")
    parser.add_argument("--check", action="store_true", help="只检查不修复")
    parser.add_argument("--mp-src", default=None, help="后端源码目录（默认取 $MP_SRC）")
    parser.add_argument("--plugins-dir", default=None,
                        help="持久插件目录（默认取 CONFIG_DIR 同级的 plugins_src）")
    args = parser.parse_args()

    mp_src = Path(args.mp_src or os.environ.get("MP_SRC") or "").expanduser()
    if not (mp_src / "app").is_dir():
        log(f"错误: 后端源码目录不存在或结构异常: {mp_src}")
        return 2
    mp_src = mp_src.resolve()

    persist = resolve_persist_dir(args.plugins_dir or os.environ.get("MP_PLUGINS_DIR", ""))
    if persist is None:
        log("错误: 无法确定持久插件目录（请设置 CONFIG_DIR 或 MP_PLUGINS_DIR）")
        return 2
    # 持久目录落在源码树里等于没做持久化：装包/自更新照样会连它一起换掉
    if str(persist).startswith(str(mp_src) + os.sep) or persist == mp_src:
        log(f"错误: 持久插件目录不能位于源码树内部: {persist}")
        return 2

    if args.check:
        return check(mp_src, persist)
    return repair(mp_src, persist)


if __name__ == "__main__":
    sys.exit(main())
