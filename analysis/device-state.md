# 设备侧取证：fnOS 上 MoviePilot 免登录开关与运行状态

- 设备：`192.168.0.2`（fnOS，profile `home`）
- CLI：`/c/Users/Administrator/.dsh/skills/fnos-cli/scripts/fnos-cli.cmd`（win_bash），`fnos-cli 0.5.1`
- 取证时刻：**2026-11-11 20:00:28 +08:00**（Asia/Shanghai，下文所有时间均已换算）
- 约束：全部为只读命令；未执行任何带 `--yes` 的写操作（无安装/卸载/启停/上传/改名/删除/清日志）。本报告不记录任何密码/token/授权码。

---

## 0. 结论先行

| # | 结论 | 强度 |
|---|---|---|
| C1 | 设备上「网关免登录」开关当前值为 **`wizard_gateway_autologin = "0"`（关闭）** | 强 |
| C2 | `app config wizard` 返回的 `initValue` 是**设备上保存的当前值**，不是包内默认值 | 强（有决定性反证字段，见 §2） |
| C3 | 最近一次安装/升级窗口是 **2026-11-09 21:15:58 → 21:19:21**；其后应用**再未重启**（至今约 46h41m） | 强 |
| C4 | 「网关免登录」是**本次（11-09 21:16 左右）升级才第一次进入设备**的新功能：现存 `app.env` 创建于 2026-10-05 00:01:39，早于该功能首次提交（11-09 20:51），因此升级当时该键必然缺失，被 `mp_env_ensure` 以 **默认 0** 补入 | 中强（见 §3.3 的推理链与前提） |
| C5 | 升级/重装**不可能**把该开关从 1 刷回 0（代码路径只补缺失键、不覆盖已有值；且 app.env 没有被重建） | 强（代码+元数据双重） |
| C6 | 应用进程组存在且后端活跃，但 **proc-list/proc-srv 中不存在任何 `node` 进程**（前端 `frontend-server.js` 未运行） | 强（对"不存在"这一事实）；成因未定 |
| C7 | 因此"局域网打开也不跳登录页"的**直接充分解释就是 C1：开关没开** | 强 |

---

## 1. 应用状态

```bash
"$CLI" --profile home app list
"$CLI" --profile home app status moviepilot
```

`app status moviepilot` 关键字段：

```json
{ "appName": "moviepilot", "name": "MoviePilot", "version": "3.2.5",
  "status": "running", "source": "thirdparty", "docker": false,
  "dependencyAppNames": ["nodejs_v24"],
  "appServiceInfo": { "type": "iframe", "microApp": true,
                      "urls": { "path": "/app/moviepilot" } } }
```

`app list` 共 24 个应用，其中相关项：

```
moviepilot    | MoviePilot   | 3.2.5      | running | deps=['nodejs_v24']
nodejs_v24    | Node.js v24  | 24.22.1-1  | nostart | deps=None
deepseek.harness | DeepSeek Harness | 0.4.6 | running | deps=['nodejs_v24']
```

`app config detail moviepilot`：

```json
{ "appName": "moviepilot", "hasWizardConfig": true, "enableAutoUpdate": true,
  "installType": "volume", "installVolumeID": 1, "isDocker": false,
  "disableAuthorizationPath": false, "userName": "moviepilot" }
```

> 注意：`nodejs_v24` 的状态是 **`nostart`**（应用中心没有需要常驻的守护进程）；它只提供 `/var/apps/nodejs_v24/target/bin/node`。这与 §5 的 node 进程缺失问题相关。

---

## 2. 问题（1）：免登录开关当前到底是什么值

### 2.1 设备返回值

```bash
"$CLI" --profile home app config wizard moviepilot
```

返回（结构完整保留，**密码字段已由本报告打码**）：

```json
{
  "wizardContent": [
    { "stepTitle": "服务配置", "items": [
      { "field": "wizard_port",            "initValue": "3002", "type": "text" },
      { "field": "wizard_nginx_port",      "initValue": "3005", "type": "text" },
      { "field": "wizard_auto_update",     "initValue": "1",    "type": "select" },
      { "field": "wizard_auto_update_resource", "initValue": "1", "type": "select" },
      { "field": "wizard_gateway_autologin",    "initValue": "0", "type": "select",
        "options": [ {"label":"关闭（默认，最安全）","value":"0"},
                     {"label":"开启","value":"1"} ] }
    ]},
    { "stepTitle": "管理员密码", "items": [
      { "field": "wizard_superuser_password", "initValue": "[REDACTED-BY-AGENT]", "type": "password" }
    ]}
  ]
}
```

