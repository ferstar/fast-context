from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import core  # noqa: E402


class ModelFallbackTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_root = self.temp_dir.name

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @patch("core._search_once")
    @patch("core.check_rate_limit")
    @patch("core.build_local_anchor_brief")
    @patch("core.get_repo_map")
    def test_search_falls_back_after_resource_exhausted(
        self,
        mock_repo_map,
        mock_anchor_brief,
        mock_check_rate_limit,
        mock_search_once,
    ) -> None:
        mock_repo_map.return_value = ("/codebase\n├── src", 3, 512, False)
        mock_anchor_brief.return_value = ""
        mock_check_rate_limit.side_effect = [True, True]
        mock_search_once.side_effect = [
            {
                "files": [],
                "error": "[Error] resource_exhausted: backend overloaded",
                "_meta": {
                    "tree_depth": 3,
                    "tree_size_kb": 0.5,
                    "fell_back": False,
                    "project_root": self.project_root,
                    "model": "MODEL_SWE_1_6_FAST",
                    "error_code": "resource_exhausted",
                },
            },
            {
                "files": [],
                "raw_response": "<ANSWER></ANSWER>",
                "_meta": {
                    "tree_depth": 3,
                    "tree_size_kb": 0.5,
                    "fell_back": False,
                    "project_root": self.project_root,
                    "model": "MODEL_SWE_1_5",
                },
            },
        ]

        with patch.dict(
            os.environ,
            {
                "WS_MODEL": "MODEL_SWE_1_6_FAST",
                "WS_FALLBACK_MODELS": "MODEL_SWE_1_5",
            },
            clear=False,
        ):
            result = core.search(
                query="where is auth checked",
                project_root=self.project_root,
                api_key="token",
                jwt="jwt",
            )

        self.assertEqual(result["_meta"]["model"], "MODEL_SWE_1_5")
        self.assertEqual(
            result["_meta"]["model_attempts"],
            ["MODEL_SWE_1_6_FAST", "MODEL_SWE_1_5"],
        )
        self.assertTrue(result["_meta"]["fallback_used"])
        self.assertEqual(mock_search_once.call_count, 2)
        self.assertEqual(mock_search_once.call_args_list[0].kwargs["model"], "MODEL_SWE_1_6_FAST")
        self.assertEqual(mock_search_once.call_args_list[1].kwargs["model"], "MODEL_SWE_1_5")
        self.assertEqual(
            mock_check_rate_limit.call_args_list[0].args,
            ("token", "jwt", "MODEL_SWE_1_6_FAST"),
        )
        self.assertEqual(
            mock_check_rate_limit.call_args_list[1].args,
            ("token", "jwt", "MODEL_SWE_1_5"),
        )

    @patch("core._search_once")
    @patch("core.check_rate_limit")
    @patch("core.build_local_anchor_brief")
    @patch("core.get_repo_map")
    def test_search_skips_rate_limited_primary_model(
        self,
        mock_repo_map,
        mock_anchor_brief,
        mock_check_rate_limit,
        mock_search_once,
    ) -> None:
        mock_repo_map.return_value = ("/codebase\n├── src", 3, 512, False)
        mock_anchor_brief.return_value = ""
        mock_check_rate_limit.side_effect = [False, True]
        mock_search_once.return_value = {
            "files": [],
            "raw_response": "<ANSWER></ANSWER>",
            "_meta": {
                "tree_depth": 3,
                "tree_size_kb": 0.5,
                "fell_back": False,
                "project_root": self.project_root,
                "model": "MODEL_SWE_1_5",
            },
        }

        with patch.dict(
            os.environ,
            {
                "WS_MODEL": "MODEL_SWE_1_6_FAST",
                "WS_FALLBACK_MODELS": "MODEL_SWE_1_5",
            },
            clear=False,
        ):
            result = core.search(
                query="where is auth checked",
                project_root=self.project_root,
                api_key="token",
                jwt="jwt",
            )

        self.assertEqual(result["_meta"]["model"], "MODEL_SWE_1_5")
        self.assertEqual(
            result["_meta"]["model_attempts"],
            ["MODEL_SWE_1_6_FAST", "MODEL_SWE_1_5"],
        )
        self.assertTrue(result["_meta"]["fallback_used"])
        self.assertEqual(mock_search_once.call_count, 1)
        self.assertEqual(mock_search_once.call_args.kwargs["model"], "MODEL_SWE_1_5")


if __name__ == "__main__":
    unittest.main()
