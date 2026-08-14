from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any


SKILL_KINDS = {"workflow", "code"}
CODE_PERMISSIONS = {"filesystem", "network", "process", "system"}
SAFE_IMPORTS = {
    "collections",
    "datetime",
    "decimal",
    "functools",
    "itertools",
    "json",
    "math",
    "random",
    "re",
    "statistics",
    "time",
}
PERMISSION_IMPORTS = {
    "filesystem": {"csv", "os", "pathlib", "shutil"},
    "network": {"httpx", "urllib"},
    "process": {"subprocess"},
    "system": {"ctypes", "platform", "winreg"},
}
FORBIDDEN_CALLS = {
    "__import__",
    "breakpoint",
    "compile",
    "delattr",
    "eval",
    "exec",
    "getattr",
    "globals",
    "locals",
    "print",
    "setattr",
    "vars",
}


def generation_prompt(kind: str, description: str, available_tools: list[str]) -> list[dict[str, str]]:
    if kind not in SKILL_KINDS:
        raise ValueError("未知的技能生成类型")
    common = (
        "你是 MaoMao 的技能设计器。只返回一个 JSON 对象，不要 Markdown。"
        "字段必须包含 name、description、triggers、category。triggers 是 1 到 6 个用户可能说出的短语，"
        "category 使用已有中文分类名或简洁的新分类名。不要包含密钥、个人路径或用户数据。"
    )
    if kind == "workflow":
        instructions = (
            "这是组合技能。额外返回 instruction 和 required_tools。instruction 是交给猫猫执行的明确步骤；"
            "required_tools 只能从给定工具名中选择，不得编造工具。可用工具："
            + ", ".join(sorted(available_tools))
        )
    else:
        instructions = (
            "这是 Python 代码技能。额外返回 code 和 permissions。code 必须定义 run(input_text: str) -> str，"
            "不得在模块顶层执行动作，不得使用 eval/exec/compile/__import__。permissions 只能从 "
            "filesystem、network、process、system 中选择实际需要的最小集合。代码应简短、可读并处理错误。"
            "不要打印输出，只通过 run 的返回值提供结果。"
        )
    return [
        {"role": "system", "content": common + instructions},
        {"role": "user", "content": description.strip()},
    ]


def _json_object(text: str) -> dict[str, Any]:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate, flags=re.IGNORECASE)
        candidate = re.sub(r"\s*```$", "", candidate)
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("Kimi K3 没有返回技能 JSON")
    value = json.loads(candidate[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("技能草案必须是 JSON 对象")
    return value


def _clean_text(value: Any, field: str, limit: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"技能缺少 {field}")
    if len(text) > limit:
        raise ValueError(f"技能的 {field} 过长")
    return text


def _clean_list(value: Any, field: str, limit: int) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"技能的 {field} 必须是列表")
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text[:80])
    if not result or len(result) > limit:
        raise ValueError(f"技能的 {field} 数量无效")
    return result


def validate_code(code: str, permissions: list[str]) -> None:
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise ValueError(f"生成代码语法错误：{exc.msg}") from exc
    allowed_imports = set(SAFE_IMPORTS)
    for permission in permissions:
        allowed_imports.update(PERMISSION_IMPORTS[permission])
    run_function: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for statement in tree.body:
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            names = [alias.name.split(".", 1)[0] for alias in statement.names]
            if isinstance(statement, ast.ImportFrom) and statement.module:
                names = [statement.module.split(".", 1)[0]]
            unknown = [name for name in names if name not in allowed_imports]
            if unknown:
                raise ValueError(f"代码导入未获权限的模块：{', '.join(unknown)}")
        elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if statement.decorator_list:
                raise ValueError("代码函数不得使用装饰器")
            for default in [*statement.args.defaults, *statement.args.kw_defaults]:
                if default is not None:
                    try:
                        ast.literal_eval(default)
                    except (ValueError, TypeError) as exc:
                        raise ValueError("代码函数默认值必须是常量") from exc
            annotations = [argument.annotation for argument in statement.args.args]
            annotations.append(statement.returns)
            if any(
                annotation is not None
                and not (
                    isinstance(annotation, ast.Name)
                    and annotation.id in {"str", "int", "float", "bool", "dict", "list", "None"}
                )
                for annotation in annotations
            ):
                raise ValueError("代码函数只能使用简单内置类型注解")
            if statement.name == "run" and isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                run_function = statement
        elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
            value = statement.value
            if value is not None:
                try:
                    ast.literal_eval(value)
                except (ValueError, TypeError) as exc:
                    raise ValueError("模块级变量只能使用常量") from exc
        elif isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant):
            continue
        else:
            raise ValueError("代码不得在模块顶层执行动作")
    if run_function is None or isinstance(run_function, ast.AsyncFunctionDef):
        raise ValueError("代码必须定义同步 run(input_text) 函数")
    if len(run_function.args.args) != 1 or run_function.args.vararg or run_function.args.kwarg:
        raise ValueError("run 函数必须只接收 input_text 一个参数")
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            raise ValueError("代码不得访问双下划线名称")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in FORBIDDEN_CALLS:
                raise ValueError(f"代码使用了禁止调用：{node.func.id}")
            if node.func.id == "open" and "filesystem" not in permissions:
                raise ValueError("代码使用文件访问但未声明 filesystem 权限")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise ValueError("代码不得访问双下划线属性")
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "os"
            and (
                node.func.attr in {"popen", "system", "startfile"}
                or node.func.attr.startswith("exec")
                or node.func.attr.startswith("spawn")
            )
        ):
            raise ValueError("代码不得通过 os 启动进程，请使用并声明 process 权限")


