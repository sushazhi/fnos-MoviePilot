#!/usr/bin/env node
/**
 * MoviePilot fnOS 前端服务
 * =========================
 * 作用：
 *   - 提供 MoviePilot 前端静态资源（dist 目录）
 *   - 将 /api 与 /cookiecloud 反向代理到 Python 后端
 *   - SPA 回退到 index.html
 *
 * 环境变量：
 *   MP_FRONTEND_DIR  前端 dist 目录（必填）
 *   MP_BACKEND_HOST  后端地址，默认 127.0.0.1
 *   MP_BACKEND_PORT  后端端口，默认 3001
 *   MP_FRONTEND_PORT 本服务监听端口，默认 3000
 */
"use strict";

const path = require("path");
const fs = require("fs");
const http = require("http");
const net = require("net");

const FRONTEND_DIR = process.env.MP_FRONTEND_DIR;
const BACKEND_HOST = process.env.MP_BACKEND_HOST || "127.0.0.1";
const BACKEND_PORT = parseInt(process.env.MP_BACKEND_PORT || "3001", 10);
const FRONTEND_PORT = parseInt(process.env.MP_FRONTEND_PORT || "3000", 10);
// 后端启动等待上限：uvicorn 要跑完 lifespan（建库/迁移、插件安装、调度器启动）
// 才 bind 端口，首次启动可能耗时数分钟，必须留足预算。
const BACKEND_WAIT_TIMEOUT = Math.max(
  0,
  parseInt(process.env.MP_BACKEND_WAIT_TIMEOUT || "180000", 10) || 180000
);

if (!FRONTEND_DIR) {
  console.error("[moviepilot-frontend] MP_FRONTEND_DIR is not set");
  process.exit(1);
}

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "application/javascript; charset=utf-8",
  ".mjs": "application/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".gif": "image/gif",
  ".svg": "image/svg+xml",
  ".ico": "image/x-icon",
  ".webp": "image/webp",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
  ".ttf": "font/ttf",
  ".eot": "application/vnd.ms-fontobject",
  ".txt": "text/plain; charset=utf-8",
  ".map": "application/json",
  ".webmanifest": "application/manifest+json",
  ".mp4": "video/mp4",
  ".webm": "video/webm",
};

function contentType(p) {
  return MIME[path.extname(p).toLowerCase()] || "application/octet-stream";
}

// 带这些扩展名的请求必须命中真实文件：找不到就 404。
// 若像 SPA 路由一样回退 index.html，浏览器会把 HTML 当 JS/CSS 解析并抛
// "Unexpected token '<'"，升级后残留的旧 hash chunk 会表现为整页白屏且难以定位。
// 官方 nginx 对 js/css 同样是 try_files $uri =404。
const ASSET_EXT = new Set([
  ".js", ".mjs", ".css", ".map", ".json", ".txt", ".webmanifest",
  ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".webp",
  ".woff", ".woff2", ".ttf", ".eot", ".otf", ".mp4", ".webm",
]);

