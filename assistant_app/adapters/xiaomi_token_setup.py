from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.request import urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TOKEN_PATH = PROJECT_ROOT / "xiaomi_token.txt"
EXTRACTOR_COMMIT = "c4db715dace9806e905153c2977608873e8ab7c9"
EXTRACTOR_URL = (
    "https://raw.githubusercontent.com/"
    "PiotrMachowski/Xiaomi-cloud-tokens-extractor/"
    f"{EXTRACTOR_COMMIT}/token_extractor.py"
)
EXTRACTOR_SHA256 = "e4eb8220df0f68c945c21cde5dbc5a0a276e7a035857b568d9423788ef192a30"


def _download_extractor() -> str:
    with urlopen(EXTRACTOR_URL, timeout=20) as response:
        source_bytes = response.read()
    digest = hashlib.sha256(source_bytes).hexdigest()
    if digest != EXTRACTOR_SHA256:
        raise RuntimeError("下载的令牌提取器校验值不匹配，已停止运行。")
    source = source_bytes.decode("utf-8")
    original = 'print_entry("TOKEN", device["token"], 3)'
    replacement = 'print_entry("TOKEN", "[saved securely by MaoMao]", 3)'
    if original not in source:
        raise RuntimeError("令牌提取器结构与预期不一致，已停止运行。")
    source = source.replace(original, replacement, 1)
    image_server = "        start_image_server(image_content)\n        print_if_interactive(message_url)"
    open_image = (
        "        start_image_server(image_content)\n"
        "        __import__('webbrowser').open("
        "f\"http://{args.host or '127.0.0.1'}:31415\")\n"
        "        print_if_interactive(message_url)"
    )
    if image_server not in source:
        raise RuntimeError("无法启用二维码自动打开功能，已停止运行。")
    return source.replace(image_server, open_image, 1)


def _devices(payload: list[dict]) -> list[dict]:
    devices: list[dict] = []
    for server in payload:
        for home in server.get("homes", []):
            for device in home.get("devices", []):
                token = str(device.get("token") or "").removeprefix("0x").lower()
                if re.fullmatch(r"[0-9a-f]{32}", token):
                    devices.append(device)
    return devices


def _choose_device(devices: list[dict], requested_did: str = "") -> dict:
    if requested_did:
        for device in devices:
            if str(device.get("did") or "") == requested_did:
                return device
        raise RuntimeError("没有找到指定设备。")
    if not devices:
        raise RuntimeError("账号下没有返回可用的本地设备令牌。")
    print("\n请选择要交给猫猫控制的设备：")
    for index, device in enumerate(devices, 1):
        name = str(device.get("name") or "未命名设备")
        model = str(device.get("model") or "未知型号")
        ip = str(device.get("localip") or "IP 未知")
        print(f"  {index}. {name} | {model} | {ip}")
    while True:
        answer = input("输入编号：").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(devices):
            return devices[int(answer) - 1]
        print("编号无效，请重新输入。")


def configure(server: str = "cn", requested_did: str = "") -> Path:
    source = _download_extractor()
    with tempfile.TemporaryDirectory(prefix="maomao-xiaomi-") as directory:
        temp_root = Path(directory)
        extractor_path = temp_root / "token_extractor.py"
        output_path = temp_root / "devices.json"
        extractor_path.write_text(source, encoding="utf-8")
        completed = subprocess.run(
            [sys.executable, str(extractor_path), "--server", server, "--output", str(output_path)],
            check=False,
        )
        if completed.returncode != 0 or not output_path.is_file():
            raise RuntimeError("二维码登录或设备读取没有完成。")
        payload = json.loads(output_path.read_text(encoding="utf-8"))
        device = _choose_device(_devices(payload), requested_did)
        token = str(device.get("token") or "").removeprefix("0x").lower()
        values = {
            "did": str(device.get("did") or ""),
            "name": str(device.get("name") or ""),
            "model": str(device.get("model") or ""),
            "ip": str(device.get("localip") or ""),
            "token": token,
        }
        TOKEN_PATH.write_text(
            "".join(f"{key}={value}\n" for key, value in values.items()),
            encoding="utf-8",
        )
        return TOKEN_PATH


def main() -> int:
    parser = argparse.ArgumentParser(description="猫猫小米设备连接工具")
    parser.add_argument("--server", default="cn")
    parser.add_argument("--did", default="")
    args = parser.parse_args()
    print("猫猫 · 小米本地令牌设置")
    print("请按提示使用二维码登录；令牌不会显示在屏幕上，也不会进入 Git。\n")
    try:
        path = configure(args.server, args.did)
        print(f"\n设置完成，已安全保存到：{path.name}")
        input("按 Enter 关闭窗口……")
        return 0
    except Exception as exc:
        print(f"\n设置失败：{type(exc).__name__}: {exc}")
        input("按 Enter 关闭窗口……")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
