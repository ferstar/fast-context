from __future__ import annotations

import json
import math
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Any


BM25_K1 = 1.2
BM25_B = 0.75
RRF_K = 60
PROFILE_CACHE_TTL_SECONDS = int(os.environ.get("FC_PROFILE_CACHE_TTL", "120") or "120")

FIELD_WEIGHTS = {
    "dir_name": 1.0,
    "path_tokens": 4.0,
    "metadata": 3.0,
    "headers": 2.0,
}

STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "must", "shall", "can", "need", "to", "of",
    "in", "for", "on", "with", "at", "by", "from", "as", "into", "through",
    "and", "but", "or", "not", "only", "same", "than", "too", "very", "just",
    "also", "this", "that", "these", "those", "here", "there", "all", "any",
    "some", "each", "every", "other", "another", "such", "get", "set", "use",
    "used", "using", "make", "made", "if", "then", "else", "return", "new",
    "like", "where", "which", "who", "what", "when", "why", "how", "it", "its",
    "we", "you", "your", "repo", "repository", "project", "code", "file", "files",
}

SOURCE_PATH_PATTERNS = ("/src/", "/core/", "/lib/", "/internal/", "/pkg/", "/cmd/", "/app/")
NOISE_PATH_PATTERNS = (
    "/migrations/", "/test/", "/tests/", "/__tests__/", "/fixtures/", "/examples/",
    "/vendor/", "/mock/", "/mocks/", "/i18n/", "/locales/", "/versions/",
)


@dataclass
class DirectoryProfile:
    dir_name: str
    path_tokens: list[str] = field(default_factory=list)
    metadata: str = ""
    headers: list[str] = field(default_factory=list)
    file_count: int = 0
    file_paths: list[str] = field(default_factory=list)
    tokenized: dict[str, list[str]] = field(default_factory=dict)

    @property
    def path_tokens_text(self) -> str:
        return " ".join(self.path_tokens)

    @property
    def headers_text(self) -> str:
        return " ".join(self.headers)


_PROFILE_CACHE: dict[str, tuple[float, DirectoryProfile]] = {}


def _normalize_rel_path(path: str) -> str:
    return path.replace(os.sep, "/").lstrip("./")


def _is_excluded_path(rel_path: str, exclude_paths: list[str]) -> bool:
    normalized = _normalize_rel_path(rel_path)
    base = Path(normalized).name
    if base.startswith(".") and base != ".github":
        return True
    for pattern in exclude_paths:
        token = pattern.replace("\\", "/").lstrip("./").lstrip("!").rstrip("/")
        if not token:
            continue
        if any(ch in token for ch in "*?[]"):
            if fnmatch(normalized, token) or fnmatch(base, token):
                return True
            continue
        if normalized == token or base == token:
            return True
        if normalized.startswith(f"{token}/") or f"/{token}/" in f"/{normalized}/":
            return True
    return False


def _stem(word: str) -> str:
    value = word.lower()
    for suffix, replacement in (
        ("ies", "y"), ("ing", ""), ("edly", ""), ("ly", ""), ("ed", ""),
        ("ation", "ate"), ("tion", "t"), ("ment", ""), ("ness", ""), ("ful", ""),
        ("less", ""), ("able", ""), ("ible", ""), ("ally", "al"), ("ity", ""),
        ("ive", ""),
    ):
        if len(value) > len(suffix) + 2 and value.endswith(suffix):
            return value[: -len(suffix)] + replacement
    if len(value) > 4 and value.endswith("s"):
        return value[:-1]
    return value


def tokenize(text: str, min_len: int = 2) -> list[str]:
    if not text:
        return []
    parts = re.sub(r"[^\w\s\-./\\@]", " ", text.lower()).split()
    tokens: list[str] = []
    for part in parts:
        for token in re.split(r"[\s\-./\\_]+", part):
            if len(token) < min_len or token in STOPWORDS:
                continue
            tokens.append(_stem(token))
    return tokens


