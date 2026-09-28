# -*- coding: utf-8 -*-
"""
固件烧录平台的 HTTPS 静态服务(Web Serial 要求安全上下文)。
用法:
  1. 用 mkcert 之类的工具生成证书,放本目录:cert.pem / key.pem
     例: mkcert 192.168.x.x localhost 127.0.0.1
  2. python serve_https.py           # 默认 8443 端口
本机测试不需要它:http://localhost:8000 直接用 python -m http.server 即可
(localhost 本身就是安全上下文)。
"""
import http.server
import os
import ssl

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = 8443

cert = os.path.join(BASE_DIR, "cert.pem")
key = os.path.join(BASE_DIR, "key.pem")
if not (os.path.exists(cert) and os.path.exists(key)):
    raise SystemExit("缺少 cert.pem / key.pem,请先用 mkcert 生成证书(见文件头注释)")

os.chdir(BASE_DIR)
server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT),
                                         http.server.SimpleHTTPRequestHandler)
server.socket = ssl.wrap_socket(server.socket, certfile=cert, keyfile=key,
                                server_side=True)
print(f"固件烧录平台: https://0.0.0.0:{PORT}  (内网访问 https://<服务器IP>:{PORT})")
server.serve_forever()
