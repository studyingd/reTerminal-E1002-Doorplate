#!/bin/sh
# 容器入口:三个服务各自带崩溃自动重启,日志全部走 stdout(docker logs 可看)
cd /app
mkdir -p logs out

if [ ! -f /app/config.json ]; then
  echo "ERROR: 缺少 /app/config.json —— 请用 -v 把服务器上的 config.json 挂载进来,例如:" >&2
  echo "  docker run -v /opt/sensecraft/config.json:/app/config.json ..." >&2
  exit 1
fi

run_loop() {
  while true; do
    "$@"
    echo "[entrypoint] $1 退出(码 $?),3 秒后重启..."
    sleep 3
  done
}

http_8000() {
  # 静态图服务。访问日志必须落到 logs/http8000.err.log:
  # webapp/server.py 从该文件解析设备心跳(每 60s 的 .ver 轮询),
  # 只走 stdout 的话管理平台上设备在线状态会永远显示离线
  while true; do
    python -u -m http.server 8000 --directory /app/out 2>> /app/logs/http8000.err.log
    echo "[entrypoint] http.server 8000 退出(码 $?),3 秒后重启..."
    sleep 3
  done
}

run_loop python -u bridge.py &
run_loop python -u webapp/server.py &
http_8000 &

trap 'kill $(jobs -p) 2>/dev/null' TERM INT
wait
