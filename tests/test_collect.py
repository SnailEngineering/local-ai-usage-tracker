from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import collect


class PathResolutionTests(unittest.TestCase):
    def test_relative_paths_resolve_against_the_repo_not_the_cwd(self) -> None:
        """`.env` lives in the repository and the shell aliases call collect.py
        by absolute path, so a CWD-relative rule would make the documented
        `AIU_DB=./data/usage.db` point somewhere new every time you ran
        `aiusage` from a different directory -- silently starting an empty
        second database rather than reading yours."""
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            self.addCleanup(os.chdir, cwd)

            for value in ("./data/usage.db", "data/usage.db"):
                with self.subTest(value):
                    self.assertEqual(collect._expand(value),
                                     collect.ROOT / "data" / "usage.db")

    def test_absolute_and_tilde_paths_are_left_where_they_point(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            absolute = Path(tmp).resolve() / "elsewhere.db"
            self.assertEqual(collect._expand(str(absolute)), absolute)
        self.assertEqual(collect._expand("~/.claude"),
                         Path.home().joinpath(".claude").resolve())


class LoadEnvTests(unittest.TestCase):
    def test_real_environment_variables_win_over_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("# comment\nAIU_TEST_A=from_file\nAIU_TEST_B=from_file\n"
                           "malformed line\nAIU_TEST_C='quoted'\n")
            os.environ["AIU_TEST_A"] = "from_shell"
            for key in ("AIU_TEST_A", "AIU_TEST_B", "AIU_TEST_C"):
                self.addCleanup(os.environ.pop, key, None)

            collect.load_env(env)

            self.assertEqual(os.environ["AIU_TEST_A"], "from_shell")
            self.assertEqual(os.environ["AIU_TEST_B"], "from_file")
            self.assertEqual(os.environ["AIU_TEST_C"], "quoted")


if __name__ == "__main__":
    unittest.main()
