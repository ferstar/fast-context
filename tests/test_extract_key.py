from __future__ import annotations

import json
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

        self.assertEqual(sources, [self.home / ".local" / "share" / "devin" / "credentials.toml"])
        self.assertEqual(db_paths[0], self.home / ".config" / "Deviv" / "User" / "globalStorage" / "state.vscdb")
        self.assertEqual(db_paths[1], self.home / ".config" / "Devin" / "User" / "globalStorage" / "state.vscdb")
        self.assertEqual(db_paths[2], self.home / ".config" / "Windsurf" / "User" / "globalStorage" / "state.vscdb")

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
