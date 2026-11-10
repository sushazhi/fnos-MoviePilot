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
                                                         │ X-MP-Gateway-Prefix
                              Node 前端 frontend-server.js :3005（静态资源 + /api 代理
                                                    + 网关免登录注入，两条路径共用）
                                                         │
                              Python 后端 app/main.py :3002

直连前端端口（飞牛 APP / FN Connect 映射到应用端口）──────────▲
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
| `MP_GATEWAY_AUTOLOGIN` | `0` | 网关免登录：凭飞牛登录态直接进 MoviePilot，跳过 MP 登录页（见下节，**会放大权限**） |
| `GITHUB_PROXY` | `https://gh-proxy.com/` | 加速前缀，只作用于 `github.com` 系 URL；`api.github.com` 一律直连 |
| `GITHUB_PROXY_MIRRORS` | `https://v4.gh-proxy.org/,https://ghfast.top/,https://ghproxy.imciel.com/` | 额外加速前缀，逗号分隔，如 `https://a/,https://b/` |

> 下载策略：**先直连，超时再加速**（直连只给 8 秒判死）；加速源清单与顺序参照同作者的
> fnos-qbittorrent：`gh-proxy.com` → `v4.gh-proxy.org` → `ghfast.top` → `ghproxy.imciel.com`
> （与 `build.py` 的 `PROXIES` 逐项一致，H21b 钉住）。`v4.gh-proxy.org` 必须带 `v4.` 前缀。
> 镜像均为第三方服务，介意供应链风险可换成自建或可信镜像。

### 网关免登录（`MP_GATEWAY_AUTOLOGIN`，默认关闭）

飞牛网关已经校验过 NAS 登录态，但 MoviePilot 自己的登录态存在浏览器里，所以点开应用
仍要再登一次。打开这个开关后，**前端服务代你登录一次**，点开就是仪表盘。

- **怎么做的**：`frontend-server.js`（所有 HTML 的唯一产地）用 `app.env` 的
  `SUPERUSER` / `SUPERUSER_PASSWORD` 调一次 `/api/v1/login/access-token`，把结果经
  `/__mp/autologin.json` 交给页面头部注入的小脚本，由脚本写进 `localStorage` 的
  `auth` / `user` 两个 store（pinia 持久化键）。MoviePilot 自身的鉴权、权限、资源
  Cookie 全部保持原样，**没有改一行上游代码**，也不会被应用内自更新覆盖（自更新只换
  `mp/` 与 `frontend/`）。
- **两条访问路径都覆盖**：统一网关那条路（浏览器 → fnOS 网关 → unix socket →
  `gateway-proxy` → 前端服务）和直连前端端口那条路（飞牛 APP、FN Connect 映射到应用
  端口）最终都由前端服务吐出 `index.html`，所以注入点放在那里；代理只用
  `X-MP-Gateway-Prefix` 告诉它页面挂在哪个前缀下，脚本据此优先请求
  `/app/moviepilot/__mp/autologin.json`，直连时退回 `/__mp/autologin.json`。
  > 早期版本把注入做在 `gateway-proxy` 里，直连端口那条路不经过代理，表现就是
  > **局域网能免登录、外网（飞牛 APP / FN Connect）不能**。`tools/autologin_smoke.py`
  > 的 J 组契约就是钉这个回归点。
- **资源 Cookie 必须一起转发**：`system/message`、`system/logging` 这类 EventSource
  接口只认登录时下发的资源令牌 Cookie，不认 Bearer 头。少了它，界面能进但实时消息、
  系统日志会一直空着。
- **令牌会自动续期**：到期前 10 分钟重新登录；服务启动时预热，避免"刚开机第一次打开
  仍是登录页"。
- **失败一律降级**：密码被改过、开了二次验证、被登录限流时，端点返回
  `available=false`，页面就是普通登录页 —— 绝不白屏，也不会把密码或后端错误细节
  吐给页面。失败后进入退避（401 → 60 秒，429 → 300 秒），不会把账号打进登录限流。

> ⚠️ **安全边界**：开启后，任何能打开本应用（即已通过飞牛网关登录）的人都会拿到
> **MoviePilot 超级管理员**身份。多用户 NAS、或把飞牛面板暴露到公网时请保持关闭。
> 在 MoviePilot 界面里改过密码后，`app.env` 的 `SUPERUSER_PASSWORD` 也要同步修改，
> 否则免登录会自动降级回登录页（日志里会写明代登录失败）。
>
> 排障：`app.log` 里搜 `[mp-autologin]`。四种落点——未启用 / 已启用（带账号与配置来源）
> / 开关打开但缺凭据 / 代登录失败（带状态码与重试间隔，不含密码）；端点每次应答也会
> 记一行 `available=`，所以"页面到底拿没拿到令牌"在服务端日志里能直接看到。
> 临时联调可用环境变量覆盖 `app.env` 里的值（优先级更高，重启即失效）：
> `MP_GATEWAY_AUTOLOGIN`、`MP_GATEWAY_AUTOLOGIN_USER`、`MP_GATEWAY_AUTOLOGIN_PASSWORD`。

### 手动操作与回退

```bash
cd /var/apps/moviepilot/target
./cmd/main update          # 立即检查并更新主程序（忽略冷却）
./cmd/main update-check    # 只看有没有新版本，不动任何文件
./cmd/main resources       # 立即同步站点资源（认证扩展 + 站点索引）
./cmd/main rollback        # 回滚到更新前的版本（备份在 <应用目录>/.mp-backup/）
./cmd/main repair          # 修复站点资源与插件源码（模块错位/缺失、插件显示「插件文件缺失」时，纯本地、无网络）
```

