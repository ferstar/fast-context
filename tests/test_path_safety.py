from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import core  # noqa: E402

SECRET = "OUTSIDE-ROOT-SECRET"


class ToolPathContainmentTest(unittest.TestCase):
    """模型给出的路径不得读到项目根之外的内容。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.project = self.base / "project"
        self.project.mkdir()
        (self.project / "inside.py").write_text("print('inside')\n", encoding="utf-8")
        (self.project / "nested").mkdir()
        (self.project / "nested" / "deep.py").write_text("# deep\n", encoding="utf-8")

        self.outside = self.base / "outside"
        self.outside.mkdir()
        self.secret_file = self.outside / "secret.txt"
        self.secret_file.write_text(f"{SECRET}\n", encoding="utf-8")

        self.executor = core.ToolExecutor(str(self.project))
        self.original_cwd = os.getcwd()

    def tearDown(self) -> None:
        os.chdir(self.original_cwd)
        self.temp_dir.cleanup()

    def assert_rejected(self, output: str) -> None:
        self.assertIn("path outside codebase", output)
        self.assertNotIn(SECRET, output)

    def test_reads_virtual_and_relative_paths_inside_root(self) -> None:
        self.assertIn("inside", self.executor.readfile("/codebase/inside.py"))
        self.assertIn("inside", self.executor.readfile("inside.py"))
        self.assertIn("deep", self.executor.readfile("/codebase/nested/deep.py"))

    def test_relative_paths_do_not_depend_on_process_cwd(self) -> None:
        os.chdir(self.base)

        self.assertIn("inside", self.executor.readfile("inside.py"))

    def test_rejects_absolute_path_outside_root(self) -> None:
        self.assert_rejected(self.executor.readfile(str(self.secret_file)))

    def test_rejects_traversal_outside_root(self) -> None:
        relative_escape = os.path.join("..", "outside", "secret.txt")

        self.assert_rejected(self.executor.readfile(relative_escape))
        self.assert_rejected(self.executor.readfile(f"/codebase/../{relative_escape}"))
        self.assert_rejected(self.executor.readfile("/etc/hosts"))

    def test_rejects_symlink_escape(self) -> None:
        link = self.project / "escape.txt"
        link.symlink_to(self.secret_file)
        dir_link = self.project / "escape_dir"
        dir_link.symlink_to(self.outside, target_is_directory=True)

        self.assert_rejected(self.executor.readfile("escape.txt"))
        self.assert_rejected(self.executor.readfile("escape_dir/secret.txt"))
        self.assert_rejected(self.executor.ls("escape_dir"))

    def test_rejects_windows_absolute_paths(self) -> None:
        self.assert_rejected(self.executor.readfile(r"C:\Users\someone\.ssh\id_rsa"))
        self.assert_rejected(self.executor.readfile(r"\\server\share\secret.txt"))

    def test_rg_and_tree_and_ls_stay_inside_root(self) -> None:
        self.assert_rejected(self.executor.rg("SECRET", str(self.outside)))
        self.assert_rejected(self.executor.tree(str(self.outside)))
        self.assert_rejected(self.executor.ls(str(self.outside)))

        inside_hit = self.executor.rg("inside", "/codebase")
        self.assertIn("inside.py", inside_hit)

    def test_glob_cannot_escape_through_pattern_or_path(self) -> None:
        self.assert_rejected(self.executor.glob_cmd("*", str(self.outside)))

        escaping_pattern = os.path.join("..", "outside", "*.txt")
        self.assertNotIn(SECRET, self.executor.glob_cmd(escaping_pattern, "."))

    def test_resolve_reports_rejection_for_empty_input(self) -> None:
        resolved, error = self.executor.resolve("")

        self.assertIsNone(resolved)
        self.assertIsNotNone(error)


if __name__ == "__main__":
    unittest.main()
