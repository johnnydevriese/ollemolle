from __future__ import annotations

import os
import unittest
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ollemolle import discovery, main
from ollemolle.models import Issue, Session
from ollemolle.storage import read_snapshot

_PROCESS_STARTED = "Thu Oct  1 08:00:00 2026"
_OMP_SESSION_ID = "01a0ee4c-92e4-718b-b055-1678bb49b697"
_PROCESS_TABLE = f"""\
  100     1 ??       ghostty  /Applications/Ghostty.app/Contents/MacOS/ghostty
  110   100 ttys001  zsh      -zsh
  123   110 ttys001  omp      omp --resume {_OMP_SESSION_ID}
  124   123 ?        omp      /opt/homebrew/Cellar/omp/18.4.9/bin/omp __omp_worker_daemon_broker
  125   124 ??       omp      /opt/homebrew/Cellar/omp/18.4.9/bin/omp __omp_worker_lsp_mux
  130   123 ?        omp      omp update
  140   100 ttys002  zsh      -zsh
"""


class _FakeSystem:
    """Answers the bounded ps/lsof calls discovery makes for _PROCESS_TABLE."""

    def __init__(self, omp_executable: Path, root_fds: str) -> None:
        self._omp_executable = omp_executable
        self._root_fds = root_fds
        self.probed_pids: set[int] = set()

    def run(self, args: Sequence[str], *, env: dict[str, str] | None = None) -> str:
        argv = tuple(args)
        if argv == ("ps", "-axo", "pid=,ppid=,tty=,comm=,command="):
            return _PROCESS_TABLE
        pid = int(argv[argv.index("-p") + 1])
        self.probed_pids.add(pid)
        if pid != 123:
            raise AssertionError(f"detached helper pid {pid} was probed: {argv}")
        if argv[:2] == ("ps", "eww"):
            return f"omp --resume {_OMP_SESSION_ID}\n"
        if argv[-1] == "lstart=":
            return f"{_PROCESS_STARTED}\n"
        if argv[0] == "lsof" and "txt" in argv:
            return f"p{pid}\nftxt\nn{self._omp_executable}\n"
        if argv[0] == "lsof" and "0,1,2" in argv:
            return self._root_fds
        raise AssertionError(f"unexpected discovery command: {argv}")


class OmpLiveSaveTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)

    def _run_with_live_tab(self, root_fds: str) -> tuple[Path, _FakeSystem]:
        root = Path(self._directory.name)
        cwd = root / "repo"
        cwd.mkdir()
        session_file = root / "session.jsonl"
        session_file.write_text(
            '{"type":"title","v":1,"title":"repo work"}\n'
            + f'{{"type":"session","version":3,"id":"{_OMP_SESSION_ID}","cwd":"{cwd}"}}\n',
            encoding="utf-8",
        )
        registry = root / "terminal-sessions"
        registry.mkdir()
        record = registry / "ttys001"
        record.write_text(f"{cwd}\n{session_file}\ncwdstat 1 2\n", encoding="utf-8")
        written = datetime(2026, 10, 1, 8, 1, tzinfo=UTC).timestamp()
        os.utime(record, (written, written))
        executable = root / "bin" / "omp"
        executable.parent.mkdir()
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)
        fake = _FakeSystem(executable, root_fds)
        for patcher in (
            patch.dict(os.environ, {"OLLEMOLLE_STATE_DIR": str(root / "state")}),
            patch.object(discovery, "_run", side_effect=fake.run),
            patch.object(discovery, "_omp_registry_dir", return_value=registry),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        return root, fake

    def test_named_save_captures_only_terminal_root_despite_detached_omp_helpers(
        self,
    ) -> None:
        root, fake = self._run_with_live_tab(
            "p123\nf0\nn/dev/ttys001\nf1\nn/dev/ttys001\nf2\nn/dev/ttys001\n"
        )

        path = main.save_snapshot("after-update")
        snapshot = read_snapshot("after-update")

        self.assertEqual(path, root / "state" / "after-update.json")
        self.assertEqual(fake.probed_pids, {123})
        self.assertEqual(len(snapshot.sessions), 1)
        session = snapshot.sessions[0]
        self.assertEqual(
            (session.tool, session.pid, session.tty, session.session_id),
            ("omp", 123, "ttys001", _OMP_SESSION_ID),
        )
        self.assertEqual(session.cwd, root / "repo")
        self.assertEqual(session.label, "repo work")
        self.assertEqual(session.resume_args, ("--resume", str(root / "session.jsonl")))

        with self.assertRaises(main.RestorePreflightError) as raised:
            main.preflight_restore(snapshot)
        message = str(raised.exception)
        self.assertIn(f"omp:{_OMP_SESSION_ID}: already active on tty ttys001", message)
        self.assertNotIn("active-session discovery reported issue", message)

    def test_terminal_root_without_tty_fds_still_blocks_save(self) -> None:
        root, _ = self._run_with_live_tab(
            "p123\nf0\nn/dev/null\nf1\nn/dev/null\nf2\nn/dev/null\n"
        )

        with self.assertRaises(main.DiscoveryBlockedError) as raised:
            main.save_snapshot("after-update")

        self.assertIn(
            "omp tty: pid 123 has no readable /dev/ttys* fd0/fd1/fd2",
            str(raised.exception),
        )
        self.assertFalse((root / "state" / "after-update.json").exists())


class ProcessTableTests(unittest.TestCase):
    def test_controlling_terminal_column_decides_omp_root_candidates(self) -> None:
        # macOS ps prints `?`/`??` for no controlling terminal and may print a
        # non-`ttys*` name such as `stdin` for a real one; only fds name the tty.
        table = (
            "  201     1 ?        omp  omp update\n"
            "  202     1 ??       omp  omp\n"
            "  203     1 stdin    omp  omp\n"
            "  204     1 ttys004  omp  omp --resume abc\n"
            "  205     1 ttys005  omp  omp __omp_worker_text_predict\n"
        )
        issues: list[Issue] = []

        with patch.object(discovery, "_run", return_value=table):
            processes = discovery._process_table(issues)

        self.assertEqual(issues, [])
        self.assertEqual(
            {pid: process.controlling_tty for pid, process in processes.items()},
            {201: None, 202: None, 203: "stdin", 204: "ttys004", 205: "ttys005"},
        )
        self.assertEqual(processes[201].command, "omp update")
        self.assertEqual(
            sorted(
                pid
                for pid, process in processes.items()
                if discovery._is_interactive_omp(process)
            ),
            [203, 204],
        )


class OmpDiscoveryTests(unittest.TestCase):
    def _stale_record_session(
        self,
        *,
        holds_lease: bool = False,
        command: str = "omp",
        session_name: str = "session.jsonl",
    ) -> tuple[Session | None, list[Issue], Path]:
        start = datetime.now(UTC).replace(microsecond=0)
        process_started = start.strftime("%a %b %d %H:%M:%S %Y")
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        cwd = root / "repo"
        cwd.mkdir()
        session_file = root / session_name
        session_file.write_text(
            f'{{"type":"session","version":3,"id":"session-1","cwd":"{cwd}"}}\n',
            encoding="utf-8",
        )
        record = root / "ttys123"
        record.write_text(f"{cwd}\n{session_file}\n", encoding="utf-8")
        os.utime(record, (start.timestamp() - 1, start.timestamp() - 1))
        real_root = root.resolve()
        held_lock = (
            f".{session_name}.owner.lock" if holds_lease else ".other.jsonl.owner.lock"
        )
        open_files = (
            f"p123\nfcwd\nn{real_root / 'repo'}\nf20\nn{real_root / session_name}\n"
            + f"f21\nn{real_root / held_lock}\n"
        )

        def lsof(args: Sequence[str], *, env: dict[str, str] | None = None) -> str:
            self.assertEqual(tuple(args), ("lsof", "-p", "123", "-Fn"))
            return open_files

        issues: list[Issue] = []
        with patch.object(discovery, "_run", side_effect=lsof):
            session = discovery._session_from_omp_record(
                record_path=record,
                process=discovery.ProcessInfo(
                    pid=123,
                    ppid=100,
                    controlling_tty="ttys123",
                    comm="omp",
                    command=command.format(session=session_file),
                ),
                process_started=process_started,
                tty="ttys123",
                executable=Path("/opt/homebrew/bin/omp"),
                runtime_config=discovery.OmpRuntimeConfig(),
                issues=issues,
            )
        return session, issues, session_file

    def _assert_rejected_as_stale(
        self, session: Session | None, issues: list[Issue]
    ) -> None:
        self.assertIsNone(session)
        self.assertTrue(
            any("predates live pid" in issue.detail for issue in issues), issues
        )

    def _assert_bound(
        self, session: Session | None, issues: list[Issue], session_file: Path
    ) -> None:
        self.assertEqual(issues, [])
        assert session is not None, issues
        self.assertEqual((session.session_id, session.pid), ("session-1", 123))
        self.assertEqual(session.resume_args, ("--resume", str(session_file)))

    def test_reused_tty_registry_record_predating_process_is_rejected(self) -> None:
        # Having the transcript open, or another transcript's lease, is not ownership.
        session, issues, _ = self._stale_record_session()

        self._assert_rejected_as_stale(session, issues)

    # OMP skips rewriting an identical breadcrumb, so a transcript resumed on a
    # reused tty keeps the old record mtime.
    def test_unchanged_registry_record_is_accepted_when_pid_owns_its_transcript(
        self,
    ) -> None:
        session, issues, session_file = self._stale_record_session(holds_lease=True)

        self._assert_bound(session, issues, session_file)

    def test_unchanged_registry_record_is_accepted_for_idle_restore_launch(
        self,
    ) -> None:
        # OMP claims the lease only on its first write, so an idle restored root
        # is bound by ollemolle's exact restore argv instead.
        session, issues, session_file = self._stale_record_session(
            command="/opt/homebrew/bin/omp --resume {session}"
        )

        self._assert_bound(session, issues, session_file)

    def test_unchanged_registry_record_rejects_ambiguous_resume_launches(
        self,
    ) -> None:
        cases = {
            "message before resume": ("omp hello --resume {session}", "session.jsonl"),
            "message after resume": ("omp --resume {session} hello", "session.jsonl"),
            "extra option": ("omp --cwd /tmp --resume {session}", "session.jsonl"),
            "unexpected profile": (
                "omp --profile work --resume {session}",
                "session.jsonl",
            ),
            "whitespace path": ("omp --resume {session}", "my session.jsonl"),
        }
        for name, (command, session_name) in cases.items():
            with self.subTest(name):
                session, issues, _ = self._stale_record_session(
                    command=command, session_name=session_name
                )

                self._assert_rejected_as_stale(session, issues)

    def test_no_session_omp_reports_unresumable_without_reading_stale_registry(
        self,
    ) -> None:
        processes = {
            100: discovery.ProcessInfo(
                pid=100,
                ppid=1,
                controlling_tty=None,
                comm="ghostty",
                command="/Applications/Ghostty.app/Contents/MacOS/ghostty",
            ),
            123: discovery.ProcessInfo(
                pid=123,
                ppid=100,
                controlling_tty="ttys123",
                comm="omp",
                command="omp --no-session",
            ),
        }
        issues: list[Issue] = []

        with (
            patch.object(
                discovery,
                "_omp_runtime_config",
                return_value=discovery.OmpRuntimeConfig(),
            ),
            patch.object(
                discovery,
                "_text_executable_for_basename",
                return_value=Path("/opt/homebrew/bin/omp"),
            ),
            patch.object(discovery, "_tty_for_pid", return_value="ttys123"),
            patch.object(
                discovery,
                "_session_from_omp_record",
                side_effect=AssertionError("registry should not be consulted"),
            ),
        ):
            sessions = tuple(discovery._discover_omp(processes, issues))

        self.assertEqual(sessions, ())
        self.assertTrue(
            any(
                issue.label == "omp session" and "--no-session" in issue.detail
                for issue in issues
            ),
            issues,
        )

    def test_custom_omp_storage_is_unresolved_not_guessed(self) -> None:
        process = discovery.ProcessInfo(
            pid=123,
            ppid=100,
            controlling_tty="ttys123",
            comm="omp",
            command="omp --session-dir /tmp/custom-omp-agent",
        )
        issues: list[Issue] = []

        with patch.object(
            discovery, "_allowlisted_process_environment", return_value={}
        ):
            config = discovery._omp_runtime_config(process, issues)

        self.assertIsNone(config)
        self.assertTrue(
            any("custom OMP session storage" in issue.detail for issue in issues),
            issues,
        )

    def test_non_ghostty_claude_registration_does_not_block_save(self) -> None:
        process_started = "Thu Sep 10 17:00:00 2026"
        registration = discovery.ClaudeRegistration(
            cwd=Path("/tmp"),
            session_id="11111111-1111-4111-8111-111111111111",
            session_file=Path("/tmp/11111111-1111-4111-8111-111111111111.jsonl"),
            executable=Path("/usr/local/bin/claude"),
            label="tmp 11111111",
            tty="ttys200",
            pid=200,
            process_started=process_started,
            resume_args=("--resume", "11111111-1111-4111-8111-111111111111"),
        )
        processes = {
            200: discovery.ProcessInfo(
                pid=200,
                ppid=1,
                controlling_tty="ttys200",
                comm="claude",
                command="/usr/local/bin/claude",
            )
        }
        issues: list[Issue] = []

        with patch.object(
            discovery,
            "_process_started",
            side_effect=AssertionError(
                "non-Ghostty registrations are ignored before start checks"
            ),
        ):
            sessions = tuple(
                discovery._discover_registered_claude(
                    processes,
                    {discovery._registry_key(200, process_started): registration},
                    issues,
                )
            )

        self.assertEqual(sessions, ())
        self.assertEqual(issues, [])


if __name__ == "__main__":
    unittest.main()
