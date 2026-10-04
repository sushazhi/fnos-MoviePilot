#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bump_upstream.py 的 manifest 改写逻辑离线冒烟测试（Windows 也能跑，不联网）。

背景：manifest 的 changelog 会进包内并显示在 fnOS 应用中心，同时又是 GitHub
Release notes 的来源（build-and-release.yml 直接 grep 它）。本仓库的既定口径是
**只保留最新一条**（见提交 7b97fda「manifest 更新日志只保留最新版本」），不做
累积 —— 但这条口径此前只靠 MAX_CHANGELOG_ENTRIES = 1 这一个数字兜着，没有任何
测试覆盖：

  - 谁把常量改成 2，累积就会悄悄回来，且只有用户升级后在应用中心里才看得见；
  - bump_manifest 的"版本没变就不动 changelog"是 --force 重打包不塞重复条目的
    前提，一旦被改掉，每周二的重打包会把同一版本写进去一遍又一遍。

所以本测试把这两条都钉住。全部在沙箱里做，不碰仓库里真实的 manifest / build.py：

  A 单条基线：只有一条旧条目时，替换成新条目，旧的消失
  B 累积收敛：入参里预置三条历史条目，改完**只剩最新一条**（本仓库的核心口径）
  C 幂等重打包：version 已是目标值 → 文本原样返回、changed=False、不新增条目
  D 切分边界：条目内部的 `<br><br>`（后面不跟版本号）不算条目边界
  E 保真：只动 version / changelog 两行，键名对齐（等号两侧空白）与其它行不变
  F fnpack 守卫：取值里出现 `;` 时 write_manifest 必须拒绝写盘
  G 策略钉住：MAX_CHANGELOG_ENTRIES 必须仍是 1（否则本文件的 A/B 会失效）
