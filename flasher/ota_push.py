# -*- coding: utf-8 -*-
"""通过 ESPHome 原生 OTA 协议(端口 3232)推送固件,免 USB。
协议实现对照 esphome/espota2.py(2026.7.4)。仅支持无 OTA 密码的设备。
用法:python ota_push.py <ip> <ota.bin 路径>
"""
import hashlib
import socket
import struct
import sys
import time

MAGIC = bytes([0x6C, 0x26, 0xF7, 0x5C, 0x45])
RESP_OK, RESP_REQ_AUTH, RESP_REQ_SHA = 0x00, 0x01, 0x02
RESP_AUTH_OK, RESP_PREPARE_OK = 0x41, 0x42
RESP_MD5_OK, RESP_RECV_OK, RESP_END_OK = 0x43, 0x44, 0x45
RESP_SUPPORTS_COMP, RESP_FEATURE_FLAGS, RESP_CHUNK_OK = 0x46, 0x48, 0x47
FEAT_COMPRESS, FEAT_SHA, FEAT_EXT = 0x01, 0x02, 0x04
BLOCK = 8192


def recv_exact(sock, n, what):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise RuntimeError(f"设备断开连接({what})")
        buf += chunk
    return buf


def expect(sock, code, what):
    b = recv_exact(sock, 1, what)[0]
    if b >= 0x80:
        raise RuntimeError(f"设备返回错误码 0x{b:02X}({what})")
    if b != code:
        raise RuntimeError(f"意外响应 0x{b:02X},期望 0x{code:02X}({what})")
    return b


def main():
    ip, path = sys.argv[1], sys.argv[2]
    data = open(path, "rb").read()
    print(f"固件 {path}:{len(data)} bytes,MD5 {hashlib.md5(data).hexdigest()}")

    sock = socket.create_connection((ip, 3232), timeout=20)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.sendall(MAGIC)
    _, version = recv_exact(sock, 2, "version")
    if version > 2:
        raise RuntimeError(f"不支持的 OTA 版本 {version}")
    print(f"OTA 版本:{version}")

    # 能力协商:不声明压缩支持,保持明文上传,实现最简单
    sock.sendall(bytes([FEAT_EXT]))
    feat = recv_exact(sock, 1, "features")[0]
    if feat == RESP_FEATURE_FLAGS:
        feat = recv_exact(sock, 1, "feature flags")[0]
    elif feat == RESP_SUPPORTS_COMP:
        feat = FEAT_COMPRESS
    else:
        feat = 0
    if feat & FEAT_COMPRESS:
        print("设备支持压缩,但本工具按明文上传(设备同样接受)")
    print(f"设备能力字节:0x{feat:02X}")

    auth = recv_exact(sock, 1, "auth")[0]
    if auth != RESP_AUTH_OK:
        raise RuntimeError(f"设备要求认证(0x{auth:02X}),本工具只支持无密码 OTA")
    print("认证通过(无密码)")

    sock.settimeout(90)
    sock.sendall(bytes([0x00]))                      # ota_type = app
    sock.sendall(struct.pack(">I", len(data)))       # 上传大小
    expect(sock, RESP_PREPARE_OK, "update prepare")
    sock.sendall(hashlib.md5(data).hexdigest().encode())
    expect(sock, RESP_MD5_OK, "md5 check")

    t0 = time.time()
    off = 0
    while off < len(data):
        chunk = data[off:off + BLOCK]
        sock.sendall(chunk)
        off += len(chunk)
        if version >= 2:
            expect(sock, RESP_CHUNK_OK, f"chunk@{off}")
        if off % (BLOCK * 16) == 0 or off >= len(data):
            print(f"  上传 {off}/{len(data)} ({off * 100 // len(data)}%)")
    print(f"上传完成,耗时 {time.time() - t0:.1f}s,等待设备落盘校验...")

    expect(sock, RESP_RECV_OK, "receive")
    expect(sock, RESP_END_OK, "update end")
    sock.sendall(bytes([RESP_OK]))
    print("OTA 成功,设备即将重启进入新固件")
    sock.close()


if __name__ == "__main__":
    main()