const IMAGE_EXT = new Set([".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".webp"]);
const FONT_EXT = new Set([".woff", ".woff2", ".ttf", ".eot", ".otf"]);
const SCRIPT_EXT = new Set([".js", ".mjs", ".css", ".map"]);

// 缓存策略对齐官方 docker/nginx.common.conf：
//   /assets/ 与图片、字体 -> 1 年 immutable（文件名带内容哈希）
//   js/css/map            -> 30 天
//   service-worker.js / manifest.webmanifest -> no-cache（否则前端更新后浏览器
//                            可能继续注册旧版本）
//   index.html 与 SPA 回退 -> no-store
function cacheControlFor(pathname) {
  const p = pathname.toLowerCase();
  const base = p.slice(p.lastIndexOf("/") + 1);
  if (base === "service-worker.js" || base === "service.js" ||
      base === "manifest.webmanifest") {
    return "no-cache, must-revalidate";
  }
  if (p.endsWith(".html") || p === "/") {
    return "no-cache, no-store, must-revalidate";
  }
  const ext = path.extname(p);
  if (p.startsWith("/assets/")) return "public, max-age=31536000, immutable";
  if (IMAGE_EXT.has(ext) || FONT_EXT.has(ext)) {
    return "public, max-age=31536000, immutable";
  }
  if (SCRIPT_EXT.has(ext)) return "public, max-age=2592000";
  return "no-cache";
}

// 与官方 nginx 的 rewrite ^.+mock-server/?(?<suffix>.*)$ /$suffix break; 对齐：
// CookieCloud 客户端把服务地址配成含 mock-server 的形式时，只保留其后部分。
const MOCK_MARKER = "mock-server";

function rewriteMockServer(pathname) {
  const idx = pathname.indexOf(MOCK_MARKER);
  // idx === 0 时 nginx 的 `.+` 无法匹配，保持原路径
  if (idx <= 0) return pathname;
  return "/" + pathname.slice(idx + MOCK_MARKER.length).replace(/^\/+/, "");
}

// ---------------------------------------------------------------------------
// 网关免登录（MP_GATEWAY_AUTOLOGIN，默认关闭）
// ---------------------------------------------------------------------------
// 为什么注入点在这台服务里，而不是在 gateway-proxy.py：
//   本服务是**所有 HTML 的唯一产地** —— 统一网关那条路（浏览器 → fnOS 网关 →
//   unix socket → gateway-proxy → 这里）与直连端口那条路（浏览器 → 这里）最终都由
//   这里吐出 index.html；gateway-proxy 只在中转路上做前缀/头部改写。过去把注入
//   放在代理里，走直连端口（飞牛 APP、FN Connect 映射到应用端口这类访问）时
//   页面根本不经过代理，于是"局域网能免登录、外网不能"。
//
// 机制：用 app.env 的 SUPERUSER / SUPERUSER_PASSWORD 代浏览器登录一次
// `/api/v1/login/access-token`，把令牌经 `/__mp/autologin.json` 交给页面头部注入的
// 脚本，由脚本写进 localStorage 的 `auth` / `user` 两个 store（MoviePilot 前端
// pinia 的持久化键）。上游鉴权链路一个字节都没改。
//
// 为什么不用 API_TOKEN：`X-API-KEY` 只对 API 生效，且不会下发 EventSource 接口
// （system/message、system/logging）只认的资源令牌 Cookie，界面会"能进但实时消息空着"。
//
// 安全边界：开启后任何能打开本应用（即已通过飞牛网关登录）的人都会拿到
// MoviePilot 超级管理员身份 —— 这正是"跳过登录"的定义。多用户 NAS、或把面板
// 暴露到公网时不要开。
const AUTOLOGIN_ENDPOINT = "/__mp/autologin.json";
const AUTOLOGIN_JS_MARK = "__mp_autologin";
const RESOURCE_COOKIE_NAME = "MoviePilot";

// app.env：与 Python 侧（cmd/lib.sh 生成的模板）同一份配置，后出现的键覆盖先出现的，
// 与 python-dotenv 的语义一致。
const CONFIG_DIR = process.env.CONFIG_DIR || "";
const APP_ENV_FILE = process.env.MP_APP_ENV ||
  (CONFIG_DIR ? path.join(CONFIG_DIR, "app.env") : "");

function readAppEnv(file) {
  const out = {};
  if (!file) return out;
  let text;
  try {
    text = fs.readFileSync(file, "utf8");
  } catch (e) {
    return out;
  }
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const eq = line.indexOf("=");
    if (eq <= 0) continue;
    const key = line.slice(0, eq).trim();
    let value = line.slice(eq + 1).trim();
    if (value.length >= 2) {
      const first = value[0];
      if ((first === '"' || first === "'") && value[value.length - 1] === first) {
        value = value.slice(1, -1);
      }
    }
    if (key) out[key] = value;
  }
  return out;
}

const APP_ENV = readAppEnv(APP_ENV_FILE);

function envFlag(value, fallback = false) {
  if (value === undefined || value === null || String(value).trim() === "") {
    return fallback;
  }
  return ["1", "true", "yes", "on"].includes(String(value).trim().toLowerCase());
}

// 环境变量优先于 app.env：便于排障时临时覆盖，也让冒烟测试不必造文件。
const AUTOLOGIN_FLAG = envFlag(
  process.env.MP_GATEWAY_AUTOLOGIN !== undefined
    ? process.env.MP_GATEWAY_AUTOLOGIN
    : APP_ENV.MP_GATEWAY_AUTOLOGIN
);
const AUTOLOGIN_USER = String(
  process.env.MP_GATEWAY_AUTOLOGIN_USER || APP_ENV.SUPERUSER || ""
).trim();
const AUTOLOGIN_PASSWORD = String(
  process.env.MP_GATEWAY_AUTOLOGIN_PASSWORD !== undefined
    ? process.env.MP_GATEWAY_AUTOLOGIN_PASSWORD
    : (APP_ENV.SUPERUSER_PASSWORD || "")
);

// 前端 store 的形状跟着前端打包产物走，前端改结构时这里要跟着改；
// 失效表现是"又回到登录页"，不会白屏。
const FRONTEND_PERMISSIONS_DEFAULT = {
  discovery: true,
  search: true,
  subscribe: true,
  manage: true,
  admin: true,
  features: {},
};

function jwtExpMs(token) {
  try {
    const part = String(token).split(".")[1] || "";
    const b64 = part.replace(/-/g, "+").replace(/_/g, "/");
    const pad = b64 + "=".repeat((4 - (b64.length % 4)) % 4);
    const payload = JSON.parse(Buffer.from(pad, "base64").toString("utf8"));
    return Number(payload.exp || 0) * 1000;
  } catch (e) {
    return 0;
  }
}

class AutoLoginSession {
  // 提前 10 分钟续期：避免"用着用着突然跳登录页"
  static REFRESH_MARGIN = 600000;
  static FAIL_BACKOFF = 60000;      // 普通失败 1 分钟内不再重试
  static THROTTLE_BACKOFF = 300000; // 被限流（429）5 分钟
  static TIMEOUT = 10000;
  static WARM_INTERVAL = 2000;
  static WARM_SECONDS = 120;
  // 这些状态代表"现在不行、过会儿可能行"（后端还在冷启动），预热可以继续等；
  // 其余（401 密码不对、429 限流）是确定性失败，必须停手 —— 否则每 2s 撞一次
  // 登录接口，会把账号打进 MoviePilot 的登录限流，连人工登录都被挡在门外。
  static TRANSIENT = new Set([0, 500, 502, 503, 504]);

  constructor(enabled, user, password) {
    this.enabled = Boolean(enabled && user && password);
    this.user = user || "";
    this.password = password || "";
    this.payload = null;
    this.expireAt = 0;
    this.nextTry = 0;
    this.cookie = "";
    this.lastStatus = -1;
    this.inflight = null;
    this.hits = 0;
  }

  login() {
    return new Promise((resolve) => {
      const body = new URLSearchParams({
        username: this.user,
        password: this.password,
      }).toString();
      const payload = Buffer.from(body, "utf8");
      let req;
      try {
        req = http.request({
          hostname: BACKEND_HOST,
          port: BACKEND_PORT,
          path: "/api/v1/login/access-token",
          method: "POST",
          headers: {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "Content-Length": payload.length,
            Connection: "close",
          },
          timeout: AutoLoginSession.TIMEOUT,
        }, (res) => {
          const chunks = [];
          res.on("data", (d) => chunks.push(d));
          res.on("end", () => {
            const raw = Buffer.concat(chunks).toString("utf8");
            let json = null;
            if (res.statusCode === 200) {
              try { json = JSON.parse(raw); } catch (e) { json = null; }
            }
            const cookies = res.headers["set-cookie"] || [];
            const cookie = cookies.find(
              (c) => String(c).trim().startsWith(RESOURCE_COOKIE_NAME + "=")
            ) || "";
            resolve({ json, status: res.statusCode, cookie });
          });
        });
      } catch (err) {
        resolve({ json: null, status: 0, cookie: "" });
        return;
      }
      // 连不上后端（冷启动窗口）时收敛成 status=0，而不是抛异常：
      // 预热要靠这个值区分"该继续等"和"该停手"。
      req.on("error", () => resolve({ json: null, status: 0, cookie: "" }));
      req.on("timeout", () => {
        try { req.destroy(); } catch (e) { /* ignore */ }
        resolve({ json: null, status: 0, cookie: "" });
      });
      req.end(payload);
    });
  }

  buildPayload(data) {
    const token = String(data.access_token || "");
    if (!token) return null;
    const isSuper = Boolean(data.super_user);
    const permissions = Object.assign({}, FRONTEND_PERMISSIONS_DEFAULT);
    const raw = data.permissions;
    if (raw && typeof raw === "object" && !Array.isArray(raw)) {
      for (const key of Object.keys(raw)) {
        if (key === "features" && (!raw.features || typeof raw.features !== "object")) {
          continue;
        }
        permissions[key] = raw[key];
      }
    }
    // 超管在菜单过滤里靠 superUser 短路，这里仍显式打开 manage/admin，
    // 避免个别页面直接读 permissions.manage。
    if (isSuper) {
      permissions.manage = true;
      permissions.admin = true;
    }
    const userId = Number(data.user_id);
    const level = Number(data.level || 1);
    return {
      available: true,
      auth: { token, remember: true, originalPath: null },
      user: {
        superUser: isSuper,
        userID: Number.isFinite(userId) ? userId : -1,
        userName: String(data.user_name || this.user),
        avatar: String(data.avatar || ""),
        level: Number.isFinite(level) ? level : 1,
        permissions,
      },
    };
  }

  async refresh() {
    const result = await this.login();
    this.lastStatus = result.status;
    const built = result.json ? this.buildPayload(result.json) : null;
    if (!built) {
      // 429 = 登录限流；401 = 密码不对 / 需要二次验证（MFA）
      const backoff = result.status === 429
        ? AutoLoginSession.THROTTLE_BACKOFF
        : AutoLoginSession.FAIL_BACKOFF;
      this.nextTry = Date.now() + backoff;
      console.warn(
        `[mp-autologin] 代登录失败（status=${result.status}），${Math.round(backoff / 1000)}s 后重试；` +
        "请检查 app.env 的 SUPERUSER / SUPERUSER_PASSWORD 是否与 MoviePilot 当前密码一致，" +
        "并确认该账号未开启二次验证"
      );
      return null;
    }
    const exp = jwtExpMs(built.auth.token) || (Date.now() + 1800000);
    this.payload = built;
    this.expireAt = exp;
    this.cookie = result.cookie || "";
    this.nextTry = 0;
    console.log(
      `[mp-autologin] 已获取 ${built.user.userName} 的登录令牌（有效至 ` +
      `${new Date(exp).toLocaleString("zh-CN", { hour12: false })}）`
    );
    return built;
  }

  /** 返回 [给浏览器的 JSON, 要转发的 Set-Cookie]；永不抛异常。 */
  async state(force = false) {
    if (!this.enabled) return [{ available: false, reason: "disabled" }, ""];
    const now = Date.now();
    if (this.payload && this.expireAt - now > AutoLoginSession.REFRESH_MARGIN) {
      return [this.payload, this.cookie];
    }
    if (!force && now < this.nextTry) {
      return [{ available: false, reason: "backoff" }, ""];
    }
    if (this.inflight) {
      // 首屏多个标签页/预热撞车：等同一个在飞的登录，不要再打一遍
      try { await this.inflight; } catch (e) { /* ignore */ }
      if (this.payload && this.expireAt > Date.now()) {
        return [this.payload, this.cookie];
      }
      return [{ available: false, reason: "in_progress" }, ""];
    }
    this.inflight = this.refresh();
    try {
      const built = await this.inflight;
      if (!built) return [{ available: false, reason: "login_failed" }, ""];
    } catch (e) {
      console.warn(`[mp-autologin] 登录异常: ${e && e.message}`);
      return [{ available: false, reason: "error" }, ""];
    } finally {
      this.inflight = null;
    }
    return [this.payload, this.cookie];
  }

  /** 启动预热：代理比后端先就绪是常态，别让用户撞上"第一次打开还是登录页"。 */
  async warm() {
    if (!this.enabled) return;
    const rounds = Math.max(1, Math.floor(AutoLoginSession.WARM_SECONDS /
      AutoLoginSession.WARM_INTERVAL));
    for (let i = 0; i < rounds; i += 1) {
      const [payload] = await this.state(true);
      if (payload && payload.available) return;
      if (!AutoLoginSession.TRANSIENT.has(this.lastStatus)) {
        console.warn(`[mp-autologin] 预热放弃（status=${this.lastStatus}），` +
          "后续仅在用户打开应用时按退避重试");
        return;
      }
      await new Promise((r) => setTimeout(r, AutoLoginSession.WARM_INTERVAL));
    }
  }
}

const AUTOLOGIN = new AutoLoginSession(AUTOLOGIN_FLAG, AUTOLOGIN_USER, AUTOLOGIN_PASSWORD);

// 网关前缀白名单：只允许 `/app/<目录名>` 这种形状，避免把任意请求头拼进注入的 JS。
const GATEWAY_PREFIX_RE = /^\/app\/[A-Za-z0-9._-]+$/;

/** 网关前缀由 gateway-proxy 通过请求头带过来；直连端口时没有该头 -> 根路径。 */
function gatewayPrefix(req) {
  const raw = String(req.headers["x-mp-gateway-prefix"] || "").trim().replace(/\/+$/, "");
  return GATEWAY_PREFIX_RE.test(raw) ? raw : "";
}

const AUTOLOGIN_NO_CACHE = "no-cache, no-store, must-revalidate";

/**
 * 页面头部注入的引导脚本。
 *
 * 关键点：
 *   * 已有登录态绝不覆盖 —— 用户手动登录/登出、换账号都不受干扰；
 *   * 凭据现取现用，脚本本身不含令牌与密码；
 *   * 端点地址按"网关前缀 + 根路径"两个候选依次试：走统一网关时是
 *     `/app/<app>/__mp/autologin.json`，直连端口时是 `/__mp/autologin.json`，
 *     这样两条访问路径用同一份注入逻辑；
 *   * 只有真正写进 localStorage 才 reload（写失败就不 reload，避免死循环）；
 *     每个标签页会话最多尝试一次，登出后不会被立刻拽回去。
 */
function buildAutologinScript(base) {
  const candidates = [base + AUTOLOGIN_ENDPOINT];
  if (base) candidates.push(AUTOLOGIN_ENDPOINT);
  return "<script>/* " + AUTOLOGIN_JS_MARK + " */" +
    "(function(){var E=" + JSON.stringify(candidates) + ",K=\"mp_autologin_tried\";" +
    "function has(){try{var s=localStorage.getItem(\"auth\");if(!s){return false;}" +
    "var o=JSON.parse(s);return !!(o&&o.token);}catch(e){return false;}}" +
    "if(has()){return;}" +
    "try{if(sessionStorage.getItem(K)===\"1\"){return;}sessionStorage.setItem(K,\"1\");}catch(e){}" +
    "function apply(d){if(!d||d.available!==true||!d.auth||!d.auth.token){return;}" +
    "try{localStorage.setItem(\"auth\",JSON.stringify(d.auth));" +
    "if(d.user){localStorage.setItem(\"user\",JSON.stringify(d.user));}}catch(e){return;}" +
    "try{location.reload();}catch(e){}}" +
    "var i=0;" +
    "function next(){if(i>=E.length){return;}var u=E[i++];" +
    "try{var x=new XMLHttpRequest();" +
    "x.open(\"GET\",u+(u.indexOf(\"?\")<0?\"?\":\"&\")+\"t=\"+Date.now(),true);" +
    "x.withCredentials=true;x.timeout=15000;" +
    "x.onload=function(){if(x.status!==200){next();return;}" +
    "var d=null;try{d=JSON.parse(x.responseText);}catch(e){d=null;}" +
    // 服务端明确说"不可用"（available=false）时不再换地址试，避免无谓请求
    "if(d&&d.available===true){apply(d);}};" +
    "x.onerror=function(){next();};x.ontimeout=function(){next();};x.send();" +
    "}catch(e){next();}}" +
    "next();})();</script>";
}

/**
 * 把引导脚本插进 HTML。插入点依次尝试 `</head>`、`<head>` 之后、`</body>`；
 * 都没有就原样返回（宁可没有免登录，也不能往文档最前面塞脚本 —— 前面有
 * doctype，脚本跑到 doctype 之前会把页面打成 quirks 模式）。
 */
function injectAutologin(html, base) {
  if (!AUTOLOGIN.enabled) return html;
  const tag = buildAutologinScript(base);
  const lower = html.toLowerCase();
  const headEnd = lower.lastIndexOf("</head>");
  if (headEnd >= 0) return html.slice(0, headEnd) + tag + html.slice(headEnd);
  const headOpen = lower.indexOf("<head");
  if (headOpen >= 0) {
    const gt = html.indexOf(">", headOpen);
    if (gt >= 0) return html.slice(0, gt + 1) + tag + html.slice(gt + 1);
  }
  const bodyEnd = lower.lastIndexOf("</body>");
  if (bodyEnd >= 0) return html.slice(0, bodyEnd) + tag + html.slice(bodyEnd);
  return html;
}

/** 应答 `/__mp/autologin.json`：只回令牌、身份、资源 Cookie，不含密码与后端细节。 */
async function handleAutologin(req, res) {
  if (req.method !== "GET" && req.method !== "HEAD") {
    res.writeHead(405, { Allow: "GET, HEAD", "Content-Length": 0 });
    res.end();
    return;
  }
  let payload = { available: false, reason: "error" };
  let cookie = "";
  try {
    [payload, cookie] = await AUTOLOGIN.state();
  } catch (err) {
    console.warn(`[mp-autologin] 生成登录状态异常: ${err && err.message}`);
  }
  const body = Buffer.from(JSON.stringify(payload), "utf8");
  const headers = {
    "Content-Type": "application/json; charset=utf-8",
    // 令牌绝不能被缓存：Service Worker / 磁盘缓存拿到旧令牌就等于登录失效
    "Cache-Control": "no-store, no-cache, must-revalidate",
    Pragma: "no-cache",
    Expires: "0",
    "Content-Length": body.length,
  };
  if (cookie) headers["Set-Cookie"] = cookie;
  res.writeHead(200, headers);
  if (req.method !== "HEAD") res.end(body); else res.end();
  console.log(`[mp-autologin] 端点应答 available=${payload.available === true}` +
    ` reason=${payload.reason || "-"}`);
}

/**
 * 发 index.html（含 SPA 回退）：必须整份读出再注入。
 * 流式 pipe 无法在 `</head>` 前插内容；index.html 只有几十 KB，缓冲代价可忽略。
 */
function sendIndexHtml(req, res, indexFile) {
  let html;
  try {
    html = fs.readFileSync(indexFile, "utf8");
  } catch (e) {
    res.writeHead(404, { "Cache-Control": "no-cache" });
    res.end("Not Found");
    return;
  }
  const body = Buffer.from(injectAutologin(html, gatewayPrefix(req)), "utf8");
  res.writeHead(200, {
    "Content-Type": "text/html; charset=utf-8",
    "Cache-Control": AUTOLOGIN_NO_CACHE,
    "Content-Length": body.length,
  });
  res.end(body);
}

// 幂等方法：连接级失败（多半是复用了被对端静默关闭的 keep-alive 连接）时
// 自动重试一次，避免前端健康检查（如 system/ping）偶发失败误报"正在重新连接"。
const IDEMPOTENT_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

// ---------------------------------------------------------------------------
// 后端就绪门（启动预热）
// ---------------------------------------------------------------------------
// uvicorn 先跑完 lifespan 再 bind 监听端口，而建库/迁移、插件安装、调度器启动
// 都在 lifespan 里。因此"后端进程已存在"与"后端端口可连接"之间可能隔着几十秒
// 到几分钟。这段窗口里前端静态页本来是能正常打开的，但对 /api 的转发会立刻
// ECONNREFUSED —— 早期实现直接回 502，用户在浏览器里看到的是 "Bad Gateway"
// 而不是 MoviePilot 的加载界面，还会在日志里刷出一串 502。
//
// 改为：转发前先探测后端端口，未就绪就把请求挂起（此时还没消费请求体，
// 可以安全地原样重放），等后端可连接再真正发起；超过 MP_BACKEND_WAIT_TIMEOUT
// 才放行一个带 Retry-After 的 503，由前端自行重试。
let backendReady = false;
let backendProbing = false;
let backendWaiters = [];

function probeBackendOnce() {
  return new Promise((resolve) => {
    let settled = false;
    let sock = null;
    const finish = (ok) => {
      if (settled) return;
      settled = true;
      try { if (sock) sock.destroy(); } catch (e) { /* ignore */ }
      resolve(ok);
    };
    try {
      sock = net.createConnection({ host: BACKEND_HOST, port: BACKEND_PORT });
    } catch (e) {
      finish(false);
      return;
    }
    sock.setTimeout(5000);
    sock.once("connect", () => finish(true));
    sock.once("error", () => finish(false));
    sock.once("timeout", () => finish(false));
  });
}

// 后端进程在运行但连不上：多半是刚被重启/刚崩溃，回退到等待模式。
function markBackendDown(reason) {
  if (backendReady) {
    console.error("[moviepilot-frontend] 后端连接中断，进入等待模式: " + reason);
  }
  backendReady = false;
}

// 所有等待者共用一个探测循环，避免 N 个并发请求各起一个定时器把后端打爆。
function ensureBackend(cb) {
  if (backendReady) {
    cb(true);
    return;
  }
  backendWaiters.push(cb);
  if (backendProbing) return;
  backendProbing = true;

  const startedAt = Date.now();
  const deadline = startedAt + BACKEND_WAIT_TIMEOUT;
  let hinted = false;
  console.error(
    "[moviepilot-frontend] 后端 " + BACKEND_HOST + ":" + BACKEND_PORT +
    " 尚未就绪，最多等待 " + Math.round(BACKEND_WAIT_TIMEOUT / 1000) + "s"
  );

  (function tick() {
    probeBackendOnce().then((ok) => {
      if (ok) {
        backendReady = true;
      }
      const elapsed = Date.now() - startedAt;
      if (backendReady) {
        console.error(
          "[moviepilot-frontend] 后端已就绪（等待 " + (elapsed / 1000).toFixed(1) + "s）"
        );
      } else if (Date.now() >= deadline) {
        console.error("[moviepilot-frontend] 等待后端就绪超时（" +
          Math.round(BACKEND_WAIT_TIMEOUT / 1000) + "s）");
      } else {
        if (!hinted && elapsed > 15000) {
          hinted = true;
          console.error(
            "[moviepilot-frontend] 后端仍在启动中（uvicorn 需先完成 lifespan 才监听端口）"
          );
        }
        setTimeout(tick, 1000);
        return;
      }
      backendProbing = false;
      const waiters = backendWaiters;
      backendWaiters = [];
      for (let i = 0; i < waiters.length; i++) {
        try { waiters[i](backendReady); } catch (e) { /* ignore */ }
      }
    });
  })();
}

function writeBackendUnavailable(res) {
  res.writeHead(503, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
    "Retry-After": "10",
  });
  res.end(JSON.stringify({ detail: "后端服务启动中，请稍后重试" }));
}

