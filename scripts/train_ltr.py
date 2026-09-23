# -*- coding: utf-8 -*-
"""Offline learning-to-rank experiment over the synthetic graded query pairs.

Trains and compares two ranking models on one shared feature set — a pointwise
gradient-boosted regressor (scikit-learn) and a pairwise RankNet-style MLP
(PyTorch) — then reports nDCG@5 / nDCG@10 / MRR on the held-out split against
three production-rule baselines (lex order, linear fusion, reciprocal rank
fusion), plus a feature ablation.

Features deliberately contain only signals available at inference time
(lex_score, vec_score, rank position and per-dimension match flags); the
deterministic judge grade and the LLM semantic score are labels only and never
enter the feature vector, so the reported metrics measure ranking rather than
label recall.

Deliberately standalone: reads only the JSONL pair files under
``research/ltr_data/`` (stdlib + numpy + scikit-learn + PyTorch), imports nothing
from the project's ``src/``, makes no network or LLM calls, and is deterministic
for a fixed ``--seed``.

With ``--export DIR`` the full-feature RankNet MLP is also written out as
``ltr_mlp.pt`` (state_dict) and ``ltr_spec.json`` (feature contract, training
normalisation stats, held-out metrics). The exported artefacts are then re-loaded
and re-scored against the in-training ranking; a per-query nDCG@10 mismatch above
1e-6 fails the run.

Input: one JSON object per line, ``{"query_id", "query", "candidates": [...]}``.
Each candidate carries ``uid``, 1-based ``pos``, ``lex_score``, an optional
``vec_score`` (dense-embedding cosine; absent or null counts as 0 with a
``has_vec`` companion flag), a deterministic constraint ``grade`` (2 = every
constraint met, 1 = exactly one violated, 0 = two or more) and a per-dimension
``matched`` map; ``llm_semantic`` (0-3, possibly absent) is the optional LLM
relevance score. Split files are read in layers of increasing richness —
``synth_{split}_vec.jsonl`` > ``_labeled.jsonl`` > ``_nl.jsonl`` >
``synth_{split}.jsonl`` — where a higher-priority row overrides the same query_id
and lower layers fill in the rest, because the augmentation jobs write their
layers incrementally.

Features (9 floats per candidate): lex_score normalised by the query maximum,
vec_score and its has_vec flag, 1/pos, the four fixed ``matched`` slots
(species / tissue / disease / has_raw_data; a missing key counts as 0), and the
query's has_raw_data_required flag.

Labels (``--label``): ``det`` = grade/2; ``sem`` = llm_semantic/3 (candidates
without a semantic score are dropped); ``mix`` = 0.5*grade/2 + 0.5*llm_semantic/3
when a semantic score exists, else grade/2. Gains for nDCG use 2**label - 1.

Usage:
  python scripts/train_ltr.py --label det
  python scripts/train_ltr.py --label mix --seed 20260910
  python scripts/train_ltr.py --label mix --export models/ltr
"""
from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import numpy as np
import torch
from sklearn.ensemble import HistGradientBoostingRegressor

AGENT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_IN_DIR = AGENT_ROOT / "research" / "ltr_data"
DEFAULT_SEED = 20260910

#: Split-file layers from richest to plainest; every layer that exists is merged,
#: with earlier entries overriding later ones per query_id.
LAYER_SUFFIXES = ("vec", "labeled", "nl", "")

#: Fixed matched-dimension slots, in feature order. A candidate whose ``matched``
#: map omits a key is treated as not matching it.
DIM_SLOTS = ("species", "tissue", "disease", "has_raw_data")

FEATURE_NAMES = (
    "lex_norm",
    "vec_norm",
    "has_vec",
    "inv_pos",
    "matched_species",
    "matched_tissue",
    "matched_disease",
    "matched_has_raw_data",
    "raw_required",
)
_CONSTRAINT_COLS = (4, 5, 6, 7, 8)
ABLATIONS = {
    "constraint": _CONSTRAINT_COLS,
    "constraint+lex": (0,) + _CONSTRAINT_COLS,
    "full": tuple(range(len(FEATURE_NAMES))),
}

