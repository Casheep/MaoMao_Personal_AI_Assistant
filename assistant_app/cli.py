from __future__ import annotations

import argparse
import sys
import uuid

from .agent import PersonalAgent
from .audio import AudioRecorder, SpeechTranscriber, input_devices
from .budget import BudgetExceeded, BudgetManager
from .config import app_paths, load_config
from .database import Database
from .providers import HybridModelClient, KimiClient, MiMoClient
from .secrets import load_kimi_key, load_mimo_key
from .tools import ToolRegistry
from .tts import SpeechSynthesizer


def confirm_tool(name: str, arguments: dict) -> bool:
    print(f"\n[需要确认] 工具={name}")
    safe_arguments = dict(arguments)
    if "text" in safe_arguments and len(str(safe_arguments["text"])) > 200:
        safe_arguments["text"] = str(safe_arguments["text"])[:200] + "…"
    print(f"参数={safe_arguments}")
    return input("执行吗？输入 y 确认：").strip().lower() == "y"


def build_agent(
    config: dict,
    session_id: str,
    confirmation_callback=confirm_tool,
    progress_callback=None,
    cancel_event=None,
    include_tts: bool = True,
) -> tuple[PersonalAgent, Database, SpeechSynthesizer | None]:
    paths = app_paths()
    database = Database(paths.database)
    budget = BudgetManager(database, config["api"])
    key = load_kimi_key(paths.key_file)
    kimi_client = KimiClient(key, config, database, budget, session_id)
    mimo_client = MiMoClient(
        load_mimo_key(paths.key_file), config, database, budget, session_id
    )
    client = HybridModelClient(kimi_client, mimo_client, config)
    tools = ToolRegistry(
        paths,
        database,
        session_id,
        confirmation_callback,
        application_config=config.get("applications", {}),
        smart_home_config=config.get("smart_home", {}),
        automation_config=config.get("automation", {}),
        skills_config=config.get("skills", {}),
        cancel_event=cancel_event,
    )
    agent = PersonalAgent(
        config,
        database,
        client,
        tools,
        session_id,
        progress_callback=progress_callback,
        cancel_event=cancel_event,
    )
    tts = SpeechSynthesizer(config["tts"]) if include_tts else None
    return agent, database, tts


def run_check(config: dict) -> int:
    paths = app_paths()
    load_kimi_key(paths.key_file)
    database = Database(paths.database)
    try:
        print("配置文件：正常")
        print("API Key：格式正常（内容未显示）")
        print(f"数据库：{paths.database}")
        print(f"语音识别：{config['audio']['backend']} / {config['audio']['qwen_model']}")
        print(f"语音播报：{config['tts']['backend']} / {config['tts']['speaker']}")
        devices = input_devices()
        print(f"可用输入设备：{len(devices)}")
        for device in devices[:10]:
            print(f"  {device}")
        try:
            voices = SpeechSynthesizer.list_voices()
            print(f"本地 SAPI 语音：{len(voices)}")
            for voice in voices[:10]:
                print(f"  {voice}")
        except Exception as exc:
            print(f"本地 SAPI 检查失败：{exc}")
            return 1
    finally:
        database.close()
    return 0


def run_tts_smoke(config: dict, output: str) -> int:
    path = app_paths().root / output
    tts = SpeechSynthesizer(config["tts"])
    try:
        tts.synthesize_to_file(
            "你好，我是猫猫。之后请多多关照。",
            path,
        )
        print(f"本地 TTS 测试语音已生成：{path.resolve()}")
    finally:
        tts.close()
    return 0


def run_api_smoke(config: dict, selection: str) -> int:
    paths = app_paths()
    database = Database(paths.database)
    session_id = f"smoke-{uuid.uuid4().hex}"
    budget = BudgetManager(database, config["api"])
    client = KimiClient(load_kimi_key(paths.key_file), config, database, budget, session_id)
    models = {
        "k2": [(config["models"]["routine"], "disabled")],
        "k3": [(config["models"]["complex"], "low")],
        "both": [
            (config["models"]["routine"], "disabled"),
            (config["models"]["complex"], "low"),
        ],
    }[selection]
    try:
        for model, reasoning in models:
            task_id = f"smoke-{uuid.uuid4().hex}"
            response = client.chat(
                messages=[
                    {"role": "system", "content": "这是连通性测试。回答务必简短。"},
                    {"role": "user", "content": "只回复 OK"},
                ],
                tools=[],
                model=model,
                reasoning=reasoning,
                task_id=task_id,
            )
            content = str(response.message.get("content") or "").strip()
            print(
                f"{model}: {content!r}; 输入={response.input_tokens}, "
                f"输出={response.output_tokens}, "
                f"估算={config['api']['currency_symbol']}{response.cost:.2f}"
            )
    finally:
        client.close()
        database.close()
    return 0