function proxyRequest(req, res, pathname, retried) {
  // 与官方 nginx 对齐的 mock-server 路径重写（CookieCloud 客户端场景）
  pathname = rewriteMockServer(pathname);

  // 构造安全的后端转发路径：只对 pathname 编码，避免 ERR_UNESCAPED_CHARACTERS。
  // query string 由浏览器已正确 URL 编码，必须原样透传，绝不能再次 encodeURI——
  // 否则 `server=%E9%A3%9E...` 里的 `%` 会被二次编码成 `%25`，后端解码后得到
  // 字面字符串 `%E9%A3%9E...`，导致按 server 名匹配媒体服务器配置失败、
  // 媒体库数据加载失败（如 mediacenter/library?server=飞牛影视）。
  function encodeSafePath(p) {
    let s;
    try {
      s = encodeURI(p);
    } catch (e) {
      s = "/";
    }
    return s;
  }

  // 从 req.url 提取 query string（如 "?a=1&b=2"，无则空串）。
  // 注意：不能使用 req.url.search —— req.url 是字符串，.search 是 String.prototype 的原生方法，
  // 会得到 "function search() { [native code] }"，拼接出畸形路径导致后端 404。
  let queryStr = "";
  try {
    const rawUrl = req.url || "";
    const qIdx = rawUrl.indexOf("?");
    if (qIdx >= 0) queryStr = rawUrl.slice(qIdx);
  } catch (e) {
    queryStr = "";
  }

  let fullPath;
  try {
    // 只对 pathname 做安全编码，query 已由浏览器编码、原样透传，避免二次编码
    fullPath = encodeSafePath(pathname) + queryStr;
  } catch (e) {
    fullPath = "/";
  }
  // 空路径兜底
  if (!fullPath || fullPath === "") fullPath = "/";

  const options = {
    hostname: BACKEND_HOST,
    port: BACKEND_PORT,
    path: fullPath,
    method: req.method,
    headers: Object.assign({}, req.headers, {
      host: `${BACKEND_HOST}:${BACKEND_PORT}`,
    }),
  };

  let proxy;
  try {
    proxy = http.request(options, (pRes) => {
      const headers = Object.assign({}, pRes.headers);
      res.writeHead(pRes.statusCode, headers);
      pRes.pipe(res);
    });
  } catch (err) {
    console.error("[moviepilot-frontend] proxy create error:", err.message);
    if (!res.headersSent) {
      res.writeHead(502, { "Content-Type": "text/plain; charset=utf-8" });
      res.end("Bad Gateway");
    }
    return;
  }

  proxy.on("error", (err) => {
    console.error("[moviepilot-frontend] proxy error:", err.message);
    if (res.headersSent) {
      try { res.end(); } catch (e) {}
      return;
    }
    try { proxy.destroy(); } catch (e) {}

    // ECONNREFUSED 说明后端还没监听（启动窗口）或刚被重启，不能当普通抖动处理
    // ——必须回退到等待模式，否则下一秒进来的每个请求都会再吃一次失败。
    const refused = err.code === "ECONNREFUSED";
    if (refused) markBackendDown(err.code);

    // 幂等请求可以安全重放（ECONNRESET/EPIPE 等多半是 keep-alive 连接被对端
    // 静默关闭）；非幂等请求体已经发出，重放有副作用，只能报错。
    if (!retried && IDEMPOTENT_METHODS.has(req.method)) {
      console.error("[moviepilot-frontend] retrying once:", req.method, pathname);
      if (refused) {
        ensureBackend((ok) => {
          if (ok) proxyRequest(req, res, pathname, true);
          else writeBackendUnavailable(res);
        });
      } else {
        proxyRequest(req, res, pathname, true);
      }
      return;
    }
    if (refused) {
      writeBackendUnavailable(res);
      return;
    }
    res.writeHead(502, { "Content-Type": "text/plain; charset=utf-8" });
    res.end("Bad Gateway: backend not reachable");
  });

  // 防止 req 读取出错导致崩溃
  req.on("error", (err) => {
    console.error("[moviepilot-frontend] request error:", err.message);
    try { proxy.destroy(); } catch (e) {}
  });

  try {
    req.pipe(proxy);
  } catch (e) {
    console.error("[moviepilot-frontend] pipe error:", e.message);
    if (!res.headersSent) {
      res.writeHead(502);
      res.end();
    }
  }
}

