from __future__ import annotations

import argparse
import os
import shlex
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from pydantic import ValidationError

from . import discovery
from .ghostty import (
    LaunchRequest,
    ensure_ghostty_available,
    launch_requests,
    open_windows,
)
from .models import Discovery, Issue, Session, Snapshot
from .storage import (
    OllemolleError,
    RestoreLock,
    read_snapshot,
    snapshot_path,
    write_snapshot,
)


class DiscoveryBlockedError(OllemolleError):
    """Raised when discovery cannot produce an unambiguous full snapshot."""


class RestorePreflightError(OllemolleError):
    """Raised when restore cannot safely open every saved session."""


def _issue_lines(issues: Sequence[Issue]) -> tuple[str, ...]:
    return tuple(f"{issue.label}: {issue.detail}" for issue in issues)


def _print_lines(lines: Sequence[str], stream: TextIO = sys.stdout) -> None:
    for line in lines:
        print(line, file=stream)


def _snapshot_label(name: str | None) -> Path:
    return snapshot_path(name)


def save_snapshot(name: str | None) -> Path:
    result = discovery.discover_sessions()
    if result.issues:
        destination = _snapshot_label(name)
        details = "\n".join(_issue_lines(result.issues))
        raise DiscoveryBlockedError(
            f"discovery reported {len(result.issues)} issue(s); not overwriting {destination}\n{details}"
        )
    if not result.sessions:
        raise DiscoveryBlockedError(
            f"discovery found zero sessions; not overwriting {_snapshot_label(name)}"
        )
    snapshot = Snapshot(
        saved_at=datetime.now(UTC),
        sessions=result.sessions,
    )
    return write_snapshot(snapshot, name)


def _session_identity(session: Session) -> str:
    return f"{session.tool}:{session.session_id}"


def _same_live_session(left: Session, right: Session) -> bool:
    if left.tool != right.tool:
        return False
    return (
        left.session_id == right.session_id or left.session_file == right.session_file
    )


def _session_preflight_errors(session: Session) -> tuple[str, ...]:
    errors: list[str] = []
    identity = _session_identity(session)
    if not session.cwd.is_dir():
        errors.append(f"{identity}: cwd is not a directory: {session.cwd}")
    if not session.session_file.is_file():
        errors.append(f"{identity}: session file is not a file: {session.session_file}")
    if not session.executable.is_file():
        errors.append(f"{identity}: executable is not a file: {session.executable}")
    elif not os.access(session.executable, os.X_OK):
        errors.append(f"{identity}: executable is not runnable: {session.executable}")
    return tuple(errors)


def _duplicate_snapshot_errors(sessions: Sequence[Session]) -> tuple[str, ...]:
    seen: dict[str, Session] = {}
    errors: list[str] = []
    for session in sessions:
        identity = _session_identity(session)
        previous = seen.get(identity)
        if previous is not None:
            errors.append(
                f"{identity}: duplicate saved session entries: {previous.cwd} and {session.cwd}"
            )
        else:
            seen[identity] = session
    return tuple(errors)


def _active_conflict_errors(
    saved_sessions: Sequence[Session], active: Discovery
) -> tuple[str, ...]:
    errors: list[str] = []
    for active_session in active.sessions:
        for saved_session in saved_sessions:
            if _same_live_session(active_session, saved_session):
                errors.append(
                    f"{_session_identity(saved_session)}: already active on tty {active_session.tty}; not opening a duplicate"
                )
    return tuple(errors)


def _restorable_session(session: Session) -> Session:
    executable = discovery.restore_executable(session.executable)
    if executable == session.executable:
        return session
    return session.model_copy(update={"executable": executable})


def preflight_restore(snapshot: Snapshot) -> tuple[LaunchRequest, ...]:
    errors: list[str] = []
    if not snapshot.sessions:
        errors.append("snapshot has no sessions")
    try:
        ensure_ghostty_available()
    except OllemolleError as exc:
        errors.append(str(exc))
    errors.extend(_duplicate_snapshot_errors(snapshot.sessions))
    requests: list[LaunchRequest] = []
    for saved_session in snapshot.sessions:
        session = _restorable_session(saved_session)
        errors.extend(_session_preflight_errors(session))
        try:
            requests.extend(launch_requests((session,)))
        except OllemolleError as exc:
            errors.append(str(exc))
    active = discovery.discover_sessions()
    if active.issues:
        errors.append(
            "active-session discovery reported issue(s); not opening restore tabs until conflicts can be determined"
        )
        errors.extend(_issue_lines(active.issues))
    errors.extend(_active_conflict_errors(snapshot.sessions, active))
    if errors:
        raise RestorePreflightError("restore preflight failed:\n" + "\n".join(errors))
    return tuple(requests)


