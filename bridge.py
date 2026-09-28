# -*- coding: utf-8 -*-
"""
桥接服务:定时 拉飞书数据 -> 渲染 -> 出图(每块屏一张)
运行:python bridge.py   (Ctrl+C 停止)
推送环节留了两个路线,见 push_image():
  路线 A:上传到 SenseCraft 云端相册(设备每 10 分钟自动拉取)
  路线 B:POST 到设备内网 IP(需刷自定义固件)
"""
import hashlib
import json
import os
import re
import time
import traceback
from datetime import datetime, timedelta

from feishu_client import (FeishuClient, FeishuError,
                           build_room_data, build_room_data_from_tenant_events)
from render_room_status import render
from weather import current_weather

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE_DIR, "out")
CAL_MAP_PATH = os.path.join(BASE_DIR, "room_calendars.json")
TEMPLATE_PATH = os.path.join(BASE_DIR, "room_status_template.html")

# 每间会议室上一轮的"屏显内容签名"(日期+预约信息)。
# 签名不变 -> PNG/.ver 不重写 -> 设备端比对版本后不刷屏,
# 避免墨水屏因温度等无关变化频繁刷新。
LAST_SIG = {}
# 最近一次成功获取的温度(落盘,重启不丢),天气抖动时兜底,避免屏上出现 "--"
TEMP_CACHE_PATH = os.path.join(BASE_DIR, ".last_temp")


def push_image(png_path: str, device: str) -> None:
    """TODO: 选定路线后实现。当前只把图保存在 out/ 目录。"""
    pass


def room_calendar_id(client: FeishuClient, room: dict) -> str:
    """会议室日历 ID:优先读 config 的 calendar_id,其次 room_calendars.json 缓存"""
    if room.get("calendar_id"):
        return room["calendar_id"]
    mapping = {}
    if os.path.exists(CAL_MAP_PATH):
        mapping = json.load(open(CAL_MAP_PATH, encoding="utf-8"))
    if room["room_id"] in mapping:
        return mapping[room["room_id"]]
    raise FeishuError(
        f'会议室 {room["room_name"]} 还没有 calendar_id:请在 config.json 里补 "calendar_id",'
        f"或先跑一次 OAuth 授权让系统自动发现")


def get_room_data(client: FeishuClient, room: dict, now, day_start, day_end, aliases=None):
    """应用身份直读会议室日历(含预约人,无 2 小时限制);失败回退 freebusy(仅忙闲)"""
    try:
        cid = room_calendar_id(client, room)
        events = client.tenant_events(cid, day_start, day_end)
        freebusy = client.freebusy(room["room_id"], day_start, day_end)
        data = build_room_data_from_tenant_events(events, freebusy, now, day_start, day_end,
                                                  aliases=aliases)
        return data, "预约人"
    except FeishuError as e:
        print(f"  日程详情不可用:{e}")
        print("  回退为 freebusy(预约人显示「已预约」)")
        return build_room_data(client.freebusy(room["room_id"], day_start, day_end), now), "仅忙闲"


