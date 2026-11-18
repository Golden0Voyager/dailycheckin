#!/usr/bin/env python3
"""华为云 CodeArts（码道）本机授权 —— 浏览器登录一次，换回临时 AK/SK 落盘。

流程（照 dsh-free-model / dsh-checkin-all，MIT）：
  1. 本地起回调服务 127.0.0.1:8791（占用则换随机端口），路径 /authentication
  2. 打开华为云登录页，service 参数带跳转地址与回调 URL
  3. 用户在浏览器登录授权 → 华为云把 secret 带回 http://127.0.0.1:<port>/authentication?secret=...
  4. 拿 ticket_id + secret 轮询 snap-manager/v1/login/ticket 换 AK/SK/security_token
  5. 写入 ~/.codearts/credentials.json（0600），不回显任何凭据值

security_token 是 STS 临时凭据，会过期；过期后重跑本脚本即可。
只在用户本机运行，不要也不需要在 Actions 里跑。
"""

import http.server
import json
import os
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid

try:
    import requests
except ImportError:
    print("需要 requests 库", file=sys.stderr)
    sys.exit(4)

HUAWEI_AUTH_BASE = "https://auth.huaweicloud.com/authui/login.html"
CODEARTS_LOGIN_BASE = "https://devcloud.cn-north-4.huaweicloud.com/doer/redirect"
CREDENTIAL_ENDPOINT = "https://snap-access.cn-north-4.myhuaweicloud.com/snap-manager/v1/login/ticket"
PLUGIN_NAME = "snap_jetbrains"
PLUGIN_VERSION = "26.3.3"
CALLBACK_PATH = "/authentication"
DEFAULT_PORT = 8791
LOGIN_TIMEOUT_MS = 180_000
POLL_ATTEMPTS = 120
POLL_INTERVAL = 1
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "*",
}
CRED_PATH = os.path.expanduser("~/.codearts/credentials.json")


def build_login_url(port, ticket_id):
    callback_url = f"http://127.0.0.1:{port}{CALLBACK_PATH}"
    redirect_url = (
        f"{CODEARTS_LOGIN_BASE}?IdeaType=jetbrains"
        f"&auth_callback_url={urllib.parse.quote(callback_url, safe='')}"
        f"&plugin-name={PLUGIN_NAME}&plugin-version={PLUGIN_VERSION}"
        f"&ticket_id={urllib.parse.quote(ticket_id, safe='')}"
    )
    return f"{HUAWEI_AUTH_BASE}?service={urllib.parse.quote(redirect_url, safe='')}"


def parse_credential(data):
    """华为云有两种返回形状（credential / result），字段名也有大小写变体。"""
    if not isinstance(data, dict):
        return None
    for key in ("credential", "result"):
        obj = data.get(key)
        if not isinstance(obj, dict):
            continue
        if key == "credential":
            ak = obj.get("access") or obj.get("accessKeyId") or ""
            sk = obj.get("secret") or obj.get("secretAccessKey") or ""
            st = obj.get("securitytoken") or obj.get("securityToken") or ""
            exp = obj.get("expires_at") or obj.get("expiresAt") or obj.get("expiration") or ""
        else:
            ak = obj.get("accessKeyId") or obj.get("access_key_id") or ""
            sk = obj.get("secretAccessKey") or obj.get("secret_access_key") or ""
            st = obj.get("securityToken") or obj.get("security_token") or ""
            exp = obj.get("expiration") or obj.get("expiresAt") or ""
        if ak and st:
            cred = {
                "access_key_id": str(ak),
                "secret_access_key": str(sk),
                "security_token": str(st),
                "expires_at": str(exp),
            }
            if data.get("user_name"):
                cred["user_name"] = str(data["user_name"])
            return cred
    return None


