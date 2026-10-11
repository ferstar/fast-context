from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import core  # noqa: E402
import decision_layer  # noqa: E402
import fast_context_cli  # noqa: E402


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")


def _fixture(root: Path) -> None:
    _write(
        root,
        "src/limit.ts",
        """
        export function resolveOutputTokenLimit(): number {
          return 8192
        }
        """,
    )
    _write(
        root,
        "src/routes/chat.ts",
        """
        export function postChat(user: User) {
          if (isMemberQuotaRequired(user)) {
            const limit = resolveOutputTokenLimit()
            return limit
          }
          return 0
        }
        """,
    )
    _write(
        root,
        "src/quota.ts",
        """
        export function isMemberQuotaRequired(user: User): boolean {
          return Boolean(user)
        }
        """,
    )


class SymbolExtractionTest(unittest.TestCase):
    def test_python_typescript_and_go_definitions(self) -> None:
        py_names = decision_layer.extract_defined_symbols(
            "def resolve_output_token_limit():\n"
            "    return 1\n"
            "class Quota:\n"
            "    pass\n"
            "MAX_OUTPUT = 8192\n"
            "def _private():\n"
            "    return 0\n",
            ".py",
        )
        self.assertEqual(py_names[:3], ["resolve_output_token_limit", "Quota", "MAX_OUTPUT"])
        self.assertNotIn("_private", py_names)

        ts_names = decision_layer.extract_defined_symbols(
            "export function resolveOutputTokenLimit(): number {\n"
            "  return 8192\n"
            "}\n"
            "export const isMemberQuotaRequired = () => true\n"
            "function localOnly() { return 1 }\n",
            ".ts",
        )
        self.assertEqual(ts_names[0], "resolveOutputTokenLimit")
        self.assertIn("isMemberQuotaRequired", ts_names)
        self.assertLess(ts_names.index("isMemberQuotaRequired"), ts_names.index("localOnly"))

        go_names = decision_layer.extract_defined_symbols(
            "func (s *Server) ResolveLimit() int { return 1 }\n"
            "func unexported() {}\n"
            "func helperOne() {}\n"
            "func helperTwo() {}\n"
            "type Quota struct {}\n"
            "const MaxOutput = 8192\n",
            ".go",
        )
        self.assertEqual(go_names[0], "ResolveLimit")
        self.assertEqual(go_names[1], "Quota")
        self.assertIn("MaxOutput", go_names)
        self.assertNotIn("unexported", go_names)


class DecisionLayerBehaviorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        _fixture(self.root)
        self.hits = [{"path": "src/limit.ts", "start": 1, "end": 3}]

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _assert_guard(self, report: decision_layer.DecisionLayerReport) -> None:
        self.assertIn(decision_layer.SECTION_HEADING, report.text)
        self.assertIn("src/routes/chat.ts:3", report.text.replace("\\", "/"))
        self.assertIn("calls resolveOutputTokenLimit", report.text)
        self.assertIn("isMemberQuotaRequired defined at", report.text)
        self.assertIn("src/quota.ts:1", report.text.replace("\\", "/"))
        self.assertEqual(report.guard_symbols, ["isMemberQuotaRequired"])
        self.assertLessEqual(len(report.entries), decision_layer.DEFAULT_MAX_ENTRIES)

    def test_python_fallback_finds_guard_definition(self) -> None:
        with patch("decision_layer._rg_binary", return_value=None):
            report = decision_layer.build_decision_layer(str(self.root), self.hits)
        self._assert_guard(report)
        self.assertTrue(report.ran)

    def test_rg_command_uses_explicit_path_and_fixed_strings(self) -> None:
        def fake_run(args, **kwargs):
            command = list(args)
            root = command[-1]
            if "-w" in command:
                symbol = command[command.index("-w") + 1]
                stdout = f"{root}/src/routes/chat.ts:3:    const limit = {symbol}()\n"
            else:
                stdout = (
                    f"{root}/src/quota.ts:1:"
                    "export function isMemberQuotaRequired(user: User): boolean {\n"
                )
            return subprocess.CompletedProcess(command, 0, stdout, "")

        with (
            patch("decision_layer._rg_binary", return_value="rg"),
            patch("decision_layer.subprocess.run", side_effect=fake_run) as run,
        ):
            report = decision_layer.build_decision_layer(str(self.root), self.hits)

        self._assert_guard(report)
        command = run.call_args_list[0].args[0]
        self.assertEqual(command[-1], str(self.root.resolve()))
        self.assertIn("-F", command)
        self.assertIn("-w", command)
        self.assertIn("--max-count", command)
        self.assertIn("resolveOutputTokenLimit", command)
        self.assertGreaterEqual(run.call_count, 1)

    def test_disabled_env_skips_work(self) -> None:
        with (
            patch.dict(os.environ, {"FAST_CONTEXT_DECISION_LAYER": "0"}),
            patch("decision_layer.subprocess.run") as run,
        ):
            report = decision_layer.build_decision_layer(str(self.root), self.hits)
        self.assertEqual(report.text, "")
        self.assertFalse(report.ran)
        run.assert_not_called()

    def test_failures_return_an_empty_section(self) -> None:
        with patch("decision_layer._expand", side_effect=RuntimeError("boom")):
            report = decision_layer.build_decision_layer(str(self.root), self.hits)
        self.assertEqual(report.text, "")
        self.assertEqual(report.entries, [])
        self.assertTrue(report.ran)

        missing = decision_layer.build_decision_layer(
            str(self.root / "missing"),
            [{"path": "src/limit.ts", "start": 1, "end": 3}],
        )
        self.assertEqual(missing.text, "")

    def test_entry_cap(self) -> None:
        for index in range(8):
            _write(
                self.root,
                f"src/callers/c{index}.ts",
                f"""
                export function caller{index}() {{
                  if (guard{index}()) {{
                    return resolveOutputTokenLimit()
                  }}
                }}
                """,
            )
            _write(
                self.root,
                f"src/guards/g{index}.ts",
                f"""
                export function guard{index}(): boolean {{
                  return true
                }}
                """,
            )
        with patch("decision_layer._rg_binary", return_value=None):
            report = decision_layer.build_decision_layer(str(self.root), self.hits, max_entries=6)
        self.assertEqual(len(report.entries), 6)
        bullets = [line for line in report.text.splitlines() if line.startswith("- ")]
        self.assertEqual(len(bullets), 6)

    def test_zero_timeout_does_not_raise(self) -> None:
        report = decision_layer.build_decision_layer(str(self.root), self.hits, timeout_s=0)
        self.assertEqual(report.text, "")
        self.assertTrue(report.ran)


class DecisionLayerOutputTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        _fixture(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _remote_result(self) -> dict:
        return {
            "files": [
                {
                    "path": "src/limit.ts",
                    "full_path": str(self.root / "src" / "limit.ts"),
                    "ranges": [(1, 3)],
                }
            ],
            "rg_patterns": ["resolveOutputTokenLimit"],
            "_meta": {"tree_depth": 2, "tree_size_kb": 0.1, "timing_ms": {"remote": 12}},
        }

    @patch("core.search")
    def test_search_appends_guard_section_and_followup(self, mock_search) -> None:
        mock_search.return_value = self._remote_result()
        with patch("decision_layer._rg_binary", return_value=None):
            result = core.search_with_content(
                query="where is default max output tokens 8192 set and what decides whether it applies",
                project_root=str(self.root),
                backend="remote",
            )
        self.assertIn("Callers / guard sites (low confidence, lexical):", result)
        self.assertIn("isMemberQuotaRequired defined at", result)
        self.assertIn("Follow-up search terms:", result)
        followup = result.split("Follow-up search terms:", 1)[1].splitlines()[1]
        self.assertIn("resolveOutputTokenLimit", followup)
        self.assertIn("isMemberQuotaRequired", followup)

    @patch("core.search")
    def test_flag_and_env_disable_section(self, mock_search) -> None:
        mock_search.return_value = self._remote_result()
        disabled = core.search_with_content(
            query="where is the token limit",
            project_root=str(self.root),
            backend="remote",
            decision_layer=False,
        )
        self.assertNotIn("Callers / guard sites", disabled)
        self.assertNotIn("isMemberQuotaRequired", disabled)

        with patch.dict(os.environ, {"FAST_CONTEXT_DECISION_LAYER": "0"}):
            from_env = core.search_with_content(
                query="where is the token limit",
                project_root=str(self.root),
                backend="remote",
            )
        self.assertNotIn("Callers / guard sites", from_env)

    @patch("core.search")
    def test_verbose_timing_includes_remote_and_decision_layer(self, mock_search) -> None:
        mock_search.return_value = self._remote_result()
        with patch("decision_layer._rg_binary", return_value=None):
            result = core.search_with_content(
                query="where is the token limit",
                project_root=str(self.root),
                backend="remote",
                verbose=True,
            )
        self.assertIn("[timing]", result)
        self.assertIn("remote=12ms", result)
        self.assertIn("decision_layer=", result)

    @patch("core.semble_search")
    def test_local_search_includes_guard_section(self, mock_search) -> None:
        limit = (self.root / "src" / "limit.ts").read_text(encoding="utf-8")
        mock_search.return_value = {
            "query": "tokens",
            "results": [
                {
                    "chunk": {
                        "content": limit,
                        "file_path": "src/limit.ts",
                        "start_line": 1,
                        "end_line": 3,
                    },
                    "score": 0.8,
                }
            ],
            "_meta": {"backend": "semble", "runner": "library"},
        }
        with patch("decision_layer._rg_binary", return_value=None):
            result = core.local_search_with_content(
                query="where is the output token limit",
                project_root=str(self.root),
                max_results=3,
            )
        self.assertIn("Local Semble results:", result)
        self.assertIn("Callers / guard sites (low confidence, lexical):", result)
        self.assertIn("isMemberQuotaRequired", result)
        self.assertEqual(mock_search.call_args.kwargs["top_k"], 6)

    def test_cli_keeps_old_flags_and_adds_opt_out(self) -> None:
        parser = fast_context_cli._build_parser()
        args = parser.parse_args(
            [
                "search",
                "--query",
                "q",
                "--backend",
                "remote",
                "--max-results",
                "4",
                "--tree-depth",
                "2",
                "--exclude",
                "vendor",
            ]
        )
        self.assertEqual(args.backend, "remote")
        self.assertEqual(args.max_results, 4)
        self.assertEqual(args.exclude, ["vendor"])
        self.assertFalse(args.no_decision_layer)

        disabled = parser.parse_args(["local-search", "--query", "q", "--no-decision-layer"])
        self.assertTrue(disabled.no_decision_layer)
        with patch("fast_context_cli.local_search_with_content", return_value="ok") as mocked:
            code = fast_context_cli.run_local_search(disabled)
        self.assertEqual(code, 0)
        self.assertFalse(mocked.call_args.kwargs["decision_layer"])


class DecisionLayerNoiseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        _write(
            self.root,
            "src/limit.ts",
            """
            export function resolveOutputTokenLimit(): number {
              return 8192
            }
            """,
        )
        self.hits = [{"path": "src/limit.ts", "start": 1, "end": 3}]

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _report(self) -> decision_layer.DecisionLayerReport:
        with patch("decision_layer._rg_binary", return_value=None):
            return decision_layer.build_decision_layer(str(self.root), self.hits)

    def test_guard_names_skip_methods_and_builtins(self) -> None:
        lines = [
            "if (user.values() && items.flatMap(xs) && flatMap(xs) && keys() && isinstance(flag, str) && len(flag) && isMemberQuotaRequired(user)) {",
            "  return resolveOutputTokenLimit()",
        ]
        names = decision_layer._guard_names(lines, 2, "resolveOutputTokenLimit")
        self.assertEqual(names, ["isMemberQuotaRequired"])

    def test_method_and_builtin_guards_are_omitted(self) -> None:
        _write(
            self.root,
            "apps/desktop/src/route.ts",
            """
            export function post(user: User, items: Item[]) {
              if (user.customQuotaGate() && flatMap(items) && isMemberQuotaRequired(user)) {
                return resolveOutputTokenLimit()
              }
              return 0
            }
            """,
        )
        _write(
            self.root,
            "apps/desktop/src/quota.ts",
            """
            export function isMemberQuotaRequired(user: User): boolean {
              return true
            }
            export function customQuotaGate(): boolean {
              return true
            }
            export function flatMap<T>(items: T[]): T[] {
              return items
            }
            """,
        )
        _write(
            self.root,
            "benchmarks/helpers.py",
            """
            def values():
                return 1
            def flatMap():
                return []
            """,
        )
        report = self._report()
        self.assertEqual(report.guard_symbols, ["isMemberQuotaRequired"])
        self.assertEqual(len(report.entries), 1)
        self.assertEqual(report.entries[0].guard_path, "apps/desktop/src/quota.ts")
        self.assertNotIn("customQuotaGate", report.text)
        self.assertNotIn("flatMap", report.text)
        self.assertNotIn("benchmarks", report.text.replace("\\", "/"))

    def test_definition_prefers_same_app_over_tests_benchmarks_and_other_apps(self) -> None:
        _write(
            self.root,
            "apps/desktop/src/route.ts",
            """
            export function post(user: User) {
              if (isMemberQuotaRequired(user)) {
                return resolveOutputTokenLimit()
              }
              return 0
            }
            """,
        )
        _write(
            self.root,
            "benchmarks/quota.py",
            """
            def isMemberQuotaRequired(user):
                return True
            """,
        )
        _write(
            self.root,
            "apps/desktop/src/quota.test.ts",
            """
            export function isMemberQuotaRequired(user: User): boolean {
              return false
            }
            """,
        )
        _write(
            self.root,
            "apps/web/src/quota.ts",
            """
            export function isMemberQuotaRequired(user: User): boolean {
              return false
            }
            """,
        )
        _write(
            self.root,
            "packages/shared/quota.ts",
            """
            export function isMemberQuotaRequired(user: User): boolean {
              return false
            }
            """,
        )
        _write(
            self.root,
            "apps/desktop/src/quota.ts",
            """
            export function isMemberQuotaRequired(user: User): boolean {
              return true
            }
            """,
        )
        report = self._report()
        self.assertEqual(report.entries[0].guard_path, "apps/desktop/src/quota.ts")
        self.assertNotIn("quota.test.ts", report.text)
        self.assertNotIn("benchmarks", report.text.replace("\\", "/"))
        self.assertNotIn("apps/web", report.text.replace("\\", "/"))
        self.assertNotIn("packages/shared", report.text.replace("\\", "/"))

    def test_shared_package_beats_other_app_and_foreign_language(self) -> None:
        _write(
            self.root,
            "apps/desktop/src/route.ts",
            """
            export function post(user: User) {
              if (isMemberQuotaRequired(user)) {
                return resolveOutputTokenLimit()
              }
              return 0
            }
            """,
        )
        _write(
            self.root,
            "benchmarks/quota.py",
            """
            def isMemberQuotaRequired(user):
                return True
            """,
        )
        _write(
            self.root,
            "apps/desktop/src/quota.test.ts",
            """
            export function isMemberQuotaRequired(user: User): boolean {
              return false
            }
            """,
        )
        _write(
            self.root,
            "services/api/quota.go",
            """
            func isMemberQuotaRequired() bool {
                return true
            }
            """,
        )
        _write(
            self.root,
            "apps/web/src/quota.ts",
            """
            export function isMemberQuotaRequired(user: User): boolean {
              return false
            }
            """,
        )
        _write(
            self.root,
            "packages/shared/quota.ts",
            """
            export function isMemberQuotaRequired(user: User): boolean {
              return true
            }
            """,
        )
        report = self._report()
        self.assertEqual(report.entries[0].guard_path, "packages/shared/quota.ts")

    def test_cross_language_definition_is_not_accepted(self) -> None:
        _write(
            self.root,
            "apps/desktop/src/route.ts",
            """
            export function post(user: User) {
              if (isMemberQuotaRequired(user)) {
                return resolveOutputTokenLimit()
              }
              return 0
            }
            """,
        )
        _write(
            self.root,
            "lib/quota.py",
            """
            def isMemberQuotaRequired(user):
                return True
            """,
        )
        report = self._report()
        self.assertEqual(len(report.entries), 1)
        self.assertIsNone(report.entries[0].guard_path)
        self.assertIn("(definition not resolved)", report.text)
        self.assertNotIn("quota.py", report.text)

    def test_unresolved_guard_dropped_when_another_resolves(self) -> None:
        _write(
            self.root,
            "src/routes/chat.ts",
            """
            export function postChat(user: User) {
              if (isMemberQuotaRequired(user) && missingDecisionFlag()) {
                return resolveOutputTokenLimit()
              }
              return 0
            }
            """,
        )
        _write(
            self.root,
            "src/quota.ts",
            """
            export function isMemberQuotaRequired(user: User): boolean {
              return true
            }
            """,
        )
        report = self._report()
        self.assertEqual(report.guard_symbols, ["isMemberQuotaRequired"])
        self.assertTrue(all(entry.guard_path for entry in report.entries))
        self.assertNotIn("missingDecisionFlag", report.text)
        self.assertNotIn("definition not resolved", report.text)

    def test_unresolved_guard_kept_when_nothing_resolves(self) -> None:
        _write(
            self.root,
            "src/routes/chat.ts",
            """
            export function postChat(user: User) {
              if (missingDecisionFlag()) {
                return resolveOutputTokenLimit()
              }
              return 0
            }
            """,
        )
        report = self._report()
        self.assertEqual(len(report.entries), 1)
        self.assertEqual(report.entries[0].guard_name, "missingDecisionFlag")
        self.assertIsNone(report.entries[0].guard_path)
        self.assertIn("(definition not resolved)", report.text)

    def test_cross_file_guard_ranks_before_same_file_guard(self) -> None:
        _write(
            self.root,
            "src/routes/same.ts",
            """
            export function localGuard(): boolean {
              return true
            }

            export function callSame() {
              if (localGuard()) {
                return resolveOutputTokenLimit()
              }
              return 0
            }
            """,
        )
        _write(
            self.root,
            "src/routes/other.ts",
            """
            export function callOther() {
              if (remoteGuard()) {
                return resolveOutputTokenLimit()
              }
              return 0
            }
            """,
        )
        _write(
            self.root,
            "src/guards/remote.ts",
            """
            export function remoteGuard(): boolean {
              return true
            }
            """,
        )
        report = self._report()
        self.assertEqual([entry.guard_name for entry in report.entries], ["remoteGuard", "localGuard"])
        self.assertNotEqual(report.entries[0].guard_path, report.entries[0].caller_path)
        self.assertEqual(report.entries[1].guard_path, report.entries[1].caller_path)
        remote_at = report.text.index("remoteGuard")
        local_at = report.text.index("localGuard")
        self.assertLess(remote_at, local_at)


def _comment_only(lines: int = 16) -> str:
    body = ["/**", " * default max output tokens"]
    body.extend([" *"] * (lines - 3))
    body.append(" */")
    return "\n".join(body)


class DecisionLayerFileFallbackTest(unittest.TestCase):
    def test_fallback_keeps_the_first_exported_names(self) -> None:
        exports = "\n".join(
            f"export function exported{index}() {{ return {index} }}" for index in range(1, 10)
        )
        text = "function localHelper() { return 0 }\n" + exports
        names = decision_layer.extract_fallback_symbols(text, ".ts")
        self.assertEqual(names, [f"exported{index}" for index in range(1, 9)])
        self.assertNotIn("localHelper", names)
        self.assertNotIn("exported9", names)

        python = "def resolve_output_token_limit():\n    return 8192\n"
        self.assertEqual(
            decision_layer.extract_fallback_symbols(python, ".py"),
            ["resolve_output_token_limit"],
        )

    def test_comment_chunk_uses_file_exports_past_duplicate_hits(self) -> None:
        """The reported local-search miss: L1-16 is a doc comment, and duplicate chunks of another file sit above it."""
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root = Path(temp_dir.name)
        limit_text = _comment_only(16) + "\nexport function resolveOutputTokenLimit(): number {\n  return 8192\n}\n"
        _write(root, "src/output-token-limit.ts", limit_text)
        _write(root, "src/decoy.ts", _comment_only(4) + "\n")
        _write(
            root,
            "src/routes/chat.ts",
            """
            export function postChat(user: User) {
              if (isMemberQuotaRequired(user)) {
                const limit = resolveOutputTokenLimit()
                return limit
              }
              return 0
            }
            """,
        )
        _write(
            root,
            "src/quota.ts",
            """
            export function isMemberQuotaRequired(user: User): boolean {
              return true
            }
            """,
        )
        comment = "\n".join(limit_text.splitlines()[:16])
        decoy = (root / "src" / "decoy.ts").read_text(encoding="utf-8")
        hits = [
            {"chunk": {"content": decoy, "file_path": "src/decoy.ts", "start_line": 1, "end_line": 4}, "score": 0.99},
            {"chunk": {"content": decoy, "file_path": "src/decoy.ts", "start_line": 1, "end_line": 4}, "score": 0.98},
            {"chunk": {"content": decoy, "file_path": "src/decoy.ts", "start_line": 1, "end_line": 4}, "score": 0.97},
            {
                "chunk": {
                    "content": comment,
                    "file_path": "src/output-token-limit.ts",
                    "start_line": 1,
                    "end_line": 16,
                },
                "score": 0.9,
            },
        ]
        self.assertNotIn("resolveOutputTokenLimit", comment)
        with patch("decision_layer._rg_binary", return_value=None):
            report = decision_layer.build_decision_layer(str(root), hits)
        self.assertIn(decision_layer.SECTION_HEADING, report.text)
        self.assertIn("calls resolveOutputTokenLimit", report.text)
        self.assertIn("isMemberQuotaRequired defined at", report.text)
        self.assertIn("src/quota.ts", report.text.replace("\\", "/"))
        self.assertEqual(report.guard_symbols, ["isMemberQuotaRequired"])

    def test_fourth_distinct_file_is_not_expanded(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root = Path(temp_dir.name)
        for name in ("a.ts", "b.ts", "c.ts"):
            _write(root, f"src/{name}", _comment_only(4) + "\n")
        _write(
            root,
            "src/output-token-limit.ts",
            _comment_only(16) + "\nexport function resolveOutputTokenLimit(): number {\n  return 8192\n}\n",
        )
        _write(
            root,
            "src/routes/chat.ts",
            """
            export function postChat(user: User) {
              if (isMemberQuotaRequired(user)) {
                return resolveOutputTokenLimit()
              }
              return 0
            }
            """,
        )
        hits = [
            {"path": "src/a.ts", "start": 1, "end": 4},
            {"path": "src/b.ts", "start": 1, "end": 4},
            {"path": "src/c.ts", "start": 1, "end": 4},
            {"path": "src/output-token-limit.ts", "start": 1, "end": 16},
        ]
        with patch("decision_layer._rg_binary", return_value=None):
            report = decision_layer.build_decision_layer(str(root), hits)
        self.assertEqual(report.entries, [])
        self.assertNotIn("resolveOutputTokenLimit", report.text)


if __name__ == "__main__":
    unittest.main()


class SymbolUnionTest(unittest.TestCase):
    def test_range_symbols_come_first_then_file_exports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "limit.ts").write_text(
                textwrap.dedent(
                    """\
                    /** doc */
                    export function resolveLimit(x: number) { return x; }
                    export function advertisedLimit() { return 1; }
                    """
                ),
                encoding="utf-8",
            )
            names = decision_layer._symbols_for_file(root, "limit.ts", [(3, 3, "")])
            self.assertEqual(names[0], "advertisedLimit")
            self.assertIn("resolveLimit", names)
