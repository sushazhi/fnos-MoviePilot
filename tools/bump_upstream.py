#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查上游 MoviePilot 是否有新版本；有则把本仓库「跟着上游走」的位置一次性改到位。

为什么需要这个脚本
------------------
本仓库只做飞牛 fnOS 适配层，上游 MoviePilot 源码不入库（构建时才下载），所以
「升级上游」在这里等价于「改几个数字」。这些数字互相耦合，漏掉任何一处都会
在构建期或安装后才暴露：

  1. build.py  UPSTREAM_TAG   —— 决定构建时下载哪份上游源码（见该常量的注释）
  2. manifest  version        —— 与上游 tag 逐字对应（去掉前导 v），如 3.0.10-1
  3. manifest  changelog      —— 版本说明，同时是 GitHub Release notes 的来源

只改 pin 不改 manifest，会打出「包名说 3.0.9、内容其实是 3.0.10」的包；只改
manifest 不改 pin，构建脚本仍会去下旧源码 —— 两种错都得等装到 NAS 上才发现，
而且发布出去就收不回来。本脚本把「查上游 → 改 pin → 改 manifest → 自证」串成
一步，供 GitHub Actions 每周二自动执行，也可以本地手动跑。

本地不带 GITHUB_TOKEN 也能跑，只是会受匿名 API 限流（60 次/小时）；Actions 里
用内置 GITHUB_TOKEN（5000 次/小时）就够。

版本号规则
----------
上游自己会用 `-N` 表示「同一版本的重新打包」（v3.0.10-1 是在 v3.0.10 之后的
重新发布，**不是预发布**），所以合法形态有 v3.x.y 与 v3.x.y-N 两种，且 `-N`
排在裸版本之后（v3.0.10-1 > v3.0.10）。预发布（alpha/beta/rc）与 v1/v2 时代的
tag 一律不跟 —— 自动流水线只跟正式版，要不要跟预发布由人决定。

manifest.version 取上游 tag 去掉前导 v 后的**原文**，不另起本地序号：这样
「包名里的版本」与「上游 tag」永远一一对应，用户报问题时能直接对回上游。

用法
----
    python tools/bump_upstream.py --detect        # 只检查，不改任何文件
    python tools/bump_upstream.py --apply         # 检查并应用（版本取自上游）
    python tools/bump_upstream.py --apply --upstream-version 3.0.11
    python tools/bump_upstream.py --apply --force # 上游无新版本也重跑一遍

