import re

from kaos.path import KaosPath
from kimi_cli.soul.agent import BuiltinSystemPromptArgs, Runtime
from kimi_cli.soul.approval import Approval
from kimi_cli.tools.file.read import ReadFile
from kimi_cli.tools.file.replace import StrReplaceFile
from kimi_cli.tools.file.write import WriteFile
from kimi_cli.tools.shell import Params as ShellParams
from kimi_cli.tools.shell import Shell
from kimi_cli.utils.environment import Environment
from kimi_cli.utils.path import is_within_directory
from kosong.tooling import ToolError, ToolReturnValue
from typing import override


_URL = re.compile(r"https?://[^\s'\"]+", re.IGNORECASE)
_LOCAL_ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|\\\\)")
_SHELL_SEPARATORS = re.compile(r"(?:\r|\n|;|&|\||`|\$\()")
_ALLOWED_COMMANDS = {
    "compress-archive",
    "copy-item",
    "curl",
    "curl.exe",
    "expand-archive",
    "get-childitem",
    "get-content",
    "git",
    "invoke-webrequest",
    "move-item",
    "new-item",
    "pip",
    "pip3",
    "py",
    "python",
    "python3",
    "remove-item",
    "resolve-path",
    "set-content",
    "tar",
    "test-path",
}
_BLOCKED_COMMANDS = re.compile(
    r"\b(?:add-type|choco|cmd|cmd\.exe|docker|iex|invoke-expression|msiexec|"
    r"reg|reg\.exe|sc|sc\.exe|schtasks|set-executionpolicy|shutdown|start-process|"
    r"taskkill|winget|wsl)\b|-(?:encodedcommand|enc)\b",
    re.IGNORECASE,
)
_OUTSIDE_REFERENCES = re.compile(
    r"(?:\.\.[\\/]|(?:^|\s)~[\\/]|\$(?:env:|home\b|profile\b)|"
    r"%(?:userprofile|appdata|localappdata|programdata|systemroot|windir)%)",
    re.IGNORECASE,
)


def shell_command_is_allowed(command: str) -> bool:
    """Allow bounded development commands without granting general Windows control."""
    value = command.strip()
    if not value or len(value) > 4000:
        return False
    without_urls = _URL.sub("", value)
    if _SHELL_SEPARATORS.search(without_urls) or _BLOCKED_COMMANDS.search(value):
        return False
    if _LOCAL_ABSOLUTE_PATH.search(without_urls) or _OUTSIDE_REFERENCES.search(without_urls):
        return False
    first = value.split(maxsplit=1)[0].strip("'\"").lower()
    if first not in _ALLOWED_COMMANDS:
        return False
    lowered = value.lower()
    if first == "git" and (" --global" in lowered or " --system" in lowered):
        return False
    pip_install = first in {"pip", "pip3"} and re.search(r"\sinstall(?:\s|$)", lowered)
    python_pip_install = first in {"py", "python", "python3"} and re.search(
        r"\s-m\s+pip\s+install(?:\s|$)", lowered
    )
    if (pip_install or python_pip_install) and not re.search(
        r"(?:^|\s)(?:--target|-t)(?:\s|=)", lowered
    ):
        return False
    return True


class WorkspaceShell(Shell):
    """Kimi Shell limited to single, reviewable commands in its temporary workspace."""

    def __init__(
        self,
        approval: Approval,
        environment: Environment,
        builtin_args: BuiltinSystemPromptArgs,
    ) -> None:
        super().__init__(approval, environment)
        self._work_dir = builtin_args.KIMI_WORK_DIR

    @override
    async def __call__(self, params: ShellParams) -> ToolReturnValue:
        if not shell_command_is_allowed(params.command):
            return ToolError(
                message=(
                    "Command rejected by MaoMao: Shell may only use one bounded command "
                    "inside the temporary harness workspace. Absolute/parent paths, shell "
                    "chaining, global installers and persistent system changes are blocked."
                ),
                brief="Command outside temporary harness policy",
            )
        return await super().__call__(params)


class WorkspaceReadFile(ReadFile):
    def __init__(self, runtime: Runtime) -> None:
        super().__init__(runtime)

    @override
    async def _validate_path(self, path: KaosPath) -> ToolError | None:
        if not is_within_directory(path.canonical(), self._work_dir):
            return ToolError(
                message="MaoMao harness can only read files in its temporary workspace.",
                brief="File outside temporary workspace",
            )
        return None


class WorkspaceWriteFile(WriteFile):
    def __init__(self, builtin_args: BuiltinSystemPromptArgs, approval: Approval) -> None:
        super().__init__(builtin_args, approval)

    @override
    async def _validate_path(self, path: KaosPath) -> ToolError | None:
        if not is_within_directory(path.canonical(), self._work_dir):
            return ToolError(
                message="MaoMao harness can only write files in its temporary workspace.",
                brief="File outside temporary workspace",
            )
        return None


class WorkspaceStrReplaceFile(StrReplaceFile):
    def __init__(self, builtin_args: BuiltinSystemPromptArgs, approval: Approval) -> None:
        super().__init__(builtin_args, approval)

    @override
    async def _validate_path(self, path: KaosPath) -> ToolError | None:
        if not is_within_directory(path.canonical(), self._work_dir):
            return ToolError(
                message="MaoMao harness can only edit files in its temporary workspace.",
                brief="File outside temporary workspace",
            )
        return None
