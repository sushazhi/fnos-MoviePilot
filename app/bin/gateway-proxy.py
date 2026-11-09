#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MoviePilot fnOS 统一网关反向代理
=================================
作用：把 fnOS 网关转发的 Unix Socket 流量，桥接到 MoviePilot 前端服务端口(默认 3000)，
并完成：
  - 剥离网关前缀 /app/moviepilot
  - 注入 JS polyfill（fetch/XHR/WebSocket/EventSource 路径改写 + 反逃生）
  - WebSocket 原始 TCP 双向透传
  - 移除 X-Frame-Options、追加允许 iframe 的 CSP
  - 静态资源 LRU 缓存 + 后端连接池

用法：
    python3 gateway-proxy.py <socket_path> <target_host> <port>

依赖：仅 Python 标准库。
"""
import http.server
import socket
import sys
import os
import signal
import re
import time
import json
import base64
import threading
import gzip
import zlib
import select
import logging
import urllib.parse
from http.client import HTTPConnection
from collections import OrderedDict

# ---------------------------------------------------------------------------
# 配置（与 manifest / app/ui/config 保持一致）
# ---------------------------------------------------------------------------
APPNAME = "moviepilot"
GATEWAY_PREFIX = "/app/moviepilot"

STATIC_EXTENSIONS = frozenset({
    'js', 'css', 'png', 'jpg', 'jpeg', 'gif', 'svg', 'ico', 'woff', 'woff2',
    'ttf', 'eot', 'webp', 'mp4', 'webm',
})

# 稳定文件名的 PWA 资源绝不能进 LRU 缓存。
# Service Worker 会按自身 precache 清单继续拉取**旧 chunk**，浏览器刷新也拿不到
# 新前端：表现为升级后界面报「服务器返回了无效响应」/各页面数据加载失败，且
# "刷新后重试"永远无效（旧 SW 一直顶在前面）。上游 nginx 对它们是
# `Cache-Control: no-cache, must-revalidate`（前端服务这一层也已对齐），
# 而本代理的 LRU 完全不看 Cache-Control，且文件名不带内容哈希 ——
# 一旦命中就直到代理重启都不会失效。
_NO_CACHE_BASENAMES = frozenset({
    'service-worker.js', 'service.js', 'manifest.webmanifest', 'index.html',
})

# ---------------------------------------------------------------------------
# 网关免登录（可选，默认关闭）
# ---------------------------------------------------------------------------
# 背景：本应用跑在 fnOS 统一网关后面，网关已经校验过 NAS 登录态；但 MoviePilot
# 自己的登录态完全在浏览器里（pinia 的 `auth` / `user` store，persist 到
# localStorage），所以用户点开应用仍然要再登一次。
#
# 开启 MP_GATEWAY_AUTOLOGIN 后，由本代理**代浏览器**登录一次（账号取 app.env 的
# SUPERUSER / SUPERUSER_PASSWORD），把令牌经 `/__mp/autologin.json` 交给页面头部
# 注入的脚本，由脚本写进 localStorage 的两个 store —— 效果是"点开即进"，而
# MoviePilot 自身的鉴权链路（JWT、资源 Cookie、权限校验）一个字节都没改。
#
# 为什么不在前端直接登录、也不用 API_TOKEN：
#   * API_TOKEN 走 `X-API-KEY` 只对 API 生效，且**不会**下发 EventSource 接口
#     （system/message、system/logging）只认的资源令牌 Cookie，界面会"能打开但
#     实时消息/日志空着"；
#   * 前端拿密码自己登 = 明文密码躺进 HTML/JS，随缓存、快照、DevTools 外泄。
#
# 安全边界（务必如实告知用户）：开启后，任何能打开本应用（即已通过 fnOS 网关
# 登录）的人都会拿到 MoviePilot 超级管理员身份 —— 这正是"跳过登录"的定义。
# 多用户 NAS、或把飞牛面板暴露到公网时不要开。
AUTOLOGIN_ENDPOINT = "/__mp/autologin.json"
_AUTOLOGIN_JS_MARK = "__mp_autologin"


def _read_app_env(path):
    """最小 dotenv 解析：KEY=VALUE + `#` 注释 + 可选引号，后出现的键覆盖先出现的。

    与上游 python-dotenv 的"后者生效"语义保持一致；解析失败一律当作没有配置，
    绝不让一个坏行把代理整体拖垮（代理挂了 = 应用打不开）。
    """
    data = {}
    if not path:
        return data
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if not key:
                    continue
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                data[key] = value
    except OSError:
        return {}
    return data


def _env_bool(value, default=False):
    """把 app.env / 环境变量里的开关值解析成 bool（"0"/"false"/"off" 为假）。"""
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


_ENV_FILE = os.environ.get("MP_APP_ENV") or (
    os.path.join(os.environ["CONFIG_DIR"], "app.env")
    if os.environ.get("CONFIG_DIR") else "")
_APP_ENV = _read_app_env(_ENV_FILE)

# 环境变量优先于 app.env：便于排障时临时覆盖，也让冒烟测试不必造文件。
AUTOLOGIN_ENABLED = _env_bool(
    os.environ.get("MP_GATEWAY_AUTOLOGIN") or _APP_ENV.get("MP_GATEWAY_AUTOLOGIN")
)
AUTOLOGIN_USER = (
    os.environ.get("MP_GATEWAY_AUTOLOGIN_USER") or _APP_ENV.get("SUPERUSER") or ""
).strip()
AUTOLOGIN_PASSWORD = (
    os.environ.get("MP_GATEWAY_AUTOLOGIN_PASSWORD")
    or _APP_ENV.get("SUPERUSER_PASSWORD")
    or ""
)


def _build_autologin_js():
    """页面头部注入的引导脚本（静态内容，因此可被 Service Worker 缓存复用）。

    设计要点：
      * **已有登录态绝不覆盖** —— `auth` store 里已有 token 就什么都不做，
        用户手动登录/登出、或用别的账号登录都不会被抢；
      * 凭据走 XHR 现取现用，脚本本身不含令牌与密码，所以即使 index.html 被
        SW 长期缓存、或令牌过期，重新加载也会拿到新令牌；
      * 只有真正写到 localStorage 才 reload（写失败就不 reload，避免死循环）；
        每次标签页会话最多尝试一次（sessionStorage 标记），登出后不会被立刻
        拽回去；
      * 用 XHR 而非 fetch：不依赖 polyfill 的改写顺序（URL 本身已带前缀）。
    """
    P = GATEWAY_PREFIX
    E = AUTOLOGIN_ENDPOINT
    return (
        '<script>'
        '/* %s */'
        '(function(){'
        'var P="%s",E="%s",K="mp_autologin_tried";'
        'function has(){'
        'try{var s=localStorage.getItem("auth");if(!s){return false;}'
        'var o=JSON.parse(s);return !!(o&&o.token);}catch(e){return false;}}'
        'if(has()){return;}'
        'try{if(sessionStorage.getItem(K)==="1"){return;}'
        'sessionStorage.setItem(K,"1");}catch(e){}'
        'function apply(d){'
        'if(!d||d.available!==true||!d.auth||!d.auth.token){return;}'
        'try{'
        'localStorage.setItem("auth",JSON.stringify(d.auth));'
        'if(d.user){localStorage.setItem("user",JSON.stringify(d.user));}'
        '}catch(e){return;}'
        'try{location.reload();}catch(e){}'
        '}'
        'try{'
        'var x=new XMLHttpRequest();'
        'x.open("GET",P+E+"?t="+Date.now(),true);'
        'x.withCredentials=true;'
        'x.onload=function(){'
        'if(x.status!==200){return;}'
        'try{apply(JSON.parse(x.responseText));}catch(e){}'
        '};'
        'x.onerror=function(){};'
        'x.send();'
        '}catch(e){}'
        '})();'
        '</script>' % (_AUTOLOGIN_JS_MARK, P, E)
    )


_AUTOLOGIN_READY = bool(AUTOLOGIN_ENABLED and AUTOLOGIN_USER and AUTOLOGIN_PASSWORD)
# 缺凭据时**不注入**脚本：注入了也只会换来一次 404，白白多一个请求。
_AUTOLOGIN_JS_BYTES = _build_autologin_js().encode() if _AUTOLOGIN_READY else b""

# 认证头改名（见 _build_polyfill 内的详细说明）。
#   AUTH_HEADER     —— 后端（MoviePilot）认的请求头
#   AUTH_HEADER_ALT —— 浏览器里实际发送的头；fnOS 网关只校验前者，校验不过会
#                      直接返回 200 text/plain "invalid token"，请求到不了应用
AUTH_HEADER = "Authorization"
AUTH_HEADER_ALT = "X-MP-Auth"

# 懒加载 queue 模块（保持启动快）
_queue = None
def _get_queue():
    global _queue
    if _queue is None:
        import queue
        _queue = queue
    return _queue

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stderr,
)

_RE_HTML_ATTR = re.compile(rb'(src|href|action)=([\'"])/(?!/?(?:app|cgi)/)')
_RE_REFERER = re.compile(r'^https?://[^/]+')

# ---------------------------------------------------------------------------
# JS polyfill 模板（注入到 HTML <head>）
# ---------------------------------------------------------------------------
def _build_polyfill():
    P = GATEWAY_PREFIX
    return (
        '<script>'
        '(function(){'
        'var P="%s";'
        'var A="%s",B="%s";'
        # fnOS 网关把 Authorization 当成**它自己的**令牌来校验：MoviePilot 的 JWT
        # 校验不过，网关直接返回 200 text/plain "invalid token"（13 字节），请求
        # 根本到不了应用。前端因此拿到非 JSON，表现为登录之后所有页面报
        # 「服务器返回了无效响应」、仪表盘与关于页数据全空、站点认证 No data
        # available。自定义头不会被网关校验，所以这里把该头改名为 X-MP-Auth，
        # 由 gateway-proxy 转发时再翻译回 Authorization 交给后端。
        'function _isA(n){return String(n).toLowerCase()==="authorization";}'
        'function _rh(h){'
        'if(!h){return h;}'
        'var o={},i,k;'
        'if(typeof h.forEach==="function"&&typeof h.get==="function"){'
        'h.forEach(function(v,n){o[_isA(n)?B:n]=v;});'
        'return o;}'
        'if(Object.prototype.toString.call(h)==="[object Array]"){'
        'for(i=0;i<h.length;i++){o[_isA(h[i][0])?B:h[i][0]]=h[i][1];}'
        'return o;}'
        'for(k in h){if(Object.prototype.hasOwnProperty.call(h,k)){o[_isA(k)?B:k]=h[k];}}'
        'return o;}'
        'var _f=window.fetch;'
        'window.fetch=function(u,o){'
        'if(typeof u==="string"&&u.charAt(0)==="/"&&!u.startsWith(P)){u=P+u;}'
        'if(o&&o.headers){o=Object.assign({},o);o.headers=_rh(o.headers);}'
        'else if(u&&typeof u==="object"&&u.headers&&window.Headers&&window.Request){'
        'try{'
        'var hh=new Headers(u.headers);'
        'if(hh.has(A)){hh.set(B,hh.get(A));hh.delete(A);}'
        'u=new Request(u,{headers:hh});'
        '}catch(e){}'
        '}'
        'return _f.call(this,u,o);};'
        'var _o=XMLHttpRequest.prototype.open;'
        'XMLHttpRequest.prototype.open=function(m,u,s){'
        'if(typeof u==="string"&&u.charAt(0)==="/"&&!u.startsWith(P)){arguments[1]=P+u;}'
        'return _o.apply(this,arguments);};'
        'var _srh=XMLHttpRequest.prototype.setRequestHeader;'
        'if(_srh){'
        'XMLHttpRequest.prototype.setRequestHeader=function(n,v){'
        'if(_isA(n)){n=B;}'
        'return _srh.call(this,n,v);};'
        '}'
        # EventSource：MoviePilot 的实时消息流（通知中心、系统日志）用它而非 fetch。
        # 它不走 fetch/XHR，天然绕过上面的改写；一旦前端用绝对路径（"/api/..."）
        # 就会静默打到网关根路径而拿不到数据，且不会像 fetch 那样留下明显的失败。
        # 这里与 fetch/XHR 保持同一套改写规则。
        'var _es=window.EventSource;'
        'if(_es){'
        'window.EventSource=function(u,c){'
        'if(typeof u==="string"&&u.charAt(0)==="/"&&!u.startsWith(P)){u=P+u;}'
        'return c?new _es(u,c):new _es(u);};'
        # 静态常量必须一并复制：MoviePilot 的 SSE 封装里有
        # `readyState===EventSource.CLOSED` 这类比较，丢了会永远判为未关闭。
        'var _esC=["CONNECTING","OPEN","CLOSED"];'
        'for(var _i=0;_i<_esC.length;_i++){'
        'var _k=_esC[_i];'
        'try{window.EventSource[_k]=_es[_k];}catch(e){}'
        '}'
        'window.EventSource.prototype=_es.prototype;}'
        'var _cw=window.WebSocket;'
        'if(_cw){'
        'window.WebSocket=function(u,p){'
        'if(typeof u==="string"&&u.charAt(0)==="/"&&!u.startsWith(P)){'
        'var _proto=location.protocol==="https:"?"wss:":"ws:";'
        'u=_proto+"//"+location.host+P+u;}'
        'return p?new _cw(u,p):new _cw(u);};'
        'window.WebSocket.prototype=_cw.prototype;}'
        'var _ws=window.WebSocket;'
        'if(_ws&&_ws.prototype){'
        'var _oOpen=_ws.prototype.open;'
        'if(!_oOpen){'
        'try{'
        'var _orig=_ws;'
        'Object.defineProperty(window,"WebSocket",{get:function(){return _wrap;}});'
        'function _wrap(u,p){'
        'if(typeof u==="string"&&u.charAt(0)==="/"&&!u.startsWith(P)){'
        'var _proto2=location.protocol==="https:"?"wss:":"ws:";'
        'u=_proto2+"//"+location.host+P+u;}'
        'return new _orig(u,p);}'
        '_wrap.prototype=_orig.prototype;'
        '}catch(e){}}'
        '}'
        '})();'
        '</script>' % (P, AUTH_HEADER, AUTH_HEADER_ALT)
    )

_POLYFILL_BYTES = _build_polyfill().encode()

# ---------------------------------------------------------------------------
# 命令行参数
# ---------------------------------------------------------------------------
if len(sys.argv) < 4:
    logging.error("Usage: gateway-proxy.py <socket_path> <target_host> <port>")
    sys.exit(1)
SOCK_PATH = sys.argv[1]
TARGET_HOST = sys.argv[2]
INITIAL_PORT = int(sys.argv[3])

# ---------------------------------------------------------------------------
# 自动登录会话（代浏览器登录 MoviePilot，缓存令牌）
# ---------------------------------------------------------------------------
# 前端 store 的形状是**跟着前端打包产物走的**，所以这里照抄 MoviePilot 前端的
# pinia 定义（`auth` / `user` 两个 store，persist 键即 store id）：
#   auth: {token, remember, originalPath}
#   user: {superUser, userID, userName, avatar, level, permissions{...}}
# 前端一旦改结构，本文件要跟着改 —— 失效表现是"又回到登录页"，不是白屏。
_FRONTEND_PERMISSIONS_DEFAULT = {
    "discovery": True,
    "search": True,
    "subscribe": True,
    "manage": True,
    "admin": True,
    "features": {},
}

# 只转发 MoviePilot 自己的资源令牌 Cookie，避免把后端其它 Set-Cookie 带出去。
_RESOURCE_COOKIE_NAME = "MoviePilot"


class AutoLoginSession:
    """代浏览器登录 MoviePilot，并把结果整理成前端 store 能直接吃的 JSON。

    * 令牌到期前 REFRESH_MARGIN 秒提前续期，避免"用着用着突然跳登录页"；
    * 登录失败（密码被用户改过、开了二次验证、被登录限流）时按退避重试，
      并把**不含凭据**的原因交给前端脚本 —— 脚本只在 available=true 时才写
      store，所以失败时表现为普通的登录页，而不是白屏或死循环；
    * 全部状态用一把锁保护：ThreadingHTTPServer 下多个请求会并发打进来。
    """

    REFRESH_MARGIN = 600          # 提前 10 分钟续期
    FAIL_BACKOFF = 60             # 普通失败：1 分钟内不再重试
    THROTTLE_BACKOFF = 300        # 被限流（429）：5 分钟
    WARM_INTERVAL = 2             # 启动预热的探测间隔（秒）
    WARM_SECONDS = 120            # 启动预热最多持续多久（后端冷启动可能很慢）
    TIMEOUT = 10
    # 这些状态码代表"现在不行、过会儿可能行"（后端还没起来 / 网关后面重启中），
    # 预热可以继续等；其余（401 密码不对、403、429 限流）是确定性失败，必须停手 ——
    # 否则每 2s 撞一次登录接口，会把账号打进 MoviePilot 的登录限流，连人工登录
    # 都会被挡在门外。
    TRANSIENT_STATUS = frozenset({0, 500, 502, 503, 504})

    def __init__(self, enabled, username, password, host, port):
        self.enabled = bool(enabled and username and password)
        self._username = username or ""
        self._password = password or ""
        self._host = host
        self._port = port
        self._lock = threading.Lock()
        self._payload = None      # 上次成功的 JSON 载荷（缓存）
        self._expire_at = 0.0     # 令牌绝对过期时间（time.time()）
        self._next_try = 0.0      # 失败退避：早于此刻不重试
        self._cookie = ""         # 资源令牌 Set-Cookie（原样转发给浏览器）
        self._reason = ""         # 最近一次失败原因（仅排障日志用，不返回细节）
        self._last_status = -1    # 最近一次登录的 HTTP 状态（0=连不上后端）
        # 登录去重：同一时刻只允许一次 POST，其余请求等这个 Event
        self._login_running = False
        self._login_done = threading.Event()

    # -- 内部 ---------------------------------------------------------------
    @staticmethod
    def _jwt_exp(token):
        """从 JWT 里读 exp（不验签，只用于判断续期时机）。读不到返回 0。"""
        try:
            part = str(token).split(".")[1]
            part += "=" * (-len(part) % 4)
            payload = json.loads(base64.urlsafe_b64decode(part.encode()))
            return float(payload.get("exp") or 0)
        except Exception:
            return 0.0

    def _post_login(self):
        """POST /api/v1/login/access-token，返回 (json_or_None, status, set_cookie)。

        连不上后端时返回 status=0（而不是抛异常）：预热要靠这个值判断"该继续等"
        还是"该停手"，异常会让两种情形混在一起。
        """
        form = urllib.parse.urlencode({
            "username": self._username,
            "password": self._password,
        }).encode()
        conn = HTTPConnection(self._host, self._port, timeout=self.TIMEOUT)
        try:
            conn.request(
                "POST", "/api/v1/login/access-token", form,
                {
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                    "Content-Length": str(len(form)),
                    "Connection": "close",
                },
            )
            resp = conn.getresponse()
            raw = resp.read()
            status = resp.status
            cookie = ""
            try:
                for value in resp.msg.get_all("Set-Cookie") or []:
                    if value.strip().startswith(_RESOURCE_COOKIE_NAME + "="):
                        cookie = value
                        break
            except Exception:
                cookie = ""
        except (OSError, ValueError) as exc:
            logging.debug("网关免登录：连接后端失败 %s", exc)
            return None, 0, ""
        finally:
            try:
                conn.close()
            except Exception:
                pass
        if status != 200:
            return None, status, cookie
        try:
            return json.loads(raw.decode("utf-8", errors="replace")), status, cookie
        except Exception:
            return None, status, cookie

    def _build_payload(self, data):
        """"把登录响应整理成前端两个 store 的初始状态。"""
        token = str(data.get("access_token") or "")
        if not token:
            return None
        is_super = bool(data.get("super_user"))
        raw_permissions = data.get("permissions")
        permissions = dict(_FRONTEND_PERMISSIONS_DEFAULT)
        if isinstance(raw_permissions, dict):
            for key, value in raw_permissions.items():
                if key == "features" and not isinstance(value, dict):
                    continue
                permissions[key] = value
        # 超管在菜单过滤里靠 superUser 短路，这里仍把 manage/admin 显式打开，
        # 避免个别页面直接读 permissions.manage。
        if is_super:
            permissions["manage"] = True
            permissions["admin"] = True
        try:
            user_id = int(data.get("user_id"))
        except (TypeError, ValueError):
            user_id = -1
        try:
            level = int(data.get("level") or 1)
        except (TypeError, ValueError):
            level = 1
        return {
            "available": True,
            "auth": {"token": token, "remember": True, "originalPath": None},
            "user": {
                "superUser": is_super,
                "userID": user_id,
                "userName": str(data.get("user_name") or self._username),
                "avatar": str(data.get("avatar") or ""),
                "level": level,
                "permissions": permissions,
            },
        }

    # -- 对外 ---------------------------------------------------------------
    def state(self, force=False):
        """返回 (给浏览器的 JSON, 要转发的 Set-Cookie)。

        只有 enabled 且有可用令牌时 available 才为 true；其余情况一律返回
        available=false，页面上就是普通登录页 —— "跳过登录"失败绝不阻断登录。
        force=True 时忽略失败退避（仅供启动预热使用）。

        登录本身在锁**外**执行：后端冷启动时一次 POST 可能耗到 TIMEOUT，持锁做
        HTTP 会把所有并发端点请求一起堵住（页面头部脚本正好在首屏这时候发）。
        并发靠 `_login_running/_login_done` 去重：同一时刻只有一次登录在飞，
        其它请求等它的结果，不会各自打一遍登录接口。
        """
        if not self.enabled:
            return {"available": False, "reason": "disabled"}, ""
        now = time.time()
        with self._lock:
            if self._payload and self._expire_at - now > self.REFRESH_MARGIN:
                return self._payload, self._cookie
            if not force and now < self._next_try:
                return {"available": False, "reason": "backoff"}, ""
            running = self._login_running
            if not running:
                self._login_running = True

        if running:
            # 别人正在登录（预热或另一个标签页）：等结果，别再发一次
            self._login_done.wait(self.TIMEOUT + 2)
            with self._lock:
                if self._payload and self._expire_at - time.time() > 0:
                    return self._payload, self._cookie
            return {"available": False, "reason": "in_progress"}, ""

        try:
            data, status, cookie = self._post_login()
        finally:
            with self._lock:
                self._login_running = False
                self._login_done.set()

        with self._lock:
            self._last_status = status
            payload = self._build_payload(data) if data else None
            if not payload:
                # 429 = 登录限流；401 = 密码不对 / 需要二次验证（MFA）
                backoff = self.THROTTLE_BACKOFF if status == 429 else self.FAIL_BACKOFF
                self._next_try = time.time() + backoff
                self._reason = "status=%s" % status
                logging.warning(
                    "网关免登录：代登录失败（%s），%ds 后重试；"
                    "请检查 app.env 的 SUPERUSER / SUPERUSER_PASSWORD 是否与"
                    "MoviePilot 当前密码一致，并确认该账号未开启二次验证",
                    self._reason, backoff,
                )
                return {"available": False, "reason": "login_failed"}, ""

            now = time.time()
            expire_at = self._jwt_exp(payload["auth"]["token"])
            if not expire_at:
                # 令牌里有 exp，读不到就保守按 30 分钟算，保证会重新登录
                expire_at = now + 1800
            self._payload = payload
            self._expire_at = expire_at
            self._cookie = cookie
            self._next_try = 0.0
            self._reason = ""
            logging.info("网关免登录：已获取 %s 的登录令牌（有效至 %s）",
                         payload["user"]["userName"],
                         time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expire_at)))
            return self._payload, self._cookie

    def warm(self):
        """启动预热：把令牌先拿到手，浏览器第一次打开时端点就是一次内存读。

        为什么不直接复用 state() 的失败退避：代理比 MoviePilot 后端先就绪是常态
        （后端要跑完迁移才监听端口），若第一次探测失败就进入 60s 退避，用户在这
        一分钟里打开应用仍会看到登录页。这里自己按固定节奏重试，成功后即结束。
        """
        if not self.enabled:
            return
        for _ in range(int(self.WARM_SECONDS / self.WARM_INTERVAL) or 1):
            try:
                payload, _cookie = self.state(force=True)
            except Exception as exc:
                logging.debug("网关免登录：预热异常 %s", exc)
                payload = None
                self._last_status = 0
            if payload and payload.get("available"):
                return
            if self._last_status not in self.TRANSIENT_STATUS:
                # 密码不对 / 被限流 / 返回了非 200 的确定性失败：交给 state() 的
                # 退避处理，这里不再探测（详见 TRANSIENT_STATUS 的注释）
                logging.warning(
                    "网关免登录：预热放弃（status=%s），后续仅在用户打开应用时按"
                    "退避重试", self._last_status)
                return
            time.sleep(self.WARM_INTERVAL)