LINEAR_ALPHA = 0.5
RRF_K = 60
PAIR_CAP = 200_000
RANKNET_HIDDEN = (32, 16)
RANKNET_EPOCHS = 400
RANKNET_LR = 0.02

ScoreFn = Callable[[np.ndarray], np.ndarray]
OrderFn = Callable[[dict], np.ndarray]


def _read_jsonl(path: Path) -> list[dict]:
    """Read one JSON object per line, skipping blank or torn trailing lines."""
    rows: list[dict] = []
    malformed = 0
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                # The augmentation jobs append to their layer files live, so a
                # partially flushed last line is possible; skip it rather than crash.
                malformed += 1
    if malformed:
        print(f"  ! skipped {malformed} malformed line(s) in {path.name}")
    return rows


def _layer_path(in_dir: Path, stem: str, suffix: str) -> Path:
    name = f"synth_{stem}_{suffix}.jsonl" if suffix else f"synth_{stem}.jsonl"
    return in_dir / name


def load_split(in_dir: Path, stem: str) -> tuple[list[dict], dict]:
    """Merge the split's existing layers, richest first, filling gaps from plainer ones."""
    layers = []
    for suffix in LAYER_SUFFIXES:
        path = _layer_path(in_dir, stem, suffix)
        if path.exists():
            layers.append((path.name, _read_jsonl(path)))
    if not layers:
        names = ", ".join(_layer_path(in_dir, stem, s).name for s in LAYER_SUFFIXES)
        raise FileNotFoundError(f"none of {names} exists in {in_dir}")
    # Overlay plainer layers first so richer rows win, and keep the plainer order
    # for queries only the base file covers: a partial augmentation layer must not
    # shrink the corpus.
    merged: list[dict] = []
    index: dict[str, int] = {}
    for _name, rows in reversed(layers):
        for row in rows:
            qid = row.get("query_id")
            if qid and qid in index:
                merged[index[qid]] = row
            else:
                if qid:
                    index[qid] = len(merged)
                merged.append(row)
    info = {"layers": {name: len(rows) for name, rows in layers}, "queries": len(merged)}
    return merged, info


def merge_mix_in(train_rows: list[dict], paths: list[Path]) -> tuple[list[dict], dict]:
    """Merge extra training rows (e.g. OOD xscore exports) into the train split.

    Rows dedupe by query_id against the base split and each other; on collision
    the row with more candidates wins (a same-id double can be an A/B rescore).
    Extra rows never touch the heldout split — mixing into evaluation would leak."""
    merged = list(train_rows)
    index = {row.get("query_id"): i for i, row in enumerate(merged) if row.get("query_id")}
    stats: dict[str, dict] = {}
    for path in paths:
        rows = _read_jsonl(path)
        added = replaced = 0
        for row in rows:
            qid = row.get("query_id")
            if not qid:
                merged.append(row)  # no id: always appended, never deduped
                added += 1
                continue
            if qid in index:
                if len(row.get("candidates") or []) > len(merged[index[qid]].get("candidates") or []):
                    merged[index[qid]] = row
                    replaced += 1
            else:
                index[qid] = len(merged)
                merged.append(row)
                added += 1
        stats[path.name] = {"rows": len(rows), "added": added, "replaced": replaced}
    return merged, stats


def candidate_features(row: dict) -> tuple[np.ndarray, list[float], list[float]]:
    """Build the feature matrix (n_candidates x n_features), lex scores and vec scores.

    Vec scores are returned with NaN for missing values so the fusion baselines can
    tell "no embedding for this candidate" from "embedding scored zero"."""
    candidates = row.get("candidates") or []
    lex = [float(c.get("lex_score") or 0.0) for c in candidates]
    lex_max = max(lex) if lex else 0.0
    raw_required = 1.0 if row.get("has_raw_data_required") else 0.0
    vec: list[float] = []
    feats = []
    for cand, score in zip(candidates, lex):
        matched = cand.get("matched") or {}
        vec_score = cand.get("vec_score")
        missing = vec_score is None
        vec.append(float("nan") if missing else float(vec_score))
        feats.append([
            (score / lex_max) if lex_max > 0 else 0.0,
            0.0 if missing else float(vec_score),
            0.0 if missing else 1.0,
            1.0 / float(cand.get("pos") or 1),
            *(1.0 if matched.get(dim) else 0.0 for dim in DIM_SLOTS),
            raw_required,
        ])
    matrix = np.asarray(feats, dtype=np.float32).reshape(len(feats), len(FEATURE_NAMES))
    return matrix, lex, vec