**结论 C1：`wizard_gateway_autologin = "0"` → 「网关免登录」当前是关闭状态。**

### 2.2 逐字段对比：`initValue` 是"当前保存值"而非"包内默认值"

包内文件 `wizard/config`（本仓库，与设备返回的 step 结构一一对应：`服务配置` / `管理员密码`，说明设备渲染的就是这份 config 向导）：

| 字段 | 包内 `wizard/config` 是否给 `initValue` | 设备返回 | 该值只能来自哪里 |
|---|---|---|---|
| `wizard_port` | **没有** | `"3002"` | 保存值（安装时提交/持久化的值） |
| `wizard_nginx_port` | **没有** | `"3005"` | 保存值 |
| `wizard_superuser_password` | **没有** | 非空明文密码（已打码） | **只能是保存值** |
| `wizard_auto_update` | 有，`"1"` | `"1"` | 两可（保存值=默认值） |
| `wizard_auto_update_resource` | 有，`"1"` | `"1"` | 两可 |
| `wizard_gateway_autologin` | 有，`"0"` | `"0"` | 两可**→ 由下面第 3 点定夺** |

**论证：**

1. **决定性反证字段是 `wizard_superuser_password`。** 它在 `wizard/config` 里**没有** `initValue`，在 `wizard/install` 里也只有 `rules`（`required: true`、`min/max/pattern`），**同样没有 `initValue`**。任何软件包都不可能内置一个真实密码作为"默认值"，设备却返回了一个非空明文密码 → 该端点**确实会把"已保存的用户输入"回填进 `initValue`**。这就证明了 `app config wizard` 的语义是「包内定义（label/options/rules）+ 当前保存值」的合并结果，而不是「包内定义原样回显」。
2. 若该端点只是回显包内默认值，那么配置向导里的每一次"打开-保存"都会把所有开关重置为出厂值 —— 这会使该向导在功能上不可用，与它的用途（`重置超级管理员密码（留空则不修改）` 这类语义）矛盾。
3. 于是 `wizard_gateway_autologin = "0"` 的含义是：**当前保存值就是 0**。残余的不确定性只有"从未显式保存过（走包内默认 0）"与"曾显式保存为 0"这两种情形 —— **但两者对功能是同一结论：免登录不生效。**

**诚实标注（可能的替代解释）：** `wizard_port=3002` / `wizard_nginx_port=3005` 恰好等于 `wizard/install` 里写的默认值（`wizard/install` 确实给了 `"initValue": "3002"` / `"3005"`）。所以这两个字段单独看**不能排除**"后端把 install 向导默认值当作 config 向导 initValue 兜底"这一替代解释 —— 因此它们只是**旁证**，不是判决性证据；判决性证据是上表第 3 行的密码字段。

### 2.3 附带发现（安全问题，已打码）

`app config wizard moviepilot` 对 `wizard_superuser_password` 返回了**明文**的已保存超级管理员密码。任何具备应用中心读权限的 API 调用者都能读到它。本报告不记录该值。这是应用中心侧的一个凭据暴露面，建议作为独立问题处理。

---

## 3. 问题（2）：最近一次安装/升级/启动时间，以及"升级是否会把开关刷回默认值"

### 3.1 系统日志（`logger list --module 0`，即 System）

```bash
"$CLI" --profile home logger list --module 0 --page 1 --page-size 100
```

该模块**只有 86 行**（page 2/3 为空），最早一条是 `2026-11-05 19:24:40`，即系统日志保留窗口约 6 天。MoviePilot 相关行（`eventtm` → Asia/Shanghai，`id` 为日志主键）：

