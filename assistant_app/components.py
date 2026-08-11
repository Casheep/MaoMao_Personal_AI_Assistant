from __future__ import annotations

import json
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.request import Request, urlopen

from .config import PROJECT_ROOT


ProgressCallback = Callable[[int], None]


@dataclass(frozen=True)
class RuntimeComponent:
    id: str
    name: str
    description: str
    download_url: str
    archive_directory: str
    install_directory: str
    required_file: str
    required_at_startup: bool = True
    max_download_bytes: int = 100 * 1024 * 1024
    max_extracted_bytes: int = 250 * 1024 * 1024
    allowed_extensions: tuple[str, ...] = (
        "",
        ".conf",
        ".dubm",
        ".fst",
        ".ie",
        ".int",
        ".mat",
        ".mdl",
        ".stats",
    )


COMPONENTS: tuple[RuntimeComponent, ...] = (
    RuntimeComponent(
        id="wake-word-model",
        name="中文唤醒词模型",
        description="约 42 MB，用于本地监听“猫猫”，不占用显存。",
        download_url="https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip",
        archive_directory="vosk-model-small-cn-0.22",
        install_directory="engines/vosk/vosk-model-small-cn-0.22",
        required_file="am/final.mdl",
    ),
)

COMPONENTS_BY_ID = {component.id: component for component in COMPONENTS}


def distribution_edition(root: Path = PROJECT_ROOT) -> str:
    manifest_path = root / "release-manifest.json"
    if not manifest_path.is_file():
        return "development"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return "unknown"
    return str(manifest.get("edition", "unknown")).strip().lower()


def missing_startup_components(root: Path = PROJECT_ROOT) -> list[str]:
    if distribution_edition(root) != "lite":
        return []
    return [
        component.id
        for component in COMPONENTS
        if component.required_at_startup and not component_installed(component.id, root)
    ]


def component_installed(component_id: str, root: Path = PROJECT_ROOT) -> bool:
    component = COMPONENTS_BY_ID[component_id]
    return (root / component.install_directory / component.required_file).is_file()


def extract_component_archive(
    archive_path: Path,
    extracted_root: Path,
    component: RuntimeComponent,
) -> None:
    """Extract data-only component files with strict path and size checks."""
    extracted_root.mkdir()
    extracted_resolved = extracted_root.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        if len(members) > 2000:
            raise RuntimeError("组件压缩包包含过多文件。")
        if sum(member.file_size for member in members) > component.max_extracted_bytes:
            raise RuntimeError("组件解压后的大小超出限制。")
        for member in members:
            member_path = (extracted_root / member.filename).resolve()
            if member_path != extracted_resolved and extracted_resolved not in member_path.parents:
                raise RuntimeError("组件压缩包包含不安全路径。")
            unix_mode = member.external_attr >> 16
            if stat.S_IFMT(unix_mode) == stat.S_IFLNK:
                raise RuntimeError("组件压缩包不能包含符号链接。")
            if member.is_dir():
                continue
            archive_name = Path(member.filename.replace("\\", "/"))
            if not archive_name.parts or archive_name.parts[0] != component.archive_directory:
                raise RuntimeError("组件压缩包结构不正确。")
            if archive_name.suffix.lower() not in component.allowed_extensions:
                raise RuntimeError("组件压缩包包含不允许的文件类型。")
        archive.extractall(extracted_root)


def install_component(
    component_id: str,
    root: Path = PROJECT_ROOT,
    progress: ProgressCallback | None = None,
) -> Path:
    component = COMPONENTS_BY_ID[component_id]
    destination = (root / component.install_directory).resolve()
    project_root = root.resolve()
    if project_root not in destination.parents:
        raise RuntimeError("组件安装路径不安全。")
    if component_installed(component_id, root):
        return destination

    download_root = root / "data" / "downloads"
    download_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="component-", dir=download_root) as temporary:
        temporary_root = Path(temporary)
        archive_path = temporary_root / "component.zip"
        request = Request(component.download_url, headers={"User-Agent": "MaoMao/0.1"})
        with urlopen(request, timeout=90) as response, archive_path.open("wb") as output:
            total = int(response.headers.get("Content-Length") or 0)
            received = 0
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                output.write(block)
                received += len(block)
                if received > component.max_download_bytes:
                    raise RuntimeError("组件下载大小超出限制。")
                if progress is not None and total:
                    progress(min(95, int(received * 95 / total)))

        extracted_root = temporary_root / "extracted"
        extract_component_archive(archive_path, extracted_root, component)

        source = extracted_root / component.archive_directory
        if not (source / component.required_file).is_file():
            raise RuntimeError("下载的组件内容不完整。")
        if destination.exists():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))

    if progress is not None:
        progress(100)
    return destination