function serveStatic(req, res, pathname) {
  // 归一化，防止路径穿越
  let rel;
  try {
    rel = decodeURIComponent(pathname);
  } catch (e) {
    rel = pathname;
  }
  if (rel.indexOf("\0") !== -1) {
    res.writeHead(400);
    res.end();
    return;
  }
  // 必须带 path.sep 校验前缀：只比到 FRONTEND_DIR 字符串前缀的话，
  // "../frontend-evil/x" 这类兄弟前缀目录能通过校验造成路径穿越。
  // FRONTEND_DIR 本身（根路径 "/"）也放行，交给后面的 SPA 回退。
  const root = path.normalize(FRONTEND_DIR + path.sep);
  let filePath = path.normalize(path.join(FRONTEND_DIR, rel));
  if (filePath !== path.normalize(FRONTEND_DIR) && !filePath.startsWith(root)) {
    res.writeHead(403);
    res.end("Forbidden");
    return;
  }

  if (!fs.existsSync(filePath) || !fs.statSync(filePath).isFile()) {
    // 静态资源缺失必须 404，不能回退 index.html（见 ASSET_EXT 注释）
    if (ASSET_EXT.has(path.extname(filePath).toLowerCase())) {
      res.writeHead(404, {
        "Content-Type": "text/plain; charset=utf-8",
        "Cache-Control": "no-cache",
      });
      res.end("Not Found");
      return;
    }
    // SPA 回退：非文件请求全部返回 index.html
    const indexFile = path.join(FRONTEND_DIR, "index.html");
    if (fs.existsSync(indexFile)) {
      sendIndexHtml(req, res, indexFile);
      return;
    }
    res.writeHead(404, { "Cache-Control": "no-cache" });
    res.end("Not Found");
    return;
  }

  // 真实存在的 index.html 与 SPA 回退走同一条注入路径：网关前缀下的页面
  // 与直连端口的页面必须是同一份 HTML，否则"局域网能免登录、外网不能"。
  const ext = path.extname(filePath).toLowerCase();
  if (ext === ".html" && path.basename(filePath).toLowerCase() === "index.html") {
    sendIndexHtml(req, res, filePath);
    return;
  }

  res.writeHead(200, {
    "Content-Type": contentType(filePath),
    "Cache-Control": cacheControlFor(rel),
  });
  fs.createReadStream(filePath).pipe(res);
}

