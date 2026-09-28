# -*- coding: utf-8 -*-
"""
OAuth 一次性授权脚本(双栈监听版)
流程:本地 8899 端口同时监听 IPv4(127.0.0.1)和 IPv6(::1)
     -> 自动打开浏览器 -> 飞书扫码/确认 -> 换取用户令牌存 token.json

前置条件:开发者后台重定向 URL 已添加 http://localhost:8899/callback
运行:python oauth_setup.py

如果浏览器跳转到 localhost:8899 后显示"无法访问":
  直接复制浏览器地址栏里的完整 URL(里面有 ?code=...)发给协助你的人工智
  能/或粘贴到本脚本同目录下的 code.txt,再运行:python oauth_setup.py --from-file
"""
import json
import os
import secrets
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_PATH = os.path.join(BASE_DIR, "token.json")
REDIRECT_URI = "http://localhost:8899/callback"
AUTH_URL = ("https://accounts.feishu.cn/open-apis/authen/v1/authorize"
            "?client_id={app_id}&redirect_uri={uri}&response_type=code&state={state}"
            "&scope={scope}")
# 显式声明需要的用户侧权限:日历读取 + 通讯录基础信息
SCOPE = "calendar:calendar:readonly contact:user.base:readonly"
TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"

cfg = json.load(open(os.path.join(BASE_DIR, "config.json"), encoding="utf-8"))
APP_ID, APP_SECRET = cfg["app_id"], cfg["app_secret"]


def post_json(url: str, body: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def exchange_code(code: str) -> None:
    """用授权码换令牌并保存,随后列出可见日历"""
    resp = post_json(TOKEN_URL, {
        "grant_type": "authorization_code",
        "client_id": APP_ID,
        "client_secret": APP_SECRET,
        "code": code,
        "redirect_uri": REDIRECT_URI,
    })
    print("令牌接口原始返回:", json.dumps(resp, ensure_ascii=False)[:600])
    if resp.get("error"):
        print("换取令牌失败。")
        return
    data = resp if "access_token" in resp else resp.get("data", {})
    if not data.get("access_token"):
        print("返回里没有 access_token。")
        return

    now = time.time()
    token = {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", ""),
        "expires_at": now + data.get("expires_in", 7200),
        "refresh_expires_at": now + data.get("refresh_token_expires_in", 2592000),
        "name": data.get("name", ""),
        "scope": data.get("scope", ""),
    }
    with open(TOKEN_PATH, "w", encoding="utf-8") as f:
        json.dump(token, f, ensure_ascii=False, indent=2)
    print(f"授权成功!授权身份:{token['name']},令牌已保存到 token.json")

    from feishu_client import FeishuClient
    client = FeishuClient(APP_ID, APP_SECRET)
    print("\n你的账号当前可见的日历:")
    for c in client.user_calendars():
        print("  -", c.get("summary"), "|", c.get("calendar_id"))
    print("\n如果列表里没有 210 会议室,请先在飞书客户端订阅该会议室日历,"
          "之后直接跑 bridge.py 即可(无需重新授权)。")


def serve_and_wait() -> None:
    shared = {"code": None}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(q.query)
            if q.path == "/callback" and "code" in params:
                shared["code"] = params["code"][0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write("<h2>授权成功</h2><p>可以关闭此页面,回到终端查看结果。</p>".encode("utf-8"))
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args):
            pass

    class V6HTTPServer(HTTPServer):
        address_family = socket.AF_INET6

    servers = []
    for cls, addr in ((HTTPServer, ("127.0.0.1", 8899)), (V6HTTPServer, ("::1", 8899))):
        try:
            servers.append(cls(addr, Handler))
        except OSError as e:
            print(f"监听 {addr} 失败: {e}")
    if not servers:
        print("8899 端口无法监听,请检查端口占用后重试。")
        return
    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()

    state = secrets.token_hex(8)
    url = AUTH_URL.format(app_id=APP_ID,
                          uri=urllib.parse.quote(REDIRECT_URI, safe=""),
                          state=state,
                          scope=urllib.parse.quote(SCOPE, safe=""))
    print("即将打开浏览器进行飞书授权。若没有自动打开,请手动访问:\n", url, "\n")
    print("提示:授权后如果页面显示\"无法访问\",把地址栏的完整 URL 复制发出来即可。")
    webbrowser.open(url)

    for _ in range(180):                      # 最多等 3 分钟
        if shared["code"]:
            break
        time.sleep(1)
    for s in servers:
        s.shutdown()
    if not shared["code"]:
        print("超时未收到授权回调。")
        return
    exchange_code(shared["code"])


def main():
    if "--from-file" in sys.argv:
        # 兜底:浏览器打不开本地回调时,把地址栏 URL 存进 code.txt 手动喂给脚本
        raw = open(os.path.join(BASE_DIR, "code.txt"), encoding="utf-8").read().strip()
        params = urllib.parse.parse_qs(urllib.parse.urlparse(raw).query)
        exchange_code(params["code"][0])
    else:
        serve_and_wait()


if __name__ == "__main__":
    main()
