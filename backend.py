"""File-system and process helpers used by the Dpyle GUI."""

from __future__ import annotations

import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import requests


PROJECT_ROOT = Path(__file__).resolve().parent
WORK_FOLDER = PROJECT_ROOT / "WorkFolder"
EXTRACTOR = PROJECT_ROOT / "Extractor.py"
INDEX_FILE_NAME = "index.xml"
INDEX_FILE = WORK_FOLDER / INDEX_FILE_NAME
PYLINGUAL_API_URL = "https://api.pylingual.io"
POLL_INTERVAL_SECONDS = 2
MAX_DECOMPILATION_WAIT_SECONDS = 1200
DISCORD_WEBHOOK_PATTERN = re.compile(
    r"https?://(?:discord(?:app)?\.com)/api/(?:v\d+/)?webhooks/"
    r"(?P<webhook_id>\d+)/(?P<token>[A-Za-z0-9._-]+)",
    re.IGNORECASE,
)
RESOURCE_FILE_ENTRY_PATTERN = re.compile(
    r'^\s*(?:"[^"]+"|\S+)\s+'
    r'(?:"(?P<quoted_type>[^"]+)"|(?P<type>\S+))\s+'
    r'"(?P<file>[^"]+)"\s*$'
)
INLINE_TEXT_RESOURCE_PATTERN = re.compile(
    r'^\s*(?P<name>"[^"]+"|\S+)\s+'
    r'(?P<type>VERSIONINFO|MANIFEST|RT_VERSION|RT_MANIFEST|16|24)\s*$',
    re.IGNORECASE,
)
RESOURCE_TYPE_NAMES = {
    "16": "VERSION",
    "24": "MANIFEST",
    "VERSIONINFO": "VERSION",
    "VERSION_INFO": "VERSION",
    "RT_VERSION": "VERSION",
    "RT_MANIFEST": "MANIFEST",
    "RT_GROUP_ICON": "ICON",
}


@dataclass(frozen=True)
class DiscordWebhook:
    url: str
    webhook_id: str
    token: str
    lines: tuple[int, ...]


@dataclass(frozen=True)
class DiscordWebhookCheck:
    status: str
    name: str | None = None
    avatar_data: bytes | None = None


def find_discord_webhooks(content: str) -> list[DiscordWebhook]:
    """Find literal Discord webhook URLs in source without contacting Discord."""
    matches: dict[tuple[str, str], tuple[str, set[int]]] = {}
    for match in DISCORD_WEBHOOK_PATTERN.finditer(content):
        url = match.group(0).rstrip(".,;:!?)]}'\"")
        webhook_id = match.group("webhook_id")
        token = match.group("token")
        key = (webhook_id, token)
        line = content.count("\n", 0, match.start()) + 1
        if key not in matches:
            matches[key] = (url, set())
        matches[key][1].add(line)
    return [
        DiscordWebhook(url, webhook_id, token, tuple(sorted(lines)))
        for (webhook_id, token), (url, lines) in matches.items()
    ]


def check_discord_webhook(webhook: DiscordWebhook) -> DiscordWebhookCheck:
    """Get webhook identity and existence with a read-only Discord API request."""
    response = requests.get(webhook.url, timeout=10)
    if response.status_code == 200:
        try:
            data = response.json()
        except requests.exceptions.JSONDecodeError:
            return DiscordWebhookCheck("invalid_response")
        if not isinstance(data, dict):
            return DiscordWebhookCheck("invalid_response")
        name = data.get("name")
        avatar_hash = data.get("avatar")
        avatar_data = None
        if isinstance(avatar_hash, str) and avatar_hash:
            avatar_url = (
                f"https://cdn.discordapp.com/avatars/"
                f"{webhook.webhook_id}/{avatar_hash}.png?size=64"
            )
        else:
            avatar_url = (
                "https://cdn.discordapp.com/embed/avatars/"
                f"{int(webhook.webhook_id) % 6}.png"
            )
        try:
            avatar_response = requests.get(avatar_url, timeout=10)
            avatar_response.raise_for_status()
            if avatar_response.headers.get("Content-Type", "").startswith("image/"):
                avatar_data = avatar_response.content
        except requests.RequestException:
            pass
        return DiscordWebhookCheck(
            "exists",
            name if isinstance(name, str) and name else None,
            avatar_data,
        )
    if response.status_code == 404:
        return DiscordWebhookCheck("missing")
    return DiscordWebhookCheck(f"http_{response.status_code}")


