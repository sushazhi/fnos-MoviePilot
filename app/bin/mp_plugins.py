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

几条必须守住的约束（都是踩过的坑，改动前先读）：

1. **属主**：`cmd/upgrade_callback` / `cmd/config_callback` 会以 **root** 调
   `cmd/main restart`（安装阶段脚本就是 root），于是本脚本在 root 下创建
   `plugins_src` 与里面的插件副本。后端以专用应用用户运行（`config/privilege`
   声明 run-as=package，用户名在 `TRIM_USERNAME`），属主不对就写不进插件目录：
   插件安装/更新 PermissionError，umask 偏紧时连 `plugin_dir.is_dir()` 都是假 ——
   又回到「插件文件缺失」。所以 root 下一律 `chown` 回应用用户；非 root 下必须
   确认目录真的可写。`os.access(W_OK)` 对 root 恒为真，不能拿它当判据。
2. **原子落位**：每个插件先复制到同目录临时名（`.incoming-*`）再 `os.replace`。
   直接往目标名复制，失败会留下"半个插件"，而后续运行只看名字在不在 —— 半份
   副本会被当成完整副本永久保留，源码树那份又已被换成软链，等于永久损坏。
   宿主兼容入口 `__init__.py` 同理必须原子写（写坏会变成 SyntaxError）。
3. **兼容入口只是命名空间标记**：`app/plugins/__init__.py` 的正文就是一个
   docstring（上游 `runtime/compat/imports.py` 会在运行时重写它，并留下
   `__init__.py.legacy-bak`）。所以它缺失不影响导入（普通目录会成为命名空间包），
   绝不因为它就判失败；但源码树里那份永远优先（旧版不得遮蔽新版）。
4. **失败要么可回滚、要么说清楚**：迁移只复制不移动，链接失败时把本次刚复制进
   持久目录的条目撤掉（否则下次运行会拿"上次失败的半成品"当准），源码树保持原样；
   软链失败一律回滚成原来的真实目录，绝不留一个没有 `app/plugins` 的源码树。
5. **断链要自愈**：软链目标目录被删（断链）时 `import app.plugins` 直接
   ModuleNotFoundError，后端插件全废。这里把树恢复成真实目录（空目录也是合法
   命名空间包），让应用能启动、用户能在插件页重装。

只复制不移动（链接成功后源码树里的旧目录才随暂存目录一起清掉）；纯标准库、幂等、
可重复执行；不删除任何"可能是唯一副本"的东西。

用法：
    python mp_plugins.py --check      # 只检查，未持久化时退出码 1
    python mp_plugins.py              # 检查并修复

退出码：
    0 = 已就位（或修复成功）
    1 = 未持久化 / 修复失败（可重试，或等下一次装包）
    2 = 用法或环境非法（持久目录推导不出来、落在源码树里、父目录不存在等）
    3 = 目录已恢复可用，但需要人工介入（插件源码已丢失需重装、软链指向别处等）

环境变量：
    MP_SRC          后端源码目录（${TRIM_APPDEST}/mp）
    CONFIG_DIR      MoviePilot 配置目录（${TRIM_PKGVAR}/config）
    MP_PLUGINS_DIR  持久插件目录（覆盖按 CONFIG_DIR 推导的默认值）
    TRIM_USERNAME   fnOS 注入的应用运行用户（root 下 chown 用；缺省 moviepilot）
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

# app/plugins 下不属于插件、也不参与迁移的固定名字
EXCLUDE = {"__init__.py", "__pycache__", ".DS_Store"}
# 宿主兼容入口：随宿主版本变化，必须以包内那份为准（但缺了也不算故障，见文件头）
SHIM_NAME = "__init__.py"
# 复制插件时丢弃的构建产物（版本无关，复制过去只会让排查误判）
COPY_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")
# 临时/归档前缀：中断的复制、以及"未能迁移"的旧目录归档
INCOMING_PREFIX = ".incoming-"
LEFTOVER_PREFIX = ".leftover-"

RC_OK = 0
RC_FAIL = 1
RC_USAGE = 2
RC_ACTION = 3


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


