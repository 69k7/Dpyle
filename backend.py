"""File-system and process helpers used by the Dpyle GUI."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
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
