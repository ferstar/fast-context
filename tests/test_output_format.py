from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import core  # noqa: E402


def _write_file(path: Path, total_lines: int, replacements: dict[int, str]) -> None:
    lines = [f"# filler {idx}\n" for idx in range(1, total_lines + 1)]
    for line_no, text in replacements.items():
        lines[line_no - 1] = text + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines), encoding="utf-8")


class SearchOutputFormatTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_root = Path(self.temp_dir.name)

        _write_file(
            self.project_root / "src" / "core.py",
            60,
            {
                10: "def search_with_content(query: str) -> str:",
                11: '    """Format CLI output for agent-facing search results."""',
                12: '    return "ok"',
                30: "def _parse_response(data: bytes):",
                31: '    return "[Error]", tool_info',
            },
        )
        _write_file(
            self.project_root / "src" / "fast_context_cli.py",
            30,
            {
                5: "def run_search() -> int:",
                6: "    return 0",
            },
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _mock_result(self) -> dict:
        core_path = self.project_root / "src" / "core.py"
        cli_path = self.project_root / "src" / "fast_context_cli.py"
        return {
            "files": [
                {
                    "path": "src/core.py",
                    "full_path": str(core_path),
                    "ranges": [(10, 12), (30, 31)],
                },
                {
                    "path": "src/fast_context_cli.py",
                    "full_path": str(cli_path),
                    "ranges": [(5, 6)],
                },
            ],
            "rg_patterns": [
                "core",
                "main",
                "search_with_content",
                "tool_info",
            ],
            "_meta": {
                "tree_depth": 3,
                "tree_size_kb": 0.5,
                "fell_back": True,
            },
        }

    @patch("core.search")
    def test_default_success_output_is_agent_focused(self, mock_search) -> None:
        mock_search.return_value = self._mock_result()

        result = core.search_with_content(
            query="where is search_with_content parsed and where is tool_info returned",
            project_root=str(self.project_root),
        )

        self.assertIn("Start here:", result)
        self.assertIn("search_with_content()", result)
        self.assertIn("Format CLI output for agent-facing search results.", result)
        self.assertIn("Follow-up search terms:", result)
        self.assertIn("search_with_content, tool_info", result)
        self.assertNotIn("IMPORTANT:", result)
        self.assertNotIn("[config]", result)
        self.assertNotIn("anchor:", result)
        self.assertNotIn("core, main", result)

    @patch("core.search")
    def test_verbose_success_output_includes_anchor_and_config(self, mock_search) -> None:
        mock_search.return_value = self._mock_result()

        result = core.search_with_content(
            query="where is search_with_content parsed and where is tool_info returned",
            project_root=str(self.project_root),
            verbose=True,
        )

        self.assertIn("anchor: def search_with_content(query: str) -> str:", result)
        self.assertIn("[config] tree_depth=3, tree_size=0.5KB", result)
        self.assertIn("fell back from requested depth", result)


if __name__ == "__main__":
    unittest.main()