def send_discord_webhook_message(webhook: DiscordWebhook, message: str) -> str:
    """Send one user-submitted message to a Discord webhook."""
    response = requests.post(
        webhook.url,
        json={
            "content": message,
            "allowed_mentions": {"parse": []},
        },
        timeout=10,
    )
    if response.status_code in (200, 204):
        return "sent"
    if response.status_code == 404:
        return "missing"
    return f"http_{response.status_code}"


def reset_work_folder() -> None:
    """Remove files left by a previous extraction."""
    WORK_FOLDER.mkdir(parents=True, exist_ok=True)
    for path in WORK_FOLDER.iterdir():
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()


def create_index() -> None:
    """Create an index for a fresh extraction."""
    root = ET.Element("dpyle-index", {"version": "1"})
    hidden = ET.SubElement(root, "hidden")
    ET.SubElement(hidden, "file").text = INDEX_FILE_NAME
    ET.SubElement(root, "decompilations")
    ET.SubElement(root, "resource-extractions")
    save_index(root)


def load_index() -> ET.Element:
    """Load the extraction index, creating it if necessary."""
    if not INDEX_FILE.is_file():
        create_index()
    return ET.parse(INDEX_FILE).getroot()


def save_index(root: ET.Element) -> None:
    """Persist the extraction index using UTF-8 XML."""
    ET.indent(root, space="  ")
    ET.ElementTree(root).write(INDEX_FILE, encoding="utf-8", xml_declaration=True)


def hidden_files() -> set[str]:
    """Return relative paths excluded from the GUI file list."""
    root = load_index()
    hidden = root.find("hidden")
    return {
        element.text
        for element in hidden.findall("file")
        if element.text
    } if hidden is not None else set()


def decompiled_file_for(relative_path: str) -> str | None:
    """Return the decompiled path associated with a bytecode path."""
    root = load_index()
    decompilations = root.find("decompilations")
    if decompilations is None:
        return None
    for entry in decompilations.findall("file"):
        if entry.get("pyc") == relative_path:
            return entry.get("py")
    return None


def record_decompilation(pyc_path: str, py_path: str) -> None:
    """Hide a decompiled bytecode file and save its source mapping."""
    root = load_index()
    hidden = root.find("hidden")
    decompilations = root.find("decompilations")
    if hidden is None:
        hidden = ET.SubElement(root, "hidden")
    if decompilations is None:
        decompilations = ET.SubElement(root, "decompilations")

    for element in list(decompilations.findall("file")):
        if element.get("pyc") == pyc_path:
            decompilations.remove(element)
    ET.SubElement(decompilations, "file", {"pyc": pyc_path, "py": py_path})

    if not any(element.text == pyc_path for element in hidden.findall("file")):
        ET.SubElement(hidden, "file").text = pyc_path
    save_index(root)


def resource_folders() -> dict[str, str]:
    """Return extracted resource folders and their original DLL/PYD paths."""
    root = load_index()
    extractions = root.find("resource-extractions")
    if extractions is None:
        return {}
    folders = {}
    for entry in extractions.findall("directory"):
        path = entry.get("path")
        file_path = entry.get("file")
        if path and file_path:
            folders[path] = file_path
    return folders


def resource_file_for_directory(relative_path: str) -> str | None:
    """Return the original DLL or PYD path for an extracted resource folder."""
    return resource_folders().get(relative_path)