def label_value(cand: dict, mode: str) -> float | None:
    """Relevance label for one candidate; None means the sample is dropped."""
    grade = float(cand.get("grade") or 0) / 2.0
    if mode == "det":
        return grade
    semantic = cand.get("llm_semantic")
    if mode == "sem":
        return None if semantic is None else float(semantic) / 3.0
    if semantic is None:
        return grade
    return 0.5 * grade + 0.5 * float(semantic) / 3.0


def build_training_matrix(rows: list[dict], mode: str) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict]:
    """Stack labelled candidates into (X, y, group) arrays; ``group`` indexes the query row."""
    xs: list[np.ndarray] = []
    ys: list[float] = []
    groups: list[int] = []
    dropped = 0
    semantic_seen = 0
    vec_seen = 0
    candidates_seen = 0
    for group, row in enumerate(rows):
        feats, _lex, _vec = candidate_features(row)
        for i, cand in enumerate(row.get("candidates") or []):
            candidates_seen += 1
            if cand.get("llm_semantic") is not None:
                semantic_seen += 1
            if cand.get("vec_score") is not None:
                vec_seen += 1
            value = label_value(cand, mode)
            if value is None:
                dropped += 1
                continue
            xs.append(feats[i])
            ys.append(value)
            groups.append(group)
    matrix = np.asarray(xs, dtype=np.float32).reshape(len(xs), len(FEATURE_NAMES))
    stats = {
        "labelled_candidates": len(ys),
        "dropped_unlabelled": dropped,
        "candidates_seen": candidates_seen,
        "semantic_coverage": round(semantic_seen / candidates_seen, 4) if candidates_seen else 0.0,
        "vec_coverage": round(vec_seen / candidates_seen, 4) if candidates_seen else 0.0,
    }
    return matrix, np.asarray(ys, dtype=np.float64), np.asarray(groups, dtype=np.int64), stats


def eval_units(rows: list[dict], mode: str) -> list[dict]:
    """Per-query arrays needed for ranking metrics, restricted to labelled candidates."""
    units = []
    for row in rows:
        feats, lex, vec = candidate_features(row)
        candidates = row.get("candidates") or []
        labels = [label_value(c, mode) for c in candidates]
        valid = [i for i, value in enumerate(labels) if value is not None]
        if not valid:
            continue
        units.append({
            "feats": feats[valid],
            "gains": np.asarray([2.0 ** labels[i] - 1.0 for i in valid], dtype=np.float64),
            "lex": np.asarray([lex[i] for i in valid], dtype=np.float64),
            "vec": np.asarray([vec[i] for i in valid], dtype=np.float64),
            "pos": np.asarray([float(candidates[i].get("pos") or 1) for i in valid], dtype=np.float64),
        })
    return units


def _minmax(values: np.ndarray) -> np.ndarray:
    """Min-max scale to [0, 1]; missing (non-finite) entries become 0."""
    finite = np.isfinite(values)
    if not finite.any():
        return np.zeros_like(values)
    low = values[finite].min()
    high = values[finite].max()
    if high - low < 1e-12:
        return np.where(finite, 0.5, 0.0)
    return np.where(finite, (values - low) / (high - low), 0.0)


