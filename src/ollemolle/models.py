"""Typed data boundaries for ollemolle snapshots."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Session(_FrozenModel):
    tool: Literal["omp", "claude"]
    cwd: Path
    session_id: str
    session_file: Path
    executable: Path
    label: str
    tty: str
    pid: int = Field(gt=0)
    process_started: str
    resume_args: tuple[str, ...] = ()

    @field_validator("cwd", "session_file", "executable")
    @classmethod
    def _absolute_path(cls, value: Path) -> Path:
        path = value.expanduser()
        if not path.is_absolute():
            raise ValueError("path must be absolute")
        return path

    @field_validator("session_id", "label", "tty", "process_started")
    @classmethod
    def _nonempty_string(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must be nonempty")
        return value

    @field_validator("resume_args")
    @classmethod
    def _nonempty_resume_args(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("resume arguments must be nonempty")
        return value

    @model_validator(mode="after")
    def _resume_excludes_executable(self) -> Session:
        executable_name = self.executable.name
        if self.resume_args and self.resume_args[0] == str(self.executable):
            raise ValueError("resume_args must exclude executable")
        if self.resume_args and self.resume_args[0] == executable_name:
            raise ValueError("resume_args must exclude executable basename")
        return self

    @field_serializer("cwd", "session_file", "executable")
    def _serialize_path(self, value: Path) -> str:
        return str(value)


class Issue(_FrozenModel):
    label: str
    detail: str

    @field_validator("label", "detail")
    @classmethod
    def _nonempty_string(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must be nonempty")
        return value


class Discovery(_FrozenModel):
    sessions: tuple[Session, ...] = ()
    issues: tuple[Issue, ...] = ()


class Snapshot(_FrozenModel):
    version: Literal[1] = 1
    saved_at: datetime
    sessions: tuple[Session, ...]

    @field_validator("saved_at")
    @classmethod
    def _aware_saved_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("saved_at must be timezone-aware")
        return value
