# 局域网打开 MoviePilot 也跳不过登录页 —— 根因报告

- 取证对象：fnOS `192.168.0.2`（profile `home`，fnos-cli 0.5.1，全程只读）
- 取证时刻：2026-11-11 19:45 – 20:05 (+08:00)
- 结论强度标注：**强** = 有设备实测数字/字节数支撑；**中** = 多条推理链收敛但有残余不确定性

---

## 0. 一句话结论

**设备上「网关免登录」开关是关的（`MP_GATEWAY_AUTOLOGIN=0`），而且自 2026-11-09 21:19:21 应用启动后从未重启过 —— 所以跑着的网关代理从一开始就没注入过免登录脚本；这跟"局域网还是外网"无关，两条路都不会跳。** 另外，设备上跑的是 11-09 那一代包（注入写在 `gateway-proxy.py` 里），你 11-10 写的"改由前端服务注入"（`99568ba`）既没有 tag、也没有 Release、`manifest` 版本号也没变，因此**从未进入任何可安装包，设备上自然也没有**。

---

## 1. 证据链

### 1.1 开关当前值 = 0（强）

```bash
fnos-cli --profile home app config wizard moviepilot
```

```json
{ "field": "wizard_gateway_autologin", "initValue": "0",
  "options": [ {"label":"关闭（默认，最安全）","value":"0"}, {"label":"开启","value":"1"} ] }
```

**这个 `initValue` 是"设备上保存的当前值"，不是"包内默认值"**，判决性证据是同一个响应里的密码字段：

| 字段 | 包内 `wizard/config` 是否写了 `initValue` | 设备返回 | 只能来自哪里 |
|---|---|---|---|
| `wizard_port` | 没有 | `"3002"` | 保存值（旁证） |
| `wizard_nginx_port` | 没有 | `"3005"` | 保存值（旁证） |
| `wizard_superuser_password` | **没有**（`wizard/install` 里也只有 rules） | **非空明文密码** | **只能是保存值** |
| `wizard_gateway_autologin` | 有 `"0"` | `"0"` | 保存值 = 默认值 = **0（关闭）** |

包不可能内置一个真实密码当默认值，设备却回填了明文密码 → 该端点确实把"已保存的用户输入"回填进 `initValue`（见 [device-state.md](<analysis/device-state.md>) §2）。

诚实标注：`wizard_port/wizard_nginx_port` 恰好等于 `wizard/install` 的默认值，单独看两可，所以只当旁证；判决性的是密码字段。残余的不确定性只有"从未显式保存过"与"曾显式保存为 0"两种情形 —— **两者对功能是同一结论：免登录不生效。**

### 1.2 自 11-09 21:19:21 起，应用一次都没重启过（强）

四条互相独立的证据同时指向"安装以来生命周期零变动"：

| 证据 | 数值 | 含义 |
|---|---|---|
| 系统日志 `logger list --module 0` | id 2761「应用 MoviePilot **更新成功**」21:18:59；id 2762「**启用成功**」21:19:21 | 最后一次起停就是这次升级；此后无停用/启用/异常退出 |
| `file ls vol1/@apptemp/moviepilot` | `moviepilot-supervisor-stdio.log` 701B，mtim **21:20:01**；`moviepilot-super.pid` mtim 21:19 | `cmd/main start` 每次都会 `nohup … >> SUPER_STDIO_LOG` 并重写 pid 文件 → 文件没动 = 没重启 |
| `monitor proc-srv` / `proc-list` | 应用进程 pid 全部聚集在 **51352–51365** | 同一批进程连续运行至今 |
| `file prop …/config/app.env` | btim 2026-10-05 00:01:39，mtim **2026-11-09 21:16:56** | app.env 35 天前创建（覆盖安装不重建），本次升级后只被动过一次 |

