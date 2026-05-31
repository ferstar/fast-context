from __future__ import annotations

import unittest

from benchmarks.metrics import bootstrap_paired_delta_ci, metrics_for_ranking, path_matches


class BenchmarkMetricsTest(unittest.TestCase):
    def test_path_matches_suffix_paths(self) -> None:
        self.assertTrue(path_matches("dependencies/utils.py", "fastapi/dependencies/utils.py"))
        self.assertTrue(path_matches("lib/core/Axios.js", "axios/lib/core/Axios.js"))
        self.assertFalse(path_matches("lib/helpers.js", "lib/core/Axios.js"))

    def test_metrics_for_ranking_scores_file_hits(self) -> None:
        metrics = metrics_for_ranking(
            ranked_paths=[
                "dependencies/utils.py",
                "routing.py",
                "params.py",
            ],
            relevant_paths=[
                "fastapi/dependencies/utils.py",
                "fastapi/params.py",
            ],
            k=10,
        )

        self.assertAlmostEqual(metrics["recall10"], 1.0)
        self.assertAlmostEqual(metrics["top1"], 1.0)
        self.assertAlmostEqual(metrics["mrr"], 1.0)
        self.assertGreater(metrics["ndcg10"], 0.9)

    def test_bootstrap_paired_delta_ci_is_deterministic(self) -> None:
        mean_delta, low, high = bootstrap_paired_delta_ci(
            [0.9, 0.8, 0.7],
            [0.6, 0.5, 0.4],
            samples=200,
            seed=7,
        )

        self.assertAlmostEqual(mean_delta, 0.3)
        self.assertLessEqual(low, mean_delta)
        self.assertGreaterEqual(high, mean_delta)


if __name__ == "__main__":
    unittest.main()