def _format_session(request: LaunchRequest, index: int) -> tuple[str, ...]:
    session = request.session
    command = shlex.join(request.argv)
    return (
        f"{index}. {session.tool} {session.session_id}",
        f"   repo: {session.cwd}",
        f"   label: {session.label}",
        f"   session_file: {session.session_file}",
        f"   command: {command}",
    )


def describe_requests(requests: Sequence[LaunchRequest]) -> tuple[str, ...]:
    lines: list[str] = []
    for index, request in enumerate(requests, start=1):
        lines.extend(_format_session(request, index))
    return tuple(lines)


def command_save(name: str | None) -> int:
    path = save_snapshot(name)
    snapshot = read_snapshot(name)
    print(f"saved {len(snapshot.sessions)} session(s) to {path}")
    return 0


def command_list(name: str | None) -> int:
    snapshot = read_snapshot(name)
    requests = launch_requests(snapshot.sessions)
    print(f"snapshot: {snapshot_path(name)}")
    print(f"saved_at: {snapshot.saved_at.isoformat()}")
    print(f"sessions: {len(snapshot.sessions)}")
    _print_lines(describe_requests(requests))
    return 0


def command_restore(name: str | None, *, dry_run: bool) -> int:
    snapshot = read_snapshot(name)
    if dry_run:
        requests = preflight_restore(snapshot)
        print(
            f"dry run: would open {len(requests)} Ghostty tab(s) in one window from {snapshot_path(name)}"
        )
        _print_lines(describe_requests(requests))
        return 0
    with RestoreLock():
        requests = preflight_restore(snapshot)
        result = open_windows(requests)
    print(
        f"opened {len(result.tab_ids)} Ghostty tab(s) in window {result.window_id} from {snapshot_path(name)}"
    )
    for index, tab_id in enumerate(result.tab_ids, start=1):
        print(f"{index}. tab: {tab_id}")
    return 0


def command_register_claude() -> int:
    payload = sys.stdin.read()
    try:
        discovery.register_claude(payload)
    except ValueError as exc:
        raise OllemolleError(f"register-claude rejected payload: {exc}") from exc
    except OSError as exc:
        raise OllemolleError(
            f"register-claude could not update registry: {exc}"
        ) from exc
    except RuntimeError as exc:
        raise OllemolleError(
            f"register-claude could not identify the live Claude session: {exc}"
        ) from exc
    return 0


def build_parser(program: str = "ollemolle") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=program)
    subparsers = parser.add_subparsers(dest="command", required=True)

    save_parser = subparsers.add_parser(
        "save", help="save all discovered OMP/Claude sessions"
    )
    save_parser.add_argument("--name", help="safe snapshot name; default: latest")

    restore_parser = subparsers.add_parser(
        "restore", help="open saved sessions as tabs in one new Ghostty window"
    )
    restore_parser.add_argument("--name", help="safe snapshot name; default: latest")
    restore_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="preflight and print commands without opening restore tabs",
    )

    list_parser = subparsers.add_parser(
        "list", help="show saved sessions without opening restore tabs"
    )
    list_parser.add_argument("--name", help="safe snapshot name; default: latest")

    subparsers.add_parser(
        "register-claude",
        help="register the current Claude Code session from hook stdin",
    )
    return parser


def run_ollemolle(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "save":
        return command_save(args.name)
    if args.command == "restore":
        return command_restore(args.name, dry_run=args.dry_run)
    if args.command == "list":
        return command_list(args.name)
    if args.command == "register-claude":
        return command_register_claude()
    parser.error(f"unknown command: {args.command}")
    return 2


def _entrypoint(argv: Sequence[str] | None = None) -> int:
    try:
        return run_ollemolle(argv)
    except OllemolleError as exc:
        print(f"ollemolle: {exc}", file=sys.stderr)
        return 1
    except ValidationError as exc:
        print(f"ollemolle: invalid session data: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("ollemolle: interrupted", file=sys.stderr)
        return 130


def main() -> None:
    raise SystemExit(_entrypoint())


if __name__ == "__main__":
    main()