而 `cmd/config_callback` 改完配置**一定会**调 `cmd/main restart`（[config_callback:119](cmd/config_callback#L119)，`restart` = stop+start，见 [main:451](cmd/main#L451)）。所以：**任何"后来在设置里改过"的值，只要没在 11-09 21:20 之后重启过，就没进到正在跑的那个进程里。**

### 1.3 免登录从来没被打开过，而不是被升级刷掉了（中强）

- `MP_GATEWAY_AUTOLOGIN` 这个键是随 `ef992db`（**11-09 20:51:31 +0800**）随功能一起引入的。
- 现存 `app.env` 创建于 **2026-10-05 00:01:39**，比功能早 35 天 → 升级时该键**必然缺失**。
- 缺键只会走 `mp_env_ensure … "0"`（[upgrade_callback:80](cmd/upgrade_callback#L80)、[config_callback:94](cmd/config_callback#L94)、[lib.sh:124](cmd/lib.sh#L124)），安全侧补 **0**；会覆盖的 `mp_env_upsert` 只由向导显式提交触发，而**安装向导 `wizard/install` 里根本没有这个字段**。
- 因此代码上不存在"1 被刷成 0"的路径；真相是这个开关在这台设备上**从未处于 1**。

### 1.4 设备跑的是 11-09 那一代包，不是 HEAD（强 —— 用字节数判定）

`manifest` 的版本号在 `99568ba` 前后都是 `3.2.5`，所以**设备上报的 3.2.5 无法定位版本**。用设备上的文件字节数可以直接判定：

```bash
fnos-cli --profile home file ls vol1/@appcenter/moviepilot/bin
```

| 文件 | 设备实测 | v3.2.5 该文件 | HEAD 该文件 |
|---|---|---|---|
| `frontend-server.js` | **20152** | 20152（`git show v3.2.5:… \| wc -c`） | 38699 |
| `gateway-proxy.py` | **52878** | 52878 | 31914 |

两个文件都恰好等于 v3.2.5 的长度 → 设备上是干净的 v3.2.5 那一代（也说明**不是** app/bin 混装）：

```bash
git show v3.2.5:app/bin/frontend-server.js | grep -c -i autologin   # → 0
git show v3.2.5:app/bin/gateway-proxy.py   | grep -n autologin      # → 10 处（_build_autologin_js / 注入点 / _serve_autologin）
git tag --contains 99568ba                                          # → 空（修复无 tag、无 Release）
git diff v3.2.5 HEAD -- manifest                                    # → 空（版本号没升）
```

### 1.5 三条访问路径矩阵（解释"为什么局域网也不跳"）

| 访问路径 | 是否经过 `gateway-proxy` | v3.2.5（设备）是否注入 | HEAD（未发布）是否注入 |
|---|---|---|---|
| fnOS 桌面 → 统一网关 → unix socket → 代理 → 前端 :3005 | 是 | **会**（开关打开时） | 会（前端服务注入） |
| 飞牛 APP / FN Connect → 应用端口 → 前端 :3005（**直连端口**） | 否 | **不会**（0 注入） | 会 |
| 局域网直接访问 `IP:3005` | 否 | 不会 | 会（但前端只 `listen("127.0.0.1")`，本机实测 `curl` = 000） |

两条结论：
1. 你这台设备上"局域网"能到达应用的方式只有走统一网关那条（`app/ui/config` 里 `type: iframe`、`gatewayPrefix: /app/moviepilot`、`gatewaySocket: moviepilot.sock`；`app config detail` 的 `services[].urls.port` 为空 = 没给应用配端口；3005 只绑回环）。那条路 v3.2.5 是**会**注入的 —— 所以"局域网也不跳"只剩开关/凭据一个解释。
2. 直连端口那条路（你 11-10 的修复想覆盖的）在设备上仍然是 0 注入，因为修复没发版、也没装。

> 你 README 里写的历史症状是"局域网能免登录、外网不能"——方向跟这次报告的相反。方向相反恰恰说明这次不是注入点位置的问题，而是注入的**前置条件**（开关/凭据/进程有没有读到新值）没成立。

### 1.6 应用是好的，不是崩了（强，附一处对队友结论的更正）

- 后端 `MoviePilot` pid 51360，RSS ≈ 417MB，`cpuTime` 持续增长；`backend.log` 20:00:06 仍在写。
- 网关代理：`python3` pid 51365（5 个一组的 python 进程里）。
- **前端 node 服务在跑**：pid **51364**。`proc-list` 里它的进程名不是 `node` 而是 `MainThread`（Node 主线程名会覆盖 `/proc/pid/comm`），所以按名字 grep 会漏；`monitor proc-srv` 把它归在 **`nodejs_v24`** 应用组里（该组只有它和一个 root 的 1162795），uid = **936** = moviepilot 应用用户，pid 又正好落在 51352–51365 这一批里。`app/bin/supervisor.py` 用 `/var/apps/nodejs_v24/target/bin/node` 起前端，与分组合得起来。
  - 更正：[device-state.md](<analysis/device-state.md>) §5/§0-C6 写的"完全没有 node 进程、前端没在跑"**不成立**，结论以本节为准；这条更正不影响该报告其余结论。

---

## 2. 为什么"连局域网也不行"—— 两种情形都落在同一处

- **情形 A（最可能）：你从没在应用设置里把「网关免登录」选成开启并保存。** 于是代理 `_AUTOLOGIN_JS_BYTES` 为空：**完全不注入脚本**（[gateway-proxy.py:188-190](app/bin/gateway-proxy.py#L188-L190)），页面就是普通的 MP 登录页，没有任何报错提示 —— 与"局域网也不跳"完全吻合。
- **情形 B：你改过，但没生效。** 可能是保存后应用没重启（本次取证显示从未重启）、或改了 `app.env` 但代理只在进程启动时读一次 `_APP_ENV`（[gateway-proxy.py:125-136](app/bin/gateway-proxy.py#L125-L136)），运行中的进程仍是 0。
- **情形 C（次要）：开关确实是 1，但代登录失败。** 典型是 MoviePilot 界面里改过密码、`app.env` 的 `SUPERUSER_PASSWORD` 没同步 → 后端 401 → 退避 60s（[gateway-proxy.py:533](app/bin/gateway-proxy.py#L533)），表现同样是停回登录页。

三种情形都用**同一条命令**区分（见下一节）。

---

## 3. 一条命令定案（需要 fnOS 登录态或 SSH）

在带飞牛登录态的浏览器里直接打开（或浏览器控制台 fetch）：

```
https://192.168.0.2:8667/app/moviepilot/__mp/autologin.json
```

- 返回 `{"available":true,…}` → 免登录已生效（问题在浏览器侧缓存/localStorage）。
- 返回 `reason:"disabled"` → **情形 A/B：开关没生效**（本次证据强烈指向这里）。
- 返回 `reason:"login_failed"` / `"backoff"` → **情形 C：凭据与 MP 库里的密码不一致（或开了 2FA / 被限流）**。
- HTTP 404 → 网关没把该路径转进应用（v3.2.5 只有写死的单候选 `PREFIX+E`，没有回退候选）。

或者 SSH 上看服务端日志关键字：

```bash
grep -aE 'MP_GATEWAY_AUTOLOGIN' /vol1/@appdata/moviepilot/config/app.env        # 期望看到 =1
grep -a '网关免登录' /vol1/@appdata/moviepilot/moviepilot.log | tail -20         # v3.2.5 的代理关键字
grep -a '\[mp-autologin\]' /vol1/@appdata/moviepilot/moviepilot.log | tail -20    # 若已装 HEAD 版则用这个关键字
```

> `fnos-cli` 读不到文件内容（`file download-url` 恒被脱敏成 `[REDACTED]`），这是产品设计，本次没有绕过。

---

## 4. 修复步骤

1. **打开开关**（应用中心 → MoviePilot → 设置 → 网关免登录 → 开启 → 保存）。`config_callback` 会 `mp_env_upsert MP_GATEWAY_AUTOLOGIN 1` 并**自动重启应用**；等 30–60 秒再打开应用。
   - 或 SSH：把 `app.env` 里 `MP_GATEWAY_AUTOLOGIN=0` 改成 `1`，然后 `/vol1/@appcenter/moviepilot/cmd/main restart`。
   - 安全提醒（README 也写了）：开启后**任何能打开本应用（即已通过飞牛网关登录）的人都拿到 MoviePilot 超管身份**；多用户 NAS / 面板暴露公网时不要开。
2. **同步凭据**：如果 MP 里改过超管密码，`app.env` 的 `SUPERUSER_PASSWORD` 必须一致，否则会落进情形 C。
3. **要让"直连端口 / 飞牛 APP / FN Connect"也免登录**，得把 HEAD 的修复发版：
   - `99568ba` 目前无 tag、无 Release，`manifest` 版本号没动 → 装不上（`build-and-release.yml` 只由 `push tags: ['v*']` 触发）。
   - 建议：升 `manifest` 版本（如 `3.2.6` 或 `3.2.5-1`）→ 打 tag 触发 CI → 再在应用中心升级。
   - **两个文件必须成对替换**：HEAD 的 J5 契约（`tools/autologin_smoke.py`）要求 `gateway-proxy.py` 不再注入；只换其中一个会让两条路径全部静默回登录页。
4. **验证**：重复第 3 节的两条命令；桌面/APP 各开一次。

---

## 5. 顺带发现（建议单独跟进）

1. **凭据暴露面（中）**：`app config wizard moviepilot`（`GET /app-center/v1/config/wizard`）会向**具备应用中心读权限**的调用者返回**明文**的已保存超级管理员密码。本报告与两份队友报告均未记录该值，但这是 fnOS 应用中心侧的一个真实暴露面。
2. **两份 app.env（低）**：`vol1/@appdata/moviepilot/config/app.env`（应用版，7174B，uid 936）与 Docker 遗留 `vol1/1000/AppData/moviepilot/config/app.env`（6720B，uid 0，10-01）。排查时别改错文件。
3. **两个游离 python 进程（低）**：uid 936 下除主管/后端/代理外还有 `python` pid 52105、369937，各 ~5MB、`cpuTime=0`，像是卡住的更新/资源脚本残留；与本次问题无关，但值得顺手看一眼。

---

## 6. 证据文件

- 设备侧取证（fnos-cli 只读，417 行）：[analysis/device-state.md](<analysis/device-state.md>)
- 代码侧契约与版本状态（215 行）：[analysis/code-contract.md](<analysis/code-contract.md>)
- 本报告：[analysis/root-cause-lan-autologin.md](<analysis/root-cause-lan-autologin.md>)
