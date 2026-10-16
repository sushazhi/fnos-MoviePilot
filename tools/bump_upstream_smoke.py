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
  H 上游摘要清洗：markdown 链接/裸 URL/标题被洗掉，bullet 折叠为编号列表
  I 分号与尖括号归一：上游说明里的半角 `;` 必须变成全角 `；`
  J 摘要回退：抓不到 / 空正文 / tag 不匹配 / 清洗后为空 → 用固定文案，且不抛异常

H/I/J 对应「changelog 第 1 条改为引用上游 Release 摘要」这次改动。其中 I 是**硬约束**：
fnpack 遇到半角分号会截断整行取值，而 check_manifest_values() 是抛异常而不是回退 ——
上游说明里混进一个分号，下周二的自动发布就直接失败。所以这里必须钉死。
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
    print("== H. 上游摘要清洗：markdown 噪音被洗掉，bullet 折叠为编号 ==")
    body = (
        "## 本次更新内容\n"
        "\n"
        "- 优化在线升级流程：主程序、配套前端和站点资源统一准备，全部就绪后只需重启一次。\n"
        "- 新增整理成功通知聚合开关，见 [文档](https://example.com/doc) 了解详情。\n"
        "- 修复 Telegram 富消息降级\n"
        "\n"
        "## 完整变更记录\n"
        "\n"
        "- feat(update): prepare latest updates by @jxxghp ([edc7195](https://github.com/x/y/commit/edc7195))\n"
    )
    section = bu.extract_summary_section(body)
    check("H1 只取「本次更新内容」段", "本次更新内容" not in section
          and "优化在线升级流程" in section, section[:80])
    check("H2 「完整变更记录」段未被取用", "feat(update)" not in section
          and "edc7195" not in section, section[:80])

    clean = bu.sanitize_notes(section)
    check("H3 markdown 链接只剩文字", "[文档]" not in clean and "](http" not in clean,
          clean[:120])
    check("H4 无裸 URL 残留", "http" not in clean, clean[:120])
    check("H5 无 markdown 标题残留", "#" not in clean, clean[:120])
    check("H6 无换行（必须是 manifest 单行取值）",
          "\n" not in clean and "\r" not in clean, repr(clean[:80]))
    check("H7 条目连成一句、无内层编号",
          not clean.startswith("1.") and "；" in clean, clean[:120])
    check("H8 首条内容正确", "优化在线升级流程" in clean, clean[:80])
    check("H9 上游自带的 `1.` 前缀已剥离", not re.search(r"；\d+[.)]", clean), clean[:120])
    check("H10 无两套打架的编号（外层 1./2./3. 只在 changelog 层出现）",
          not re.search(r"(?:^|；)\d+[.)]", clean), clean[:120])
    check("H11 无「。；」畸形标点", "。；" not in clean and ".；" not in clean, clean[:140])
    check("H12 无连续分号", "；；" not in clean, clean[:140])

    print()
    print("== I. 分号/尖括号归一（硬约束：半角 ; 会让 fnpack 截断取值） ==")
    dirty = "- 主程序、配套前端和站点资源统一准备; 全部就绪后只需重启一次\n- a < b > c"
    fixed = bu.sanitize_notes(dirty)
    check("I1 半角分号已归一为全角", ";" not in fixed and "；" in fixed, fixed[:100])
    check("I2 半角尖括号已归一", "<" not in fixed and ">" not in fixed, fixed[:100])
    check("I3 归一后能通过 fnpack 守卫", bu.check_manifest_values("changelog = " + fixed) == [],
          str(bu.check_manifest_values("changelog = " + fixed))[:120])
    # 反向对照：不归一的话守卫必然会拦下来 —— 这正是本组测试存在的理由
    check("I4 反证：未归一的分号确实会被守卫拦下",
          bu.check_manifest_values("changelog = 准备; 就绪") != [],
          "守卫没拦住半角分号，说明 F 组的前提变了")

    print()
    print("== J. 摘要回退：任何异常都要落到固定文案，且不抛异常 ==")
    long_item = "- " + "很长的条目" * 40
    capped = bu.sanitize_notes(long_item)
    check("J1 超长摘要被截断到上限内",
          len(capped) <= bu.SUMMARY_MAX_CHARS + 1, f"len={len(capped)}")
    check("J2 截断后有省略号", capped.endswith("…"), capped[-12:])
    capped_items = bu.sanitize_notes("\n".join(f"- 条目{i}" for i in range(10)))
    check("J3 bullet 条数受限", capped_items.count("；") + 1 <= bu.SUMMARY_MAX_ITEMS,
          capped_items[:120])
    check("J4 空文本清洗后为空串", bu.sanitize_notes("") == "", repr(bu.sanitize_notes("")))
    check("J5 只有标记符的文本清洗后为空", bu.sanitize_notes("---\n\n###\n") == "",
          repr(bu.sanitize_notes("---\n\n###\n")))

    # fetch_upstream_notes 在各种失败下都必须返回 ""，不能抛
    orig_api_get = bu.api_get
    cases = [
        ("网络异常", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))),
        ("返回非对象", lambda *a, **k: ["not", "a", "dict"]),
        ("tag 不匹配", lambda *a, **k: {"tag_name": "v9.9.9", "body": "## 本次更新内容\n- x"}),
        ("body 为空", lambda *a, **k: {"tag_name": "v3.1.6", "body": "   "}),
        ("body 非字符串", lambda *a, **k: {"tag_name": "v3.1.6", "body": None}),
    ]
    for label, fake in cases:
        bu.api_get = fake
        try:
            got = bu.fetch_upstream_notes("v3.1.6")
            check(f"J6 {label} -> 返回空串", got == "", repr(got)[:80])
        except Exception as e:
            check(f"J6 {label} -> 返回空串", False, f"抛了异常: {e!r}")
        try:
            got2 = bu.upstream_summary("v3.1.6")
            check(f"J7 {label} -> upstream_summary 也返回空串", got2 == "", repr(got2)[:80])
        except Exception as e:
            check(f"J7 {label} -> upstream_summary 也返回空串", False, f"抛了异常: {e!r}")
    bu.api_get = orig_api_get

    print()
    print("== K. changelog 第 1 条：有摘要用摘要，无摘要回退固定文案 ==")
    with_notes = bu.changelog_entry("v3.1.6", "3.1.6", "优化在线升级流程；新增通知聚合开关")
    check("K1 有摘要时写「更新内容」", "更新内容——优化在线升级流程；新增通知聚合开关" in with_notes,
          with_notes[:140])
    fallback = bu.changelog_entry("v3.1.6", "3.1.6")
    check("K2 无摘要时回退固定文案", "跟进上游 MoviePilot v3.1.6" in fallback, fallback[:120])
    check("K3 两种形态第 2~4 条一致",
          with_notes.split("<br>")[2:] == fallback.split("<br>")[2:], "第 2~4 条不一致")
    check("K4 两种形态都能过守卫",
          bu.check_manifest_values("changelog = " + with_notes) == []
          and bu.check_manifest_values("changelog = " + fallback) == [])

    # 端到端：带摘要走一遍 bump_manifest，确认落到 manifest 且仍是单条。
    # 注意必须先造一份「版本还没到位」的 manifest —— 上面 bump() 已经把沙箱里的
    # version 写成 3.0.11 了，直接再跑会命中"版本未变就不动 changelog"的幂等守卫
    # （这正是 C 组要的行为），这里测的是另一条路径。
    SANDBOX.mkdir(parents=True, exist_ok=True)
    (SANDBOX / "manifest").write_text(
        MANIFEST_TEMPLATE.format(version="3.0.10-1", changelog=OLD_ENTRY),
        encoding="utf-8", newline="\n")
    text2, changed2 = bu.bump_manifest("v3.0.11", "3.0.11", "优化在线升级流程")
    vals2 = bu.manifest_values(text2)
    check("K5 摘要进入 changelog", "更新内容——优化在线升级流程" in vals2["changelog"],
          vals2["changelog"][:120])
    check("K5b changed=True", changed2 is True, f"changed={changed2}")
    check("K6 端到端仍是单条条目", len(entries_of(vals2["changelog"])) == 1,
          f"entries={len(entries_of(vals2['changelog']))}")

    print()
    if FAILS:
        print(f"{len(FAILS)} 项失败: {', '.join(FAILS)}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
