from __future__ import annotations

import unittest
from datetime import UTC, datetime
from pathlib import Path

from ollemolle.models import Session, Snapshot


class SnapshotModelTests(unittest.TestCase):
    def test_snapshot_json_round_trips_path_fields(self) -> None:
        session = Session(
            tool="omp",
            cwd=Path("/tmp"),
            session_id="01a08d75-85e3-701e-b313-0e97c211d492",
            session_file=Path("/tmp/session.jsonl"),
            executable=Path("/bin/echo"),
            label="dd-ingest-api 01a08d75",
            tty="ttys000",
            pid=17095,
            process_started="Thu Sep 10 16:34:43 2026",
            resume_args=("--resume", "/tmp/session.jsonl"),
        )
        snapshot = Snapshot(saved_at=datetime.now(UTC), sessions=(session,))

        restored = Snapshot.model_validate_json(snapshot.model_dump_json())

        self.assertEqual(restored, snapshot)
        self.assertIsInstance(restored.sessions[0].cwd, Path)
        self.assertEqual(restored.sessions[0].session_file, Path("/tmp/session.jsonl"))
        self.assertEqual(restored.sessions[0].executable, Path("/bin/echo"))


if __name__ == "__main__":
    unittest.main()
