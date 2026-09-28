# -*- coding: utf-8 -*-
"""
批量构建会议室门牌固件,并刷新烧录平台的设备清单。
流程:config.json 的每个 room -> 生成 ESPHome YAML -> esphome compile
     -> factory.bin 收进 flasher/firmware/ -> 写 manifest -> 更新 devices.json
用法:python build_firmware.py            # 全部重建
     python build_firmware.py screen-2   # 只建指定设备
"""
import json
import os
import re
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))          # flasher/
PROJ_DIR = os.path.dirname(BASE_DIR)                            # 项目根
CFG = json.load(open(os.path.join(PROJ_DIR, "config.json"), encoding="utf-8"))

# 拉图的 base URL(服务器地址),从 config.json 读取
IMAGE_BASE = CFG.get("image_base", "http://192.168.1.100:8000")

YAML_TEMPLATE = """\
esphome:
  name: {name}

esp32:
  board: esp32-s3-devkitc-1
  framework:
    type: {framework_type}
{psram_block}
wifi:
  ssid: "{wifi_ssid}"
  password: "{wifi_password}"
  on_connect:
    - component.update: sign_image

logger:
  level: INFO
  hardware_uart: UART0

http_request:
  timeout: 15s

globals:
  - id: last_ver
    type: std::string
    initial_value: '""'

script:
  - id: refresh_sign
    then:
      - component.update: sign_image

# 变化驱动刷屏:每 60s 查一次版本号(几十字节),
# 服务器内容(预约信息/日期)变了才拉图刷屏,温度变化不刷
interval:
  - interval: 60s
    then:
      - http_request.get:
          url: "{ver_url}"
          capture_response: true
          on_response:
            then:
              - lambda: |-
                  if (response->status_code == 200 && !body.empty()) {{
                    std::string ver = body;
                    while (!ver.empty() && (ver.back() == '\\n' || ver.back() == '\\r'
                                             || ver.back() == ' '))
                      ver.pop_back();
                    if (ver != id(last_ver)) {{
                      ESP_LOGI("sign", "内容变化 %s -> %s,拉图刷屏",
                               id(last_ver).c_str(), ver.c_str());
                      id(last_ver) = ver;
                      id(refresh_sign).execute();
                    }}
                  }}

api:
  # 门牌没有任何 API 客户端连接;默认 15 分钟"无客户端就重启"会导致
  # 设备周期性重启→每次重启都拉图刷屏,还可能把刷屏打断留下白屏。关掉。
  reboot_timeout: 0s
ota:
  - platform: esphome

spi:
  clk_pin: GPIO7
  mosi_pin: GPIO9
{spi_extra}

online_image:
  - id: sign_image
    url: "{image_url}"
    format: png
    type: {image_type}
    update_interval: never        # 不自动拉,由版本号轮询触发
    on_download_finished:
      # 回调参数 cached:内容没变(304)时为 true——此时屏幕保持原样,
      # 不重绘;只有真正下载到新图(cached=false)才刷屏。
      # 否则 Wi-Fi 重连等场景图没变也会闪一次全屏。
      - if:
          condition:
            lambda: 'return !cached;'
          then:
            - component.update: epaper_display

{display_yaml}"""

# 显示配置:E1001(7.5寸黑白)与 E1002(7.3寸 ACeP 七色彩屏)驱动不同。
# 两者 SPI 引脚一致,引脚由各自模型内置;彩屏整屏刷新约 30~45s 属硬件特性。
# 每个房间的硬件由 config.json 里该房间的 "hw" 字段决定,缺省按 e1002
# (后续设备均为 E1002 彩屏;黑白屏房间必须在 config 里显式写 hw=e1001,
# 否则彩屏固件刷到黑白屏、或反过来,屏幕都会静默不显示)。
# 框架与内存注意(2026-08-19 大排查结论):
# epaper_spi 的 E1002 七彩全帧缓冲约 192KB,默认 RAMAllocator 在 PSRAM 未开启时
# 只能吃内部 RAM(总共 ~340KB),把 WiFi 驱动的数据面缓冲挤爆——表现为扫描正常、
# 关联能过,但 EAPOL 数据帧收不到,四次握手超时(任何框架、任何引脚组合都一样)。
# 解法:e1002 必须开 PSRAM(板载 8MB 八线),显存走外部 RAM,WiFi 与刷屏兼得,
# esp-idf 下全链路实测通过。waveshare(e1001)缓冲仅 48KB,内部 RAM 够用,不动。
HW_FRAMEWORK = {
    "e1001": "esp-idf",
    "e1002": "esp-idf",
}