def run_once(config: dict, text: str, no_tts: bool) -> int:
    if no_tts:
        config["tts"]["enabled"] = False
    session_id = uuid.uuid4().hex
    agent, database, tts = build_agent(config, session_id)
    assert tts is not None
    try:
        answer = agent.run(text, input_mode="text")
        reason = "、".join(answer.route.reasons)
        print(
            f"助手[{answer.route.model}/{answer.route.reasoning}; {reason}; "
            f"本任务 {config['api']['currency_symbol']}{answer.task_cost:.2f}]> {answer.text}"
        )
        tts.speak(answer.text)
    finally:
        tts.close()
        agent.close()
        database.close()
    return 0


def interactive(text_only: bool, no_tts: bool) -> int:
    config = load_config()
    if no_tts:
        config["tts"]["enabled"] = False
    session_id = uuid.uuid4().hex
    agent, database, tts = build_agent(config, session_id)
    assert tts is not None
    recorder = AudioRecorder(
        int(config["audio"]["sample_rate"]), int(config["audio"]["channels"])
    )
    transcriber = SpeechTranscriber(config["audio"])
    print("\n私人助手 MVP 已启动")
    print(
        "直接输入文字；空行开始录音。命令：/remember、/memories、"
        "/usage、/voices、/voice、/pause、/resume、/stop、/quit"
    )
    try:
        while True:
            input_mode = "text"
            try:
                raw = input("\n你> ").strip()
            except EOFError:
                break
            if raw == "/quit":
                break
            if raw.startswith("/remember "):
                memory_id = agent.remember(raw.removeprefix("/remember ").strip())
                print(f"已保存本地记忆 #{memory_id}")
                continue
            if raw == "/memories":
                for item in database.list_memories():
                    print(f"#{item['id']} {item['content']}")
                continue
            if raw == "/usage":
                usage = database.usage_summary(config["api"]["currency"])
                symbol = config["api"]["currency_symbol"]
                print(f"今日 {symbol}{usage['today']:.2f}，本月 {symbol}{usage['month']:.2f}")
                continue
            if raw == "/voices":
                descriptions = tts.voice_descriptions()
                for name, description in descriptions.items():
                    marker = "（当前）" if name == tts.speaker else ""
                    print(f"{name}: {description}{marker}")
                continue
            if raw.startswith("/voice "):
                try:
                    tts.set_speaker(raw.removeprefix("/voice ").strip())
                    print(f"已切换音色：{tts.speaker}")
                except ValueError as exc:
                    print(exc)
                continue
            if raw == "/pause":
                tts.pause()
                print("已暂停播报。")
                continue
            if raw == "/resume":
                tts.resume()
                print("已继续播报。")
                continue
            if raw == "/stop":
                tts.stop()
                print("已停止播报。")
                continue
            if not raw:
                if text_only:
                    print("当前为纯文字模式。")
                    continue
                try:
                    audio_path = recorder.record_until_enter()
                    raw = transcriber.transcribe(audio_path)
                    input_mode = "voice"
                    print(f"识别> {raw}")
                except Exception as exc:
                    print(f"录音或识别失败：{exc}")
                    continue
            if not raw:
                continue
            try:
                answer = agent.run(raw, input_mode=input_mode)
                reason = "、".join(answer.route.reasons)
                print(
                    f"\n助手[{answer.route.model}/{answer.route.reasoning}; {reason}; "
                    f"本任务 {config['api']['currency_symbol']}{answer.task_cost:.2f}]> {answer.text}"
                )
                try:
                    tts.speak(answer.text)
                except Exception as exc:
                    print(f"本地语音播报失败：{exc}")
            except BudgetExceeded as exc:
                print(f"预算限制：{exc}")
            except Exception as exc:
                print(f"任务失败：{exc}")
    finally:
        tts.close()
        agent.close()
        database.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="本地优先的猫猫私人语音助手")
    parser.add_argument("--check", action="store_true", help="检查本地环境但不调用 API")
    parser.add_argument(
        "--api-smoke",
        choices=["k2", "k3", "both"],
        help="用极短请求测试 Kimi API（会产生少量费用）",
    )
    parser.add_argument("--once", help="执行一条文字指令后退出")
    parser.add_argument("--gui", action="store_true", help="启动可点击的桌面界面")
    parser.add_argument(
        "--tts-smoke",
        nargs="?",
        const="data/tts-smoke.wav",
        help="生成一段 CosyVoice 3 测试语音到指定 WAV 文件",
    )
    parser.add_argument("--text-only", action="store_true", help="禁用录音入口")
    parser.add_argument("--no-tts", action="store_true", help="禁用语音播报")
    args = parser.parse_args()
    config = load_config()
    if args.check:
        return run_check(config)
    if args.gui:
        from .rounded_gui import main as gui_main

        return gui_main()
    if args.api_smoke:
        return run_api_smoke(config, args.api_smoke)
    if args.tts_smoke:
        return run_tts_smoke(config, args.tts_smoke)
    if args.once:
        return run_once(config, args.once, args.no_tts)
    return interactive(args.text_only, args.no_tts)


if __name__ == "__main__":
    sys.exit(main())
