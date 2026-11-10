#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网关免登录（MP_GATEWAY_AUTOLOGIN）的冒烟测试（Windows 也能跑）。

背景：本应用有**两条**访问路径，都必须能免登录 ——
  1. 局域网/统一网关：浏览器 → fnOS 网关 → unix socket → gateway-proxy → 前端服务
  2. 直连前端端口：浏览器/飞牛 APP/FN Connect 映射 → 前端服务（不经过 gateway-proxy）

历史事故正是把注入放在 gateway-proxy 里：路径 2 的页面根本不经过代理，于是
"局域网能免登录、外网（飞牛 APP / FN Connect）不能"。修法是让**所有 HTML 的唯一
产地**（frontend-server.js）负责注入与凭据端点，代理只通过
`X-MP-Gateway-Prefix` 告诉它页面挂在哪个前缀下。

本测试因此把真实的 node 前端服务 + 真实的 gateway-proxy 一起拉起来跑，逐条验证：

  A 关闭（默认）：两条路径都不注入、端点不存在、一次登录都不发
  B 直连前端端口：注入脚本、端点为根路径、令牌/资源 Cookie/权限对象齐备、
    重复请求命中缓存、端点请求不落到后端
  C 走统一网关：HTML 同时含 polyfill 与免登录脚本，且端点带网关前缀；
    代理确实把前缀头交给了前端服务
  D 令牌将到期：端点自动续期（换到新令牌）
  E 代登录失败（401）：页面仍 200、available=false、退避且不把错误细节/密码吐给页面
  F 开关打开但缺凭据：不注入脚本（避免只多一个 404 请求）
  G 前缀头白名单：伪造的畸形前缀不得被拼进注入脚本
  H 静态资源与 SPA 回退：只有 index.html 注入，.js 不被注入
  I 并发端点请求：只登录一次