| id | 时间 (+08:00) | 内容 |
|---|---|---|
| 2762 | 2026-11-09 21:19:21 | 应用 MoviePilot **启用成功** |
| 2761 | 2026-11-09 21:18:59 | 应用 MoviePilot **更新成功** |
| 2758 | 2026-11-09 20:03:20 | 应用 MoviePilot **自动启动成功**（开机自启） |
| 2697 | 2026-11-08 19:20:25 | 应用 MoviePilot 异常退出 |
| 2678 | 2026-11-08 10:32:47 | 应用 MoviePilot 异常退出 |
| 2648 | 2026-11-06 11:41:20 | 应用 MoviePilot 异常退出 |
| 2622 | 2026-11-05 19:27:35 | 应用 MoviePilot 启用成功 |
| 2621 | 2026-11-05 19:27:18 | 应用 MoviePilot 停用成功 |
| 2620 | 2026-11-05 19:25:52 | 应用 MoviePilot 启用成功 |
| 2619 | 2026-11-05 19:25:20 | 应用 MoviePilot 停用成功 |
| 2618 | 2026-11-05 19:24:40 | 复制 `/vol1/@appdata/moviepilot/config/plugins_backup/autosignin` 到 `/vol1/@appcenter/moviepilot/mp/app/plugins/autosignin` |

> 保留窗口内**没有**"应用 MoviePilot 安装成功"记录。最早的相关事件是 11-05，故"首次安装时间"无法由系统日志确认（只能由文件创建时间推断，见 §3.2）。

### 3.2 文件元数据（`file ls` / `file prop`）

路径语义（由 id 2618 的日志行直接印证，非猜测）：

- `TRIM_PKGVAR` = **`/vol1/@appdata/moviepilot`**
- `TRIM_APPDEST` = **`/vol1/@appcenter/moviepilot`**（其 `bin/` 内即 `supervisor.py`、`frontend-server.js`、`gateway-proxy.py`）

```bash
"$CLI" --profile home file ls vol1/@appdata/moviepilot     # 注意：不带前导斜杠才被 CLI 接受
"$CLI" --profile home file ls vol1/@appcenter/moviepilot
"$CLI" --profile home file prop vol1/@appdata/moviepilot/config/app.env
```

`vol1/@appdata/moviepilot`（全部 uid=936）：

| 条目 | size | btim | mtim | mtim (+08:00) |
|---|---|---|---|---|
| `config/` | dir | 1790863536 | 1794230401 | 2026-10-01 22:05:36 → / 2026-11-09 21:20:01 |
| `moviepilot.log` | 666149 | 1790866788 | 1794397762 | 2026-10-01 22:59:48 → **2026-11-11 19:49:22** |
| `tmp/` | dir | 1792232755 | 1792868402 | 2026-10-17 18:25:55 → / 2026-10-24 20:20:02 |
| `update.log` | 7779 | 1794225577 | 1794230360 | 2026-11-09 19:59:37 → **2026-11-09 21:19:20** |
| `backend.log.1` | 9084876 | 1793702886 | 1794225350 | 2026-11-03 18:48:06 → 2026-11-09 19:55:50 |
| `backend.log` | 934922 | 1794225790 | 1794398148 | 2026-11-09 20:03:10 → **2026-11-11 19:55:48** |
| `upgrade.log` | 252 | 1794229995 | 1794230283 | 2026-11-09 21:13:15 → **2026-11-09 21:18:03** |
| `install.log` | 1238 | 1794230215 | 1794230264 | 2026-11-09 21:16:55 → **2026-11-09 21:17:44** |
| `plugins_src/` | dir | 1794230360 | 1794230557 | 2026-11-09 21:19:20 → / 2026-11-09 21:22:37 |

`vol1/@appcenter/moviepilot`：`bin/ config/ frontend/ mp/ python/ ui/ moviepilot.sock`，全部 btim=**1794230158 = 2026-11-09 21:15:58**，`mp/` 的 mtim=**1794230241 = 2026-11-09 21:17:21**。
（`bin/` 内含 `frontend-server.js`、`gateway-proxy.py`、`mp_init_superuser.py`、`mp_plugins.py`、`mp_resources.py`、`mp_updater.py`、`supervisor.py`。）

`vol1/@appdata/moviepilot/config/app.env`：

```json
{ "prop": [ { "btim": 1791129699, "mtim": 1794230216, "size": 7174, "space": 8192, "uid": 936 } ],
  "result": "succ" }
```

- **btim 1791129699 = 2026-10-05 00:01:39** ← app.env 的创建时间
- **mtim 1794230216 = 2026-11-09 21:16:56** ← 最后一次修改

`btim` 语义的判定依据：`@appcenter/moviepilot/*` 这些**刚被本次升级新建**的条目 `btim == mtim ==` 升级时刻；而 `app.env` 的 `btim`(10-05) 显著早于 `mtim`(11-09)。故 `btim` 是**创建时间**，不会因内容修改而前移。

