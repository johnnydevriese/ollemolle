"""Discover live coding sessions and maintain Claude hook registrations."""

from __future__ import annotations

import fcntl
import os
import shlex
import shutil
import subprocess
from collections.abc import Generator, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)

from .models import Discovery, Issue, Session

_RUN_TIMEOUT_SECONDS = 5
_STATE_ENV = "OLLEMOLLE_STATE_DIR"
_DEFAULT_OMP_REGISTRY = Path.home() / ".omp" / "agent" / "terminal-sessions"
_CLAUDE_REGISTRY_NAME = "claude-live.json"
_CLAUDE_LOCK_NAME = "claude-live.lock"
_NO_CONTROLLING_TTY = frozenset({"?", "??"})


class DiscoveryCommandError(RuntimeError):
    """Raised when a bounded system discovery command fails."""


class _FrozenDiscoveryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProcessInfo(_FrozenDiscoveryModel):
    pid: int = Field(gt=0)
    ppid: int = Field(ge=0)
    controlling_tty: str | None
    comm: str = Field(min_length=1)
    command: str = Field(min_length=1)


class OmpHeader(_FrozenDiscoveryModel):
    session_id: str = Field(min_length=1)
    cwd: Path
    label_title: str = ""

    @field_validator("cwd")
    @classmethod
    def _cwd_is_absolute(cls, value: Path) -> Path:
        path = value.expanduser()
        if not path.is_absolute():
            raise ValueError("cwd must be absolute")
        if not path.is_dir():
            raise ValueError(f"cwd does not exist: {path}")
        return path