"""
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SANDBOX = ROOT / ".local-build" / "_smoke" / "bump"

sys.path.insert(0, str(ROOT / "tools"))
import bump_upstream as bu  # noqa: E402

FAILS = []


def check(label, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + label
          + (f"  <- {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(label)


# ---------------------------------------------------------------------------
# 沙箱
# ---------------------------------------------------------------------------

# 真实 manifest 的键名对齐方式：键名后补空格到 22 列再写 `= `。
MANIFEST_TEMPLATE = """appname               = moviepilot
version               = {version}
changelog             = {changelog}
desc                  = MoviePilot for fnOS
source                = thirdparty
"""

OLD_ENTRY = ("v3.0.10-1<br>1.跟进上游 MoviePilot v3.0.10-1：构建 pin 与 manifest "
             "版本同步更新<br>2.上游源码零改动原样集成")


def entries_of(value):
    """按脚本自己的切分规则数条目，避免测试里另写一套口径。"""
    return [e for e in bu.ENTRY_SPLIT_RE.split(value) if e.strip()]


def bump(changelog, version, target, target_version):
    """在沙箱里写一份 manifest，跑一次 bump_manifest，返回 (新文本, changed, changelog 值)。"""
    SANDBOX.mkdir(parents=True, exist_ok=True)
    (SANDBOX / "manifest").write_text(
        MANIFEST_TEMPLATE.format(version=version, changelog=changelog),
        encoding="utf-8", newline="\n")
    text, changed = bu.bump_manifest(target, target_version)
    (SANDBOX / "manifest").write_text(text, encoding="utf-8", newline="\n")
    return text, changed, bu.manifest_values(text)["changelog"]


def main():
    if SANDBOX.exists():
        shutil.rmtree(SANDBOX)
    # bump_manifest / read_manifest / write_manifest 都从 PROJECT_DIR 取路径，
    # 改它即可把整个读写重定向到沙箱。
    bu.PROJECT_DIR = str(SANDBOX)
    bu.log = lambda msg: None          # 静音脚本自身的日志

    print("== A. 单条基线：旧条目被新条目替换 ==")
    text, changed, cl = bump(OLD_ENTRY, "3.0.10-1", "v3.0.11", "3.0.11")
    es = entries_of(cl)
    check("A1 只剩一条", len(es) == 1, f"entries={len(es)}")
    check("A2 是新的那一条", cl.startswith("v3.0.11<br>"), cl[:60])
    check("A3 旧版本号已消失", "v3.0.10-1<br>" not in cl, cl[:80])
    check("A4 changed=True", changed is True, f"changed={changed}")

    print()
    print("== B. 累积收敛：三条历史条目 -> 只剩最新一条 ==")
    accumulated = ("v3.0.10-1<br>1.旧条目A<br>2.细节"
                   "<br><br>v3.0.9<br>1.旧条目B"
                   "<br><br>v3.0.8<br>1.旧条目C")
    check("B0 入参确实是三条", len(entries_of(accumulated)) == 3,
          f"entries={len(entries_of(accumulated))}")
    text, changed, cl = bump(accumulated, "3.0.10-1", "v3.0.11", "3.0.11")
    es = entries_of(cl)
    check("B1 改完只剩一条", len(es) == 1, f"entries={len(es)}")
    check("B2 内容是新的那条", cl.startswith("v3.0.11<br>"), cl[:60])
    check("B3 历史条目全部清掉",
          all(v not in cl for v in ("旧条目A", "旧条目B", "旧条目C", "v3.0.9", "v3.0.8")),
          cl[:120])
    check("B4 没有残留的条目分隔", "<br><br>v" not in cl, cl[:120])

    print()
    print("== C. 幂等重打包：版本未变则原样返回，不塞重复条目 ==")
    text, changed, cl = bump(OLD_ENTRY, "3.0.10-1", "v3.0.10-1", "3.0.10-1")
    check("C1 changed=False", changed is False, f"changed={changed}")
    check("C2 changelog 原样", cl == OLD_ENTRY, cl[:80])
    check("C3 仍只有一条", len(entries_of(cl)) == 1, f"entries={len(entries_of(cl))}")
    check("C4 文本整体未变",
          text == MANIFEST_TEMPLATE.format(version="3.0.10-1", changelog=OLD_ENTRY),
          "manifest 文本被改动了")

    print()
    print("== D. 切分边界：条目内部段落分隔不算条目边界 ==")
    multi_para = ("v3.0.10-1<br>1.第一段<br><br>2.第二段（同一条目内的换段，"
                  "后面不跟版本号）<br>3.第三段")
    check("D1 段落分隔未被切开", len(entries_of(multi_para)) == 1,
          f"entries={len(entries_of(multi_para))}")
    check("D2 真边界仍被切开", len(entries_of(multi_para + "<br><br>v3.0.9<br>x")) == 2,
          "版本号前的 <br><br> 没被当作边界")
    text, changed, cl = bump(multi_para, "3.0.10-1", "v3.0.11", "3.0.11")
    check("D3 含段落的旧条目同样被整条替换", len(entries_of(cl)) == 1, cl[:80])

    print()
    print("== E. 保真：只动 version / changelog 两行 ==")
    text, changed, cl = bump(OLD_ENTRY, "3.0.10-1", "v3.0.11", "3.0.11")
    vals = bu.manifest_values(text)
    check("E1 version 已更新", vals["version"] == "3.0.11", vals["version"])
    check("E2 其它键未被改动",
          vals["appname"] == "moviepilot" and vals["desc"] == "MoviePilot for fnOS"
          and vals["source"] == "thirdparty",
          str(vals))
    check("E3 键名对齐保留",
          re.search(r"(?m)^changelog\s+=\s+v3\.0\.11<br>", text) is not None,
          text.splitlines()[2][:40])
    check("E4 行数不变", len(text.splitlines()) == len(MANIFEST_TEMPLATE.splitlines()),
          f"lines={len(text.splitlines())}")

    print()
    print("== F. fnpack 守卫：取值含 `;` 必须拒绝写盘 ==")
    SANDBOX.mkdir(parents=True, exist_ok=True)
    try:
        bu.write_manifest(MANIFEST_TEMPLATE.format(
            version="3.0.11", changelog="v3.0.11<br>含分号;的取值"))
        check("F1 含 `;` 时抛错", False, "write_manifest 未报错")
    except RuntimeError as e:
        check("F1 含 `;` 时抛错", "fnpack" in str(e), str(e)[:80])
    check("F2 正常取值可写盘",
          bu.write_manifest(MANIFEST_TEMPLATE.format(
              version="3.0.11", changelog="v3.0.11<br>正常")) is None)

    print()
    print("== G. 策略钉住 ==")
    check("G1 MAX_CHANGELOG_ENTRIES == 1（提交 7b97fda 的既定口径：只保留最新版本）",
          bu.MAX_CHANGELOG_ENTRIES == 1,
          f"MAX_CHANGELOG_ENTRIES={bu.MAX_CHANGELOG_ENTRIES} —— 改大于 1 会让 changelog 重新累积；"
          f"若确实要改口径，请同步更新本测试与 README")

    print()
    if FAILS:
        print(f"{len(FAILS)} 项失败: {', '.join(FAILS)}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