def tokenize_path(path: str) -> list[str]:
    return tokenize(path.replace("/", " ").replace("\\", " ").replace(".", " ").replace("_", " "))


def _safe_read_text(path: Path, limit: int = 4000) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(limit)
    except OSError:
        return ""


def _extract_metadata(dir_path: Path) -> str:
    metadata: list[str] = []

    package_json = dir_path / "package.json"
    if package_json.exists():
        try:
            data = json.loads(_safe_read_text(package_json, 80_000))
            metadata.extend(str(data.get(key, "")) for key in ("name", "description") if data.get(key))
            keywords = data.get("keywords") or []
            if isinstance(keywords, list):
                metadata.extend(str(item) for item in keywords)
            dependencies = data.get("dependencies") or {}
            if isinstance(dependencies, dict):
                metadata.extend(dependencies.keys())
        except Exception:
            pass

    for filename in ("go.mod", "Cargo.toml", "pyproject.toml"):
        content = _safe_read_text(dir_path / filename, 20_000)
        if not content:
            continue
        module_match = re.search(r"^\s*module\s+(\S+)", content, re.MULTILINE)
        name_match = re.search(r"^\s*name\s*=\s*[\"']([^\"']+)[\"']", content, re.MULTILINE)
        if module_match:
            metadata.append(module_match.group(1))
        if name_match:
            metadata.append(name_match.group(1))

    return " ".join(metadata)


def _extract_file_headers(path: Path) -> str:
    content = _safe_read_text(path, 2000)
    if not content:
        return ""
    headers: list[str] = []
    headers.extend(re.sub(r"^#+\s+", "", line) for line in re.findall(r"^#+\s+.+$", content, re.MULTILINE))
    for line in content.splitlines()[:10]:
        match = re.match(r"^\s*(?://|#|;|\*)\s*(.+)$", line)
        if match:
            headers.append(match.group(1))
    return " ".join(headers)


def _profile_cache_key(project_root: str, dir_name: str, exclude_paths: list[str]) -> str:
    return f"{project_root}|{dir_name}|{','.join(sorted(exclude_paths))}"


