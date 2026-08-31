from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

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
    archive_format: str = "zip"
    sha256: str = ""
    included_files: tuple[str, ...] = ()
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
        ".json",
        ".mat",
        ".mdl",
        ".onnx",
        ".stats",
        ".txt",
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
    RuntimeComponent(
        id="wake-word-checker-model",
        name="高精度中文唤醒检查模型",
        description="下载约 32 MB、安装约 5 MB，用于本地二次检查唤醒词，不占用显存。",
        download_url=(
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/"
            "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01.tar.bz2"
        ),
        archive_directory="sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01",
        install_directory=(
            "engines/sherpa-onnx/"
            "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
        ),
        required_file="encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
        archive_format="tar.bz2",
        sha256="B2F7C89690DC8CE4C6ED6AFEAB7CD800C36AD1421FB6B6302B4A4B194CF7F35F",
        included_files=(
            "tokens.txt",
            "keywords.txt",
            "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
            "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
            "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
        ),
        max_download_bytes=40 * 1024 * 1024,
        max_extracted_bytes=80 * 1024 * 1024,
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
    required_files = component.included_files or (component.required_file,)
    install_root = root / component.install_directory
    return all((install_root / relative).is_file() for relative in required_files)


def extract_component_archive(
    archive_path: Path,
    extracted_root: Path,
    component: RuntimeComponent,
) -> None:
    """Extract data-only component files with strict path and size checks."""
    import shutil
    import stat
    import tarfile
    import zipfile

    extracted_root.mkdir()
    extracted_resolved = extracted_root.resolve()
    selected = set(component.included_files)
    if component.archive_format == "tar.bz2":
        with tarfile.open(archive_path, mode="r:bz2") as archive:
            members = archive.getmembers()
            if len(members) > 2000:
                raise RuntimeError("组件压缩包包含过多文件。")
            if sum(member.size for member in members if member.isfile()) > component.max_extracted_bytes:
                raise RuntimeError("组件解压后的大小超出限制。")
            extracted_files: set[str] = set()
            for member in members:
                archive_name = Path(member.name.replace("\\", "/"))
                member_path = (extracted_root / archive_name).resolve()
                if member_path != extracted_resolved and extracted_resolved not in member_path.parents:
                    raise RuntimeError("组件压缩包包含不安全路径。")
                if member.issym() or member.islnk():
                    raise RuntimeError("组件压缩包不能包含符号链接。")
                if not member.isfile():
                    continue
                if not archive_name.parts or archive_name.parts[0] != component.archive_directory:
                    raise RuntimeError("组件压缩包结构不正确。")
                relative = Path(*archive_name.parts[1:]).as_posix()
                if selected and relative not in selected:
                    continue
                if archive_name.suffix.lower() not in component.allowed_extensions:
                    raise RuntimeError("组件压缩包包含不允许的文件类型。")
                source = archive.extractfile(member)
                if source is None:
                    raise RuntimeError("组件压缩包内容不完整。")
                member_path.parent.mkdir(parents=True, exist_ok=True)
                with source, member_path.open("wb") as output:
                    shutil.copyfileobj(source, output)
                extracted_files.add(relative)
            if selected and extracted_files != selected:
                raise RuntimeError("组件压缩包内容不完整。")
        return
    if component.archive_format != "zip":
        raise RuntimeError("不支持的组件压缩格式。")
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
    import hashlib
    import shutil
    import tempfile
    from urllib.request import Request, urlopen

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
        digest = hashlib.sha256()
        with urlopen(request, timeout=90) as response, archive_path.open("wb") as output:
            total = int(response.headers.get("Content-Length") or 0)
            received = 0
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                output.write(block)
                digest.update(block)
                received += len(block)
                if received > component.max_download_bytes:
                    raise RuntimeError("组件下载大小超出限制。")
                if progress is not None and total:
                    progress(min(95, int(received * 95 / total)))
        if component.sha256 and digest.hexdigest().upper() != component.sha256.upper():
            raise RuntimeError("组件下载校验失败。")

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
