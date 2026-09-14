from __future__ import annotations

import json
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from ollemolle.ghostty import launch_requests, open_windows
from ollemolle.models import Session


def _session(session_id: str) -> Session:
    return Session(
        tool="claude",
        cwd=Path("/tmp"),
        session_id=session_id,
        session_file=Path(f"/tmp/{session_id}.jsonl"),
        executable=Path("/bin/echo"),
        label=session_id,
        tty="ttys000",
        pid=1,
        process_started="Thu Sep 10 16:34:43 2026",
        resume_args=("--resume", session_id),
    )


class GhosttyLaunchTests(unittest.TestCase):
    def test_restore_opens_one_window_with_a_tab_per_session(self) -> None:
        requests = launch_requests((_session("first"), _session("second")))
        completed = CompletedProcess(
            args=[],
            returncode=0,
            stdout='{"windowId":"window-1","tabIds":["tab-1","tab-2"]}',
            stderr="",
        )

        with (
            patch("ollemolle.ghostty.ensure_ghostty_available"),
            patch("ollemolle.ghostty.subprocess.run", return_value=completed) as run,
        ):
            result = open_windows(requests)

        self.assertEqual(result.window_id, "window-1")
        self.assertEqual(result.tab_ids, ("tab-1", "tab-2"))
        command = run.call_args.args[0]
        payload = json.loads(command[5])
        self.assertEqual(
            payload["tabs"],
            [
                {"cwd": "/tmp", "command": requests[0].ghostty_command},
                {"cwd": "/tmp", "command": requests[1].ghostty_command},
            ],
        )

    def test_empty_restore_does_not_open_a_window(self) -> None:
        with (
            patch("ollemolle.ghostty.ensure_ghostty_available"),
            patch("ollemolle.ghostty.subprocess.run") as run,
        ):
            result = open_windows(())

        self.assertIsNone(result.window_id)
        self.assertEqual(result.tab_ids, ())
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