def _rank_desc(values: np.ndarray) -> np.ndarray:
    """1-based average ranks, highest value ranked first (ties share a rank)."""
    order = np.argsort(-values, kind="stable")
    ranked = values[order]
    ranks = np.empty(len(values), dtype=np.float64)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and ranked[j + 1] == ranked[i]:
            j += 1
        ranks[order[i:j + 1]] = 0.5 * (i + 1 + j + 1)
        i = j + 1
    return ranks


def lex_order(unit: dict) -> np.ndarray:
    """The retrieval pipeline's own order: lex_score descending, then original pos."""
    return np.lexsort((unit["pos"], -unit["lex"]))


def linear_order(unit: dict) -> np.ndarray:
    """Production linear fusion of min-max lex and vec at alpha=0.5.

    A query with no vec scores gets all-zero vec_norm, so its score stays monotone
    in lex_norm and the query degrades to pure lex order."""
    score = LINEAR_ALPHA * _minmax(unit["lex"]) + (1.0 - LINEAR_ALPHA) * _minmax(unit["vec"])
    return np.argsort(-score, kind="stable")


def rrf_order(unit: dict) -> np.ndarray:
    """Reciprocal rank fusion of the lex and vec rankings at k=RRF_K.

    Candidates without a vec score rank last; a query with no vec scores at all
    fuses the lex ranking alone."""
    lex_rank = _rank_desc(unit["lex"])
    if np.isfinite(unit["vec"]).any():
        vec_rank = _rank_desc(np.where(np.isfinite(unit["vec"]), unit["vec"], -np.inf))
        score = 1.0 / (RRF_K + lex_rank) + 1.0 / (RRF_K + vec_rank)
    else:
        score = 1.0 / (RRF_K + lex_rank)
    return np.argsort(-score, kind="stable")


BASELINES: dict[str, OrderFn] = {
    "lex order": lex_order,
    f"linear a={LINEAR_ALPHA}": linear_order,
    f"rrf k={RRF_K}": rrf_order,
}


def _ndcg_at_k(gains_in_order: np.ndarray, k: int) -> float:
    m = min(k, len(gains_in_order))
    if m == 0:
        return 0.0
    discount = 1.0 / np.log2(np.arange(2, m + 2))
    dcg = float(np.dot(gains_in_order[:m], discount))
    ideal = np.sort(gains_in_order)[::-1]
    idcg = float(np.dot(ideal[:m], discount))
    return dcg / idcg if idcg > 0 else 0.0


def evaluate(units: list[dict], order_fn: OrderFn) -> dict:
    """Mean nDCG@5 / nDCG@10 / MRR over queries; MRR skips queries with no positive gain."""
    ndcg5 = ndcg10 = mrr = 0.0
    mrr_queries = 0
    for unit in units:
        gains = unit["gains"][order_fn(unit)]
        ndcg5 += _ndcg_at_k(gains, 5)
        ndcg10 += _ndcg_at_k(gains, 10)
        hits = np.nonzero(gains > 0)[0]
        if hits.size:
            mrr += 1.0 / float(hits[0] + 1)
            mrr_queries += 1
    n = len(units)
    return {
        "nDCG@5": round(ndcg5 / n, 4) if n else None,
        "nDCG@10": round(ndcg10 / n, 4) if n else None,
        "MRR": round(mrr / mrr_queries, 4) if mrr_queries else None,
        "queries": n,
        "mrr_queries": mrr_queries,
    }


def model_order(score_fn: ScoreFn) -> OrderFn:
    def _order(unit: dict) -> np.ndarray:
        return np.argsort(-score_fn(unit["feats"]), kind="stable")

    return _order


def train_pointwise(X: np.ndarray, y: np.ndarray, cols: tuple[int, ...], seed: int) -> ScoreFn:
    model = HistGradientBoostingRegressor(random_state=seed)
    model.fit(X[:, cols], y)

    def _score(feats: np.ndarray) -> np.ndarray:
        return model.predict(feats[:, cols])

    return _score