_AUTOLOGIN = AutoLoginSession(
    AUTOLOGIN_ENABLED, AUTOLOGIN_USER, AUTOLOGIN_PASSWORD, TARGET_HOST, INITIAL_PORT,
)

# ---------------------------------------------------------------------------
# 连接池
# ---------------------------------------------------------------------------
class PooledHTTPConnection(HTTPConnection):
    """带 TCP keepalive 的后端连接。

    对端（Node 前端/后端服务）闲置超时后会静默关闭连接，而本代理池可能
    复用到这种"半死"连接，导致首个请求（如前端 system/ping 健康检查）失败，
    触发 MoviePilot 前端"正在重新连接"提示。TCP keepalive 让 OS 更快发现
    对端异常关闭；配合连接池空闲超时（idle_timeout）主动丢弃长期闲置连接，
    显著降低复用死连接的几率。
    """

    def connect(self):
        super().connect()
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            try:
                # Linux: 空闲 15s 后每 5s 探测一次，3 次无响应判定连接死亡
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 15)
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 5)
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
            except (AttributeError, OSError):
                pass  # 非 Linux 或系统不支持，忽略
        except OSError:
            pass


class ConnectionPool:
    def __init__(self, host, port, maxsize=10, idle_timeout=20, timeout=60):
        self._host, self._port, self._timeout = host, port, timeout
        self._idle_timeout = idle_timeout
        self._pool = _get_queue().Queue(maxsize)
        self._released_at = {}  # conn -> 最近释放(入库)时间(time.monotonic)

    @staticmethod
    def _is_sock_alive(sock):
        # getpeername() 无法检测对端已关闭的连接（FIN 后仍处于 CLOSE_WAIT）。
        # 用非阻塞可读性检查：可读说明有数据/FIN/RST，此时 peek 0 字节即已死亡。
        try:
            r, _, _ = select.select([sock], [], [], 0)
            if r:
                try:
                    return len(sock.recv(1, socket.MSG_PEEK)) > 0
                except (BlockingIOError, OSError):
                    return False
            return True
        except (OSError, ValueError):
            return False

    def _new_conn(self):
        return PooledHTTPConnection(self._host, self._port, timeout=self._timeout)

    def acquire(self):
        q = _get_queue()
        now = time.monotonic()
        while True:
            try:
                conn = self._pool.get_nowait()
            except q.Empty:
                break
            released_at = self._released_at.pop(conn, None)
            # 空闲超过 idle_timeout 的连接直接丢弃：
            # 对端（Node keepAliveTimeout=60s、uvicorn 等默认仅数秒）可能已
            # 静默关闭，与其在请求时踩雷再重试，不如主动重建，让首个请求
            # （如 system/ping）始终命中健康连接。
            if released_at is not None and now - released_at > self._idle_timeout:
                conn.close()
                continue
            if conn.sock is not None and self._is_sock_alive(conn.sock):
                return conn
            conn.close()
            break  # 只丢弃一条坏连接即可，其余留待后续使用
        return self._new_conn()

    def release(self, conn):
        q = _get_queue()
        if conn.sock is None:
            # 连接已被关闭（如请求失败后 close 过），不再放回池中复用
            conn.close()
            self._released_at.pop(conn, None)
            return
        self._released_at[conn] = time.monotonic()
        try:
            self._pool.put_nowait(conn)
        except q.Full:
            conn.close()
            self._released_at.pop(conn, None)

    def close_all(self):
        q = _get_queue()
        while True:
            try:
                conn = self._pool.get_nowait()
                self._released_at.pop(conn, None)
                conn.close()
            except q.Empty:
                break
        self._released_at.clear()