### 3.3 时间线（Asia/Shanghai）

```
2026-10-01 22:05:36   @appdata/moviepilot/config 创建（应用数据目录存在）
2026-10-05 00:01:39   app.env 创建                     ← 现存 app.env 的生日
2026-11-09 19:55:50   backend.log 轮转（backend.log.1 mtim）
2026-11-09 19:59:37   update.log 创建
2026-11-09 20:03:10   backend.log 重建（应用被拉起）
2026-11-09 20:03:20   系统日志：应用 MoviePilot 自动启动成功
2026-11-09 21:13:15   upgrade.log 创建
2026-11-09 21:15:58   @appcenter/moviepilot/* 全部创建   ← 升级写包体
2026-11-09 21:16:55   install.log 创建                   ← install_callback 开始
2026-11-09 21:16:56   app.env mtim 更新                  ← 本次升级唯一一次触碰 app.env
2026-11-09 21:17:21   mp/ mtim 更新
2026-11-09 21:17:44   install.log 最后一次写入
2026-11-09 21:18:03   upgrade.log 最后一次写入
2026-11-09 21:18:59   系统日志：应用 MoviePilot 更新成功
2026-11-09 21:19:20   update.log 最后一次写入；plugins_src 创建
2026-11-09 21:19:21   系统日志：应用 MoviePilot 启用成功     ← 当前进程组的起点
2026-11-09 21:20:01   @appdata/moviepilot mtim
2026-11-11 19:26:39   登录日志：mistyrain 登录成功 IP:192.168.0.8（最近一次局域网登录）
2026-11-11 19:49:22   moviepilot.log mtim（仍在写）
2026-11-11 19:55:48   backend.log mtim（仍在写）
```

**回答"最近一次安装/升级/启动"：**

- **最近一次安装/升级：2026-11-09 21:15:58 → 21:19:21**（包体写入 21:15:58；`install.log` 21:16:55–21:17:44；`upgrade.log` 到 21:18:03；系统日志"更新成功"21:18:59）。
- **最近一次启动：2026-11-09 21:19:21**（"启用成功"）。此后 system 日志中**没有任何** MoviePilot 的"停用/异常退出/启用"记录，且 `backend.log`（创建于 11-09 20:03:10）至今仍在被写 → 当前进程组已连续运行约 **1 天 22 小时 41 分**。
- 升级前该应用还经历过 11-05 19:24–19:27 的两次"停用→启用"、以及 11-06、11-08 的"异常退出"（异常退出是整机/多应用同时记录，属重启类事件，非本应用特有）。

### 3.4 升级/重装是否可能把开关刷回默认值 —— **不会，且关键点在于"该键在升级前根本不存在"**

**（a）代码路径只能"补缺失键"，不能覆盖已有值。** 本仓库：

`cmd/upgrade_callback:80`
```sh
# 网关免登录：老 app.env 没有这个键，补 0（关闭）——补 1 等于替用户打开超管直通。
mp_env_ensure "${CONFIG_DIR}/app.env" "MP_GATEWAY_AUTOLOGIN" "0"
```
`cmd/config_callback:94` 同样是 `mp_env_ensure ... "0"`；只有在向导**显式**提交了 `wizard_gateway_autologin` 时（`cmd/config_callback:81-87`）才走 `mp_env_upsert`（存在则替换）。

`cmd/lib.sh:124-133`：
```sh
mp_env_ensure() {   # 新增功能默认值补齐：已有值一律不动 —— 用户手动改过的配置必须保留
    if grep -q "^${key}=" "${env_file}"; then return 0; fi   # 已存在 → 直接返回
    echo "${key}=${value}" >> "${env_file}"
}
```
而会覆盖的 `mp_env_upsert`（lib.sh:148）**仅**在向导显式填写时被调用。安装向导 `wizard/install` 的字段只有 `wizard_superuser` / `wizard_superuser_password` / `wizard_port` / `wizard_nginx_port` —— **没有 gateway_autologin**；`wizard/config` 才有它，而 config 向导只有经用户"保存"才触发 `config_callback`。

**（b）app.env 没有被重建（本次是覆盖安装，不是全新安装）。** `app.env` 的 `btim = 2026-10-05 00:01:39`，早于本次升级 35 天；`cmd/lib.sh:216-219` 明确："fnOS 的『重新安装』同样会走 install_callback，所以覆盖安装路径绝不能重建"，文件已存在时只补缺失键。因此**不存在"删掉 app.env → 键丢失 → 被补成 0"**这条把 1 刷成 0 的路径。

