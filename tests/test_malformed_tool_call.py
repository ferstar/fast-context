from __future__ import annotations

import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import core  # noqa: E402

# Captured from a live remote search (2026-10): the backend model dropped the
# `": ` key/value separators while generating the ARGS JSON. Repairable now —
# parse_json_with_repair turns this back into valid JSON in one pass.
MALFORMED_PAYLOAD = (
    '[TOOL_CALLS]restricted_exec[ARGS]{"command1": {"type": "readfile", '
    '"file": "/codebase/src/homework_print_prep/orient.py", "start_350, "end_450}, '
    '"command2": {"type": "readfile", "file": "/codebase/src/homework_print_prep/pipeline.py", "start_180, "end_250}, '
    '"command3": {"type": "readfile", "file": "/codebase/src/homework_print_prep/stamps.py", "start_1, "end_50}, '
    '"command4": {"type": "rg", "pattern": "line.*score", "path": "/codebase/src/homework_print_prep", "exclude": []}, '
    '"command5": {"type": "readfile", "file": "/codebase/src/homework_print_prep/orient.py", "start_640, "end": 700}, '
    '"command6": {"type": "rg", "pattern": "stamp.*frame", "path": "/codebase/tests", "exclude": ["test_*"]}, '
    '"command7": {"type": "readfile", "file": "/codebase/src/homework_print_prep/stamps_wipe.py", "start_1, "end": 100}, '
    '"command8": {"type": "rg", "pattern": "perspective", "path": "/codebase/tests", "exclude": []}}</s>'
)

# Truncated mid-string: no repair rule can close an unterminated string and
# nothing salvageable (no closed file path, no pattern) survives.
UNRECOVERABLE_PAYLOAD = (
    '[TOOL_CALLS]restricted_exec[ARGS]{"command1": {"type": "readfile", "file": "/cod'
)

ANSWER_PAYLOAD = (
    '[TOOL_CALLS]answer[ARGS]{"answer": '
    '"<file path=\\"/codebase/a.py\\"><range>1-2</range></file>"}'
)


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _frame(text: str) -> bytes:
    """Wrap text in one Connect frame carrying a protobuf string field."""
    body = text.encode("utf-8")
    payload = b"\x1a" + _varint(len(body)) + body  # field 3, wire type 2
    return b"\x00" + struct.pack(">I", len(payload)) + payload


class ParseToolCallTest(unittest.TestCase):
    def test_wellformed_tool_call(self) -> None:
        parsed = core._parse_tool_call(
            'thinking\n[TOOL_CALLS]restricted_exec[ARGS]'
            '{"command1": {"type": "rg", "pattern": "x", "path": "/codebase"}}'
        )
        self.assertIsNotNone(parsed)
        self.assertNotEqual(parsed, core._MALFORMED)
        thinking, name, args = parsed
        self.assertEqual(name, "restricted_exec")
        self.assertEqual(args["command1"]["pattern"], "x")

    def test_missing_marker_is_final_answer(self) -> None:
        self.assertIsNone(core._parse_tool_call("<ANSWER>done</ANSWER>"))

    def test_captured_malformed_payload_repairs_in_place(self) -> None:
        parsed = core._parse_tool_call(MALFORMED_PAYLOAD)
        self.assertNotEqual(parsed, core._MALFORMED)
        self.assertIsNotNone(parsed)
        thinking, name, args = parsed
        self.assertEqual(name, "restricted_exec")
        self.assertEqual(len(args), 8)
        self.assertEqual(args["command1"]["file"], "/codebase/src/homework_print_prep/orient.py")
        self.assertEqual(args["command1"]["start"], 350)
        self.assertEqual(args["command1"]["end"], 450)
        self.assertEqual(args["command4"]["pattern"], "line.*score")

    def test_truncated_payload_is_flagged_malformed(self) -> None:
        self.assertEqual(core._parse_tool_call(UNRECOVERABLE_PAYLOAD), core._MALFORMED)

    def test_parse_response_reports_malformed_flag(self) -> None:
        text, tool_info, malformed = core._parse_response(_frame(UNRECOVERABLE_PAYLOAD))
        self.assertIsNone(tool_info)
        self.assertTrue(malformed)
        self.assertIn("[TOOL_CALLS]", text)


class MalformedFeedbackLoopTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_root = self.temp_dir.name
        (Path(self.project_root) / "a.py").write_text(
            "def target():\n    pass\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _search_once(self, max_turns: int = 4) -> dict:
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

    def test_malformed_turn_is_retried_then_answer_parsed(self) -> None:
        responses = [_frame(UNRECOVERABLE_PAYLOAD), _frame(ANSWER_PAYLOAD)]
        with patch("core._streaming_request", side_effect=responses) as mock_req:
            result = self._search_once()
        self.assertEqual(mock_req.call_count, 2)
        self.assertEqual([f["path"] for f in result["files"]], ["a.py"])

    def test_feedback_messages_injected(self) -> None:
        captured: list[list[dict]] = []
        real_build = core._build_request

        def spy(api_key, jwt, messages, tool_defs, model):
            captured.append(list(messages))
            return real_build(api_key, jwt, messages, tool_defs, model)

        responses = [_frame(UNRECOVERABLE_PAYLOAD), _frame(ANSWER_PAYLOAD)]
        with patch("core._build_request", side_effect=spy), \
             patch("core._streaming_request", side_effect=responses):
            self._search_once()

        self.assertEqual(len(captured), 2)
        second = captured[1]
        self.assertEqual(second[-1], {"role": 1, "content": core.MALFORMED_TOOL_CALL_HINT})
        self.assertEqual(second[-2]["role"], 2)
        self.assertIn("[TOOL_CALLS]", second[-2]["content"])

    def test_persistent_malformed_output_ends_search_with_meta(self) -> None:
        responses = [_frame(UNRECOVERABLE_PAYLOAD)] * 5
        with patch("core._streaming_request", side_effect=responses) as mock_req:
            result = self._search_once(max_turns=4)
        self.assertEqual(result["files"], [])
        self.assertIn("raw_response", result)
        self.assertEqual(
            result["_meta"]["malformed_tool_calls"],
            core.MAX_MALFORMED_FEEDBACK + 1,
        )
        self.assertEqual(mock_req.call_count, core.MAX_MALFORMED_FEEDBACK + 1)


class ExecutorGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.executor = core.ToolExecutor(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_non_dict_command_rejected(self) -> None:
        self.assertEqual(
            self.executor.exec_command("junk"),
            "Error: missing or invalid command",
        )

    def test_missing_or_wrong_typed_fields_rejected(self) -> None:
        self.assertEqual(
            self.executor.exec_command({"type": "rg", "pattern": "x"}),
            "Error: missing or invalid path",
        )
        self.assertEqual(
            self.executor.exec_command({"type": "readfile"}),
            "Error: missing or invalid file path",
        )
        self.assertEqual(
            self.executor.exec_command({"type": "tree", "path": 42}),
            "Error: missing or invalid path",
        )
        self.assertEqual(
            self.executor.exec_command({"type": "glob", "path": "/codebase"}),
            "Error: missing or invalid pattern",
        )

    def test_wrong_typed_line_range_rejected(self) -> None:
        (Path(self.temp_dir.name) / "a.py").write_text(
            "def target():\n    pass\n", encoding="utf-8"
        )
        self.assertEqual(
            self.executor.exec_command(
                {"type": "readfile", "file": "a.py", "start_line": "350"}
            ),
            "Error: missing or invalid line range",
        )
        out = self.executor.exec_tool_call_async(
            {"command1": {"type": "readfile", "file": "a.py", "end_line": "450"}}
        )
        self.assertIn("missing or invalid line range", out)
        self.assertIn(
            "1:",
            self.executor.exec_command(
                {"type": "readfile", "file": "a.py", "start_line": 1, "end_line": 2}
            ),
        )

    def test_non_dict_command_gets_error_result(self) -> None:
        out_async = self.executor.exec_tool_call_async({"command1": "junk"})
        self.assertIn("missing or invalid command", out_async)
        out_sync = self.executor.exec_tool_call({"command1": None})
        self.assertIn("missing or invalid command", out_sync)

    def test_unknown_command_type_still_reported(self) -> None:
        self.assertEqual(
            self.executor.exec_command({"type": "bogus"}),
            "Error: unknown command type 'bogus'",
        )


if __name__ == "__main__":
    unittest.main()
