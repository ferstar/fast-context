from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import local_rank  # noqa: E402


def _hit(path: str, score: float, content: str = "export const value = 1\n") -> dict:
    return {
        "chunk": {
            "file_path": path,
            "content": content,
            "start_line": 1,
            "end_line": 3,
        },
        "score": score,
    }


class LocalRankTest(unittest.TestCase):
    def test_code_query_demotes_noise_and_keeps_it(self) -> None:
        results = [
            _hit("deploy/seed.sql", 0.99),
            _hit("migrations/001_init.sql", 0.97),
            _hit(".env.example", 0.95),
            _hit("pnpm-lock.yaml", 0.93),
            _hit("apps/web/dist/app.min.js", 0.91),
            _hit("apps/web/src/__snapshots__/button.snap", 0.89),
            _hit("src/limit.ts", 0.4),
        ]
        ranked = local_rank.rerank_local_results(
            results,
            "where is the default output token limit set",
            max_results=10,
        )
        self.assertEqual(local_rank.result_path(ranked[0]), "src/limit.ts")
        self.assertEqual(len(ranked), len(results))
        by_path = {local_rank.result_path(item): item["score"] for item in ranked}
        self.assertAlmostEqual(by_path["deploy/seed.sql"], 0.99 * local_rank.NOISE_SCORE_FACTOR)
        self.assertAlmostEqual(by_path["migrations/001_init.sql"], 0.97 * local_rank.NOISE_SCORE_FACTOR)
        self.assertAlmostEqual(by_path[".env.example"], 0.95 * local_rank.NOISE_SCORE_FACTOR)
        self.assertAlmostEqual(by_path["pnpm-lock.yaml"], 0.93 * local_rank.NOISE_SCORE_FACTOR)
        self.assertLess(by_path["apps/web/dist/app.min.js"], 0.91)
        self.assertLess(by_path["apps/web/src/__snapshots__/button.snap"], 0.89)
        self.assertAlmostEqual(by_path["src/limit.ts"], 0.4)

    def test_each_noise_shape_is_demoted_once(self) -> None:
        samples = [
            "db/schema.sql",
            "services/api/migrations/002.sql",
            "deploy/helm/values.yaml",
            ".env",
            ".env.local",
            "config/app.yaml.example",
            "package-lock.json",
            "yarn.lock",
            "uv.lock",
            "Cargo.lock",
            "poetry.lock",
            "assets/vendor.min.js",
            "packages/web/generated/types.ts",
            "apps/web/build/index.js",
            "src/comp.snap",
        ]
        ranked = local_rank.rerank_local_results(
            [_hit(path, 1.0) for path in samples] + [_hit("src/real.ts", 0.3)],
            "where is the handler implemented",
            content_types=["code"],
        )
        self.assertEqual(local_rank.result_path(ranked[0]), "src/real.ts")
        for item in ranked[1:]:
            self.assertLess(item["score"], 0.5)

    def test_query_terms_skip_matching_noise(self) -> None:
        results = [
            _hit("migrations/001_init.sql", 0.8),
            _hit("deploy/job.sql", 0.7),
            _hit(".env.example", 0.6),
            _hit("uv.lock", 0.5),
            _hit("src/app.ts", 0.4),
        ]
        ranked = local_rank.rerank_local_results(
            results,
            "apply the SQL migration from deploy and update the env lockfile",
        )
        scores = {local_rank.result_path(item): item["score"] for item in ranked}
        self.assertAlmostEqual(scores["migrations/001_init.sql"], 0.8)
        self.assertAlmostEqual(scores["deploy/job.sql"], 0.7)
        self.assertAlmostEqual(scores[".env.example"], 0.6)
        self.assertAlmostEqual(scores["uv.lock"], 0.5)

    def test_environment_word_does_not_exempt_env_files(self) -> None:
        ranked = local_rank.rerank_local_results(
            [_hit(".env.local", 1.0), _hit("src/app.ts", 0.3)],
            "where is the runtime environment prepared",
        )
        self.assertEqual(local_rank.result_path(ranked[0]), "src/app.ts")
        self.assertAlmostEqual(ranked[1]["score"], local_rank.NOISE_SCORE_FACTOR)

    def test_docs_and_all_content_keep_noise_scores(self) -> None:
        results = [_hit("deploy/seed.sql", 0.9), _hit("src/app.ts", 0.2)]
        for content in (["docs"], ["config"], ["all"]):
            ranked = local_rank.rerank_local_results(
                results,
                "where is the schema documented",
                content_types=content,
            )
            self.assertEqual(local_rank.result_path(ranked[0]), "deploy/seed.sql", content)
            self.assertAlmostEqual(ranked[0]["score"], 0.9)

    def test_query_prefers_named_app_and_same_basename(self) -> None:
        results = [
            _hit("apps/web/src/Button.tsx", 0.95),
            _hit("apps/web/src/Header.tsx", 0.9),
            _hit("apps/web/src/nav.tsx", 0.85),
            _hit("apps/desktop/src/Button.tsx", 0.3),
        ]
        ranked = local_rank.rerank_local_results(
            results,
            "where is the desktop button click handler",
        )
        paths = [local_rank.result_path(item) for item in ranked]
        self.assertLess(
            paths.index("apps/desktop/src/Button.tsx"),
            paths.index("apps/web/src/Button.tsx"),
        )
        desktop = next(item for item in ranked if item["chunk"]["file_path"].endswith("desktop/src/Button.tsx"))
        web = next(item for item in ranked if item["chunk"]["file_path"].endswith("web/src/Button.tsx"))
        self.assertGreater(desktop["score"], web["score"])

    def test_webhook_does_not_select_web_app(self) -> None:
        results = [
            _hit("apps/web/src/hook.ts", 0.4),
            _hit("apps/api/src/hook.ts", 0.9),
        ]
        ranked = local_rank.rerank_local_results(results, "where is the webhook signature checked")
        self.assertEqual(local_rank.result_path(ranked[0]), "apps/api/src/hook.ts")
        self.assertAlmostEqual(ranked[0]["score"], 0.9)

    def test_majority_of_top_results_selects_app(self) -> None:
        results = [
            _hit("apps/desktop/src/a.ts", 0.4),
            _hit("apps/desktop/src/b.ts", 0.39),
            _hit("apps/desktop/src/c.ts", 0.38),
            _hit("apps/web/src/a.ts", 0.99),
        ]
        ranked = local_rank.rerank_local_results(results, "where is the handler registered")
        paths = [local_rank.result_path(item) for item in ranked]
        self.assertLess(paths.index("apps/desktop/src/a.ts"), paths.index("apps/web/src/a.ts"))

    def test_hybrid_anchors_override_result_majority(self) -> None:
        results = [
            _hit("apps/web/src/Button.tsx", 0.95),
            _hit("apps/web/src/Home.tsx", 0.9),
            _hit("apps/web/src/nav.tsx", 0.8),
            _hit("apps/desktop/src/Button.tsx", 0.3),
        ]
        ranked = local_rank.rerank_local_results(
            results,
            "where is the button component rendered",
            anchor_paths=[
                "apps/desktop/src/screen.ts",
                "apps/desktop/electron/ipc.ts",
                "apps/desktop/src/Button.tsx",
            ],
        )
        paths = [local_rank.result_path(item) for item in ranked]
        self.assertLess(
            paths.index("apps/desktop/src/Button.tsx"),
            paths.index("apps/web/src/Button.tsx"),
        )

    def test_query_beats_anchor_majority(self) -> None:
        results = [
            _hit("packages/web/src/Panel.tsx", 0.2),
            _hit("packages/desktop/src/Panel.tsx", 0.9),
        ]
        ranked = local_rank.rerank_local_results(
            results,
            "fix the web panel layout",
            anchor_paths=["packages/desktop/src/main.ts", "packages/desktop/src/Panel.tsx"],
        )
        self.assertEqual(local_rank.result_path(ranked[0]), "packages/web/src/Panel.tsx")

    def test_truncation_happens_after_rerank(self) -> None:
        results = [
            _hit("deploy/noise.sql", 0.99),
            _hit("src/limit.ts", 0.4),
        ]
        ranked = local_rank.rerank_local_results(results, "token limit", max_results=1)
        self.assertEqual(len(ranked), 1)
        self.assertEqual(local_rank.result_path(ranked[0]), "src/limit.ts")

    def test_services_root_is_an_app_root(self) -> None:
        self.assertEqual(local_rank.app_root_key("services/billing/src/quota.go"), ("services", "billing"))
        self.assertIsNone(local_rank.app_root_key("src/quota.go"))


if __name__ == "__main__":
    unittest.main()
