from __future__ import annotations

import json
import os
import shlex
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .models import Session
from .storage import OllemolleError

GHOSTTY_APP = Path("/Applications/Ghostty.app")
_OSASCRIPT = Path("/usr/bin/osascript")
_ZSH = Path("/bin/zsh")
_JXA_TIMEOUT_SECONDS = 45


class GhosttyUnavailableError(OllemolleError):
    """Raised when Ghostty or osascript is unavailable."""


class GhosttyLaunchError(OllemolleError):
    """Raised when Ghostty refuses to create one or more restore tabs."""


class ResumeCommandError(OllemolleError):
    """Raised when a saved session cannot be converted to a safe resume command."""


@dataclass(frozen=True)
class LaunchRequest:
    session: Session
    argv: tuple[str, ...]
    ghostty_command: str


@dataclass(frozen=True)
class LaunchResult:
    window_id: str | None
    tab_ids: tuple[str, ...]


class _GhosttyLaunchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    window_id: str = Field(alias="windowId")
    tab_ids: tuple[str, ...] = Field(alias="tabIds")


def ensure_ghostty_available() -> None:
    if not GHOSTTY_APP.is_dir():
        raise GhosttyUnavailableError(f"Ghostty app not found at {GHOSTTY_APP}")
    if not _OSASCRIPT.is_file() or not os.access(_OSASCRIPT, os.X_OK):
        raise GhosttyUnavailableError(f"osascript is not executable at {_OSASCRIPT}")
    if not _ZSH.is_file() or not os.access(_ZSH, os.X_OK):
        raise GhosttyUnavailableError(f"zsh is not executable at {_ZSH}")


def _validate_omp_resume_args(session: Session, args: tuple[str, ...]) -> None:
    resume_target: str | None = None
    index = 0
    while index < len(args):
        token = args[index]
        if token in {"--profile", "--session-dir", "--resume"}:
            if index + 1 >= len(args) or not args[index + 1]:
                raise ResumeCommandError(
                    f"unsafe OMP resume arguments for {session.session_id}; {token} requires a value"
                )
            value = args[index + 1]
            if token == "--resume":
                resume_target = value
            index += 2
            continue
        for option in ("--profile=", "--session-dir=", "--resume="):
            if token.startswith(option) and token != option:
                if option == "--resume=":
                    resume_target = token[len(option) :]
                index += 1
                break
        else:
            raise ResumeCommandError(
                f"unsafe OMP resume argument for {session.session_id}: {token}"
            )
    expected = str(session.session_file)
    if resume_target != expected:
        raise ResumeCommandError(
            f"unsafe OMP resume target for {session.session_id}; expected {expected}"
        )


def _validate_claude_resume_args(session: Session, args: tuple[str, ...]) -> None:
    if len(args) != 2 or args[0] != "--resume" or args[1] != session.session_id:
        raise ResumeCommandError(
            f"unsafe Claude resume arguments for {session.session_id}; only '--resume {session.session_id}' is restored"
        )


def safe_resume_args(session: Session) -> tuple[str, ...]:
    args = tuple(session.resume_args)
    if not args:
        if session.tool == "omp":
            args = ("--resume", str(session.session_file))
        elif session.tool == "claude":
            args = ("--resume", session.session_id)
        else:
            raise ResumeCommandError(
                f"unsupported tool for {session.label}: {session.tool}"
            )
    if session.tool == "omp":
        _validate_omp_resume_args(session, args)
    elif session.tool == "claude":
        _validate_claude_resume_args(session, args)
    else:
        raise ResumeCommandError(
            f"unsupported tool for {session.label}: {session.tool}"
        )
    return args


def resume_argv(session: Session) -> tuple[str, ...]:
    return (str(session.executable), *safe_resume_args(session))


def ghostty_command(cwd: Path, argv: Sequence[str]) -> str:
    restore_line = f"cd {shlex.quote(str(cwd))} && exec {shlex.join(argv)}"
    return shlex.join((str(_ZSH), "-lic", restore_line))


def launch_request(session: Session) -> LaunchRequest:
    argv = resume_argv(session)
    return LaunchRequest(
        session=session,
        argv=argv,
        ghostty_command=ghostty_command(session.cwd, argv),
    )


def launch_requests(sessions: Sequence[Session]) -> tuple[LaunchRequest, ...]:
    return tuple(launch_request(session) for session in sessions)


def open_windows(requests: Sequence[LaunchRequest]) -> LaunchResult:
    ensure_ghostty_available()
    if not requests:
        return LaunchResult(window_id=None, tab_ids=())
    payload = {
        "tabs": [
            {
                "cwd": str(request.session.cwd),
                "command": request.ghostty_command,
            }
            for request in requests
        ]
    }
    script = """
function surfaceConfiguration(item) {
  return {
    initialWorkingDirectory: item.cwd,
    command: item.command,
    waitAfterCommand: true
  };
}

function run(argv) {
  const config = JSON.parse(argv[0]);
  const app = Application('Ghostty');
  const window = app.newWindow({withConfiguration: surfaceConfiguration(config.tabs[0])});
  const tabIds = [String(window.selectedTab().id())];
  for (const item of config.tabs.slice(1)) {
    const tab = app.newTab({in: window, withConfiguration: surfaceConfiguration(item)});
    tabIds.push(String(tab.id()));
  }
  return JSON.stringify({windowId: String(window.id()), tabIds});
}
""".strip()
    timeout = min(180, _JXA_TIMEOUT_SECONDS + (5 * len(requests)))
    try:
        completed = subprocess.run(
            [str(_OSASCRIPT), "-l", "JavaScript", "-e", script, json.dumps(payload)],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise GhosttyLaunchError(
            "Ghostty did not create restore tabs before the launcher timeout"
        ) from exc
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        detail = f": {stderr}" if stderr else ""
        raise GhosttyLaunchError(f"Ghostty tab creation failed{detail}")
    try:
        result = _GhosttyLaunchResponse.model_validate_json(completed.stdout)
    except ValidationError as exc:
        raise GhosttyLaunchError(
            f"Ghostty launcher returned malformed JSON output: {exc}"
        ) from exc
    if len(result.tab_ids) != len(requests):
        raise GhosttyLaunchError(
            f"Ghostty created {len(result.tab_ids)} tab(s), expected {len(requests)}"
        )
    return LaunchResult(window_id=result.window_id, tab_ids=result.tab_ids)