依赖：需要 node（前端服务）与 python（代理）。缺 node 时明确 SKIP 并返回 0。
"""
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROXY = ROOT / "app" / "bin" / "gateway-proxy.py"
FRONTEND = ROOT / "app" / "bin" / "frontend-server.js"
SANDBOX = ROOT / ".local-build" / "_smoke" / "autologin"
GATEWAY_PREFIX = "/app/moviepilot"
ENDPOINT = "/__mp/autologin.json"
APP_ENV_SAMPLE = "SUPERUSER=admin\nSUPERUSER_PASSWORD=pw-123456\n"

INDEX_HTML = (
    "<!doctype html><html><head><title>MoviePilot</title></head>"
    "<body><div id=\"app\"></div></body></html>"
)

FAILS = []
SKIPS = []


def check(label, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + label
          + (f"  <- {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(label)


def skip(label, detail=""):
    print("SKIP " + label + (f"  <- {detail}" if detail else ""))
    SKIPS.append(label)


def node_path():
    return shutil.which("node")


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def http_get(url, timeout=8, headers=None):
    req = urllib.request.Request(url, headers=headers or {"Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()
    except Exception:
        return 0, {}, b""


def make_jwt(exp_offset, user_id=7, name="admin"):
    """造一个形如 JWT 的令牌：服务端只读 payload 里的 exp，不验签。"""
    def b64(obj):
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return ".".join([
        b64({"alg": "HS256", "typ": "JWT"}),
        b64({"sub": str(user_id), "username": name, "iat": int(time.time()),
             "exp": int(time.time()) + exp_offset, "jti": uuid.uuid4().hex}),
        "sig",
    ])


# ---------------------------------------------------------------------------
# 假 MoviePilot 后端
# ---------------------------------------------------------------------------
STATE = {
    "logins": 0,
    "status": 200,
    "exp": 3600,
    "usernames": [],
    "paths": [],
    "prefix_headers": [],
}


def reset_state():
    STATE.update({"logins": 0, "status": 200, "exp": 3600, "usernames": [],
                  "paths": [], "prefix_headers": []})


class FakeBackend(BaseHTTPRequestHandler):
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
        STATE["prefix_headers"].append(self.headers.get("X-MP-Gateway-Prefix") or "")
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
        self._send(200, json.dumps({
            "access_token": make_jwt(STATE["exp"]),
            "token_type": "bearer",
            "super_user": True,
            "user_id": 7,
            "user_name": "admin",
            "avatar": "",
            "level": 2,
            "permissions": {"discovery": True, "search": True, "subscribe": True,
                            "manage": True, "admin": True, "features": {}},
        }), extra={"Set-Cookie": "MoviePilot=res-%d; Path=/; HttpOnly" % STATE["logins"]})

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        STATE["paths"].append(path)
        STATE["prefix_headers"].append(self.headers.get("X-MP-Gateway-Prefix") or "")
        if path == "/api/v1/system/ping":
            self._send(200, b'{"success":true}')
            return
        self._send(404, b'{"detail":"not found"}', extra={"X-Backend": "1"})


class FakeServer:
    def __init__(self):
        self.port = free_port()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), FakeBackend)
        self.httpd.daemon_threads = True

    def __enter__(self):
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        return False


# ---------------------------------------------------------------------------
# 起真实进程：node 前端服务（+ 可选的 python 网关代理）
# ---------------------------------------------------------------------------
PROXY_RUNNER = r'''
import sys, socket, importlib.util, http.server
proxy_path, proxy_port, frontend_port = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
if not hasattr(socket, "AF_UNIX"):
    socket.AF_UNIX = socket.AF_INET
spec = importlib.util.spec_from_file_location("gw", proxy_path)
gw = importlib.util.module_from_spec(spec)
sys.argv = ["gateway-proxy.py", "unused.sock", "127.0.0.1", str(frontend_port)]
spec.loader.exec_module(gw)
class H(gw.ProxyHandler):
    pass
http.server.ThreadingHTTPServer(("127.0.0.1", proxy_port), H).serve_forever()
'''


class Stack:
    """假后端 + 真实 node 前端服务 +（可选）真实 gateway-proxy。"""

    def __init__(self, app_env_text, env_extra=None, with_proxy=False):
        self.app_env_text = app_env_text
        self.env_extra = env_extra or {}
        self.with_proxy = with_proxy
        self.tmp = None
        self.backend = None
        self.fe = None
        self.proxy = None
        self.fe_port = free_port()
        self.proxy_port = free_port()
        self.fe_log = ""
        self.proxy_log = ""

    def __enter__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mp-autologin-"))
        (self.tmp / "app.env").write_text(self.app_env_text, encoding="utf-8")
        dist = self.tmp / "dist"
        (dist / "assets").mkdir(parents=True, exist_ok=True)
        (dist / "index.html").write_text(INDEX_HTML, encoding="utf-8")
        (dist / "assets" / "app.js").write_text("console.log(1);", encoding="utf-8")

        self.backend = FakeServer().__enter__()
        env = dict(os.environ)
        env.update({
            "CONFIG_DIR": str(self.tmp),
            "MP_FRONTEND_DIR": str(dist),
            "MP_BACKEND_HOST": "127.0.0.1",
            "MP_BACKEND_PORT": str(self.backend.port),
            "MP_FRONTEND_PORT": str(self.fe_port),
            "PYTHONIOENCODING": "utf-8",
        })
        for key in ("MP_GATEWAY_AUTOLOGIN", "MP_GATEWAY_AUTOLOGIN_USER",
                    "MP_GATEWAY_AUTOLOGIN_PASSWORD", "MP_APP_ENV"):
            env.pop(key, None)
        env.update(self.env_extra)

        SANDBOX.mkdir(parents=True, exist_ok=True)
        runner = SANDBOX / "proxy_runner.py"
        runner.write_text(PROXY_RUNNER, encoding="utf-8")

        self.fe_buf = []
        self.proxy_buf = []
        self.fe = subprocess.Popen(
            [node_path(), str(FRONTEND)], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace")
        self._drain(self.fe, self.fe_buf)
        if not self._wait(self.fe_port):
            return self
        if self.with_proxy:
            self.proxy = subprocess.Popen(
                [sys.executable, str(runner), str(PROXY), str(self.proxy_port),
                 str(self.fe_port)],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace")
            self._drain(self.proxy, self.proxy_buf)
            self._wait(self.proxy_port)
        return self

    @staticmethod
    def _drain(proc, buf):
        """边跑边收 stdout：日志断言要在进程存活期间做（退出后再读已经太晚）。"""
        def reader():
            try:
                for line in proc.stdout:
                    buf.append(line)
            except Exception:
                pass
        t = threading.Thread(target=reader, daemon=True)
        t.start()

    def fe_text(self):
        return "".join(getattr(self, "fe_buf", []))

    def _wait(self, port, timeout=25):
        deadline = time.time() + timeout
        while time.time() < deadline:
            status, _h, _b = http_get(f"http://127.0.0.1:{port}/__probe__", timeout=1)
            if status:
                return True
            time.sleep(0.15)
        return False

    def __exit__(self, *exc):
        for proc, attr in ((self.proxy, "proxy_log"), (self.fe, "fe_log")):
            if proc is None:
                continue
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except Exception:
                    proc.kill()
            try:
                setattr(self, attr, proc.stdout.read() or "")
                proc.stdout.close()
            except Exception:
                pass
        if self.backend:
            self.backend.__exit__()
        if self.tmp:
            shutil.rmtree(self.tmp, ignore_errors=True)
        return False

    @property
    def ok(self):
        return self.fe is not None and self.fe.poll() is None

    def fe_url(self, path):
        return f"http://127.0.0.1:{self.fe_port}{path}"

    def proxy_url(self, path):
        return f"http://127.0.0.1:{self.proxy_port}{path}"


def script_of(html):
    """取出注入的免登录脚本片段（找不到返回空串）。"""
    marker = "/* __mp_autologin */"
    i = html.find(marker)
    if i < 0:
        return ""
    j = html.find("</script>", i)
    return html[max(0, i - 8):j + 9]


# ---------------------------------------------------------------------------
# A. 关闭（默认）
# ---------------------------------------------------------------------------
def scenario_disabled():
    print("\n-- A. 关闭（默认）：两条路径都不得注入、不得登录 --")
    reset_state()
    with Stack(APP_ENV_SAMPLE, with_proxy=True) as st:
        check("A0 前端服务与代理都已就绪", st.ok, (st.fe_log or "")[-400:])
        for label, url in (("A1 直连端口 HTML 200", st.fe_url("/")),
                           ("A2 网关路径 HTML 200", st.proxy_url(GATEWAY_PREFIX + "/"))):
            status, _h, body = http_get(url)
            html = body.decode("utf-8", "replace")
            check(label, status == 200, f"status={status}")
            if label.startswith("A1"):
                check("A3 直连端口未注入免登录脚本", "__mp_autologin" not in html)
            else:
                check("A4 网关路径未注入免登录脚本", "__mp_autologin" not in html)
                check("A5 网关路径仍注入 polyfill（未破坏原能力）",
                      "window.fetch=function" in html)
        status, headers, body = http_get(st.fe_url(ENDPOINT))
        check("A6 直连端口端点为 404（未接管）", status == 404, f"status={status}")
        status, _h, body = http_get(st.proxy_url(GATEWAY_PREFIX + ENDPOINT))
        # 关闭时这个路径只是"一个前端里不存在的 .json"：前端服务按静态资源缺失回 404，
        # 且**不能**是免登录端点的 200 JSON。
        check("A7 网关路径端点未被接管（404，非 JSON 令牌）",
              status == 404 and b"available" not in body,
              f"status={status} body={body[:80]!r}")
        check("A8 一次登录都没发", STATE["logins"] == 0, f"logins={STATE['logins']}")
        check("A9 前端日志写明未启用", "未启用" in st.fe_text(), st.fe_text()[-300:])


# ---------------------------------------------------------------------------
# B. 直连前端端口（飞牛 APP / FN Connect 走的就是这条路）
# ---------------------------------------------------------------------------
def scenario_direct():
    print("\n-- B. 直连前端端口：注入 + 根路径端点 + 缓存 --")
    reset_state()
    with Stack(APP_ENV_SAMPLE + "MP_GATEWAY_AUTOLOGIN=1\n") as st:
        check("B0 前端服务已就绪", st.ok, (st.fe_log or "")[-400:])
        status, _h, body = http_get(st.fe_url("/"))
        html = body.decode("utf-8", "replace")
        check("B1 HTML 200", status == 200, f"status={status}")
        check("B2 已注入免登录脚本", "__mp_autologin" in html)
        check("B3 注入在 </head> 之前（早于 deferred 模块脚本）",
              html.index("__mp_autologin") < html.index("</head>"))
        check("B4 端点候选为根路径", 'E=["/__mp/autologin.json"]' in html, script_of(html))
        check("B5 脚本内不含密码", "pw-123456" not in html)

        status, headers, body = http_get(st.fe_url(ENDPOINT))
        payload = json.loads(body.decode("utf-8")) if body else {}
        check("B6 端点 200", status == 200, f"status={status}")
        check("B7 available=true", payload.get("available") is True, str(payload)[:200])
        check("B8 auth store 结构 {token,remember}",
              isinstance(payload.get("auth"), dict) and payload["auth"].get("token")
              and payload["auth"].get("remember") is True)
        user = payload.get("user") or {}
        check("B9 user store 关键字段",
              user.get("superUser") is True and user.get("userID") == 7
              and user.get("userName") == "admin", str(user)[:200])
        check("B10 权限对象补齐 manage/admin/features",
              user.get("permissions", {}).get("manage") is True
              and "features" in user.get("permissions", {}))
        check("B11 资源 Cookie 被转发（EventSource 只认它）",
              "MoviePilot=" in (headers.get("Set-Cookie") or ""),
              repr(headers.get("Set-Cookie")))
        check("B12 令牌不可缓存", "no-store" in (headers.get("Cache-Control") or ""),
              repr(headers.get("Cache-Control")))
        check("B13 用 app.env 的账号登录", STATE["usernames"][:1] == ["admin"],
              str(STATE["usernames"]))

        logins_before = STATE["logins"]
        token_before = payload["auth"]["token"]
        _s2, _h2, body2 = http_get(st.fe_url(ENDPOINT))
        payload2 = json.loads(body2.decode("utf-8")) if body2 else {}
        check("B14 重复请求命中缓存（不再登录）", STATE["logins"] == logins_before,
              f"{logins_before} -> {STATE['logins']}")
        check("B15 令牌保持不变",
              payload2.get("auth", {}).get("token") == token_before)
        check("B16 端点请求没落到后端",
              ENDPOINT not in STATE["paths"], str(STATE["paths"]))
        check("B17 启动日志写明已启用（可排障）",
              "已启用" in st.fe_text(), st.fe_text()[-300:])


# ---------------------------------------------------------------------------
# C. 走统一网关：前缀由代理交给前端服务
# ---------------------------------------------------------------------------
def scenario_gateway():
    print("\n-- C. 统一网关路径：polyfill + 免登录共存，端点带前缀 --")
    reset_state()
    with Stack(APP_ENV_SAMPLE + "MP_GATEWAY_AUTOLOGIN=1\n", with_proxy=True) as st:
        check("C0 前端服务与代理都已就绪", st.ok, (st.fe_log or "")[-400:])
        status, _h, body = http_get(st.proxy_url(GATEWAY_PREFIX + "/"))
        html = body.decode("utf-8", "replace")
        check("C1 HTML 200", status == 200, f"status={status}")
        check("C2 polyfill 与免登录脚本共存",
              "window.fetch=function" in html and "__mp_autologin" in html)
        # 带前缀访问时**首选**前缀地址，根路径只作兜底：网关那条路上只有前缀形式存在
        check("C3 端点候选首选网关前缀",
              f'E=["{GATEWAY_PREFIX}{ENDPOINT}","{ENDPOINT}"]' in html, script_of(html))
        # 前缀是通过请求头交给前端服务的：用一条 /api 请求让假后端把收到的头记下来
        http_get(st.proxy_url(GATEWAY_PREFIX + "/api/v1/system/ping"))
        check("C4 代理把前缀头交给了前端/后端",
              GATEWAY_PREFIX in STATE["prefix_headers"], str(STATE["prefix_headers"]))

        status, _h, body = http_get(st.proxy_url(GATEWAY_PREFIX + ENDPOINT))
        payload = json.loads(body.decode("utf-8")) if body else {}
        check("C5 带前缀访问端点也能拿到令牌",
              status == 200 and payload.get("available") is True,
              f"status={status} {str(payload)[:160]}")
        check("C6 端点请求没落到后端",
              ENDPOINT not in STATE["paths"], str(STATE["paths"]))


# ---------------------------------------------------------------------------
# D. 令牌将到期 -> 续期
# ---------------------------------------------------------------------------
def scenario_refresh():
    print("\n-- D. 令牌将到期：端点自动续期 --")
    reset_state()
    STATE["exp"] = 120          # 远小于 10 分钟续期窗口
    with Stack(APP_ENV_SAMPLE + "MP_GATEWAY_AUTOLOGIN=1\n") as st:
        check("D0 前端服务已就绪", st.ok, (st.fe_log or "")[-400:])
        _s, _h, body = http_get(st.fe_url(ENDPOINT))
        first = json.loads(body.decode("utf-8")) if body else {}
        after_first = STATE["logins"]
        _s2, _h2, body2 = http_get(st.fe_url(ENDPOINT))
        second = json.loads(body2.decode("utf-8")) if body2 else {}
        check("D1 第一次就有令牌", first.get("available") is True)
        check("D2 第二次触发续期", STATE["logins"] > after_first,
              f"{after_first} -> {STATE['logins']}")
        check("D3 换到了新令牌",
              first.get("auth", {}).get("token") != second.get("auth", {}).get("token"))
        check("D4 续期后仍 available", second.get("available") is True)


# ---------------------------------------------------------------------------
# E. 代登录失败
# ---------------------------------------------------------------------------
def scenario_login_failed():
    print("\n-- E. 代登录失败（401）：降级登录页 + 退避 --")
    reset_state()
    STATE["status"] = 401
    with Stack(APP_ENV_SAMPLE + "MP_GATEWAY_AUTOLOGIN=1\n") as st:
        check("E0 前端服务已就绪", st.ok, (st.fe_log or "")[-400:])
        status, _h, body = http_get(st.fe_url("/"))
        check("E1 页面仍然 200（绝不阻断登录）", status == 200, f"status={status}")
        check("E2 脚本仍注入（可用与否由端点决定）",
              "__mp_autologin" in body.decode("utf-8", "replace"))
        _s, _h, body = http_get(st.fe_url(ENDPOINT))
        payload = json.loads(body.decode("utf-8")) if body else {}
        check("E3 available=false", payload.get("available") is False, str(payload))
        check("E4 不把后端错误细节透给页面",
              "bad credentials" not in json.dumps(payload), str(payload))
        check("E5 响应里不含密码", "pw-123456" not in json.dumps(payload))
        first = STATE["logins"]
        time.sleep(2.5)   # 预热若没停手，这段时间至少还要再撞 1 次
        http_get(st.fe_url(ENDPOINT))
        check("E6 失败后退避 + 预热停手（不会狂撞登录接口）",
              STATE["logins"] == first, f"first={first} now={STATE['logins']}")
        check("E7 日志写明失败原因与重试间隔",
              "代登录失败" in st.fe_text(), st.fe_text()[-300:])


# ---------------------------------------------------------------------------
# F. 开关打开但缺凭据
# ---------------------------------------------------------------------------
def scenario_no_credentials():
    print("\n-- F. 开关打开但缺凭据：不注入脚本 --")
    reset_state()
    with Stack("SUPERUSER=admin\nMP_GATEWAY_AUTOLOGIN=1\n") as st:
        check("F0 前端服务已就绪", st.ok, (st.fe_log or "")[-400:])
        _s, _h, body = http_get(st.fe_url("/"))
        check("F1 未注入脚本", "__mp_autologin" not in body.decode("utf-8", "replace"))
        status, _h, _b = http_get(st.fe_url(ENDPOINT))
        check("F2 端点未接管（404）", status == 404, f"status={status}")
        check("F3 没有发出登录请求", STATE["logins"] == 0, f"logins={STATE['logins']}")
        check("F4 日志写明缺凭据", "缺少可用凭据" in st.fe_text(),
              st.fe_text()[-300:])


# ---------------------------------------------------------------------------
# G. 伪造前缀头不得注入脚本
# ---------------------------------------------------------------------------
def scenario_bad_prefix():
    print("\n-- G. 前缀头白名单：畸形前缀不得拼进脚本 --")
    reset_state()
    with Stack(APP_ENV_SAMPLE + "MP_GATEWAY_AUTOLOGIN=1\n") as st:
        check("G0 前端服务已就绪", st.ok, (st.fe_log or "")[-400:])
        evil = '"/><script>alert(1)</script>'
        status, _h, body = http_get(st.fe_url("/"),
                                    headers={"X-MP-Gateway-Prefix": evil})
        html = body.decode("utf-8", "replace")
        check("G1 HTML 仍 200", status == 200, f"status={status}")
        check("G2 注入的候选地址回退到根路径",
              'E=["/__mp/autologin.json"]' in html, script_of(html))
        check("G3 恶意前缀没被拼进页面", "alert(1)" not in html)
        # 合法前缀仍然生效，证明白名单不是把功能一起挡掉
        _s, _h, body = http_get(
            st.fe_url("/"), headers={"X-MP-Gateway-Prefix": GATEWAY_PREFIX})
        check("G4 合法前缀仍被采纳",
              f'E=["{GATEWAY_PREFIX}{ENDPOINT}","{ENDPOINT}"]' in body.decode("utf-8", "replace"),
              script_of(body.decode("utf-8", "replace")))


# ---------------------------------------------------------------------------
# H. 只注入 HTML
# ---------------------------------------------------------------------------
def scenario_scope():
    print("\n-- H. 注入范围：只有 index.html / SPA 回退，静态资源不注入 --")
    reset_state()
    with Stack(APP_ENV_SAMPLE + "MP_GATEWAY_AUTOLOGIN=1\n") as st:
        check("H0 前端服务已就绪", st.ok, (st.fe_log or "")[-400:])
        status, _h, body = http_get(st.fe_url("/dashboard"))
        html = body.decode("utf-8", "replace")
        check("H1 SPA 回退也注入", status == 200 and "__mp_autologin" in html,
              f"status={status}")
        status, _h, body = http_get(st.fe_url("/assets/app.js"))
        js = body.decode("utf-8", "replace")
        check("H2 静态 js 不注入", status == 200 and "__mp_autologin" not in js,
              f"status={status}")
        status, headers, _b = http_get(st.fe_url("/assets/app.js"))
        check("H3 js 缓存策略未被改动",
              "immutable" in (headers.get("Cache-Control") or ""),
              repr(headers.get("Cache-Control")))
        status, headers, _b = http_get(st.fe_url("/"))
        check("H4 index.html 仍是 no-store",
              "no-store" in (headers.get("Cache-Control") or ""),
              repr(headers.get("Cache-Control")))


# ---------------------------------------------------------------------------
# I. 并发端点请求
# ---------------------------------------------------------------------------
def scenario_concurrent():
    print("\n-- I. 并发端点请求：只登录一次 --")
    reset_state()
    with Stack(APP_ENV_SAMPLE + "MP_GATEWAY_AUTOLOGIN=1\n") as st:
        check("I0 前端服务已就绪", st.ok, (st.fe_log or "")[-400:])
        before = STATE["logins"]
        results = []

        def hit():
            status, _h, body = http_get(st.fe_url(ENDPOINT))
            try:
                results.append((status, json.loads(body.decode("utf-8"))))
            except Exception:
                results.append((status, {}))

        threads = [threading.Thread(target=hit) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        delta = STATE["logins"] - before
        check("I1 四个并发请求最多只触发一次登录", delta <= 1, f"logins+={delta}")
        check("I2 全部返回 200",
              all(s == 200 for s, _ in results), str([s for s, _ in results]))
        check("I3 拿到的是同一个令牌",
              len({p.get("auth", {}).get("token") for _s, p in results}) == 1,
              str([p.get("auth", {}).get("token") for _s, p in results])[:120])
        check("I4 没有出现 in_progress 降级",
              all(p.get("available") is True for _s, p in results),
              str([p.get("reason") for _s, p in results]))


def scenario_contract():
    """静态契约：钉住"注入必须在前端服务、前缀必须由代理给"这条接线。

    这正是本次要修的回归点：注入只要被挪回 gateway-proxy，直连前端端口的访问
    （飞牛 APP、FN Connect 映射到应用端口）就会重新变回"必须登录"。
    """
    print("\n-- J. 接线契约（防止注入点被挪回代理）--")
    fe_src = FRONTEND.read_text(encoding="utf-8")
    proxy_src = PROXY.read_text(encoding="utf-8")
    check("J1 前端服务定义免登录端点", "AUTOLOGIN_ENDPOINT" in fe_src
          and '"/__mp/autologin.json"' in fe_src)
    check("J2 前端服务在 index.html 上注入", "injectAutologin" in fe_src
          and "sendIndexHtml" in fe_src)
    check("J3 SPA 回退也走同一条注入路径",
          fe_src.count("sendIndexHtml(") >= 3, str(fe_src.count("sendIndexHtml(")))
    check("J4 前端服务读 app.env（账号密码来源）",
          "SUPERUSER_PASSWORD" in fe_src and "readAppEnv" in fe_src)
    check("J5 代理侧不得再注入免登录脚本（否则两条路径行为会分叉）",
          "__mp_autologin" not in proxy_src and "AUTOLOGIN" not in proxy_src)
    check("J6 代理必须把网关前缀交给前端服务",
          'headers["X-MP-Gateway-Prefix"] = GATEWAY_PREFIX' in proxy_src)
    check("J7 前端服务对前缀做白名单校验",
          "GATEWAY_PREFIX_RE" in fe_src and "/^\\/app\\/[A-Za-z0-9._-]+$/" in fe_src)
    check("J8 端点响应不可缓存",
          'no-store' in fe_src and "Cache-Control" in fe_src)


def main():
    print("== 网关免登录（MP_GATEWAY_AUTOLOGIN）冒烟测试 ==")
    if not PROXY.exists() or not FRONTEND.exists():
        check("gateway-proxy.py / frontend-server.js 存在", False,
              f"{PROXY} / {FRONTEND}")
        return 1
    if not node_path():
        skip("全部端到端场景", "未找到 node")
        print("\n== 0 failed（有 SKIP） ==")
        return 0

    scenario_contract()
    scenario_disabled()
    scenario_direct()
    scenario_gateway()
    scenario_refresh()
    scenario_login_failed()
    scenario_no_credentials()
    scenario_bad_prefix()
    scenario_scope()
    scenario_concurrent()

    print("\n== %d failed%s ==" % (len(FAILS),
                                   f", {len(SKIPS)} skipped" if SKIPS else ""))
    for name in FAILS:
        print("  - " + name)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