def build_directory_profile(
    project_root: str,
    dir_name: str,
    exclude_paths: list[str],
    max_depth: int = 4,
    max_files: int = 5000,
) -> DirectoryProfile:
    cache_key = _profile_cache_key(project_root, dir_name, exclude_paths)
    cached = _PROFILE_CACHE.get(cache_key)
    if cached and time.time() - cached[0] < PROFILE_CACHE_TTL_SECONDS:
        return cached[1]

    root = Path(project_root)
    start = root / dir_name
    profile = DirectoryProfile(dir_name=dir_name)

    def walk(current: Path, depth: int) -> None:
        if depth > max_depth or profile.file_count >= max_files:
            return
        try:
            entries = sorted(current.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
        except OSError:
            return

        for entry in entries:
            rel_path = _normalize_rel_path(str(entry.relative_to(root)))
            if _is_excluded_path(rel_path, exclude_paths):
                continue
            if entry.name.startswith(".") and entry.name != ".github":
                continue
            if entry.is_dir():
                profile.path_tokens.append(rel_path)
                walk(entry, depth + 1)
                continue
            if not entry.is_file():
                continue
            profile.path_tokens.append(rel_path)
            profile.file_paths.append(rel_path)
            profile.file_count += 1
            if entry.suffix.lower() in {".md", ".mdx", ".ts", ".tsx", ".js", ".jsx", ".py", ".go", ".rs"}:
                header = _extract_file_headers(entry)
                if header:
                    profile.headers.append(header)
            if profile.file_count >= max_files:
                break

    if start.is_dir():
        walk(start, 1)
    profile.metadata = _extract_metadata(start)
    profile.tokenized = {
        "dir_name": tokenize(profile.dir_name),
        "path_tokens": tokenize(profile.path_tokens_text),
        "metadata": tokenize(profile.metadata),
        "headers": tokenize(profile.headers_text),
    }
    _PROFILE_CACHE[cache_key] = (time.time(), profile)
    return profile


def list_top_level_dirs(project_root: str, exclude_paths: list[str], limit: int = 80) -> list[str]:
    try:
        entries = sorted(Path(project_root).iterdir(), key=lambda item: item.name.lower())
    except OSError:
        return []
    dirs: list[str] = []
    for entry in entries:
        rel = _normalize_rel_path(entry.name)
        if entry.is_dir() and not _is_excluded_path(rel, exclude_paths):
            dirs.append(rel)
        if len(dirs) >= limit:
            break
    return dirs


def _compute_idf(documents: list[list[str]]) -> dict[str, float]:
    doc_count = max(1, len(documents))
    term_doc_count: dict[str, int] = {}
    for doc in documents:
        for term in set(doc):
            term_doc_count[term] = term_doc_count.get(term, 0) + 1
    return {
        term: math.log((doc_count - count + 0.5) / (count + 0.5) + 1)
        for term, count in term_doc_count.items()
    }


def _bm25_field_score(query_terms: list[str], field_terms: list[str], avg_len: float, idf: dict[str, float]) -> float:
    if not field_terms:
        return 0.0
    term_freqs: dict[str, int] = {}
    for term in field_terms:
        term_freqs[term] = term_freqs.get(term, 0) + 1
    field_len = max(1, len(field_terms))
    avg_len = max(1.0, avg_len)
    score = 0.0
    for term in query_terms:
        tf = term_freqs.get(term, 0)
        if not tf:
            continue
        numerator = tf * (BM25_K1 + 1)
        denominator = tf + BM25_K1 * (1 - BM25_B + BM25_B * (field_len / avg_len))
        score += (idf.get(term) or math.log(2)) * (numerator / denominator)
    return score


def _bm25f_score(
    query_terms: list[str],
    profile: DirectoryProfile,
    avg_field_lens: dict[str, float],
    idf: dict[str, float],
) -> float:
    total = 0.0
    for field_name, weight in FIELD_WEIGHTS.items():
        total += weight * _bm25_field_score(
            query_terms,
            profile.tokenized.get(field_name, []),
            avg_field_lens.get(field_name, 10.0),
            idf,
        )
    return total


def _probe_grep(project_root: str, top_dirs: list[str], terms: list[str], exclude_paths: list[str]) -> dict[str, int]:
    if not terms:
        return {}
    pattern = "|".join(re.escape(term) for term in terms[:6])
    try:
        result = subprocess.run(
            ["rg", "-l", "--hidden", pattern, project_root],
            capture_output=True,
            text=True,
            timeout=8,
            env={**os.environ, "RIPGREP_CONFIG_PATH": ""},
        )
    except Exception:
        return {}
    if result.returncode not in {0, 1}:
        return {}

    hits = {directory: 0 for directory in top_dirs}
    root = Path(project_root)
    for line in result.stdout.splitlines():
        try:
            rel_path = _normalize_rel_path(str(Path(line).resolve().relative_to(root)))
        except Exception:
            continue
        if _is_excluded_path(rel_path, exclude_paths):
            continue
        top_dir = rel_path.split("/", 1)[0]
        if top_dir in hits:
            hits[top_dir] += 1
    return hits


def _rrf_fusion(rankings: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for index, item in enumerate(ranking):
            scores[item["dir"]] = scores.get(item["dir"], 0.0) + 1.0 / (RRF_K + index + 1)
    return [
        {"dir": directory, "score": score}
        for directory, score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    ]


def _adaptive_top_k(fused: list[dict[str, Any]], requested_top_k: int, total_dirs: int) -> list[str]:
    if not fused:
        return []
    k_min = min(3, len(fused))
    k_max = min(10, len(fused))
    if len(fused) <= k_min:
        return [item["dir"] for item in fused]

    scores = [float(item["score"]) for item in fused]
    k_base = max(requested_top_k, min(k_max, math.ceil(total_dirs * 0.15)))
    k_knee = k_base
    max_gap = 0.0
    for index in range(max(0, k_min - 1), min(k_max, len(scores) - 1)):
        gap = scores[index] - scores[index + 1]
        if gap > max_gap:
            max_gap = gap
            k_knee = index + 1

    max_score = max(scores)
    exp_scores = [math.exp(score - max_score) for score in scores]
    exp_sum = sum(exp_scores) or 1.0
    probs = [score / exp_sum for score in exp_scores]
    entropy = -sum(prob * math.log(prob) for prob in probs if prob > 0)
    entropy_norm = entropy / math.log(len(scores)) if len(scores) > 1 else 0.0
    k_entropy = math.ceil(k_base * (1 + 0.5 * entropy_norm))
    primary_k = max(k_min, min(k_max, max(k_base, k_knee, k_entropy)))

    hot_dirs = [item["dir"] for item in fused[:primary_k]]
    if len(fused) > primary_k:
        cutoff = scores[primary_k - 1]
        head_decay = (scores[0] - cutoff) / max(1, primary_k - 1)
        threshold = max(cutoff - head_decay, cutoff * 0.4)
        for index in range(primary_k, min(len(fused), primary_k + 6)):
            if scores[index] < threshold:
                break
            hot_dirs.append(fused[index]["dir"])
    return hot_dirs


def _extract_path_spines(
    profiles: dict[str, DirectoryProfile],
    query_terms: list[str],
    keyword_terms: list[str],
    top_n: int = 30,
) -> list[str]:
    terms = list(dict.fromkeys([*query_terms, *keyword_terms]))
    if not terms:
        return []
    candidates: list[tuple[float, str]] = []
    for profile in profiles.values():
        for rel_path in profile.file_paths:
            path_tokens = tokenize_path(rel_path)
            path_text = rel_path.lower()
            file_name = Path(rel_path).stem.lower()
            file_tokens = tokenize_path(file_name)
            score = 0.0
            for term in terms:
                if term in file_name or term in file_tokens:
                    score += 4
                elif term in path_text:
                    score += 2
                elif any(token == term or token in term or term in token for token in path_tokens):
                    score += 1
            if score <= 0:
                continue
            padded = f"/{path_text}"
            if any(pattern in padded for pattern in SOURCE_PATH_PATTERNS):
                score *= 1.5
            if any(pattern in padded for pattern in NOISE_PATH_PATTERNS):
                score *= 0.3
            candidates.append((score, rel_path))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return [path for _, path in candidates[:top_n]]


def score_directories(
    query: str,
    project_root: str,
    top_dirs: list[str],
    exclude_paths: list[str],
    top_k: int = 4,
    keywords: list[str] | None = None,
) -> dict[str, Any]:
    query_terms = tokenize(query)
    keyword_terms = tokenize(" ".join(keywords or []))
    if not top_dirs:
        return {"hot_dirs": [], "path_spines": [], "signals": {}, "raw_rankings": {}}

    profiles = {
        directory: build_directory_profile(project_root, directory, exclude_paths)
        for directory in top_dirs
    }
    all_terms = [
        [
            *profile.tokenized.get("dir_name", []),
            *profile.tokenized.get("path_tokens", []),
            *profile.tokenized.get("metadata", []),
            *profile.tokenized.get("headers", []),
        ]
        for profile in profiles.values()
    ]
    idf = _compute_idf(all_terms)
    avg_field_lens = {
        field_name: sum(len(profile.tokenized.get(field_name, [])) for profile in profiles.values()) / max(1, len(profiles))
        for field_name in FIELD_WEIGHTS
    }

    bm25f_ranking = [
        {"dir": directory, "score": _bm25f_score(query_terms, profile, avg_field_lens, idf)}
        for directory, profile in profiles.items()
    ]
    bm25f_ranking.sort(key=lambda item: (-item["score"], item["dir"]))
    rankings: list[list[dict[str, Any]]] = []
    bm25f_positive = [item for item in bm25f_ranking if item["score"] > 0]
    if bm25f_positive:
        rankings.append(bm25f_positive)
    signals: dict[str, Any] = {"bm25f": [item["dir"] for item in bm25f_ranking[:8]]}

    probe_hits = _probe_grep(project_root, top_dirs, [*query_terms, *keyword_terms], exclude_paths)
    if probe_hits:
        probe_ranking = []
        for directory in top_dirs:
            file_count = max(1, profiles[directory].file_count)
            hits = probe_hits.get(directory, 0)
            score = math.log1p(hits) / math.sqrt(file_count + 1)
            probe_ranking.append({"dir": directory, "score": score, "hits": hits})
        probe_ranking.sort(key=lambda item: (-item["score"], item["dir"]))
        probe_positive = [item for item in probe_ranking if item["score"] > 0]
        if probe_positive:
            rankings.append(probe_positive)
        signals["probe"] = [f"{item['dir']}:{item['hits']}" for item in probe_ranking[:8]]

    file_agg_ranking = []
    for directory, profile in profiles.items():
        score = 0.0
        for rel_path in profile.file_paths[:300]:
            path_tokens = tokenize_path(rel_path)
            file_score = 0.0
            for term in query_terms:
                if term in path_tokens:
                    file_score += 2
                elif term in rel_path.lower() or any(term in token or token in term for token in path_tokens):
                    file_score += 1
            if file_score > 0:
                score += math.log1p(file_score)
        file_agg_ranking.append({"dir": directory, "score": score})
    if any(item["score"] > 0 for item in file_agg_ranking):
        file_agg_ranking.sort(key=lambda item: (-item["score"], item["dir"]))
        rankings.append([item for item in file_agg_ranking if item["score"] > 0])
        signals["file_agg"] = [f"{item['dir']}:{item['score']:.2f}" for item in file_agg_ranking[:8]]

    fused = _rrf_fusion(rankings) if rankings else [
        {"dir": directory, "score": 0.001}
        for directory in top_dirs[: max(1, min(top_k, len(top_dirs)))]
    ]
    hot_dirs = _adaptive_top_k(fused, top_k, len(top_dirs))
    if not hot_dirs:
        hot_dirs = top_dirs[: max(1, min(top_k, len(top_dirs)))]

    return {
        "hot_dirs": hot_dirs,
        "path_spines": _extract_path_spines(profiles, query_terms, keyword_terms, top_n=30),
        "signals": signals,
        "raw_rankings": {"bm25f": bm25f_ranking, "fused": fused},
    }


def render_repo_tree(
    project_root: str,
    depth: int,
    exclude_paths: list[str],
    start_rel: str = "",
    root_label: str = "/codebase",
    entry_limit: int = 200,
) -> str:
    lines = [root_label]
    root = Path(project_root)
    start = root / start_rel if start_rel else root

    def walk(real_dir: Path, rel_dir: str, level: int, prefix: str) -> None:
        if level >= depth:
            return
        try:
            entries = sorted(real_dir.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
        except OSError:
            return
        visible: list[Path] = []
        for entry in entries:
            rel_path = _normalize_rel_path(str(entry.relative_to(root)))
            if not _is_excluded_path(rel_path, exclude_paths):
                visible.append(entry)
        for entry in visible[:entry_limit]:
            rel_path = _normalize_rel_path(str(entry.relative_to(root)))
            lines.append(f"{prefix}├── {entry.name}")
            if entry.is_dir() and not entry.name.startswith("."):
                walk(entry, rel_path, level + 1, prefix + "│   ")

    walk(start, _normalize_rel_path(start_rel), 0, "")
    return "\n".join(lines)


def build_classic_repo_map(
    project_root: str,
    target_depth: int,
    exclude_paths: list[str],
    max_bytes: int,
) -> dict[str, Any]:
    safe_depth = max(1, min(target_depth, 6))
    for depth in range(safe_depth, 0, -1):
        tree = render_repo_tree(project_root, depth, exclude_paths)
        size_bytes = len(tree.encode("utf-8"))
        if size_bytes <= max_bytes:
            return {
                "tree": tree,
                "depth": depth,
                "size_bytes": size_bytes,
                "fell_back": depth < safe_depth,
                "strategy": "classic",
                "hot_dirs": [],
                "path_spines": [],
                "signals": {},
            }
    try:
        entries = sorted(
            entry.name for entry in Path(project_root).iterdir()
            if not _is_excluded_path(entry.name, exclude_paths)
        )
    except OSError:
        tree = "/codebase\n(empty or inaccessible)"
        return {
            "tree": tree,
            "depth": 0,
            "size_bytes": len(tree.encode("utf-8")),
            "fell_back": True,
            "strategy": "classic",
            "hot_dirs": [],
            "path_spines": [],
            "signals": {},
        }
    tree = "\n".join(["/codebase"] + [f"├── {entry}" for entry in entries[:200]])
    return {
        "tree": tree,
        "depth": 0,
        "size_bytes": len(tree.encode("utf-8")),
        "fell_back": True,
        "strategy": "classic",
        "hot_dirs": [],
        "path_spines": [],
        "signals": {},
    }


def build_optimized_repo_map(
    project_root: str,
    query: str,
    target_depth: int,
    exclude_paths: list[str],
    max_bytes: int,
    hotspot_top_k: int = 4,
    hotspot_tree_depth: int = 2,
) -> dict[str, Any]:
    top_dirs = list_top_level_dirs(project_root, exclude_paths)
    if not query.strip() or not top_dirs:
        return build_classic_repo_map(project_root, target_depth, exclude_paths, max_bytes)

    bootstrap = build_classic_repo_map(project_root, min(1, target_depth), exclude_paths, max_bytes)
    scored = score_directories(query, project_root, top_dirs, exclude_paths, top_k=hotspot_top_k)
    hot_dirs = scored["hot_dirs"]

    hotspot_sections = [
        render_repo_tree(
            project_root,
            hotspot_tree_depth,
            exclude_paths,
            start_rel=directory,
            root_label=f"/codebase/{directory}",
        )
        for directory in hot_dirs
        if (Path(project_root) / directory).is_dir()
    ]
    path_spines = list(scored["path_spines"])

    def compose_tree() -> tuple[str, int]:
        sections: list[str] = []
        if path_spines:
            sections.append("# Relevant File Paths\n" + "\n".join(f"- /codebase/{path}" for path in path_spines))
        if hotspot_sections:
            sections.append("# Hotspot Subtrees\n" + "\n\n".join(hotspot_sections))
        tree_text = bootstrap["tree"]
        if sections:
            tree_text = f"{tree_text}\n\n" + "\n\n".join(sections)
        return tree_text, len(tree_text.encode("utf-8"))

    tree, size_bytes = compose_tree()

    while size_bytes > max_bytes and len(hotspot_sections) > 1:
        hotspot_sections.pop()
        tree, size_bytes = compose_tree()

    while size_bytes > max_bytes and len(path_spines) > 4:
        path_spines.pop()
        tree, size_bytes = compose_tree()

    if size_bytes > max_bytes and hotspot_sections:
        hotspot_sections.clear()
        tree, size_bytes = compose_tree()

    while size_bytes > max_bytes and path_spines:
        path_spines.pop()
        tree, size_bytes = compose_tree()

    if size_bytes > max_bytes:
        fallback = build_classic_repo_map(project_root, target_depth, exclude_paths, max_bytes)
        fallback["strategy"] = "classic_fallback"
        return fallback

    return {
        "tree": tree,
        "depth": bootstrap["depth"],
        "size_bytes": size_bytes,
        "fell_back": bool(bootstrap["fell_back"] or target_depth > bootstrap["depth"]),
        "strategy": "bootstrap_hotspot",
        "hot_dirs": hot_dirs,
        "path_spines": path_spines,
        "signals": scored["signals"],
    }