> `repair` 是离线自愈（按当前源码结构重新对位资源文件、把插件源码对位到数据目录）；`resources` 是在线升级（比对版本并下载）。启动路径三个都跑：先 `repair`（资源）再 `resources`，最后 `repair_plugins`。
> 通过 fpk 重新安装/升级应用时，应用目录被整包替换，自更新内容随之回到安装包版本 —— 这是预期行为，也让「装包升级」始终是可靠的回退路径。**唯一例外是插件源码**，见下节。

### 插件源码不会被装包/自更新丢掉

MoviePilot 把用户插件装在**源码树**里（上游写死 `ROOT_PATH/app/plugins`），而这正是每次升级都会被整体替换的那一半目录：fnOS 应用中心装包换掉整个 `@appcenter/<app>`，应用内自更新（`stage_replace`）换掉整个 `app/`。插件记录留在数据库里，于是插件页永远显示「插件文件缺失」（前端 `sourceMissing` / 后端 `PluginRuntimeStatus.SOURCE_MISSING`，判定条件只是 `app/plugins/<id>` 不是目录）。

上游的插件备份/恢复在原生部署下**一次都不会触发**（`backup_plugins` / `restore_plugins` 要求存在 `/.dockerenv`）。所以本包在启动前把插件源码对位到应用数据目录并做成软链：

```
${TRIM_PKGVAR}/plugins_src/          # 真实插件源码（不随应用包替换）
<应用目录>/mp/app/plugins -> 上面的目录
```

- 由 `app/bin/mp_plugins.py` 实现；`cmd/main start`（以及 `./cmd/main repair`）调用，失败只记日志、不阻塞启动。后端"起不来就自愈"的那条路（`supervisor.py` 的 `repair_backend_resources`）也会兜一次，避免插件目录问题把启动打进"启动→秒退→重启"的循环
- 每个插件**原子落位**：先复制到持久目录里的临时名再 `os.replace`，失败不留"半个插件"（半份副本会被后续运行当成完整副本，而源码树那份已经换成软链 —— 那等于永久损坏）；链接失败时把本次刚复制进去的条目撤掉，源码树保持原样
- `app/plugins/__init__.py` 只是命名空间标记（上游运行时自己会重写它），缺失不影响导入；源码树里有那份时以包内为准，写入同样原子（写坏会变成 SyntaxError）
- root 下（安装/升级/配置回调会以 root 调 `cmd/main restart`）创建或迁移的插件文件会 `chown` 回应用运行用户（`TRIM_USERNAME`，缺省 `moviepilot`）—— 否则应用写不进插件目录，插件安装直接 PermissionError
- 软链目标被删（断链）时把 `app/plugins` 恢复成真实目录并报 3：应用能起来，插件需在插件页重装
- 卸载时若选择删除数据，`plugins_src` 会与 `config/` 一起清掉（插件源码里带着插件自己的数据）

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
python tools/plugins_smoke.py     # 插件源码持久化（软链不受装包/自更新影响）
python tools/supervisor_smoke.py
python tools/pkgsize_smoke.py
python tools/cmd_env_smoke.py     # 需 bash
python tools/polyfill_smoke.py    # 需 node，缺失则 SKIP
python tools/autologin_smoke.py   # 网关免登录：起真实 node 前端服务 + 网关代理做端到端（需 node）
```

## 已知限制

- 产物体积数百 MB（自带解释器 + 全部依赖），这是「安装时零联网」的代价
- 依赖优先用预编译 wheel（`anitopy`、`pinyin2hanzi` 只有 sdist，不能一刀切 `--no-build`）；wheel 的 manylinux 基线不得高于 glibc 2.36（fnOS / Debian 12）
- MoviePilot 插件依赖由后端运行时 pip 装进 `app/python/site-packages`，**整包升级后需重新安装插件依赖**（插件源码本身已由 `plugins_src` 持久化，不再随升级丢失）
- 自更新要求应用目录（`TRIM_APPDEST`）可写；只读时跳过更新并记日志，只能靠重新装包升级
- 自更新不替换 `app/python/` 运行时；新版引入新依赖时由更新器 pip 增量补装（需联网，走国内镜像）
- 站点资源同步只替换认证扩展与站点索引两个文件，不动 `site-packages`
- 默认 SQLite；需 PostgreSQL 时在 `app.env` 设 `DB_TYPE=postgresql` 并自行安装 PostgreSQL
- 应用中心装包升级的耗时主要来自 fnOS 解压 fpk 本身，瓶颈是**文件数**而非体积，只能在构建期优化

## 致谢与许可

基于 [MoviePilot](https://github.com/jxxghp/MoviePilot)、[MoviePilot-Frontend](https://github.com/jxxghp/MoviePilot-Frontend)、[MoviePilot-Resources](https://github.com/jxxghp/MoviePilot-Resources)（均 GPL-3.0，版权归 jxxghp），运行时来自 [python-build-standalone](https://github.com/astral-sh/python-build-standalone)（PSF-2.0）、依赖管理用 [uv](https://github.com/astral-sh/uv)（MIT / Apache-2.0），运行环境由 [fnOS](https://www.fnnas.com/) 提供。下载加速由 [gh.dpik.top](https://gh.dpik.top/)、[v4.gh-proxy.org](https://v4.gh-proxy.org/) 等镜像提供。

本应用包**整体基于 GPL-3.0 发布**，封装代码（`cmd/`、`app/bin/`、`wizard/`、`build.py` 等）同样以 GPL-3.0 授权。全文见 `LICENSE`，第三方许可见 `THIRD-PARTY-LICENSES`。本应用包仅为第三方封装，请自行评估使用风险。