// ---------------------------------------------------------------------------
// WebSocket 升级转发
// ---------------------------------------------------------------------------
// Node 的 http.Server 在没有 'upgrade' 监听时，会把带 Upgrade 的请求当作普通
// 请求处理（走 SPA 回退返回 200 HTML），浏览器握手必然失败。网关代理
// (gateway-proxy.py) 有完整的 WS 隧道，若这里不转发，整条 WS 链路就是断的。
// 对应官方 nginx 的 proxy_set_header Upgrade $http_upgrade。
function isProxyPath(pathname) {
  return pathname === "/api" || pathname.startsWith("/api/") ||
    pathname === "/cookiecloud" || pathname.startsWith("/cookiecloud/");
}

function writeRawResponse(socket, res) {
  const lines = [`HTTP/1.1 ${res.statusCode} ${res.statusMessage}`];
  const raw = res.rawHeaders || [];
  for (let i = 0; i + 1 < raw.length; i += 2) {
    lines.push(`${raw[i]}: ${raw[i + 1]}`);
  }
  socket.write(lines.join("\r\n") + "\r\n\r\n");
}

// WebSocket 握手同样要过就绪门：后端启动窗口内握手必然失败，前端会进入
// 无限静默重连。这里与 HTTP 路径共用同一个等待逻辑。
function proxyUpgrade(req, socket, head) {
  const pathname = (req.url || "/").split("?")[0];
  if (!isProxyPath(pathname)) {
    socket.destroy();
    return;
  }
  ensureBackend((ok) => {
    if (!ok) {
      try {
        socket.write(
          "HTTP/1.1 503 Service Unavailable\r\n" +
          "Connection: close\r\nRetry-After: 10\r\nContent-Length: 0\r\n\r\n"
        );
      } catch (e) { /* ignore */ }
      socket.destroy();
      return;
    }
    doProxyUpgrade(req, socket, head);
  });
}

