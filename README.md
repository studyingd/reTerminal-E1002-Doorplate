# Sensecraft · 飞书会议室水墨屏门牌

把飞书会议室日程 + 实时天气渲染成 800×480 图片，推给 Seeed reTerminal E 系列墨水屏门牌；
配套一个纯标准库的 Web 管理平台，可在浏览器里加会议室、绑日历、构建固件、Web Serial 烧录、OTA 升级。

整个后端零第三方 Web 框架：出图用 Pillow，其余全是 Python 标准库；固件用 ESPHome 生成，可在容器内编译。

---

## 它能做什么

- **飞书日程上屏**：应用身份直读会议室日历（含预约人、场次时间），拿不到详情时自动回退 freebusy（只显示「已预约」）。
- **实时天气**：Open-Meteo（免费、无需 Key），WMO 天气码转中文短文案，并用 Pillow 手绘线性天气图标；失败时复用上次成功值（落盘 `.last_temp`），屏上不会出现 `--`。
- **变化驱动刷屏**：只有「日期 + 预约信息」变化才重写 PNG 与 `.ver`，温度波动不刷屏，保护墨水屏寿命与观感。设备每 60s 只拉几十字节的 `.ver`，比对后才拉图。
- **重复日程展开**：飞书只返回重复事件的 master + RRULE，`feishu_client.expand_recurrence` 自行按规则展开到当天（否则周期性会议不上屏）。
- **双配色输出**：`sign-<号>.png` 纯黑白（E1001 / 旧固件），`sign-<号>-color.png` RGB565（E1002 七色彩屏）。彩屏绿色底刻意压到 `(127,195,31)`，避免 E6 驱动量化落进 YELLOW 桶。
- **管理平台**（`http://<主机>:8899/`）：会议室增删、OAuth 绑定日历、实时状态、设备在线心跳、一键构建固件（SSE 实时进度 + 完整日志）、浏览器烧录。
- **设备在线状态**：解析 8000 端口静态服务的访问日志（设备的 `.ver` 轮询就是心跳），180s 内有心跳视为在线，并显示设备 IP。

---

## 架构与数据流

```
飞书开放平台                Open-Meteo
   │  会议室日历/freebusy        │ 气温+天气码
   ▼                            ▼
┌──────────────  bridge.py  (每 poll_seconds 一轮) ──────────────┐
│  feishu_client.py   weather.py                                │
│        └──► render_room_status.py (Pillow, 800×480)           │
│                ├─ out/sign-<号>.png        (黑白)             │
│                ├─ out/sign-<号>-color.png  (彩色 RGB565)      │
│                ├─ out/sign-<号>.ver        (内容签名)         │
│                ├─ out/rooms.json + out/<device>.html (Web 保底)│
└───────────────────────────┬───────────────────────────────────┘
                            ▼
        python -m http.server 8000 --directory out/   ← 墨水屏拉图
                            ▲  每 60s GET /sign-<号>.ver，变了才拉 PNG
                            │
                    ESPHome 固件 (ESP32-S3 + epaper_spi)
                            ▲  Web Serial 烧录 / OTA
                            │
        webapp/server.py :8899  管理平台 + /flash/* 烧录页 + 构建固件
```

容器内三个服务由 `entrypoint.sh` 拉起，各自带崩溃自动重启，日志全部走 stdout（`docker compose logs -f` 可看）：

| 服务 | 入口 | 端口 | 职责 |
|---|---|---|---|
| bridge | `bridge.py` | — | 拉数据 → 渲染 → 写 `out/` |
| 管理平台 | `webapp/server.py` | 8899 | 管理页、API、OAuth、构建固件、烧录静态页 |
| 图片服务 | `python -m http.server` | 8000 | 给设备提供 PNG / `.ver`，访问日志兼作心跳 |

---

## 目录结构

