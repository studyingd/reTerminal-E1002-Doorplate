# -*- coding: utf-8 -*-
"""
水墨屏门牌管理平台(纯标准库,无依赖)
  /            管理页:会议室列表、添加会议室、OAuth 绑定、实时预约状态
  /api/rooms   GET 列表 / POST 添加 / DELETE 删除
  /oauth/start     跳飞书授权(?device=screen-N)
  /oauth/callback  回调:列出授权人可见日历,点选绑定
  /flash/*     固件烧录静态页(esp-web-tools)

运行:python server.py            (默认 9000 端口;目录下存在 cert.pem/key.pem 时自动启用 HTTPS)

注意:飞书后台的重定向 URL 需要登记为  http://<本机地址>:9000/oauth/callback
(之前调试用的 http://localhost:8899/callback 只在本机访问时有效)
"""
import json
import os
import re
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJ_DIR = os.path.dirname(BASE_DIR)
CONFIG_PATH = os.path.join(PROJ_DIR, "config.json")
ROOMS_JSON = os.path.join(PROJ_DIR, "out", "rooms.json")
# 静态图服务(python -m http.server 8000)的访问日志:设备每 60s 轮询
# sign-<label>.ver,这就是设备心跳;从中解析 IP 与最后在线时间
HTTP8000_LOG = os.path.join(PROJ_DIR, "logs", "http8000.err.log")
FLASHER_DIR = os.path.join(PROJ_DIR, "flasher")
OAUTH_TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
FEISHU_BASE = "https://open.feishu.cn"


# ---------------- 配置读写 ----------------
BUILD_STATE = {}      # device -> "building" | "done" | "error: ..."


def room_label(room: dict) -> str:
    m = re.search(r"(\d+)\s*室", room["room_name"])
    nums = re.findall(r"\d+", room["room_name"])
    return m.group(1) if m else (nums[-1] if nums else room.get("device", ""))


def firmware_info(room: dict) -> dict:
    label = room_label(room)
    manifest = os.path.join(FLASHER_DIR, "manifests", f"meeting-sign-{label}.json")
    ready = os.path.exists(manifest)
    dev = room.get("device")
    default = {"phase": "完成", "pct": 100, "state": "done"} if ready else \
              {"phase": "未构建", "pct": 0, "state": "none"}
    st = BUILD_STATE.get(dev, default)
    if isinstance(st, str):          # 兼容旧字符串状态
        st = {"phase": "构建中" if st == "building" else "未构建",
              "pct": 50 if st == "building" else 0, "state": st}
    return {
        "ready": ready and st["state"] == "done",
        "manifest": f"/flash/manifests/meeting-sign-{label}.json",
        "state": st["state"],
        "pct": st.get("pct", 0),
        "phase": st.get("phase", ""),
    }