# ---------------------------------------------------------------------------
# 静态缓存（LRU）
# ---------------------------------------------------------------------------
class StaticCache:
    def __init__(self, max_size=50):
        self._cache = OrderedDict()
        self._max = max_size
        self._lock = threading.Lock()
    def get(self, key):
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
            return None
    def set(self, key, status, headers, body):
        with self._lock:
            if len(self._cache) >= self._max:
                self._cache.popitem(last=False)
            self._cache[key] = (status, headers, body)

_static_cache = StaticCache()

# ---------------------------------------------------------------------------
# 反向代理请求处理器
# ---------------------------------------------------------------------------
_REMOVE_HEADERS = frozenset({"x-frame-options"})

class ProxyHandler(http.server.BaseHTTPRequestHandler):
    _conn_pool = None
    protocol_version = "HTTP/1.1"

    def _strip_prefix(self):
        """剥离网关前缀，保留 query string。

        必须按路径段边界匹配：裸 startswith 会把 /app/moviepilotXYZ 也剥成
        "XYZ"（无前导斜杠），拼出非法的请求行。
        """
        path = self.path
        if path == GATEWAY_PREFIX:
            return "/"
        if path.startswith(GATEWAY_PREFIX + "/"):
            return path[len(GATEWAY_PREFIX):]
        return path

    def _read_chunked_body(self):
        """读取 chunked 编码的请求体（解码为原始字节）。

        BaseHTTPRequestHandler 不会自动解码 chunked body，若直接走 Content-Length
        分支会得到 None，POST body 丢失导致后端 422。这里按 RFC 7230 逐块解析：
        每个 chunk 先是一行十六进制长度，随后是该长度的数据 + CRLF，直到长度 0。
        """
        chunks = []
        try:
            while True:
                size_line = self.rfile.readline(65536)
                if not size_line:
                    break
                size_hex = size_line.split(b";", 1)[0].strip()
                if not size_hex:
                    continue
                try:
                    size = int(size_hex, 16)
                except ValueError:
                    break
                if size == 0:
                    # 读取 chunked 结尾的 trailer 直到空行
                    while True:
                        trailer = self.rfile.readline(65536)
                        if not trailer or trailer in (b"\r\n", b"\n"):
                            break
                    break
                chunk = self.rfile.read(size)
                if not chunk:
                    break
                chunks.append(chunk)
                self.rfile.read(2)  # 吃掉 chunk 后的 CRLF
        except (OSError, ValueError):
            return None
        return b"".join(chunks)

    def _rewrite_html(self, data):
        # 自动登录脚本先注入：它要赶在 deferred 的模块脚本之前执行，但两者都在
        # </head> 之前插入即可（classic script 天然早于 type="module"）。
        if _AUTOLOGIN_JS_BYTES:
            data = data.replace(b'</head>', _AUTOLOGIN_JS_BYTES + b'</head>', 1)
        data = data.replace(b'</head>', _POLYFILL_BYTES + b'</head>', 1)
        data = _RE_HTML_ATTR.sub(rb'\1=\2' + GATEWAY_PREFIX.encode() + rb'/', data)
        return data

    def _serve_autologin(self):
        """应答 `/__mp/autologin.json`：由代理自己出，不转发给前端/后端。

        只回"够用且最小"的信息：令牌、用户身份、资源 Cookie。后端地址、错误
        细节、密码一个字节都不出现 —— 这个端点的响应可能被浏览器缓存/被截图。
        """
        if self.command not in ("GET", "HEAD"):
            self.send_response(405)
            self.send_header("Allow", "GET, HEAD")
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            return
        try:
            result, cookie = _AUTOLOGIN.state()
        except Exception as exc:   # 兜底：任何意外都降级成普通登录页
            logging.warning("网关免登录：生成登录状态异常: %s", exc)
            result, cookie = {"available": False, "reason": "error"}, ""
        body = json.dumps(result, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            # 令牌绝不能被缓存：Service Worker / 磁盘缓存拿到旧令牌就等于登录失效
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_request(self):
        # 避免 HTTP/1.1 长连接在 BaseHTTPRequestHandler 中的 keep-alive 问题
        self.close_connection = True

        if self.path == GATEWAY_PREFIX:
            self.send_response(301)
            self.send_header("Location", GATEWAY_PREFIX + "/")
            self.send_header("Connection", "close")
            self.end_headers()
            return

        # WebSocket 升级
        if self.headers.get("Upgrade", "").lower() == "websocket":
            self._handle_ws()
            return

        path = self._strip_prefix()

        # 免登录凭据端点：开关打开时由代理应答（必须在取连接池之前返回，
        # 否则会白白占用一条到前端的连接）
        if _AUTOLOGIN.enabled and path.split("?", 1)[0] == AUTOLOGIN_ENDPOINT:
            self._serve_autologin()
            return

        is_head = self.command == "HEAD"
        port = INITIAL_PORT
        pool = ProxyHandler._conn_pool
        conn = pool.acquire() if pool else PooledHTTPConnection(TARGET_HOST, port, timeout=60)

        # 构造转发头
        headers = {}
        skip = frozenset({"host", "connection", "transfer-encoding",
                          "accept-encoding", "origin", "referer", "accept"})
        for k, v in self.headers.items():
            if k.lower() not in skip:
                headers[k] = v
        # 还原被 polyfill 改名的认证头（原因见 AUTH_HEADER_ALT 的注释）：
        # 浏览器只能发 X-MP-Auth（Authorization 会被 fnOS 网关拦掉），后端只认
        # Authorization，所以在最后一跳翻译回来。仅在没有 Authorization 时补齐，
        # 不影响经 Unix socket 直连、自带 Authorization 的调用方。
        for _k in list(headers):
            if _k.lower() == AUTH_HEADER_ALT.lower():
                _alt = headers.pop(_k)
                if not any(_h.lower() == AUTH_HEADER.lower() for _h in headers):
                    headers[AUTH_HEADER] = _alt
                break
        backend_url = "http://%s:%d" % (TARGET_HOST, port)
        headers["Host"] = "%s:%d" % (TARGET_HOST, port)
        headers["Accept-Encoding"] = "gzip, deflate"
        headers["Accept"] = "*/*"
        referer = self.headers.get("Referer", "")
        if referer:
            headers["Referer"] = _RE_REFERER.sub(backend_url, referer)

        body = None
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            # 浏览器/axios 对较大请求体可能用 chunked 编码。若按 Content-Length 读取
            # 会得到 None，转发时丢失整个 POST body，导致后端收到空 body 返回 422。
            body = self._read_chunked_body()
        else:
            clen = self.headers.get("Content-Length")
            if clen:
                try:
                    body = self.rfile.read(int(clen))
                except (ValueError, OSError):
                    body = None

        # 静态缓存命中
        # 后缀判定沿用原语义（含 query，保守）；额外排除稳定文件名的 PWA 资源，
        # 否则升级后浏览器会一直用旧 Service Worker 拉旧 chunk（见 _NO_CACHE_BASENAMES）。
        cache_key = self.command + ":" + path
        base_name = path.split("?", 1)[0].rsplit("/", 1)[-1].lower()
        cacheable = self.command == "GET" and base_name not in _NO_CACHE_BASENAMES \
            and path.rfind('.') > 0 and \
            path[path.rfind('.') + 1:].lower() in STATIC_EXTENSIONS
        if cacheable:
            cached = _static_cache.get(cache_key)
            if cached:
                c_status, c_headers, c_body = cached
                try:
                    self.send_response(c_status)
                    for k, v in c_headers:
                        self.send_header(k, v)
                    self.send_header("Content-Length", str(len(c_body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    if not is_head:
                        self.wfile.write(c_body)
                finally:
                    if pool:
                        pool.release(conn)
                    else:
                        conn.close()
                return

        try:
            conn.request(self.command, path, body, headers)
            resp = conn.getresponse()
        except Exception as e:
            logging.warning("request failed, retry fresh: %s -> %s", self.command, e)
            conn.close()
            fresh = PooledHTTPConnection(TARGET_HOST, port, timeout=60)
            try:
                fresh.request(self.command, path, body, headers)
                resp = fresh.getresponse()
                conn = fresh
            except Exception as e2:
                self.send_error(502, str(e2))
                fresh.close()
                return

        all_headers = resp.getheaders()
        content_type = next(
            (v for k, v in all_headers if k.lower() == "content-type"), "")
        is_html = "text/html" in content_type
        # SSE 实时日志等流式响应：必须逐块透传，不能 resp.read() 等流结束，
        # 否则 MoviePilot 系统日志（text/event-stream 长连接）永远收不到数据而显示空白。
        is_streaming = content_type.startswith("text/event-stream")
        content_encoding = next(
            (v for k, v in all_headers if k.lower() == "content-encoding"), None)

        try:
            self.send_response(resp.status)
            for k, v in all_headers:
                kl = k.lower()
                if kl in _REMOVE_HEADERS:
                    continue
                if kl == "set-cookie":
                    self.send_header(k, v)
                    continue
                if kl == "content-encoding" and is_html:
                    continue
                if kl in ("transfer-encoding", "connection", "content-length"):
                    continue
                self.send_header(k, v)

            if is_streaming:
                # 流式透传：不发 Content-Length，保持长连接逐块转发，
                # 直到后端流结束或浏览器断开。
                # 必须用 read1() 而非 read()：对裸流 SSE（Connection: close + 非 chunked，
                # 无 Content-Length），read(amt) 会阻塞等待填满 amt 字节，导致后端推送的
                # 小块日志永远无法返回给浏览器（系统日志空白）。read1() 只返回当前可用数据。
                self.send_header("Connection", "close")
                self.end_headers()
                if not is_head:
                    try:
                        while True:
                            chunk = resp.read1(65536)
                            if not chunk:
                                break
                            try:
                                self.wfile.write(chunk)
                                self.wfile.flush()
                            except (BrokenPipeError, ConnectionResetError):
                                break  # 浏览器提前断开
                    except Exception as e:
                        logging.debug("stream read error: %s", e)
                return

            data = resp.read()
            if is_html:
                if content_encoding == "gzip":
                    data = gzip.decompress(data)
                elif content_encoding == "deflate":
                    data = zlib.decompress(data)
                data = self._rewrite_html(data)
                self.send_header("Content-Security-Policy",
                    "default-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob:; "
                    "frame-ancestors *; img-src 'self' data: blob: https:; "
                    "font-src 'self' data: https://fonts.gstatic.com; "
                    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                    "connect-src 'self' https: http://127.0.0.1:*/api/ ws: wss:;")
            elif cacheable and 200 <= resp.status < 300:
                ch = [(k, v) for k, v in all_headers
                      if k.lower() not in ("transfer-encoding", "connection",
                                           "content-length", "set-cookie")]
                _static_cache.set(cache_key, resp.status, ch, data)

            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            if not is_head:
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            # 客户端（浏览器）提前断开连接：刷新/取消请求时常见，属正常现象，
            # 静默忽略，避免向日志刷 traceback。
            pass
        except Exception as e:
            logging.warning("response write error: %s", e)
        finally:
            if is_streaming:
                # SSE 长连接结束后对端连接已关闭/处于流式未完成态，直接关闭，
                # 不要放回连接池复用，避免污染复用连接的读写状态。
                try:
                    conn.close()
                except Exception:
                    pass
            elif pool:
                pool.release(conn)
            else:
                conn.close()

    # WS 握手头部：下列头由代理显式构造或按规范不得透传
    _WS_SKIP_HEADERS = frozenset({
        "host", "connection", "upgrade", "origin",
        "sec-websocket-key", "sec-websocket-version",
        "sec-websocket-protocol", "sec-websocket-extensions",
        "content-length", "transfer-encoding",
    })

    def _handle_ws(self):
        path = self._strip_prefix()
        port = INITIAL_PORT
        backend = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        backend.settimeout(10)
        try:
            backend.connect((TARGET_HOST, port))
        except Exception as e:
            backend.close()
            self.send_error(502, str(e))
            return

        ws_key = self.headers.get("Sec-WebSocket-Key", "")
        ws_ver = self.headers.get("Sec-WebSocket-Version", "13")
        extra = self.headers.get("Sec-WebSocket-Protocol", "")

        # 透传客户端其余头部。早期实现只发握手必需的 4 个头，Cookie/Authorization
        # 全部丢失，任何需要登录态的 WebSocket 都会被后端拒绝（401/403）。
        passthrough = []
        for k, v in self.headers.items():
            if k.lower() not in self._WS_SKIP_HEADERS:
                passthrough.append("%s: %s\r\n" % (k, v))

        req_line = (
            "GET %s HTTP/1.1\r\n"
            "Host: %s:%d\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "%s"
            "Sec-WebSocket-Version: %s\r\n"
            "%s"
            "%s"
            "Origin: http://%s:%d\r\n\r\n"
        ) % (path, TARGET_HOST, port,
             ("Sec-WebSocket-Key: %s\r\n" % ws_key) if ws_key else "",
             ws_ver,
             ("Sec-WebSocket-Protocol: %s\r\n" % extra) if extra else "",
             "".join(passthrough),
             TARGET_HOST, port)
        try:
            backend.sendall(req_line.encode())
        except Exception as e:
            backend.close()
            self.send_error(502, str(e))
            return

        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = backend.recv(4096)
            if not chunk:
                backend.close()
                self.send_error(502, "backend closed")
                return
            resp += chunk
        hdr_end = resp.index(b"\r\n\r\n")
        hdr_raw = resp[:hdr_end].decode("utf-8", errors="replace")
        remaining = resp[hdr_end + 4:]
        try:
            status_code = int(hdr_raw.split(" ", 2)[1])
        except Exception:
            status_code = 502

        try:
            self.send_response(status_code)
            for line in hdr_raw.split("\r\n")[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    self.send_header(k.strip(), v.strip())
            self.end_headers()
            if remaining:
                self.wfile.write(remaining)
                self.wfile.flush()
        except Exception as e:
            logging.warning("ws response write failed: %s", e)
            backend.close()
            return

        client = self.connection
        backend.setblocking(True)
        client.setblocking(True)
        # 两端开启 TCP keepalive：WebSocket 长时间无业务消息是常态，靠应用层超时
        # 判断存活不可靠，交给内核探测半开连接（60s 空闲 + 20s×3 探测）。
        for s in (client, backend):
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 20)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
            except (AttributeError, OSError):
                pass  # 非 Linux 或系统不支持，忽略

        def tunnel(a, b):
            try:
                while True:
                    # 超时只用于周期性唤醒，不能因"空闲"就关闭连接：WebSocket 静默
                    # 数分钟是正常状态，早期实现空闲 30s 即 return，会把正常连接
                    # 反复踢断。真正的断线由 recv 返回空、sendall 抛错或 TCP
                    # keepalive 判定。
                    r, _, _ = select.select([a, b], [], [], 30)
                    if not r:
                        continue
                    for s in r:
                        data = s.recv(65536)
                        if not data:
                            return
                        if s is a:
                            b.sendall(data)
                        else:
                            a.sendall(data)
            except Exception:
                pass
            finally:
                try: a.close()
                except Exception: pass
                try: b.close()
                except Exception: pass

        t = threading.Thread(target=tunnel, args=(client, backend))
        t.daemon = True
        t.start()
        # 必须阻塞到隧道结束：ThreadingHTTPServer 在 handler 返回后会立即对客户端
        # socket 执行 shutdown(SHUT_WR)+close，不等待的话 WebSocket 握手成功后
        # 毫秒级即被框架断开，前端只能无限静默重连。
        t.join()

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_PATCH = do_OPTIONS = do_request

    def log_message(self, format, *args):
        logging.info(format % args)


class ThreadedUnixHTTPServer(http.server.ThreadingHTTPServer):
    address_family = socket.AF_UNIX
    daemon_threads = True
    def server_bind(self):
        self.socket.bind(self.server_address)
        os.chmod(self.server_address, 0o660)


def cleanup(signum, frame):
    logging.info("shutting down")
    server.server_close()
    if os.path.exists(SOCK_PATH):
        os.unlink(SOCK_PATH)
    sys.exit(0)


if __name__ == "__main__":
    ProxyHandler._conn_pool = ConnectionPool(TARGET_HOST, INITIAL_PORT)
    if os.path.exists(SOCK_PATH):
        os.unlink(SOCK_PATH)
    server = ThreadedUnixHTTPServer(SOCK_PATH, ProxyHandler)
    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)
    logging.info("gateway-proxy started: %s -> %s:%d", SOCK_PATH, TARGET_HOST, INITIAL_PORT)
    # 免登录的状态必须在启动日志里留下痕迹：否则"开了开关却仍要登录"时，
    # 排障只能靠猜（凭据缺失 / 密码与库里不一致，两种都在这里一眼看出）。
    if AUTOLOGIN_ENABLED:
        if _AUTOLOGIN.enabled:
            logging.info("网关免登录：已启用（账号 %s，来源 %s）",
                         AUTOLOGIN_USER or "(未配置)",
                         _ENV_FILE or "(环境变量)")
        else:
            logging.warning(
                "网关免登录：开关已打开，但缺少可用凭据（SUPERUSER=%r，"
                "SUPERUSER_PASSWORD %s），本次按关闭处理",
                AUTOLOGIN_USER, "已配置" if AUTOLOGIN_PASSWORD else "为空",
            )
    else:
        logging.info("网关免登录：未启用（MP_GATEWAY_AUTOLOGIN=0）")
    if _AUTOLOGIN.enabled:
        # 后台预热：失败只记日志，绝不影响代理启动（代理起不来 = 应用打不开）
        threading.Thread(target=_AUTOLOGIN.warm, name="autologin-warm",
                         daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()
    if ProxyHandler._conn_pool:
        ProxyHandler._conn_pool.close_all()
    if os.path.exists(SOCK_PATH):
        os.unlink(SOCK_PATH)