```
bridge.py                 定时轮询主循环（配置热加载，改完下一轮生效）
feishu_client.py          飞书客户端：token 缓存、freebusy、会议室日历事件
render_room_status.py     Pillow 渲染（布局与 room_status_template.html 逐像素对应）
weather.py                Open-Meteo 气温/天气，10 分钟缓存
oauth_setup.py            本机一次性 OAuth 授权脚本（token.json）
room_status_template.html Web 端动态页面模板（数据烤入作保底）
room_calendars.json       room_id → calendar_id 缓存
config.json               全部配置与密钥（不打进镜像，运行时挂载）
webapp/                   管理平台（server.py + index.html）
fonts/                    NotoSansSC.ttf（Linux 容器兜底字体，字重轴拨到 600）
firmware/                 每台设备的 ESPHome YAML（由 build_firmware.py 生成，含 Wi-Fi 密码，不入库）
flasher/
  build_firmware.py       由 config.json 生成 YAML → esphome compile → factory.bin + manifest
  ota_push.py             推 OTA 到设备 IP（ESPHome 原生协议，端口 3232，免 USB）
  serve_https.py          本机 HTTPS 静态服务（Web Serial 需要安全上下文）
  com4_capture.py         抓串口启动日志（排查 OTA 后刷屏用）
  devices.json            烧录平台的设备清单（构建时自动刷新，不入库）
  manifests/              esp-web-tools manifest（构建时自动生成，不入库）
  firmware/               factory.bin 产物（含 Wi-Fi 密码，不入库）
  vendor/                 esp-web-tools 组件（本地化，不依赖 CDN）
out/                      渲染产物（PNG/.ver/rooms.json/HTML）
logs/                     http8000.err.log（心跳源）、build-<device>.log
Dockerfile / docker-compose.yml / entrypoint.sh
start_services.ps1        Windows 本机一键起三服务（脱离终端，日志写 logs/）
DEPLOY.md                 服务器 Docker 部署与迁移步骤
```

---

## 快速开始

### 前提

1. **飞书自建应用**，按代码实际调用的接口开通对应权限（应用身份）：
   - `calendar/v4/freebusy/list` — 会议室忙闲（添加房间时的校验也走它）
   - `calendar/v4/calendars/<calendar_id>/events` — 会议室日历事件（含预约人）
   - `meeting_room/building/list`、`meeting_room/room/list` — 会议室目录（管理平台的「选择会议室」）
   - `contact/v3/users/<open_id>` — 预约人姓名
   - OAuth 用户授权 scope（代码里写死）：`calendar:calendar:readonly contact:user.base:readonly`
   - 安全设置 → 重定向 URL 加上 `http://<你的地址>:8899/oauth/callback`
2. **会议室在飞书后台已建好**，能拿到 `omm_` 开头的 `room_id`（管理平台的「会议室目录」可直接列出租户全部会议室）。
3. Python 3.12+，`pip install -r requirements.txt`（Pillow + esphome；只跑渲染不需要 esphome）。
4. 准备配置：

```bash
cp config.example.json config.json   # 填入 app_id/app_secret、Wi-Fi、image_base、rooms
```

> `config.json`（含密钥）以及生成物 `firmware/*.yaml`、`flasher/firmware/*.factory.bin`（两者内部都烤了
> Wi-Fi 密码与内网拉图地址）、`out/`、`logs/` 均在 `.gitignore` 里，不入库。
> 刚克隆的仓库里没有固件，进管理平台点「构建固件」即可从 `config.json` 重新生成。

### A. 本机跑（Windows 有现成脚本）

```powershell
.\start_services.ps1     # 停旧实例 → 起 bridge / webapp / http.server → 打印监听状态
```

macOS / Linux：

```bash
python bridge.py &
python webapp/server.py &
python -m http.server 8000 --directory out &
```

浏览器打开 `http://localhost:8899/`。

### B. Docker（推荐，含固件构建工具链）

```bash
docker compose build && docker compose up -d
docker compose logs -f
```

服务器部署、离线构建与镜像搬运（注意 `--platform linux/amd64`）、
迁移后重刷固件等完整步骤见 **[DEPLOY.md](DEPLOY.md)**。

---

## 配置 `config.json`

