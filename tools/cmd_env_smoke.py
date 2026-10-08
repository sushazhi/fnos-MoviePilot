#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cmd 生命周期脚本的 app.env 引导冒烟测试（Windows 也能跑）。

背景（本测试要钉住的线上事故）：
    install_callback 早期无条件 `cat > app.env` 重建配置，而 fnOS 的"重新安装"同样
    会走 install_callback。app.env 不只是部署参数 —— 它同时是 MoviePilot 自己的
    配置落点（后端用 dotenv 的 set_key 把用户设置写回这里），并承载两个密钥。
    一次覆盖安装就把密钥和用户配置整份清空，实际表现是：
      * RESOURCE_SECRET_KEY 变化 -> 站点索引解不开、站点页无数据，
        且只认资源 Cookie 的 system/message、system/logging 持续 401；
      * SECRET_KEY 变化 -> 登录令牌作废，前端 401 后直接登出。

测试分两层：
  A-H 行为：用真实 bash 跑 cmd/lib.sh 的四个函数（新装/覆盖安装/向导显式值/幂等）
  I-K 契约：静态断言"覆盖安装路径不得重建 app.env、不得用 sed 写 app.env、
        启动路径必须兜底补密钥"，防止以后被改回去

依赖：需要 `bash`。没有 bash 时只做静态检查并明确 SKIP 行为层，不误判失败。
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CMD = ROOT / "cmd"
SANDBOX = ROOT / ".local-build" / "_smoke" / "cmdenv"

FAILS = []