**（c）真正的因果是"新功能首次落地"。** 本仓库提交时间（+08:00）：

```
ef992db  2026-11-09 20:51:31  feat(gateway): 网关免登录 —— 代理代登录一次，点开应用不再要求登录
9bb6276  2026-11-09 20:56:53  chore(upstream): 跟进上游 v3.2.5，应用版本 3.2.5
99568ba  2026-11-10 20:53:12  fix(gateway): 免登录改由前端服务注入
```

`wizard/config` 里的 `wizard_gateway_autologin`、`mp_env_ensure ... "0"` 都是随 `ef992db`（11-09 20:51）引入的。设备升级发生在 **11-09 21:15:58 起**，即**该功能上线约 25 分钟后**。现存 `app.env` 创建于 10-05，早于该功能 → 升级时该键**必然缺失** → 被 `mp_env_ensure` 以 **0** 补入（这正是 21:16:56 那次 app.env 修改）。**所以开关是"从未被打开过"，而不是"被升级刷掉了"。** 这同时解释了它为什么与包内默认值一致。

**（d）版本层面的旁证（推理，非设备实测）。** 设备版本号 `3.2.5` 对 `9bb6276` 与 `99568ba` 是一样的（`manifest` 版本未变），因此**无法从设备侧区分装的是哪一个**。但设备升级时间（11-09 21:15）早于 `99568ba`（11-10 20:53）约 23 小时，若设备包来自本仓库 master 构建，则设备上跑的是 `ef992db`/`9bb6276` 一代代码 —— 在那一代：

```
git show 9bb6276:app/bin/frontend-server.js | grep -c AUTOLOGIN   → 0
git show 9bb6276:app/bin/gateway-proxy.py   | grep -c AUTOLOGIN   → 32
grep -c AUTOLOGIN app/bin/gateway-proxy.py  (HEAD)                → 2
```

即：设备那一代的免登录注入在 **`gateway-proxy.py`（Python 网关代理）** 里，覆盖"经飞牛网关打开 `/app/moviepilot`"这条路径；而 `99568ba` 才把注入改到前端 node 服务。**气质上这与"开关关闭 ⇒ 桌面打开也不跳登录页"完全一致。**

---

## 4. 用户与权限

`file share list` 返回 `[]`；`file ls`（聚合根）返回 `AppData/Photos/pt/media/scripts`，`uid` 为 0/989；应用运行用户 **uid 936**。

`logger list --module 1`（Login）保留 19 行，全部为 `mistyrain`：

| 时间 (+08:00) | 内容 |
|---|---|
| 2026-11-11 19:26:39 | mistyrain 登录成功 IP:192.168.0.8 |
| 2026-11-11 11:29:22 | mistyrain 登录成功 IP:27.27.95.248（公网） |
| 2026-11-11 08:30:37 | mistyrain 登录成功(via token) IP:2408:844f:...（IPv6） |
| 2026-11-10 21:47:50 | mistyrain 登录成功 IP:192.168.0.7 |
| 2026-11-10 19:05:13 | mistyrain 登录成功 IP:192.168.0.7 |
| 2026-11-09 23:27:21 | mistyrain 登录成功(via token) IP:192.168.0.4 |
| 2026-11-09 20:00:00 | mistyrain 登录成功 IP:192.168.0.3 |

**能说明：** 局域网内确有大量 `mistyrain` 的登录成功记录（.3/.4/.5/.6/.7/.8 多个内网地址），最近一次是取证前 34 分钟的 `192.168.0.8`。这与"用户确实通过局域网在访问设备"一致。
**不能说明：** 这些是 **fnOS 系统**登录，不是 MoviePilot 登录；系统日志**不记录**"谁在什么时候打开了哪个应用"。因此**无法**用日志证明"某次打开 MoviePilot 时被要求登录 MP 页面"。应用打开行为需要 `backend.log`/`moviepilot.log` 的内容（拿不到，见 §6）。

---

## 5. 运行进程证据

```bash
"$CLI" --profile home monitor proc-list          # 182 个进程，字段: pid,name,uid,status,cpu,cpuTime,memory
"$CLI" --profile home monitor proc-info --pids 51360,51352,51364,51365,52105,369937
"$CLI" --profile home monitor proc-srv           # 按应用分组的进程
```