function doProxyUpgrade(req, socket, head) {
  // 原样透传客户端头（含 Sec-WebSocket-* 与 Cookie/Authorization），
  // 仅替换 host；Connection/Upgrade 必须保留，否则后端不会返回 101。
  const headers = Object.assign({}, req.headers, {
    host: `${BACKEND_HOST}:${BACKEND_PORT}`,
  });

  let proxyReq;
  try {
    proxyReq = http.request({
      hostname: BACKEND_HOST,
      port: BACKEND_PORT,
      path: req.url || "/",
      method: req.method || "GET",
      headers,
    });
  } catch (err) {
    console.error("[moviepilot-frontend] upgrade request error:", err.message);
    socket.destroy();
    return;
  }

  proxyReq.on("upgrade", (pRes, pSocket, pHead) => {
    writeRawResponse(socket, pRes);
    // 客户端在握手请求之后立刻发出的字节（罕见）与后端 101 之后带出的字节，
    // 都要按顺序补给对端，否则会丢帧。
    if (head && head.length) pSocket.write(head);
    if (pHead && pHead.length) socket.write(pHead);
    pSocket.pipe(socket);
    socket.pipe(pSocket);
    const cleanup = () => {
      try { pSocket.destroy(); } catch (e) { /* ignore */ }
      try { socket.destroy(); } catch (e) { /* ignore */ }
    };
    pSocket.on("error", cleanup);
    socket.on("error", cleanup);
    pSocket.on("close", cleanup);
    socket.on("close", cleanup);
  });

  // 后端拒绝升级（401/404 等）：原样回传状态与头部后关闭
  proxyReq.on("response", (pRes) => {
    writeRawResponse(socket, pRes);
    pRes.pipe(socket);
  });

  proxyReq.on("error", (err) => {
    console.error("[moviepilot-frontend] upgrade proxy error:", err.message);
    try {
      socket.write("HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nContent-Length: 0\r\n\r\n");
    } catch (e) { /* ignore */ }
    socket.destroy();
  });

  proxyReq.end();
}

