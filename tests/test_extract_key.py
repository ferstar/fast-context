from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import extract_key  # noqa: E402


class ExtractKeyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.home = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_extracts_devin_cli_toml_credentials(self) -> None:
        credentials = self.home / ".local" / "share" / "devin" / "credentials.toml"
        credentials.parent.mkdir(parents=True)
        credentials.write_text('api_key = "sk-devin-test"\n', encoding="utf-8")

        result = extract_key.extract_key(credentials)

        self.assertEqual(result["api_key"], "sk-devin-test")
        self.assertEqual(result["source_type"], "devin_cli_credentials")

    def test_linux_sources_try_devin_cli_before_app_databases(self) -> None:
        sources = extract_key.get_cli_credential_path_candidates(system="Linux", home=self.home)
        db_paths = extract_key.get_db_path_candidates(
            system="Linux",
            home=self.home,
            env={"XDG_CONFIG_HOME": str(self.home / ".config")},
        )

        config = self.home / ".config"
        self.assertEqual(sources, [self.home / ".local" / "share" / "devin" / "credentials.toml"])
        # 规范大小写在前，小写形态紧随其后：安装器对目录名大小写并不一致。
        self.assertEqual(
            db_paths,
            [
                config / "Deviv" / "User" / "globalStorage" / "state.vscdb",
                config / "deviv" / "User" / "globalStorage" / "state.vscdb",
                config / "Devin" / "User" / "globalStorage" / "state.vscdb",
                config / "devin" / "User" / "globalStorage" / "state.vscdb",
                config / "Windsurf" / "User" / "globalStorage" / "state.vscdb",
                config / "windsurf" / "User" / "globalStorage" / "state.vscdb",
            ],
        )

    def test_discovers_credentials_in_lowercase_install_dir(self) -> None:
        db_path = self.home / ".config" / "devin" / "User" / "globalStorage" / "state.vscdb"
        db_path.parent.mkdir(parents=True)
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO ItemTable (key, value) VALUES (?, ?)",
            ("windsurfAuthStatus", json.dumps({"apiKey": "devin-session-token$lowercase"})),
        )
        conn.commit()
        conn.close()

        candidates = extract_key.get_db_path_candidates(
            system="Linux",
            home=self.home,
            env={"XDG_CONFIG_HOME": str(self.home / ".config")},
        )
        with patch("extract_key.get_credential_sources") as mock_sources:
            mock_sources.return_value = [{"type": "sqlite", "path": path} for path in candidates]

            result = extract_key.extract_key()

        self.assertEqual(result["api_key"], "devin-session-token$lowercase")
        # 大小写不敏感的文件系统（macOS/Windows）会让规范候选直接命中该目录，
        # 报告的来源可能是任一种写法；断言两条路径指向同一文件，Linux 亦然。
        self.assertTrue(os.path.samefile(result["db_path"], db_path))

    def test_explicit_credentials_db_env_var_wins(self) -> None:
        db_path = self.home / "copied-from-another-host.vscdb"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO ItemTable (key, value) VALUES (?, ?)",
            ("windsurfAuthStatus", json.dumps({"apiKey": "devin-session-token$explicit"})),
        )
        conn.commit()
        conn.close()

        with patch.dict("os.environ", {"WINDSURF_CREDENTIALS_DB": str(db_path)}):
            sources = extract_key.get_credential_sources()
            result = extract_key.extract_key()

        self.assertEqual(sources, [{"type": "sqlite", "path": db_path}])
        self.assertEqual(result["api_key"], "devin-session-token$explicit")

    def test_missing_explicit_credentials_db_is_reported_with_its_path(self) -> None:
        missing = self.home / "not-there.vscdb"

        with patch.dict("os.environ", {"WINDSURF_CREDENTIALS_DB": str(missing)}):
            result = extract_key.extract_key()

        self.assertNotIn("api_key", result)
        self.assertIn("WINDSURF_CREDENTIALS_DB", result["error"])
        self.assertEqual(result["tried_paths"], [str(missing)])

    def test_reports_searched_paths_when_no_source_exists(self) -> None:
        missing_toml = self.home / "credentials.toml"
        missing_db = self.home / "state.vscdb"

        with patch("extract_key.get_credential_sources") as mock_sources:
            mock_sources.return_value = [
                {"type": "toml", "path": missing_toml},
                {"type": "sqlite", "path": missing_db},
            ]

            result = extract_key.extract_key()

        self.assertNotIn("api_key", result)
        self.assertEqual(result["tried_paths"], [str(missing_toml), str(missing_db)])

    def test_auto_discovery_uses_first_existing_source(self) -> None:
        db_path = self.home / ".config" / "Devin" / "User" / "globalStorage" / "state.vscdb"
        db_path.parent.mkdir(parents=True)
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO ItemTable (key, value) VALUES (?, ?)",
            ("windsurfAuthStatus", json.dumps({"apiKey": "devin-session-token$abc"})),
        )
        conn.commit()
        conn.close()

        with patch("extract_key.get_credential_sources") as mock_sources:
            mock_sources.return_value = [
                {"type": "toml", "path": self.home / ".local" / "share" / "devin" / "credentials.toml"},
                {"type": "sqlite", "path": db_path},
            ]

            result = extract_key.extract_key()

        self.assertEqual(result["api_key"], "devin-session-token$abc")
        self.assertEqual(result["source_type"], "sqlite")


if __name__ == "__main__":
    unittest.main()
