#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mp_plugins.py 插件源码持久化的离线冒烟测试（Windows 也能跑；需要软链的用例在
本机没有软链权限时逐条 SKIP，不需要软链的用例照跑，不会整份静默跳过）。

背景：用户插件装在**源码树**的 app/plugins 下，而这一半目录随"应用中心整包装升级"
（整个 @appcenter/<app> 被换成新包）与"应用内自更新"（mp_updater 的 stage_replace
替换整个 app/）一起被换掉；插件记录却留在数据库里，于是插件页永远显示
「插件文件缺失」（前端 sourceMissing / 后端 PluginRuntimeStatus.SOURCE_MISSING，
判定条件只是 app/plugins/<id> 不是目录）。上游的插件备份/恢复要求 /.dockerenv，
原生版一次都不会触发。本测试覆盖修复脚本的每一条边界：

  A 首次修复：真实 app/plugins -> 插件源码落持久目录，源码树变软链，内容一致
  B 兼容入口刷新：宿主 __init__.py 变化后必须以包内那份为准
  C 幂等：已就位时不再改动、不重复迁移
  D 整树替换（装包/自更新）：新树里 app/plugins 只剩 __init__.py -> 软链重建、插件不丢
  E 同名冲突：持久目录已有同名插件时以持久那份为准，绝不被源码树里那份覆盖
  F 迁移失败（复制一开始就失败）：必须保持源码树原样
  F2 复制中途失败：绝不能留下"半个插件"被后续运行当成完整副本（原子落位）
  G 边界：持久目录落在源码树内 / 源码树结构异常 / 推导不出持久目录 -> 退出码 2
  G4 反向边界：持久目录是源码树的**父目录**（整个 @appcenter 会被装包替换）-> 2
  H 软链指向别处：不擅自改动，退出码 3（需人工确认）
  I --check 语义：未持久化 1，持久化后 0
  J cmd/main 的实际调用形态：只给 CONFIG_DIR
  K 测试自身不污染仓库工作区
  L 真实更新器集成：直接驱动 mp_updater 的 stage_replace / rollback / prune_backups
  M 断链自愈：目标目录被删 -> app/plugins 恢复成真实目录（后端可导入），退出码 3
  N 缺兼容入口只是警告（命名空间包可导入）-> 退出码 0
  O 兼容入口写入原子性：中途失败不得留下写坏的 __init__.py（会变 SyntaxError）
  P 属主（仅 Linux + root）：root 下必须把持久目录 chown 回应用运行用户