def is_ignored(name: str) -> bool:
    """判断 app/plugins 下的条目是否为"非插件"内容。

    `__init__.py.legacy-bak` 是上游兼容层自己留下的备份，`.incoming-*` / `.leftover-*`
    是本脚本的中间产物，都不该被当成插件统计或迁移。
    """
    return (name in EXCLUDE
            or name.startswith(INCOMING_PREFIX)
            or name.startswith(LEFTOVER_PREFIX)
            or name.startswith(SHIM_NAME + "."))


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
    会把"软链还在、目标目录已被人删掉"误判成已就位 —— 那时 `app.plugins` 根本读不
    出来，正是最需要报出来的状态。
    """
    if not plugins_dir.is_symlink() or not persist.is_dir():
        return False
    try:
        return os.path.realpath(plugins_dir) == os.path.realpath(persist)
    except OSError:
        return False


def plugin_names(root: Path) -> List[str]:
    """列出目录下真正的插件项（排除兼容入口、备份与中间产物）。"""
    try:
        return sorted(p.name for p in root.iterdir() if not is_ignored(p.name))
    except OSError:
        # 只用于日志与统计；列不出来不能把已经做好的修复变成异常
        return []


# ---------------------------------------------------------------------------
# 属主：root 场景下必须把插件目录交回应用运行用户
# ---------------------------------------------------------------------------
def app_identity() -> Optional[Tuple[int, int]]:
    """返回应用运行用户的 `(uid, gid)`：`$TRIM_USERNAME` > `moviepilot`。

    只在 POSIX 上有意义（Windows 没有属主概念），拿不到时返回 `None`。
    """
    if not hasattr(os, "geteuid"):
        return None
    try:
        import pwd
    except ImportError:
        return None
    candidates = [os.environ.get("TRIM_USERNAME") or "", "moviepilot"]
    for name in candidates:
        if not name:
            continue
        try:
            entry = pwd.getpwnam(name)
        except KeyError:
            continue
        return (entry.pw_uid, entry.pw_gid)
    return None


def _chown_tree(path: Path, uid: int, gid: int) -> int:
    """把 path（含子树）的属主改成 uid:gid，返回改动项数。

    `follow_symlinks=False`：插件目录里可能有软链，改软链本身而不是它指向的目标。
    """
    if not hasattr(os, "chown"):
        return 0
    targets = [path]
    for root, dirs, files in os.walk(str(path)):
        targets.extend(Path(root) / name for name in dirs + files)
    changed = 0
    for item in targets:
        try:
            st = os.lstat(str(item))
            if (st.st_uid, st.st_gid) != (uid, gid):
                os.chown(str(item), uid, gid, follow_symlinks=False)
                changed += 1
        except OSError:
            continue
    return changed


def ensure_usable(persist: Path) -> bool:
    """确保持久目录对**应用运行用户**可用（可写）。

    root 下（安装/升级回调会以 root 调 `cmd/main restart`）只能靠 chown 兜底：
    `os.access(W_OK)` 对 root 恒为真，拿它当判据会得到一个"root 能写、应用写不进去"
    的假 OK；非 root 下则直接验可写性。
    """
    if not hasattr(os, "geteuid"):
        return True
    if os.geteuid() == 0:
        ident = app_identity()
        if ident is None:
            log("错误: 以 root 运行但无法确定应用运行用户（TRIM_USERNAME / moviepilot），"
                "插件目录可能不可写")
            return False
        changed = _chown_tree(persist, *ident)
        if changed:
            log(f"  已把 {changed} 项插件文件的属主改回 uid {ident[0]}")
        return True
    if os.access(str(persist), os.W_OK):
        return True
    log(f"错误: 持久插件目录不可写（当前 uid {os.geteuid()}）: {persist}")
    return False


# ---------------------------------------------------------------------------
# 文件操作
# ---------------------------------------------------------------------------
def _remove(path: Path) -> None:
    """删除文件、软链或目录（尽力而为，不抛）。"""
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(str(path), ignore_errors=True)
    except OSError:
        pass


def _copy_entry(src: Path, dst: Path) -> None:
    """把 src 原子地复制成 dst：先复制到同目录临时名，再 `os.replace` 落位。

    目标名只在复制**完整成功**之后才出现，避免"半个插件"被后续运行当成完整副本
    （源码树那份随后会被换成软链，半份副本就成了唯一副本）。
    """
    tmp = dst.parent / f"{INCOMING_PREFIX}{os.getpid()}-{dst.name}"
    _remove(tmp)
    try:
        if src.is_dir() and not src.is_symlink():
            shutil.copytree(str(src), str(tmp), symlinks=True, ignore=COPY_IGNORE)
        else:
            shutil.copy2(str(src), str(tmp), follow_symlinks=False)
        os.replace(str(tmp), str(dst))
    except BaseException:
        _remove(tmp)
        raise


def _cleanup_incoming(persist: Path) -> None:
    """清掉上次被中断的复制残留（`.incoming-*`）。

    安全前提：临时名只在源码树那份还在时存在，且落位用 `os.replace`；因此任何
    残留都不可能是某插件的唯一副本。
    """
    try:
        entries = list(persist.iterdir())
    except OSError:
        return
    for item in entries:
        if item.name.startswith(INCOMING_PREFIX):
            log(f"  清理中断的复制残留 {item.name}")
            _remove(item)


def _refresh_shim(plugins_dir: Path, persist: Path) -> bool:
    """用包内那份刷新宿主兼容入口，返回持久目录里是否**有**该入口。

    只有 `plugins_dir` 还是真实目录时才有"包内那份"可取；它已经是软链时，链接里的
    `__init__.py` 就是持久目录里那份，没有第二份可比。写入必须是原子的：写坏这个
    文件会变成 SyntaxError，比不刷新更糟。缺它不影响导入（命名空间包），所以这里
    失败只告警、不判失败。
    """
    host_shim = plugins_dir / SHIM_NAME
    if plugins_dir.is_symlink() or not host_shim.is_file():
        return (persist / SHIM_NAME).is_file()
    tmp = persist / f"{INCOMING_PREFIX}{os.getpid()}-{SHIM_NAME}"
    try:
        shutil.copy2(str(host_shim), str(tmp))
        os.replace(str(tmp), str(persist / SHIM_NAME))
    except OSError as e:
        _remove(tmp)
        log(f"警告: 刷新 {SHIM_NAME} 失败（不影响导入）: {e}")
    return (persist / SHIM_NAME).is_file()


def _migrate(plugins_dir: Path, persist: Path) -> Tuple[List[Path], List[str]]:
    """把源码树里的真实插件复制进持久目录，返回 (本次新建的目标, 跳过的名字)。

    跳过判据是"目标已存在（含软链）"：落位用 `os.replace` 原子完成，所以已存在的
    目标一定是完整副本，可以直接以它为准。
    """
    created: List[Path] = []
    skipped: List[str] = []
    for item in sorted(plugins_dir.iterdir()):
        if is_ignored(item.name):
            continue
        target = persist / item.name
        if target.exists() or target.is_symlink():
            skipped.append(item.name)
            continue
        _copy_entry(item, target)
        log(f"  迁移插件源码 {item.name}")
        created.append(target)
    return created, skipped


def _rollback_created(created: List[Path]) -> None:
    """撤掉本次刚复制进持久目录的条目（链接失败时用）。

    不撤的话，下次修复会把"上次失败的这批"当成持久副本优先保留，反而把源码树里
    更新的那份丢掉 —— 表现为插件被悄悄回退到旧版本。
    """
    for target in created:
        if target.exists() or target.is_symlink():
            _remove(target)
            log(f"  回滚本次迁入的 {target.name}")


def _clear_recreated_dir(plugins_dir: Path) -> bool:
    """软链建立前发现 `plugins_dir` 又被创建时的处理。

    返回 True 表示已清成可建链的状态。空目录直接删掉；非空（并发写入/插件安装
    正在往里写）则不动它，由调用方改为"合并后放弃本次链接"，绝不覆盖别人的数据。
    """
    if not (plugins_dir.exists() or plugins_dir.is_symlink()):
        return True
    try:
        if plugins_dir.is_dir() and not plugins_dir.is_symlink() and not any(plugins_dir.iterdir()):
            plugins_dir.rmdir()
            return True
    except OSError:
        return False
    return False


def _merge_stash(stash: Path, plugins_dir: Path) -> None:
    """把暂存目录里的条目合并回 `plugins_dir`（链接失败且目标被占用时的兜底）。"""
    try:
        plugins_dir.mkdir(parents=True, exist_ok=True)
        for item in stash.iterdir():
            target = plugins_dir / item.name
            if target.exists() or target.is_symlink():
                continue
            shutil.move(str(item), str(target))
    except OSError as e:
        log(f"错误: 合并暂存目录失败: {e}")


def _link(plugins_dir: Path, persist: Path) -> int:
    """把真实目录换成指向持久目录的软链，失败时把原目录放回去。

    先改名再建链：任何一步失败都能恢复成"原来的真实目录"，绝不会留下一个没有
    `app/plugins` 的源码树。暂存目录名用 `mkdtemp` 生成，保证唯一（重名会让
    `os.rename` 直接报错，Windows 上目标存在即失败）。
    """
    try:
        stash = Path(tempfile.mkdtemp(dir=str(plugins_dir.parent), prefix=".plugins.old-"))
        os.rmdir(str(stash))          # mkdtemp 建的是空目录，rename 需要目标不存在
    except OSError as e:
        log(f"错误: 无法准备暂存目录: {e}")
        return RC_FAIL
    try:
        os.rename(str(plugins_dir), str(stash))
    except OSError as e:
        log(f"错误: 无法移开旧插件目录 {plugins_dir}: {e}")
        return RC_FAIL
    # rename 之后到建链之前有个空窗：后端的插件安装/别的进程可能又把目录建出来。
    # 非空说明有人在写，宁可这次不链接，也不能覆盖或丢弃它。
    if not _clear_recreated_dir(plugins_dir):
        log("警告: 建立软链前 app/plugins 又被重新创建且非空，本次放弃链接")
        _merge_stash(stash, plugins_dir)
        _remove(stash)
        return RC_FAIL
    try:
        os.symlink(str(persist), str(plugins_dir), target_is_directory=True)
    except OSError as e:
        try:
            os.rename(str(stash), str(plugins_dir))
        except OSError:
            # 目标名又被占用：改成合并，优先保住插件
            log(f"警告: 回滚改名失败，改为把旧目录内容合并回 {plugins_dir}")
            _merge_stash(stash, plugins_dir)
            _remove(stash)
            return RC_FAIL
        log(f"错误: 建立软链失败（已回滚，插件位置不变）: {e}")
        return RC_FAIL
    # 旧目录里剩下的项只有在持久目录里都有同名条目时才敢删：漏掉一个就等于删了
    # 用户插件。判据用 exists()/is_symlink()（软链条目也算"有对应副本"）。
    unaccounted = []
    for name in plugin_names(stash):
        target = persist / name
        if not (target.exists() or target.is_symlink()):
            unaccounted.append(name)
    if unaccounted:
        # 不能留在源码树里：下一次 stage_replace / 装包会把这棵目录整个换掉，
        # 这些"还没安顿好"的条目会跟着一起消失。整体归档到持久目录里。
        archive = persist / f"{LEFTOVER_PREFIX}{os.path.basename(stash)}"
        try:
            shutil.move(str(stash), str(archive))
            log(f"警告: 旧目录里仍有未迁移项 {', '.join(unaccounted)}，"
                f"已归档到 {archive}")
        except OSError as e:
            log(f"警告: 旧目录里仍有未迁移项 {', '.join(unaccounted)}，"
                f"归档失败（{e}），保留在 {stash}")
    else:
        _remove(stash)
    return RC_OK


def check(mp_src: Path, persist: Path) -> int:
    """只检查不改动：可用的持久化状态返回 0，需要修复返回 1，需人工介入返回 3。"""
    plugins_dir = plugin_root(mp_src)
    persistent = is_persisted(plugins_dir, persist)
    if plugins_dir.is_symlink():
        if persistent:
            if not (persist / SHIM_NAME).is_file():
                log(f"警告: 持久目录没有 {SHIM_NAME}（命名空间包，不影响导入）: {persist}")
            if not hasattr(os, "geteuid") or os.geteuid() != 0:
                if not os.access(str(persist), os.W_OK):
                    log(f"持久目录不可写: {persist}")
                    return RC_FAIL
            names = plugin_names(persist)
            log(f"OK 插件源码已持久化: app/plugins -> {persist}"
                f"（{len(names)} 个插件）")
            return RC_OK
        if not plugins_dir.exists():
            log(f"app/plugins 断链（软链目标不存在）: {os.path.realpath(plugins_dir)}")
        else:
            log(f"app/plugins 指向 {os.path.realpath(plugins_dir)}，"
                f"不是本应用的持久目录 {persist}（保持原样，需人工确认）")
        return RC_ACTION
    if not plugins_dir.is_dir():
        log(f"app/plugins 不存在（源码树不完整？）: {plugins_dir}")
        return RC_FAIL
    names = plugin_names(plugins_dir)
    log(f"app/plugins 还是源码树里的真实目录（{len(names)} 个插件），"
        "装包/自更新会把它们一起换掉")
    return RC_FAIL


def _heal_dangling(plugins_dir: Path, persist: Path) -> int:
    """把断链的 `app/plugins` 变回真实目录（空目录即合法的命名空间包）。"""
    target = os.path.realpath(plugins_dir)
    if os.path.realpath(persist) != target:
        log(f"警告: app/plugins 断链且目标 {target} 不是本应用的持久目录；保持原样，需人工确认")
        return RC_ACTION
    try:
        os.unlink(str(plugins_dir))
        plugins_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log(f"错误: 断链恢复失败: {e}")
        return RC_FAIL
    # root 下（安装/升级回调会以 root 调 cmd/main restart）新建的目录属主是 root，
    # 后端就写不进插件了 —— 这里同样交回应用运行用户
    ensure_usable(plugins_dir)
    log(f"错误: 插件源码目录断链（{target} 不存在）——已把 app/plugins 恢复成真实目录，"
        f"应用可以启动；插件源码本身已丢失，需在插件页重装（下次装包会带回宿主兼容入口）")
    return RC_ACTION


def repair(mp_src: Path, persist: Path) -> int:
    """检查并修复：插件源码落到持久目录，源码树用软链指过去。"""
    plugins_dir = plugin_root(mp_src)

    if is_persisted(plugins_dir, persist):
        if not ensure_usable(persist):
            return RC_FAIL
        _refresh_shim(plugins_dir, persist)
        _cleanup_incoming(persist)
        log(f"插件源码目录已就位: app/plugins -> {persist}"
            f"（{len(plugin_names(persist))} 个插件）")
        return RC_OK

    if plugins_dir.is_symlink():
        if not plugins_dir.exists():
            return _heal_dangling(plugins_dir, persist)
        log(f"警告: app/plugins 已是指向 {os.path.realpath(plugins_dir)} 的软链，"
            f"与本应用的持久目录 {persist} 不一致；保持原样，需人工确认")
        return RC_ACTION

    if not plugins_dir.is_dir():
        log(f"错误: 源码树缺少 app/plugins 目录: {plugins_dir}")
        return RC_FAIL

    # 不要在"连父目录都不存在"时层层建目录：那通常是数据卷没挂上，凭空新建只会
    # 在挂载回来后被盖住，插件看去来像是"又丢了"。
    if not persist.parent.is_dir():
        log(f"错误: 持久目录的上级不存在（数据卷未挂载？）: {persist.parent}")
        return RC_USAGE
    try:
        persist.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log(f"错误: 无法创建持久插件目录 {persist}: {e}")
        return RC_FAIL
    if not ensure_usable(persist):
        return RC_FAIL
    _cleanup_incoming(persist)
    _refresh_shim(plugins_dir, persist)

    try:
        created, skipped = _migrate(plugins_dir, persist)
    except OSError as e:
        # 复制失败：源码树完全没动过（落位是原子的，半份副本已被清掉），直接放弃
        log(f"错误: 迁移插件源码失败（源码树保持原样）: {e}")
        return RC_FAIL
    for name in skipped:
        log(f"  持久目录已有 {name}，保留持久那份（源码树里这份不迁移）")

    if _link(plugins_dir, persist) != RC_OK:
        _rollback_created(created)
        return RC_FAIL
    # 新建进来的副本可能带着安装者的属主（root 跑安装回调时）
    if not ensure_usable(persist):
        return RC_FAIL
    log(f"插件源码已持久化: app/plugins -> {persist}"
        f"（本次迁入 {len(created)} 个，共 {len(plugin_names(persist))} 个插件）")
    return RC_OK


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
        return RC_USAGE
    mp_src = mp_src.resolve()

    persist = resolve_persist_dir(args.plugins_dir or os.environ.get("MP_PLUGINS_DIR", ""))
    if persist is None:
        log("错误: 无法确定持久插件目录（请设置 CONFIG_DIR 或 MP_PLUGINS_DIR）")
        return RC_USAGE
    # 持久目录与源码树不能互相包含：两者都会被"整包替换"牵连，一方包含另一方就
    # 等于没做持久化（源码树在 @appcenter 下，其父目录同样每次装包都被换掉）。
    if persist == mp_src or mp_src in persist.parents or persist in mp_src.parents:
        log(f"错误: 持久插件目录与源码树不能互相包含: persist={persist} mp_src={mp_src}")
        return RC_USAGE

    if args.check:
        return check(mp_src, persist)
    return repair(mp_src, persist)


if __name__ == "__main__":
    sys.exit(main())