# ---------------- 设备心跳(解析 8000 端口访问日志) ----------------
_MONTHS = {m: i + 1 for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"))}

# ::ffff:192.168.1.50 - - [20/Aug/2026 09:43:57] "GET /sign-<号>.ver HTTP/1.1" 200 -
_HB_RE = re.compile(
    r'^(\S+) - - \[(\d{2})/(\w{3})/(\d{4}) (\d{2}):(\d{2}):(\d{2})\]'
    r' "GET /sign-([\w-]+)\.(?:ver|png)\b')

_hb_cache = {"time": 0.0, "data": {}}


def device_heartbeats() -> dict:
    """每台门牌最近一次拉图/查版本的记录 {label: {"ip", "ts"}}。
    只读日志尾部 512KB(逆序找每个 label 的第一条),结果缓存 10s。"""
    if time.time() - _hb_cache["time"] < 10:
        return _hb_cache["data"]
    beats = {}
    try:
        size = os.path.getsize(HTTP8000_LOG)
        with open(HTTP8000_LOG, "rb") as f:
            f.seek(max(0, size - 512 * 1024))
            tail = f.read().decode("utf-8", "replace")
        for line in reversed(tail.splitlines()):
            m = _HB_RE.match(line)
            if not m:
                continue
            label = m.group(8)
            if label in beats:
                continue
            mon = _MONTHS.get(m.group(3))
            if not mon:
                continue
            ip = m.group(1)
            if ip.startswith("::ffff:"):
                ip = ip[7:]
            ts = time.mktime((int(m.group(4)), mon, int(m.group(2)),
                              int(m.group(5)), int(m.group(6)), int(m.group(7)),
                              0, 0, -1))
            beats[label] = {"ip": ip, "ts": ts}
    except OSError:
        pass
    _hb_cache.update(time=time.time(), data=beats)
    return beats


def net_status(room: dict) -> dict:
    """设备在线状态:设备每 60s 轮询一次 .ver,180s 内有心跳视为在线"""
    beat = device_heartbeats().get(room_label(room))
    if not beat:
        return {"online": False, "ip": None, "last_seen": None, "ago": None}
    ago = int(time.time() - beat["ts"])
    return {"online": ago <= 180, "ip": beat["ip"],
            "last_seen": time.strftime("%H:%M:%S", time.localtime(beat["ts"])),
            "ago": ago}


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def next_device_id(cfg: dict) -> str:
    nums = []
    for r in cfg["rooms"]:
        m = re.match(r"screen-(\d+)$", r.get("device", ""))
        if m:
            nums.append(int(m.group(1)))
    return f"screen-{(max(nums) + 1) if nums else 1}"


# ---------------- 飞书 HTTP(管理平台自用) ----------------
def feishu_tenant_token(cfg: dict) -> str:
    body = json.dumps({"app_id": cfg["app_id"],
                       "app_secret": cfg["app_secret"]}).encode("utf-8")
    req = urllib.request.Request(
        FEISHU_BASE + "/open-apis/auth/v3/tenant_access_token/internal",
        data=body, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))["tenant_access_token"]


def check_room_id(cfg: dict, room_id: str) -> tuple:
    """用 freebusy 验证 room_id 是否有效。返回 (ok, message)"""
    try:
        token = feishu_tenant_token(cfg)
        now = int(time.time())
        body = json.dumps({
            "time_min": time.strftime("%Y-%m-%dT00:00:00+08:00", time.localtime(now)),
            "time_max": time.strftime("%Y-%m-%dT00:00:00+08:00", time.localtime(now + 86400)),
            "room_id": room_id,
        }).encode("utf-8")
        req = urllib.request.Request(
            FEISHU_BASE + "/open-apis/calendar/v4/freebusy/list",
            data=body, method="POST")
        req.add_header("Content-Type", "application/json; charset=utf-8")
        req.add_header("Authorization", "Bearer " + token)
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if payload.get("code") == 0:
            return True, "room_id 验证通过"
        return False, f'飞书返回错误 {payload.get("code")}: {payload.get("msg")}'
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}"
    except Exception as e:
        return False, str(e)


_room_dir_cache = {"time": 0.0, "data": []}


