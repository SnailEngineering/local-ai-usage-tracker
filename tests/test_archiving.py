from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from aiusage.sources import claude_code_local, codex_local


class ArchiveTests(unittest.TestCase):
    def test_same_size_rewrite_is_archived_again(self) -> None:
        for source in (claude_code_local, codex_local):
            with self.subTest(source=source.SOURCE), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source_dir = root / "source"
                archive_dir = root / "archive"
                path = source_dir / "session.jsonl"
                path.parent.mkdir(parents=True)
                path.write_text('{"value":1}\n')

                self.assertEqual(source.archive(source_dir, archive_dir)[0], 1)
                original_mtime = path.stat().st_mtime_ns
                path.write_text('{"value":2}\n')
                os.utime(path, ns=(original_mtime, original_mtime + 1_000_000))

                self.assertEqual(source.archive(source_dir, archive_dir)[0], 1)
                self.assertEqual((archive_dir / "session.jsonl").read_text(), '{"value":2}\n')


if __name__ == "__main__":
    unittest.main()
