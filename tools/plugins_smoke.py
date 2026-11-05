#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mp_plugins.py 插件源码持久化的离线冒烟测试（Windows 也能跑，无符号链接权限时 SKIP）。

背景：用户插件装在**源码树**的 app/plugins 下，而这一半目录随"应用中心整包装升级"
（整个 @appcenter/<app> 被换成新包）与"应用内自更新"（mp_updater 的 stage_replace
替换整个 app/）一起被换掉；插件记录却留在数据库里，于是插件页永远显示
「插件文件缺失」（前端 sourceMissing / 后端 PluginRuntimeStatus.SOURCE_MISSING，
判定条件只是 app/plugins/<id> 不是目录）。上游的插件备份/恢复要求 /.dockerenv，
原生版一次都不会触发。本测试覆盖修复脚本的每一条边界：

  A 首次修复：真实 app/plugins -> 插件源码落持久目录，源码树变软链，内容一致
  B 兼容入口刷新：宿主 __init__.py 变化后必须以包内那份为准（旧版不得遮蔽新版符号）
  C 幂等：已就位时不再改动、不重复迁移
  D 整树替换（装包/自更新）：新树里 app/plugins 只剩 __init__.py -> 软链重建、插件不丢
  E 同名冲突：持久目录已有同名插件时以持久那份为准，绝不被源码树里那份覆盖
  F 迁移失败：复制插件失败时必须保持源码树原样（不回滚成"目录没了"）
  G 边界：持久目录落在源码树内 / 源码树结构异常 / 无法推导持久目录 -> 退出码 2
  H 软链指向别处：不擅自改动，退出码 1
  I --check 语义：未持久化 1，持久化后 0
