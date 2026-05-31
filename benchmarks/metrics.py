from __future__ import annotations

import math
import random


def normalize_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def path_matches(file_path: str, target_path: str) -> bool:
    norm_file = normalize_path(file_path)
    norm_target = normalize_path(target_path)
    return norm_file == norm_target or norm_file.endswith(f"/{norm_target}") or norm_target.endswith(f"/{norm_file}")


def dedupe_paths(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for path in paths:
        normalized = normalize_path(path)
        if normalized in seen:
            continue
        seen.add(normalized)
        unique.append(normalized)
    return unique


def dcg(binary_relevances: list[int]) -> float:
    return sum(rel / math.log2(index + 2) for index, rel in enumerate(binary_relevances))


def metrics_for_ranking(
    ranked_paths: list[str],
    relevant_paths: list[str],
    *,
    k: int = 10,
) -> dict[str, float]:
    ranked = dedupe_paths(ranked_paths)[:k]
    relevant = dedupe_paths(relevant_paths)
    if not relevant:
        return {"ndcg10": 0.0, "recall10": 0.0, "top1": 0.0, "mrr": 0.0}

    binary: list[int] = []
    matched_targets: set[str] = set()
    first_rank: int | None = None
    for index, candidate in enumerate(ranked, 1):
        is_relevant = False
        for target in relevant:
            if path_matches(candidate, target):
                is_relevant = True
                matched_targets.add(target)
        binary.append(1 if is_relevant else 0)
        if is_relevant and first_rank is None:
            first_rank = index

    ideal = dcg([1] * min(k, len(relevant)))
    ndcg10 = dcg(binary) / ideal if ideal else 0.0
    recall10 = len(matched_targets) / len(relevant)
    top1 = 1.0 if binary and binary[0] else 0.0
    mrr = 1.0 / first_rank if first_rank else 0.0
    return {
        "ndcg10": ndcg10,
        "recall10": recall10,
        "top1": top1,
        "mrr": mrr,
    }


def bootstrap_mean_ci(
    values: list[float],
    *,
    samples: int = 5000,
    seed: int = 0,
) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    rng = random.Random(seed)
    size = len(values)
    means = []
    for _ in range(samples):
        draw = [values[rng.randrange(size)] for _ in range(size)]
        means.append(sum(draw) / size)
    means.sort()
    low_index = max(0, int(samples * 0.025) - 1)
    high_index = min(samples - 1, int(samples * 0.975))
    return means[low_index], means[high_index]


def bootstrap_paired_delta_ci(
    left: list[float],
    right: list[float],
    *,
    samples: int = 5000,
    seed: int = 0,
) -> tuple[float, float, float]:
    if len(left) != len(right):
        raise ValueError("paired bootstrap requires equal-length inputs")
    if not left:
        return 0.0, 0.0, 0.0
    diffs = [lhs - rhs for lhs, rhs in zip(left, right)]
    mean_delta = sum(diffs) / len(diffs)
    low, high = bootstrap_mean_ci(diffs, samples=samples, seed=seed)
    return mean_delta, low, high