`proc-srv` 中 `moviepilot` 组（共 5 个）：

| pid | name | uid | memory | cpuTime |
|---|---|---|---|---|
| 51352 | `python3` | 936 | 12,120,064 | 108 |
| 51360 | `MoviePilot` | 936 | 416,972,800 | 33601 |
| 51365 | `python3` | 936 | 7,213,056 | 85 |
| 52105 | `python` | 936 | 5,124,096 | 0 |
| 369937 | `python` | 936 | 5,373,952 | 0 |

`proc-list` 按 uid=936 过滤另多出：`MainThread` pid 51364（21,197,376）。全表 114 个不同进程名中，`python3×6`、`nginx×5`、`postgres×51`、`fngateway-linux×1`（pid 4426，uid 917）。

**要点：**

1. **没有 `node` 进程。** 对 `proc-list` 原始 JSON 做 `grep -i node` 只命中 `nginx`；114 个进程名里不存在 `node`。而 `app/bin/supervisor.py`（两代代码均如此）把前端描述为 `node frontend-server.js (127.0.0.1:NGINX_PORT)`，并以"进程名是 node，只能按命令行匹配 frontend-server.js 精确路径"来识别它 —— 也就是说 **若前端在跑，进程名就应该叫 `node`**。
   - 结论：**前端（node）服务没有运行**；后端（pid 51360，进程名被 `setproctitle` 改名为 `MoviePilot`，RSS≈417MB）与若干 python 进程在运行，后端日志仍在写（19:55:48）。
   - **成因无法从 CLI 判定**：`supervisor.py:656-657` 在找不到 node 时会记 `错误: 未找到 node 可执行文件`，而 `nodejs_v24` 应用状态是 `nostart`；但这属于**代码层可能原因**，设备侧的 supervisor/backend 日志内容读不到（§6），故不能断言。
   - 与"用户能看到 MP 登录页"存在**表面矛盾**（若前端未运行，经网关的页面本应打不开）。我不为此矛盾编造结论 —— 它只能靠日志内容或人工在 NAS 上 `ps`/`curl` 收敛。**无论该矛盾的答案是什么，都不改变 C1（开关=0）这一直接原因。**
2. `proc-info` 与 `proc-list` 字段相同：**没有命令行、没有进程启动时间**。因此"启停多久"**不能**由进程数据回答；本报告 §3.3 的"启动于 11-09 21:19:21"来自系统日志事件，不是进程数据。
3. `cpuTime` 字段单位在文档中未定义（`MoviePilot` 从 33587→33601 在小幅增长），**不要**用它推算 uptime 或负载。
4. `fngateway-linux`（pid 4426，uid 917）在运行 —— 飞牛统一网关在线。

### 5.1 桌面入口指向（确认用户打开的是哪个 MoviePilot）

```bash
"$CLI" --profile home app entry list
"$CLI" --profile home app desktop list
```

```json
{ "appName": "moviepilot", "entryKey": "entry:469421",
  "entryName": "moviepilot.Application", "type": "iframe", "status": "running",
  "title": "MoviePilot", "uri": { "path": "/app/moviepilot", "fnDomain": "moviepilot" } }
```

桌面条目 `MoviePilot`（sort 310，`openType: iframe`）指向 **`/app/moviepilot`**，即 fnOS 应用（不是任何 Docker 实例）。

**排除"打开的是 Docker 版 MoviePilot"：**

```bash
"$CLI" --profile home docker container ls --all
"$CLI" --profile home docker compose ls
```

- 容器（含已停止）：`qinglong`(running)、`lucky`(running)、`openlist`(created)、`kvrocks`(created)、`xiaoya`(created)、`decotv`(exited)、`xiaoyakeeper`(exited) —— **没有任何 moviepilot 容器**。
- compose 项目 `moviepilot` 存在（`/vol1/1000/AppData/moviepilot/docker-compose.yml`，Status `created`），即一份**未启动的 Docker 遗留工程**。
- 这解释了设备上存在**两份** `app.env`：应用版 `/vol1/@appdata/moviepilot/config/app.env`（7174B，uid 936，mtim 11-09 21:16:56）与 Docker 遗留版 `/vol1/1000/AppData/moviepilot/config/app.env`（6720B，uid 0，mtim **2026-10-01 23:30:44**，同目录还有 `user.db` 20MB、`docker-compose.yml`）。**取证只针对前者（fnOS 应用）**，后者与本次问题无关。