"""
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SANDBOX = ROOT / ".local-build" / "_smoke" / "plugins"

sys.path.insert(0, str(ROOT / "app" / "bin"))
import mp_plugins as plugins  # noqa: E402

FAILS = []
SKIPS = []


def check(label, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + label
          + (f"  <- {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(label)


def skip_all(why):
    print(f"SKIP 全部用例  <- {why}")
    sys.exit(0)


def symlink_supported() -> bool:
    """探测本机能否创建目录软链（Windows 无权限时不能）。"""
    probe = SANDBOX / "_symlink-probe"
    target = SANDBOX / "_symlink-target"
    try:
        target.mkdir(parents=True, exist_ok=True)
        if probe.is_symlink() or probe.exists():
            probe.unlink()
        os.symlink(str(target), str(probe), target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        return False
    finally:
        try:
            if probe.is_symlink():
                probe.unlink()
        except OSError:
            pass


def make_tree(case):
    """构造一个干净的 {base, mp_src, plugins_dir, persist} 沙箱。"""
    base = SANDBOX / case
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
    mp_src = base / "mp"
    plugins_dir = mp_src / "app" / "plugins"
    plugins_dir.mkdir(parents=True)
    (mp_src / "version.py").write_text("APP_VERSION = '3.2.3'\n", encoding="utf-8")
    persist = base / "appdata" / "plugins_src"
    return base, mp_src, plugins_dir, persist


def add_shim(plugins_dir, text="from app.sdk import *  # host shim v1\n"):
    (plugins_dir / "__init__.py").write_text(text, encoding="utf-8")


def add_plugin(plugins_dir, name, body="class Plugin:\n    pass\n"):
    d = plugins_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "__init__.py").write_text(body, encoding="utf-8")
    (d / "sites").mkdir(exist_ok=True)
    (d / "sites" / "data.json").write_text('{"k": 1}\n', encoding="utf-8")
    return d


def run_cli(args):
    """以 CLI 方式跑一次（覆盖 main() 的参数与退出码路径）。"""
    old = sys.argv
    sys.argv = ["mp_plugins.py", *args]
    try:
        return plugins.main()
    finally:
        sys.argv = old


def leftovers(d):
    return [p.name for p in d.iterdir() if p.name.startswith(".plugins.old-")]


SANDBOX.mkdir(parents=True, exist_ok=True)
if not symlink_supported():
    skip_all("本机无法创建目录软链（Windows 需要开发者模式/管理员权限），"
             "该修复在 fnOS(Linux) 上依赖软链")

print("== A. 首次修复：真实目录 -> 持久目录 + 软链 ==")
base, mp_src, plugins_dir, persist = make_tree("first")
add_shim(plugins_dir)
add_plugin(plugins_dir, "autosignin", "class Plugin:\n    version = '1.0'\n")
add_plugin(plugins_dir, "gladossign")
rc = plugins.repair(mp_src, persist)
check("A1 修复成功", rc == 0, f"rc={rc}")
check("A2 源码树 app/plugins 变成软链", plugins_dir.is_symlink())
check("A3 软链指向持久目录", plugins.is_persisted(plugins_dir, persist))
check("A4 插件源码落到持久目录",
      (persist / "autosignin" / "__init__.py").is_file()
      and (persist / "gladossign" / "__init__.py").is_file())
check("A5 插件数据文件一并带上",
      (persist / "autosignin" / "sites" / "data.json").is_file())
check("A6 兼容入口落到持久目录", (persist / "__init__.py").is_file())
check("A7 内容一致",
      (persist / "autosignin" / "__init__.py").read_text(encoding="utf-8")
      == (plugins_dir / "autosignin" / "__init__.py").read_text(encoding="utf-8"))
check("A8 旧目录不留残骸", leftovers(mp_src / "app") == [])

print("== B. 兼容入口刷新（旧版不得遮蔽新版） ==")
new_shim = "from app.sdk import *  # host shim v2\n"
plugins_dir.unlink()          # 模拟下一次宿主换树：软链被真实目录取代
plugins_dir.mkdir()
add_shim(plugins_dir, new_shim)
rc = plugins.repair(mp_src, persist)
check("B1 修复成功", rc == 0, f"rc={rc}")
check("B2 持久目录里的兼容入口已更新为新版",
      (persist / "__init__.py").read_text(encoding="utf-8") == new_shim)
check("B3 插件目录没被这次换树带走",
      (persist / "autosignin" / "__init__.py").is_file())

print("== C. 幂等 ==")
before = sorted(str(p.relative_to(persist)) for p in persist.rglob("*"))
rc = plugins.repair(mp_src, persist)
after = sorted(str(p.relative_to(persist)) for p in persist.rglob("*"))
check("C1 再次修复返回 0", rc == 0, f"rc={rc}")
check("C2 持久目录内容不变", before == after)
check("C3 仍是软链", plugins.is_persisted(plugins_dir, persist))

print("== D. 整树替换（应用中心装包 / mp_updater stage_replace） ==")
# stage_replace 的真实形态：旧 app 改名进 .mp-backup，新树整体搬进来，插件只剩兼容入口
backup = base / ".mp-backup" / "20261105-000000"
shutil.move(str(mp_src / "app"), str(backup / "app"))
new_app = mp_src / "app"
(new_app / "plugins").mkdir(parents=True)
add_shim(new_app / "plugins")
(new_app / "main.py").write_text("# new host\n", encoding="utf-8")
check("D1 换树后新树里确实没有插件",
      plugins.plugin_names(new_app / "plugins") == [])
rc = plugins.repair(mp_src, persist)
check("D2 修复成功", rc == 0, f"rc={rc}")
check("D3 软链重建", plugins.is_persisted(new_app / "plugins", persist))
check("D4 插件没丢（autosignin / gladossign 都在）",
      plugins.plugin_names(persist) == ["autosignin", "gladossign"],
      f"names={plugins.plugin_names(persist)}")

print("== E. 同名冲突：以持久那份为准 ==")
mid = new_app / "plugins"
mid.unlink()
mid.mkdir()
add_shim(mid)
add_plugin(mid, "autosignin", "class Plugin:\n    version = 'OLD-FROM-TREE'\n")
add_plugin(mid, "customsites")
rc = plugins.repair(mp_src, persist)
check("E1 修复成功", rc == 0, f"rc={rc}")
check("E2 持久那份没被源码树里那份覆盖",
      "OLD-FROM-TREE" not in (persist / "autosignin" / "__init__.py").read_text(encoding="utf-8"))
check("E3 新增插件被迁入", (persist / "customsites" / "__init__.py").is_file())
check("E4 无旧目录残骸", leftovers(new_app) == [])
check("E5 迁移后源码树仍是软链", plugins.is_persisted(mid, persist))

print("== F. 迁移失败必须保持源码树原样 ==")
base6, mp_src6, plugins_dir6, persist6 = make_tree("fail")
add_shim(plugins_dir6)
add_plugin(plugins_dir6, "aaa")
add_plugin(plugins_dir6, "bbb")
real_copytree = shutil.copytree


def boom(src, dst, *a, **kw):
    if str(src).endswith("bbb"):
        raise OSError("disk full (simulated)")
    return real_copytree(src, dst, *a, **kw)


plugins.shutil.copytree = boom
try:
    rc = plugins.repair(mp_src6, persist6)
finally:
    plugins.shutil.copytree = real_copytree
check("F1 迁移失败返回 1", rc == 1, f"rc={rc}")
check("F2 源码树仍是真实目录（没有被换成软链）",
      plugins_dir6.is_dir() and not plugins_dir6.is_symlink())
check("F3 源码树里的插件都还在",
      plugins.plugin_names(plugins_dir6) == ["aaa", "bbb"],
      f"names={plugins.plugin_names(plugins_dir6)}")

print("== G. 边界：持久目录非法 / 源码树结构异常 ==")
base7, mp_src7, plugins_dir7, _ = make_tree("bounds")
add_shim(plugins_dir7)
rc = run_cli(["--mp-src", str(mp_src7), "--plugins-dir", str(mp_src7 / "inside")])
check("G1 持久目录落在源码树内 -> 2", rc == 2, f"rc={rc}")
bad = base7 / "not-a-tree"
bad.mkdir(parents=True, exist_ok=True)
rc = run_cli(["--mp-src", str(bad), "--plugins-dir", str(base7 / "persist")])
check("G2 源码树结构异常 -> 2", rc == 2, f"rc={rc}")
old_env = os.environ.pop("CONFIG_DIR", None)
old_env2 = os.environ.pop("MP_PLUGINS_DIR", None)
try:
    rc = run_cli(["--mp-src", str(mp_src7)])
finally:
    if old_env is not None:
        os.environ["CONFIG_DIR"] = old_env
    if old_env2 is not None:
        os.environ["MP_PLUGINS_DIR"] = old_env2
check("G3 既无 CONFIG_DIR 也无 --plugins-dir -> 2", rc == 2, f"rc={rc}")

print("== H. 软链指向别处：不擅自改动 ==")
base8, mp_src8, plugins_dir8, persist8 = make_tree("elsewhere")
other = base8 / "other"
other.mkdir(parents=True, exist_ok=True)
shutil.rmtree(plugins_dir8)
os.symlink(str(other), str(plugins_dir8), target_is_directory=True)
rc = plugins.repair(mp_src8, persist8)
check("H1 指向别处 -> 1", rc == 1, f"rc={rc}")
check("H2 没被改写", os.path.realpath(plugins_dir8) == os.path.realpath(other))

print("== I. --check 语义 ==")
base9, mp_src9, plugins_dir9, persist9 = make_tree("check")
add_shim(plugins_dir9)
add_plugin(plugins_dir9, "autosignin")
rc = run_cli(["--check", "--mp-src", str(mp_src9), "--plugins-dir", str(persist9)])
check("I1 未持久化 -> 1", rc == 1, f"rc={rc}")
plugins.repair(mp_src9, persist9)
rc = run_cli(["--check", "--mp-src", str(mp_src9), "--plugins-dir", str(persist9)])
check("I2 已持久化 -> 0", rc == 0, f"rc={rc}")

print("== J. cmd/main 的实际调用形态：只给 CONFIG_DIR ==")
base10, mp_src10, plugins_dir10, _ = make_tree("env-default")
add_shim(plugins_dir10)
add_plugin(plugins_dir10, "autosignin")
config_dir = base10 / "appdata" / "config"
config_dir.mkdir(parents=True, exist_ok=True)
old_cfg = os.environ.get("CONFIG_DIR")
old_pdir = os.environ.get("MP_PLUGINS_DIR")
os.environ["CONFIG_DIR"] = str(config_dir)
os.environ.pop("MP_PLUGINS_DIR", None)
try:
    rc = run_cli(["--mp-src", str(mp_src10)])
    expected = base10 / "appdata" / "plugins_src"
    check("J1 修复成功", rc == 0, f"rc={rc}")
    check("J2 持久目录落在 CONFIG_DIR 同级 plugins_src（应用数据目录，不随装包替换）",
          expected.is_dir() and (expected / "autosignin" / "__init__.py").is_file())
    check("J3 源码树指向它", plugins.is_persisted(plugins_dir10, expected))
    check("J4 持久目录不在源码树内", not str(expected).startswith(str(mp_src10) + os.sep))
finally:
    if old_cfg is not None:
        os.environ["CONFIG_DIR"] = old_cfg
    else:
        os.environ.pop("CONFIG_DIR", None)
    if old_pdir is not None:
        os.environ["MP_PLUGINS_DIR"] = old_pdir

print("== K. 测试自身不污染仓库工作区 ==")
# 曾出现过 persist 解析失败退化成 Path('.') 的缺陷：那会把插件的 shim/软链写进
# 当前工作目录（仓库根）。这里把它固化成断言，避免同类问题再溜过去。
check("K1 仓库根没有冒出 plugins_src", not (ROOT / "plugins_src").exists())
check("K2 仓库根没有冒出 __init__.py 兼容入口", not (ROOT / "__init__.py").exists())

print("== L. 软链 vs 更新器的整树操作（备份 / 清理 / 移动 / 回滚） ==")
# 设计前提：更新器从不对 app/plugins 本身动手，只对整个 app/ 做 rename/rmtree。
# 这里把该前提钉死：一旦有人改成"跟进软链"的实现，持久目录会被连带删掉 —— 那是
# 比原缺陷更严重的数据损失。
base11, mp_src11, plugins_dir11, persist11 = make_tree("semantics")
add_shim(plugins_dir11)
add_plugin(plugins_dir11, "autosignin")
plugins.repair(mp_src11, persist11)
backup11 = base11 / ".mp-backup" / "20261105-000000"
shutil.copytree(str(mp_src11 / "app"), str(backup11 / "app"), symlinks=True)
check("L1 备份树里的 app/plugins 仍是软链", (backup11 / "app" / "plugins").is_symlink())
shutil.rmtree(str(backup11))                      # prune_backups
check("L2 清理备份不会跟进软链删掉持久目录",
      (persist11 / "autosignin" / "__init__.py").is_file()
      and (persist11 / "__init__.py").is_file())
moved11 = base11 / "mp-moved"
os.rename(str(mp_src11), str(moved11))            # stage_replace: dst.rename(backup)
check("L3 整树移动后软链语义不变",
      (moved11 / "app" / "plugins").is_symlink()
      and plugins.is_persisted(moved11 / "app" / "plugins", persist11))
os.rename(str(moved11), str(mp_src11))            # rollback: backup.rename(dst)
check("L4 回滚后插件仍可用",
      plugins.is_persisted(mp_src11 / "app" / "plugins", persist11)
      and (mp_src11 / "app" / "plugins" / "autosignin" / "__init__.py").is_file())
shutil.rmtree(str(base11), ignore_errors=True)    # 收尾：删沙箱不能连带删持久目录之外的东西
check("L5 收尾清理沙箱后持久目录已随之消失（未越界）", not persist11.exists())

print("== M. 断链：绝不能报 OK ==")
base12, mp_src12, plugins_dir12, persist12 = make_tree("dangling")
add_shim(plugins_dir12)
shutil.rmtree(plugins_dir12)
os.symlink(str(persist12), str(plugins_dir12), target_is_directory=True)
check("M1 前置：确实是断链", plugins_dir12.is_symlink() and not plugins_dir12.exists())
check("M2 is_persisted 不认断链", not plugins.is_persisted(plugins_dir12, persist12))
rc = run_cli(["--check", "--mp-src", str(mp_src12), "--plugins-dir", str(persist12)])
check("M3 --check 返回 1", rc == 1, f"rc={rc}")
rc = plugins.repair(mp_src12, persist12)
check("M4 repair 返回 1（不谎报已就位）", rc == 1, f"rc={rc}")
check("M5 不擅自改动断链", plugins_dir12.is_symlink() and not persist12.exists())

print("== N. 已持久化但宿主兼容入口丢失 ==")
base13, mp_src13, plugins_dir13, persist13 = make_tree("noshim")
add_shim(plugins_dir13)
add_plugin(plugins_dir13, "autosignin")
plugins.repair(mp_src13, persist13)
(persist13 / "__init__.py").unlink()
rc = plugins.repair(mp_src13, persist13)
check("N1 repair 返回 1（app.plugins 已无法导入，不能报就位）", rc == 1, f"rc={rc}")
rc = run_cli(["--check", "--mp-src", str(mp_src13), "--plugins-dir", str(persist13)])
check("N2 --check 返回 1", rc == 1, f"rc={rc}")

print()
if SKIPS:
    print(f"SKIP {len(SKIPS)} 项：{', '.join(SKIPS)}")
if FAILS:
    print(f"FAILED {len(FAILS)}: {', '.join(FAILS)}")
    sys.exit(1)
print("ALL PASS")