# e1002 必须开 PSRAM(见上);e1001 不需要。
HW_PSRAM = {
    "e1001": "",
    "e1002": """
# 板载 8MB 八线 PSRAM:epaper_spi 192KB 显存走外部 RAM,
# 否则挤爆内部 RAM 导致 WiFi 四次握手超时(实测踩坑)
psram:
  mode: octal
  speed: 80MHz
""",
}

# E1002 按 Seeed 官方示例带上 MISO(GPIO8,与 SD 卡共总线);E1001 保持验证过的最小配置。
HW_SPI_EXTRA = {
    "e1001": "",
    "e1002": "  miso_pin: GPIO8",
}

DISPLAY_BLOCKS = {
    "e1001": """\
display:
  - platform: waveshare_epaper
    id: epaper_display
    model: 7.50inv2
    cs_pin: GPIO10
    dc_pin: GPIO11
    reset_pin:
      number: GPIO12
      inverted: false
    busy_pin:
      number: GPIO13
      inverted: true
    update_interval: never
    lambda: |-
      it.image(0, 0, id(sign_image), COLOR_OFF, COLOR_ON);
""",
    # 彩屏:online_image 用 RGB565 保留颜色,直接按原色画(驱动按 E6 七色调量化)
    "e1002": """\
display:
  - platform: epaper_spi
    model: Seeed-reTerminal-E1002
    id: epaper_display
    update_interval: never
    lambda: |-
      it.image(0, 0, id(sign_image));
""",
}

HW_NOTE = {
    "e1001": "reTerminal E1001 · 7.5寸黑白 · 800×480",
    "e1002": "reTerminal E1002 · 7.3寸彩色 · 800×480",
}

MANIFEST_TEMPLATE = {
    "name": "",
    "version": "1.0.0",
    "new_install_prompt_erase": True,
    "builds": [{
        "chipFamily": "ESP32-S3",
        "parts": [{"path": "", "offset": 0}],
    }],
}


def room_label(room: dict) -> str:
    """会议室号:取"室"前的数字,取不到就用最后一组数字,再没有就用 device"""
    m = re.search(r"(\d+)\s*室", room["room_name"])
    nums = re.findall(r"\d+", room["room_name"])
    return m.group(1) if m else (nums[-1] if nums else room["device"])


