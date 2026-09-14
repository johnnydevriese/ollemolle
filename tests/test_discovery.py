from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ollemolle import discovery
from ollemolle.models import Issue


class OmpDiscoveryTests(unittest.TestCase):
    def test_reused_tty_registry_record_predating_process_is_rejected(self) -> None:
        start = datetime.now(UTC).replace(microsecond=0)
        process_started = start.strftime("%a %b %d %H:%M:%S %Y")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            cwd = root / "repo"
            cwd.mkdir()
            session_file = root / "session.jsonl"
            session_file.write_text(
                '{"type":"title","v":1,"title":""}\n'
                + f'{{"type":"session","version":3,"id":"session-1","cwd":"{cwd}"}}\n',
                encoding="utf-8",
            )
            record = root / "ttys123"
            record.write_text(f"{cwd}\n{session_file}\n", encoding="utf-8")
            os.utime(record, (start.timestamp() - 1, start.timestamp() - 1))
            issues: list[Issue] = []

            session = discovery._session_from_omp_record(
                record_path=record,
                process=discovery.ProcessInfo(
                    pid=123, ppid=100, comm="omp", command="omp"
                ),
                process_started=process_started,
                tty="ttys123",
                executable=Path("/opt/homebrew/bin/omp"),
                runtime_config=discovery.OmpRuntimeConfig(),
                issues=issues,
            )

        self.assertIsNone(session)
        self.assertTrue(
            any("predates live pid" in issue.detail for issue in issues), issues
        )

    def test_no_session_omp_reports_unresumable_without_reading_stale_registry(
        self,
    ) -> None:
        processes = {
            100: discovery.ProcessInfo(
                pid=100,
                ppid=1,
                comm="ghostty",
                command="/Applications/Ghostty.app/Contents/MacOS/ghostty",
            ),
            123: discovery.ProcessInfo(
                pid=123, ppid=100, comm="omp", command="omp --no-session"
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
