from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from email.message import Message
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import core  # noqa: E402


def _jwt(payload: dict) -> str:
    def enc(obj: dict) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    return f"{enc({'alg': 'none'})}.{enc(payload)}.sig"


class JwtCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"XDG_CACHE_HOME": self.temp_dir.name}, clear=False)
        self.env.start()
        core._RATE_LIMIT_OK_UNTIL.clear()
        self.api_key = "super-secret-api-key"

    def tearDown(self) -> None:
        core._RATE_LIMIT_OK_UNTIL.clear()
        self.env.stop()
        self.temp_dir.cleanup()

    def test_cache_path_hashes_the_key_and_respects_xdg(self) -> None:
        path = core._jwt_cache_path(self.api_key)
        digest = hashlib.sha256(self.api_key.encode("utf-8")).hexdigest()[:16]
        self.assertEqual(path, Path(self.temp_dir.name) / "fast-context" / f"jwt-{digest}.json")
        self.assertNotIn(self.api_key, str(path))
        self.assertNotIn("super-secret", str(path))

    def test_store_uses_private_mode(self) -> None:
        token = _jwt({"exp": int(time.time()) + 3600})
        opened: list[tuple[int, int]] = []
        real_open = os.open

        def spy(path, flags, mode=0o777, *args, **kwargs):
            opened.append((flags, mode))
            return real_open(path, flags, mode)

        old_umask = os.umask(0)
        try:
            with patch("core.os.open", side_effect=spy):
                core._store_cached_jwt(self.api_key, token)
        finally:
            os.umask(old_umask)

        path = core._jwt_cache_path(self.api_key)
        self.assertTrue(any(flags & os.O_CREAT and mode == 0o600 for flags, mode in opened))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["token"], token)
        self.assertEqual(set(payload), {"token", "exp"})

    def test_reuse_window_and_missing_exp(self) -> None:
        token = _jwt({"sub": "cached"})
        core._store_cached_jwt(self.api_key, token, now=1_000_000)
        payload = json.loads(core._jwt_cache_path(self.api_key).read_text(encoding="utf-8"))
        self.assertEqual(payload["exp"], 1_000_000 + 600)
        self.assertEqual(core._load_cached_jwt(self.api_key, now=1_000_539), token)
        self.assertIsNone(core._load_cached_jwt(self.api_key, now=1_000_540))

        fresh = _jwt({"exp": int(time.time()) + 3600})
        core._store_cached_jwt(self.api_key, fresh)
        with patch.object(core, "_request_user_jwt", side_effect=AssertionError("network")):
            self.assertEqual(core.fetch_jwt(self.api_key), fresh)
        self.assertEqual(core.fetch_jwt.last_cache, "hit")

        expiring = _jwt({"exp": int(time.time()) + 30})
        core._store_cached_jwt(self.api_key, expiring)
        with patch.object(core, "_request_user_jwt", return_value="fetched-token") as request:
            self.assertEqual(core.fetch_jwt(self.api_key), "fetched-token")
        request.assert_called_once_with(self.api_key)
        self.assertEqual(core.fetch_jwt.last_cache, "miss")

    def test_env_disables_cache_without_rewriting_it(self) -> None:
        token = _jwt({"exp": int(time.time()) + 3600})
        core._store_cached_jwt(self.api_key, token)
        path = core._jwt_cache_path(self.api_key)
        with patch.dict(os.environ, {"FAST_CONTEXT_JWT_CACHE": "0", "XDG_CACHE_HOME": self.temp_dir.name}):
            self.assertIsNone(core._load_cached_jwt(self.api_key))
            with patch.object(core, "_request_user_jwt", return_value="fresh") as request:
                self.assertEqual(core.fetch_jwt(self.api_key), "fresh")
            request.assert_called_once()
            self.assertEqual(core.fetch_jwt.last_cache, "disabled")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["token"], token)

    def test_auth_error_refetches_once_and_does_not_leak_the_token(self) -> None:
        cached = _jwt({"exp": int(time.time()) + 3600, "sub": "cached"})
        fresh = _jwt({"exp": int(time.time()) + 3600, "sub": "fresh"})
        core._store_cached_jwt(self.api_key, cached)
        calls = {"n": 0, "jwts": []}

        def remote(**kwargs):
            calls["n"] += 1
            calls["jwts"].append(kwargs["jwt"])
            if calls["n"] == 1:
                return {
                    "files": [],
                    "error": "AUTH_ERROR: HTTP 401 auth error",
                    "_meta": {"error_code": "AUTH_ERROR"},
                }
            return {
                "files": [{"path": "a.py", "full_path": "/tmp/a.py", "ranges": [(1, 2)]}],
                "_meta": {},
            }

        with (
            patch.object(core, "_get_repo_map_with_meta", return_value=("tree", 1, 8, False, {})),
            patch.object(core, "build_local_anchor_brief", return_value=""),
            patch.object(core, "_search_remote_models", side_effect=remote),
            patch.object(core, "_request_user_jwt", return_value=fresh) as request,
        ):
            result = core.search(
                query="where is auth checked",
                project_root=self.temp_dir.name,
                api_key=self.api_key,
            )

        self.assertEqual(calls["n"], 2)
        self.assertEqual(calls["jwts"], [cached, fresh])
        request.assert_called_once_with(self.api_key)
        self.assertEqual(result["_meta"]["jwt_cache"], "refetch")
        self.assertEqual(core._load_cached_jwt(self.api_key), fresh)
        blob = json.dumps(result)
        self.assertNotIn(cached, blob)
        self.assertNotIn(fresh, blob)
        self.assertIn("jwt", result["_meta"]["timing_ms"])
        self.assertIn("jwt_refetch", result["_meta"]["timing_ms"])

    def test_second_auth_error_does_not_loop(self) -> None:
        cached = _jwt({"exp": int(time.time()) + 3600})
        core._store_cached_jwt(self.api_key, cached)
        calls = {"n": 0}

        def remote(**kwargs):
            calls["n"] += 1
            return {
                "files": [],
                "error": "[Error] unauthenticated: token rejected",
                "_meta": {"error_code": "unauthenticated"},
            }

        with (
            patch.object(core, "_get_repo_map_with_meta", return_value=("tree", 1, 8, False, {})),
            patch.object(core, "build_local_anchor_brief", return_value=""),
            patch.object(core, "_search_remote_models", side_effect=remote),
            patch.object(core, "_request_user_jwt", return_value=_jwt({"exp": int(time.time()) + 60})) as request,
        ):
            result = core.search(query="q", project_root=self.temp_dir.name, api_key=self.api_key)

        self.assertEqual(calls["n"], 2)
        self.assertEqual(request.call_count, 1)
        self.assertIn("unauthenticated", result["error"])

    def test_supplied_jwt_is_not_refetched(self) -> None:
        calls = {"n": 0}

        def remote(**kwargs):
            calls["n"] += 1
            self.assertEqual(kwargs["jwt"], "supplied-token")
            return {
                "files": [],
                "error": "AUTH_ERROR: HTTP 403 auth error",
                "_meta": {"error_code": "AUTH_ERROR"},
            }

        with (
            patch.object(core, "_get_repo_map_with_meta", return_value=("tree", 1, 8, False, {})),
            patch.object(core, "build_local_anchor_brief", return_value=""),
            patch.object(core, "_search_remote_models", side_effect=remote),
            patch.object(core, "_request_user_jwt", side_effect=AssertionError("refetch")) as request,
        ):
            result = core.search(
                query="q",
                project_root=self.temp_dir.name,
                api_key=self.api_key,
                jwt="supplied-token",
            )

        self.assertEqual(calls["n"], 1)
        request.assert_not_called()
        self.assertEqual(result["_meta"]["jwt_cache"], "supplied")
        self.assertNotIn("supplied-token", json.dumps(result))

    def test_refetch_failure_keeps_the_original_auth_error(self) -> None:
        cached = _jwt({"exp": int(time.time()) + 3600})
        core._store_cached_jwt(self.api_key, cached)
        calls = {"n": 0}

        def remote(**kwargs):
            calls["n"] += 1
            return {
                "files": [],
                "error": "AUTH_ERROR: HTTP 401 auth error",
                "_meta": {"error_code": "AUTH_ERROR"},
            }

        with (
            patch.object(core, "_get_repo_map_with_meta", return_value=("tree", 1, 8, False, {})),
            patch.object(core, "build_local_anchor_brief", return_value=""),
            patch.object(core, "_search_remote_models", side_effect=remote),
            patch.object(core, "_request_user_jwt", side_effect=RuntimeError("无法从 GetUserJwt 响应中提取 JWT")),
        ):
            result = core.search(query="q", project_root=self.temp_dir.name, api_key=self.api_key)

        self.assertEqual(calls["n"], 1)
        self.assertIn("AUTH_ERROR", result["error"])
        self.assertEqual(result["_meta"]["jwt_cache"], "refetch_failed")
        self.assertIsNone(core._load_cached_jwt(self.api_key))

    def test_positive_rate_limit_is_cached_and_429_is_not(self) -> None:
        with patch.object(core, "_unary_request", return_value=b"") as request:
            self.assertTrue(core.check_rate_limit("k", "jwt", "swe-1-7"))
            self.assertFalse(core.check_rate_limit.last_cached)
            core._RATE_LIMIT_OK_UNTIL.clear()
            self.assertTrue(core.check_rate_limit("k", "jwt", "swe-1-7"))
            self.assertTrue(core.check_rate_limit.last_cached)
            self.assertEqual(request.call_count, 1)
            self.assertTrue(core.check_rate_limit("k", "jwt", "other-model"))
            self.assertFalse(core.check_rate_limit.last_cached)
            self.assertEqual(request.call_count, 2)

        core._forget_rate_limit_ok(core._rate_limit_cache_key("k", "swe-1-7"))
        err = core.FastContextError("HTTP 429 rate limited", "RATE_LIMITED", status=429)
        with patch.object(core, "_unary_request", side_effect=err) as request:
            self.assertFalse(core.check_rate_limit("k", "jwt", "swe-1-7"))
            self.assertFalse(core.check_rate_limit("k", "jwt", "swe-1-7"))
            self.assertEqual(request.call_count, 2)
            self.assertFalse(core.check_rate_limit.last_cached)

        http_err = HTTPError(
            "https://example.test",
            429,
            "Too Many Requests",
            Message(),
            BytesIO(b""),
        )
        with patch.object(core, "_unary_request", side_effect=http_err):
            self.assertFalse(core.check_rate_limit("k2", "jwt", "swe-1-7"))

    def test_timing_line_omits_secrets(self) -> None:
        line = core._format_timing_line(
            {
                "timing_ms": {"jwt": 12.4, "remote": 4000, "rate_limit": 0},
                "jwt_cache": "hit",
                "rate_limit_cached": 1,
                "remote_lock_wait_ms": 3,
            }
        )
        self.assertIsNotNone(line)
        assert line is not None
        self.assertIn("jwt=12ms", line)
        self.assertIn("remote=4000ms", line)
        self.assertIn("rate_limit=0ms", line)
        self.assertIn("jwt_cache=hit", line)
        self.assertIn("rate_limit_cached=1", line)
        self.assertIn("lock_wait=3ms", line)
        self.assertNotIn(self.api_key, line)
        self.assertIsNone(core._format_timing_line({}))


if __name__ == "__main__":
    unittest.main()