---

## 6. 能证明什么 / 不能证明什么

### 6.1 能证明（已由命令输出直接支撑）

1. **免登录开关当前 = `"0"`（关闭）**，且该端点的 `initValue` 语义是"当前保存值"（决定性反证：无包内初值的 `wizard_superuser_password` 返回了具体值）。→ 命令：`app config wizard moviepilot`；对比文件：`wizard/config`、`wizard/install`。
2. **升级确实发生过，时间是 2026-11-09 21:15:58 → 21:19:21，随后应用于 21:19:21 启动并持续运行至今**（≈1天22小时41分，期间无任何停用/异常退出事件）。→ `logger list --module 0` + `file ls` / `file prop` 的 mtime。
3. **本次是覆盖安装而非全新安装**：`app.env` 的创建时间（10-05）远早于升级（11-09），且升级过程中它只被修改过一次（21:16:56）。→ `file prop vol1/@appdata/moviepilot/config/app.env`。
4. **代码上不存在"升级把开关由 1 覆盖成 0"的路径**：`mp_env_ensure` 只补缺失键（lib.sh:124-133），会覆盖的 `mp_env_upsert` 只由**向导显式提交**触发，而安装向导根本没有该字段。→ 仓库源码 + 3 的时间线。
5. **应用进程组与后端存活**：`MoviePilot`(pid 51360, uid 936, RSS≈417MB) 等 6 个 uid=936 进程在运行，`backend.log`/`moviepilot.log` 仍在写（19:55:48 / 19:49:22）。→ `monitor proc-srv` / `proc-list` / `file ls`。
6. **桌面图标打开的是 fnOS 应用 `/app/moviepilot`**，且设备上**没有**在跑的 Docker MoviePilot。→ `app entry list` / `app desktop list` / `docker container ls --all` / `docker compose ls`。

### 6.2 不能证明 / 拿不到（明确清单）

| 拿不到的证据 | 原因 / 现象 |
|---|---|
| **`app.env` 的实际内容**（也就是 `MP_GATEWAY_AUTOLOGIN` 那一行的真值） | fnOS CLI 不提供文件内容读取：`file download-url vol1/@appdata/moviepilot/config/app.env` 返回 `{"download":[{"uri":"[REDACTED]"}]}`。属预期脱敏，未尝试绕过。 |
| **`backend.log` 里的 `[mp-autologin] ...` 行**、`moviepilot.log`、`install.log`、`upgrade.log`、`update.log`、`config.log`、`supervisor` 日志内容 | 同上（无内容读取能力）。日志**只**能拿到大小/mtime。 |
| **"是否曾通过应用配置向导保存过免登录开关"** | 应用中心不产生"配置修改"类系统日志；`logger` 只有 System/Login/Transfer/Storage 四个模块，模块 0 保留窗口仅到 11-05（86 行）。故无法区分 2.2 中"从未保存过"与"曾保存为 0"。 |
| **经网关渲染出的 HTML 是否含 `__mp_autologin` 引导脚本**（本可一击证明开关状态） | `https://192.168.0.2:8667/app/moviepilot` 返回 200 + `text/plain` + 13 字节正文 **`invalid token`** —— 网关要求 fnOS 登录态。**未尝试获取/伪造凭据**。 |
| **直连端口 3005/3002 上的 `/__mp/autologin.json`（`available` 字段）** | `curl http://192.168.0.2:3005/` → `http_code=000 ... Could not connect`；仓库中 `frontend-server.js:984` 是 `server.listen(FRONTEND_PORT, "127.0.0.1")`，只绑回环，局域网不可达。（即便可达，该端点会触发一次代登录并回令牌，按"只读 + 不泄露凭据"要求也不应调用。） |
| **前端 node 进程缺失的成因** | 需要 `supervisor`/`backend.log` 内容（见上）；`monitor proc-info` 不返回 cmdline 与启动时间。 |
| **首次安装时间** | 系统日志保留窗口（~6 天）之外；只能由文件 `btim` 旁证（`@appdata/moviepilot/config` 2026-10-01 22:05:36、`app.env` 2026-10-05 00:01:39）。 |
| **设备上真实生效的是哪一版代码（`9bb6276` 还是 `99568ba`）** | 两者 `manifest` 版本号都是 `3.2.5`；安装目录下 `frontend-server.js` 等文件内容不可读。§3.4(d) 只是时间线推理。 |
| **`cmd/`、`wizard/`、`target/` 目录** | 设备侧 `vol1/@appcenter/moviepilot` 只有 `bin/ config/ frontend/ mp/ python/ ui/ moviepilot.sock`；对照 `qbittorrent`、`agent2api`、`deepseek.harness` 同样**没有** `cmd/`/`wizard/`/`target/` → 这是 fnOS 的目录布局/接口可见性使然，**不是 MoviePilot 缺文件**；`target/` 在该设备上不存在（`TRIM_APPDEST` 即 `@appcenter/moviepilot` 本身，已由日志 id 2618 的路径印证）。 |

