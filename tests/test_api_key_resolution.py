from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import core  # noqa: E402


class ApiKeyResolutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.home = Path(self.temp_dir.name)
        # 显式来源指向不存在的文件，保证探测结果只来自本用例。
        self.missing = self.home / "missing.vscdb"
        self.env = {
            "WINDSURF_CREDENTIALS_DB": str(self.missing),
        }

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_env_var_key_takes_precedence_over_credentials_db(self) -> None:
        with patch.dict(os.environ, {**self.env, "WINDSURF_API_KEY": "explicit-key"}, clear=False):
            self.assertEqual(core.get_api_key(), "explicit-key")

    def test_credentials_db_env_var_is_honoured(self) -> None:
        db_path = self.home / "copied.vscdb"
        conn = __import__("sqlite3").connect(db_path)
        conn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO ItemTable (key, value) VALUES (?, ?)",
            ("windsurfAuthStatus", '{"apiKey": "devin-session-token$from-db"}'),
        )
        conn.commit()
        conn.close()

        with patch.dict(os.environ, {"WINDSURF_CREDENTIALS_DB": str(db_path)}, clear=False):
            os.environ.pop("WINDSURF_API_KEY", None)
            self.assertEqual(core.get_api_key(), "devin-session-token$from-db")

    def test_missing_key_error_reports_source_and_searched_paths(self) -> None:
        with patch.dict(os.environ, self.env, clear=False):
            os.environ.pop("WINDSURF_API_KEY", None)
            with self.assertRaises(RuntimeError) as raised:
                core.get_api_key()

        message = str(raised.exception)
        # 用户要能从这里看出「去哪里放凭据」，而不是只看到一句泛化提示。
        self.assertIn("WINDSURF_CREDENTIALS_DB", message)
        self.assertIn(str(self.missing), message)


if __name__ == "__main__":
    unittest.main()
