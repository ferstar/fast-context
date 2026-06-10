from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import local_repo_map  # noqa: E402


class LocalRepoMapTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_root = Path(self.temp_dir.name)
        self.excludes = ["node_modules", ".git", "__pycache__"]

        self._write(
            "apps/desktop/src/auth/session.py",
            (
                "# Desktop auth session manager\n"
                "def validate_handoff_state(token: str) -> bool:\n"
                "    return token.startswith('devin-session-token')\n"
            ),
        )
        self._write(
            "apps/desktop/src/auth/handoff.py",
            (
                "# Browser login handoff state\n"
                "def redeem_browser_handoff(state: str) -> str:\n"
                "    return state\n"
            ),
        )
        self._write(
            "packages/ui/src/button.tsx",
            "export function Button() { return null }\n",
        )
        self._write(
            "docs/release.md",
            "# Release notes\n\nNothing about login.\n",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write(self, rel_path: str, content: str) -> None:
        path = self.project_root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_score_directories_uses_query_relevance(self) -> None:
        top_dirs = local_repo_map.list_top_level_dirs(str(self.project_root), self.excludes)

        result = local_repo_map.score_directories(
            "where is desktop browser login handoff state validated",
            str(self.project_root),
            top_dirs,
            self.excludes,
            top_k=2,
        )

        self.assertEqual(result["hot_dirs"][0], "apps")
        self.assertIn("apps/desktop/src/auth/handoff.py", result["path_spines"])
        self.assertIn("bm25f", result["signals"])

    def test_optimized_repo_map_adds_hotspot_subtree_and_path_spines(self) -> None:
        result = local_repo_map.build_optimized_repo_map(
            project_root=str(self.project_root),
            query="where is desktop browser login handoff state validated",
            target_depth=3,
            exclude_paths=self.excludes,
            max_bytes=12 * 1024,
        )

        self.assertEqual(result["strategy"], "bootstrap_hotspot")
        self.assertIn("apps", result["hot_dirs"])
        self.assertIn("# Hotspot Subtrees", result["tree"])
        self.assertIn("├── desktop", result["tree"])
        self.assertIn("# Relevant File Paths", result["tree"])
        self.assertIn("/codebase/apps/desktop/src/auth/session.py", result["tree"])


if __name__ == "__main__":
    unittest.main()