"""
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SANDBOX = ROOT / ".local-build" / "_smoke" / "plugins"

os.environ.setdefault("MP_UPDATE_LOG", str(SANDBOX / "updater.log"))
sys.path.insert(0, str(ROOT / "app" / "bin"))
import mp_plugins as plugins  # noqa: E402

FAILS = []
SKIPS = []


def check(label, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + label
          + (f"  <- {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(label)


def skip(label, why):
    print(f"SKIP {label}  <- {why}")
    SKIPS.append(label)


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
    persist.parent.mkdir(parents=True, exist_ok=True)
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


def incoming(d):
    if not d.is_dir():
        return []
    return [p.name for p in d.iterdir() if p.name.startswith(".incoming-")]


SANDBOX.mkdir(parents=True, exist_ok=True)
LINKS = symlink_supported()
if not LINKS:
    print("注意: 本机无法创建目录软链（Windows 需要管理员/开发者模式），"
          "依赖软链的用例将逐条 SKIP；不需要软链的用例照跑。\n")

print("== A. 首次修复：真实目录 -> 持久目录 + 软链 ==")
base, mp_src, plugins_dir, persist = make_tree("first")
add_shim(plugins_dir)
add_plugin(plugins_dir, "autosignin", "class Plugin:\n    version = '1.0'\n")
add_plugin(plugins_dir, "gladossign")
rc = plugins.repair(mp_src, persist)
check("A1 修复成功", rc == 0, f"rc={rc}")
if LINKS:
    check("A2 源码树 app/plugins 变成软链", plugins_dir.is_symlink())
    check("A3 软链指向持久目录（原样核对 realpath）",
          plugins_dir.is_symlink()
          and os.path.realpath(plugins_dir) == os.path.realpath(persist),
          f"realpath={os.path.realpath(plugins_dir) if plugins_dir.is_symlink() else '-'}")
check("A4 插件源码落到持久目录",
      (persist / "autosignin" / "__init__.py").is_file()
      and (persist / "gladossign" / "__init__.py").is_file())
check("A5 插件数据文件一并带上",
      (persist / "autosignin" / "sites" / "data.json").is_file())
check("A6 兼容入口落到持久目录", (persist / "__init__.py").is_file())
check("A7 内容一致",
      (persist / "autosignin" / "__init__.py").read_text(encoding="utf-8")
      == "class Plugin:\n    version = '1.0'\n")
check("A8 无中间产物残留", incoming(persist) == [])
if LINKS:
    check("A9 旧目录不留残骸", leftovers(mp_src / "app") == [])
else:
    skip("A2/A3/A9", "本机无软链权限")

print("== B. 兼容入口刷新（旧版不得遮蔽新版） ==")
if not LINKS:
    skip("B", "需要软链")
else:
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

print("== D. 整树替换（应用中心装包 / mp_updater stage_replace） ==")
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
check("D4 插件没丢（autosignin / gladossign 都在）",
      plugins.plugin_names(persist) == ["autosignin", "gladossign"],
      f"names={plugins.plugin_names(persist)}")
if LINKS:
    check("D3 软链重建", plugins.is_persisted(new_app / "plugins", persist))

print("== E. 同名冲突：以持久那份为准 ==")
if not LINKS:
    skip("E", "需要软链")
else:
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

print("== F. 复制一开始就失败：源码树原样 ==")
base6, mp_src6, plugins_dir6, persist6 = make_tree("fail")
add_shim(plugins_dir6)
add_plugin(plugins_dir6, "aaa")
add_plugin(plugins_dir6, "bbb")
real_copytree = shutil.copytree


def boom_early(src, dst, *a, **kw):
    if str(src).endswith("bbb"):
        raise OSError("disk full (simulated)")
    return real_copytree(src, dst, *a, **kw)


plugins.shutil.copytree = boom_early
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

print("== F2. 复制中途失败：绝不能留下半个插件（原子落位） ==")
base6b, mp_src6b, plugins_dir6b, persist6b = make_tree("partial")
add_shim(plugins_dir6b)
add_plugin(plugins_dir6b, "ccc")


def boom_after_partial(src, dst, *a, **kw):
    """先真的建出目标目录并写一个文件，再抛错 —— 模拟 ENOSPC 之类的中途失败。"""
    Path(dst).mkdir(parents=True, exist_ok=True)
    (Path(dst) / "partial.txt").write_text("half\n", encoding="utf-8")
    raise OSError("no space left on device (simulated midway)")


plugins.shutil.copytree = boom_after_partial
try:
    rc = plugins.repair(mp_src6b, persist6b)
finally:
    plugins.shutil.copytree = real_copytree
check("F2a 中途失败返回 1", rc == 1, f"rc={rc}")
check("F2b 持久目录里没有半个插件", not (persist6b / "ccc").exists())
check("F2c 没有 .incoming-* 残留", incoming(persist6b) == [])
check("F2d 源码树原样（插件还在、还是真实目录）",
      plugins_dir6b.is_dir() and not plugins_dir6b.is_symlink()
      and plugins.plugin_names(plugins_dir6b) == ["ccc"])
# 再跑一次：这次允许成功，必须真的把插件完整搬过去，而不是"看见名字就跳过"
rc = plugins.repair(mp_src6b, persist6b)
check("F2e 之后的修复能正常完成", rc == 0, f"rc={rc}")
check("F2f 插件是完整的（含数据文件）",
      (persist6b / "ccc" / "__init__.py").is_file()
      and (persist6b / "ccc" / "sites" / "data.json").is_file())

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
rc = run_cli(["--mp-src", str(mp_src7), "--plugins-dir", str(mp_src7.parent)])
check("G4 持久目录是源码树的父目录（@appcenter 整包会被替换）-> 2", rc == 2, f"rc={rc}")

print("== H. 软链指向别处：不擅自改动 ==")
if not LINKS:
    skip("H", "需要软链")
else:
    base8, mp_src8, plugins_dir8, persist8 = make_tree("elsewhere")
    other = base8 / "other"
    other.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(plugins_dir8)
    os.symlink(str(other), str(plugins_dir8), target_is_directory=True)
    rc = plugins.repair(mp_src8, persist8)
    check("H1 指向别处 -> 3（需人工确认）", rc == 3, f"rc={rc}")
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
    check("J2 持久目录落在 CONFIG_DIR 同级 plugins_src",
          expected.is_dir() and (expected / "autosignin" / "__init__.py").is_file())
finally:
    if old_cfg is not None:
        os.environ["CONFIG_DIR"] = old_cfg
    else:
        os.environ.pop("CONFIG_DIR", None)
    if old_pdir is not None:
        os.environ["MP_PLUGINS_DIR"] = old_pdir

print("== K. 测试自身不污染仓库工作区 ==")
check("K1 仓库根没有冒出 plugins_src", not (ROOT / "plugins_src").exists())
check("K2 仓库根没有冒出 __init__.py 兼容入口", not (ROOT / "__init__.py").exists())

print("== L. 真实更新器集成（mp_updater 的整树操作） ==")
if not LINKS:
    skip("L", "需要软链")
else:
    import mp_updater as upd  # noqa: E402

    base11, mp_src11, plugins_dir11, persist11 = make_tree("updater")
    add_shim(plugins_dir11)
    add_plugin(plugins_dir11, "autosignin")
    plugins.repair(mp_src11, persist11)
    # 上游新包解出来的源码树：只有 app/ 与 version.py，app/plugins 里只有兼容入口
    staged = base11 / "staged"
    (staged / "app" / "plugins").mkdir(parents=True)
    add_shim(staged / "app" / "plugins", "from app.sdk import *  # shim from new package\n")
    (staged / "app" / "main.py").write_text("# new host\n", encoding="utf-8")
    (staged / "version.py").write_text("APP_VERSION = '3.3.0'\n", encoding="utf-8")

    backup_root = base11 / ".mp-backup" / "20261105-120000"
    moves = upd.stage_replace(staged, mp_src11, backup_root)
    check("L1 stage_replace 执行了替换", bool(moves), f"moves={moves}")
    check("L2 备份树里的 app/plugins 仍是软链",
          (backup_root / "app" / "plugins").is_symlink())
    check("L3 替换后持久目录毫发无损",
          (persist11 / "autosignin" / "__init__.py").is_file())
    check("L4 替换后确实处于未持久化状态", not plugins.is_persisted(mp_src11 / "app" / "plugins", persist11))
    rc = plugins.repair(mp_src11, persist11)
    check("L5 一次修复即恢复软链", rc == 0
          and plugins.is_persisted(mp_src11 / "app" / "plugins", persist11), f"rc={rc}")
    check("L6 兼容入口用上了新包那份",
          (persist11 / "__init__.py").read_text(encoding="utf-8")
          == "from app.sdk import *  # shim from new package\n")
    # 回滚：mp_updater.rollback 会把备份搬回来
    upd.rollback(moves, backup_root, base11 / "frontend")
    check("L7 回滚后 app/plugins 又是软链", (mp_src11 / "app" / "plugins").is_symlink())
    check("L8 回滚后插件还在",
          (mp_src11 / "app" / "plugins" / "autosignin" / "__init__.py").is_file())
    # 清理：prune_backups 对含软链的备份树 rmtree，绝不能跟进软链
    for i in range(4):
        old = base11 / ".mp-backup" / f"2026110{i}-000000"
        shutil.copytree(str(mp_src11 / "app"), str(old / "app"), symlinks=True)
    upd.prune_backups(base11 / ".mp-backup")
    check("L9 prune_backups 不会跟进软链删掉插件",
          (persist11 / "autosignin" / "__init__.py").is_file()
          and (persist11 / "__init__.py").is_file())
    shutil.rmtree(str(base11), ignore_errors=True)

print("== M. 断链自愈（目标目录被删） ==")
if not LINKS:
    skip("M", "需要软链")
else:
    base12, mp_src12, plugins_dir12, persist12 = make_tree("dangling")
    add_shim(plugins_dir12)
    add_plugin(plugins_dir12, "autosignin")
    shutil.rmtree(plugins_dir12)
    os.symlink(str(persist12), str(plugins_dir12), target_is_directory=True)
    check("M1 前置：确实是断链", plugins_dir12.is_symlink() and not plugins_dir12.exists())
    check("M2 is_persisted 不认断链", not plugins.is_persisted(plugins_dir12, persist12))
    rc = run_cli(["--check", "--mp-src", str(mp_src12), "--plugins-dir", str(persist12)])
    check("M3 --check -> 3", rc == 3, f"rc={rc}")
    rc = plugins.repair(mp_src12, persist12)
    check("M4 repair -> 3（已恢复可用，但插件需重装）", rc == 3, f"rc={rc}")
    check("M5 app/plugins 已恢复成真实目录（后端 import app.plugins 不再失败）",
          plugins_dir12.is_dir() and not plugins_dir12.is_symlink())

print("== N. 缺兼容入口只是警告（命名空间包可导入） ==")
if not LINKS:
    skip("N", "需要软链")
else:
    base13, mp_src13, plugins_dir13, persist13 = make_tree("noshim")
    add_shim(plugins_dir13)
    add_plugin(plugins_dir13, "autosignin")
    plugins.repair(mp_src13, persist13)
    (persist13 / "__init__.py").unlink()
    rc = plugins.repair(mp_src13, persist13)
    check("N1 repair -> 0（缺入口不影响导入，不谎报失败）", rc == 0, f"rc={rc}")
    rc = run_cli(["--check", "--mp-src", str(mp_src13), "--plugins-dir", str(persist13)])
    check("N2 --check -> 0", rc == 0, f"rc={rc}")
    # 真实导入验证：普通目录 = 命名空间包，照样能 import
    pkg = base13 / "pkg"
    (pkg / "app" / "plugins").mkdir(parents=True)
    (pkg / "app" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "app" / "plugins" / "autosignin").mkdir()
    (pkg / "app" / "plugins" / "autosignin" / "__init__.py").write_text("X = 1\n", encoding="utf-8")
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, r'%s'); import app.plugins.autosignin as m; print(m.X)" % pkg],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
    check("N3 没有 __init__.py 的 app/plugins 依然可导入",
          out.returncode == 0 and b"1" in out.stdout,
          out.stdout.decode("utf-8", "replace")[:200])

print("== O. 兼容入口写入原子性 ==")
if not LINKS:
    skip("O", "需要软链")
else:
    base14, mp_src14, plugins_dir14, persist14 = make_tree("shim-atomic")
    add_shim(plugins_dir14, "OLD-SHIM\n")
    add_plugin(plugins_dir14, "autosignin")
    plugins.repair(mp_src14, persist14)
    good = (persist14 / "__init__.py").read_text(encoding="utf-8")
    # 下一次宿主换树：包内 ram 变成新版，但复制到一半失败
    plugins_dir14.unlink()
    plugins_dir14.mkdir()
    add_shim(plugins_dir14, "NEW-SHIM\n")
    real_copy2 = shutil.copy2


    def boom_copy2(src, dst, *a, **kw):
        if str(src).endswith("__init__.py"):
            Path(dst).write_text("'''half written", encoding="utf-8")
            raise OSError("no space left on device (simulated)")
        return real_copy2(src, dst, *a, **kw)


    plugins.shutil.copy2 = boom_copy2
    try:
        rc = plugins.repair(mp_src14, persist14)
    finally:
        plugins.shutil.copy2 = real_copy2
    shim_text = (persist14 / "__init__.py").read_text(encoding="utf-8")
    check("O1 修复本身不因入口刷新失败而失败", rc == 0, f"rc={rc}")
    check("O2 兼容入口没被写坏（要么旧内容要么新内容，绝不是半截）",
          shim_text in (good, "NEW-SHIM\n"), repr(shim_text))
    check("O3 没有 .incoming-* 残留", incoming(persist14) == [])
    check("O4 插件照常迁移", (persist14 / "autosignin" / "__init__.py").is_file())

print("== P. 属主（仅 Linux + root） ==")
posix_root = hasattr(os, "geteuid") and os.geteuid() == 0
if not posix_root:
    skip("P", "需要 POSIX + root（本机不是）")
else:
    base15, mp_src15, plugins_dir15, persist15 = make_tree("owner")
    add_shim(plugins_dir15)
    add_plugin(plugins_dir15, "autosignin")
    os.environ.setdefault("TRIM_USERNAME", "nobody")
    rc = plugins.repair(mp_src15, persist15)
    ident = plugins.app_identity()
    check("P1 修复成功", rc == 0, f"rc={rc}")
    if ident is None:
        check("P2 能确定应用运行用户", False, "app_identity() 返回 None")
    else:
        st = os.stat(str(persist15))
        check("P2 持久目录属主是应用运行用户", (st.st_uid, st.st_gid) == ident,
              f"{st.st_uid}:{st.st_gid} != {ident}")
        st2 = os.stat(str(persist15 / "autosignin"))
        check("P3 插件目录属主同样是应用运行用户", (st2.st_uid, st2.st_gid) == ident,
              f"{st2.st_uid}:{st2.st_gid} != {ident}")
        check("P4 应用运行用户可写", bool(os.stat(str(persist15)).st_mode & stat.S_IWUSR))

print("== Q. 建链窗口里的并发写入：不覆盖、不丢弃 ==")
if not LINKS:
    skip("Q", "需要软链")
else:
    base16, mp_src16, plugins_dir16, persist16 = make_tree("race")
    add_shim(plugins_dir16)
    add_plugin(plugins_dir16, "autosignin")
    real_rename = os.rename

    def rename_then_recreate(src, dst):
        real_rename(src, dst)
        # 模拟"改名之后、建链之前"有并发写入者又把 app/plugins 建了出来
        plugins_dir16.mkdir(exist_ok=True)
        (plugins_dir16 / "inflight.txt").write_text("x\n", encoding="utf-8")

    plugins.os.rename = rename_then_recreate
    try:
        rc = plugins._link(plugins_dir16, persist16)
    finally:
        plugins.os.rename = real_rename
    check("Q1 并发写入时放弃本次链接（返回 1）", rc == 1, f"rc={rc}")
    check("Q2 并发写入的内容没被覆盖", (plugins_dir16 / "inflight.txt").is_file())
    check("Q3 旧插件目录内容被合并回来，没有丢",
          (plugins_dir16 / "autosignin" / "__init__.py").is_file()
          and (plugins_dir16 / "__init__.py").is_file())
    check("Q4 源码树没有变成软链", not plugins_dir16.is_symlink())

print()
if SKIPS:
    print(f"SKIP {len(SKIPS)} 项：{', '.join(SKIPS)}")
if FAILS:
    print(f"FAILED {len(FAILS)}: {', '.join(FAILS)}")
    sys.exit(1)
print("ALL PASS")
