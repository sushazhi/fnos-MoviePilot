#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mp_updater.py 离线冒烟测试（Windows 也能跑）。

用沙箱目录模拟 fnOS 的安装布局：
  sandbox/appdest/mp          —— 后端源码（旧版 v3.0.3）
  sandbox/appdest/frontend    —— 前端 dist
  sandbox/pkgvar/config       —— CONFIG_DIR（放 app.env 与状态文件）
  sandbox/pkgvar/config/temp/movietpilot-update/ —— 上游"已下载"的安装包

场景：
  A 正常更新（禁用自检以避免真去 import fastapi）：验证替换/资源回填/前端/状态
  B 自检失败：验证自动回滚（版本、资源、前端全部还原）与失败计数
  C 上游产物校验：sha256 不符时忽略
  D 纯函数：版本比较、uv.lock 解析、依赖计划
  E/F 真实网络（可选，不可达时 SKIP）
  G 命令行参数契约
  H 版本发现降级链（API / 网页 latest / atom / 分支 version.py）与镜像配置（离线打桩）
  I 依赖清单的平台 marker 过滤（pyobjc-* 等其他平台专属包不得进安装计划）
  J 源上不可得的版本：本机已装则跳过继续，本机未装则仍然失败
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from argparse import Namespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SANDBOX = ROOT / ".local-build" / "_smoke" / "updater"
REAL_SRC = ROOT / ".local-build" / "_src" / "v303" / "MoviePilot-3.0.3"

os.environ.setdefault("MP_UPDATE_LOG", str(SANDBOX / "update.log"))
sys.path.insert(0, str(ROOT / "app" / "bin"))
import mp_updater as mod  # noqa: E402

# 场景 A 会把 mod.install_dependencies 打桩成 no-op（Windows 上不能真装 179 个包）
# 且**故意不复原**（B 之后的场景都依赖这个桩）。想测真函数必须提前留一份引用，
# 否则测的就是那个 lambda —— 表现为"用例永远通过/永远失败"。
REAL_INSTALL_DEPS = mod.install_dependencies

# 资源文件名不能写死：verify_site_resources 要求扩展 ABI 与**当前解释器**一致
# （cp314-aarch64-linux-gnu.so 是 fnOS 目标平台，在 Windows/cp312 的开发机上会被
# 正确地判为"缺少本机可用的 sites 扩展"）。写死会让整套用例只在目标平台能过，
# 开发机上永远红着，反而掩盖真正的回归。这里直接取更新器的推导结果。
INDEX_NAME, NATIVE_NAME = mod.needed_resource_files()

FAILS = []