const server = http.createServer((req, res) => {
  const pathname = req.url.split("?")[0];

  // 免登录凭据端点：必须是本服务自己应答（不转发后端，后端没有这个路由）。
  // 同时兼容"带网关前缀"的形式：走统一网关时 gateway-proxy 会剥掉前缀再转发，
  // 但直连端口访问 `/app/<app>/__mp/...` 时前缀还在。
  if (AUTOLOGIN.enabled &&
      (pathname === AUTOLOGIN_ENDPOINT || pathname.endsWith(AUTOLOGIN_ENDPOINT))) {
    handleAutologin(req, res);
    return;
  }

  // API 与 CookieCloud 代理到后端（先等后端就绪，见"后端就绪门"注释）
  if (isProxyPath(pathname)) {
    ensureBackend((ok) => {
      // 等待期间用户可能已经刷新或关闭页面：此时响应已销毁，再写会触发
      // ERR_STREAM_DESTROYED，必须直接放弃。
      if (res.writableEnded || res.destroyed) return;
      if (!ok) {
        writeBackendUnavailable(res);
        return;
      }
      proxyRequest(req, res, pathname, false);
    });
    return;
  }

  serveStatic(req, res, pathname);
});

server.on("upgrade", proxyUpgrade);

server.on("error", (err) => {
  console.error("[moviepilot-frontend] server error:", err.message);
  // 端口被占用(EADDRINUSE)时，短暂等待再退出，给残留进程释放端口留出时间，
  // 避免 supervisor 立即拉起重启再次抢端口形成风暴。
  if (err.code === "EADDRINUSE") {
    console.error("[moviepilot-frontend] port already in use, waiting before exit");
    setTimeout(() => process.exit(1), 3000);
  } else {
    process.exit(1);
  }
});