def _pair_indices(y: np.ndarray, groups: np.ndarray, rng: np.random.Generator) -> tuple[np.ndarray, ...]:
    """Within-query pairs with differing labels, as (i, j, sign(y_i - y_j)) arrays."""
    by_group: dict[int, list[int]] = {}
    for idx, group in enumerate(groups):
        by_group.setdefault(int(group), []).append(idx)
    left: list[int] = []
    right: list[int] = []
    signs: list[float] = []
    for members in by_group.values():
        for a in range(len(members)):
            for b in range(a + 1, len(members)):
                ia, ib = members[a], members[b]
                if y[ia] == y[ib]:
                    continue
                left.append(ia)
                right.append(ib)
                signs.append(1.0 if y[ia] > y[ib] else -1.0)
    i_arr = np.asarray(left, dtype=np.int64)
    j_arr = np.asarray(right, dtype=np.int64)
    s_arr = np.asarray(signs, dtype=np.float32)
    if len(i_arr) > PAIR_CAP:
        keep = rng.choice(len(i_arr), size=PAIR_CAP, replace=False)
        i_arr, j_arr, s_arr = i_arr[keep], j_arr[keep], s_arr[keep]
    return i_arr, j_arr, s_arr


def _build_mlp(input_dim: int) -> torch.nn.Sequential:
    """RankNet MLP: input_dim -> RANKNET_HIDDEN -> 1, ReLU between layers."""
    layers: list[torch.nn.Module] = []
    last = input_dim
    for width in RANKNET_HIDDEN:
        layers += [torch.nn.Linear(last, width), torch.nn.ReLU()]
        last = width
    layers.append(torch.nn.Linear(last, 1))
    return torch.nn.Sequential(*layers)


def train_ranknet(
    X: np.ndarray,
    y: np.ndarray,
    groups: np.ndarray,
    cols: tuple[int, ...],
    seed: int,
) -> tuple[ScoreFn | None, dict, dict | None]:
    """Fit the pairwise RankNet MLP.

    Returns (score_fn, info, artifacts). score_fn and artifacts are None when no
    differing-label pair exists, so there is nothing to train or export; otherwise
    artifacts carries the state_dict and the normalisation stats used at inference."""
    torch.manual_seed(seed)
    rng = np.random.default_rng(seed)
    i_arr, j_arr, s_arr = _pair_indices(y, groups, rng)
    info = {"pairs": int(len(i_arr)), "epochs": RANKNET_EPOCHS, "lr": RANKNET_LR}
    if len(i_arr) == 0:
        info["note"] = "no differing-label pairs; model not trained"
        return None, info, None

    sub = X[:, cols]
    mean = sub.mean(axis=0)
    std = np.where(sub.std(axis=0) < 1e-8, 1.0, sub.std(axis=0))
    normalized = ((sub - mean) / std).astype(np.float32)

    net = _build_mlp(len(cols))
    optimizer = torch.optim.Adam(net.parameters(), lr=RANKNET_LR)
    features = torch.from_numpy(normalized)
    i_t = torch.from_numpy(i_arr)
    j_t = torch.from_numpy(j_arr)
    s_t = torch.from_numpy(s_arr)
    for _ in range(RANKNET_EPOCHS):
        optimizer.zero_grad()
        scores = net(features).squeeze(1)
        loss = -torch.nn.functional.logsigmoid(s_t * (scores[i_t] - scores[j_t])).mean()
        loss.backward()
        optimizer.step()
    info["final_loss"] = round(float(loss.item()), 4)
    net.eval()

    def _score(feats: np.ndarray) -> np.ndarray:
        scaled = ((feats[:, cols] - mean) / std).astype(np.float32)
        with torch.no_grad():
            return net(torch.from_numpy(scaled)).squeeze(1).numpy()

    artifacts = {
        "state_dict": {key: value.detach().clone() for key, value in net.state_dict().items()},
        "mean": mean,
        "std": std,
        "cols": cols,
    }
    return _score, info, artifacts


def _per_query_ndcg10(units: list[dict], order_fn: OrderFn) -> np.ndarray:
    return np.asarray([_ndcg_at_k(u["gains"][order_fn(u)], 10) for u in units], dtype=np.float64)


