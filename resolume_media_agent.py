#!/usr/bin/env python3
"""Pull media from a web-app manifest into local Resolume-readable folders."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import logging.handlers
import os
import signal
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


DEFAULT_ALLOWED_EXTENSIONS = {
    ".3gp",
    ".aif",
    ".aiff",
    ".avi",
    ".bmp",
    ".gif",
    ".jpeg",
    ".jpg",
    ".m4a",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".png",
    ".tif",
    ".tiff",
    ".wav",
    ".webm",
    ".wmv",
}

INVALID_WINDOWS_CHARS = '<>:"|?*'
LOGGER = logging.getLogger("resolume_media_agent")
SHOULD_STOP = False


@dataclass(frozen=True)
class ServerConfig:
    manifest_url: str
    auth_token: str | None
    timeout_seconds: float
    verify_tls: bool


@dataclass(frozen=True)
class LocalConfig:
    media_root: Path
    temp_root: Path
    log_file: Path
    allowed_extensions: set[str]
    max_file_size_bytes: int | None
    delete_removed: bool


@dataclass(frozen=True)
class SyncConfig:
    poll_interval_seconds: float


@dataclass(frozen=True)
class Config:
    server: ServerConfig
    local: LocalConfig
    sync: SyncConfig


@dataclass(frozen=True)
class ManifestEntry:
    path: str
    url: str
    sha256: str | None
    size: int | None


class AgentError(Exception):
    """Expected runtime error that should be logged without a stack trace."""


def handle_stop_signal(signum: int, _frame: object) -> None:
    global SHOULD_STOP
    LOGGER.info("Received signal %s; stopping after current operation", signum)
    SHOULD_STOP = True


def load_config(config_path: Path) -> Config:
    with config_path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)

    server_raw = raw.get("server", {})
    local_raw = raw.get("local", {})
    sync_raw = raw.get("sync", {})

    manifest_url = str(server_raw.get("manifest_url", "")).strip()
    if not manifest_url:
        raise AgentError("config server.manifest_url is required")

    media_root_value = local_raw.get("media_root")
    if not media_root_value:
        raise AgentError("config local.media_root is required")

    media_root = Path(str(media_root_value)).expanduser()
    temp_root = Path(str(local_raw.get("temp_root") or media_root / ".agent_tmp")).expanduser()
    log_file = Path(
        str(local_raw.get("log_file") or config_path.parent / "logs" / "resolume_media_agent.log")
    ).expanduser()

    allowed_extensions = {
        normalize_extension(ext)
        for ext in local_raw.get("allowed_extensions", sorted(DEFAULT_ALLOWED_EXTENSIONS))
    }
    max_mb = local_raw.get("max_file_size_mb")
    max_file_size_bytes = None if max_mb in (None, "", 0) else int(float(max_mb) * 1024 * 1024)

    return Config(
        server=ServerConfig(
            manifest_url=manifest_url,
            auth_token=(str(server_raw.get("auth_token")).strip() or None)
            if server_raw.get("auth_token") is not None
            else None,
            timeout_seconds=float(server_raw.get("timeout_seconds", 30)),
            verify_tls=bool(server_raw.get("verify_tls", True)),
        ),
        local=LocalConfig(
            media_root=media_root,
            temp_root=temp_root,
            log_file=log_file,
            allowed_extensions=allowed_extensions,
            max_file_size_bytes=max_file_size_bytes,
            delete_removed=bool(local_raw.get("delete_removed", False)),
        ),
        sync=SyncConfig(
            poll_interval_seconds=float(sync_raw.get("poll_interval_seconds", 30)),
        ),
    )


def normalize_extension(extension: str) -> str:
    extension = str(extension).strip().lower()
    if not extension:
        return extension
    return extension if extension.startswith(".") else f".{extension}"


def configure_logging(log_file: Path, verbose: bool) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    logging.basicConfig(level=level, handlers=[file_handler, stream_handler])


def request_json(config: Config) -> Any:
    request = build_request(config.server.manifest_url, config)
    with urllib.request.urlopen(
        request,
        timeout=config.server.timeout_seconds,
        context=ssl_context(config),
    ) as response:
        body = response.read()
    return json.loads(body.decode("utf-8"))


def build_request(url: str, config: Config) -> urllib.request.Request:
    headers = {"User-Agent": "CompanionAgent/1.0"}
    if config.server.auth_token:
        headers["Authorization"] = f"Bearer {config.server.auth_token}"
    return urllib.request.Request(url, headers=headers)


def ssl_context(config: Config) -> ssl.SSLContext | None:
    if config.server.verify_tls:
        return None
    return ssl._create_unverified_context()  # noqa: SLF001 - stdlib-supported escape hatch.


def parse_manifest(raw: Any, manifest_url: str) -> list[ManifestEntry]:
    raw_files = raw.get("files") if isinstance(raw, dict) else raw
    if not isinstance(raw_files, list):
        raise AgentError("manifest must be a JSON list or an object with a files list")

    entries: list[ManifestEntry] = []
    for index, item in enumerate(raw_files):
        if not isinstance(item, dict):
            raise AgentError(f"manifest item {index} must be an object")

        path = str(item.get("path", "")).strip()
        url = str(item.get("url", "")).strip()
        if not path or not url:
            raise AgentError(f"manifest item {index} requires path and url")

        sha256 = item.get("sha256")
        if sha256 is not None:
            sha256 = str(sha256).strip().lower()
            if len(sha256) != 64:
                raise AgentError(f"manifest item {path} has invalid sha256")

        size = item.get("size")
        if size in (None, ""):
            parsed_size = None
        else:
            parsed_size = int(size)
            if parsed_size < 0:
                raise AgentError(f"manifest item {path} has negative size")

        entries.append(
            ManifestEntry(
                path=path,
                url=urllib.parse.urljoin(manifest_url, url),
                sha256=sha256,
                size=parsed_size,
            )
        )

    return entries


def safe_relative_path(raw_path: str) -> Path:
    normalized = raw_path.replace("\\", "/")
    parts: list[str] = []
    for raw_part in normalized.split("/"):
        part = raw_part.strip().strip(". ")
        if not part:
            continue
        if raw_part in {".", ".."}:
            raise AgentError(f"unsafe relative path rejected: {raw_path}")
        for char in INVALID_WINDOWS_CHARS:
            part = part.replace(char, "_")
        parts.append(part)

    if not parts:
        raise AgentError(f"empty relative path rejected: {raw_path}")

    candidate = Path(*parts)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise AgentError(f"unsafe relative path rejected: {raw_path}")
    return candidate


def validate_entry(entry: ManifestEntry, config: Config) -> Path:
    relative_path = safe_relative_path(entry.path)
    extension = relative_path.suffix.lower()
    if extension not in config.local.allowed_extensions:
        raise AgentError(f"extension not allowed for {entry.path}: {extension}")
    if (
        entry.size is not None
        and config.local.max_file_size_bytes is not None
        and entry.size > config.local.max_file_size_bytes
    ):
        raise AgentError(f"file exceeds max size and was skipped: {entry.path}")
    return relative_path


def file_matches(path: Path, entry: ManifestEntry) -> bool:
    if not path.exists() or not path.is_file():
        return False
    if entry.size is not None and path.stat().st_size != entry.size:
        return False
    if entry.sha256 and sha256_file(path) != entry.sha256:
        return False
    if entry.size is None and entry.sha256 is None:
        return False
    return True


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_file(entry: ManifestEntry, destination: Path, config: Config) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    config.local.temp_root.mkdir(parents=True, exist_ok=True)
    temp_path = config.local.temp_root / f"{destination.name}.{os.getpid()}.part"

    bytes_downloaded = 0
    try:
        request = build_request(entry.url, config)
        with urllib.request.urlopen(
            request,
            timeout=config.server.timeout_seconds,
            context=ssl_context(config),
        ) as response, temp_path.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                bytes_downloaded += len(chunk)
                if (
                    config.local.max_file_size_bytes is not None
                    and bytes_downloaded > config.local.max_file_size_bytes
                ):
                    raise AgentError(f"download exceeded max size for {entry.path}")

        if entry.size is not None and temp_path.stat().st_size != entry.size:
            raise AgentError(f"downloaded size mismatch for {entry.path}")
        if entry.sha256 and sha256_file(temp_path) != entry.sha256:
            raise AgentError(f"downloaded sha256 mismatch for {entry.path}")

        os.replace(temp_path, destination)
        LOGGER.info("Downloaded %s to %s", entry.path, destination)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp_path.unlink()


def sync_once(config: Config) -> None:
    LOGGER.info("Fetching manifest from %s", config.server.manifest_url)
    entries = parse_manifest(request_json(config), config.server.manifest_url)
    expected_files: set[Path] = set()
    downloaded = 0
    skipped = 0
    failed = 0

    config.local.media_root.mkdir(parents=True, exist_ok=True)

    for entry in entries:
        try:
            relative_path = validate_entry(entry, config)
            destination = config.local.media_root / relative_path
            expected_files.add(destination.resolve())

            if file_matches(destination, entry):
                skipped += 1
                LOGGER.debug("Unchanged: %s", entry.path)
                continue

            download_file(entry, destination, config)
            downloaded += 1
        except Exception as exc:  # noqa: BLE001 - keep one bad media file from stopping sync.
            failed += 1
            LOGGER.error("Failed to sync %s: %s", entry.path, exc)

    if config.local.delete_removed:
        remove_missing_files(config, expected_files)

    LOGGER.info(
        "Sync complete: %s downloaded, %s unchanged, %s failed",
        downloaded,
        skipped,
        failed,
    )


def remove_missing_files(config: Config, expected_files: set[Path]) -> None:
    removed = 0
    for path in iter_media_files(config.local.media_root, config.local.allowed_extensions):
        if path.resolve() in expected_files:
            continue
        path.unlink()
        removed += 1
        LOGGER.info("Removed file no longer in manifest: %s", path)
    if removed:
        prune_empty_dirs(config.local.media_root)
        LOGGER.info("Removed %s stale files", removed)


def iter_media_files(root: Path, allowed_extensions: set[str]) -> Iterable[Path]:
    if not root.exists():
        return
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in allowed_extensions:
            continue
        if ".agent_tmp" in path.parts:
            continue
        yield path


def prune_empty_dirs(root: Path) -> None:
    for path in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
        with contextlib.suppress(OSError):
            path.rmdir()


@contextlib.contextmanager
def single_instance(lock_path: Path) -> Iterable[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise AgentError(f"another agent instance appears to be running: {lock_path}") from exc

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
        yield
    finally:
        with contextlib.suppress(FileNotFoundError):
            lock_path.unlink()


def run_loop(config: Config) -> None:
    while not SHOULD_STOP:
        try:
            sync_once(config)
        except (urllib.error.URLError, TimeoutError, AgentError, json.JSONDecodeError) as exc:
            LOGGER.error("Sync cycle failed: %s", exc)
        except Exception:
            LOGGER.exception("Unexpected sync cycle failure")

        sleep_until = time.monotonic() + config.sync.poll_interval_seconds
        while not SHOULD_STOP and time.monotonic() < sleep_until:
            time.sleep(min(1.0, sleep_until - time.monotonic()))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync web-app media into a local Resolume folder.")
    parser.add_argument("--config", required=True, help="Path to config.json")
    parser.add_argument("--once", action="store_true", help="Run one sync and exit")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = Path(args.config).expanduser().resolve()

    try:
        config = load_config(config_path)
        configure_logging(config.local.log_file, args.verbose)
        signal.signal(signal.SIGINT, handle_stop_signal)
        signal.signal(signal.SIGTERM, handle_stop_signal)

        LOGGER.info("Starting CompanionAgent with config %s", config_path)
        lock_path = config.local.temp_root / "resolume_media_agent.lock"
        with single_instance(lock_path):
            if args.once:
                sync_once(config)
            else:
                run_loop(config)
        LOGGER.info("CompanionAgent stopped")
        return 0
    except AgentError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