def tick(client: FeishuClient, rooms: list, location: dict, aliases: dict = None,
         data_url: str = "") -> None:
    now = datetime.now().astimezone()   # 顶部时间/日期每轮自动取当前时刻,无需配置
    # 温度+天气现象:获取失败就复用最近一次成功值(落盘兜底),避免屏上长时间显示 "--"
    # 缓存文件格式 "27.1|多云"(竖线分隔,兼容旧的纯温度格式)
    temp_str, weather_str = None, ""
    try:
        t, weather_str = current_weather(location["latitude"], location["longitude"])
        temp_str = f"{t:.1f}"
    except Exception as e:
        print(f"  天气获取失败:{e}")
    if temp_str is not None:
        try:
            with open(TEMP_CACHE_PATH, "w", encoding="utf-8") as f:
                f.write(f"{temp_str}|{weather_str}")
        except OSError:
            pass
    else:
        cached, cached_w = None, ""
        try:
            with open(TEMP_CACHE_PATH, encoding="utf-8") as f:
                cached, _, cached_w = f.read().strip().partition("|")
        except OSError:
            pass
        temp_str = cached or "--"
        weather_str = cached_w
        if cached:
            print(f"  复用上次成功温度 {cached}°C {cached_w}")
    weekday_cn = "一二三四五六日"[now.weekday()]
    time_str = now.strftime("%H:%M")
    date_str = f"{now.strftime('%Y-%m-%d')}  周{weekday_cn}"

    display = {}                        # device -> 屏显数据(供 JSON/HTML 用)
    for room in rooms:
        try:
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            day_end = day_start + timedelta(days=1)
            data, source = get_room_data(client, room, now, day_start, day_end, aliases)
            # 会议室号:优先取"会议室"前的数字(210会议室→210),否则取最后一组数字
            m = re.search(r"(\d+)\s*室", room["room_name"])
            nums = re.findall(r"\d+", room["room_name"])
            label = m.group(1) if m else (nums[-1] if nums else room["room_name"])
            room_data = {
                "name": room["room_name"],
                "label": label,
                "status": data["status"],
                "temperature": temp_str,
                "weather": weather_str,
                "bookings": data["bookings"],
            }
            # 双图:sign-<label>.png 保持纯黑白(已部署的旧固件只认这个地址,
            # 黑白屏 BINARY 阈值转换无歧义);彩屏新固件拉 -color.png(RGB565)
            img = render(room_data, now, mode="color")
            img_bw = render(room_data, now, mode="bw")
            out_path = os.path.join(OUT_DIR, f'{room["device"]}.png')
            # 只在"日期 + 预约信息"变化时才重写 PNG 和版本文件:
            # 温度变化不触发刷屏(墨水屏寿命/观感),Web 端仍每分钟拿实时数据。
            # temp_str=="--"(从未成功过)计入签名,保证它变回真实值时必刷一屏
            sig = (date_str, temp_str == "--", tuple(tuple(b) for b in data["bookings"]))
            if LAST_SIG.get(label) != sig:
                LAST_SIG[label] = sig
                img.save(out_path)
                # 按会议室号再存两份:固件拉图只认会议室号,
                # 设备增删/换机后旧固件仍能拉到正确的图,无需重建。
                # .png 纯黑白(已部署旧固件/黑白屏),-color.png 给彩屏新固件
                img_bw.save(os.path.join(OUT_DIR, f"sign-{label}.png"))
                img.save(os.path.join(OUT_DIR, f"sign-{label}-color.png"))
                ver = hashlib.md5(repr(sig).encode("utf-8")).hexdigest()[:12]
                with open(os.path.join(OUT_DIR, f"sign-{label}.ver"), "w",
                          encoding="utf-8") as vf:
                    vf.write(ver)
                print(f"  内容变化,sign-{label}.png 已更新(ver={ver})")
            else:
                print(f"  内容未变,跳过写盘(设备不刷屏)")
            push_image(out_path, room["device"])
            display[room["device"]] = {
                "name": room["room_name"],
                "label": label,
                "status": data["status"],
                "temperature": temp_str,
                "weather": weather_str,
                "time": time_str,
                "date": date_str,
                "bookings": [list(b) for b in data["bookings"]],
            }
            print(f'[{now.strftime("%H:%M:%S")}] {room["room_name"]}: '
                  f'{data["status"]}, {len(data["bookings"])} 条预约({source}) -> {out_path}')
        except Exception:
            print(f'[{now.strftime("%H:%M:%S")}] {room["room_name"]} 更新失败:')
            traceback.print_exc()

    # ---- 动态 HTML 通道:写 rooms.json + 每块屏一份 HTML(数据烤入作保底) ----
    try:
        with open(os.path.join(OUT_DIR, "rooms.json"), "w", encoding="utf-8") as f:
            json.dump({"updated": now.isoformat(), "rooms": display}, f,
                      ensure_ascii=False)
        with open(TEMPLATE_PATH, encoding="utf-8") as f:
            template = f.read()
        for device, d in display.items():
            html = (template
                    .replace("{{ROOM_KEY}}", device)
                    .replace("{{DATA_URL}}", data_url)
                    .replace("{{FALLBACK_JSON}}",
                             json.dumps(d, ensure_ascii=False)))
            with open(os.path.join(OUT_DIR, f"{device}.html"), "w", encoding="utf-8") as f:
                f.write(html)
    except Exception:
        print("HTML/JSON 生成失败:")
        traceback.print_exc()


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    client = None
    last_app = None
    print("桥接服务启动(Web 端修改 config.json 后下一轮自动生效)。")
    while True:
        cfg = json.load(open(os.path.join(BASE_DIR, "config.json"), encoding="utf-8"))
        # 飞书凭证没变就复用 client(token 缓存还在);变了重建
        if client is None or last_app != (cfg["app_id"], cfg["app_secret"]):
            client = FeishuClient(cfg["app_id"], cfg["app_secret"])
            last_app = (cfg["app_id"], cfg["app_secret"])
        interval = cfg.get("poll_seconds", 60)
        try:
            tick(client, cfg["rooms"], cfg["location"], cfg.get("booker_aliases"),
                 cfg.get("data_url", ""))
        except Exception:
            traceback.print_exc()
        time.sleep(interval)


if __name__ == "__main__":
    main()
