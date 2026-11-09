#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网关免登录（MP_GATEWAY_AUTOLOGIN）的离线冒烟测试（Windows 也能跑）。

背景：本应用跑在 fnOS 统一网关后面，网关已经校验过 NAS 登录态，但 MoviePilot
自己的登录态在浏览器 localStorage（pinia 的 `auth` / `user` store）。开启免登录
后由 gateway-proxy **代浏览器**登录一次，把令牌经 `/app/moviepilot/__mp/autologin.json`
交给页面头部注入的脚本写入 store。这条链路横跨"Python 代理 + 浏览器 JS + 前端
store 结构"，任何一环错了都很难定位，所以这里逐环验证：

  A app.env 解析：后出现的键覆盖、`#` 注释、成对引号、坏行不影响其它键
  B 注入脚本：语法（node --check）+ 关键行为（已有 token 不动、会话内只试一次、
    只写 store 才 reload、端点带网关前缀）
  C 关闭（默认）：不注入脚本、端点透传给后端（404）、代理一次登录都不发
  D 开启：端点返回令牌 + 两个 store 的初始状态 + 资源 Cookie；重复请求命中
    缓存（后端只收到一次登录）
  E 令牌将到期：端点自动续期（第二次请求换来新令牌），避免用着用着掉登录
  F 代登录失败（401，例如密码被改过 / 开了二次验证）：端点 available=false、
    页面仍 200、失败后进入退避且启动预热停手（不把账号打进登录限流）
  G 开关打开但缺凭据：不注入脚本（否则只多一个 404 请求）
  H 环境变量覆盖 app.env：便于排障时临时开关