def fetch_room_directory(cfg: dict) -> list:
    """拉取租户全部会议室目录(建筑物->房间),缓存 5 分钟。
    需要 calendar:room:readonly(应用身份)权限。"""
    if time.time() - _room_dir_cache["time"] < 300:
        return _room_dir_cache["data"]
    token = feishu_tenant_token(cfg)
    headers = {"Authorization": "Bearer " + token}

    def get(path):
        req = urllib.request.Request(FEISHU_BASE + path, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8")).get("data", {})

    buildings = get("/open-apis/meeting_room/building/list"
                    "?country_id=1&district_id=1").get("buildings", [])
    rooms = []
    for b in buildings:
        items = get(f"/open-apis/meeting_room/room/list"
                    f"?building_id={b['building_id']}&page_size=100").get("rooms", [])
        for r in items:
            rooms.append({
                "room_id": r["room_id"],
                "name": r.get("name", ""),
                "building": b.get("name", ""),
                "floor": r.get("floor_name", ""),
                "capacity": r.get("capacity", 0),
                "disabled": r.get("is_disabled", False),
            })
    _room_dir_cache.update(time=time.time(), data=rooms)
    return rooms


def oauth_exchange(code: str, redirect_uri: str, cfg: dict) -> dict:
    body = json.dumps({
        "grant_type": "authorization_code",
        "client_id": cfg["app_id"], "client_secret": cfg["app_secret"],
        "code": code, "redirect_uri": redirect_uri,
    }).encode("utf-8")
    req = urllib.request.Request(OAUTH_TOKEN_URL, data=body, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def list_user_calendars(user_token: str) -> list:
    url = FEISHU_BASE + "/open-apis/calendar/v4/calendars?page_size=50"
    req = urllib.request.Request(url)
    req.add_header("Authorization", "Bearer " + user_token)
    with urllib.request.urlopen(req, timeout=10) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return payload.get("data", {}).get("calendar_list", [])


# ---------------- 页面 ----------------
def page_shell(title: str, body: str) -> bytes:
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>{title}</title>
<style>
 body{{font-family:"Microsoft YaHei",sans-serif;background:#f4f5f7;color:#1c1c1e;padding:40px 20px}}
 .card{{max-width:640px;margin:0 auto;background:#fff;border-radius:12px;padding:24px;box-shadow:0 1px 3px rgba(0,0,0,.08)}}
 h1{{font-size:20px;margin-bottom:16px}}
 a.btn,button{{display:inline-block;background:#111;color:#fff;border:none;border-radius:8px;
   padding:9px 18px;font-size:14px;cursor:pointer;text-decoration:none}}
 .row{{padding:10px 4px;border-bottom:1px solid #eee;font-size:14px;display:flex;justify-content:space-between;align-items:center}}
 .muted{{color:#888;font-size:13px}}
</style></head><body><div class="card">{body}</div></body></html>""".encode("utf-8")


# ---------------- HTTP 处理 ----------------
class Handler(BaseHTTPRequestHandler):

    def _send(self, content: bytes, ctype="text/html; charset=utf-8", code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(content)))
        # 管理页/manifest 都要即时最新,不让浏览器缓存旧版本
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", code)

    def _base_url(self) -> str:
        # 反代(Traefik/nginx)场景:TLS 终结在代理层,后端拿到的是 http,
        # 以 X-Forwarded-Proto 为准,否则 OAuth 回调地址的 scheme 会错
        scheme = self.headers.get("X-Forwarded-Proto") or \
            ("https" if isinstance(self.request, ssl.SSLSocket) else "http")
        return f"{scheme}://{self.headers.get('Host', 'localhost')}"

    def log_message(self, *a):
        pass

    # ---------- GET ----------
    def do_GET(self):
        q = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(q.query)

        if q.path == "/" or q.path == "/index.html":
            with open(os.path.join(BASE_DIR, "index.html"), "rb") as f:
                self._send(f.read())

        elif q.path == "/api/rooms":
            cfg = load_config()
            live = {}
            if os.path.exists(ROOMS_JSON):
                live = json.load(open(ROOMS_JSON, encoding="utf-8")).get("rooms", {})
            out = []
            for r in cfg["rooms"]:
                out.append({
                    "device": r.get("device"),
                    "room_name": r.get("room_name"),
                    "room_id": r.get("room_id"),
                    "calendar_id": r.get("calendar_id"),
                    "bound": bool(r.get("calendar_id")),
                    "live": live.get(r.get("device")),
                    "firmware": firmware_info(r),
                    "net": net_status(r),
                })
            self._json({"rooms": out})

        elif q.path == "/api/room_directory":
            try:
                cfg = load_config()
                rooms = fetch_room_directory(cfg)
                added = {r["room_id"] for r in cfg["rooms"]}
                for r in rooms:
                    r["added"] = r["room_id"] in added
                self._json({"ok": True, "rooms": rooms})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)

        elif q.path == "/api/build_status":
            self._json({"ok": True, "builds": BUILD_STATE})

        elif q.path == "/api/build_log":
            # 某设备最近一次构建的完整日志(尾部 200KB)
            dev = dict(urllib.parse.parse_qsl(q.query)).get("device", "")
            if not re.fullmatch(r"[\w-]+", dev):
                self._json({"ok": False, "error": "device 参数不合法"}, 400)
                return
            path = os.path.join(PROJ_DIR, "logs", f"build-{dev}.log")
            try:
                with open(path, "rb") as f:
                    f.seek(0, 2)
                    size = f.tell()
                    f.seek(max(0, size - 200 * 1024))
                    body = f.read().decode("utf-8", "replace")
            except OSError:
                self._json({"ok": False, "error": f"{dev} 还没有构建日志"}, 404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

        elif q.path == "/api/build_events":
            # SSE:服务端主动推送构建进度,前端实时更新、无需刷新页面
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            last = None
            idle = 0
            try:
                while True:
                    snapshot = json.dumps(BUILD_STATE, ensure_ascii=False, sort_keys=True)
                    if snapshot != last:
                        self.wfile.write(f"data: {snapshot}\n\n".encode("utf-8"))
                        last = snapshot
                        idle = 0
                    else:
                        idle += 1
                        if idle % 20 == 0:      # 心跳,防连接被中间层判死
                            self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    time.sleep(0.5)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return

        elif q.path == "/oauth/start":
            device = params.get("device", [""])[0]
            redirect_uri = self._base_url() + "/oauth/callback"
            url = ("https://accounts.feishu.cn/open-apis/authen/v1/authorize"
                   f"?client_id={load_config()['app_id']}"
                   f"&redirect_uri={urllib.parse.quote(redirect_uri, safe='')}"
                   f"&response_type=code&state={urllib.parse.quote(device)}"
                   f"&scope={urllib.parse.quote('calendar:calendar:readonly contact:user.base:readonly', safe='')}")
            self.send_response(302)
            self.send_header("Location", url)
            self.end_headers()

        elif q.path == "/oauth/callback":
            code = params.get("code", [""])[0]
            device = params.get("state", [""])[0]
            if not code:
                self._send(page_shell("授权失败", "<h1>授权失败:没有拿到 code</h1>"), code=400)
                return
            try:
                cfg = load_config()
                redirect_uri = self._base_url() + "/oauth/callback"
                token_resp = oauth_exchange(code, redirect_uri, cfg)
                user_token = token_resp.get("access_token") \
                    or token_resp.get("data", {}).get("access_token")
                if not user_token:
                    raise RuntimeError(json.dumps(token_resp, ensure_ascii=False)[:300])
                calendars = list_user_calendars(user_token)
            except Exception as e:
                self._send(page_shell("授权失败", f"<h1>授权换 token 失败</h1><p class='muted'>{e}</p>"), code=500)
                return

            rows = []
            for c in calendars:
                name = c.get("summary") or "(未命名)"
                cid = c.get("calendar_id")
                rows.append(
                    f"<div class='row'><span>{name}</span>"
                    f"<form method='POST' action='/oauth/bind' style='margin:0'>"
                    f"<input type='hidden' name='device' value='{device}'>"
                    f"<input type='hidden' name='calendar_id' value='{cid}'>"
                    f"<input type='hidden' name='calendar_name' value='{name}'>"
                    f"<button type='submit'>绑定</button></form></div>")
            body = (f"<h1>选择要绑定到「{device}」的会议室日历</h1>"
                    f"<p class='muted'>列表来自你授权账号可见的日历。没看到目标会议室?"
                    f"请先在飞书客户端订阅该会议室日历,然后重新授权。</p>"
                    + "".join(rows) +
                    f"<p style='margin-top:16px'><a class='btn' href='/'>返回管理页</a></p>")
            self._send(page_shell("选择日历", body))

        elif q.path.startswith("/flash/"):
            rel = q.path[len("/flash/"):] or "index.html"
            fpath = os.path.normpath(os.path.join(FLASHER_DIR, rel))
            if not fpath.startswith(FLASHER_DIR) or not os.path.isfile(fpath):
                self._send(b"not found", "text/plain", 404)
                return
            ctype = ("application/javascript" if fpath.endswith(".js") else
                     "application/json" if fpath.endswith(".json") else
                     "application/octet-stream" if fpath.endswith(".bin") else
                     "text/html; charset=utf-8" if fpath.endswith(".html") else
                     "application/octet-stream")
            with open(fpath, "rb") as f:
                self._send(f.read(), ctype)

        else:
            self._send(b"not found", "text/plain", 404)

    # ---------- POST ----------
    def do_POST(self):
        q = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8", "replace")

        if q.path == "/api/rooms":
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                self._json({"ok": False, "error": "JSON 格式错误"}, 400)
                return
            room_name = (data.get("room_name") or "").strip()
            room_id = (data.get("room_id") or "").strip()
            if not room_name or not room_id.startswith("omm_"):
                self._json({"ok": False, "error": "参数不完整:需要 room_name 和 omm_ 开头的 room_id"}, 400)
                return
            cfg = load_config()
            if any(r["room_id"] == room_id for r in cfg["rooms"]):
                self._json({"ok": False, "error": "该 room_id 已存在"}, 400)
                return
            ok, msg = check_room_id(cfg, room_id)
            if not ok:
                self._json({"ok": False, "error": f"room_id 校验失败:{msg}"}, 400)
                return
            device = next_device_id(cfg)
            entry = {"room_id": room_id, "room_name": room_name, "device": device}
            # 日历绑定按 room_id 持久保存:删除后重加自动恢复,不用重新授权
            saved_cid = cfg.get("calendar_bindings", {}).get(room_id)
            if saved_cid:
                entry["calendar_id"] = saved_cid
            cfg["rooms"].append(entry)
            save_config(cfg)
            # 按会议室号判断固件是否已构建过(固件与拉图地址都只认会议室号)
            label = room_label({"room_name": room_name})
            fw_ready = os.path.exists(os.path.join(
                FLASHER_DIR, "manifests", f"meeting-sign-{label}.json"))
            self._json({"ok": True, "device": device, "label": label,
                        "firmware_ready": fw_ready,
                        "binding_restored": bool(saved_cid),
                        "message": "已添加。忙闲即刻生效;绑定 calendar_id 后可显示预约人。"})

        elif q.path == "/oauth/bind":
            form = urllib.parse.parse_qs(raw)
            device = form.get("device", [""])[0]
            calendar_id = form.get("calendar_id", [""])[0]
            calendar_name = form.get("calendar_name", [""])[0]
            cfg = load_config()
            room = next((r for r in cfg["rooms"] if r.get("device") == device), None)
            if not room:
                self._send(page_shell("失败", "<h1>找不到该设备</h1>"), code=404)
                return
            room["calendar_id"] = calendar_id
            # 写入持久绑定表,以后删除重加可直接恢复
            cfg.setdefault("calendar_bindings", {})[room["room_id"]] = calendar_id
            save_config(cfg)
            body = (f"<h1>绑定成功</h1>"
                    f"<p>会议室「{room['room_name']}」已绑定日历 <b>{calendar_name}</b></p>"
                    f"<p class='muted'>下一轮刷新(约 1 分钟)后,预约人姓名就会出现在该房间的输出里。</p>"
                    f"<p style='margin-top:16px'><a class='btn' href='/'>返回管理页</a></p>")
            self._send(page_shell("绑定成功", body))

        elif q.path.startswith("/api/build/"):
            device = q.path[len("/api/build/"):]
            cfg = load_config()
            if not any(r.get("device") == device for r in cfg["rooms"]):
                self._json({"ok": False, "error": "设备不存在"}, 404)
                return
            st = BUILD_STATE.get(device)
            building = (st.get("state") == "building") if isinstance(st, dict) \
                else st == "building"
            if building:
                self._json({"ok": True, "status": "building"})
                return

            def work(dev):
                BUILD_STATE[dev] = {"phase": "构建中", "pct": 0, "state": "building"}
                # 构建全量日志落盘(logs/build-<device>.log),排查用
                # 在线查看:/api/build_log?device=<device>;容器内也可直接 cat
                os.makedirs(os.path.join(PROJ_DIR, "logs"), exist_ok=True)
                log_path = os.path.join(PROJ_DIR, "logs", f"build-{dev}.log")
                try:
                    log_file = open(log_path, "w", encoding="utf-8")
                except OSError:
                    log_file = None
                try:
                    proc = subprocess.Popen(
                        [sys.executable,
                         os.path.join(PROJ_DIR, "flasher", "build_firmware.py"), dev],
                        cwd=PROJ_DIR, stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
                    # 进度解析:ninja 的 [N/M] 是真实编译进度(主工程编译目标数百个),
                    # 映射到 10%~80%;bootloader 的小计数和链接行单独处理。
                    # 全部只增不减,避免 60/85 来回跳。
                    err_tail = ""
                    for line in proc.stdout:
                        if log_file:
                            log_file.write(line)
                            log_file.flush()
                        low = line.lower()
                        if "error" in low or "failed" in low or "exception" in low:
                            if line.strip():
                                err_tail = line.strip()
                        st = BUILD_STATE[dev]
                        m = re.search(r"\[(\d+)/(\d+)\]", line)
                        if m and int(m.group(2)) >= 100:      # 主工程的 ninja 进度
                            pct = 10 + int(m.group(1)) * 70 // int(m.group(2))
                            if pct > st["pct"]:
                                st.update(phase="编译中", pct=pct)
                        elif "Compiling" in line or "Building" in line:
                            if st["pct"] < 10:
                                st.update(phase="编译中", pct=10)
                        elif "Linking" in line and "bootloader" not in line:
                            if st["pct"] < 85:
                                st.update(phase="链接中", pct=85)
                        elif "固件已收集" in line or "factory.bin" in line:
                            if st["pct"] < 95:
                                st.update(phase="收集固件", pct=95)
                    proc.wait()
                    if proc.returncode == 0:
                        BUILD_STATE[dev] = {"phase": "完成", "pct": 100, "state": "done"}
                    else:
                        hint = f" · {err_tail[:150]}" if err_tail else ""
                        BUILD_STATE[dev] = {"phase": "失败", "pct": 0,
                                            "state": f"error: 退出码 {proc.returncode}{hint} · 详见 logs/build-{dev}.log"}
                except Exception as e:
                    BUILD_STATE[dev] = {"phase": "失败", "pct": 0, "state": f"error: {e}"}
                finally:
                    if log_file:
                        log_file.close()

            threading.Thread(target=work, args=(device,), daemon=True).start()
            self._json({"ok": True, "status": "building"})

        else:
            self._send(b"not found", "text/plain", 404)

    # ---------- DELETE ----------
    def do_DELETE(self):
        q = urllib.parse.urlparse(self.path)
        m = re.match(r"^/api/rooms/([\w-]+)$", q.path)
        if not m:
            self._json({"ok": False, "error": "bad path"}, 400)
            return
        device = m.group(1)
        cfg = load_config()
        # 删除前把绑定存入持久表,重加同房间时自动恢复,不用重新授权
        room = next((r for r in cfg["rooms"] if r.get("device") == device), None)
        if room and room.get("calendar_id"):
            cfg.setdefault("calendar_bindings", {})[room["room_id"]] = room["calendar_id"]
        before = len(cfg["rooms"])
        cfg["rooms"] = [r for r in cfg["rooms"] if r.get("device") != device]
        if len(cfg["rooms"]) == before:
            self._json({"ok": False, "error": "设备不存在"}, 404)
            return
        save_config(cfg)
        self._json({"ok": True})


def main():
    port = int(os.environ.get("PORT", "8899"))
    # Windows 的 SO_REUSEADDR 允许两个进程同时监听同一端口(请求被随机分配),
    # 重启时会留下跑旧代码的幽灵服务——启动前先探测,被占用就直接报错退出。
    import socket as _socket
    probe = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    probe.settimeout(0.5)
    occupied = probe.connect_ex(("127.0.0.1", port)) == 0
    probe.close()
    if occupied:
        print(f"端口 {port} 已被占用,请先停掉旧服务再启动。")
        sys.exit(1)
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    cert = os.path.join(BASE_DIR, "cert.pem")
    key = os.path.join(BASE_DIR, "key.pem")
    scheme = "http"
    if os.path.exists(cert) and os.path.exists(key):
        server.socket = ssl.wrap_socket(server.socket, certfile=cert, keyfile=key,
                                        server_side=True)
        scheme = "https"
    print(f"管理平台已启动:{scheme}://0.0.0.0:{port}")
    print("提示:飞书后台请登记重定向 URL -> "
          f"{scheme}://<本机地址>:{port}/oauth/callback")
    server.serve_forever()


if __name__ == "__main__":
    main()
