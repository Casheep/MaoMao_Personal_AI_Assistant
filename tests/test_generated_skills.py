from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from assistant_app.code_skill_harness import (
    CODE_SKILL_AGENT,
    HARNESS_GENERATOR,
    repair_code_skill_draft,
)
from assistant_app.generated_skills import GeneratedSkillStore, generation_prompt, parse_generated_skill, validate_code
from assistant_app.kimi_harness_tools import shell_command_is_allowed
from assistant_app.providers.kimi import KimiClient


class GeneratedSkillTests(unittest.TestCase):
    def test_runtime_baseline_and_code_generator_use_the_sdk_harness(self) -> None:
        project = Path("pyproject.toml").read_text(encoding="utf-8")
        requirements = Path("requirements-release.txt").read_text(encoding="utf-8")
        bridge = Path("assistant_app/qt_quick/bridge.py").read_text(encoding="utf-8")
        self.assertIn('requires-python = ">=3.12,<3.14"', project)
        self.assertIn("kimi-agent-sdk==0.0.5", requirements)
        self.assertIn('if kind == "code":', bridge)
        self.assertIn("kimi.generate_code_skill(description)", bridge)

    def test_code_harness_uses_bounded_tools_and_repairs_invalid_draft(self) -> None:
        responses = iter(
            [
                "not json",
                json.dumps(
                    {
                        "name": "字符统计",
                        "description": "统计输入字符",
                        "triggers": ["字符统计"],
                        "category": "文本工具",
                        "permissions": [],
                        "code": "def run(input_text: str) -> str:\n    return str(len(input_text))",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        prompts: list[str] = []

        def turn(prompt: str) -> str:
            prompts.append(prompt)
            return next(responses)

        draft = repair_code_skill_draft("统计字符", turn)
        self.assertEqual(draft["generator"], HARNESS_GENERATOR)
        self.assertEqual(draft["harnessAttempts"], 2)
        self.assertIn("本地安全校验", prompts[1])
        self.assertIn("WorkspaceShell", CODE_SKILL_AGENT)
        self.assertIn("WorkspaceReadFile", CODE_SKILL_AGENT)
        self.assertIn("WorkspaceWriteFile", CODE_SKILL_AGENT)
        self.assertIn("subagents: {}", CODE_SKILL_AGENT)

    def test_harness_shell_allows_local_work_and_downloads(self) -> None:
        self.assertTrue(shell_command_is_allowed("python skill.py"))
        self.assertTrue(shell_command_is_allowed("python -m py_compile skill.py"))
        self.assertTrue(
            shell_command_is_allowed(
                "Invoke-WebRequest https://example.com/tool.zip -OutFile tool.zip"
            )
        )
        self.assertTrue(shell_command_is_allowed("pip install sample --target .deps"))

    def test_harness_shell_rejects_escape_and_persistent_changes(self) -> None:
        self.assertFalse(shell_command_is_allowed("Get-Content C:\\Users\\person\\secret.txt"))
        self.assertFalse(shell_command_is_allowed("Get-Content ..\\secret.txt"))
        self.assertFalse(shell_command_is_allowed("python skill.py; Remove-Item other.txt"))
        self.assertFalse(shell_command_is_allowed("python skill.py & winget install Example.App"))
        self.assertFalse(shell_command_is_allowed("pip install sample"))
        self.assertFalse(shell_command_is_allowed("python -m pip install sample"))
        self.assertFalse(shell_command_is_allowed("git config --global user.name Kimi"))
        self.assertFalse(shell_command_is_allowed("winget install Example.App"))
        self.assertFalse(shell_command_is_allowed("powershell -EncodedCommand ZQBjAGgAbwA="))

    def test_code_harness_stops_after_bounded_failures(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "连续 2 次"):
            repair_code_skill_draft("生成技能", lambda _prompt: "{}", attempts=2)

    def test_k3_generation_request_omits_empty_tool_configuration(self) -> None:
        class Budget:
            currency = "CNY"

            @staticmethod
            def check_before_call(task_id: str) -> None:
                del task_id

            @staticmethod
            def estimate_cost(*args: object) -> float:
                return 0.0

        class Database:
            @staticmethod
            def log_usage(*args: object) -> None:
                del args

        class Response:
            @staticmethod
            def raise_for_status() -> None:
                return None

            @staticmethod
            def json() -> dict:
                return {"choices": [{"message": {"content": "{}"}}], "usage": {}}

        class Http:
            payload: dict | None = None

            def post(self, path: str, json: dict) -> Response:
                del path
                self.payload = json
                return Response()

        client = object.__new__(KimiClient)
        client.config = {"api": {}, "models": {"k3_max_output_tokens": 100, "k2_max_output_tokens": 50}}
        client._api_key = "test-key"
        client.database = Database()
        client.budget = Budget()
        client.session_id = "generator-test"
        client.client = Http()
        client.chat([{"role": "user", "content": "test"}], [], "kimi-k3", "high", "task")
        self.assertNotIn("tools", client.client.payload)
        self.assertNotIn("tool_choice", client.client.payload)
        self.assertEqual(client.client.payload["model"], "kimi-k3")

    def test_workflow_prompt_is_k3_structured_and_tool_bounded(self) -> None:
        messages = generation_prompt("workflow", "每天整理下载目录", ["list_directory", "read_text_file"])
        self.assertIn("组合技能", messages[0]["content"])
        self.assertIn("list_directory", messages[0]["content"])
        self.assertEqual(messages[1]["content"], "每天整理下载目录")

    def test_workflow_draft_rejects_invented_tools(self) -> None:
        response = json.dumps({"name": "整理文件", "description": "整理下载目录", "triggers": ["整理下载"], "category": "文件", "instruction": "先读取目录，再汇报。", "required_tools": ["invented_tool"]}, ensure_ascii=False)
        with self.assertRaisesRegex(ValueError, "不存在的工具"):
            parse_generated_skill("workflow", response, ["list_directory"])

    def test_code_draft_accepts_a_small_reviewable_run_function(self) -> None:
        response = json.dumps({"name": "字符统计", "description": "统计输入字符", "triggers": ["字符统计"], "category": "文本工具", "permissions": [], "code": "def run(input_text: str) -> str:\n    return str(len(input_text))"}, ensure_ascii=False)
        draft = parse_generated_skill("code", response, [])
        self.assertEqual(draft["generator"], "Kimi K3")
        self.assertEqual(draft["permissions"], [])

    def test_code_validation_rejects_unapproved_imports_and_top_level_actions(self) -> None:
        with self.assertRaisesRegex(ValueError, "未获权限"):
            validate_code("import socket\ndef run(input_text):\n    return input_text", [])
        with self.assertRaisesRegex(ValueError, "模块级变量"):
            validate_code("VALUE = print('bad')\ndef run(input_text):\n    return input_text", [])

    def test_store_persists_matches_runs_disables_and_deletes_skills(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = GeneratedSkillStore(Path(temporary))
            saved = store.save_draft({"kind": "code", "name": "回声", "description": "返回输入", "triggers": ["运行回声"], "category": "文本工具", "generator": "Kimi K3", "permissions": [], "code": "def run(input_text: str) -> str:\n    return '收到：' + input_text"})
            self.assertEqual(store.match("请运行回声测试")["id"], saved["id"])
            self.assertIn("收到：", store.execute_code(saved, "运行回声测试"))
            self.assertTrue(store.set_category(saved["id"], "实用工具"))
            self.assertEqual(store.load()[0]["category"], "实用工具")
            self.assertTrue(store.set_enabled(saved["id"], False))
            self.assertIsNone(store.match("运行回声"))
            code_path = Path(temporary) / saved["code_path"]
            code_path.write_text("def run(input_text):\n    return 'changed'\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "发生变化"):
                store.execute_code(saved, "运行回声")
            self.assertTrue(store.delete(saved["id"]))
            self.assertEqual(store.load(), [])

    def test_workflow_expansion_keeps_original_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = GeneratedSkillStore(Path(temporary))
            prompt = store.workflow_prompt({"name": "晨间准备", "instruction": "先报时，再打开日历。", "required_tools": ["get_current_time"]}, "开始晨间准备")
            self.assertIn("先报时", prompt)
            self.assertIn("开始晨间准备", prompt)


if __name__ == "__main__":
    unittest.main()
