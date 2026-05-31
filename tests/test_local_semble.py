from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import core  # noqa: E402
import local_semble  # noqa: E402


class _FakeContentType:
    CODE = "code"
    DOCS = "docs"
    CONFIG = "config"

    def __call__(self, value: str) -> str:
        return value


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


class LocalSembleAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_search_saves_fresh_index_to_semble_cache(self) -> None:
        fake_index = Mock()
        fake_index.loaded_from_disk = False
        fake_index.search.return_value = ["hit"]
        fake_semble_index = Mock()
        fake_semble_index.from_path.return_value = fake_index
        fake_format_results = Mock(return_value={"query": "q", "results": ["hit"]})
        fake_save_index = Mock()

        with patch(
            "local_semble._load_semble_api",
            return_value=(
                fake_semble_index,
                _FakeContentType(),
                fake_format_results,
                Mock(),
                fake_save_index,
            ),
        ):
            payload = local_semble.search(
                query="q",
                project_root=str(self.project_root),
                top_k=2,
                content=["code"],
            )

        resolved_root = str(self.project_root.resolve())
        fake_semble_index.from_path.assert_called_once_with(resolved_root, content=["code"])
        fake_index.search.assert_called_once_with("q", top_k=2)
        fake_save_index.assert_called_once_with(fake_index, resolved_root)
        self.assertEqual(payload["_meta"]["cache"], "saved")
        self.assertEqual(payload["_meta"]["runner"], "library")

    def test_search_reuses_loaded_index_without_resaving(self) -> None:
        fake_index = Mock()
        fake_index.loaded_from_disk = True
        fake_index.search.return_value = ["hit"]
        fake_semble_index = Mock()
        fake_semble_index.from_path.return_value = fake_index
        fake_save_index = Mock()

        with patch(
            "local_semble._load_semble_api",
            return_value=(
                fake_semble_index,
                _FakeContentType(),
                Mock(return_value={"query": "q", "results": ["hit"]}),
                Mock(),
                fake_save_index,
            ),
        ):
            payload = local_semble.search(
                query="q",
                project_root=str(self.project_root),
                top_k=2,
                content=["all"],
            )

        fake_semble_index.from_path.assert_called_once_with(
            str(self.project_root.resolve()),
            content=["code", "docs", "config"],
        )
        fake_save_index.assert_not_called()
        self.assertEqual(payload["_meta"]["cache"], "hit")


class LocalSembleCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.cache_root = self.root / "semble-cache"
        self.cache_root.mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_metadata(self, entry: Path, root_path: str | None, timestamp: float = 0.0) -> None:
        index = entry / "index"
        index.mkdir(parents=True)
        (index / "metadata.json").write_text(
            json.dumps(
                {
                    "root_path": root_path,
                    "time": timestamp,
                    "model_path": "minishlab/potion-code-16M",
                    "content_type": ["code"],
                    "file_paths": ["src/app.py"],
                }
            ),
            encoding="utf-8",
        )
        (index / "chunks.json").write_text("[]", encoding="utf-8")

    def test_clear_project_cache_removes_project_hash_entry(self) -> None:
        project_root = self.root / "repo"
        project_root.mkdir()
        cache_entry = self.cache_root / "project-hash"
        self._write_metadata(cache_entry, str(project_root))

        def fake_find_index_from_cache_folder(_path: str) -> Path:
            return cache_entry / "index"

        with patch(
            "local_semble._load_semble_cache_api",
            return_value=(fake_find_index_from_cache_folder, Mock(return_value=self.cache_root)),
        ):
            result = local_semble.clear_project_cache(str(project_root))

        self.assertTrue(result["existed"])
        self.assertTrue(result["removed"])
        self.assertGreater(result["bytes"], 0)
        self.assertFalse(cache_entry.exists())

    def test_gc_stale_caches_removes_missing_roots_and_broken_entries(self) -> None:
        live_root = self.root / "live"
        live_root.mkdir()
        live_entry = self.cache_root / "live-hash"
        stale_entry = self.cache_root / "stale-hash"
        broken_entry = self.cache_root / "broken-hash"
        self._write_metadata(live_entry, str(live_root))
        self._write_metadata(stale_entry, str(self.root / "missing"))
        (broken_entry / "index").mkdir(parents=True)
        (broken_entry / "index" / "payload.bin").write_text("broken", encoding="utf-8")

        with patch(
            "local_semble._load_semble_cache_api",
            return_value=(Mock(), Mock(return_value=self.cache_root)),
        ):
            result = local_semble.gc_stale_caches()

        self.assertEqual(result["removed_count"], 2)
        self.assertFalse(stale_entry.exists())
        self.assertFalse(broken_entry.exists())
        self.assertTrue(live_entry.exists())
        reasons = {entry["reason"] for entry in result["entries"]}
        self.assertEqual(reasons, {"missing-root-path", "missing-metadata"})


if __name__ == "__main__":
    unittest.main()