def build_one(room: dict, wifi_ssid: str, wifi_password: str) -> None:
    device = room["device"]
    name = f"meeting-sign-{room_label(room)}"
    yaml_path = os.path.join(PROJ_DIR, "firmware", f"{name}.yaml")
    hw = room.get("hw", "e1002")
    display_yaml = DISPLAY_BLOCKS.get(hw)
    if display_yaml is None:
        raise RuntimeError(
            f"[{device}] 未知硬件类型 hw={hw!r}(支持: {sorted(DISPLAY_BLOCKS)})")
    # 拉图地址绑定会议室号(而非设备号):同一会议室删了重加、换设备硬件,固件都不用重建。
    # 彩屏新固件拉 -color.png(RGB565);黑白屏/旧固件的 .png 保持纯黑白不动
    if hw == "e1002":
        image_url = f"{IMAGE_BASE}/sign-{room_label(room)}-color.png"
        image_type = "RGB565"
    else:
        image_url = f"{IMAGE_BASE}/sign-{room_label(room)}.png"
        image_type = "BINARY"
    ver_url = f"{IMAGE_BASE}/sign-{room_label(room)}.ver"

    with open(yaml_path, "w", encoding="utf-8") as f:
        f.write(YAML_TEMPLATE.format(
            name=name, wifi_ssid=wifi_ssid, wifi_password=wifi_password,
            image_url=image_url, ver_url=ver_url, image_type=image_type,
            display_yaml=display_yaml,
            framework_type=HW_FRAMEWORK.get(hw, "esp-idf"),
            psram_block=HW_PSRAM.get(hw, ""),
            spi_extra=HW_SPI_EXTRA.get(hw, "")))
    print(f"[{device}] YAML 已生成({hw}) -> {yaml_path}")

    print(f"[{device}] 开始编译(首次较慢)...")
    subprocess.run(["esphome", "compile", yaml_path], check=True, cwd=PROJ_DIR)

    src = os.path.join(PROJ_DIR, "firmware", ".esphome", "build",
                       name, "build", "firmware.factory.bin")
    dst_dir = os.path.join(BASE_DIR, "firmware")
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, f"{name}.factory.bin")
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        fo.write(fi.read())
    print(f"[{device}] 固件已收集 -> {dst}")

    # 产物校验:bin 里必须真的包含本会议室的图/版本号 URL。
    # 踩过的坑:esphome compile 可能"静默跳过编译"(MSYS/Git-Bash 环境、
    # 构建缓存异常),只把陈旧产物重新打包——烧进去的还是旧固件。
    # 校验不过直接报错,绝不让陈旧固件悄悄出货。
    with open(dst, "rb") as fv:
        blob = fv.read()
    for marker in (image_url.rsplit("/", 1)[-1], f"sign-{room_label(room)}.ver"):
        if marker.encode() not in blob:
            raise RuntimeError(
                f"[{device}] 固件校验失败:bin 中找不到 {marker!r}。"
                f"编译很可能被缓存跳过、打包了陈旧产物。请删除 "
                f"firmware/.esphome/build/{name} 目录后,在 cmd/PowerShell"
                f"(不要用 Git-Bash/MSYS)中重新构建。")

    manifest = json.loads(json.dumps(MANIFEST_TEMPLATE))
    manifest["name"] = f'{room["room_name"]}门牌'
    manifest["builds"][0]["parts"][0]["path"] = f"../firmware/{name}.factory.bin"
    mdir = os.path.join(BASE_DIR, "manifests")
    os.makedirs(mdir, exist_ok=True)
    with open(os.path.join(mdir, f"{name}.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"[{device}] manifest 已写入")

    return {
        "id": device,
        "room": room["room_name"],
        "manifest": f"./manifests/{name}.json",
        "image": f"sign-{room_label(room)}.png",
        "note": HW_NOTE.get(hw, hw),
    }


def main() -> None:
    target = sys.argv[1] if len(sys.argv) > 1 else None
    wifi_ssid = CFG.get("wifi_ssid", "SEEED-HA_2.4G")
    wifi_password = CFG.get("wifi_password", "")
    if not wifi_password:
        print("提示:config.json 里可填 wifi_ssid / wifi_password,否则用模板里的值")

    devices_path = os.path.join(BASE_DIR, "devices.json")
    devices = json.load(open(devices_path, encoding="utf-8")) \
        if os.path.exists(devices_path) else []

    for room in CFG["rooms"]:
        if target and room["device"] != target:
            continue
        entry = build_one(room, wifi_ssid, wifi_password)
        devices = [d for d in devices if d["id"] != entry["id"]]
        devices.append(entry)

    with open(devices_path, "w", encoding="utf-8") as f:
        json.dump(devices, f, ensure_ascii=False, indent=2)
    print(f"devices.json 已更新({len(devices)} 台设备)")


if __name__ == "__main__":
    main()
