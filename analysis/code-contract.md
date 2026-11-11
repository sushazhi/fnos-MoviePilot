# 免登录（MP_GATEWAY_AUTOLOGIN）代码侧契约 · 局域网路径审计

审计对象：`D:\fnos\fnos-MoviePilot`，HEAD = `99568ba`（2026-11-10 20:53:12 +0800），tag `v3.2.5` = `9bb6276`（2026-11-09T12:56:53Z）。
只读审计；本文件是本次唯一新增文件；`git status --porcelain` 全程为空（无被跟踪文件改动）。

---

## 0. 结论速览（先看这三条）

1. **v3.2.5 的包 = 代理注入（`gateway-proxy.py`），不含任何前端注入。**
   `git show v3.2.5:app/bin/frontend-server.js | grep -c -i autologin` → **0**；
   `git show v3.2.5:app/bin/gateway-proxy.py | grep -n autologin` → **10 行命中**（`grep -c -i` 为 **33** 行），含 `AUTOLOGIN_ENDPOINT`、`_build_autologin_js`、`_serve_autologin`。
   反向：HEAD 的 `app/bin/frontend-server.js` `grep -c -i autologin` → **55** 行；HEAD 的 `app/bin/gateway-proxy.py` → **2** 行，且都在注释里（[gateway-proxy.py:426-427](app/bin/gateway-proxy.py#L426-L427)）。

2. **HEAD 的修复没有进入任何可安装包。**
   `git tag --contains 99568ba` → **空**；发布 workflow 只由 `push tags: ['v*']` 触发（[build-and-release.yml:12-15](.github/workflows/build-and-release.yml#L12-L15)），`release` job 还有 tag 守卫（[build-and-release.yml:204](.github/workflows/build-and-release.yml#L204)）。
   而且 **manifest 版本号没跟着改**：`git diff v3.2.5 HEAD -- manifest` → **空**，两边都是 `version = 3.2.5`（[manifest:2](manifest#L2)）。
   外部核对：GitHub Releases API 最新一条仍是 `v3.2.5`（published 2026-11-09T12:58:43Z，assets `moviepilot-3.2.5-amd64.fpk` / `-arm64.fpk`），其后没有任何 Release。

3. **因此“设备上装的是 3.2.5”这句话本身不能定位版本**：从 release 装的 3.2.5 是代理注入，从 HEAD 手工构建的包**也叫 3.2.5**、文件名字节相同（`moviepilot-3.2.5.fpk`）。必须用设备上文件内容或日志关键字区分（见 §5.1）。

---

## 1. 谁注入 HTML、端点契约、退避与降级

### 1.1 两个版本各自的行为（实测，非推断）

| 代码版本 | 直连前端端口 | 走统一网关（gateway-proxy） |
|---|---|---|
| **v3.2.5**（tag，release 里那版） | **不注入**，端点 404 → 停在登录页 | 注入，脚本写死 `P="/app/moviepilot"`，端点在**代理**里应答 → 可免登录 |
| **HEAD `99568ba`**（未发布） | 注入，端点 `/__mp/autologin.json` → 可免登录 | 注入，端点首选 `/app/moviepilot/__mp/autologin.json`，回退根路径 → 可免登录 |

实测证据（本机实跑，非推理）：

```
# v3.2.5 的 frontend-server.js 直连端口（真实 node 起服务 + 假后端）
V3.2.5 CODE / direct port  status=200 injected=False
V3.2.5 CODE / direct endpoint status=404
V3.2.5 CODE / frontend-server log:
[moviepilot-frontend] serving ... on 127.0.0.1:57238      <- 没有任何 [mp-autologin] 行
[moviepilot-frontend] backend at 127.0.0.1:57237

# v3.2.5 的 gateway-proxy.py 走网关前缀（真实代理进程，AF_UNIX 打成 TCP）
V3.2.5 GATEWAY path  status=200 injected=True
V3.2.5 GATEWAY injected script: var P="/app/moviepilot",E="/__mp/autologin.json",K="
V3.2.5 GATEWAY endpoint status=200 body=b'{"available": true, "auth": {"token": "eyJhbGciOiJIUzI1NiIsInR5c'
```

HEAD 侧全量冒烟 `python tools/autologin_smoke.py` → **`== 0 failed ==`**（A–I 组 + J 组全通过），其中 B 组（直连端口）与 C 组（统一网关）都验证了注入与取令牌。

### 1.2 为什么 HEAD 把注入点搬到前端服务

[frontend-server.js:120-125](app/bin/frontend-server.js#L120-L125) 写明理由：本服务是**所有 HTML 的唯一产地**，统一网关那条路和直连端口那条路最终都由它吐 `index.html`；代理只做前缀/头部改写。放在代理里注入时，直连端口（飞牛 APP、FN Connect 映射到应用端口）的页面根本不经过代理。
`README.md` 的同一段把历史症状写成 **“局域网能免登录、外网不能”**（[README.md:111-113](README.md#L111-L113)）——**注意：这与本次用户报告的“局域网也无法跳过”方向相反**，是本轮排查的重要线索（见 §6）。

### 1.3 注入点与端点

- 开关与凭据：`MP_GATEWAY_AUTOLOGIN`，账号取 `MP_GATEWAY_AUTOLOGIN_USER` → `SUPERUSER`，密码取 `MP_GATEWAY_AUTOLOGIN_PASSWORD` → `SUPERUSER_PASSWORD`
  （HEAD：[frontend-server.js:185-197](app/bin/frontend-server.js#L185-L197)；v3.2.5：[gateway-proxy.py:125-136](app/bin/gateway-proxy.py#L125-L136)）。
- `app.env` 读取：`MP_APP_ENV` 或 `${CONFIG_DIR}/app.env`，后者覆盖前者、支持引号与注释（HEAD [frontend-server.js:144-175](app/bin/frontend-server.js#L144-L175)）。
- 端点：`/__mp/autologin.json`（[frontend-server.js:138](app/bin/frontend-server.js#L138)）。HEAD 由**前端服务**应答（[frontend-server.js:936-940](app/bin/frontend-server.js#L936-L940)），v3.2.5 由**代理**应答（`git show v3.2.5:app/bin/gateway-proxy.py` 第 777、831-834 行）。
- 注入位置：`</head>` 前，classic script，早于 `type="module"` 的入口脚本（HEAD [frontend-server.js:471-485](app/bin/frontend-server.js#L471-L485)；v3.2.5 代理第 768-773 行）。
- 注入只发生在 index.html 与 SPA 回退（HEAD [frontend-server.js:803-806](app/bin/frontend-server.js#L803-L806)、[814-819](app/bin/frontend-server.js#L814-L819)），静态资源不注入。
- 响应体字段（[frontend-server.js:322-333](app/bin/frontend-server.js#L322-L333)）：`{available: true, auth:{token, remember:true, originalPath:null}, user:{superUser,userID,userName,avatar,level,permissions}}`；`available:false` 时带 `reason`。
- `reason` 取值全集（[frontend-server.js:367-393](app/bin/frontend-server.js#L367-L393)）：`disabled` / `backoff` / `in_progress` / `login_failed` / `error`。每次应答都会在服务端日志记一行 `[mp-autologin] 端点应答 available=... reason=...`（[frontend-server.js:513-514](app/bin/frontend-server.js#L513-L514)）。
- 退避：普通失败 60s，429 限流 300s（[frontend-server.js:225-226](app/bin/frontend-server.js#L225-L226)、[342-345](app/bin/frontend-server.js#L342-L345)）；令牌到期前 10 分钟续期（[frontend-server.js:224](app/bin/frontend-server.js#L224)、[369-371](app/bin/frontend-server.js#L369-L371)）；启动预热 2s×120s，只在“暂时性失败”（0/500/502/503/504）时继续（[frontend-server.js:228-233](app/bin/frontend-server.js#L228-L233)、[396-411](app/bin/frontend-server.js#L396-L411)）。
- Cookie：登录时拿到的资源令牌 Cookie 会随端点响应一起 `Set-Cookie` 转发（[frontend-server.js:510](app/bin/frontend-server.js#L510)）；少了它界面能进但 EventSource（`system/message`、`system/logging`）永远空。
- 降级：页面永远 200，端点 `available=false`，浏览器侧不写 localStorage 就不 reload（[frontend-server.js:448-463](app/bin/frontend-server.js#L448-L463)），失败只退化成普通登录页，不白屏、不吐密码/后端细节。
- 浏览器侧只写 `localStorage` 的 `auth`（含 `token`）与 `user` 两个 pinia 持久化键，且**已有 `auth.token` 时不覆盖**（[frontend-server.js:443-451](app/bin/frontend-server.js#L443-L451)）。

---

## 2. 局域网路径（fnOS 桌面 → 统一网关 → unix socket → gateway-proxy → frontend-server）要跳过登录页，必须**同时**成立的条件

fnOS 桌面入口确实走统一网关，不是直连端口 —— `app/ui/config` 里 `"type": "iframe"`、`"gatewayPrefix": "/app/moviepilot"`、`"gatewaySocket": "moviepilot.sock"`、`"url": "/app/moviepilot"`；manifest 的 desc 也写“通过 fnOS 统一网关嵌入桌面访问”。

| # | 条件 | 证据 / 失效表现 |
|---|---|---|
| 1 | **开关为真**：`MP_GATEWAY_AUTOLOGIN` ∈ {1,true,yes,on}，环境变量优先于 app.env | [frontend-server.js:177-189](app/bin/frontend-server.js#L177-L189)。默认值是 **0**：新装向导 [lib.sh:290-296](cmd/lib.sh#L290-L296)、老 app.env 补齐 [lib.sh:262](cmd/lib.sh#L262)、升级 [upgrade_callback:80](cmd/upgrade_callback#L80)、配置回调 [config_callback:94](cmd/config_callback#L94) 全部写 0 |
| 2 | **开关真的能被读到**：读的是 `MP_APP_ENV` 或 `${CONFIG_DIR}/app.env` | 文件不存在/不可读 → `readAppEnv` 静默返回 `{}`（[frontend-server.js:148-156](app/bin/frontend-server.js#L148-L156)）→ 等价于关闭。两个子进程都继承 `os.environ.copy()`（[supervisor.py:659-672](app/bin/supervisor.py#L659-L672)、[706-708](app/bin/supervisor.py#L706-L708)），`CONFIG_DIR` 来自 supervisor（[supervisor.py:37](app/bin/supervisor.py#L37)） |
| 3 | **账号非空**：`MP_GATEWAY_AUTOLOGIN_USER` 或 `SUPERUSER` | 空 → 直接不注入脚本（[frontend-server.js:995-998](app/bin/frontend-server.js#L995-L998)） |
| 4 | **密码非空**：`MP_GATEWAY_AUTOLOGIN_PASSWORD` 或 `SUPERUSER_PASSWORD` | 同上。新装向导密码留空即落空值（[lib.sh:297-306](cmd/lib.sh#L297-L306)） |
| 5 | **后端可达**：能 POST `/api/v1/login/access-token` 并拿到 200 | 冷启动期是 0/5xx（暂时性），预热最多等 120s；否则 `reason=login_failed` + 60s 退避（[frontend-server.js:336-351](app/bin/frontend-server.js#L336-L351)） |
| 6 | **凭据与当前密码一致、且未开二次验证** | 401 → `login_failed` → 60s 退避；429 → 300s（[frontend-server.js:341-345](app/bin/frontend-server.js#L341-L345)）。在 MP 界面改过密码后必须同步改 app.env |
| 7 | **网关前缀到达前端服务**：请求头 `X-MP-Gateway-Prefix` 被代理带上，且形如 `/app/<name>` 才被采纳 | 代理侧 [gateway-proxy.py:424-428](app/bin/gateway-proxy.py#L424-L428)；前端白名单 `^/app/[A-Za-z0-9._-]+$`（[frontend-server.js:416-423](app/bin/frontend-server.js#L416-L423)）。**若该头缺失，脚本只会去请求根路径 `/__mp/autologin.json`，而在网关 origin 下这个地址不带前缀，取不到令牌** —— 这正是“新旧文件混装”会造成的静默失败（§4） |
| 8 | **网关把 `/app/moviepilot/__mp/autologin.json` 路由到应用** | 冒烟只覆盖“真代理 + AF_UNIX 打成 TCP”，不覆盖真 fnOS 网关（§3 覆盖面缺口）。若网关拦/404 这个子路径，v3.2.5 没有回退候选（`P` 写死），HEAD 有根路径回退但网关 origin 下同样无效 |
| 9 | **脚本能在页面里执行**（无 CSP 拦截） | 仓库里真实构建产物 `.local-build/_after/fe/dist/index.html`：`Content-Security-Policy` 命中 **0**，`</head>` 恰好 1 处（1031 行）。CSP 不是当前嫌疑 |
| 10 | **浏览器侧写 key 与前端 store 一致**：`auth`（`{token,...}`）与 `user` | [frontend-server.js:448-450](app/bin/frontend-server.js#L448-L450)；前端改结构会“又回到登录页”但不会白屏（[frontend-server.js:199-200](app/bin/frontend-server.js#L199-L200)） |
| 11 | **写到 localStorage 才会 reload 一次** | 写失败不 reload，避免死循环（[frontend-server.js:451](app/bin/frontend-server.js#L451)） |
| 12 | **本标签页会话未被打过“已尝试”标记** | `sessionStorage.mp_autologin_tried`：**先置标记再发请求**（[frontend-server.js:447](app/bin/frontend-server.js#L447)），所以第一次因冷启动失败后，同一标签页内刷新**不会**再试，必须换标签页/关掉重开 |
| 13 | **index.html 不是旧缓存** | 代理把 `index.html` 放进不缓存名单（v3.2.5 第 56 行），前端 index 也 `no-store`（散烟 H4） |

---

## 3. 版本与发布状态（git / workflow / GitHub 实证）

### 3.1 提交时间线（`git log --format='%h %ad %s' --date=iso`）

```
99568ba 2026-11-10 20:53:12 +0800  fix(gateway): 免登录改由前端服务注入 —— 飞牛 APP / FN Connect 等直连端口的访问也能免登录
9bb6276 2026-11-09 12:56:53 +0000  chore(upstream): 跟进上游 v3.2.5，应用版本 3.2.5      <- tag v3.2.5
ef992db 2026-11-09 20:51:31 +0800  feat(gateway): 网关免登录 —— 代理代登录一次，点开应用不再要求登录
```

- `git rev-parse v3.2.5` → `9bb62765cd100b5de7040129e127ead7a030ff06`；`git cat-file -t v3.2.5` → `commit`（轻量 tag）。
- `git merge-base --is-ancestor v3.2.5 HEAD` → **是**；`git log --oneline v3.2.5..HEAD` → **只有 `99568ba` 一个提交**。
- 也就是说：**代理注入（`ef992db`）在 v3.2.5 里，前端注入（`99568ba`）不在 v3.2.5 里**；两者相差约 24 小时。
- `99568ba` 的改动面：`README.md`、`app/bin/frontend-server.js`(+460)、`app/bin/gateway-proxy.py`(-471，注入逻辑整体移除)、`tools/autologin_smoke.py`（重写）。
- `9bb6276`（= v3.2.5）只改了 `build.py` 与 `manifest`。

### 3.2 发布包由什么触发

[build-and-release.yml:12-15](.github/workflows/build-and-release.yml#L12-L15)：**只有 `push tags: ['v*']` 会构建并发布**；`workflow_dispatch` 只构建不发布（`release` job 的 `if: startsWith(github.ref, 'refs/tags/v')`，[第 204 行](.github/workflows/build-and-release.yml#L204)）；刻意不挂 `pull_request`。
发布时还会校验 `tag == manifest.version`（[第 75-83 行](.github/workflows/build-and-release.yml#L75-L83)），产物名取 manifest 版本（[第 66-70、109 行](.github/workflows/build-and-release.yml#L66-L70)）。
自动发版链路另见 [check-upstream.yml](.github/workflows/check-upstream.yml)：每周二检查上游 → 改 pin/manifest → 提交 master → 打 tag → **派发** build-and-release（因为 GITHUB_TOKEN 推的 tag 不会触发别的 workflow）。

### 3.3 关键判定：设备上那个 3.2.5 含哪版注入逻辑

- `git tag --contains 99568ba` → **空** ⇒ 前端注入从未被打过 tag ⇒ 从未产出 Release。
- `git diff v3.2.5 HEAD -- manifest` → **空** ⇒ HEAD 的 `manifest.version` 仍是 `3.2.5`。[manifest:2](manifest#L2)
- 所以：**从官方 Release 安装的 3.2.5 = tag `v3.2.5` = 代理注入（无前端注入）**；构建 HEAD 只会产出同样叫 `moviepilot-3.2.5(-arch).fpk` 的包，版本号无法区分。
- GitHub Releases API（`https://api.github.com/repos/sushazhi/fnos-MoviePilot/releases?per_page=5`）：最新 Release 为 `v3.2.5`，created 2026-11-09T12:56:53Z / published 12:58:43Z，assets `moviepilot-3.2.5-amd64.fpk`(sha256 defb8da6…)、`moviepilot-3.2.5-arm64.fpk`(sha256 83bfb9b6…)；其后无任何 Release。Release 正文只有上游 v3.2.5 的 changelog，**没有一句提到免登录**，用户无从得知该修复未发布。
- 代码来源不受“构建耗时”影响：tag 触发的运行 checkout 的就是该 tag 指向的 `9bb6276`，因此 release 资产只能来自 `9bb6276`。

> 结论（确定）：**用户设备上那个 3.2.5 包，若来自官方 Release，注入逻辑是“代理注入”（只在经过 gateway-proxy 的路径生效），不含前端注入修复**。修复只存在于未打 tag、未发版的 HEAD。

---

## 4. 两个文件必须一起换（混装即静默失效）

HEAD 把注入点从前端“搬走”了：`gateway-proxy.py` 不再注入也不再自己应答端点（[gateway-proxy.py:426-427](app/bin/gateway-proxy.py#L426-L427) 是仅存的注释），端点与注入都在前端服务。
因此：

- 只把 HEAD 的 `gateway-proxy.py` 覆盖到 3.2.5 上 → 代理不注入、也不再应答端点，前端也不注入 ⇒ **两条路径全废，静默回登录页**。
- 只把 HEAD 的 `frontend-server.js` 覆盖到 3.2.5 上 → 代理仍注入并把 `P` 写死 `/app/moviepilot`，代理还会**抢答** `/__mp/autologin.json`（v3.2.5 第 831-834 行），前端那份永远拿不到请求；两条路径仍可免登录，但 J5 契约（代理侧不得注入）被破坏，两条路径行为分叉。

这正是 `tools/autologin_smoke.py` J 组要钉死的东西（J5：`"__mp_autologin" not in proxy_src and "AUTOLOGIN" not in proxy_src`，[autologin_smoke.py:631-632](tools/autologin_smoke.py#L631-L632)）。

---

## 5. 冒烟测试覆盖分析 + Windows 运行方式

### 5.1 用例清单（HEAD，[tools/autologin_smoke.py](tools/autologin_smoke.py)）

| 组 | 场景 | 关键断言 |
|---|---|---|
| A `scenario_disabled` [351](tools/autologin_smoke.py#L351) | 关闭（默认） | 两条路径都不注入；端点 404；一次登录都不发；日志写“未启用” |
| B `scenario_direct` [382](tools/autologin_smoke.py#L382) | **直连前端端口** | 已注入、注入在 `</head>` 前、候选为根路径、`available=true`、store 结构、Cookie 转发、`no-store`、重复请求命中缓存、端点请求不落后端 |
| C `scenario_gateway` [435](tools/autologin_smoke.py#L435) | **走统一网关** | polyfill 与免登录脚本共存、候选首选前缀、代理确实把头交出去、带前缀端点取到令牌 |
| D `scenario_refresh` [465](tools/autologin_smoke.py#L465) | 令牌将到期 | 自动续期、换到新令牌 |
| E `scenario_login_failed` [487](tools/autologin_smoke.py#L487) | **401 凭据错误降级** | 页面仍 200、脚本仍注入、`available=false`、不透错误细节/密码、退避且预热停手、日志写原因 |
| F `scenario_no_credentials` [515](tools/autologin_smoke.py#L515) | 开关开但缺凭据 | 不注入、端点 404、不发登录、日志写“缺少可用凭据” |
| G `scenario_bad_prefix` [532](tools/autologin_smoke.py#L532) | 伪造前缀头 | 白名单拦截，回退根路径，合法前缀仍生效 |
| H `scenario_scope` [556](tools/autologin_smoke.py#L556) | 注入范围 | SPA 回退也注入；静态 js 不注入且缓存策略未变；index 仍 no-store |
| I `scenario_concurrent` [582](tools/autologin_smoke.py#L582) | 并发端点 | 4 并发最多登录一次、同一令牌、无 `in_progress` 降级 |
| J `scenario_contract` [614](tools/autologin_smoke.py#L614) | **接线契约（回归钉点）** | J1 前端定义端点；J2 前端在 index 注入；J3 `sendIndexHtml(` ≥3 处；J4 前端读 app.env；J5 **代理侧不得再注入**；J6 代理必须传 `X-MP-Gateway-Prefix`；J7 前缀白名单；J8 端点不可缓存 |

**已覆盖**：开关开+凭据正确时**直连端口**（B）与**统一网关**（C）两条路径都能取到令牌；凭据错误（E）、缺凭据（F）、关闭（A）、畸形前缀（G）、并发（I）的降级。
**未覆盖 / 覆盖不到**：

1. **真 fnOS 统一网关与真 unix socket**：代理是真的，但 `socket.AF_UNIX` 被替换成 TCP 直连（[autologin_smoke.py:208-209](tools/autologin_smoke.py#L208-L209)），网关行为完全缺席。**“网关是否会把 `/app/moviepilot/__mp/autologin.json` 转发到应用”这件事冒烟测不出来**（§2 条件 8）。
2. **真实 MoviePilot 前端产物**：index.html 是 24 字的最小样本（[autologin_smoke.py:55-58](tools/autologin_smoke.py#L55-L58)），不含真实 SPA 脚本、Service Worker、CSP。
3. **浏览器端脚本执行**：没有任何 DOM/浏览器断言 —— 断言的是“HTML 里含脚本文本”，不是“脚本真的写了 localStorage / 真的 reload”。
4. **sessionStorage 一次性标记**（§2 条件 12）没有用例，冷启动失败后同标签页不再重试这条行为无人钉。
5. **后端冷启动 + 预热窗口**的正例（只有 E 组的“停手”是反例）。
6. **v3.2.5 的旧冒烟更薄**：只有 A–I，且 `ProxyStack` 直接 `load_proxy` 造 `ProxyHandler`（第 104-150、250-290 行），**根本不起 `frontend-server.js`** ⇒ 直连端口那条路径在 v3.2.5 的测试里完全不存在。这也是“直连端口一直不工作却测试全绿”的原因。

### 5.2 Windows 上跑冒烟的准确命令

```bash
# win_bash 里的可用路径（本机已实跑）
export PATH="/c/Users/Administrator/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/node/bin:$PATH"
export PATH="/c/Users/Administrator/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/python:$PATH"
export PYTHONIOENCODING=utf-8
cd /d/fnos/fnos-MoviePilot
python tools/autologin_smoke.py          # HEAD 期望：== 0 failed ==
```

依赖与副产物：`node` 必须在 PATH 上（`node_path()` 用 `shutil.which("node")`，[autologin_smoke.py:76-77](tools/autologin_smoke.py#L76-L77)；本机 node v24.21.0）；缺 node 时全部端到端场景 SKIP 并返回 0（[autologin_smoke.py:647-650](tools/autologin_smoke.py#L647-L650)），**看起来成功其实什么都没测**，别被 0 退出码骗了。脚本会在仓库内生成 `.local-build/_smoke/autologin/`（未跟踪）。注意 MS Store 的 `python` 别名会失败，需用 DSH 自带解释器（本机 Python 3.12.14）。
本次实跑结果：HEAD **0 failed**；v3.2.5 版本（在仓库外临时目录里 `git show` 取出两个文件 + 旧冒烟）也是 **0 failed** —— 两者都“绿”，但覆盖的路径完全不同，这正是本次事故的形态。

---

## 6. 排障判定表（局域网路径“不跳登录页”）

先做一次**版本判定**，它决定后面查什么：

```bash
# 设备上（示例路径）：看 app/bin 里到底是哪一版
grep -c -i autologin /var/apps/moviepilot/target/app/bin/frontend-server.js   # 0 => 代理注入版(v3.2.5 官方包)；>0 => 前端注入版(HEAD)
grep -c -i autologin /var/apps/moviepilot/target/app/bin/gateway-proxy.py     # v3.2.5=33；HEAD=2(仅注释)
# 日志关键字（两版前缀不同，一眼可辨）
grep -n "mp-autologin" /var/apps/moviepilot/*/app.log        # HEAD 的前端服务
grep -n "网关免登录" /var/apps/moviepilot/*/app.log           # v3.2.5 的代理
```

| 症状 | 最可能原因 | 用哪条证据定位 |
|---|---|---|
| 日志里**连一行都没有**（既无 `[mp-autologin]` 也无 `网关免登录`） | 开关没开，或 app.env 没被读到（条件 1/2）。v3.2.5 缺凭据/关闭时**完全不注入脚本**，页面无任何异常表现 | `grep -n "MP_GATEWAY_AUTOLOGIN\|SUPERUSER" ${CONFIG_DIR}/app.env`；分行看 `[mp-autologin] 未启用（MP_GATEWAY_AUTOLOGIN=0）`（[frontend-server.js:1000](app/bin/frontend-server.js#L1000)） |
| 日志 `[mp-autologin] 开关已打开，但缺少可用凭据（SUPERUSER="admin"，SUPERUSER_PASSWORD 为空）` | 条件 3/4：密码为空（向导留空即空值） | 该行本身（[frontend-server.js:995-998](app/bin/frontend-server.js#L995-L998)）；v3.2.5 对应 `网关免登录：开关已打开，但缺少可用凭据`（v3.2.5 第 1188-1191 行） |
| 日志有“已启用（账号 admin）”，端点也返回 `available=true`，但浏览器仍停在登录页 | 浏览器侧：① 端点请求没发出去（网关不转发该子路径，条件 8）② `sessionStorage` 一次性标记已被置位（条件 12）③ localStorage 写失败/键不匹配（条件 10/11） | ① 浏览器地址栏直接打开 `http://<NAS>:<网关端口>/app/moviepilot/__mp/autologin.json`：能返回 JSON 说明路由通；② 换新标签页/无痕再试；③ DevTools 看 localStorage 的 `auth`/`user` 与 Network 里 `?t=` 那条请求的响应 |
| 端点返回 `available=false reason=login_failed`，日志 `代登录失败（status=401）…60s 后重试` | 条件 6：app.env 密码与 MP 库里不一致，或账号开了二次验证 | 日志行 + 端点 `reason`（[frontend-server.js:342-351](app/bin/frontend-server.js#L342-L351)） |
| 端点返回 `reason=backoff` | 刚失败过，正在 60s（或 429 的 300s）退避窗口内 | `reason=backoff`（[frontend-server.js:372-374](app/bin/frontend-server.js#L372-L374)）；429 时查 MoviePilot 登录限流 |
| 端点在**直连端口** 404、网关路径 200 | 设备是 v3.2.5（代理注入版）：直连端口那条路本来就不注入 | §1.1 的实测表；`grep -c -i autologin frontend-server.js` = 0 |
| 网关路径与直连端口**都**不注入，但日志显示“已启用” | 混装了 HEAD 的 `gateway-proxy.py` + 3.2.5 的 `frontend-server.js`（§4），或在用旧缓存 index | 两个 `app/bin` 文件的 grep 计数；index 响应头 `Cache-Control` 是否 `no-store`（散烟 H4） |
| 页面能进但实时消息/系统日志一直是空 | 只有令牌没有资源 Cookie | 端点响应是否带 `Set-Cookie: MoviePilot=...`（[frontend-server.js:510](app/bin/frontend-server.js#L510)，散烟 B11） |

---

## 7. 结论与待确认项

**确定结论**：局域网（fnOS 桌面 → 统一网关 → gateway-proxy）这条路，在 **v3.2.5 官方包**里由**代理注入**负责，理论上可用；但该包**不含**前端注入修复，任何不经过代理的访问（飞牛 APP / FN Connect / 直连端口）拿不到任何注入脚本。前端注入修复只存在于未打 tag、未发版、manifest 版本号未变的 HEAD `99568ba`。
另需注意：**仓库自身文档把历史症状描述为“局域网能免登录、外网不能”**（[README.md:111-113](README.md#L111-L113)），与用户“局域网也无法跳过”的报告方向相反 ⇒ 局域网失败更可能落在 §2 的条件 1-4（开关/凭据）或条件 8（网关是否转发 `/__mp/autologin.json`）上，而不是注入点位置。这两条都需要设备侧证据（app.log 关键字 + 浏览器直连端点 URL）才能定案。

**待确认（不在本仓库可判定范围）**：真 fnOS 网关是否会把 `/app/moviepilot/__mp/autologin.json` 转发到应用（冒烟用 TCP 打桩绕过了 socket 与网关）；设备上 `app/bin` 两个文件的实际 grep 计数；`MP_GATEWAY_AUTOLOGIN`/`SUPERUSER_PASSWORD` 在设备 app.env 里的取值。
