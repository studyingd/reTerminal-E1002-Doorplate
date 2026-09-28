# 服务器部署说明（Docker）

镜像内容：bridge（出图）+ 管理平台（8899）+ 图片服务（8000）+ ESPHome 2026.7.4（可在服务器上构建固件）。
config.json 含密钥，**不打进镜像**，运行时挂载（docker-compose.yml 已配好）。

## 1. 上传到服务器

需要上传的内容（都在本项目目录）：

| 文件/目录 | 说明 |
|---|---|
| `Dockerfile` `requirements.txt` `entrypoint.sh` `docker-compose.yml` | 构建与编排 |
| `*.py`（bridge/feishu_client/render_room_status/weather/oauth_setup） | 核心代码 |
| `room_status_template.html` `room_calendars.json` | 模板与日历绑定 |
| `fonts/` | 渲染字体 |
| `webapp/` | 管理平台 |
| `config.json` | 配置（含密钥，勿公开） |
| `flasher/` | 整个目录（含 vendor 烧录组件、firmware 固件、manifests；`.esphome`/`__pycache__` 可不传） |
| `firmware/` | 整个目录（YAML；`.esphome` 缓存目录不传） |

> 镜像**不**打包 `config.json`、`firmware/*.yaml`、`flasher/firmware/*.bin`（三者分别含飞书密钥、
> Wi-Fi 密码），均由 docker-compose 挂载宿主目录提供，见 `.dockerignore`。

```bash
rsync -av --exclude='__pycache__' --exclude='.esphome' --exclude='logs' --exclude='out' \
  ./ user@server:/opt/sensecraft/
```

## 2. 改 config.json（服务器上）

- `image_base` 改成设备能访问到的服务器地址，例如 `http://192.168.4.x:8000`
  （固件里的拉图 URL 由它生成；改完需在平台上重新构建固件并 OTA/烧录）
- `location` 已是深圳南山，按需调整

## 3. 构建并启动（服务器能联网时）

```bash
cd /opt/sensecraft
docker compose build        # 首次约几分钟(pip 装 esphome 较大)
docker compose up -d
docker compose logs -f      # 看三服务日志
```

浏览器开 `http://<服务器>:8899/`。

服务器**不能**联网装包时：在能联网的机器上构建，再把镜像搬过去。
注意**服务器架构**——x86 服务器在 Apple Silicon 上构建要加 `--platform linux/amd64`：

```bash
docker buildx build --platform linux/amd64 -t sensecraft:latest --load .
docker save sensecraft:latest | gzip > sensecraft-image.tar.gz   # scp 到服务器
docker load -i sensecraft-image.tar.gz && docker compose up -d
```

镜像里**没有** `config.json`、`firmware/*.yaml`、`flasher/firmware/*.bin`（分别含飞书密钥与
Wi-Fi 密码），全部由 `docker-compose.yml` 挂载宿主目录提供，见 `.dockerignore`。

## 4. 飞书控制台要改一处

自建应用 → 安全设置 → 重定向 URL，添加：
`http://<服务器>:8899/oauth/callback`
（服务器上要重新授权绑定日历时用；已绑定的房间不受影响，
绑定关系存在 config.json 的 `calendar_bindings` 里，删房重加自动恢复。）

## 5. 迁移后设备要重新刷一次固件

已部署设备固件里烤的是旧服务器地址（拉图 URL 是编译期写死的）：
image_base 改完后，平台上对每台设备「重新构建固件」，再 OTA
（`python flasher/ota_push.py <设备IP> flasher/firmware/<name>.ota.bin`，
容器内构建时 ota bin 在 firmware/.esphome/build/<name>/build/，
或浏览器 Web Serial 烧录）。.ver 由内容签名决定，迁移本身不会触发设备刷屏。

## 6. 固件构建与烧录

- 构建：平台上点「构建固件」即可在容器内编译（首次要在线下载
  ESP-IDF 工具链，约几百 MB，之后缓存在 esphome-cache 卷）。
- 烧录：浏览器 Web Serial 要求**安全上下文**——
  要么服务器套 HTTPS（nginx 反代 + 证书），要么在本机用
  `ssh -L 8899:localhost:8899` 隧道后访问 `http://localhost:8899` 烧录。
  纯 HTTP 的远程页面浏览器会拒绝串口权限。

## 7. 日常

- 日志：`docker compose logs -f`（bridge/webapp 的 stdout 全在这里；
  8000 端口访问日志在容器内 /app/logs/http8000.err.log，设备在线状态由它解析）
- 停止：`docker compose down`（卷数据保留）
- 升级：改代码后 `docker compose build && docker compose up -d`
