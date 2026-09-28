# Sensecraft 水墨屏门牌平台
# 含:bridge(出图) + 管理平台(8899) + 图片服务(8000) + ESPHome 固件构建
# config.json 不打进镜像(含密钥),运行时挂载,见 docker-compose.yml
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 项目代码与静态资源(不含 config.json,密钥运行时挂载)
COPY *.py ./
COPY room_status_template.html room_calendars.json ./
COPY fonts/ fonts/
COPY webapp/ webapp/
COPY flasher/ flasher/
# 固件 YAML 不打进镜像:它们由 flasher/build_firmware.py 从 config.json 生成,
# 内部烤了 Wi-Fi 密码与内网拉图地址。运行时由 docker-compose 挂载 ./firmware 提供。
RUN mkdir -p /app/logs /app/out /app/firmware

COPY entrypoint.sh /entrypoint.sh
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

EXPOSE 8899 8000
CMD ["/entrypoint.sh"]