def export_ranknet(
    export_dir: Path,
    artifacts: dict,
    label_mode: str,
    seed: int,
    train_counts: dict,
    heldout_metrics: dict,
) -> dict:
    """Write the RankNet MLP state_dict and its feature contract into ``export_dir``."""
    export_dir.mkdir(parents=True, exist_ok=True)
    torch.save(artifacts["state_dict"], export_dir / "ltr_mlp.pt")
    spec = {
        "feature_order": [FEATURE_NAMES[c] for c in artifacts["cols"]],
        "label_mode": label_mode,
        "seed": seed,
        "feature_mean": [float(value) for value in artifacts["mean"]],
        "feature_std": [float(value) for value in artifacts["std"]],
        "trained_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "train_counts": train_counts,
        "heldout_metrics": heldout_metrics,
        "model": {
            "type": "ranknet_mlp",
            "input_dim": len(artifacts["cols"]),
            "hidden_layers": list(RANKNET_HIDDEN),
            "activation": "relu",
            "output_dim": 1,
            "loss": "pairwise_logistic",
        },
    }
    (export_dir / "ltr_spec.json").write_text(
        json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return spec


def selfcheck_export(export_dir: Path, units: list[dict], expected_order: OrderFn) -> float:
    """Re-load the exported MLP and spec, re-score heldout, return max |delta nDCG@10|.

    The normalisation stats are cast back to float32 exactly as training used them,
    so a faithful round-trip reproduces the in-training per-query ranking."""
    spec = json.loads((export_dir / "ltr_spec.json").read_text(encoding="utf-8"))
    cols = tuple(FEATURE_NAMES.index(name) for name in spec["feature_order"])
    mean = np.asarray(spec["feature_mean"], dtype=np.float32)
    std = np.asarray(spec["feature_std"], dtype=np.float32)
    net = _build_mlp(spec["model"]["input_dim"])
    net.load_state_dict(torch.load(export_dir / "ltr_mlp.pt", map_location="cpu", weights_only=True))
    net.eval()

    def _score(feats: np.ndarray) -> np.ndarray:
        scaled = ((feats[:, cols] - mean) / std).astype(np.float32)
        with torch.no_grad():
            return net(torch.from_numpy(scaled)).squeeze(1).numpy()

    reloaded = _per_query_ndcg10(units, model_order(_score))
    expected = _per_query_ndcg10(units, expected_order)
    if reloaded.size == 0:
        return 0.0
    return float(np.max(np.abs(reloaded - expected)))


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--in-dir", type=Path, default=DEFAULT_IN_DIR)
    parser.add_argument("--label", choices=("det", "sem", "mix"), default="mix")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument(
        "--export",
        type=Path,
        default=None,
        help="export the full-feature RankNet MLP (ltr_mlp.pt + ltr_spec.json) to this directory",
    )
    parser.add_argument(
        "--mix-in",
        type=Path,
        action="append",
        default=[],
        help="extra labelled JSONL to merge into the TRAIN split only (repeatable); "
        "dedupes by query_id, more candidates wins. Never touches heldout.",
    )
    args = parser.parse_args()

    in_dir: Path = args.in_dir
    out_path: Path = args.out or (in_dir / "_ltr_metrics.json")
    started = time.time()

    train_rows, train_src = load_split(in_dir, "train")
    held_rows, held_src = load_split(in_dir, "heldout")
    mix_stats: dict = {}
    if args.mix_in:
        train_rows, mix_stats = merge_mix_in(train_rows, args.mix_in)
        train_src = {**train_src, "mix_in": mix_stats}
    X, y, groups, label_stats = build_training_matrix(train_rows, args.label)
    units = eval_units(held_rows, args.label)

    print(f"label={args.label} seed={args.seed} in_dir={in_dir}")
    print(f"  train: {len(train_rows)} queries, {label_stats['labelled_candidates']} labelled candidates "
          f"({label_stats['dropped_unlabelled']} dropped), vec coverage {label_stats['vec_coverage']}, "
          f"semantic coverage {label_stats['semantic_coverage']}")
    print(f"  heldout: {len(held_rows)} queries, {len(units)} usable for ranking")
    print(f"  layers: train={train_src['layers']} heldout={held_src['layers']}")
    if mix_stats:
        print(f"  mix-in: {mix_stats} -> train now {len(train_rows)} queries")

    results: dict = {"baselines": {}, "models": {"hist_gbdt": {}, "ranknet_mlp": {}}}
    table: list[tuple[str, str, dict]] = []
    full_artifacts: dict | None = None
    full_order: OrderFn | None = None
    full_info: dict | None = None
    full_metrics: dict | None = None
    if units:
        for name, order_fn in BASELINES.items():
            metrics = evaluate(units, order_fn)
            results["baselines"][name] = metrics
            table.append(("baseline", name, metrics))

    if X.shape[0] == 0 or not units:
        print("  ! no labelled candidates for this --label mode; models skipped")
        for name in results["models"]:
            for ablation in ABLATIONS:
                results["models"][name][ablation] = {"nDCG@5": None, "nDCG@10": None, "MRR": None}
    else:
        for ablation, cols in ABLATIONS.items():
            score_fn = train_pointwise(X, y, cols, args.seed)
            metrics = evaluate(units, model_order(score_fn))
            results["models"]["hist_gbdt"][ablation] = metrics
            table.append(("hist_gbdt", ablation, metrics))
        for ablation, cols in ABLATIONS.items():
            score_fn, info, artifacts = train_ranknet(X, y, groups, cols, args.seed)
            if score_fn is None:
                metrics = {"nDCG@5": None, "nDCG@10": None, "MRR": None, **info}
            else:
                metrics = {**evaluate(units, model_order(score_fn)), **info}
            results["models"]["ranknet_mlp"][ablation] = metrics
            table.append(("ranknet_mlp", ablation, metrics))
            if ablation == "full" and artifacts is not None:
                full_artifacts, full_order, full_info, full_metrics = artifacts, model_order(score_fn), info, metrics

    print()
    print(f"{'model':<14}{'features':<16}{'nDCG@5':>9}{'nDCG@10':>9}{'MRR':>9}")
    print("-" * 57)
    for name, features, metrics in table:
        print(f"{name:<14}{features:<16}{_fmt(metrics.get('nDCG@5')):>9}"
              f"{_fmt(metrics.get('nDCG@10')):>9}{_fmt(metrics.get('MRR')):>9}")

    if args.export:
        if full_artifacts is None or full_order is None or full_info is None or full_metrics is None:
            print("  ! --export given but the full-feature RankNet model was not trained; export skipped")
        else:
            spec = export_ranknet(
                args.export,
                full_artifacts,
                args.label,
                args.seed,
                {"queries": len(train_rows), "pairs": full_info["pairs"]},
                {key: full_metrics.get(key) for key in ("nDCG@5", "nDCG@10", "MRR")},
            )
            max_delta = selfcheck_export(args.export, units, full_order)
            print(f"  export: {args.export} -> ltr_mlp.pt + ltr_spec.json "
                  f"({spec['model']['input_dim']} features: {', '.join(spec['feature_order'])})")
            print(f"  export self-check: max per-query |delta nDCG@10| = {max_delta:.3e}")
            if max_delta > 1e-6:
                print("  ! export self-check FAILED: reloaded ranking differs from in-training ranking")
                return 1

    payload = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "label_mode": args.label,
        "seed": args.seed,
        "in_dir": str(in_dir),
        "out_path": str(out_path),
        "feature_names": list(FEATURE_NAMES),
        "ablations": {name: [FEATURE_NAMES[c] for c in cols] for name, cols in ABLATIONS.items()},
        "data": {
            "train_queries": len(train_rows),
            "heldout_queries": len(held_rows),
            "heldout_ranked_queries": len(units),
            "train_label_stats": label_stats,
            "sources": {"train": train_src, "heldout": held_src},
        },
        "results": results,
        "elapsed_s": round(time.time() - started, 2),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {out_path} in {payload['elapsed_s']}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