在 GitHub Actions 里会把结果写进 $GITHUB_OUTPUT（update / newer / upstream_tag /
app_version / tag / current_upstream_tag），供后续步骤与 Summary 使用。
"""
import importlib
import json
import os
import re
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import build  # noqa: E402  （要放在 sys.path 调整之后）

# 上游正式版 tag 形态：v3.0.10 / 3.0.10 / v3.0.10-1。
# 第 4 组是上游的「重新打包」序号，缺省记 0 —— 于是 v3.0.10-1 > v3.0.10。
TAG_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-(\d+))?$")

# manifest 里保留几条版本条目。本仓库的既定口径是**只保留最新一条**
# （见提交 7b97fda「manifest 更新日志只保留最新版本」），不做累积：
# changelog 会进包内 manifest 并显示在应用中心，越滚越长没有意义。
MAX_CHANGELOG_ENTRIES = 1

# 分隔两个版本条目的写法（`…<br><br>v3.0.10-1<br>…`）。只在 `<br><br>` 后面
# 紧跟版本号时才切分，避免把条目内部的段落分隔也当成边界。
ENTRY_SPLIT_RE = re.compile(r"<br><br>(?=v?\d+\.\d+\.\d+)")

# fnpack 解析 manifest 时遇到这些字符会**截断**该行的取值（本仓库 build.py 没有
# 对应的守卫函数，所以在这里自带一道）。取值里出现它们会导致「包内 desc/changelog
# 只剩前半段」这种静默缺陷。
MANIFEST_VALUE_TERMINATORS = (";",)


def log(msg):
    """复用 build.py 的日志函数（它已处理 Windows 控制台的编码问题）。"""
    build.log(msg)


def parse_tag(s):
    """把 tag 解析成可比较的元组 (major, minor, patch, rebuild)；不合法返回 None。

    刻意**不接受** alpha/beta/rc：自动流水线只跟正式版。
    """
    m = TAG_RE.match((s or "").strip())
    if not m:
        return None
    major, minor, patch, rebuild = m.groups()
    return (int(major), int(minor), int(patch), int(rebuild) if rebuild else 0)


def tag_of(key):
    """把比较元组还原成上游 tag 文本（重建序号为 0 时不写 `-0`）。"""
    major, minor, patch, rebuild = key
    return f"v{major}.{minor}.{patch}" + (f"-{rebuild}" if rebuild else "")


def api_get(path, token=None):
    url = f"https://api.github.com/{path}"
    headers = {
        "User-Agent": "fnos-moviepilot-bump",
        "Accept": "application/vnd.github+json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def upstream_tags(token=None):
    """列出上游所有可跟的正式版 tag，返回 {比较元组: tag 原文}。

    tags 是权威来源：上游的 Release 是给终端用户的安装包，tag 才是我们要编的那份
    源码；两者偶有先后（先补 tag 后发 Release），所以只信 tags，另用 releases/latest
    兜底（万一上游先发 Release 后补 tag）。

    翻两页（每页 100）足够覆盖当前全部 tag 还有富余；上游 tag 已经过百，只翻一页
    会在 tag 数继续增长后**悄悄漏掉最新版本**，所以这里显式翻到第二页。
    """
    found = {}
    for page in (1, 2):
        try:
            tags = api_get(f"repos/{build.UPSTREAM_REPO}/tags?per_page=100&page={page}", token)
        except Exception as e:
            # 限流 / 网络抖动：不抛原始 traceback，交给文件末尾那句
            # 「未能从上游取到任何版本号」统一报错 —— 结果一样是失败退出，
            # 但人能一眼看懂原因（在 Actions 日志里尤其重要）。
            log(f"  [提示] 读取上游 tags 第 {page} 页失败: {e}")
            break
        if not isinstance(tags, list) or not tags:
            break
        for t in tags:
            name = str(t.get("name") or "")
            key = parse_tag(name)
            if key:
                # 同一元组只可能来自同一 tag；保留先到的（顺序无关）
                found.setdefault(key, name if name.startswith("v") else f"v{name}")
    try:
        rel = api_get(f"repos/{build.UPSTREAM_REPO}/releases/latest", token)
        name = str((rel or {}).get("tag_name") or "")
        key = parse_tag(name)
        if key:
            found.setdefault(key, name if name.startswith("v") else f"v{name}")
    except Exception as e:  # 404（仓库没有 Release）或限流都不该让检查失败
        log(f"  [提示] 读取 releases/latest 失败，忽略: {e}")
    if not found:
        raise RuntimeError("未能从上游取到任何版本号（网络或 API 限流？）")
    return found


# ---------------------------------------------------------------------------
# 上游 Release 说明的抓取与清洗
# ---------------------------------------------------------------------------

# 上游 Release body 的章节标题形态：`## 本次更新内容` / `## 完整变更记录` / `## 版本对比`。
SECTION_RE = re.compile(r"(?m)^#{1,6}\s+(?P<title>.+?)\s*$")

# 只取「本次更新内容」这段。**刻意不取「完整变更记录」**：那段是
# `feat(update): prepare latest updates for a single restart by @jxxghp` 这种
# 中英混杂的逐条 commit 列表，整体塞进应用中心的更新日志里既长又难读；
# 而「本次更新内容」是上游写给终端用户看的自然语言摘要，正是我们要的口径。
SUMMARY_SECTION_TITLES = ("本次更新内容",)

# 「版本对比」之外还要裁掉 body 里裸放的 compare 链接与分隔线：上游把
# `https://github.com/.../compare/...` 直接写在正文里（不带 markdown 链接语法），
# GitHub 渲染时自动 linkify，但 API 返回的原文里就是一条光秃秃的长 URL。
_URL_RE = re.compile(r"https?://\S+")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\((?:[^)]*)\)")
_HR_RE = re.compile(r"(?m)^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?P<body>.+)$")

# 摘要总长上限（Unicode 字符数，不是字节 —— 中文按 1 个字符算）。
# 200 字符约合 3~4 行中文：够说清上游这次改了什么，又不会让应用中心的
# 更新日志长到没人看。
SUMMARY_MAX_CHARS = 200

# 单条 bullet 的字符上限：上游个别条目很长（一句话里塞三四个修复），
# 不设上限会导致第 1 条就吃满总预算、后面所有条目全被截掉。
SUMMARY_MAX_ITEM_CHARS = 90

# 摘要里最多保留几条 bullet。
SUMMARY_MAX_ITEMS = 3


def fetch_upstream_notes(upstream_tag, token=None):
    """取上游 Release 里「本次更新内容」段的原始文本；取不到返回 ""。

    为什么用 releases/latest 而不是 releases/tags/<tag>
    ------------------------------------------------
    两个接口的返回是**不同的 schema**：`releases/tags/<tag>` 给的是轻量 schema，
    body 往往是空的；只有 `releases/latest` 才带完整 body（实测 6000+ 字符）。
    所以这里必须用 latest，**再校验 tag_name 与目标 tag 完全相等** —— 上游发新版
    后 latest 会立刻指向新 tag，而我们要编的是 pin 住的那个版本，两者不一致时
    宁可不写摘要，也不能把新版本的说明写到旧版本的 changelog 里。

    本函数**不抛异常**：任何失败（网络、限流、404、tag 不匹配、body 为空）都返回
    ""，由调用方回退到固定文案。摘要只是锦上添花，绝不能让它把每周的发布流程拖挂。
    """
    try:
        rel = api_get(f"repos/{build.UPSTREAM_REPO}/releases/latest", token)
    except Exception as e:
        log(f"  [提示] 读取上游 Release 失败，changelog 用固定文案: {e}")
        return ""

    if not isinstance(rel, dict):
        log("  [提示] 上游 Release 返回不是对象，changelog 用固定文案")
        return ""

    remote_tag = str(rel.get("tag_name") or "").strip()
    if remote_tag != upstream_tag:
        log(f"  [提示] 上游 latest 是 {remote_tag or '（空）'}，"
            f"与目标 {upstream_tag} 不一致，changelog 用固定文案")
        return ""

    body = rel.get("body")
    if not isinstance(body, str) or not body.strip():
        log("  [提示] 上游 Release 没有说明正文，changelog 用固定文案")
        return ""

    return extract_summary_section(body)


def extract_summary_section(body):
    """从 Release body 里切出「本次更新内容」段，返回该段文本（找不到返回整个 body）。"""
    matches = list(SECTION_RE.finditer(body))

    # 优先按标题找目标段。
    for i, m in enumerate(matches):
        if m.group("title").strip() in SUMMARY_SECTION_TITLES:
            end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
            return body[m.end():end]

    # 没有标题的 body（上游偶尔直接写正文）：整体拿来用，让下面的清洗去兜底。
    return body if not matches else ""


def sanitize_notes(raw):
    """把上游 Markdown 摘要洗成一行、可直接塞进 manifest changelog 的文本。

    清洗链的每一步都对应一个**真实会咬人的坑**：

      1. 去 markdown 链接语法，只留链接文字（应用中心不渲染 markdown，留着就是噪音）
      2. 去裸 URL 与分隔线
      3. `;` → `；`  —— 最关键的一步：fnpack 遇到半角分号会**截断整行取值**，
         而 check_manifest_values() 是抛异常，会让整个 job 失败（不是回退！）。
         上游说明里出现一句半角分号，下周二的自动发布就直接挂掉。
         当前上游 body 实测 0 处半角分号（全角 5 处），但这是运气不是保证。
      4. 压空白、去空行
      5. bullet 剥掉上游自带编号、连成一句（`；` 连接），并逐条限长
      6. 总长限 SUMMARY_MAX_CHARS，超出加省略号
    """
    text = raw

    # 1. markdown 链接 → 只留文字
    text = _MD_LINK_RE.sub(r"\1", text)
    # 2. 裸 URL / 分隔线 / 标题残留（`#` 开头的行）
    text = _URL_RE.sub("", text)
    text = _HR_RE.sub("", text)
    text = re.sub(r"(?m)^\s*#{1,6}\s*", "", text)

    # 3. 半角分号归一（见 docstring）—— 必须在拼接 `<br>` 之前做，
    #    否则模板里自己加的 `<br>` 结构不会被误伤（它本来也不含分号）。
    text = text.replace(";", "；")
    # 全角分号在 manifest 里是安全的（terminator 只拦半角），保留。
    # 同理顺手把会干扰 <br> 结构的尖括号风险字符归一。
    text = text.replace("<", "＜").replace(">", "＞")

    # 4. 逐行处理：bullet 提正文、其余压空白
    items = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _LIST_ITEM_RE.match(line)
        items.append(m.group("body").strip() if m else line)

    if not items:
        return ""

    # 5. 逐条限长 + 条数上限，用全角分号连成一句连续文本。
    #
    # **刻意不再给上游条目编号**：上游自己写的就是 `1.…2.…3.…`，若原样保留，
    # changelog 里就会出现两套互相打架的编号 —— 外层是「1.更新内容 / 2.源码零改动
    # / 3.自带运行时 / 4.自动生成」，内层又冒出「1.优化在线升级…3.升级多渠道消息」，
    # 读者看到「3.」无法判断指的是哪一层（实测确实如此）。改成用 `；` 连接的连续
    # 文字后，全文只剩外层一套编号，语义唯一。
    #
    # 副作用：内层编号被吃掉后，段落开头会直接接内容，所以先把上游自带的
    # 「1. / 2. / 3.」前缀剥掉，避免出现「；1.优化…」这种残留。
    clipped = []
    for it in items[:SUMMARY_MAX_ITEMS]:
        it = re.sub(r"^\d+[.)]\s*", "", it).strip()
        it = re.sub(r"[ \t]+", " ", it).strip()
        # 上游条目自带句末标点（实测是 `。`），用 `；` 连接会连出「布局。；新增」
        # 这种「句号紧跟分号」的畸形标点。连句前先把句末标点统一剥掉，只留连接符。
        it = re.sub(r"[。.；;、,，]+$", "", it).strip()
        if len(it) > SUMMARY_MAX_ITEM_CHARS:
            it = it[:SUMMARY_MAX_ITEM_CHARS] + "…"
        clipped.append(it)
    text = "；".join(c for c in clipped if c)

    # 6. 总长上限
    if len(text) > SUMMARY_MAX_CHARS:
        text = text[:SUMMARY_MAX_CHARS] + "…"
    return text


def upstream_summary(upstream_tag, token=None):
    """取并清洗上游摘要，返回可直接写进 changelog 的文本；取不到返回 ""。"""
    raw = fetch_upstream_notes(upstream_tag, token)
    if not raw:
        return ""
    clean = sanitize_notes(raw)
    if not clean:
        log("  [提示] 上游说明清洗后为空，changelog 用固定文案")
        return ""
    return clean


# ---------------------------------------------------------------------------
# build.py 的 pin 改写
# ---------------------------------------------------------------------------

def rewrite_pin(upstream_tag):
    """把 build.py 里的 UPSTREAM_TAG 改成目标 tag（必须恰好命中 1 行）。"""
    path = os.path.join(PROJECT_DIR, "build.py")
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    new, n = re.subn(r'(?m)^UPSTREAM_TAG\s*=\s*"[^"]+"',
                     f'UPSTREAM_TAG = "{upstream_tag}"', text)
    if n != 1:
        raise RuntimeError(f"build.py 里 UPSTREAM_TAG 匹配到 {n} 处，应为 1 处")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(new)
    log(f"  build.py: UPSTREAM_TAG = {upstream_tag}")


def load_build():
    """改完 build.py 后重新载入，让常量（含由 pin 派生的下载地址）生效。"""
    importlib.reload(build)
    return build


# ---------------------------------------------------------------------------
# manifest 的改写
# ---------------------------------------------------------------------------

MANIFEST_LINE_RE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)(?P<sep>\s*=\s*)(?P<val>.*)$")


def read_manifest():
    with open(os.path.join(PROJECT_DIR, "manifest"), "r", encoding="utf-8") as f:
        return f.read()


def manifest_values(text):
    """取出 manifest 的键值对（跳过注释行与空行）。"""
    out = {}
    for line in text.splitlines():
        m = MANIFEST_LINE_RE.match(line)
        if m:
            out[m.group("key")] = m.group("val")
    return out


def check_manifest_values(text):
    """检查取值里有没有会让 fnpack 截断的字符，返回问题描述列表。"""
    problems = []
    for key, val in manifest_values(text).items():
        for term in MANIFEST_VALUE_TERMINATORS:
            i = val.find(term)
            if i >= 0:
                problems.append(
                    f"{key}: 取值里第 {i} 个字符是 {term!r}，fnpack 会在此处截断 —— "
                    f"包内只会保留前 {i} 个字符（当前共 {len(val)} 个）"
                )
    return problems


def write_manifest(text):
    problems = check_manifest_values(text)
    if problems:
        raise RuntimeError("manifest 取值会被 fnpack 截断:\n    " + "\n    ".join(problems))
    # newline="\n"：仓库 .gitattributes 强制 manifest 以 LF 入库（本地 core.autocrlf
    # 为 true，不显式指定会写成 CRLF，diff 里整文件都变）。
    with open(os.path.join(PROJECT_DIR, "manifest"), "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def manifest_version(text):
    m = re.search(r"(?m)^version\s*=\s*(\S+)\s*$", text)
    if not m:
        raise RuntimeError("manifest 里读不到 version")
    return m.group(1)


def set_manifest_key(text, key, value):
    """按行改写 manifest 的某个键，保留键名对齐（等号两侧空白）与行尾。

    逐行替换而不是对整段文本做 `re.sub`：manifest 的 desc/changelog 都是超长单行，
    连续两次整段替换时第二次的偏移会失效（实测会把文件末尾的换行吃掉）。本仓库的
    manifest **本来就没有末尾换行**，所以这里连"补换行"都不能做 —— 保持原样。
    """
    lines = text.splitlines(keepends=True)
    hits = 0
    for i, line in enumerate(lines):
        m = MANIFEST_LINE_RE.match(line.rstrip("\r\n"))
        if not m or m.group("key") != key:
            continue
        eol = "\n" if line.endswith("\n") else ""
        lines[i] = f"{m.group('key')}{m.group('sep')}{value}{eol}"
        hits += 1
    if hits != 1:
        raise RuntimeError(f"manifest 里 {key} 匹配到 {hits} 行，应为 1 行")
    return "".join(lines)


def changelog_entry(upstream_tag, app_version, upstream_notes=""):
    """生成新版本的 changelog 条目（单条，不累积）。

    第 1 条的文案分两种：
      - 拿到上游 Release 摘要时，写「上游 <tag> 更新内容——<摘要>」；
      - 拿不到（网络/限流/tag 不匹配/正文为空）时，回退成本仓库视角的固定文案。

    第 2~4 条固定描述本仓库的打包口径，与上游说明无关，两种情况都一样。

    `upstream_notes` 必须是已经过 sanitize_notes 的文本 —— 直接拼原始 body 会把
    半角分号带进 manifest，进而让 fnpack 截断取值、job 失败。
    """
    if upstream_notes:
        first = f"1.上游 MoviePilot {upstream_tag} 更新内容——{upstream_notes}"
    else:
        first = (f"1.跟进上游 MoviePilot {upstream_tag}：构建 pin 与 manifest 版本同步更新，"
                 f"包名版本与上游 tag 一一对应")

    return (
        f"v{app_version}<br>"
        f"{first}<br>"
        f"2.上游源码零改动原样集成，仅做 fnOS 打包适配（生命周期脚本、统一网关、桌面入口）<br>"
        f"3.自带 CPython {build.PYTHON_FULL_VERSION} 运行时与全部依赖，安装时无需联网<br>"
        f"4.本条目由 GitHub Actions 定时检查上游后自动生成"
    )


def bump_manifest(upstream_tag, app_version, upstream_notes=""):
    """把 manifest 的 version / changelog 改到位，返回 (新文本, 是否有改动)。

    版本号已经是目标值时**不动 changelog** —— 这样 `--force` 就是纯粹的
    「按当前版本重打一次包」，不会每次重跑都往 changelog 里塞重复条目。
    """
    text = read_manifest()
    cur = manifest_version(text)
    if cur == app_version:
        log(f"  manifest: version 已是 {app_version}，changelog 保持不变（重打包）")
        return text, False

    new_entry = changelog_entry(upstream_tag, app_version, upstream_notes)
    m = re.search(r"(?m)^changelog\s*=\s*(.*)$", text)
    if not m:
        raise RuntimeError("manifest 里读不到 changelog")
    entries = [e for e in ENTRY_SPLIT_RE.split(m.group(1)) if e.strip()]
    value = "<br><br>".join([new_entry] + entries[: MAX_CHANGELOG_ENTRIES - 1])

    text = set_manifest_key(text, "version", app_version)
    text = set_manifest_key(text, "changelog", value)
    log(f"  manifest: version {cur} -> {app_version}，changelog 替换为最新条目")
    return text, True


# ---------------------------------------------------------------------------
# GitHub Actions 输出
# ---------------------------------------------------------------------------

def emit_outputs(pairs):
    """把结果写给 GitHub Actions：$GITHUB_OUTPUT 供下游步骤取值。

    只写 key=value，不写 markdown 表格 —— workflow 里的 Summary step 负责展示
    （那边能同时列出「上游 tag / 应用版本 / 发布 tag」的全貌，而本脚本在 --detect
    早期就可能退出，写两份会出现两个内容不同的表格）。本地运行时该环境变量不存在，
    整个函数就是空操作。
    """
    out = os.environ.get("GITHUB_OUTPUT", "").strip()
    for k, v in pairs.items():
        log(f"  output {k}={v}")
        if out:
            with open(out, "a", encoding="utf-8") as f:
                f.write(f"{k}={v}\n")


# ---------------------------------------------------------------------------

def main():
    args = sys.argv[1:]
    detect_only = "--detect" in args
    apply_changes = "--apply" in args
    force = "--force" in args
    if not detect_only and not apply_changes:
        print(__doc__)
        sys.exit(2)

    want = ""
    if "--upstream-version" in args:
        want = args[args.index("--upstream-version") + 1]
        if not parse_tag(want):
            raise SystemExit(f"ERROR: --upstream-version 不是合法版本号: {want!r}")
        want = tag_of(parse_tag(want))

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""

    log("=" * 46)
    log(" 上游 MoviePilot 版本检查")
    log(f" 仓库: {build.UPSTREAM_REPO}")
    log(f" 当前 pin: {build.UPSTREAM_TAG or '（未设置）'}")
    log("=" * 46)

    cur = parse_tag(build.UPSTREAM_TAG)
    if cur is None:
        # pin 为空/写坏：不当作致命错误 —— 下面会把任意上游版本视为「比它新」，
        # 正好完成一次"补上 pin"的自我修复。但写坏的 pin 值得显式提醒。
        if (build.UPSTREAM_TAG or "").strip():
            log(f"  [提示] build.py 的 UPSTREAM_TAG 无法解析: {build.UPSTREAM_TAG!r}，"
                f"将按「无 pin」处理")

    try:
        if want:
            latest_key, latest_tag = parse_tag(want), want
        else:
            found = upstream_tags(token)
            latest_key = max(found)
            latest_tag = found[latest_key]
    except Exception as e:
        # 取不到上游版本就是「这次检查失败」，但不能让它以 traceback 收场：Actions
        # 日志里一个 traceback 会被当成脚本 bug，而真实原因通常是 API 限流或网络。
        # job 照样非零退出 —— 绝不能在「不知道上游有没有新版」时判定「无更新」并静默跳过。
        raise SystemExit(f"ERROR: 无法确定上游最新版本，本次检查失败: {e}")
    log(f" 上游最新: {latest_tag}")

    newer = cur is None or latest_key > cur
    update = newer or force
    target_tag = latest_tag if newer else build.UPSTREAM_TAG
    # 与上游 tag 逐字对应：去掉前导 v 就是应用版本（不另起本地序号）。
    # 即使上游没新版本也照此推导 —— 于是「pin 与 manifest 脱钩」会被自动纠偏
    # （例如手工改了 pin 却忘了改 manifest，或反过来）。
    app_version = target_tag.lstrip("v")

    log(f" 判定: {'有新版本' if newer else '已是最新'}"
        f"{'（--force 仍重跑一遍）' if force and not newer else ''}")
    log(f" 目标: 上游 {target_tag} → 应用版本 {app_version}")

    emit_outputs({
        "update": "true" if update else "false",
        "newer": "true" if newer else "false",
        "upstream_tag": target_tag,
        "current_upstream_tag": build.UPSTREAM_TAG,
        "app_version": app_version,
        "tag": f"v{app_version}",
    })

    if detect_only or not update:
        log("")
        log(" 未改动任何文件" if not update else " --detect：未改动任何文件")
        return

    log("")
    log("[1/3] 改 build.py 的上游 pin ...")
    if newer:
        rewrite_pin(target_tag)
        load_build()
    else:
        log("  （--force：上游无新版本，pin 不变）")

    log("[2/3] 改 manifest（version + changelog）...")
    # 只有真要改 changelog 时才去抓上游说明：版本未变（--force 重打包）时
    # bump_manifest 会原样返回，这里多抓一次纯属浪费一次 API 配额。
    notes = ""
    if manifest_version(read_manifest()) != app_version:
        log("  抓取上游 Release 说明 ...")
        notes = upstream_summary(target_tag, token)
        if notes:
            log(f"  已取到上游摘要（{len(notes)} 字符）")
    text, changed = bump_manifest(target_tag, app_version, notes)
    write_manifest(text)
    load_build()

    log("[3/3] 自证：常量确实落到目标值上 ...")
    # 防止「文件改了但正则没匹配上」这种静默失配
    if build.UPSTREAM_TAG != target_tag:
        raise SystemExit(f"ERROR: 改完后 UPSTREAM_TAG={build.UPSTREAM_TAG!r} != {target_tag!r}")
    if manifest_version(read_manifest()) != app_version:
        raise SystemExit(f"ERROR: 改完后 manifest.version 不是 {app_version!r}")
    log("")
    log(f" 完成：上游 {target_tag}，应用版本 {app_version}"
        f"{'，manifest 已更新' if changed else '，manifest 版本未变'}")


if __name__ == "__main__":
    main()