> ⚠️ 含飞书 `app_secret`、Wi-Fi 密码、SenseCraft api_key。**已在 `.gitignore` 与 `.dockerignore` 中排除**，不要提交到仓库；服务器上以 volume 挂载进容器。从 `config.example.json` 复制一份改。

| 字段 | 说明 |
|---|---|
| `app_id` / `app_secret` | 飞书自建应用凭证 |
| `poll_seconds` | bridge 轮询间隔，默认 60 |
| `location.latitude/longitude` | 天气坐标（默认深圳南山） |
| `image_base` | **设备能访问到的** 图片服务地址，如 `http://192.168.1.100:8000`。固件里的拉图 URL 由它生成，改完必须重新构建固件并 OTA/烧录 |
| `wifi_ssid` / `wifi_password` | 烤进固件的 Wi-Fi（2.4G） |
| `rooms[]` | `room_id`(omm_…)、`room_name`、`device`(screen-N)、`hw`(`e1001`/`e1002`，缺省 `e1002`)、可选 `calendar_id` |
| `calendar_bindings` | `room_id → calendar_id` 持久表：删房重加自动恢复绑定，不用重新授权 |
| `booker_aliases` | 日历 ID/账号 → 屏显名（如把招聘机器人日历显示成「招聘助手」） |
| `data_url` | Web 端动态 HTML 取数地址（可选） |

会议室号取自 `room_name`：优先匹配「数字+室」（`210会议室` → `210`），否则取最后一组数字。**固件与拉图地址都只认这个号**，所以换设备/改 device 名不用重建固件。

---

## 使用流程

### 1. 添加会议室

管理平台 → 填 `room_name` + `room_id`（或从「会议室目录」里选）→ 添加。
后端会用 freebusy 校验 `room_id` 有效性，`device` 自动按 `screen-N` 递增分配。
添加后**忙闲立刻生效**；绑定日历后才显示预约人。

### 2. 绑定会议室日历（显示预约人）

点该房间的「绑定日历」→ 跳飞书授权 → 回调页列出授权账号可见的日历 → 点「绑定」。
写入 `calendar_id` 与 `calendar_bindings`，约 1 分钟后预约人姓名上屏。

看不到目标会议室？先在飞书客户端**订阅该会议室日历**，再重新授权。
（也可以直接编辑 `config.json` 填 `calendar_id`，或用 `python oauth_setup.py` 走本机一次性授权。）

### 3. 构建固件

管理平台上点「构建固件」→ 容器内 `esphome compile`，SSE 实时推进度（ninja 的 `[N/M]` 映射到 10%~80%），
完整日志落 `logs/build-<device>.log`，可在线看 `/api/build_log?device=screen-8`。
首次构建要在线下载 ESP-IDF 工具链（几百 MB），之后缓存在 `esphome-cache` 卷。

命令行等价：

```bash
python flasher/build_firmware.py            # 全部
python flasher/build_firmware.py screen-8   # 单台
```

### 4. 烧录

浏览器 Web Serial **要求安全上下文**，纯 HTTP 的远程页面会被浏览器拒绝串口权限。两种办法：

- 服务器套 HTTPS（nginx/Traefik 反代 + 证书；`webapp/server.py` 检测到 `webapp/cert.pem` + `key.pem` 也会自动起 HTTPS）；
- 或本机隧道：`ssh -L 8899:localhost:8899 user@server`，再访问 `http://localhost:8899` 烧录。

管理页的「连接并烧录固件」用的是本地化的 esp-web-tools（`flasher/vendor/`），manifest 在 `flasher/manifests/`。

### 5. OTA 升级

```bash
python flasher/ota_push.py <设备IP> flasher/firmware/<name>.ota.bin
# 容器内构建时 ota bin 在 firmware/.esphome/build/<name>/build/
```

`.ver` 由内容签名决定，**迁移服务器/重启服务本身不会触发设备刷屏**。

---

