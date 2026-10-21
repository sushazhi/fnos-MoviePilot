# MoviePilot 飞牛 fnOS 原生应用

将 [MoviePilot](https://github.com/jxxghp/MoviePilot)（NAS 媒体库自动化管理工具）封装为 **fnOS 原生应用**，非 Docker 版。

## 特性

- **原生运行**：不依赖 Docker，直接在 fnOS 上以 Python + Node.js 运行
- **统一网关接入**：通过 fnOS 统一网关 + 反向代理嵌入桌面 iframe，自动校验 NAS 登录态
- **自带 Python 运行时**：随包分发 CPython 3.14 与全部依赖，**安装时零联网、零编译**
- **SQLite 数据库**：默认 SQLite，无需额外安装 PostgreSQL
- **重启即升级**：应用每次启动前自动检查上游 Release 并就地更新，无需重新构建 fpk
- **生命周期管理**：安装向导、启停控制、升级数据备份/恢复、卸载数据保留/删除

## 环境要求

| 项目 | 要求 |
|------|------|
| 系统 | 飞牛 fnOS |
| 架构 | `amd64` 或 `arm64`（分别构建，不可混用） |
| 运行时 | Node.js `nodejs_v24`（应用中心自动依赖安装）；Python 已自带 |
| 构建 | 打 `--with-runtime` 包需 **Linux/macOS** 构建机，且架构与目标 NAS 一致 |

## 安装

在 fnOS 应用中心「手动安装」上传 `.fpk`，或：

```bash
appcenter-cli install-fpk moviepilot-<version>-amd64.fpk
```

安装向导会收集**超级管理员用户名 / 密码**、**后端端口**（默认 3002）、**前端端口**（默认 3005）。安装时自动初始化 SQLite 数据库并创建管理员。

> 请使用 CI 产物或 Linux/macOS 上带 `--with-runtime` 构建的包。未自带运行时的包安装时会尝试用 fnOS `python312` 建 venv 装依赖，但上游要求 `>= 3.14`，这条路径实际不可用，仅作本地开发降级。

## 使用

安装后在 fnOS 桌面点击 MoviePilot 图标即可打开（嵌入 iframe），首个登录使用向导设置的管理员账号。

| 用途 | 位置 |
|------|------|
| 配置 / 数据库 | `TRIM_PKGVAR/config`（持久） |
| 下载目录 | `TRIM_DATA_SHARE_PATHS` 共享目录 |
| 媒体库目录 | 在应用设置中授权 |
| 更新日志 | `TRIM_PKGVAR/update.log` |
| 更新状态 | `TRIM_PKGVAR/config/mp_update.json` |

端口与超级管理员密码可在应用设置中修改。

## 架构

```
浏览器 (fnOS 桌面 iframe)  ──/app/moviepilot/...──▶  fnOS 统一网关（校验登录态）
                                                         │ moviepilot.sock
                                                         ▼
                              gateway-proxy.py（剥离前缀 + JS polyfill + WS 隧道）
                                                         │
                              Node 前端 frontend-server.js :3005（静态资源 + /api 代理）
                                                         │
                              Python 后端 app/main.py :3002
```

`gateway-proxy.py` 会把 `Authorization` 改名为 `X-MP-Auth` 再发出（fnOS 网关会把该头当成自己的令牌校验，不通过就返回 `200 text/plain "invalid token"`，请求到不了应用），代理转发时再还原。

## 上游自动更新（重启即升级）

上游发新版本时**不需要重新构建 fpk**：应用每次启动（含重启）前检查上游 Release，有新版就下载并就地替换后端源码与前端 dist，然后用新代码启动。

- 主程序与**站点资源**（站点认证扩展 + 站点索引）是**两条独立发布通道**，各自检查、各自开关与冷却
- 更新流程：查版本 → 下载后端 zip（校验结构与版本）→ 依赖预检 → 备份替换（含资源包回填）→ 前端 dist 替换 → 自检（失败**整树回滚**）→ 写状态文件（保留最近 2 代备份）
- 若 MoviePilot 界面已下载过更新包（`config/temp/movietpilot-update/`），更新器直接复用，不重复下载
- 版本发现四级降级：`api.github.com` 直连 → 网页 `releases/latest` → `releases.atom` → 分支 `version.py`
- 上游对同一版本重新打包（如 `v3.0.10-1`）按**正式版**处理，且 `v3.0.10-1 > v3.0.10`

### 开关（`app.env`，应用设置里可切换）

| 变量 | 默认 | 说明 |
|------|------|------|
| `MP_AUTO_UPDATE` | `1` | 主程序自更新总开关 |
| `MP_UPDATE_CHANNEL` | `release` | `release` 仅正式版 / `prerelease` 含测试版 / `off` 关闭 |
| `MP_UPDATE_INTERVAL` | `21600` | 检查间隔（秒）；站点资源共用此间隔，各记各的时间戳 |
| `MP_UPDATE_DEPS` | `1` | 是否同步 Python 依赖；关闭且新版有新增依赖时会**拒绝更新** |
| `MP_AUTO_UPDATE_RESOURCE` | `1` | 是否同步站点资源 |
| `GITHUB_PROXY` | `https://gh.dpik.top/` | 加速前缀，只作用于 `github.com` 系 URL；`api.github.com` 一律直连 |
| `GITHUB_PROXY_MIRRORS` | `https://v4.gh-proxy.org/` | 额外加速前缀，逗号分隔，如 `https://a/,https://b/` |

> 内置加速前缀顺序：`gh.dpik.top` → `v4.gh-proxy.org`。`v4.gh-proxy.org` 必须带 `v4.` 前缀。
> 已移除：`gh-proxy.com`（速度≈0）、`ghfast.top`（对本仓库全量 403）。
> `gh.dpik.top` 是个人运营的第三方服务，介意供应链风险可换成自建或可信镜像。

### 手动操作与回退

```bash
cd /var/apps/moviepilot/target
./cmd/main update          # 立即检查并更新主程序（忽略冷却）
./cmd/main update-check    # 只看有没有新版本，不动任何文件
./cmd/main resources       # 立即同步站点资源（认证扩展 + 站点索引）
./cmd/main rollback        # 回滚到更新前的版本（备份在 <应用目录>/.mp-backup/）
./cmd/main repair          # 修复站点资源（模块错位/缺失导致起不来时，纯本地、无网络）
```

> `repair` 是离线自愈（按当前源码结构重新对位资源文件）；`resources` 是在线升级（比对版本并下载）。启动路径两个都跑：先 `repair` 再 `resources`。
> 通过 fpk 重新安装/升级应用时，应用目录被整包替换，自更新内容随之回到安装包版本 —— 这是预期行为，也让「装包升级」始终是可靠的回退路径。

## 从源码构建

```bash
python build.py                       # 下载源码 + 前端并打包
python build.py --with-runtime        # 自带 CPython 3.14 + 全部依赖（仅 Linux/macOS，推荐）
python build.py --with-runtime --no-build   # 严格模式：禁止源码构建，只用预编译 wheel
python build.py --force               # 强制重新下载外部资源
python build.py --clean               # 构建前清理 .local-build
python build.py --arch arm64          # 显式声明目标架构
python build.py --skip-mp / --skip-fe # 跳过下载后端 / 前端
```

构建脚本会：下载 `UPSTREAM_TAG` 钉住的上游源码 → 下载前端 `dist.zip` → 同步站点资源包 →（可选）装配 CPython 运行时与依赖 → 组装目录树并用 fnpack 打 `.fpk`。

所有构建产物收敛在 `.local-build/`（已 gitignore），项目根不残留。

打包口径：

- **只构建当前上游版本**：`build.py` 的 `UPSTREAM_TAG` 钉死上游 tag，同一次构建结果可复现；升级上游就是改这一个常量（`tools/bump_upstream.py` 自动改并同步 `manifest`）
- **版本号 = 上游 tag**（去前导 `v`）：上游 `v3.0.10-1` → 应用版本 `3.0.10-1`，git tag 同名
- **manifest 的 `changelog` 只保留当前版本一条**，每次跟进整条替换；Release 正文只取「版本号 + 第 1 条」
- 打 tag 前须先同步 `manifest` 的 `version`，CI 会校验二者一致，不一致直接失败

## CI

| workflow | 作用 | 触发 |
|----------|------|------|
| `.github/workflows/check-upstream.yml` | 检查上游新版本 → 改 pin/manifest → 提交 → 打 tag → 派发构建 | 每周二 11:23（北京时间）或手动 |
| `.github/workflows/build-and-release.yml` | 分架构（`amd64` / `arm64`）打包并创建 Release | 上面派发、push `v*` tag、手动 |

产物命名 `moviepilot-<version>-<arch>.fpk`。构建后校验包内 `app/python/bin/python3` 存在，并抽查 `fastapi/uvicorn/sqlalchemy/pydantic_core/orjson`。

> 本仓库约定：用内置 `GITHUB_TOKEN` 推的 tag **不会触发其它 workflow**，所以「改完 pin 打 tag」后由 `check-upstream.yml` 显式 `workflow_dispatch` 派发构建，而不是等 tag 触发。

## 测试

```bash
python tools/updater_smoke.py     # 自更新器（主程序 + 站点资源 + 下载提速），全离线
python tools/bump_upstream_smoke.py
python tools/resources_smoke.py
python tools/supervisor_smoke.py
python tools/pkgsize_smoke.py
python tools/cmd_env_smoke.py     # 需 bash
python tools/polyfill_smoke.py    # 需 node，缺失则 SKIP
```

## 已知限制

- 产物体积数百 MB（自带解释器 + 全部依赖），这是「安装时零联网」的代价
- 依赖优先用预编译 wheel（`anitopy`、`pinyin2hanzi` 只有 sdist，不能一刀切 `--no-build`）；wheel 的 manylinux 基线不得高于 glibc 2.36（fnOS / Debian 12）
- MoviePilot 插件依赖由后端运行时 pip 装进 `app/python/site-packages`，**整包升级后需重新安装插件依赖**
- 自更新要求应用目录（`TRIM_APPDEST`）可写；只读时跳过更新并记日志，只能靠重新装包升级
- 自更新不替换 `app/python/` 运行时；新版引入新依赖时由更新器 pip 增量补装（需联网，走国内镜像）
- 站点资源同步只替换认证扩展与站点索引两个文件，不动 `site-packages`
- 默认 SQLite；需 PostgreSQL 时在 `app.env` 设 `DB_TYPE=postgresql` 并自行安装 PostgreSQL
- 应用中心装包升级的耗时主要来自 fnOS 解压 fpk 本身，瓶颈是**文件数**而非体积，只能在构建期优化

## 致谢与许可

基于 [MoviePilot](https://github.com/jxxghp/MoviePilot)、[MoviePilot-Frontend](https://github.com/jxxghp/MoviePilot-Frontend)、[MoviePilot-Resources](https://github.com/jxxghp/MoviePilot-Resources)（均 GPL-3.0，版权归 jxxghp），运行时来自 [python-build-standalone](https://github.com/astral-sh/python-build-standalone)（PSF-2.0）、依赖管理用 [uv](https://github.com/astral-sh/uv)（MIT / Apache-2.0），运行环境由 [fnOS](https://www.fnnas.com/) 提供。下载加速由 [gh.dpik.top](https://gh.dpik.top/)、[v4.gh-proxy.org](https://v4.gh-proxy.org/) 等镜像提供。

本应用包**整体基于 GPL-3.0 发布**，封装代码（`cmd/`、`app/bin/`、`wizard/`、`build.py` 等）同样以 GPL-3.0 授权。全文见 `LICENSE`，第三方许可见 `THIRD-PARTY-LICENSES`。本应用包仅为第三方封装，请自行评估使用风险。