def check(label, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + label
          + (f"  <- {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(label)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 行为层：真实 bash 调用
# ---------------------------------------------------------------------------
_BEHAVIOR_SH = r"""#!/usr/bin/env bash
# 由 tools/cmd_env_smoke.py 生成，直接 source 真实 lib.sh 后调用被测函数。
set -u
REPO="$1"
WORK="$2"
. "${REPO}/cmd/lib.sh"

fail=0
ok()  { echo "  ok   $1"; }
bad() { echo "  BAD  $1"; fail=1; }
expect_eq() {  # $1 名称  $2 期望  $3 实际
    if [ "$2" = "$3" ]; then ok "$1"; else bad "$1（期望 [$2] 实际 [$3]）"; fi
}
key_value() {  # $1 文件  $2 键  -> 取第一个匹配行的值（保留 = 之后的全部字符）
    grep -m1 "^$2=" "$1" | cut -d= -f2-
}
key_count() {  # $1 文件  $2 键
    grep -c "^$2=" "$1"
}

echo "--- A 全新安装 ---"
ENV_A="${WORK}/a.env"
rm -f "${ENV_A}"
export wizard_superuser_password='Adm1n@2026'
out="$(mp_env_bootstrap "${ENV_A}" "/vol1/@appdata/moviepilot/config")"
expect_eq "A1 全新安装返回 fresh" "fresh" "${out}"
expect_eq "A2 SECRET_KEY 唯一" "1" "$(key_count "${ENV_A}" SECRET_KEY)"
expect_eq "A3 RESOURCE_SECRET_KEY 唯一" "1" "$(key_count "${ENV_A}" RESOURCE_SECRET_KEY)"
expect_eq "A4 SECRET_KEY 为 44 字符（32 字节 base64）" "44" \
    "$(printf '%s' "$(key_value "${ENV_A}" SECRET_KEY)" | wc -c | tr -d ' ')"
expect_eq "A5 RESOURCE_SECRET_KEY 为 44 字符" "44" \
    "$(printf '%s' "$(key_value "${ENV_A}" RESOURCE_SECRET_KEY)" | wc -c | tr -d ' ')"
expect_eq "A6 CONFIG_DIR 写入模板值" "/vol1/@appdata/moviepilot/config" \
    "$(key_value "${ENV_A}" CONFIG_DIR)"
expect_eq "A7 向导密码写入" "Adm1n@2026" "$(key_value "${ENV_A}" SUPERUSER_PASSWORD)"
# A8：全新安装的模板里必须带上站点资源同步开关（默认开启）
expect_eq "A8 模板含站点资源同步开关" "1" \
    "$(key_value "${ENV_A}" MP_AUTO_UPDATE_RESOURCE)"

echo "--- B 覆盖安装：保留密钥与用户配置 ---"
ENV_B="${WORK}/b.env"
cat > "${ENV_B}" <<'ENVEOF'
CONFIG_DIR=/vol1/@appdata/moviepilot/config
PORT=3011
SECRET_KEY=OLDsecret==
RESOURCE_SECRET_KEY=OLDresource==
SUPERUSER=admin
SUPERUSER_PASSWORD=OldPass@1
TMDB_API_KEY=user-configured-key
AUTH_SITE=nexusphp
ENVEOF
unset wizard_port wizard_nginx_port wizard_superuser wizard_superuser_password wizard_api_token
out="$(mp_env_bootstrap "${ENV_B}" "/vol1/@appdata/moviepilot/config")"
expect_eq "B1 覆盖安装返回 kept" "kept" "${out}"
expect_eq "B2 SECRET_KEY 未被重写" "OLDsecret==" "$(key_value "${ENV_B}" SECRET_KEY)"
expect_eq "B3 RESOURCE_SECRET_KEY 未被重写" "OLDresource==" \
    "$(key_value "${ENV_B}" RESOURCE_SECRET_KEY)"
expect_eq "B4 后端写回的用户配置保留" "user-configured-key" \
    "$(key_value "${ENV_B}" TMDB_API_KEY)"
expect_eq "B5 用户配置键 AUTH_SITE 保留" "nexusphp" "$(key_value "${ENV_B}" AUTH_SITE)"
expect_eq "B6 向导未填时密码不被清空" "OldPass@1" \
    "$(key_value "${ENV_B}" SUPERUSER_PASSWORD)"
expect_eq "B7 已有端口保留" "3011" "$(key_value "${ENV_B}" PORT)"
expect_eq "B8 缺失键补默认值" "1" "$(key_value "${ENV_B}" MP_AUTO_UPDATE)"
# B9：站点资源同步开关也是"覆盖安装必须补齐"的新键（老 app.env 没有它）。
# 默认 1 —— 关掉它站点资源就永远不更新，只能是用户显式选择的结果。
expect_eq "B9 站点资源同步开关补默认值" "1" \
    "$(key_value "${ENV_B}" MP_AUTO_UPDATE_RESOURCE)"

echo "--- C 覆盖安装 + 向导显式值 ---"
export wizard_port=3999
export wizard_superuser_password='A&b|c/d'
out="$(mp_env_bootstrap "${ENV_B}" "/vol1/@appdata/moviepilot/config")"
expect_eq "C1 向导端口覆盖生效" "3999" "$(key_value "${ENV_B}" PORT)"
expect_eq "C2 含 & | / 的值按原样写入（不走 sed 拼接）" 'A&b|c/d' \
    "$(key_value "${ENV_B}" SUPERUSER_PASSWORD)"
expect_eq "C3 多次引导密钥仍不变" "OLDsecret==" "$(key_value "${ENV_B}" SECRET_KEY)"
expect_eq "C4 键不重复写入" "1" "$(key_count "${ENV_B}" SUPERUSER_PASSWORD)"
unset wizard_port wizard_superuser_password

echo "--- D mp_env_ensure / mp_env_ensure_secret ---"
ENV_D="${WORK}/d.env"
printf 'PORT=3002\n' > "${ENV_D}"
mp_env_ensure "${ENV_D}" "PORT" "9999"
mp_env_ensure "${ENV_D}" "MP_UPDATE_DEPS" "1"
expect_eq "D1 ensure 不覆盖已有值" "3002" "$(key_value "${ENV_D}" PORT)"
expect_eq "D2 ensure 补齐缺失键" "1" "$(key_value "${ENV_D}" MP_UPDATE_DEPS)"
mp_env_ensure_secret "${ENV_D}" "SECRET_KEY"
v1="$(key_value "${ENV_D}" SECRET_KEY)"
mp_env_ensure_secret "${ENV_D}" "SECRET_KEY"
expect_eq "D3 密钥只生成一次" "${v1}" "$(key_value "${ENV_D}" SECRET_KEY)"
expect_eq "D4 密钥非空" "yes" "$([ -n "${v1}" ] && echo yes || echo no)"
expect_eq "D5 密钥可被 Fernet 接受（44 字符 base64）" "44" \
    "$(printf '%s' "${v1}" | wc -c | tr -d ' ')"
mp_env_ensure_secret "${WORK}/nope.env" "SECRET_KEY"
expect_eq "D6 文件不存在时不创建" "no" \
    "$([ -f "${WORK}/nope.env" ] && echo yes || echo no)"

echo "--- E mp_env_upsert ---"
ENV_E="${WORK}/e.env"
printf 'A=1\nB=2\n' > "${ENV_E}"
mp_env_upsert "${ENV_E}" "A" "x/y&z|w"
mp_env_upsert "${ENV_E}" "C" "3"
expect_eq "E1 upsert 覆盖已有键" "x/y&z|w" "$(key_value "${ENV_E}" A)"
expect_eq "E2 upsert 追加缺失键" "3" "$(key_value "${ENV_E}" C)"
expect_eq "E3 upsert 不影响其它键" "2" "$(key_value "${ENV_E}" B)"
mp_env_upsert "${ENV_E}" "A" "again"
expect_eq "E4 重复 upsert 不新增行" "1" "$(key_count "${ENV_E}" A)"
expect_eq "E5 重复 upsert 生效" "again" "$(key_value "${ENV_E}" A)"

echo "--- F 权限位不被 mv 破坏 ---"
# 断言语义：upsert 前后权限"不变"。若实现退回 mv（临时文件由重定向创建，权限来自
# umask），600 的 app.env 会变成 644/666 —— Windows/Git Bash 上模拟权限可能都是
# 777，此时该断言自然通过，不会误报。
ENV_F="${WORK}/f.env"
printf 'PORT=3002\n' > "${ENV_F}"
chmod 600 "${ENV_F}"
mode_before="$(stat -c '%a' "${ENV_F}" 2>/dev/null || stat -f '%Lp' "${ENV_F}" 2>/dev/null || echo '?')"
mp_env_upsert "${ENV_F}" "PORT" "3003"
mode_after="$(stat -c '%a' "${ENV_F}" 2>/dev/null || stat -f '%Lp' "${ENV_F}" 2>/dev/null || echo '?')"
expect_eq "F1 upsert 不改变文件权限（用 cat 覆盖而非 mv）" "${mode_before}" "${mode_after}"
expect_eq "F2 upsert 结果正确" "3003" "$(key_value "${ENV_F}" PORT)"

# --- G 配置备份快速路径（upgrade_init 的 backup_config） ---
# upgrade_init 是生命周期脚本（不是可 source 的库），这里只抽出 backup_config 函数体
# 单独求值 —— 目的是验证"降级链真的能产出可用备份"，而不是把整个升级流程跑一遍。
echo "--- G 配置备份降级链 ---"
BACKUP_FN="$(sed -n '/^backup_config() {/,/^}/p' "${REPO}/cmd/upgrade_init")"
if [ -z "${BACKUP_FN}" ]; then
    bad "G0 未能从 upgrade_init 抽出 backup_config"
else
    ok "G0 backup_config 可抽取"
    eval "${BACKUP_FN}"

    # 造一个有内容的 config 目录（含子目录与二进制样文件）
    SRC="${WORK}/cfg_src"
    rm -rf "${SRC}"; mkdir -p "${SRC}/sub"
    printf 'SECRET_KEY=abc\n' > "${SRC}/app.env"
    printf 'userdb' > "${SRC}/user.db"
    printf 'nested' > "${SRC}/sub/deep.bin"

    DST="${WORK}/cfg_dst"
    MODE="$(backup_config "${SRC}" "${DST}")"
    case "${MODE}" in 1|2|3) ok "G1 备份返回有效模式（${MODE}）" ;;
                  *) bad "G1 备份模式异常（[${MODE}]）" ;; esac

    # 关键语义：备份必须**内容完整**，无论走的是硬链接还是拷贝。
    # 硬链接档最容易出的错是只链了顶层、子目录没进去。
    expect_eq "G2 顶层文件已备份" "SECRET_KEY=abc" "$(cat "${DST}/app.env" 2>/dev/null)"
    expect_eq "G3 子目录文件已备份" "nested" "$(cat "${DST}/sub/deep.bin" 2>/dev/null)"
    expect_eq "G4 数据库已备份" "userdb" "$(cat "${DST}/user.db" 2>/dev/null)"

    # 备份是"快照"语义：源被改后备份不应跟着变（硬链接会跟着变是已知取舍，
    # 但升级流程在备份后不再写 config/，所以这里只钉住"备份自成一份可用副本"）。
    rm -rf "${SRC}"
    expect_eq "G5 源目录删除后备份仍可读" "SECRET_KEY=abc" "$(cat "${DST}/app.env" 2>/dev/null)"

    # 目标已存在时必须先清空：cp -a 遇已存在目录会嵌套成 dst/config/config，
    # 升级回调按 backup/config/ 取 app.env 就会找不到（历史事故）。
    mkdir -p "${DST}/config"
    printf 'stale' > "${DST}/config/old.env"
    backup_config "${SRC}" "${DST}" >/dev/null 2>&1 || true
    expect_eq "G6 不产生嵌套的 dst/config/config" "no" \
        "$([ -d "${DST}/config/config" ] && echo yes || echo no)"

    # 源目录不存在时应安静返回，不应造出空备份目录来骗过恢复判断
    rm -rf "${WORK}/absent" "${WORK}/absent_dst"
    backup_config "${WORK}/absent" "${WORK}/absent_dst" >/dev/null 2>&1
    expect_eq "G7 源不存在时不报错且不建目标" "no" \
        "$([ -d "${WORK}/absent_dst" ] && echo yes || echo no)"
fi

exit ${fail}
"""


def behavior_checks() -> None:
    if shutil.which("bash") is None:
        print("SKIP: 未找到 bash，跳过行为层检查（不计为失败）")
        return
    SANDBOX.mkdir(parents=True, exist_ok=True)
    script = SANDBOX / "_cmd_env_behavior.sh"
    # newline="\n"：Windows 上默认会把 \n 翻成 \r\n，bash 会因 CR 报 "command not found"
    script.write_text(_BEHAVIOR_SH, encoding="utf-8", newline="\n")
    r = subprocess.run(
        ["bash", script.relative_to(ROOT).as_posix(), ".",
         SANDBOX.relative_to(ROOT).as_posix()],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(ROOT),
    )
    out = (r.stdout or "") + (r.stderr or "")
    for line in out.strip().splitlines():
        print("      " + line)
    check("A-H 行为层全部通过", r.returncode == 0 and "BAD" not in out,
          f"rc={r.returncode}")


# ---------------------------------------------------------------------------
# 契约层：静态断言
# ---------------------------------------------------------------------------
def contract_checks() -> None:
    install_cb = read(CMD / "install_callback")
    lib = read(CMD / "lib.sh")
    main = read(CMD / "main")
    config_cb = read(CMD / "config_callback")

    check("I. install_callback 走 mp_env_bootstrap（不再自己重建 app.env）",
          "mp_env_bootstrap" in install_cb and 'cat > "${ENV_FILE}"' not in install_cb)
    check("I. install_callback 不再自行生成密钥",
          'SECRET_KEY="$(head -c' not in install_cb)
    check("I. 整份模板只存在于 lib.sh 的 fresh 分支",
          'cat > "${env_file}"' in lib)
    sed_cmds = [ln.strip() for ln in config_cb.splitlines()
                if ln.lstrip().startswith("sed ")]
    check("J. config_callback 不再用 sed 写 app.env（含 & 的值会写坏）",
          not sed_cmds, sed_cmds)
    check("J. config_callback 通过 mp_env_upsert 写值",
          config_cb.count("mp_env_upsert") >= 3)
    check("K. cmd/main 启动前兜底补密钥",
          "start)" in main and "mp_env_ensure_secret" in main)
    # L. 站点资源同步开关：默认值必须由 lib.sh（安装）与 config_callback（改配置）
    #    两处都补齐，否则老包升级上来的 app.env 永远没有这个键，更新器只能吃默认值，
    #    用户在面板上关不掉也开不了。
    check("L. lib.sh 全新安装模板含 MP_AUTO_UPDATE_RESOURCE",
          "MP_AUTO_UPDATE_RESOURCE=1" in lib)
    check("L. lib.sh 覆盖安装补齐 MP_AUTO_UPDATE_RESOURCE",
          'mp_env_ensure "${env_file}" "MP_AUTO_UPDATE_RESOURCE" "1"' in lib)
    check("L. config_callback 补齐 MP_AUTO_UPDATE_RESOURCE",
          'mp_env_ensure "${ENV_FILE}" "MP_AUTO_UPDATE_RESOURCE" "1"' in config_cb)
    # 非 0/1 一律按开启处理：与 MP_AUTO_UPDATE 同口径，避免把能自愈的资源同步误关。
    check("L. config_callback 对非预期开关值按开启兜底",
          '*)   MP_AUTO_UPDATE_RESOURCE_VALUE="1" ;;' in config_cb)
    # M. 重启路径必须真的调用资源同步，否则"重启即同步"只是文档里的一句话。
    check("M. cmd/main 启动路径调用 --resources 同步",
          "--resources" in main and "run_updater --resources --auto" in main)
    # M. 资源同步失败不得阻塞启动：调用点必须 `|| true`（与主程序更新一致）。
    check("M. cmd/main 资源同步失败不阻塞启动",
          "run_updater --resources --auto || true" in main)
    # M. run_updater 必须把 rc=11 当成功（不补 stdio、不打印"未完成"）。
    check("M. run_updater 把 11 视为成功退出码",
          '[ "$rc" != "11" ]' in main and '11)  log "==> 已更新站点资源' in main)

    # N. 装包升级路径必须跳过启动前的联网更新检查。
    #    upgrade_callback 末尾会调 cmd/main restart，而 cmd/main 的 start 分支会跑
    #    两遍 run_updater（主程序 + 站点资源）。装包这一刻程序目录刚被整包替换，
    #    version.py 就是包里那份，联网查询注定返回"已是最新"；而该检查是四级降级
    #    + 120s 预算，在 api.github.com 直连不通的受限网络里要逐级走完才失败，
    #    一次升级白烧一两分钟。丢了 MP_SKIP_UPDATE=1 就会把这个耗时加回来。
    upgrade_cb = read(CMD / "upgrade_callback")
    check("N. upgrade_callback 重启时带 MP_SKIP_UPDATE=1",
          "MP_SKIP_UPDATE=1" in upgrade_cb)
    check("N. MP_SKIP_UPDATE=1 直接作用于 cmd/main 调用",
          'MP_SKIP_UPDATE=1 "${TRIM_APPDEST}/cmd/main" restart' in upgrade_cb)
    # N. 该开关必须真的被 cmd/main 认（否则只是个无意义的变量）。
    check("N. cmd/main 尊重 MP_SKIP_UPDATE（跳过两段联网检查）",
          main.count('[ "${MP_SKIP_UPDATE:-}" != "1" ]') >= 2)
    # N. 跳过联网检查时 repair_resources 仍必须执行：纯本地资源对位不能一起被跳过，
    #    否则"更新后 sites 目录错位"这类历史故障会失去启动前的自愈机会。
    check("N. 跳过更新不影响 repair_resources 执行",
          "repair_resources" in main)

    # O. 升级备份必须走"硬链接 → reflink → 全量拷贝"的降级链，且校验结果非空。
    #    旧的 `cp -a` 无条件全量拷贝 config/（含 SQLite 库、插件、缓存、图片），
    #    是应用中心装包升级里实打实的 IO 时间。降级链每一档都要判空，否则
    #    --reflink=auto 在不支持的文件系统上会静默留下半份备份。
    upgrade_init = read(CMD / "upgrade_init")
    check("O. upgrade_init 抽出 backup_config 函数",
          "backup_config()" in upgrade_init)
    check("O. 第一档为硬链接 cp -al", 'cp -al "${src}/."' in upgrade_init)
    check("O. 第二档为 reflink 克隆", "--reflink=auto" in upgrade_init)
    check("O. 末档为全量拷贝兜底",
          'cp -a "${src}/." "${dst}/" 2>/dev/null || cp -r "${src}/." "${dst}/"' in upgrade_init)
    # 每一档都必须判非空，这是"备份可用"的底线。
    check("O. 每档都校验备份非空",
          upgrade_init.count('[ -n "$(ls -A "${dst}" 2>/dev/null)" ]') >= 2)
    # 备份目标路径必须是 BACKUP_DIR/config（升级回调按 backup/config/ 恢复 app.env）。
    check("O. 备份目标仍为 BACKUP_DIR/config",
          '"${BACKUP_DIR}/config"' in upgrade_init)
    # 备份失败不得中断升级（它在 fnOS 原地升级里只是保险），但必须留明确日志。
    check("O. 备份失败不中断升级且记日志",
          "警告: 配置备份失败" in upgrade_init)


def main() -> int:
    print("== cmd app.env 引导冒烟测试 ==\n")
    for name in ("lib.sh", "install_callback", "config_callback", "main",
                 "upgrade_init", "upgrade_callback"):
        check(f"cmd/{name} 存在", (CMD / name).is_file())

    print()
    behavior_checks()

    print()
    contract_checks()

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
