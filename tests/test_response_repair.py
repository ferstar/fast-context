from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import core  # noqa: E402
from response_repair import (  # noqa: E402
    parse_json_with_repair,
    repair_json_text,
    salvage_restricted_exec_args,
    salvage_search_evidence,
)

# Captured from a live remote search (2026-10): every readfile line range in
# one turn lost its `": ` separators (`"start": 350` degraded to `"start_350`).
REAL_COLLAPSED_PAYLOAD = (
    '[TOOL_CALLS]restricted_exec[ARGS]{"command1": {"type": "readfile", '
    '"file": "/codebase/src/orient.py", "start_350, "end_450}, '
    '"command2": {"type": "rg", "pattern": "line.*score", "path": "/codebase/src", "exclude": []}, '
    '"command3": {"type": "readfile", "file": "/codebase/src/stamps.py", "start_1, "end_50}}'
)


class RepairJsonTextTest(unittest.TestCase):
    def test_valid_json_passes_through(self) -> None:
        self.assertEqual(
            parse_json_with_repair('{"a": 1, "b": [1, 2]}'),
            {"a": 1, "b": [1, 2]},
        )

    def test_repairs_missing_opening_quote_and_trailing_comma(self) -> None:
        self.assertEqual(
            parse_json_with_repair('{"path":"/codebase/system",exclude":[],}'),
            {"path": "/codebase/system", "exclude": []},
        )

    def test_repairs_collapsed_separator(self) -> None:
        self.assertEqual(
            parse_json_with_repair(
                '{"type": "readfile", "file": "/codebase/a.py", "start_4, "end_9}'
            ),
            {"type": "readfile", "file": "/codebase/a.py", "start_line": 4, "end_line": 9},
        )

    def test_unterminated_string_is_unrecoverable(self) -> None:
        self.assertIsNone(parse_json_with_repair('{"file": "/cod'))

    def test_repair_does_not_touch_values_with_underscore_digits(self) -> None:
        # A string value like "name_3" followed by a comma must survive: the
        # collapse rule only fires on object members with no value after them.
        repaired = repair_json_text('{"a": "name_3", "b": 1}')
        self.assertIn('"name_3"', repaired)


class SalvageRestrictedExecArgsTest(unittest.TestCase):
    def test_salvages_truncated_outer_json(self) -> None:
        args = salvage_restricted_exec_args(
            '[TOOL_CALLS]restricted_exec[ARGS]{"command1":{"type":"readfile",'
            '"file":"/codebase/src/a.py","start_line":4,"end_line":9}'
        )
        assert args is not None
        self.assertEqual(args["command1"]["file"], "/codebase/src/a.py")
        self.assertEqual(args["command1"]["start_line"], 4)

    def test_real_collapsed_payload_recovers_all_commands(self) -> None:
        args = salvage_restricted_exec_args(REAL_COLLAPSED_PAYLOAD)
        assert args is not None
        # 3 structured commands survive; the loose layer then re-adds the
        # collapsed readfiles as plain (range-less) duplicates under new keys.
        self.assertEqual(len(args), 5)
        self.assertEqual(args["command1"]["start_line"], 350)
        self.assertEqual(args["command2"]["pattern"], "line.*score")
        plain = [c for c in args.values() if c.get("type") == "readfile" and "start_line" not in c]
        self.assertEqual(len(plain), 2)

    def test_loose_readfile_does_not_clobber_structured_command(self) -> None:
        # command2 is unparseable and dropped; the loose readfile must take a
        # free key instead of overwriting the salvaged command3.
        raw = (
            '[TOOL_CALLS]restricted_exec[ARGS]{"command1":{"type":"rg","pattern":"a","path":"/codebase/src"},'
            '"command2":{"type":"readfile","file":"/codebase/src/broken.py","start_x}, '
            '"command3":{"type":"rg","pattern":"c","path":"/codebase/tests"}, '
            '"command4":{"type":"readfile","file":"/codebase/src/loose.py"}}'
        )
        args = salvage_restricted_exec_args(raw)
        assert args is not None
        self.assertEqual(args["command1"]["pattern"], "a")
        self.assertEqual(args["command3"]["pattern"], "c")
        self.assertEqual(args["command4"]["file"], "/codebase/src/loose.py")

    def test_returns_none_when_nothing_survives(self) -> None:
        self.assertIsNone(
            salvage_restricted_exec_args('{"command1": {"file": "/cod')
        )


class SalvageSearchEvidenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_root = self.temp_dir.name
        nested = Path(self.project_root) / "src"
        nested.mkdir()
        (nested / "a.py").write_text("def target():\n    pass\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_recovers_files_ranges_and_patterns(self) -> None:
        result = salvage_search_evidence(
            '"type":"readfile","file":"/codebase/src/a.py","start_line":1,"end_line":2, '
            '"pattern":"target"',
            self.project_root,
        )
        self.assertEqual(len(result["files"]), 1)
        self.assertEqual(result["files"][0]["path"], "src/a.py")
        self.assertEqual(result["files"][0]["ranges"], [[1, 2]])
        self.assertEqual(result["rg_patterns"], ["target"])

    def test_drops_paths_outside_project(self) -> None:
        result = salvage_search_evidence(
            '"file":"/codebase/../../etc/passwd"', self.project_root
        )
        self.assertEqual(result["files"], [])

    def test_recovers_loose_paths_without_structure(self) -> None:
        result = salvage_search_evidence(
            "I would read /codebase/src/a.py first", self.project_root
        )
        self.assertEqual(
            [f["path"] for f in result["files"]],
            ["src/a.py"],
        )


class SearchIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_root = self.temp_dir.name
        (Path(self.project_root) / "a.py").write_text(
            "def target():\n    pass\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def _frame(text: str) -> bytes:
        body = text.encode("utf-8")
        varint = b""
        n = len(body)
        while True:
            b = n & 0x7F
            n >>= 7
            varint += bytes([b | 0x80]) if n else bytes([b])
            if not n:
                break
        payload = b"\x1a" + varint + body  # protobuf string field 3
        return b"\x00" + len(payload).to_bytes(4, "big") + payload

    def _search_once(self, responses: list[bytes], max_turns: int = 4) -> dict:
        with patch("core._streaming_request", side_effect=responses):
            return core._search_once(
                query="where is target",
                project_root=self.project_root,
                api_key="token",
                jwt="jwt",
                model="test-model",
                system_prompt="sys",
                tool_defs="defs",
                user_content="q",
                max_turns=max_turns,
                timeout_ms=1000,
                actual_depth=3,
                tree_size_bytes=100,
                fell_back=False,
            )

    def test_plain_text_with_evidence_returns_salvaged_result(self) -> None:
        # No [TOOL_CALLS] marker at all — the model answered in prose, but it
        # names a real file. The salvage exit recovers it as a low-confidence hit.
        text = "No tool call; I would read /codebase/a.py first."
        result = self._search_once([self._frame(text)])
        self.assertEqual(result["files"], [])
        self.assertEqual(
            [f["path"] for f in result["salvaged"]["files"]], ["a.py"]
        )
        self.assertTrue(result["_meta"]["salvaged_response"])
        self.assertIn(text, result["raw_response"])

    def test_malformed_answer_exhaustion_still_salvages_evidence(self) -> None:
        # Broken answer JSON (unrecoverable) repeated past the feedback cap;
        # the closed /codebase path inside survives as salvage evidence.
        wreck = '{"answer": "see /codebase/a.py", "note": "truncated '
        result = self._search_once([self._frame("[TOOL_CALLS]answer[ARGS]" + wreck)] * 5)
        self.assertEqual(result["files"], [])
        self.assertEqual(
            [f["path"] for f in result["salvaged"]["files"]], ["a.py"]
        )
        self.assertTrue(result["_meta"]["salvaged_response"])
        self.assertEqual(
            result["_meta"]["malformed_tool_calls"], core.MAX_MALFORMED_FEEDBACK + 1
        )

    def test_salvaged_result_renders_low_confidence_section(self) -> None:
        result = {
            "files": [],
            "rg_patterns": [],
            "salvaged": salvage_search_evidence(
                'rg "pattern":"target" in /codebase/a.py', self.project_root
            ),
            "raw_response": "wreckage",
            "_meta": {"salvaged_response": True},
        }
        output = core._format_success_output(
            files=result["files"],
            query="where is target",
            rg_patterns=result["rg_patterns"],
            meta=result["_meta"],
            raw_response=result["raw_response"],
            max_turns=3,
            max_results=8,
            max_commands=8,
            timeout_ms=30000,
            exclude_paths=None,
            verbose=False,
            salvaged=result["salvaged"],
        )
        self.assertIn(
            "Salvaged from malformed remote response (low confidence):", output
        )
        self.assertIn("a.py", output)
        self.assertNotIn("Start here:", output)

    def test_empty_answer_with_evidence_is_salvaged(self) -> None:
        answer = (
            '[TOOL_CALLS]answer[ARGS]{"answer": "I checked /codebase/a.py '
            'and searched pattern target."}'
        )
        result = self._search_once([self._frame(answer)])
        self.assertEqual(result["files"], [])
        self.assertEqual(
            [f["path"] for f in result["salvaged"]["files"]], ["a.py"]
        )
        self.assertTrue(result["_meta"]["salvaged_response"])


if __name__ == "__main__":
    unittest.main()