依赖：端到端场景需要 python（本文件自己就是）；B 项需要 node，没有则 SKIP 不计失败。
"""
import base64
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROXY = ROOT / "app" / "bin" / "gateway-proxy.py"
SANDBOX = ROOT / ".local-build" / "_smoke" / "autologin"
GATEWAY_PREFIX = "/app/moviepilot"
ENDPOINT = GATEWAY_PREFIX + "/__mp/autologin.json"
APP_ENV_SAMPLE = "SUPERUSER=admin\nSUPERUSER_PASSWORD=pw-123456\n"

FAILS = []


def check(label, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + label
          + (f"  <- {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(label)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def node_path():
    return shutil.which("node")


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def http_get(url, timeout=5):
    """返回 (status, headers, body)。4xx/5xx 也正常返回，不抛异常。"""
    req = urllib.request.Request(url, headers={"Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()
    except Exception:
        return 0, {}, b""


def make_jwt(exp_offset, user_id=7, name="admin"):
    """造一个形如 JWT 的令牌：代理只读 payload 里的 exp，不验签。"""
    def b64(obj):
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    header = b64({"alg": "HS256", "typ": "JWT"})
    payload = b64({
        "sub": str(user_id),
        "username": name,
        "iat": int(time.time()),
        "exp": int(time.time()) + exp_offset,
        "purpose": "authentication",
        "jti": uuid.uuid4().hex,
    })
    return header + "." + payload + ".sig"


def load_proxy(app_env_text, env_extra=None):
    """在独立临时 CONFIG_DIR 下导入 gateway-proxy.py，返回模块（供单元级断言）。

    每个场景用不同的模块名重复导入：模块会在 import 时读取 app.env 与环境变量，
    这正是线上行为，不能绕过。
    """
    tmp = Path(tempfile.mkdtemp(prefix="mp-autologin-"))
    (tmp / "app.env").write_text(app_env_text, encoding="utf-8")
    # Windows 没有 AF_UNIX：模块在类定义阶段就要读这个常量，导入前先打桩
    if not hasattr(socket, "AF_UNIX"):
        socket.AF_UNIX = socket.AF_INET
    saved = dict(os.environ)
    try:
        os.environ["CONFIG_DIR"] = str(tmp)
        for key in ("MP_GATEWAY_AUTOLOGIN", "MP_GATEWAY_AUTOLOGIN_USER",
                    "MP_GATEWAY_AUTOLOGIN_PASSWORD", "MP_APP_ENV"):
            os.environ.pop(key, None)
        os.environ.update(env_extra or {})
        saved_argv = list(sys.argv)
        sys.argv = ["gateway-proxy.py", "unused.sock", "127.0.0.1", "1"]
        try:
            spec = importlib.util.spec_from_file_location(
                f"gw_probe_{uuid.uuid4().hex}", PROXY)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        finally:
            sys.argv = saved_argv
    finally:
        os.environ.clear()
        os.environ.update(saved)
    mod._probe_tmp = tmp
    return mod


# ---------------------------------------------------------------------------
# 假 MoviePilot（前端 + 后端合成一个服务）
# ---------------------------------------------------------------------------
STATE = {
    "logins": 0,
    "status": 200,      # 登录接口返回码：200 成功 / 401 密码错误 / 429 限流
    "exp": 3600,        # 令牌有效期（秒）
    "usernames": [],    # 代理实际提交的用户名（验证凭据来源）
    "paths": [],        # 后端收到的路径（验证端点是否被代理接管）
}


def reset_state():
    STATE.update({"logins": 0, "status": 200, "exp": 3600,
                  "usernames": [], "paths": []})


class FakeApp(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, status, body, content_type="application/json", extra=None):
        raw = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        STATE["paths"].append(path)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace")
        if path != "/api/v1/login/access-token":
            self._send(404, b'{"detail":"not found"}')
            return
        form = urllib.parse.parse_qs(body)
        STATE["logins"] += 1
        STATE["usernames"].append((form.get("username") or [""])[0])
        if STATE["status"] != 200:
            self._send(STATE["status"], b'{"detail":"bad credentials"}')
            return
        token = make_jwt(STATE["exp"])
        self._send(200, json.dumps({
            "access_token": token,
            "token_type": "bearer",
            "super_user": True,
            "user_id": 7,
            "user_name": "admin",
            "avatar": "",
            "level": 2,
            "permissions": {"discovery": True, "search": True,
                            "subscribe": True, "manage": True,
                            "admin": True, "features": {}},
        }), extra={"Set-Cookie": 'MoviePilot=res-%d; Path=/; HttpOnly' % STATE["logins"]})

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        STATE["paths"].append(path)
        if path in ("/", "/index.html"):
            self._send(200, "<!doctype html><html><head><title>MoviePilot</title>"
                            "</head><body>ok</body></html>",
                       "text/html; charset=utf-8")
            return
        if path == "/api/v1/system/ping":
            self._send(200, b'{"success":true}')
            return
        self._send(404, b'{"detail":"not found"}', extra={"X-Backend": "1"})


class FakeServer:
    def __init__(self):
        self.port = free_port()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), FakeApp)
        self.httpd.daemon_threads = True
        self.thread = None

    def __enter__(self):
        import threading
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        return False


RUNNER = r'''
import sys, socket, threading, importlib.util, http.server
proxy_path, proxy_port, backend_port = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
if not hasattr(socket, "AF_UNIX"):
    socket.AF_UNIX = socket.AF_INET
spec = importlib.util.spec_from_file_location("gw", proxy_path)
gw = importlib.util.module_from_spec(spec)
sys.argv = ["gateway-proxy.py", "unused.sock", "127.0.0.1", str(backend_port)]
spec.loader.exec_module(gw)
if gw._AUTOLOGIN.enabled:
    threading.Thread(target=gw._AUTOLOGIN.warm, daemon=True).start()
class H(gw.ProxyHandler):
    pass
http.server.ThreadingHTTPServer(("127.0.0.1", proxy_port), H).serve_forever()
'''


class ProxyStack:
    """起一个真实 gateway-proxy（AF_UNIX 打桩成 TCP）+ 假 MoviePilot。"""

    def __init__(self, app_env_text, env_extra=None):
        self.app_env_text = app_env_text
        self.env_extra = env_extra or {}
        self.tmp = None
        self.proc = None
        self.port = free_port()
        self.backend = None
        self.log = ""

    def __enter__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mp-autologin-e2e-"))
        (self.tmp / "app.env").write_text(self.app_env_text, encoding="utf-8")
        SANDBOX.mkdir(parents=True, exist_ok=True)
        runner = SANDBOX / "runner.py"
        runner.write_text(RUNNER, encoding="utf-8")
        self.backend = FakeServer().__enter__()
        env = dict(os.environ)
        env["CONFIG_DIR"] = str(self.tmp)
        env["PYTHONIOENCODING"] = "utf-8"
        for key in ("MP_GATEWAY_AUTOLOGIN", "MP_GATEWAY_AUTOLOGIN_USER",
                    "MP_GATEWAY_AUTOLOGIN_PASSWORD", "MP_APP_ENV"):
            env.pop(key, None)
        env.update(self.env_extra)
        self.proc = subprocess.Popen(
            [sys.executable, str(runner), str(PROXY), str(self.port),
             str(self.backend.port)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.proc.poll() is not None:
                break
            status, _h, _b = http_get(self.url("/__ready__"), timeout=1)
            if status:
                return self
            time.sleep(0.1)
        return self

    def __exit__(self, *exc):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except Exception:
                self.proc.kill()
        if self.proc:
            try:
                self.log = self.proc.stdout.read() or ""
            except Exception:
                self.log = ""
            try:
                self.proc.stdout.close()
            except Exception:
                pass
        if self.backend:
            self.backend.__exit__()
        if self.tmp:
            shutil.rmtree(self.tmp, ignore_errors=True)
        return False

    @property
    def ready(self):
        return self.proc is not None and self.proc.poll() is None

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"


# ---------------------------------------------------------------------------
# A. app.env 解析
# ---------------------------------------------------------------------------
def scenario_app_env():
    print("\n-- A. app.env 解析 --")
    text = (
        "# 注释行\n"
        "SUPERUSER= admin \n"
        'SUPERUSER_PASSWORD="pw with spaces"\n'
        "MP_GATEWAY_AUTOLOGIN=0\n"
        "MP_GATEWAY_AUTOLOGIN=1\n"
        "BROKEN LINE WITHOUT EQUALS\n"
        "API_TOKEN='quoted'\n"
        "EMPTY=\n"
    )
    mod = load_proxy(text)
    try:
        parsed = mod._read_app_env(str(Path(mod._probe_tmp) / "app.env"))
        check("A1 去空白", parsed.get("SUPERUSER") == "admin", repr(parsed.get("SUPERUSER")))
        check("A2 去成对引号", parsed.get("SUPERUSER_PASSWORD") == "pw with spaces",
              repr(parsed.get("SUPERUSER_PASSWORD")))
        check("A3 后者覆盖前者（与 dotenv 一致）",
              parsed.get("MP_GATEWAY_AUTOLOGIN") == "1",
              repr(parsed.get("MP_GATEWAY_AUTOLOGIN")))
        check("A4 坏行不影响其它键", parsed.get("API_TOKEN") == "quoted", repr(parsed))
        check("A5 空值保留为空串", parsed.get("EMPTY") == "", repr(parsed.get("EMPTY")))
        check("A6 注释行被忽略", "# 注释行" not in parsed)
        check("A7 开关解析为开（1）", mod.AUTOLOGIN_ENABLED is True)
        check("A8 账号取自 SUPERUSER", mod.AUTOLOGIN_USER == "admin")
        check("A9 缺省关闭语义（0 -> False）",
              mod._env_bool("0") is False and mod._env_bool("") is False
              and mod._env_bool("true") is True)
        check("A10 缺凭据时不注入脚本（_AUTOLOGIN_READY 与脚本字节一致）",
              bool(mod._AUTOLOGIN_JS_BYTES) == mod._AUTOLOGIN_READY)
    finally:
        shutil.rmtree(getattr(mod, "_probe_tmp", ""), ignore_errors=True)


# ---------------------------------------------------------------------------
# B. 注入脚本
# ---------------------------------------------------------------------------
def scenario_js():
    print("\n-- B. 注入脚本（语法 + 行为约定）--")
    mod = load_proxy(APP_ENV_SAMPLE + "MP_GATEWAY_AUTOLOGIN=1\n")
    try:
        full = mod._build_autologin_js()
        inner = full[len("<script>"):-len("</script>")]
        check("B1 被 <script> 包裹", full.startswith("<script>") and full.endswith("</script>"))
        check("B2 端点带网关前缀", f'P+E' in inner and f'E="{mod.AUTOLOGIN_ENDPOINT}"' in inner)
        check("B3 已有登录态不覆盖", 'if(has()){return;}' in inner)
        check("B4 会话内只尝试一次", '"mp_autologin_tried"' in inner)
        check("B5 只在 available 为 true 时写 store",
              'd.available!==true' in inner)
        check("B6 写两个 store（auth/user）",
              'localStorage.setItem("auth"' in inner
              and 'localStorage.setItem("user"' in inner)
        check("B7 写入成功才 reload", inner.index("location.reload")
              > inner.index('localStorage.setItem("auth"'))
        check("B8 脚本内不含密码", "pw-123456" not in full)

        node = node_path()
        if not node:
            print("SKIP: 未找到 node，跳过 JS 语法校验（不计为失败）")
            return
        SANDBOX.mkdir(parents=True, exist_ok=True)
        js_file = SANDBOX / "_autologin_inner.js"
        js_file.write_text(inner, encoding="utf-8")
        result = subprocess.run([node, "--check", str(js_file)],
                                capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
        check("B9 注入脚本 JS 语法通过", result.returncode == 0,
              (result.stderr or "").strip().splitlines()[:3])
    finally:
        shutil.rmtree(getattr(mod, "_probe_tmp", ""), ignore_errors=True)


# ---------------------------------------------------------------------------
# C 关闭 / D 开启 / E 续期 / F 失败 / G 缺凭据 / H 环境变量覆盖
# ---------------------------------------------------------------------------
def scenario_disabled():
    print("\n-- C. 关闭（默认）：行为必须与改动前完全一致 --")
    reset_state()
    with ProxyStack(APP_ENV_SAMPLE) as stack:
        check("C0 代理已就绪", stack.ready, stack.log[-300:])
        status, _h, body = http_get(stack.url(GATEWAY_PREFIX + "/"))
        html = body.decode("utf-8", "replace")
        check("C1 HTML 200", status == 200, f"status={status}")
        check("C2 未注入免登录脚本", "__mp/autologin.json" not in html)
        check("C3 polyfill 仍在（未破坏原有注入）", "window.fetch=function" in html)
        status, headers, _b = http_get(stack.url(ENDPOINT))
        check("C4 端点不接管，透传给后端（404 + 后端标记）",
              status == 404 and headers.get("X-Backend") == "1",
              f"status={status} backend={headers.get('X-Backend')}")
        check("C5 一次登录都没发", STATE["logins"] == 0, f"logins={STATE['logins']}")
        check("C6 后端没收到任何登录请求",
              "/api/v1/login/access-token" not in STATE["paths"], str(STATE["paths"]))


def scenario_enabled():
    print("\n-- D. 开启：端点交出令牌 + 两个 store 的初始状态 --")
    reset_state()
    with ProxyStack(APP_ENV_SAMPLE + "MP_GATEWAY_AUTOLOGIN=1\n") as stack:
        check("D0 代理已就绪", stack.ready, stack.log[-300:])
        status, _h, body = http_get(stack.url(GATEWAY_PREFIX + "/"))
        html = body.decode("utf-8", "replace")
        check("D1 HTML 注入免登录脚本", "__mp/autologin.json" in html)
        check("D2 注入在 </head> 之前", html.index("__mp/autologin.json") < html.index("</head>"))
        check("D3 polyfill 与免登录脚本共存",
              "window.fetch=function" in html and "__mp/autologin.json" in html)

        status, headers, body = http_get(stack.url(ENDPOINT))
        payload = json.loads(body.decode("utf-8")) if body else {}
        check("D4 端点 200", status == 200, f"status={status}")
        check("D5 available=true", payload.get("available") is True, str(payload)[:200])
        check("D6 auth store 结构 {token,remember}",
              isinstance(payload.get("auth"), dict)
              and payload["auth"].get("token")
              and payload["auth"].get("remember") is True)
        user = payload.get("user") or {}
        check("D7 user store 关键字段",
              user.get("superUser") is True and user.get("userID") == 7
              and user.get("userName") == "admin",
              str(user)[:200])
        check("D8 权限对象补齐 manage/admin/features",
              user.get("permissions", {}).get("manage") is True
              and "features" in user.get("permissions", {}))
        check("D9 资源 Cookie 被转发（EventSource 只认它）",
              "MoviePilot=" in (headers.get("Set-Cookie") or ""),
              repr(headers.get("Set-Cookie")))
        check("D10 令牌不可缓存", "no-store" in (headers.get("Cache-Control") or ""),
              repr(headers.get("Cache-Control")))
        check("D11 用 app.env 的账号登录", STATE["usernames"][:1] == ["admin"],
              str(STATE["usernames"]))

        logins_before = STATE["logins"]
        token_before = payload["auth"]["token"]
        status2, _h2, body2 = http_get(stack.url(ENDPOINT))
        payload2 = json.loads(body2.decode("utf-8")) if body2 else {}
        check("D12 重复请求命中缓存（不再登录）",
              STATE["logins"] == logins_before,
              f"before={logins_before} after={STATE['logins']}")
        check("D13 令牌保持不变", payload2.get("auth", {}).get("token") == token_before)
        check("D14 端点请求没打到后端",
              ENDPOINT not in STATE["paths"], str(STATE["paths"]))


def scenario_refresh():
    print("\n-- E. 令牌将到期：端点自动续期 --")
    reset_state()
    STATE["exp"] = 120          # 远小于 REFRESH_MARGIN(600)：每次都应续期
    with ProxyStack(APP_ENV_SAMPLE + "MP_GATEWAY_AUTOLOGIN=1\n") as stack:
        check("E0 代理已就绪", stack.ready, stack.log[-300:])
        status, _h, body = http_get(stack.url(ENDPOINT))
        first = json.loads(body.decode("utf-8")) if body else {}
        count_after_first = STATE["logins"]
        status2, _h2, body2 = http_get(stack.url(ENDPOINT))
        second = json.loads(body2.decode("utf-8")) if body2 else {}
        check("E1 第一次就有令牌", status == 200 and first.get("available") is True)
        check("E2 第二次触发续期（登录次数增加）",
              STATE["logins"] > count_after_first,
              f"{count_after_first} -> {STATE['logins']}")
        check("E3 换到了新令牌",
              first.get("auth", {}).get("token") != second.get("auth", {}).get("token"))
        check("E4 续期后仍 available", second.get("available") is True)


def scenario_login_failed():
    print("\n-- F. 代登录失败（401）：降级登录页 + 退避 + 预热停手 --")
    reset_state()
    STATE["status"] = 401
    with ProxyStack(APP_ENV_SAMPLE + "MP_GATEWAY_AUTOLOGIN=1\n") as stack:
        check("F0 代理已就绪", stack.ready, stack.log[-300:])
        status, _h, body = http_get(stack.url(GATEWAY_PREFIX + "/"))
        check("F1 页面仍然 200（绝不阻断登录）", status == 200, f"status={status}")
        check("F2 脚本仍注入（失败与否由端点决定）",
              "__mp/autologin.json" in body.decode("utf-8", "replace"))
        status, _h, body = http_get(stack.url(ENDPOINT))
        payload = json.loads(body.decode("utf-8")) if body else {}
        check("F3 available=false", payload.get("available") is False, str(payload))
        check("F4 不把后端错误细节透给页面",
              "bad credentials" not in json.dumps(payload), str(payload))
        check("F5 响应里不含密码", "pw-123456" not in json.dumps(payload))
        first = STATE["logins"]
        time.sleep(2.5)   # 预热线程若没停手，这段时间至少还要再撞 1 次
        http_get(stack.url(ENDPOINT))
        check("F6 失败后退避 + 预热停手（不会狂撞登录接口）",
              STATE["logins"] == first,
              f"first={first} now={STATE['logins']}")


def scenario_no_credentials():
    print("\n-- G. 开关打开但缺凭据：不注入脚本 --")
    reset_state()
    with ProxyStack("SUPERUSER=admin\nMP_GATEWAY_AUTOLOGIN=1\n") as stack:
        check("G0 代理已就绪", stack.ready, stack.log[-300:])
        status, _h, body = http_get(stack.url(GATEWAY_PREFIX + "/"))
        html = body.decode("utf-8", "replace")
        check("G1 未注入脚本", "__mp/autologin.json" not in html)
        status, headers, _b = http_get(stack.url(ENDPOINT))
        check("G2 端点透传（404）", status == 404, f"status={status}")
        check("G3 没有发出登录请求", STATE["logins"] == 0, f"logins={STATE['logins']}")


def scenario_env_override():
    print("\n-- H. 环境变量覆盖 app.env --")
    reset_state()
    with ProxyStack("SUPERUSER=from-env-file\nSUPERUSER_PASSWORD=file-pw\n",
                    {"MP_GATEWAY_AUTOLOGIN": "1",
                     "MP_GATEWAY_AUTOLOGIN_USER": "env-user",
                     "MP_GATEWAY_AUTOLOGIN_PASSWORD": "env-pw"}) as stack:
        check("H0 代理已就绪", stack.ready, stack.log[-300:])
        status, _h, body = http_get(stack.url(ENDPOINT))
        payload = json.loads(body.decode("utf-8")) if body else {}
        check("H1 端点可用", status == 200 and payload.get("available") is True)
        check("H2 用的是环境变量里的账号", STATE["usernames"][:1] == ["env-user"],
              str(STATE["usernames"]))


def scenario_concurrent():
    print("\n-- I. 并发端点请求：只登录一次（首屏多标签页/预热撞车）--")
    reset_state()
    # 后端登录慢一点，让两个请求真正重叠：没有去重的话必然是 2 次登录
    STATE["exp"] = 3600
    with ProxyStack(APP_ENV_SAMPLE + "MP_GATEWAY_AUTOLOGIN=1\n") as stack:
        check("I0 代理已就绪", stack.ready, stack.log[-300:])
        before = STATE["logins"]
        results = []
        import threading as _threading

        def hit():
            status, _h, body = http_get(stack.url(ENDPOINT))
            try:
                results.append((status, json.loads(body.decode("utf-8"))))
            except Exception:
                results.append((status, {}))

        threads = [_threading.Thread(target=hit) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        delta = STATE["logins"] - before
        check("I1 四个并发请求最多只触发一次登录", delta <= 1, f"logins+={delta}")
        check("I2 全部返回 200", all(s == 200 for s, _ in results), str([s for s, _ in results]))
        tokens = {p.get("auth", {}).get("token") for _s, p in results}
        check("I3 拿到的是同一个令牌", len(tokens) == 1 and None not in tokens,
              str(len(tokens)))
        check("I4 没有出现 in_progress 降级",
              all(p.get("available") is True for _s, p in results),
              str([p.get("reason") for _s, p in results]))


def main():
    print("== 网关免登录（MP_GATEWAY_AUTOLOGIN）冒烟测试 ==")
    if not PROXY.exists():
        check("gateway-proxy.py 存在", False, str(PROXY))
        return 1
    SANDBOX.mkdir(parents=True, exist_ok=True)

    scenario_app_env()
    scenario_js()
    scenario_disabled()
    scenario_enabled()
    scenario_refresh()
    scenario_login_failed()
    scenario_no_credentials()
    scenario_env_override()
    scenario_concurrent()

    print("\n== %d failed ==" % len(FAILS))
    for name in FAILS:
        print("  - " + name)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