def parse_generated_skill(kind: str, response_text: str, available_tools: list[str]) -> dict[str, Any]:
    value = _json_object(response_text)
    draft: dict[str, Any] = {
        "kind": kind,
        "name": _clean_text(value.get("name"), "name", 40),
        "description": _clean_text(value.get("description"), "description", 240),
        "triggers": _clean_list(value.get("triggers"), "triggers", 6),
        "category": _clean_text(value.get("category"), "category", 20),
        "generator": "Kimi K3",
    }
    if kind == "workflow":
        draft["instruction"] = _clean_text(value.get("instruction"), "instruction", 4000)
        tools = [str(item) for item in value.get("required_tools", [])]
        unknown = sorted(set(tools) - set(available_tools))
        if unknown:
            raise ValueError(f"组合技能引用了不存在的工具：{', '.join(unknown)}")
        draft["required_tools"] = sorted(set(tools))
    elif kind == "code":
        code = _clean_text(value.get("code"), "code", 20000)
        permissions = sorted(set(map(str, value.get("permissions", []))))
        unknown = sorted(set(permissions) - CODE_PERMISSIONS)
        if unknown:
            raise ValueError(f"代码技能声明了未知权限：{', '.join(unknown)}")
        validate_code(code, permissions)
        draft["code"] = code
        draft["permissions"] = permissions
    else:
        raise ValueError("未知的技能生成类型")
    return draft


class GeneratedSkillStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.manifest_path = root / "skills.json"
        self.code_dir = root / "code"

    def load(self) -> list[dict[str, Any]]:
        if not self.manifest_path.is_file():
            return []
        try:
            value = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    def _write(self, skills: list[dict[str, Any]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.manifest_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(skills, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.manifest_path)

    def save_draft(self, draft: dict[str, Any]) -> dict[str, Any]:
        item = dict(draft)
        item["id"] = f"generated-{uuid.uuid4().hex[:12]}"
        item["enabled"] = True
        code = item.pop("code", None)
        if item["kind"] == "code":
            self.code_dir.mkdir(parents=True, exist_ok=True)
            relative = f"code/{item['id']}.py"
            normalized_code = str(code).rstrip() + "\n"
            (self.root / relative).write_text(normalized_code, encoding="utf-8")
            item["code_path"] = relative
            item["code_sha256"] = hashlib.sha256(normalized_code.encode("utf-8")).hexdigest().upper()
        skills = self.load()
        skills.append(item)
        self._write(skills)
        return item

    def set_enabled(self, skill_id: str, enabled: bool) -> bool:
        skills = self.load()
        changed = False
        for item in skills:
            if item.get("id") == skill_id:
                item["enabled"] = bool(enabled)
                changed = True
        if changed:
            self._write(skills)
        return changed

    def set_category(self, skill_id: str, category: str) -> bool:
        skills = self.load()
        changed = False
        for item in skills:
            if item.get("id") == skill_id:
                item["category"] = category
                changed = True
        if changed:
            self._write(skills)
        return changed

    def delete(self, skill_id: str) -> bool:
        skills = self.load()
        kept = [item for item in skills if item.get("id") != skill_id]
        target = next((item for item in skills if item.get("id") == skill_id), None)
        if target is None:
            return False
        code_path = target.get("code_path")
        if code_path:
            path = (self.root / str(code_path)).resolve()
            if path.parent == self.code_dir.resolve() and path.is_file():
                path.unlink()
        self._write(kept)
        return True

    def match(self, text: str) -> dict[str, Any] | None:
        normalized = text.strip().casefold()
        matches: list[tuple[int, dict[str, Any]]] = []
        for item in self.load():
            if not item.get("enabled", True):
                continue
            for trigger in item.get("triggers", []):
                needle = str(trigger).strip().casefold()
                if needle and (normalized == needle or needle in normalized):
                    matches.append((len(needle), item))
        return max(matches, key=lambda value: value[0])[1] if matches else None

    def workflow_prompt(self, skill: dict[str, Any], user_text: str) -> str:
        required = "、".join(map(str, skill.get("required_tools", []))) or "无"
        return (
            f"执行用户生成的组合技能“{skill['name']}”。\n"
            f"技能步骤：{skill['instruction']}\n"
            f"允许使用的既有工具：{required}\n"
            f"用户本次原始请求：{user_text}"
        )

    def execute_code(self, skill: dict[str, Any], user_text: str, timeout: float = 30.0) -> str:
        path = self.verify_code(skill)
        runner = Path(__file__).with_name("generated_skill_runner.py")
        try:
            completed = subprocess.run(
                [sys.executable, "-I", str(runner), str(path)],
                input=user_text,
                text=True,
                capture_output=True,
                timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"代码技能执行超过 {timeout:g} 秒，已终止") from exc
        if completed.returncode != 0:
            error = (completed.stderr or completed.stdout).strip()[:1000]
            raise RuntimeError(f"代码技能执行失败：{error}")
        output = completed.stdout.strip()
        if len(output) > 10000:
            output = output[:10000] + "\n…（结果已截断）"
        return output or "技能已执行完成。"

    def verify_code(self, skill: dict[str, Any]) -> Path:
        relative = str(skill.get("code_path") or "")
        path = (self.root / relative).resolve()
        if path.parent != self.code_dir.resolve() or not path.is_file():
            raise RuntimeError("代码技能文件不存在")
        code = path.read_text(encoding="utf-8")
        actual_hash = hashlib.sha256(code.encode("utf-8")).hexdigest().upper()
        expected_hash = str(skill.get("code_sha256") or "").upper()
        if not expected_hash or actual_hash != expected_hash:
            raise RuntimeError("代码技能文件在确认保存后发生变化，已拒绝执行")
        validate_code(code, list(map(str, skill.get("permissions", []))))
        return path