def check(label, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + label + (f"  <- {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(label)


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build_sandbox(tag="v3.0.4"):
    if SANDBOX.exists():
        shutil.rmtree(SANDBOX)
    appdest = SANDBOX / "appdest"
    mp = appdest / "mp"
    fe = appdest / "frontend"
    cfgdir = SANDBOX / "pkgvar" / "config"
    tmp = SANDBOX / "pkgvar" / "tmp"
    for d in (mp / "app" / "helper", fe, cfgdir, tmp):
        d.mkdir(parents=True, exist_ok=True)

    (mp / "version.py").write_text(
        "APP_VERSION = 'v3.0.3'\nFRONTEND_VERSION = 'v3.0.3'\n", encoding="utf-8")
    (mp / "app" / "main.py").write_text("print('old backend')\n", encoding="utf-8")
    (mp / "app" / "helper" / INDEX_NAME).write_text("OLD-SITES-DATA", encoding="utf-8")
    (mp / "app" / "helper" / NATIVE_NAME).write_bytes(b"OLD-SO")
    (mp / "app" / "helper" / ".resource-compat").write_text("compat", encoding="utf-8")
    # 空清单：候选范围为空集，真实 pip 安装由各场景自行打桩
    (mp / "requirements.lock.txt").write_text("", encoding="utf-8")
    (fe / "index.html").write_text("<h1>old</h1>", encoding="utf-8")
    (cfgdir / "app.env").write_text("CONFIG_DIR=%s\nMP_UPDATE_DEPS=1\n" % cfgdir,
                                    encoding="utf-8")

    # 新版本源码树（顶层目录名与上游 zip 一致）
    new_root = SANDBOX / "newsrc" / f"MoviePilot-{tag.lstrip('v')}"
    (new_root / "app").mkdir(parents=True, exist_ok=True)
    (new_root / "app" / "main.py").write_text("print('new backend')\n", encoding="utf-8")
    (new_root / "version.py").write_text(
        f"APP_VERSION = '{tag}'\nFRONTEND_VERSION = '{tag}'\n", encoding="utf-8")
    if REAL_SRC.exists():
        for name in ("pyproject.toml", "uv.lock"):
            src = REAL_SRC / name
            if src.exists():
                shutil.copy2(src, new_root / name)

    # 后端 zip（含顶层目录）
    backend_zip = SANDBOX / "backend.zip"
    with zipfile.ZipFile(backend_zip, "w") as zf:
        for p in new_root.rglob("*"):
            zf.write(p, f"{new_root.name}/{p.relative_to(new_root)}")
    # 前端 zip（故意套一层 dist/，验证提升逻辑）
    frontend_zip = SANDBOX / "frontend.zip"
    with zipfile.ZipFile(frontend_zip, "w") as zf:
        zf.writestr("dist/index.html", "<h1>new</h1>")

    up = cfgdir / "temp" / "moviepilot-update"
    up.mkdir(parents=True, exist_ok=True)
    manifest = {
        "targets": ["application"],
        "version": tag,
        "frontend_version": tag,
        "backend_archive": str(backend_zip),
        "frontend_archive": str(frontend_zip),
        "backend_sha256": sha256(backend_zip),
        "frontend_sha256": sha256(frontend_zip),
    }
    (up / "install.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                     encoding="utf-8")

    os.environ["MP_SRC"] = str(mp)
    os.environ["FRONTEND_DIR"] = str(fe)
    os.environ["CONFIG_DIR"] = str(cfgdir)
    os.environ["APP_PYTHON"] = sys.executable
    os.environ["TRIM_PKGTMP"] = str(tmp)
    os.environ["MP_UPDATE_DEPS"] = "1"
    os.environ["MP_AUTO_UPDATE"] = "1"
    os.environ.pop("SHARE_LOG", None)
    return mp, fe, cfgdir


def scenario_a():
    print("\n=== 场景 A：正常更新（复用上游已下载包）===")
    mp, fe, cfgdir = build_sandbox()
    cfg = mod.Config()
    # 两个外部副作用打桩：Windows 上既不能真装 179 个包，也没有 fastapi 可 import
    mod.smoke_test = lambda c: None
    mod.install_dependencies = lambda c, pins: True
    rc = mod.do_update(cfg, Namespace(check=False, rollback=False, force=True))
    check("A1 退出码=10（已更新）", rc == mod.EXIT_UPDATED, f"rc={rc}")
    check("A2 后端版本已升级", "APP_VERSION = 'v3.0.4'" in (mp / "version.py").read_text())
    check("A3 后端代码已替换", "new backend" in (mp / "app" / "main.py").read_text())
    # A4/A5 回填目标用 resolve_resource_dir 推导，而不是写死 app/helper：
    # 回填的本意就是"把资源搬到新版代码认的目录"，新版目录是 app/application/site。
    # 写死旧目录会让断言与实现口径脱节（实现搬对了反而判失败）。
    res_dir = mod.resolve_resource_dir(mp)
    check("A4 资源文件已回填（user.sites）",
          (res_dir / INDEX_NAME).exists()
          and (res_dir / INDEX_NAME).read_text() == "OLD-SITES-DATA", str(res_dir))
    check("A5 资源文件已回填（sites.so）", (res_dir / NATIVE_NAME).exists(), str(res_dir))
    check("A6 前端已替换（dist/ 提升）", "new" in (fe / "index.html").read_text())
    state = json.loads((cfgdir / "mp_update.json").read_text(encoding="utf-8"))
    check("A7 状态文件记录新版本", state.get("backend_version") == "v3.0.4", str(state))
    check("A8 上游 install.json 已消费",
          not (cfgdir / "temp" / "moviepilot-update" / "install.json").exists())
    backups = sorted(p.name for p in (mp.parent / ".mp-backup").iterdir())
    check("A9 已生成备份", len(backups) == 1, str(backups))


def scenario_a2():
    print("\n=== 场景 A2：关闭依赖同步且缺新依赖时应拒绝更新 ===")
    mp, fe, cfgdir = build_sandbox()
    os.environ["MP_UPDATE_DEPS"] = "0"       # 关键：不允许装依赖
    cfg = mod.Config()
    mod.smoke_test = lambda c: None
    mod.install_dependencies = lambda c, pins: True
    rc = mod.do_update(cfg, Namespace(check=False, rollback=False, force=True))
    os.environ["MP_UPDATE_DEPS"] = "1"
    check("A2-1 退出码=1（拒绝更新）", rc == mod.EXIT_FAILED, f"rc={rc}")
    check("A2-2 代码未被改动", "v3.0.3" in (mp / "version.py").read_text())
    check("A2-3 前端未被改动", "old" in (fe / "index.html").read_text())
    state = json.loads((cfgdir / "mp_update.json").read_text(encoding="utf-8"))
    check("A2-4 错误说明指向依赖", "新增依赖" in str(state.get("last_error")))


def scenario_b():
    print("\n=== 场景 B：自检失败应自动回滚 ===")
    mp, fe, cfgdir = build_sandbox()
    cfg = mod.Config()
    mod.install_dependencies = lambda c, pins: True

    def failing_smoke(c):
        raise mod.UpdateError("模拟自检失败")

    mod.smoke_test = failing_smoke
    rc = mod.do_update(cfg, Namespace(check=False, rollback=False, force=True))
    check("B1 退出码=1（失败）", rc == mod.EXIT_FAILED, f"rc={rc}")
    check("B2 版本保持旧版", "v3.0.3" in (mp / "version.py").read_text())
    check("B3 后端代码保持旧版", "old backend" in (mp / "app" / "main.py").read_text())
    check("B4 资源文件仍在",
          (mp / "app" / "helper" / INDEX_NAME).read_text() == "OLD-SITES-DATA")
    check("B5 前端保持旧版", "old" in (fe / "index.html").read_text())
    state = json.loads((cfgdir / "mp_update.json").read_text(encoding="utf-8"))
    check("B6 记录失败次数", int((state.get("failures") or {}).get("v3.0.4", 0)) == 1, str(state))


def scenario_c():
    print("\n=== 场景 C：上游产物校验 ===")
    mp, fe, cfgdir = build_sandbox()
    up = cfgdir / "temp" / "moviepilot-update"
    data = json.loads((up / "install.json").read_text(encoding="utf-8"))
    data["backend_sha256"] = "0" * 64          # 故意改坏
    (up / "install.json").write_text(json.dumps(data), encoding="utf-8")
    cfg = mod.Config()
    check("C1 sha256 不符时忽略产物", mod.upstream_artifacts(cfg, "v3.0.3") is None)
    (up / "install.json").unlink()
    check("C2 无清单时返回 None", mod.upstream_artifacts(cfg, "v3.0.3") is None)
    # 版本不高于当前也应忽略
    mp2, fe2, cfg2 = build_sandbox(tag="v3.0.2")
    cfg = mod.Config()
    check("C3 版本不高于当前时忽略", mod.upstream_artifacts(cfg, "v3.0.3") is None)


def scenario_d():
    print("\n=== 场景 D：纯函数 ===")
    vk = mod.version_key
    check("D1 版本比较 3.0.4 > 3.0.3", vk("v3.0.4") > vk("v3.0.3"))
    check("D2 正式版 > rc", vk("v3.1.0") > vk("v3.1.0-rc1"))
    check("D3 rc > beta", vk("v3.1.0-rc1") > vk("v3.1.0-beta2"))
    check("D4 无法解析的版本不参与比较", vk("dev") == (0,) and vk("dev") < vk("v3.0.0"))
    check("D5 相同版本相等", vk("v3.0.3") == vk("3.0.3"))
    # 上游对同一版本重新打包时会发 v3.0.10-1（长期惯例，见 v2.9.16-2 / v2.13.8-1）。
    # 旧实现把它当"看不懂的版本"，自更新会永久停在旧包上，所以这几条必须钉住。
    check("D11 TAG_RE 接受上游重打包 tag", bool(mod.TAG_RE.match("v3.0.10-1")))
    check("D12 TAG_RE 仍拒绝 v1/v2/dev 与非版本引用",
          not any(mod.TAG_RE.match(t) for t in
                  ("v1.9.19", "v2.9.5", "dev", "v3.0", "v3.0.10-1-2")))
    check("D12b 预发布 tag 仍被 TAG_RE 接受（由通道过滤，而非拒之门外）",
          bool(mod.TAG_RE.match("v3.0.10-beta2")) and mod.is_prerelease("v3.0.10-beta2"))
    check("D13 重打包 > 同版本正式版", vk("v3.0.10-1") > vk("v3.0.10"))
    check("D14 下一版本 > 重打包版", vk("v3.0.11") > vk("v3.0.10-1"))
    check("D15 重打包序号参与排序", vk("v3.0.10-2") > vk("v3.0.10-1"))
    check("D16 重打包不算预发布",
          not mod.is_prerelease("v3.0.10-1") and mod.is_prerelease("v3.0.10-rc1"))
    check("D17 正式版仍 > 同版本预发布",
          vk("v3.0.10-1") > vk("v3.0.10-rc1") and vk("v3.0.10") > vk("v3.0.10-beta2"))

    if not REAL_SRC.exists():
        print("SKIP 依赖相关用例：缺少 .local-build/_src/v303 上游源码")
        return
    pins = mod.uv_lock_pins(REAL_SRC / "uv.lock")
    check("D6 uv.lock 解析出依赖", len(pins) > 100, f"共 {len(pins)} 个")
    check("D7 项目自身被排除", "moviepilot" not in pins, str(sorted(pins)[:5]))
    direct = mod.pyproject_direct_deps(REAL_SRC / "pyproject.toml")
    check("D8 pyproject 直接依赖解析", "fastapi" in direct and "httpx" in direct)
    # 候选范围为空（base/installed/direct 都不认识）时不产生安装计划。
    # 注意 direct（pyproject 直接依赖）本身也是候选来源，所以这里用"只有 uv.lock、
    # 没有 pyproject"的目录来隔离，否则 uv.lock 里上游直接依赖仍会被纳入。
    mp, fe, cfgdir = build_sandbox()
    lock_only = SANDBOX / "lockonly"
    lock_only.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REAL_SRC / "uv.lock", lock_only / "uv.lock")
    cfg = mod.Config()
    # 打桩"已装清单"：候选范围 = 随包清单 ∪ 已装环境 ∪ 直接依赖，而开发机上往往
    # 恰好装着 requests/urllib3 之类同名不同版本的包，会让"候选范围为空"这个前提
    # 不成立（用例在开发机假失败、在干净环境才通过）。显式清空才测的是意图。
    real_installed = mod.installed_versions
    mod.installed_versions = lambda: {}
    to_install, missing = mod.plan_dependencies(cfg, lock_only, {})
    check("D9 无候选范围时不装包", to_install == [], f"{len(to_install)} 个")
    # 候选范围含 fastapi 时应产生计划（fastapi 必然已装或缺失）
    to_install2, missing2 = mod.plan_dependencies(cfg, lock_only, {"fastapi": "0.100.0"})
    mod.installed_versions = real_installed
    check("D10 候选范围内会产出计划", any(p.startswith("fastapi==") for p in to_install2),
          str(to_install2[:3]))


def scenario_e():
    print("\n=== 场景 E：真实网络检查（可选）===")
    mp, fe, cfgdir = build_sandbox()
    cfg = mod.Config()
    try:
        rc = mod.do_check(cfg)
        check("E1 版本检查联网成功", rc == mod.EXIT_OK, "rc=%s" % rc)
    except Exception as e:  # noqa: BLE001
        print(f"SKIP 网络不可达：{e}")


def scenario_f():
    print("\n=== 场景 F：真实下载链路（GitHub / 加速代理）===")
    cfg = mod.Config()
    dl = SANDBOX / "dl"
    dl.mkdir(parents=True, exist_ok=True)
    target = dl / "backend.zip"
    try:
        ok = mod.download_file(
            mod.BACKEND_ZIP.format(repo=mod.BACKEND_REPO, tag="v3.0.3"),
            target, cfg.proxies, timeout=120)
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"SKIP 下载异常：{e}")
    if not ok:
        print("SKIP 后端包下载失败（网络或加速代理不可达）")
        return
    size = target.stat().st_size
    check("F1 后端 zip 可下载", size > 1_000_000, f"{size} 字节")
    with zipfile.ZipFile(target) as zf:
        names = zf.namelist()
    check("F2 zip 顶层目录符合预期", bool(names) and names[0].startswith("MoviePilot-"),
          names[0] if names else "空")
    check("F3 zip 内含 version.py", any(n.endswith("/version.py") for n in names))
    fe_target = dl / "frontend.zip"
    if mod.download_file(mod.FRONTEND_ZIP.format(repo=mod.FRONTEND_REPO, tag="v3.0.3"),
                         fe_target, cfg.proxies, timeout=120):
        check("F4 前端 dist.zip 可下载", fe_target.stat().st_size > 100_000)
    else:
        print("SKIP 前端包下载失败")


def scenario_g():
    """G 命令行契约：cmd/main 传的每个开关都必须是合法参数。

    历史故障：cmd/main 的启动路径调用 `mp_updater --auto`，而 argparse 没有
    定义 --auto，于是解析阶段直接 SystemExit(2)，do_update() 从未执行 ——
    "重启即升级"静默失效（rc=2 被 cmd/main 的 `*)` 分支吞掉）。

    这条检查不依赖平台与网络，直接钉住"脚本会拒绝自己的调用方"这类问题。
    """
    print("\n=== 场景 G：命令行参数契约（与 cmd/main 保持一致）===")
    script = ROOT / "app" / "bin" / "mp_updater.py"
    expected = ["--check", "--rollback", "--force", "--auto", "--resources"]

    # 1) 直接解析 argparse 定义，而不真的执行各开关：
    #    --force / --rollback 会真的联网下载或回滚，跑起来既慢又不确定。
    r = subprocess.run([sys.executable, str(script), "--help"],
                       capture_output=True, text=True, timeout=60)
    help_text = (r.stdout or "") + (r.stderr or "")
    for name in expected:
        check(f"G1 --help 列出 {name}", name in help_text,
              help_text.strip().splitlines()[-1] if help_text else "(无输出)")

    # 2) 关键回归：--auto 必须能被解析。
    #    注意不能只看 rc==2 —— 脚本自身也用 EXIT_USAGE=2 表示"环境/用法错误"，
    #    两者撞码。argparse 拒绝的**唯一可靠特征**是它打印 "unrecognized arguments"。
    for name in ("--auto", "--force"):
        try:
            r = subprocess.run(
                [sys.executable, str(script), name],
                capture_output=True, text=True, timeout=45,
                env={**os.environ,
                     "CONFIG_DIR": str(SANDBOX / "pkgvar" / "config"),
                     "MP_SRC": str(SANDBOX / "appdest" / "mp"),
                     # 关掉自动更新，让 --auto 立刻走"跳过"分支，避免联网
                     "MP_AUTO_UPDATE": "0"})
        except subprocess.TimeoutExpired:
            # 超时说明参数已被接受并进入了真实流程（旧代码是瞬间被拒绝）
            check(f"G2 {name} 被接受（进入主流程而非 argparse 拒绝）", True)
            continue
        out = (r.stdout or "") + (r.stderr or "")
        rejected = "unrecognized arguments" in out
        check(f"G2 {name} 不是 argparse 拒绝的参数", not rejected,
              f"rc={r.returncode} {out.strip()[:200]}")

    # 3) 钉住 cmd/main 实际使用的调用形式，防止两边再次漂移
    main_sh = (ROOT / "cmd" / "main").read_text(encoding="utf-8", errors="replace")
    used = set()
    for line in main_sh.splitlines():
        s = line.strip()
        if s.startswith("run_updater"):
            for tok in s.split()[1:]:
                if tok.startswith("--"):
                    used.add(tok)
    missing = sorted(t for t in used if t not in expected)
    check("G3 cmd/main 用到的开关都在预期集合内", not missing, f"未定义: {missing}")


def scenario_h():
    """H 版本发现降级链：API → 网页 latest → releases.atom，以及镜像配置。

    真实故障（本次修复的起点）：api.github.com 被网络挡住或镜像不转发该域名时，
    旧实现直接放弃更新；而"下载"走的 github.com 通道其实是通的。这里全部用打桩
    钉住降级链、通道过滤与失败后的重试冷却，不依赖网络。
    """
    print("\n=== 场景 H：版本发现降级链（离线打桩）===")
    mp, fe, cfgdir = build_sandbox()

    check("H1 预发布后缀被识别", mod.is_prerelease("v3.1.0-beta2")
          and mod.is_prerelease("v3.1.0.rc1"))
    check("H2 正式版不误判", not mod.is_prerelease("v3.0.4"))

    latest_html = ('<html><head><meta property="og:url" '
                   'content="https://github.com/jxxghp/MoviePilot/releases/tag/v3.0.4" />'
                   "</head><body>...</body></html>")
    check("H3 从 og:url 取 tag", mod.tag_from_release_page(latest_html) == "v3.0.4")
    check("H4 canonical 兜底",
          mod.tag_from_release_page('<link rel="canonical" href='
                                    '"https://github.com/x/y/releases/tag/v3.0.5">') == "v3.0.5")

    atom = ('<feed>'
            '<entry><link rel="alternate" href="https://github.com/jxxghp/MoviePilot'
            '/releases/tag/v3.1.0-beta2"/><title>v3.1.0-beta2</title>'
            '<updated>2026-09-10T00:00:00Z</updated></entry>'
            '<entry><link rel="alternate" href="https://github.com/jxxghp/MoviePilot'
            '/releases/tag/v3.0.4"/><title>v3.0.4</title>'
            '<updated>2026-09-01T00:00:00Z</updated></entry>'
            "</feed>")
    parsed = mod.parse_atom_releases(atom)
    check("H5 atom 解析保序（新 → 旧）",
          [t[0] for t in parsed] == ["v3.1.0-beta2", "v3.0.4"], str(parsed))
    check("H6 atom 带出发布时间", parsed[1][2].startswith("2026-09-01"), str(parsed[1]))

    real_json, real_text, real_fetch = mod.http_json, mod.fetch_text, mod.fetch_latest_release
    os.environ["MP_UPDATE_CHANNEL"] = "release"

    # API 全挂 -> 网页 latest 兜底
    mod.http_json = lambda *a, **k: None
    mod.fetch_text = lambda *a, **k: (
        "https://github.com/jxxghp/MoviePilot/releases/tag/v3.0.4", latest_html)
    tag, _meta = mod.fetch_latest_release(mod.Config())
    check("H7 API 失败时网页 latest 兜底", tag == "v3.0.4", f"tag={tag}")

    # API 与网页 latest 都挂 -> atom 兜底；正式版通道必须跳过顶部的预发布
    mod.fetch_text = lambda url, *a, **k: (None, atom if "atom" in url else "")
    tag, _meta = mod.fetch_latest_release(mod.Config())
    check("H8 atom 兜底并跳过预发布（正式版通道）", tag == "v3.0.4", f"tag={tag}")

    os.environ["MP_UPDATE_CHANNEL"] = "prerelease"
    tag, _meta = mod.fetch_latest_release(mod.Config())
    check("H9 prerelease 通道取最新预发布", tag == "v3.1.0-beta2", f"tag={tag}")
    os.environ["MP_UPDATE_CHANNEL"] = "release"

    # 全部"页面型"通道都挂 -> 分支 version.py 兜底（raw 是文件型 URL，镜像普遍转发）
    def raw_only(url, *a, **k):
        if "raw.githubusercontent.com" not in url:
            return None, ""
        if "/v3/" in url:
            return url, "APP_VERSION = 'v3.0.4'\nFRONTEND_VERSION = 'v3.0.4'\n"
        # main / master 上是 v1 时代的版本号，必须被 TAG_RE 挡掉
        return url, "APP_VERSION = 'v1.9.19'\n"

    mod.fetch_text = raw_only
    tag, _meta = mod.fetch_latest_release(mod.Config())
    check("H10 末级 raw 分支兜底", tag == "v3.0.4", f"tag={tag}")

    mod.fetch_text = lambda url, *a, **k: (
        url if "raw.githubusercontent.com" in url else None,
        "APP_VERSION = 'v1.9.19'\n" if "raw.githubusercontent.com" in url else "")
    tag, _meta = mod.fetch_latest_release(mod.Config())
    check("H11 raw v1 分支版本号被过滤", tag is None, f"tag={tag}")

    mod.http_json, mod.fetch_text = real_json, real_text

    # 镜像配置：GITHUB_PROXY_MIRRORS 逗号分隔追加，非法项忽略，与内置项去重
    os.environ["GITHUB_PROXY"] = "https://mirror-a.example/"
    os.environ["GITHUB_PROXY_MIRRORS"] = "https://a.example/ , not-a-url , https://b.example"
    check("H12 镜像列表去重保序",
          mod.Config().proxies[:3] == ("https://mirror-a.example/", "https://a.example/",
                                       "https://b.example/"),
          str(mod.Config().proxies))
    os.environ.pop("GITHUB_PROXY_MIRRORS", None)
    os.environ.pop("GITHUB_PROXY", None)

    # 内置列表的**顺序**是契约：gh.dpik.top 必须排第一（实测最快），v4.gh-proxy.org 次之；
    # 已失效的镜像（ghfast.top 对 jxxghp/* 全量 403；gh-proxy.com / gh-proxy.org 本机
    # 连不上或速度≈0）必须不在列表里，留着只会白等一个失败周期。用真实常量而非硬编码
    # 字符串，避免改常量时测试跟着一起改。
    check("H18 内置镜像顺序与内容正确",
          mod.DEFAULT_PROXIES == ("https://gh.dpik.top/", "https://v4.gh-proxy.org/")
          and all(p.endswith("/") for p in mod.DEFAULT_PROXIES),
          str(mod.DEFAULT_PROXIES))

    # 只有带 v4. 前缀的子域可用，裸 gh-proxy.org 基本不通 —— 写错就整条通道失效。
    check("H19 代理前缀带 v4. 子域",
          all("gh-proxy.org" not in p or p == "https://v4.gh-proxy.org/"
              for p in mod.DEFAULT_PROXIES),
          str(mod.DEFAULT_PROXIES))

    # API 必须只直连：带上加速前缀时，不转发 api 的镜像会挂到 SSL 握手超时而不是
    # 快速报错，白白拖长启动路径的更新检查（实测 ghfast.top / gh-proxy.org）。
    seen = []
    mod.http_json = lambda url, proxies, **k: (seen.append((url, proxies)), None)[1]
    mod.fetch_text = lambda *a, **k: (None, "")
    mod.fetch_latest_release(mod.Config())
    check("H13 API 只走直连（不带加速前缀）",
          len(seen) == 1 and seen[0][1] == (), str(seen))
    mod.http_json, mod.fetch_text = real_json, real_text

    # 上游"对同一版本重新打包"的 tag（v3.0.10-1）必须能走完整条发现链。
    # 真实故障：上游发了 v3.0.10-1 之后，旧 TAG_RE 把它滤掉，正式版通道在
    # API / 网页 / atom 三处都"找不到版本"，自更新静默失效。
    mod.http_json = lambda *a, **k: {"tag_name": "v3.0.10-1", "name": "v3.0.10-1"}
    mod.fetch_text = lambda *a, **k: (None, "")
    tag, _meta = mod.fetch_latest_release(mod.Config())
    check("H16 API 返回重打包 tag 时被接受", tag == "v3.0.10-1", f"tag={tag}")

    atom_rebuild = ('<feed>'
                    '<entry><link rel="alternate" href="https://github.com/jxxghp/MoviePilot'
                    '/releases/tag/v3.0.10-1"/><title>v3.0.10-1</title>'
                    '<updated>2026-09-28T10:40:38Z</updated></entry>'
                    "</feed>")
    mod.http_json = lambda *a, **k: None
    mod.fetch_text = lambda url, *a, **k: (None, atom_rebuild if "atom" in url else "")
    tag, _meta = mod.fetch_latest_release(mod.Config())
    check("H17 正式版通道不把重打包 tag 当预发布跳过",
          tag == "v3.0.10-1", f"tag={tag}")

    # 上游发新版本后必须能从重打包版继续前进（否则会永久停在 v3.0.10-1）
    mod.http_json = lambda *a, **k: {"tag_name": "v3.0.11", "name": "v3.0.11"}
    tag, _meta = mod.fetch_latest_release(mod.Config())
    check("H18 新版本能被发现（重打包不是终点）", tag == "v3.0.11", f"tag={tag}")
    mod.http_json, mod.fetch_text = real_json, real_text

    mp, fe, cfgdir = build_sandbox()
    (cfgdir / "temp" / "moviepilot-update" / "install.json").unlink()  # 逼它走联网查版本
    os.environ["MP_UPDATE_INTERVAL"] = "21600"
    mod.fetch_latest_release = lambda c: (None, None)
    rc = mod.do_update(mod.Config(), Namespace(check=False, rollback=False, force=True))
    state = json.loads((cfgdir / "mp_update.json").read_text(encoding="utf-8"))
    remaining = 21600 - (time.time() - float(state.get("checked_at") or 0))
    check("H14 发现失败返回失败码", rc == mod.EXIT_FAILED, f"rc={rc}")
    check("H15 失败后仅待 RETRY_AFTER_FAILURE 再查",
          abs(remaining - mod.RETRY_AFTER_FAILURE) < 10, f"remaining={remaining:.0f}s")
    mod.fetch_latest_release = real_fetch
    os.environ.pop("MP_UPDATE_INTERVAL", None)


def scenario_i():
    """I 依赖清单的平台 marker 过滤（离线）。

    真实故障：requirements.lock.txt 由 `uv export` 生成，是 **universal** 清单 ——
    darwin 专属的 pyobjc-* 带着 marker 躺在里面。旧实现只看包名，于是在 Linux 的
    NAS 上试图 pip install pyobjc-core，必然失败（No matching distribution /
    Failed to build），整个自更新因此回滚，"重启即升级"永久失效。
    """
    print("\n=== 场景 I：依赖清单的平台 marker 过滤 ===")
    mp, fe, cfgdir = build_sandbox()
    cfg = mod.Config()

    check("I1 空 marker 放行", mod.marker_allows(""))
    check("I2 未知变量保守放行（不误删本机依赖）",
          mod.marker_allows("python_version >= '3.14'"))
    check("I3 extra 条件放行（由导出 group 决定）", mod.marker_allows("extra == 'runtime'"))

    darwin_only = ("(platform_machine == 'arm64' and sys_platform == 'darwin') or "
                   "(platform_machine == 'x86_64' and sys_platform == 'darwin')")
    env = mod._marker_env()
    check("I4 darwin 专属 marker 按平台判定",
          mod.marker_allows(darwin_only) == (env["sys_platform"] == "darwin"), str(env))

    # 真实清单节选（与 uv export 的输出同形）
    (mp / "requirements.lock.txt").write_text(
        "# 节选（uv export 的 universal 输出）\n"
        "moviepilot-rust==0.3.5 ; (platform_machine == 'arm64' and sys_platform == 'darwin')"
        " or (platform_machine == 'x86_64' and sys_platform == 'darwin')"
        " or (platform_machine == 'aarch64' and sys_platform == 'linux')"
        " or (platform_machine == 'x86_64' and sys_platform == 'linux')"
        " or (platform_machine == 'AMD64' and sys_platform == 'win32')\n"
        f"pyobjc-core==12.2.2 ; {darwin_only}\n"
        "pywin32==312 ; platform_machine == 'AMD64' and sys_platform == 'win32'\n"
        "orjson==3.12.0\n",
        encoding="utf-8")
    base = mod.packaged_pins(cfg)
    check("I5 base 里没有 pyobjc-core", "pyobjc-core" not in base, str(sorted(base)))
    check("I6 pywin32 只在 win32 平台保留",
          ("pywin32" in base) == (env["sys_platform"] == "win32"), str(sorted(base)))
    check("I7 无条件依赖照常解析出版本号", base.get("orjson") == "3.12.0", str(base))

    # 端到端：真实 uv.lock 下的依赖计划里不得出现任何 pyobjc-*
    real_installed = mod.installed_versions
    mod.installed_versions = lambda: {"moviepilot-rust": "0.0.1", "orjson": "0.0.1",
                                      "pystray": "0.19.5"}
    new_root = SANDBOX / "newsrc" / "MoviePilot-3.0.4"
    pins, missing = mod.plan_dependencies(cfg, new_root, base)
    mod.installed_versions = real_installed
    if pins:
        check("I8 依赖计划里不含 pyobjc-*",
              not any("pyobjc" in p for p in pins), str(pins))
        if "moviepilot-rust" in base:
            check("I9 平台相关的包仍会被计划升级",
                  any(p.startswith("moviepilot-rust") for p in pins), str(pins))
        else:
            print("SKIP I9 当前平台不匹配 moviepilot-rust 的 marker")
    else:
        print("SKIP I8/I9 缺少本地 uv.lock 样本（.local-build/_src）")


def scenario_j():
    """J 依赖容错：源上没有该版本时不整体失败（离线打桩）。

    真实故障：上游 uv.lock 引用 moviepilot-rust==0.3.6，而各镜像最高只有 0.3.5。
    旧实现直接判"依赖同步失败"并回滚 —— 一次发布顺序问题就让"重启即升级"永久失效。
    现在的口径：所有镜像都试过仍缺该版本 + **本机已装**同名包 → 警告跳过；
    本机没装的包（缺失依赖）→ 仍然失败，绝不放过。
    """
    print("\n=== 场景 J：源上不可得的版本如何处理（离线打桩）===")
    mp, fe, cfgdir = build_sandbox()
    cfg = mod.Config()

    class _FailResult:
        returncode = 1
        stdout = ""
        stderr = ("ERROR: Could not find a version that satisfies the requirement "
                  "moviepilot-rust==0.3.6 (from versions: 0.3.5)\n"
                  "ERROR: No matching distribution found for moviepilot-rust==0.3.6\n")

    real_run = mod.subprocess.run
    real_mirrors = mod.PIP_MIRRORS
    real_installed = mod.installed_versions
    mod.subprocess.run = lambda *a, **k: _FailResult()
    mod.PIP_MIRRORS = ("https://mirror-a/", "https://mirror-b/")

    mod.installed_versions = lambda: {"moviepilot-rust": "0.3.5"}
    check("J1 本机已装的包在源上不可得 → 跳过并继续",
          REAL_INSTALL_DEPS(cfg, ["moviepilot-rust==0.3.6"]) is True)

    mod.installed_versions = lambda: {}
    check("J2 本机未装的包不可得 → 仍然失败（缺失依赖不放过）",
          REAL_INSTALL_DEPS(cfg, ["moviepilot-rust==0.3.6"]) is False)

    mod.subprocess.run = real_run
    mod.PIP_MIRRORS = real_mirrors
    mod.installed_versions = real_installed


def build_resource_sandbox():
    """为场景 K 造一个"当前目录布局"的沙箱（资源在 app/application/site）。

    不复用 build_sandbox()：那里资源故意放在旧目录 app/helper（测回填），
    而资源同步要测的是"新版目录里就地升级"，两件事的起点不同。
    """
    root = SANDBOX / "res"
    if root.exists():
        shutil.rmtree(root)
    mp = root / "mp"
    res_dir = mp / "app" / "application" / "site"
    cfgdir = root / "pkgvar" / "config"
    tmp = root / "pkgvar" / "tmp"
    for d in (res_dir, cfgdir, tmp):
        d.mkdir(parents=True, exist_ok=True)
    (mp / "version.py").write_text("APP_VERSION = 'v3.0.3'\n", encoding="utf-8")
    (res_dir / "user.sites.v3.bin").write_text("OLD-INDEX", encoding="utf-8")
    index_name, native_name = mod.needed_resource_files()
    (res_dir / native_name).write_bytes(b"OLD-NATIVE")
    (cfgdir / "app.env").write_text("CONFIG_DIR=%s\n" % cfgdir, encoding="utf-8")
    os.environ["MP_SRC"] = str(mp)
    os.environ["CONFIG_DIR"] = str(cfgdir)
    os.environ["TRIM_PKGTMP"] = str(tmp)
    os.environ["APP_PYTHON"] = sys.executable
    os.environ.pop("MP_AUTO_UPDATE_RESOURCE", None)
    return mp, res_dir, cfgdir, index_name, native_name


def resource_manifest(index_name, native_name, *, index_version="3.0.17",
                      native_version="3.0.4", target="app/application/site",
                      platform_name=None, drop=None, extra=None):
    """按本机所需文件名造一份 package.v3.json（内容与上游同构）。"""
    if platform_name is None:
        platform_name = mod.resource_platform()
    resources = {
        index_name: {"type": "sites", "target": target, "version": index_version},
        native_name: {"type": "auth", "platform": platform_name, "target": target,
                      "version": native_version},
    }
    for name in (drop or ()):
        resources.pop(name, None)
    resources.update(extra or {})
    return json.dumps({"version": "20", "resources": resources})


def scenario_k():
    """K 站点资源同步（认证扩展 + 站点索引，独立发布通道）。

    这是本仓库原先完全缺失的一半：主程序走 Release 更新，站点资源只在重装 fpk 时
    才跟着变。真实故障形态是"扩展换了、索引没换"→ 站点列表解不开，而文件一个不少。
    所以这里的断言重点是：**成对替换 + 失败整批回滚 + 不阻塞启动**。
    全部离线打桩（不碰网络），并把版本探测替换成确定值（Windows 上无法真加载扩展）。
    """
    print("\n=== 场景 K：站点资源同步（认证扩展 + 站点索引）===")
    mp, res_dir, cfgdir, index_name, native_name = build_resource_sandbox()
    cfg = mod.Config()

    real_fetch, real_download = mod.fetch_text, mod.download_file
    real_probe, real_verify = mod.probe_local_resource_versions, mod.verify_site_resources

    # 版本探测打桩：按索引文件内容判断"装了没有"，模拟真实扩展的 auth_version/
    # indexer_version（本机是 Windows/cp312，真加载 .pyd 不现实）。
    #
    # 返回值必须是 ProbeResult —— 探测结果与"探测本身能否得出结论"是两件事，
    # 调用方靠 .versions 为 None 区分（历史缺陷正是把两者混同）。
    def fake_probe(c):
        if (res_dir / "user.sites.v3.bin").read_text(encoding="utf-8") == "NEW-INDEX":
            return mod.ProbeResult(("3.0.4", "3.0.17"), "", "")
        return mod.ProbeResult(("3.0.3", "3.0.12"), "", "")

    downloaded = []

    def fake_download(url, dest, proxies, timeout=60):
        downloaded.append(url)
        name = Path(dest).name
        Path(dest).write_text("NEW-INDEX" if name == index_name else "NEW-NATIVE",
                              encoding="utf-8")
        return True

    manifest = resource_manifest(index_name, native_name)
    mod.fetch_text = lambda url, proxies, timeout, deadline=None, limit=0: (url, manifest)
    mod.download_file = fake_download
    mod.probe_local_resource_versions = fake_probe
    mod.verify_site_resources = lambda src: None

    # K1 清单 target 必须等于当前源码里资源真正所在的目录（写死上游当前布局）
    check("K1 资源 target 为 app/application/site",
          mod.RESOURCE_TARGET == Path("app/application/site"), str(mod.RESOURCE_TARGET))
    # K2 ABI 标签取自**当前解释器**，而不是写死的 cp314：更新器由 $APP_PYTHON 启动，
    # 若这里写死，换运行时后会把 ABI 不符的扩展装进去（ImportError，后端起不来）。
    py_tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    check("K2 扩展名含当前解释器 ABI 标签", py_tag in native_name, native_name)

    # K3 旧版本 → 触发同步，退出码 11
    rc = mod.do_resources(cfg, Namespace(force=False))
    check("K3 有更新时退出码=11", rc == mod.EXIT_RESOURCE_UPDATED, f"rc={rc}")
    check("K4 索引已替换", (res_dir / "user.sites.v3.bin").read_text(encoding="utf-8") == "NEW-INDEX")
    check("K5 认证扩展已替换（成对）", (res_dir / native_name).read_text(encoding="utf-8") == "NEW-NATIVE")
    check("K6 两个文件都下载（索引+扩展）", len(downloaded) == 2, str(downloaded))
    state = json.loads((cfgdir / "mp_update.json").read_text(encoding="utf-8"))
    check("K7 状态文件记录资源版本",
          (state.get("resource_versions") or {}).get("indexer") == "3.0.17", str(state))
    check("K8 资源检查时间戳与主程序分开记",
          "resource_checked_at" in state and "checked_at" not in state, str(sorted(state)))
    backups = sorted((mp.parent / ".mp-res-backup").iterdir())
    check("K9 生成资源备份（含旧索引）", len(backups) == 1 and
          (backups[0] / "user.sites.v3.bin").read_text(encoding="utf-8") == "OLD-INDEX",
          str(backups))
    # K10 资源备份必须是 .mp-backup 的兄弟目录：若挂在它下面，`--rollback`（按目录名
    # 取最近一次）会把这个只含两个资源文件的目录当成主程序备份去恢复。
    check("K10 资源备份不在主程序备份目录内",
          not str(cfg.resource_backup_root).startswith(str(cfg.backup_root) + os.sep),
          f"{cfg.resource_backup_root} vs {cfg.backup_root}")

    # K11 冷却：同一次会话内再跑不重复下载
    before = len(downloaded)
    rc = mod.do_resources(cfg, Namespace(force=False))
    check("K11 冷却期内不重复下载", rc == mod.EXIT_OK and len(downloaded) == before, f"rc={rc}")
    # K12 已是最新（--force 绕过冷却）→ 0，且不再下载
    rc = mod.do_resources(cfg, Namespace(force=True))
    check("K12 已是最新时退出码=0 且不下载",
          rc == mod.EXIT_OK and len(downloaded) == before, f"rc={rc}")

    # K13 清单缺文件 → 拒绝（绝不半装）
    mod.fetch_text = lambda url, proxies, timeout, deadline=None, limit=0: (
        url, resource_manifest(index_name, native_name, drop=(native_name,)))
    try:
        mod.do_resources(cfg, Namespace(force=True))
        check("K13 清单缺文件时拒绝", False, "未抛错")
    except mod.UpdateError as e:
        check("K13 清单缺文件时拒绝", "资源包清单缺少当前平台文件" in str(e), str(e))

    # K14 平台不符 → 拒绝（跨平台装错 ABI 是 ImportError）
    mod.fetch_text = lambda url, proxies, timeout, deadline=None, limit=0: (
        url, resource_manifest(index_name, native_name, platform_name="MacOS"))
    try:
        mod.do_resources(cfg, Namespace(force=True))
        check("K14 平台不符时拒绝", False, "未抛错")
    except mod.UpdateError as e:
        check("K14 平台不符时拒绝", "资源包平台不匹配" in str(e), str(e))

    # K15 target 指向别处 → 拒绝（清单被篡改时不许写到任意目录）
    mod.fetch_text = lambda url, proxies, timeout, deadline=None, limit=0: (
        url, resource_manifest(index_name, native_name, target="app/helper"))
    try:
        mod.do_resources(cfg, Namespace(force=True))
        check("K15 非法 target 时拒绝", False, "未抛错")
    except mod.UpdateError as e:
        check("K15 非法 target 时拒绝", "资源包目标目录不安全" in str(e), str(e))

    # K16 下载失败 → 不改动现有资源（下载全成功才动运行目录）
    mod.fetch_text = lambda url, proxies, timeout, deadline=None, limit=0: (url, manifest)
    mod.download_file = lambda *a, **k: False
    mod.probe_local_resource_versions = lambda c: mod.ProbeResult(("3.0.3", "3.0.12"), "", "")
    rc = mod.do_resources(cfg, Namespace(force=True))
    check("K16 下载失败时退出码=1", rc == mod.EXIT_FAILED, f"rc={rc}")
    check("K17 下载失败后资源原样保留",
          (res_dir / "user.sites.v3.bin").read_text(encoding="utf-8") == "NEW-INDEX"
          and (res_dir / native_name).read_text(encoding="utf-8") == "NEW-NATIVE")

    # K18 安装后校验失败 → 整批回滚到**改动前**的资源（成对回滚）
    # 先把磁盘上的资源改成一组已知的"旧值"，这样"回滚成功"才可证伪：
    # 如果只断言"文件还在"，装坏不回滚也会通过。
    (res_dir / "user.sites.v3.bin").write_text("PREV-INDEX", encoding="utf-8")
    (res_dir / native_name).write_text("PREV-NATIVE", encoding="utf-8")
    mod.download_file = fake_download          # 会写出 NEW-INDEX / NEW-NATIVE
    mod.verify_site_resources = lambda src: (_ for _ in ()).throw(
        mod.UpdateError("模拟校验失败"))
    rc = mod.do_resources(cfg, Namespace(force=True))
    check("K18 校验失败时退出码=1", rc == mod.EXIT_FAILED, f"rc={rc}")
    check("K19 校验失败后索引回滚到改动前",
          (res_dir / "user.sites.v3.bin").read_text(encoding="utf-8") == "PREV-INDEX",
          (res_dir / "user.sites.v3.bin").read_text(encoding="utf-8"))
    check("K19b 校验失败后扩展一并回滚（成对）",
          (res_dir / native_name).read_text(encoding="utf-8") == "PREV-NATIVE",
          (res_dir / native_name).read_text(encoding="utf-8"))
    state = json.loads((cfgdir / "mp_update.json").read_text(encoding="utf-8"))
    check("K20 失败被计入 resource_failures", bool(state.get("resource_failures")), str(state))

    # K21 开关关闭 → 不检查不下载（MP_AUTO_UPDATE_RESOURCE=0）
    os.environ["MP_AUTO_UPDATE_RESOURCE"] = "0"
    cfg_off = mod.Config()
    before = len(downloaded)
    rc = mod.do_resources(cfg_off, Namespace(force=False))
    check("K21 开关关闭时不下载", rc == mod.EXIT_OK and len(downloaded) == before, f"rc={rc}")
    os.environ.pop("MP_AUTO_UPDATE_RESOURCE", None)

    # K22 失败冷却：连续失败达到上限后**不再重试**（与主程序 MAX_FAILURES 同口径）。
    # 每次先把检查时间戳清零让冷却过期 —— 否则测的是冷却分支，不是失败计数分支。
    def _expire_cooldown():
        st = json.loads((cfgdir / "mp_update.json").read_text(encoding="utf-8"))
        st["resource_checked_at"] = 0
        (cfgdir / "mp_update.json").write_text(json.dumps(st), encoding="utf-8")

    mod.verify_site_resources = lambda src: (_ for _ in ()).throw(mod.UpdateError("x"))
    for _ in range(mod.MAX_FAILURES):
        _expire_cooldown()
        mod.do_resources(mod.Config(), Namespace(force=False))
    state = json.loads((cfgdir / "mp_update.json").read_text(encoding="utf-8"))
    fails = int((state.get("resource_failures") or {}).get("20", 0))
    check("K22a 连续失败被累计", fails == mod.MAX_FAILURES, str(fails))
    _expire_cooldown()
    before = len(downloaded)
    rc = mod.do_resources(mod.Config(), Namespace(force=False))
    check("K22b 达到失败上限后不再尝试（冷却过期也不下载）",
          rc == mod.EXIT_OK and len(downloaded) == before, f"rc={rc}")

    # K23 只读检查不落盘（--check --resources）：此刻磁盘仍是 PREV-*（旧），
    # 清单是 3.0.4/3.0.17，所以应报"有更新"。
    mod.verify_site_resources = lambda src: None
    mod.fetch_text = lambda url, proxies, timeout, deadline=None, limit=0: (url, manifest)
    info = mod.check_resources(mod.Config())
    check("K23 check_resources 报告本地/远端版本并判定有更新",
          info.get("local") == {"auth": "3.0.3", "indexer": "3.0.12"}
          and info.get("latest") == {"auth": "3.0.4", "indexer": "3.0.17"}
          and info.get("update_available") is True, str(info))

    # K24 中途安装失败（第二个文件复制失败）→ 第一个文件也必须回滚。
    # 这是最容易漏的一条：安装是逐个文件做的，若等函数返回才拿到回滚清单，
    # 已经替换掉的那个就漏在回滚之外 —— 结果正是"扩展换了、索引没换"的坏状态。
    (res_dir / "user.sites.v3.bin").write_text("PREV-INDEX", encoding="utf-8")
    (res_dir / native_name).write_text("PREV-NATIVE", encoding="utf-8")
    mod.verify_site_resources = lambda src: None
    real_copy2 = mod.shutil.copy2
    calls = {"n": 0}

    def flaky_copy2(src, dst, *a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("模拟磁盘写入失败")
        return real_copy2(src, dst, *a, **k)

    mod.shutil.copy2 = flaky_copy2
    rc = mod.do_resources(cfg, Namespace(force=True))
    mod.shutil.copy2 = real_copy2
    check("K24 中途安装失败时退出码=1", rc == mod.EXIT_FAILED, f"rc={rc}")
    check("K25 已替换的第一个文件被回滚",
          (res_dir / "user.sites.v3.bin").read_text(encoding="utf-8") == "PREV-INDEX",
          (res_dir / "user.sites.v3.bin").read_text(encoding="utf-8"))
    check("K26 第二个文件保持原样",
          (res_dir / native_name).read_text(encoding="utf-8") == "PREV-NATIVE",
          (res_dir / native_name).read_text(encoding="utf-8"))

    # ---------------------------------------------------------------------
    # K27-K30 回归：探测不可用 ≠ 扩展损坏
    #
    # 真机故障（2026-10-09，fnOS arm64）：应用启用的启动窗口内，探测子进程以
    # rc=1 退出且不留哨兵行。旧代码把"探测没得出结论"当成"扩展不可导入"：
    #     if local and not verified: raise UpdateError("...认证扩展不可导入")
    # 而 local 是探测失败后退回状态文件的值，非空即"武装"了守卫 —— 于是这台
    # 机器上资源更新**永远**失败，每次启动重复，直到 MAX_FAILURES 静默跳过。
    # 下面用真实的 do_resources 覆盖这两个方向。
    # ---------------------------------------------------------------------
    def _probe_returns(versions):
        def _p(c):
            if versions:
                return mod.ProbeResult(versions, "", "")
            return mod.ProbeResult(None, "Traceback (most recent call last):\n  ...\n"
                                         "ModuleNotFoundError: simulated probe failure",
                                   "探测无结果（rc=1）")
        return _p

    state_file = cfgdir / "mp_update.json"

    def _reset_failures(versions=None):
        """K22 已把失败计数顶到 MAX_FAILURES；--force 只绕过冷却，**不**绕过
        失败上限（见 do_resources 的 `not force and ...MAX_FAILURES` 判断），
        所以这里必须显式清掉计数，否则用例测到的是"跳过"而不是目标分支。

        基线版本必须**比清单旧**（清单默认 index 3.0.17 / auth 3.0.4），
        否则 resource_update_info 会正确地判定"已最新"而不进安装分支，
        用例就测不到目标逻辑。
        """
        data = {"resource_failures": {},
                "resource_versions": versions if versions is not None
                else {"auth": "3.0.3", "indexer": "3.0.12"}}
        state_file.write_text(json.dumps(data), encoding="utf-8")

    # K27 探测不可用 + 状态文件有基线 → 不得回滚，必须接受安装结果。
    # 这正是真机形态：装的文件通过了文件名检查，而探测在装前装后都拿不到版本。
    (res_dir / "user.sites.v3.bin").write_text("PREV-INDEX", encoding="utf-8")
    (res_dir / native_name).write_text("PREV-NATIVE", encoding="utf-8")
    _reset_failures()
    mod.fetch_text = lambda url, proxies, timeout, deadline=None, limit=0: (url, manifest)
    mod.download_file = fake_download
    mod.verify_site_resources = lambda src: None
    mod.probe_local_resource_versions = _probe_returns(None)
    rc = mod.do_resources(mod.Config(), Namespace(force=True))
    check("K27 探测不可用但装前同样不可用 → 接受安装（不再误判扩展损坏）",
          rc == mod.EXIT_RESOURCE_UPDATED, f"rc={rc}")
    check("K28 安装结果被保留（索引已换成新的）",
          (res_dir / "user.sites.v3.bin").read_text(encoding="utf-8") == "NEW-INDEX",
          (res_dir / "user.sites.v3.bin").read_text(encoding="utf-8"))
    st = json.loads(state_file.read_text(encoding="utf-8"))
    check("K28b 未验证的记账被显式标记", st.get("resource_versions_verified") is False,
          str(st.get("resource_versions_verified")))

    # K29 探测不可用 + **没有任何基线** → 必须仍然回滚。
    # 这是相反方向的危险：基线为空时旧代码静默放行，可能装进一份坏扩展，
    # 而文件名检查根本发现不了（"更新成功但起不来"的经典形态）。
    (res_dir / "user.sites.v3.bin").write_text("PREV-INDEX", encoding="utf-8")
    (res_dir / native_name).write_text("PREV-NATIVE", encoding="utf-8")
    _reset_failures(versions={})             # 状态文件存在但没有 resource_versions
    rc = mod.do_resources(mod.Config(), Namespace(force=True))
    check("K29 无基线且探测不可用 → 回滚（不放过未验证的安装）",
          rc == mod.EXIT_FAILED, f"rc={rc}")
    check("K30 回滚后索引恢复原样",
          (res_dir / "user.sites.v3.bin").read_text(encoding="utf-8") == "PREV-INDEX",
          (res_dir / "user.sites.v3.bin").read_text(encoding="utf-8"))

    # K31 失败后的冷却必须与网络失败分支同口径（只等 RETRY_AFTER_FAILURE），
    # 否则一次可恢复的失败会让资源检查停摆一整个 interval。
    st = json.loads(state_file.read_text(encoding="utf-8"))
    interval = max(mod.Config().get_int("MP_UPDATE_INTERVAL", 21600), 0)
    expected = st.get("resource_checked_at", 0)
    check("K31 失败后按 RETRY_AFTER_FAILURE 冷却而非整个周期",
          interval > 0 and expected > 0 and
          (time.time() - expected) >= max(interval - mod.RETRY_AFTER_FAILURE, 0) - 5,
          f"interval={interval} checked_at_delta={time.time() - expected:.0f}")

    # K32/K33 要跑**真实**的探针函数，所以必须先把前面所有打桩还原掉。
    # 这里踩过一次：还原语句原本在外层末尾（本块之后），于是本块调用到的仍是
    # K29 留下的 `_probe_returns(None)` 桩，日志自然为空、断言无意义。
    mod.fetch_text, mod.download_file = real_fetch, real_download
    mod.probe_local_resource_versions = real_probe
    mod.verify_site_resources = real_verify

    # K32 真机故障的可诊断性：探测失败时**必须**把子进程输出（含 traceback）
    # 打出来。真机日志里只有 "rc=1"，六种失败形态完全同形，只能靠猜 —— 那正是
    # 这次排查绕远路的根因。这里跑**真实**的 probe 函数（不打桩），让子进程真的
    # 失败一次，断言 traceback 与失败步骤都出现在日志里。
    import tempfile as _tf
    probe_root = Path(_tf.mkdtemp(prefix="mp-probe-smoke-"))
    try:
        _mp = probe_root / "mp"
        _site = _mp / "app" / "application" / "site"
        _site.mkdir(parents=True)
        (_mp / "app" / "__init__.py").write_text("", encoding="utf-8")
        (_mp / "app" / "application" / "__init__.py").write_text("", encoding="utf-8")
        (_site / "__init__.py").write_text("", encoding="utf-8")
        # 导入能过，构造抛错 —— 正是真机形态（mp_resources 的纯 import 成功，
        # 探针的 SitesHelper() 失败）。
        (_site / "sites.py").write_text(
            "class SitesHelper:\n"
            "    def __init__(self):\n"
            "        raise RuntimeError('simulated ctor failure')\n", encoding="utf-8")
        _saved = {k: os.environ.get(k) for k in ("MP_SRC", "CONFIG_DIR", "APP_PYTHON")}
        os.environ.update({"MP_SRC": str(_mp), "CONFIG_DIR": str(probe_root / "cfg"),
                           "APP_PYTHON": sys.executable})
        (probe_root / "cfg").mkdir(parents=True, exist_ok=True)
        # 必须**自己构造** Config 并指向本用例的树：scenario K 早先已把 MP_SRC
        # 设成它自己的沙箱，而那个沙箱里的 sites 只有 10 字节的假 .pyd。若依赖
        # 环境变量，探针就会跑在错误的树上，测到的是另一个错误（这里踩过一次）。
        _pcfg = mod.Config()
        _pcfg.mp_src = _mp
        _pcfg.config_dir = probe_root / "cfg"
        _pcfg.python = sys.executable
        _logs = []
        _real_log_fn = mod.log
        # 用模块级变量而不是闭包捕获日志：probe 内部通过模块全局 `log` 输出，
        # 而 scenario K 前面若干步会把 mod.log 换成各种包装，闭包容易捕到空列表。
        # 这里直接把 mod.log 换成"既写真实日志、又追加到 _logs"的实现。
        def _capture(m, _sink=_logs, _real=_real_log_fn):
            _sink.append(str(m))
            return _real(m)
        mod.log = _capture
        try:
            _res = mod.probe_local_resource_versions(_pcfg)
        finally:
            mod.log = _real_log_fn
        _text = "\n".join(_logs)
        # 诊断信息写进断言 detail，避免"失败但看不出为什么"。
        _diag = (f"logs={len(_logs)} outlen={len(_res.output or '')} "
                 f"reason={_res.reason!r}")
        check("K32 探测失败时返回 None 且给出原因", _res.versions is None and bool(_res.reason),
              f"versions={_res.versions} reason={_res.reason!r}")
        check("K33 子进程 traceback 被记入日志（真机缺的就是这段）",
              "Traceback" in _text and "simulated ctor failure" in _text,
              _diag + " || " + _text[-200:])

        # K34 探针必须**只构造一次** SitesHelper。
        # sites 扩展里有 SiteSingleton（进程级单例），而旧探针写成
        #     [str(SitesHelper().auth_version), str(SitesHelper().indexer_version)]
        # 在一个表达式里构造两次 —— 第二次可能因单例/非幂等初始化抛错，于是
        # 表现为"纯 import 成功、探针 rc=1"，与真机症状完全同形。
        # 这里把 tests 目录换成"第二次构造必抛错"的单例式实现来固定这个契约。
        (_site / "sites.py").write_text(
            "_made = []\n"
            "class SitesHelper:\n"
            "    def __init__(self):\n"
            "        if _made:\n"
            "            raise RuntimeError('singleton already constructed')\n"
            "        _made.append(1)\n"
            "        self.auth_version = '3.0.4'\n"
            "        self.indexer_version = '3.0.18'\n", encoding="utf-8")
        _logs2 = []
        _real_log_fn2 = mod.log
        mod.log = lambda m, _s=_logs2, _r=_real_log_fn2: (_s.append(str(m)), _r(m))
        try:
            _res2 = mod.probe_local_resource_versions(_pcfg)
        finally:
            mod.log = _real_log_fn2
        check("K34 单例式扩展下探针只构造一次并成功（旧写法在此 rc=1）",
              _res2.versions == ("3.0.4", "3.0.18"),
              f"versions={_res2.versions} reason={_res2.reason!r}")

        for k, v in _saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    finally:
        shutil.rmtree(probe_root, ignore_errors=True)


if __name__ == "__main__":
    mod._real_smoke_test = mod.smoke_test
    scenario_a()
    scenario_a2()
    scenario_b()
    scenario_c()
    scenario_d()
    scenario_e()
    scenario_f()
    scenario_g()
    scenario_h()
    scenario_i()
    scenario_j()
    scenario_k()
    print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项: {FAILS}"))
    sys.exit(1 if FAILS else 0)