def poll_for_credential(ticket_id, secret):
    url = f"{CREDENTIAL_ENDPOINT}?ticket_id={urllib.parse.quote(ticket_id, safe='')}&secret={urllib.parse.quote(secret, safe='')}"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "plugin-name": PLUGIN_NAME,
        "plugin-version": PLUGIN_VERSION,
    }
    for i in range(POLL_ATTEMPTS):
        if i:
            time.sleep(POLL_INTERVAL)
        try:
            resp = requests.get(url, headers=headers, timeout=20)
            if not resp.ok:
                continue
            data = resp.json()
        except (requests.RequestException, ValueError):
            continue
        cred = parse_credential(data)
        if cred:
            return cred
    raise TimeoutError("等华为云返回凭据超时（授权可能没完成），请重跑本脚本")


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        if not parsed.path.startswith(CALLBACK_PATH):
            self.send_error(404)
            return
        params = urllib.parse.parse_qs(parsed.query)
        secret = (params.get("secret") or [None])[0]
        if not secret:
            body = "缺少 secret，登录可能没走完，请重新运行脚本再授权一次".encode("utf-8")
            self.send_response(400)
            self._cors()
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        html = (
            '<!doctype html><meta charset="utf-8"><title>CodeArts 授权</title>'
            '<div style="font:15px/1.6 -apple-system,system-ui,sans-serif;max-width:420px;'
            'margin:80px auto;text-align:center"><h2 style="color:#0f9d58">✓ 已收到华为云授权</h2>'
            '<p style="color:#666">正在换取凭据，可以关闭这个页面，回到终端查看结果。</p></div>'
        )
        body = html.encode("utf-8")
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.server.received_secret.put(secret)


class CallbackServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def start_callback_server():
    for port in (DEFAULT_PORT, 0):
        try:
            srv = CallbackServer(("127.0.0.1", port), CallbackHandler)
            srv.received_secret = __import__("queue").Queue()
            return srv, srv.server_address[1]
        except OSError:
            continue
    raise RuntimeError("本地回调端口起不来")


def open_browser(url):
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys.platform == "win32":
            subprocess.Popen(["cmd", "/c", "start", "", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


def save_credential(cred):
    os.makedirs(os.path.dirname(CRED_PATH), exist_ok=True)
    with open(CRED_PATH, "w", encoding="utf-8") as f:
        json.dump(cred, f, ensure_ascii=False, indent=2)
    os.chmod(CRED_PATH, 0o600)
    return CRED_PATH


def main():
    ticket_id = str(uuid.uuid4())
    srv, port = start_callback_server()
    login_url = build_login_url(port, ticket_id)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()

    print("=" * 70)
    print("华为云 CodeArts 授权")
    print("=" * 70)
    print("即将打开浏览器。请在华为云页面登录并授权（最多等 3 分钟）。")
    print(f"如果浏览器没自动打开，请手动复制下面地址到浏览器：\n{login_url}")
    print("=" * 70, flush=True)
    opened = open_browser(login_url)
    if not opened:
        print("无法自动打开浏览器，请手动访问上面的地址。", flush=True)

    try:
        secret = srv.received_secret.get(timeout=LOGIN_TIMEOUT_MS / 1000)
    except __import__("queue").Empty:
        print("授权超时（3 分钟未完成），请重跑本脚本。", file=sys.stderr)
        srv.shutdown()
        sys.exit(4)
    finally:
        # 注意：拿到 secret 后还要继续轮询，所以 shutdown 放在凭据拿到之后
        pass

    print("已收到授权回调，正在换取临时凭据（最多 2 分钟）...", flush=True)
    try:
        cred = poll_for_credential(ticket_id, secret)
    except TimeoutError as e:
        print(str(e), file=sys.stderr)
        srv.shutdown()
        sys.exit(4)
    finally:
        srv.shutdown()

    path = save_credential(cred)
    print(f"✓ 授权成功。凭据已写入 {path}（权限 0600，不回显内容）")
    exp = cred.get("expires_at", "")
    print(f"  凭据有效期字段：{'有（' + exp[:19] + '...）' if exp else '服务端未下发（按 1 天估）'}")
    print("  接下来可用 python3 codearts_checkin.py --status 只读核对（不打印凭据）")


if __name__ == "__main__":
    main()