def resource_extraction_format(relative_path: str) -> str | None:
    """Return the stored output-format version for an extracted resource folder."""
    root = load_index()
    extractions = root.find("resource-extractions")
    if extractions is None:
        return None
    for entry in extractions.findall("directory"):
        if entry.get("path") == relative_path:
            return entry.get("format")
    return None


def resource_file_for_member(relative_path: str) -> str | None:
    """Return the source DLL/PYD associated with an extracted resource file."""
    for directory, file_path in sorted(
        resource_folders().items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        if relative_path.startswith(f"{directory}/"):
            return file_path
    return None


def _resource_files_from_script(script_path: Path) -> dict[str, str]:
    """Map extracted filenames to their resource types in Resource Hacker's RC."""
    if not script_path.is_file():
        return {}

    resource_files: dict[str, str] = {}
    for line in script_path.read_text(encoding="utf-16").splitlines():
        match = RESOURCE_FILE_ENTRY_PATTERN.match(line)
        if not match:
            continue
        raw_type = (match.group("quoted_type") or match.group("type")).upper()
        resource_type = RESOURCE_TYPE_NAMES.get(raw_type, raw_type)
        filename = re.split(r"[\\/]", match.group("file"))[-1]
        resource_files[filename] = resource_type
    return resource_files


def _inline_text_resources_from_script(
    script_path: Path,
) -> list[tuple[str, str, str]]:
    """Return inline manifest and version blocks that Resource Hacker did not save."""
    if not script_path.is_file():
        return []

    lines = script_path.read_text(encoding="utf-16").splitlines()
    resources = []
    index = 0
    while index < len(lines):
        match = INLINE_TEXT_RESOURCE_PATTERN.match(lines[index])
        if not match:
            index += 1
            continue

        raw_type = match.group("type").upper()
        resource_type = RESOURCE_TYPE_NAMES.get(raw_type, raw_type)
        resource_name = match.group("name").strip('"')
        block = [lines[index]]
        depth = 0
        opened = False
        index += 1
        while index < len(lines):
            line = lines[index]
            block.append(line)
            stripped = line.strip()
            if stripped == "{":
                depth += 1
                opened = True
            elif stripped == "}":
                depth -= 1
                if opened and depth == 0:
                    break
            index += 1
        if opened and depth == 0:
            resources.append(
                (resource_type, resource_name, "\n".join(block).rstrip() + "\n")
            )
        index += 1
    return resources


def _resource_directory_name(resource_type: str) -> str:
    """Return a Windows-safe directory name for a resource type."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", resource_type).rstrip(" .")
    if not name or name.upper() in {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }:
        return f"TYPE_{name or 'RESOURCE'}"
    return name


def record_resource_extraction(directory: str, file_path: str) -> None:
    """Hide an extracted DLL/PYD and map its virtual folder to the source file."""
    root = load_index()
    hidden = root.find("hidden")
    if hidden is None:
        hidden = ET.SubElement(root, "hidden")
    extractions = root.find("resource-extractions")
    if extractions is None:
        extractions = ET.SubElement(root, "resource-extractions")

    for element in list(extractions.findall("directory")):
        if element.get("file") == file_path or element.get("path") == directory:
            extractions.remove(element)
    ET.SubElement(
        extractions,
        "directory",
        {"path": directory, "file": file_path, "format": "2"},
    )
    if not any(element.text == file_path for element in hidden.findall("file")):
        ET.SubElement(hidden, "file").text = file_path
    save_index(root)


def extract_resources(file_path: Path) -> Path:
    """Extract a DLL or PYD into resource-type folders using Resource Hacker CLI."""
    resource_hacker = PROJECT_ROOT / "ResourceHacker.exe"
    if not resource_hacker.is_file():
        raise FileNotFoundError(f"{resource_hacker.name} est introuvable.")

    source_path = file_path.resolve()
    work_folder = WORK_FOLDER.resolve()
    try:
        relative_file = source_path.relative_to(work_folder).as_posix()
    except ValueError as error:
        raise ValueError("Le fichier doit se trouver dans WorkFolder.") from error
    if not source_path.is_file():
        raise FileNotFoundError(f"{source_path.name} est introuvable.")
    if source_path.suffix.lower() not in {".dll", ".pyd"}:
        raise ValueError("Seuls les fichiers DLL et PYD peuvent être extraits.")
    with source_path.open("rb") as source:
        if source.read(2) != b"MZ":
            raise ValueError(f"{source_path.name} n'est pas un fichier PE valide.")
        source.seek(0x3C)
        pe_offset = source.read(4)
        if len(pe_offset) != 4:
            raise ValueError(f"{source_path.name} n'est pas un fichier PE valide.")
        source.seek(struct.unpack("<I", pe_offset)[0])
        if source.read(4) != b"PE\0\0":
            raise ValueError(f"{source_path.name} n'est pas un fichier PE valide.")

    source_relative_path = Path(relative_file)
    extracted_name = f"EXTRACTED_{source_path.stem}"
    relative_directory = (
        source_relative_path.parent / extracted_name
    ).as_posix()
    destination = work_folder / source_relative_path.parent / extracted_name
    mapped_file = resource_file_for_directory(relative_directory)
    migrate_legacy_extraction = False
    if mapped_file == relative_file and destination.is_dir():
        if resource_extraction_format(relative_directory) == "2":
            return destination
        migrate_legacy_extraction = True
    elif destination.exists():
        raise FileExistsError(
            f"Le dossier {destination.name} existe déjà et ne correspond pas "
            "à cette extraction."
        )

    with tempfile.TemporaryDirectory(prefix="dpyle-resource-") as temporary:
        staging = Path(temporary)
        all_resources = staging / "all"
        all_resources.mkdir()
        subprocess.run(
            [
                str(resource_hacker),
                "-open",
                str(source_path),
                "-save",
                str(all_resources),
                "-action",
                "extract",
                "-mask",
                ",,,",
                "-log",
                "NUL",
            ],
            cwd=PROJECT_ROOT,
            check=True,
        )

        all_script = all_resources / "ExtractedResources.rc"
        resource_files = _resource_files_from_script(all_script)
        inline_text_resources = _inline_text_resources_from_script(all_script)
        extracted_files = [
            path
            for path in all_resources.iterdir()
            if path != all_script and path.is_file()
        ]
        assembled = staging / "assembled"
        assembled.mkdir()
        for extracted_file in extracted_files:
            display_name = resource_files.get(extracted_file.name, "OTHER")
            target_name = _resource_directory_name(display_name)
            target_directory = assembled / target_name
            target_directory.mkdir(exist_ok=True)
            output_file = target_directory / extracted_file.name
            if extracted_file.suffix.lower() == ".txt":
                data = extracted_file.read_bytes()
                encoding = "utf-16" if data.startswith(
                    (b"\xff\xfe", b"\xfe\xff")
                ) else "utf-8-sig"
                output_file.write_text(
                    data.decode(encoding),
                    encoding="utf-8",
                )
            else:
                shutil.copy2(extracted_file, output_file)

        for display_name, resource_name, content in inline_text_resources:
            target_directory = assembled / _resource_directory_name(
                display_name
            )
            target_directory.mkdir(exist_ok=True)
            filename_base = _resource_directory_name(resource_name)
            filename = f"{filename_base}_{display_name}.txt"
            output_file = target_directory / filename
            suffix = 2
            while output_file.exists():
                output_file = target_directory / (
                    f"{filename_base}_{display_name}_{suffix}.txt"
                )
                suffix += 1
            output_file.write_text(content, encoding="utf-8")

        if (
            not extracted_files
            and not inline_text_resources
            and all_script.is_file()
        ):
            fallback_directory = assembled / "RESOURCES"
            fallback_directory.mkdir()
            shutil.copy2(
                all_script,
                fallback_directory / all_script.name,
            )

        backup = staging / "previous-extraction"
        if migrate_legacy_extraction:
            shutil.move(str(destination), str(backup))
        try:
            shutil.move(str(assembled), str(destination))
            record_resource_extraction(relative_directory, relative_file)
        except (OSError, subprocess.SubprocessError, ET.ParseError, UnicodeError):
            if destination.is_dir():
                shutil.rmtree(destination)
            if backup.is_dir():
                shutil.move(str(backup), str(destination))
            raise
    return destination


def launch_extractor(executable: str) -> None:
    """Extract a PyInstaller executable into the application's work folder."""
    subprocess.run(
        [sys.executable, str(EXTRACTOR), executable],
        cwd=PROJECT_ROOT,
        check=True,
    )
    create_index()


def extract_code(value: object) -> str | None:
    """Extract source text from the API's flexible result structure."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("editor_content", "content", "code", "text", "source"):
            if key in value:
                found = extract_code(value[key])
                if found:
                    return found
        candidates = [
            code
            for code in (extract_code(item) for item in value.values())
            if code
        ]
        return max(candidates, key=len) if candidates else None
    if isinstance(value, list):
        parts = [
            code
            for code in (extract_code(item) for item in value)
            if code
        ]
        return "\n".join(parts) if parts else None
    return None


def decompile_with_api(pyc_path: Path) -> str:
    """Upload a bytecode file to PyLingual and return its Python source."""
    with pyc_path.open("rb") as bytecode:
        response = requests.post(
            f"{PYLINGUAL_API_URL}/upload",
            files={
                "file": (
                    pyc_path.name,
                    bytecode,
                    "application/octet-stream",
                )
            },
            timeout=60,
        )
    response.raise_for_status()
    upload_result = response.json()
    if not isinstance(upload_result, dict):
        raise RuntimeError("Réponse d'upload PyLingual invalide.")
    if not upload_result.get("success"):
        raise RuntimeError(f"Upload PyLingual refusé : {upload_result}")
    identifier = upload_result.get("identifier")
    if not identifier:
        raise RuntimeError("L'API PyLingual n'a pas retourné d'identifiant.")

    deadline = time.monotonic() + MAX_DECOMPILATION_WAIT_SECONDS
    last_stage: object = None
    while time.monotonic() < deadline:
        response = requests.get(
            f"{PYLINGUAL_API_URL}/get_progress",
            params={"identifier": identifier},
            timeout=30,
        )
        response.raise_for_status()
        progress = response.json()
        if not isinstance(progress, dict):
            raise RuntimeError("Réponse de progression PyLingual invalide.")
        if not progress.get("success", False):
            raise RuntimeError(f"Échec du traitement PyLingual : {progress}")

        stage = progress.get("stage")
        if stage != last_stage:
            print(f"[*] Étape PyLingual : {stage}")
            last_stage = stage
        if stage == "done":
            break
        time.sleep(POLL_INTERVAL_SECONDS)
    else:
        raise TimeoutError(
            "Délai dépassé en attendant la fin du traitement PyLingual."
        )

    response = requests.get(
        f"{PYLINGUAL_API_URL}/view_chimera",
        params={"identifier": identifier},
        timeout=60,
    )
    response.raise_for_status()
    result = response.json()
    if not isinstance(result, dict):
        raise RuntimeError("Réponse de résultat PyLingual invalide.")
    try:
        raw_code = result["editor_content"]["file_raw_python"]
    except (KeyError, TypeError) as error:
        raise RuntimeError(
            "La réponse PyLingual ne contient pas editor_content/file_raw_python."
        ) from error

    code = extract_code(raw_code)
    if code is None:
        raise RuntimeError(
            "La réponse PyLingual ne contient aucun code source exploitable."
        )
    return code
