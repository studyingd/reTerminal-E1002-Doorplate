# -*- coding: utf-8 -*-
"""抓取 COM4 串口日志 N 秒(默认 180),输出到文件。用于 OTA 重启后看启动日志。"""
import sys, time
import serial  # pyserial

PORT = "COM4"
BAUD = 115200
DURATION = int(sys.argv[1]) if len(sys.argv) > 1 else 180
OUT = sys.argv[2] if len(sys.argv) > 2 else r"E:\Project\Sensecraft\logs\com4_boot.log"

ser = serial.Serial(PORT, BAUD, timeout=1)
end = time.time() + DURATION
total = 0
with open(OUT, "wb") as f:
    while time.time() < end:
        b = ser.read(4096)
        if b:
            total += len(b)
            f.write(b)
            f.flush()
ser.close()
print(f"captured {total} bytes -> {OUT}")