class OmpTitleLine(_FrozenDiscoveryModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    type: Literal["title"]
    title: str = ""


class OmpSessionLine(_FrozenDiscoveryModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    type: Literal["session"]
    id: str = Field(min_length=1)
    cwd: Path

    @field_validator("cwd")
    @classmethod
    def _cwd_is_absolute(cls, value: Path) -> Path:
        path = value.expanduser()
        if not path.is_absolute():
            raise ValueError("cwd must be absolute")
        if not path.is_dir():
            raise ValueError(f"cwd does not exist: {path}")
        return path


class OmpRuntimeConfig(_FrozenDiscoveryModel):
    profile: str | None = None

    @field_validator("profile")
    @classmethod
    def _profile_nonempty(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("profile must be nonempty")
        return value


class ClaudeHookPayload(_FrozenDiscoveryModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    session_id: str = Field(min_length=1)
    transcript_path: Path
    cwd: Path
    hook_event_name: Literal["SessionStart", "UserPromptSubmit", "SessionEnd"]
    agent_id: str | None = None

    @field_validator("transcript_path")
    @classmethod
    def _transcript_path_absolute(cls, value: Path) -> Path:
        path = value.expanduser()
        if not path.is_absolute():
            raise ValueError("transcript_path must be absolute")
        return path

    @field_validator("cwd")
    @classmethod
    def _cwd_exists(cls, value: Path) -> Path:
        path = value.expanduser()
        if not path.is_absolute():
            raise ValueError("cwd must be absolute")
        if not path.is_dir():
            raise ValueError(f"cwd does not exist: {path}")
        return path

    @field_validator("agent_id")
    @classmethod
    def _blank_agent_id_is_absent(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value if value.strip() else None


class ClaudeRegistration(_FrozenDiscoveryModel):
    cwd: Path
    session_id: str = Field(min_length=1)
    session_file: Path
    executable: Path
    label: str = Field(min_length=1)
    tty: str = Field(min_length=1)
    pid: int = Field(gt=0)
    process_started: str = Field(min_length=1)
    resume_args: tuple[str, ...]

    @field_validator("cwd", "session_file", "executable")
    @classmethod
    def _absolute_path(cls, value: Path) -> Path:
        path = value.expanduser()
        if not path.is_absolute():
            raise ValueError("path must be absolute")
        return path

    @field_validator("resume_args")
    @classmethod
    def _resume_args_nonempty(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("resume_args must be nonempty")
        if any(not item.strip() for item in value):
            raise ValueError("resume_args must contain only nonempty strings")
        return value

    @field_serializer("cwd", "session_file", "executable")
    def _serialize_path(self, value: Path) -> str:
        return str(value)


class ClaudeTranscriptHeader(_FrozenDiscoveryModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    sessionId: str | None = None
    session_id: str | None = None

    def embedded_session_id(self) -> str | None:
        return self.sessionId or self.session_id


class ClaudeRegistry(_FrozenDiscoveryModel):
    version: Literal[1] = 1
    entries: dict[str, ClaudeRegistration] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _keys_match_process_identity(self) -> Self:
        for key, entry in self.entries.items():
            expected = _registry_key(entry.pid, entry.process_started)
            if key != expected:
                raise ValueError(f"registry key {key!r} does not match {expected!r}")
        return self


def discover_sessions() -> Discovery:
    """Return verified live OMP and Claude sessions plus non-fatal issues."""
    issues: list[Issue] = []
    processes = _process_table(issues)
    registry = _read_claude_registry_for_discovery(
        _state_dir() / _CLAUDE_REGISTRY_NAME, issues
    )
    sessions: list[Session] = []
    sessions.extend(_discover_omp(processes, issues))
    sessions.extend(_discover_registered_claude(processes, registry.entries, issues))
    _report_unregistered_claude(processes, registry.entries, issues)
    sessions.sort(key=lambda session: (session.tool, session.tty, session.pid))
    return Discovery(sessions=tuple(sessions), issues=tuple(issues))


def register_claude(payload: str) -> None:
    """Register or unregister the live Claude session that invoked this hook."""
    try:
        hook_payload = ClaudeHookPayload.model_validate_json(payload)
    except ValidationError as exc:
        raise ValueError(f"Claude hook payload is invalid: {exc}") from exc
    if hook_payload.agent_id is not None:
        return

    issues: list[Issue] = []
    processes = _process_table(issues)
    if issues:
        raise RuntimeError(_joined_issues(issues))
    ancestor = _find_claude_ancestor(processes, os.getpid())
    if ancestor is None:
        raise RuntimeError("could not find live Claude ancestor for hook process")
    if not _is_descendant_of_exact(ancestor.pid, "ghostty", processes):
        return

    process_started = _process_started(ancestor.pid, issues)
    if process_started is None:
        raise RuntimeError(
            _joined_issues(issues)
            or f"could not read Claude process start time for pid {ancestor.pid}"
        )

    key = _registry_key(ancestor.pid, process_started)
    with _locked_claude_registry() as registry_path:
        registry = _read_claude_registry_or_raise(registry_path)
        entries = dict(registry.entries)
        if hook_payload.hook_event_name == "SessionEnd":
            entry = entries.get(key)
            if entry is not None and entry.session_id == hook_payload.session_id:
                del entries[key]
                _write_claude_registry(registry_path, ClaudeRegistry(entries=entries))
            return

        tty = _tty_for_pid(ancestor.pid, issues)
        if tty is None:
            raise RuntimeError(
                _joined_issues(issues)
                or f"could not read Claude tty for pid {ancestor.pid}"
            )
        executable = _claude_executable(ancestor, issues)
        if executable is None:
            raise RuntimeError(
                _joined_issues(issues)
                or f"could not resolve Claude executable for pid {ancestor.pid}"
            )
        entries[key] = ClaudeRegistration(
            cwd=hook_payload.cwd,
            session_id=hook_payload.session_id,
            session_file=hook_payload.transcript_path,
            executable=executable,
            label=_label(hook_payload.cwd, hook_payload.session_id, ""),
            tty=tty,
            pid=ancestor.pid,
            process_started=process_started,
            resume_args=("--resume", hook_payload.session_id),
        )
        _write_claude_registry(registry_path, ClaudeRegistry(entries=entries))


def _discover_omp(
    processes: dict[int, ProcessInfo], issues: list[Issue]
) -> Iterator[Session]:
    seen_ttys: set[str] = set()
    for process in sorted(processes.values(), key=lambda item: item.pid):
        if not _is_interactive_omp(process):
            continue
        if not _is_descendant_of_exact(process.pid, "ghostty", processes):
            continue
        config = _omp_runtime_config(process, issues)
        if config is None:
            continue
        actual_executable = _text_executable_for_basename(process.pid, "omp", issues)
        if actual_executable is None:
            issues.append(
                Issue(
                    label="omp executable",
                    detail=f"pid {process.pid} is named omp but its executable could not be verified",
                )
            )
            continue
        tty = _tty_for_pid(process.pid, issues)
        if tty is None:
            issues.append(
                Issue(
                    label="omp tty",
                    detail=f"pid {process.pid} has no readable /dev/ttys* fd0/fd1/fd2",
                )
            )
            continue
        if _has_flag(process, "--no-session"):
            issues.append(
                Issue(
                    label="omp session",
                    detail=f"live OMP pid {process.pid} tty {tty} was launched with --no-session and cannot be resumed",
                )
            )
            continue
        if tty in seen_ttys:
            issues.append(
                Issue(
                    label="omp tty",
                    detail=f"multiple live omp roots reported tty {tty}; skipping pid {process.pid}",
                )
            )
            continue
        seen_ttys.add(tty)
        process_started = _process_started(process.pid, issues)
        if process_started is None:
            issues.append(
                Issue(
                    label="omp start",
                    detail=f"could not read process start time for pid {process.pid}",
                )
            )
            continue
        session = _session_from_omp_record(
            record_path=_omp_registry_dir() / tty,
            process=process,
            process_started=process_started,
            tty=tty,
            executable=_preferred_executable("omp", actual_executable),
            runtime_config=config,
            issues=issues,
        )
        if session is not None:
            yield session


def _session_from_omp_record(
    *,
    record_path: Path,
    process: ProcessInfo,
    process_started: str,
    tty: str,
    executable: Path,
    runtime_config: OmpRuntimeConfig,
    issues: list[Issue],
) -> Session | None:
    if not record_path.exists():
        issues.append(
            Issue(
                label="omp registry",
                detail=f"no OMP terminal registry record for live pid {process.pid} tty {tty}: {record_path}",
            )
        )
        return None
    lines = _read_registry_lines(record_path, issues)
    if lines is None:
        return None
    if len(lines) < 2:
        issues.append(
            Issue(
                label="omp registry",
                detail=f"OMP registry record {record_path} must contain cwd and session file",
            )
        )
        return None
    cwd = Path(lines[0]).expanduser()
    session_file = Path(lines[1]).expanduser()
    if not (
        _registry_record_matches_start(record_path, process_started, issues)
        or _launched_as_omp_resume(process, runtime_config, session_file)
        or _holds_omp_session_lease(process.pid, session_file, issues)
    ):
        issues.append(
            Issue(
                label="omp registry",
                detail=f"registry record {record_path} predates live pid {process.pid}, which was not launched to resume that transcript and does not hold its OMP ownership lease; likely stale",
            )
        )
        return None
    if not cwd.is_absolute() or not cwd.is_dir():
        issues.append(
            Issue(
                label="omp cwd",
                detail=f"OMP registry cwd is not an existing absolute directory for tty {tty}: {cwd}",
            )
        )
        return None
    if not session_file.is_absolute() or not session_file.is_file():
        issues.append(
            Issue(
                label="omp transcript",
                detail=f"OMP session file is not an existing absolute file for tty {tty}: {session_file}",
            )
        )
        return None
    header = _read_omp_header(session_file, issues)
    if header is None:
        return None
    if header.cwd != cwd:
        issues.append(
            Issue(
                label="omp header",
                detail=f"registry cwd {cwd} does not match session header cwd {header.cwd}",
            )
        )
        return None
    return Session(
        tool="omp",
        cwd=cwd,
        session_id=header.session_id,
        session_file=session_file,
        executable=executable,
        label=_label(cwd, header.session_id, header.label_title),
        tty=tty,
        pid=process.pid,
        process_started=process_started,
        resume_args=_omp_resume_args(runtime_config, session_file),
    )


def _discover_registered_claude(
    processes: dict[int, ProcessInfo],
    entries: dict[str, ClaudeRegistration],
    issues: list[Issue],
) -> Iterator[Session]:
    for registration in sorted(entries.values(), key=lambda item: (item.tty, item.pid)):
        process = processes.get(registration.pid)
        if process is None:
            continue
        if not _is_descendant_of_exact(registration.pid, "ghostty", processes):
            continue
        current_started = _process_started(registration.pid, issues)
        if current_started != registration.process_started:
            issues.append(
                Issue(
                    label="claude registry",
                    detail=(
                        f"registered Claude pid {registration.pid} start time changed; "
                        "rejecting stale pid reuse"
                    ),
                )
            )
            continue
        if not _process_matches_claude(process):
            issues.append(
                Issue(
                    label="claude process",
                    detail=f"registered pid {registration.pid} no longer appears to be Claude",
                )
            )
            continue
        current_tty = _tty_for_pid(registration.pid, issues)
        if current_tty != registration.tty:
            issues.append(
                Issue(
                    label="claude tty",
                    detail=(
                        f"registered Claude pid {registration.pid} moved from "
                        f"{registration.tty} to {current_tty or 'unknown'}"
                    ),
                )
            )
            continue
        if not registration.cwd.is_dir():
            issues.append(
                Issue(
                    label="claude cwd",
                    detail=f"registered Claude cwd is not a directory: {registration.cwd}",
                )
            )
            continue
        if not registration.executable.is_file():
            issues.append(
                Issue(
                    label="claude executable",
                    detail=f"registered Claude executable is not a file: {registration.executable}",
                )
            )
            continue
        if not _claude_transcript_is_current(
            registration.session_file, registration.session_id, issues
        ):
            continue
        yield Session(
            tool="claude",
            cwd=registration.cwd,
            session_id=registration.session_id,
            session_file=registration.session_file,
            executable=registration.executable,
            label=registration.label,
            tty=registration.tty,
            pid=registration.pid,
            process_started=registration.process_started,
            resume_args=registration.resume_args,
        )


def _report_unregistered_claude(
    processes: dict[int, ProcessInfo],
    entries: dict[str, ClaudeRegistration],
    issues: list[Issue],
) -> None:
    registered_pids = {entry.pid for entry in entries.values()}
    for process in sorted(processes.values(), key=lambda item: item.pid):
        if process.pid in registered_pids:
            continue
        if not _process_matches_claude(process):
            continue
        if not _is_descendant_of_exact(process.pid, "ghostty", processes):
            continue
        issues.append(
            Issue(
                label="claude registry",
                detail=f"live Claude pid {process.pid} has no hook registration; install SessionStart/UserPromptSubmit/SessionEnd hook or provide verified runtime metadata",
            )
        )


def _process_table(issues: list[Issue]) -> dict[int, ProcessInfo]:
    try:
        output = _run(["ps", "-axo", "pid=,ppid=,tty=,comm=,command="])
    except DiscoveryCommandError as exc:
        issues.append(Issue(label="process table", detail=str(exc)))
        return {}
    processes: dict[int, ProcessInfo] = {}
    for line in output.splitlines():
        parts = line.strip().split(None, 4)
        if len(parts) < 4:
            continue
        try:
            data = {
                "pid": int(parts[0]),
                "ppid": int(parts[1]),
                "controlling_tty": None
                if parts[2] in _NO_CONTROLLING_TTY
                else parts[2],
                "comm": parts[3],
                "command": parts[4] if len(parts) == 5 else parts[3],
            }
            process = ProcessInfo.model_validate(data)
        except (ValueError, ValidationError):
            continue
        processes[process.pid] = process
    return processes


def _run(args: Sequence[str], *, env: dict[str, str] | None = None) -> str:
    process_env = os.environ.copy()
    if env is not None:
        process_env.update(env)
    try:
        completed = subprocess.run(
            list(args),
            capture_output=True,
            check=False,
            env=process_env,
            text=True,
            timeout=_RUN_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise DiscoveryCommandError(
            f"{args[0]} timed out after {_RUN_TIMEOUT_SECONDS}s"
        ) from exc
    except OSError as exc:
        raise DiscoveryCommandError(f"{args[0]} failed: {exc.strerror or exc}") from exc
    if completed.returncode != 0:
        stderr = completed.stderr.strip().splitlines()
        reason = stderr[0] if stderr else f"exit {completed.returncode}"
        raise DiscoveryCommandError(
            f"{args[0]} exited {completed.returncode}: {reason[:200]}"
        )
    return completed.stdout


def _command_tokens(command: str) -> tuple[str, ...]:
    try:
        return tuple(shlex.split(command))
    except ValueError:
        return tuple(command.split())


def _basename(token: str) -> str:
    return Path(token).name.lstrip("-")


def _has_flag(process: ProcessInfo, flag: str) -> bool:
    return flag in _command_tokens(process.command)[1:]


def _is_interactive_omp(process: ProcessInfo) -> bool:
    # An interactive OMP root always owns a controlling terminal. Detached OMP
    # processes (worker daemons, background `omp update`) have none and are
    # helpers, not sessions; terminal-backed roots still go through fd/registry
    # verification and fail closed there.
    if process.controlling_tty is None or not _name_matches(process, "omp"):
        return False
    tokens = _command_tokens(process.command)
    return not any(token.startswith("__omp_worker") for token in tokens[1:])


def _process_matches_claude(process: ProcessInfo) -> bool:
    if _name_matches(process, "claude"):
        return True
    tokens = _command_tokens(process.command)
    return any(_basename(token) == "claude" for token in tokens[:3])


def _name_matches(
    process: ProcessInfo, executable_name: Literal["omp", "claude", "ghostty"]
) -> bool:
    if process.comm == executable_name:
        return True
    tokens = _command_tokens(process.command)
    return bool(tokens and _basename(tokens[0]) == executable_name)


def _is_descendant_of_exact(
    pid: int, ancestor_name: Literal["ghostty"], processes: dict[int, ProcessInfo]
) -> bool:
    seen: set[int] = set()
    current = processes.get(pid)
    while current is not None and current.pid not in seen:
        seen.add(current.pid)
        if _name_matches(current, ancestor_name):
            return True
        if current.ppid <= 1:
            return False
        current = processes.get(current.ppid)
    return False


def _find_claude_ancestor(
    processes: dict[int, ProcessInfo], start_pid: int
) -> ProcessInfo | None:
    seen: set[int] = set()
    current = processes.get(start_pid)
    while current is not None and current.pid not in seen:
        seen.add(current.pid)
        if _process_matches_claude(current):
            return current
        current = processes.get(current.ppid)
    return None


def _lsof_paths(
    pid: int, descriptor: str, issues: list[Issue], label: str
) -> tuple[str, ...]:
    try:
        output = _run(["lsof", "-a", "-p", str(pid), "-d", descriptor, "-Fn"])
    except DiscoveryCommandError as exc:
        issues.append(Issue(label=label, detail=f"pid {pid}: {exc}"))
        return ()
    return tuple(line[1:] for line in output.splitlines() if line.startswith("n"))


def _text_executable_for_basename(
    pid: int, executable_name: Literal["omp", "claude"], issues: list[Issue]
) -> Path | None:
    for name in _lsof_paths(pid, "txt", issues, f"{executable_name} executable"):
        path = Path(name)
        if path.name == executable_name:
            return path
    return None


# OMP rewrites a terminal registry record only when its content changes, so a
# transcript resumed on a reused tty keeps a record older than the new process.
# These two checks bind such a record to the live pid without guessing.


def _launched_as_omp_resume(
    process: ProcessInfo, runtime_config: OmpRuntimeConfig, session_file: Path
) -> bool:
    # ps joins argv with single spaces and drops quoting, so accept only the exact
    # restore launch shape: one omp argv0 token followed by precisely the resume
    # arguments for this transcript. Extra flags or messages fail closed, and so
    # does any token with whitespace or control characters, whose argv boundary
    # ps cannot show.
    tokens = process.command.split(" ")
    expected = _omp_resume_args(runtime_config, session_file)
    return (
        all(_is_plain_argv_token(token) for token in (*tokens, *expected))
        and Path(tokens[0]).name == "omp"
        and tuple(tokens[1:]) == expected
    )


def _is_plain_argv_token(token: str) -> bool:
    return (
        bool(token)
        and token.isprintable()
        and not any(character.isspace() for character in token)
    )


def _holds_omp_session_lease(pid: int, session_file: Path, issues: list[Issue]) -> bool:
    # OMP holds `.<transcript>.owner.lock` open while it owns the transcript. It
    # claims the lease lazily on its first session write, so an idle resumed
    # session may not hold it yet; holding it is sufficient, not required.
    lease = session_file.parent.resolve() / f".{session_file.name}.owner.lock"
    try:
        output = _run(["lsof", "-p", str(pid), "-Fn"])
    except DiscoveryCommandError as exc:
        issues.append(Issue(label="omp lease", detail=f"pid {pid}: {exc}"))
        return False
    return f"n{lease}" in output.splitlines()


def _tty_for_pid(pid: int, issues: list[Issue]) -> str | None:
    try:
        output = _run(["lsof", "-a", "-p", str(pid), "-d", "0,1,2", "-Fn"])
    except DiscoveryCommandError as exc:
        issues.append(Issue(label="tty", detail=f"pid {pid}: {exc}"))
        return None
    current_fd = ""
    fallback: str | None = None
    for line in output.splitlines():
        if line.startswith("f"):
            current_fd = line[1:]
            continue
        if not line.startswith("n/dev/ttys"):
            continue
        tty = Path(line[1:]).name
        if current_fd == "0":
            return tty
        if fallback is None:
            fallback = tty
    return fallback


def _process_started(pid: int, issues: list[Issue]) -> str | None:
    try:
        output = _run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            env={"LC_ALL": "C", "TZ": "UTC"},
        )
    except DiscoveryCommandError as exc:
        issues.append(Issue(label="process start", detail=f"pid {pid}: {exc}"))
        return None
    started = output.strip()
    return started or None


def _started_timestamp(process_started: str) -> float | None:
    try:
        return (
            datetime.strptime(process_started, "%a %b %d %H:%M:%S %Y")
            .replace(tzinfo=UTC)
            .timestamp()
        )
    except ValueError:
        return None


def _registry_record_matches_start(
    record_path: Path, process_started: str, issues: list[Issue]
) -> bool:
    start_timestamp = _started_timestamp(process_started)
    if start_timestamp is None:
        issues.append(
            Issue(
                label="process start",
                detail=f"could not parse process start time: {process_started}",
            )
        )
        return False
    try:
        record_mtime = record_path.stat().st_mtime
    except OSError as exc:
        issues.append(
            Issue(
                label="omp registry",
                detail=f"could not stat OMP registry record {record_path}: {exc}",
            )
        )
        return False
    return record_mtime >= start_timestamp


def _read_registry_lines(path: Path, issues: list[Issue]) -> tuple[str, ...] | None:
    try:
        return tuple(
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    except OSError as exc:
        issues.append(
            Issue(
                label="omp registry",
                detail=f"could not read OMP registry record {path}: {exc}",
            )
        )
        return None


def _read_omp_header(session_file: Path, issues: list[Issue]) -> OmpHeader | None:
    title = ""
    session_line: OmpSessionLine | None = None
    try:
        with session_file.open("r", encoding="utf-8") as handle:
            for _ in range(2):
                line = handle.readline()
                if not line:
                    break
                parsed = _parse_omp_header_line(line)
                if isinstance(parsed, OmpTitleLine):
                    title = parsed.title.strip()
                elif isinstance(parsed, OmpSessionLine) and session_line is None:
                    session_line = parsed
    except OSError as exc:
        issues.append(
            Issue(
                label="omp header",
                detail=f"could not read first two JSONL header lines from {session_file}: {exc}",
            )
        )
        return None

    if session_line is None:
        issues.append(
            Issue(
                label="omp header",
                detail=f"no session header found in first two JSONL lines of {session_file}",
            )
        )
        return None
    return OmpHeader(
        session_id=session_line.id, cwd=session_line.cwd, label_title=title
    )


def _parse_omp_header_line(line: str) -> OmpTitleLine | OmpSessionLine | None:
    try:
        return OmpTitleLine.model_validate_json(line)
    except ValidationError:
        pass
    try:
        return OmpSessionLine.model_validate_json(line)
    except ValidationError:
        return None


def _omp_runtime_config(
    process: ProcessInfo, issues: list[Issue]
) -> OmpRuntimeConfig | None:
    try:
        environment = _allowlisted_process_environment(process.pid)
    except DiscoveryCommandError as exc:
        issues.append(
            Issue(
                label="omp environment",
                detail=f"pid {process.pid}: could not inspect allowlisted OMP environment; {exc}",
            )
        )
        return None
    profile = _option_value(process, "--profile") or environment.get("OMP_PROFILE")
    raw_session_dir = _option_value(process, "--session-dir") or environment.get(
        "PI_CODING_AGENT_DIR"
    )
    if raw_session_dir is not None and not _is_default_omp_storage(raw_session_dir):
        issues.append(
            Issue(
                label="omp registry",
                detail=f"pid {process.pid} uses custom OMP session storage; registry layout is not verified, so this session was not captured",
            )
        )
        return None
    try:
        return OmpRuntimeConfig(profile=profile)
    except ValidationError as exc:
        issues.append(
            Issue(
                label="omp environment",
                detail=f"pid {process.pid}: invalid OMP runtime config: {exc}",
            )
        )
        return None


def _allowlisted_process_environment(pid: int) -> dict[str, str]:
    output = _run(["ps", "eww", "-p", str(pid), "-o", "command="])
    result: dict[str, str] = {}
    for token in output.split():
        for name in ("OMP_PROFILE", "PI_CODING_AGENT_DIR"):
            prefix = name + "="
            if token.startswith(prefix):
                value = token[len(prefix) :]
                if value:
                    result[name] = value
    return result


def _is_default_omp_storage(raw_path: str) -> bool:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        return False
    try:
        return path.resolve() == _DEFAULT_OMP_REGISTRY.parent.resolve()
    except OSError:
        return path == _DEFAULT_OMP_REGISTRY.parent


def _omp_registry_dir() -> Path:
    return _DEFAULT_OMP_REGISTRY


def _omp_resume_args(config: OmpRuntimeConfig, session_file: Path) -> tuple[str, ...]:
    args: list[str] = []
    if config.profile is not None:
        args.extend(("--profile", config.profile))
    args.extend(("--resume", str(session_file)))
    return tuple(args)


def _option_value(process: ProcessInfo, option: str) -> str | None:
    tokens = _command_tokens(process.command)
    for index, token in enumerate(tokens[1:], start=1):
        if token == option and index + 1 < len(tokens):
            value = tokens[index + 1]
            return value if value else None
        prefix = option + "="
        if token.startswith(prefix):
            value = token[len(prefix) :]
            return value if value else None
    return None


def _preferred_executable(
    executable_name: Literal["omp", "claude"], actual_executable: Path
) -> Path:
    found = shutil.which(executable_name)
    if found is None:
        return actual_executable
    candidate = Path(found).expanduser()
    try:
        if candidate.resolve() == actual_executable.resolve():
            return candidate
    except OSError:
        return actual_executable
    return actual_executable


def _claude_executable(process: ProcessInfo, issues: list[Issue]) -> Path | None:
    tokens = _command_tokens(process.command)
    for token in tokens[:3]:
        if _basename(token) != "claude":
            continue
        path = Path(token).expanduser()
        if path.is_absolute():
            return path
        found = shutil.which(token)
        if found is not None:
            return Path(found)
    actual = _text_executable_for_basename(process.pid, "claude", issues)
    if actual is not None:
        return _preferred_executable("claude", actual)
    found = shutil.which("claude")
    return Path(found) if found is not None else None


def _label(cwd: Path, session_id: str, title: str) -> str:
    if title:
        return title
    return f"{cwd.name} {session_id[:8]}"


def _state_dir() -> Path:
    configured = os.environ.get(_STATE_ENV)
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_absolute() else path.resolve()
    return Path.home() / ".local" / "state" / "ollemolle"


@contextmanager
def _locked_claude_registry() -> Generator[Path]:
    root = _state_dir()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    lock_path = root / _CLAUDE_LOCK_NAME
    with lock_path.open("a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield root / _CLAUDE_REGISTRY_NAME
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _read_claude_registry_or_raise(path: Path) -> ClaudeRegistry:
    try:
        payload = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ClaudeRegistry()
    try:
        return ClaudeRegistry.model_validate_json(payload)
    except ValidationError as exc:
        raise ValueError(f"Claude registry is invalid: {path}: {exc}") from exc
    except ValueError as exc:
        raise ValueError(f"Claude registry is invalid: {path}: {exc}") from exc


def _read_claude_registry_for_discovery(
    path: Path, issues: list[Issue]
) -> ClaudeRegistry:
    try:
        return _read_claude_registry_or_raise(path)
    except OSError as exc:
        issues.append(
            Issue(
                label="claude registry",
                detail=f"could not read Claude registry {path}: {exc}",
            )
        )
    except ValueError as exc:
        issues.append(Issue(label="claude registry", detail=str(exc)))
    return ClaudeRegistry()


def _write_claude_registry(path: Path, registry: ClaudeRegistry) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    payload = registry.model_dump_json(indent=2)
    with NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        handle.write(payload)
        handle.write("\n")
        temporary_path = Path(handle.name)
    try:
        temporary_path.chmod(0o600)
        os.replace(temporary_path, path)
        path.chmod(0o600)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def _claude_transcript_is_current(
    path: Path, session_id: str, issues: list[Issue]
) -> bool:
    if not path.is_file():
        issues.append(
            Issue(
                label="claude transcript",
                detail=f"registered Claude transcript does not exist yet: {path}",
            )
        )
        return False
    if path.stem != session_id:
        issues.append(
            Issue(
                label="claude transcript",
                detail=f"registered Claude transcript filename does not match session id {session_id}: {path}",
            )
        )
        return False
    try:
        with path.open("r", encoding="utf-8") as handle:
            first_line = handle.readline()
    except OSError as exc:
        issues.append(
            Issue(
                label="claude transcript",
                detail=f"could not read Claude transcript header {path}: {exc}",
            )
        )
        return False
    if not first_line:
        issues.append(
            Issue(
                label="claude transcript",
                detail=f"registered Claude transcript is empty: {path}",
            )
        )
        return False
    try:
        header = ClaudeTranscriptHeader.model_validate_json(first_line)
    except ValidationError as exc:
        issues.append(
            Issue(
                label="claude transcript",
                detail=f"Claude transcript first line is not a supported JSON object in {path}: {exc}",
            )
        )
        return False
    embedded_id = header.embedded_session_id()
    if embedded_id is not None and embedded_id != session_id:
        issues.append(
            Issue(
                label="claude transcript",
                detail=f"Claude transcript header id {embedded_id} does not match {session_id}",
            )
        )
        return False
    return True


def _registry_key(pid: int, process_started: str) -> str:
    return f"{pid}:{process_started}"


def _joined_issues(issues: Sequence[Issue]) -> str:
    return "; ".join(f"{issue.label}: {issue.detail}" for issue in issues)
