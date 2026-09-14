from __future__ import annotations

import errno
import fcntl
import os
import re
import tempfile
from pathlib import Path
from types import TracebackType
from typing import Self

from pydantic import ValidationError

from .models import Snapshot

_STATE_ENV = "OLLEMOLLE_STATE_DIR"
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class OllemolleError(Exception):
    """Base class for expected CLI errors."""


class SnapshotNameError(OllemolleError):
    """Raised when a snapshot name is unsafe."""


class SnapshotMissingError(OllemolleError):
    """Raised when a requested snapshot does not exist."""


class SnapshotMalformedError(OllemolleError):
    """Raised when a snapshot exists but is not a valid snapshot."""


class RestoreLockError(OllemolleError):
    """Raised when another restore invocation is active."""


def state_root() -> Path:
    configured = os.environ.get(_STATE_ENV)
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_absolute() else path.resolve()
    return Path.home() / ".local" / "state" / "ollemolle"


def ensure_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def normalize_snapshot_name(name: str | None) -> str:
    raw_name = "latest" if name is None else name.strip()
    raw_name = raw_name.removesuffix(".json")
    if raw_name in {"", ".", ".."} or not _SAFE_NAME.fullmatch(raw_name):
        raise SnapshotNameError(
            "snapshot name must start with a letter or digit and contain only letters, digits, '.', '_' or '-'"
        )
    return raw_name


def snapshot_path(name: str | None = None) -> Path:
    safe_name = normalize_snapshot_name(name)
    return state_root() / f"{safe_name}.json"


def write_snapshot(snapshot: Snapshot, name: str | None = None) -> Path:
    path = snapshot_path(name)
    ensure_private_dir(path.parent)
    data = snapshot.model_dump_json(indent=2).encode("utf-8") + b"\n"
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    os.chmod(temporary, 0o600)
    try:
        with os.fdopen(file_descriptor, "wb") as handle:
            file_descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except Exception:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    return path


def read_snapshot(name: str | None = None) -> Snapshot:
    path = snapshot_path(name)
    try:
        payload = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise SnapshotMissingError(f"snapshot not found: {path}") from exc
    try:
        return Snapshot.model_validate_json(payload)
    except ValidationError as exc:
        raise SnapshotMalformedError(f"malformed snapshot: {path}: {exc}") from exc
    except ValueError as exc:
        raise SnapshotMalformedError(f"malformed snapshot: {path}: {exc}") from exc


class RestoreLock:
    def __init__(self) -> None:
        self._path = state_root() / ".restore.lock"
        self._file_descriptor = -1

    def __enter__(self) -> Self:
        ensure_private_dir(self._path.parent)
        self._file_descriptor = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self._file_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(self._file_descriptor)
            self._file_descriptor = -1
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise RestoreLockError(
                    "another ollemolle restore invocation is already running"
                ) from exc
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._file_descriptor >= 0:
            try:
                fcntl.flock(self._file_descriptor, fcntl.LOCK_UN)
            finally:
                os.close(self._file_descriptor)
                self._file_descriptor = -1