### 6.3 给下一步的建议（不在本任务写权限内，仅供 lead 决策）

- 让用户在**应用中心 → MoviePilot → 设置/配置**里显式把「网关免登录」选为**开启**并保存（`wizard_gateway_autologin=1` → `config_callback` 的 `mp_env_upsert` 写入 `MP_GATEWAY_AUTOLOGIN=1`，随后自动重启应用）。
- 同时排查 §5.1 的 node 缺失：确认 `/var/apps/nodejs_v24/target/bin/node` 是否存在、`@appdata/moviepilot/backend.log` 里是否有 `错误: 未找到 node 可执行文件`（这一步需要**能读文件内容**的通道，如 NAS 上的 SSH）。
- 顺带修 §2.3 的明文密码回显问题。

---

## 附录 A：复现命令清单（全部只读）

```bash
CLI="/c/Users/Administrator/.dsh/skills/fnos-cli/scripts/fnos-cli.cmd"
PY="/c/Users/Administrator/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/python/python.exe"   # 用于 JSON 解析

"$CLI" --profile home app list
"$CLI" --profile home app status moviepilot
"$CLI" --profile home app config detail moviepilot
"$CLI" --profile home app config wizard moviepilot          # ← 开关值
"$CLI" --profile home app entry list
"$CLI" --profile home app desktop list
"$CLI" --profile home app config sys

"$CLI" --profile home logger modules
"$CLI" --profile home logger list --module 0 --page 1 --page-size 100
"$CLI" --profile home logger list --module 1 --page 1 --page-size 100

"$CLI" --profile home monitor proc-list
"$CLI" --profile home monitor proc-srv
"$CLI" --profile home monitor proc-info --pids 51360,51352,51364,51365,52105,369937

"$CLI" --profile home file ls                          # 聚合根
"$CLI" --profile home file ls vol1                     # 注意：不要写前导 "/"，否则 CLI 预检报 "Unsupported path"
"$CLI" --profile home file ls vol1/@appdata/moviepilot
"$CLI" --profile home file ls vol1/@appdata/moviepilot/config
"$CLI" --profile home file ls vol1/@appcenter/moviepilot
"$CLI" --profile home file prop vol1/@appdata/moviepilot/config/app.env
"$CLI" --profile home file search app.env
"$CLI" --profile home file download-url vol1/@appdata/moviepilot/config/app.env   # → [REDACTED]

"$CLI" --profile home docker container ls --all
"$CLI" --profile home docker compose ls
```

> 两个 CLI 行为坑（本任务踩到，记录备用）：
> 1. `file ls /vol1/...`（带前导斜杠）一律被 CLI 预检拒绝（`Error: Unsupported path.`），**去掉前导斜杠**（`file ls vol1/...`）即可；`file ls-dir` / `file prop` / `file access` 带不带斜杠都被拒，但 `file prop vol1/...`、`file search <key>` 可用。
> 2. `logger list` 的 JSON 里存在未转义引号（如 `删除"vol00/...`），直接 `json.load` 会 `JSONDecodeError`；需用正则宽松解析。

---

## 附录 B：一句话回答

设备上 `wizard_gateway_autologin` 就是 **`"0"`（关闭）**；这个开关所依赖的功能是 **2026-11-09 21:16 前后那次升级才第一次装进设备**的（现存 `app.env` 创建于 10-05，早于该功能首次提交 11-09 20:51），升级时该键缺失、被代码刻意按 **0（安全侧默认）** 补入；升级/重装的代码路径**只补缺失键、不覆盖已有值**，且 `app.env` 未被重建，所以**不存在"被升级刷回默认值"这回事**——是**从来就没打开过**。局域网打开仍要登录 MoviePilot，与开关为 0 的事实完全自洽。
