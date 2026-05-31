from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import core  # noqa: E402


class LocalSembleOutputTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _semble_payload(self) -> dict:
        return {
            "query": "hybrid search",
            "results": [
                {
                    "chunk": {
                        "content": (
                            "def search(query: str):\n"
                            "    \"\"\"Hybrid search implementation.\"\"\"\n"
                            "    return []\n"
                        ),
                        "file_path": "src/search.py",
                        "start_line": 10,
                        "end_line": 12,
                        "language": "python",
                    },
                    "score": 0.123456,
                }
            ],
            "_meta": {"backend": "semble", "runner": "library"},
        }

    @patch("core.semble_search")
    def test_local_backend_formats_chunks(self, mock_search) -> None:
        mock_search.return_value = self._semble_payload()

        result = core.search_with_content(
            query="hybrid search",
            project_root=str(self.project_root),
            backend="local",
            max_results=3,
        )

        self.assertIn("Local Semble results:", result)
        self.assertIn(str(self.project_root / "src/search.py"), result)
        self.assertIn("L10-12: search()", result)
        self.assertIn("score=0.1235", result)
        self.assertIn("snippet: def search(query: str):", result)
        mock_search.assert_called_once()
        self.assertEqual(mock_search.call_args.kwargs["top_k"], 3)

    @patch("core.semble_search")
    @patch("core.search")
    def test_hybrid_backend_passes_local_candidates_to_remote_search(
        self,
        mock_remote_search,
        mock_semble_search,
    ) -> None:
        remote_path = self.project_root / "src" / "search.py"
        remote_path.parent.mkdir(parents=True, exist_ok=True)
        remote_path.write_text(
            "def search(query: str):\n"
            "    \"\"\"Hybrid search implementation.\"\"\"\n"
            "    return []\n",
            encoding="utf-8",
        )
        mock_remote_search.return_value = {
            "files": [
                {
                    "path": "src/search.py",
                    "full_path": str(remote_path),
                    "ranges": [(1, 3)],
                }
            ],
            "rg_patterns": ["hybrid search"],
            "_meta": {"tree_depth": 3, "tree_size_kb": 0.5},
        }
        mock_semble_search.return_value = self._semble_payload()

        result = core.search_with_content(
            query="hybrid search",
            project_root=str(self.project_root),
            backend="hybrid",
            max_results=3,
        )

        self.assertIn("Start here:", result)
        self.assertIn("Local Semble chunk candidates:", result)
        self.assertIn("src/search.py", result)
        mock_remote_search.assert_called_once()
        local_context = mock_remote_search.call_args.kwargs["local_context"]
        self.assertIn("Local Semble chunk candidates:", local_context)
        self.assertIn("/codebase/src/search.py:L10-12", local_context)
        self.assertIn("Hybrid search implementation.", local_context)
        self.assertIn("Verify them with restricted tools", local_context)

    @patch("core.semble_search")
    @patch("core.search")
    def test_hybrid_backend_uses_local_results_after_remote_error(
        self,
        mock_remote_search,
        mock_semble_search,
    ) -> None:
        mock_remote_search.return_value = {
            "files": [],
            "error": "[Error] resource_exhausted: backend overloaded",
            "_meta": {
                "tree_depth": 3,
                "tree_size_kb": 0.5,
                "fell_back": False,
                "error_code": "resource_exhausted",
            },
        }
        mock_semble_search.return_value = self._semble_payload()

        result = core.search_with_content(
            query="hybrid search",
            project_root=str(self.project_root),
            backend="hybrid",
            max_results=3,
        )

        self.assertIn("Error: [Error] resource_exhausted: backend overloaded", result)
        self.assertIn("Using local Semble results.", result)
        self.assertIn("Local Semble results:", result)
        self.assertIn("src/search.py", result)

    @patch("core.semble_find_related")
    def test_find_related_formats_chunks(self, mock_find_related) -> None:
        mock_find_related.return_value = self._semble_payload()

        result = core.find_related_with_content(
            file_path="src/search.py",
            line=10,
            project_root=str(self.project_root),
            max_results=2,
        )

        self.assertIn("Related local Semble chunks for src/search.py:10:", result)
        self.assertIn("L10-12: search()", result)
        mock_find_related.assert_called_once()
        self.assertEqual(mock_find_related.call_args.kwargs["top_k"], 2)


if __name__ == "__main__":
    unittest.main()