## 管理平台 API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/rooms` | 房间列表 + 实时状态 + 固件就绪 + 在线心跳 |
| POST | `/api/rooms` | 添加会议室（校验 `room_id`，自动分配 `device`，恢复历史绑定） |
| DELETE | `/api/rooms/<device>` | 删除（绑定存入 `calendar_bindings` 以便恢复） |
| GET | `/api/room_directory` | 拉取租户全部会议室目录（缓存 5 分钟） |
| POST | `/api/build/<device>` | 触发固件构建 |
| GET | `/api/build_status` | 构建状态快照 |
| GET | `/api/build_events` | SSE 实时构建进度 |
| GET | `/api/build_log?device=` | 构建日志尾部 200KB |
| GET | `/oauth/start?device=` `/oauth/callback` | 飞书授权与日历绑定 |
| POST | `/oauth/bind` | 写入 `calendar_id` |
| GET | `/flash/*` | 烧录静态资源（esp-web-tools + manifest + bin） |

---

## 支持的硬件

| `hw` | 屏幕 | 显示驱动 | 取图 |
|---|---|---|---|
| `e1002`（缺省） | reTerminal E1002 · 7.3 寸 ACeP 七色彩屏 · 800×480 | `epaper_spi` / RGB565，需开 PSRAM | `sign-<号>-color.png` |
| `e1001` | reTerminal E1001 · 7.5 寸黑白 · 800×480 | `waveshare_epaper` model `7.50inv2` / BINARY | `sign-<号>.png` |

黑白屏房间必须在 `config.json` 里显式写 `hw: "e1001"`，否则刷错固件屏幕会**静默不显示**。
彩屏整屏刷新约 30~45s，是 ACeP 硬件特性，不是 bug。

## 固件要点（踩过的坑）

- **PSRAM 必须开**：`epaper_spi` 的 192KB 显存走外部 RAM（octal / 80MHz），否则挤爆内部 RAM 导致 Wi-Fi 四次握手超时。
- **`api.reboot_timeout: 0s`**：门牌没有 API 客户端，默认 15 分钟「无客户端就重启」会造成周期性重启 → 每次重启都刷屏，还可能把刷屏打断留白屏。
- **`online_image.update_interval: never`**：不自动拉图，由 60s 的 `.ver` 轮询触发；`on_download_finished` 里判断 `!cached`，304 时不重绘，避免 Wi-Fi 重连闪屏。
- 屏幕型号 `Seeed-reTerminal-E1002`（7.3 寸彩色 800×480）；黑白屏走 `sign-<号>.png` + BINARY。

---

## 日常运维

```bash
docker compose logs -f          # bridge / webapp 全在这里
docker compose down             # 停止（卷数据保留）
docker compose build && docker compose up -d   # 改代码后升级
```

- 8000 端口的访问日志在容器内 `/app/logs/http8000.err.log`，**设备在线状态由它解析**，别把它重定向掉。
- 改了 `config.json` 不用重启，bridge 每轮重新读；改了 `image_base` 要重新构建固件并 OTA。
- 渲染效果本地预览：`python render_room_status.py` → `out/room_status_color.png` / `out/room_status_bw.png`。
- 天气自测：`python weather.py`。

---

## 常见问题

| 现象 | 排查 |
|---|---|
| 屏上预约人显示「已预约」 | 该房间没绑 `calendar_id`，或应用没有会议室日历读取权限 → 走 OAuth 绑定 |
| 管理平台设备一直离线 | `logs/http8000.err.log` 是否有该设备的 `.ver` 请求；`image_base` 是否是设备可达地址 |
| 加会议室报 `room_id 校验失败` | 需要 `omm_` 开头的真实会议室 ID，且应用要有 freebusy 权限 |
| 构建固件卡住/失败 | 看 `/api/build_log?device=<device>`；首次需联网下载 ESP-IDF 工具链 |
| 浏览器不给串口权限 | 非安全上下文，见「烧录」一节的 HTTPS / SSH 隧道方案 |
| 屏上温度显示 `--` | 从未成功取到天气（外网不通）；恢复后会自动刷一屏 |

---

## 许可

[MIT](LICENSE)。字体 Noto Sans SC 为 SIL OFL 1.1，`flasher/vendor/` 的 esp-web-tools 为 Apache-2.0。