// 拉长 keep-alive 存活时间，与 gateway-proxy.py 连接池(60s)匹配，
// 避免代理池复用的空闲连接被 Node 默认 keepAliveTimeout(5s) 提前关闭，
// 导致 "request failed, retry fresh: ... Remote end closed connection without response"。
server.keepAliveTimeout = 60000;
server.headersTimeout = 61000;
// 就绪门会把请求挂起最长 BACKEND_WAIT_TIMEOUT。Node 18+ 的 requestTimeout
// 默认 300s，与等待时间叠加后可能在等待期间主动回 408 并断开连接，
// 表现为"后端还没起来请求就已经失败"。这里显式放宽到等待预算之上。
server.requestTimeout = BACKEND_WAIT_TIMEOUT + 120000;

server.listen(FRONTEND_PORT, "127.0.0.1", () => {
  console.log(`[moviepilot-frontend] serving ${FRONTEND_DIR} on 127.0.0.1:${FRONTEND_PORT}`);
  console.log(`[moviepilot-frontend] backend at ${BACKEND_HOST}:${BACKEND_PORT}`);
  // 免登录的状态必须在启动日志里留下痕迹：否则"开了开关却仍要登录"时，
  // 排障只能靠猜（凭据缺失 / 密码与库里不一致，两种都在这里一眼看出）。
  if (AUTOLOGIN_FLAG) {
    if (AUTOLOGIN.enabled) {
      console.log(`[mp-autologin] 已启用（账号 ${AUTOLOGIN_USER || "(未配置)"}，` +
        `来源 ${APP_ENV_FILE || "(环境变量)"}）`);
      AUTOLOGIN.warm();
    } else {
      console.warn("[mp-autologin] 开关已打开，但缺少可用凭据" +
        `（SUPERUSER=${JSON.stringify(AUTOLOGIN_USER)}，` +
        `SUPERUSER_PASSWORD ${AUTOLOGIN_PASSWORD ? "已配置" : "为空"}），本次按关闭处理`);
    }
  } else {
    console.log("[mp-autologin] 未启用（MP_GATEWAY_AUTOLOGIN=0）");
  }
});

// 优雅退出
process.on("SIGTERM", () => {
  console.log("[moviepilot-frontend] SIGTERM received, shutting down");
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 3000).unref();
});
process.on("SIGINT", () => {
  process.exit(0);
});
