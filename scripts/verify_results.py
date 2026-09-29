#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Validate the released run summaries and the reported delivery results."""

from __future__ import annotations

import csv
import json
import math
import re
import statistics
from collections import defaultdict
from itertools import product
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
RESULTS = ROOT / "results"
WORKLOAD = ROOT / "workload"
CASES = [f"Case {number}" for number in range(1, 6)]
PATTERNS = ["coherent", "random"]
USERS = [50, 100, 150, 200, 250, 300]
REPEATS = [1, 2, 3]
METRICS = [
    "viewport_mean_s",
    "viewport_p50_s",
    "viewport_p95_s",
    "slide_open_p95_s",
    "response_mean_kB",
    "ttfb_p95_ms",
    "tiles_per_s",
    "tile_Mbps",
]


def read_csv(name: str) -> list[dict[str, str]]:
    with (DATA / name).open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def close(actual: float, expected: float, label: str) -> None:
    if not math.isclose(actual, expected, rel_tol=1e-10, abs_tol=1e-8):
        raise AssertionError(f"{label}: expected {expected!r}, found {actual!r}")


def metric_values(metrics: dict, name: str) -> dict:
    entry = metrics.get(name) or {}
    return entry.get("values", entry)


def metric_value(metrics: dict, name: str, statistic: str, default: float = 0.0) -> float:
    value = metric_values(metrics, name).get(statistic, default)
    return float(default if value is None else value)


def load_raw_summaries() -> dict[tuple[str, str, int, int], dict]:
    index: dict[tuple[str, str, int, int], dict] = {}
    name_pattern = re.compile(r"(coherent|random)_users(\d{3})_rep([123])\.json")
    for case_number, case in enumerate(CASES, start=1):
        case_dir = RESULTS / f"case{case_number}"
        files = sorted(case_dir.glob("*.json"))
        if len(files) != 36:
            raise AssertionError(f"{case_dir.relative_to(ROOT)} contains {len(files)} summaries, expected 36")
        for path in files:
            match = name_pattern.fullmatch(path.name)
            if not match:
                raise AssertionError(f"Unexpected summary filename: {path.relative_to(ROOT)}")
            pattern, users, repeat = match.groups()
            key = (case, pattern, int(users), int(repeat))
            if key in index:
                raise AssertionError(f"Duplicate raw summary: {key}")
            document = json.loads(path.read_text(encoding="utf-8"))
            if set(document) != {"metrics", "root_group"}:
                raise AssertionError(f"Unexpected top-level keys in {path.relative_to(ROOT)}")
            index[key] = document["metrics"]
    if len(index) != 180:
        raise AssertionError(f"Found {len(index)} raw summaries, expected 180")
    return index


def raw_run(metrics: dict) -> dict[str, float | int]:
    requests = int(metric_value(metrics, "tile_requests_total", "count"))
    successes = int(metric_value(metrics, "tile_success_total", "count"))
    failures = int(metric_value(metrics, "tile_failures", "count"))
    unclassified = requests - successes - failures
    expired = int(metric_value(metrics, "tile_err_expired_410_403", "count"))
    throttled = int(metric_value(metrics, "tile_throttled_429", "count")) + int(
        metric_value(metrics, "tile_throttled_503", "count")
    )
    success_rate = metric_value(metrics, "tile_success_rate", "value")
    target_met = int(
        requests > 0
        and throttled == 0
        and expired / requests < 0.001
        and success_rate > 0.95
    )
    return {
        "viewport_mean_s": metric_value(metrics, "viewport_fill_ms", "avg") / 1000.0,
        "viewport_p50_s": metric_value(metrics, "viewport_fill_ms", "med") / 1000.0,
        "viewport_p95_s": metric_value(metrics, "viewport_fill_ms", "p(95)") / 1000.0,
        "slide_open_p95_s": metric_value(metrics, "slide_open_fill_ms", "p(95)") / 1000.0,
        "response_mean_kB": metric_value(metrics, "tile_size_bytes", "avg") / 1000.0,
        "ttfb_p95_ms": metric_value(metrics, "tile_ttfb_ms", "p(95)"),
        "tiles_per_s": metric_value(metrics, "tile_success_total", "rate"),
        "tile_Mbps": metric_value(metrics, "tile_bytes", "rate") * 8.0 / 1_000_000.0,
        "tile_requests": requests,
        "tile_failures": failures,
        "tile_successes": successes,
        "unclassified_requests": unclassified,
        "tile_success_pct": successes / requests * 100.0,
        "login_failures": int(metric_value(metrics, "login_failures", "count")),
        "target_met": target_met,
    }


def validate_workloads() -> None:
    for filename, pattern in (("coherent.json", "coherent"), ("random.json", "random")):
        document = json.loads((WORKLOAD / filename).read_text(encoding="utf-8"))
        meta = document["meta"]
        if meta["pattern"] != pattern or meta["seed"] != 42:
            raise AssertionError(f"Unexpected metadata in workload/{filename}")
        if len(document["steps"]) != 38 or sum(len(step["tiles"]) for step in document["steps"]) != 6178:
            raise AssertionError(f"Unexpected replay shape in workload/{filename}")
        if len(document["variants"]) != 20 or meta["peakConcurrency"] != 64:
            raise AssertionError(f"Unexpected replay variants in workload/{filename}")


def main() -> None:
    raw = load_raw_summaries()
    validate_workloads()
    runs = read_csv("run_statistics.csv")

    expected_run_fields = {
        "case",
        "pattern",
        "users",
        "repeat",
        *METRICS,
        "tile_requests",
        "tile_failures",
        "tile_successes",
        "unclassified_requests",
        "tile_success_pct",
        "login_failures",
        "target_met",
    }
    if set(runs[0]) != expected_run_fields:
        raise AssertionError("Unexpected fields in run_statistics.csv")

    expected_keys = set(product(CASES, PATTERNS, USERS, REPEATS))
    run_index = {
        (row["case"], row["pattern"], int(row["users"]), int(row["repeat"])): row
        for row in runs
    }
    if len(runs) != 180 or set(run_index) != expected_keys or set(raw) != expected_keys:
        raise AssertionError("The run data are not the expected 5 x 2 x 6 x 3 design")

    integer_fields = {
        "tile_requests",
        "tile_failures",
        "tile_successes",
        "unclassified_requests",
        "login_failures",
        "target_met",
    }
    for key, row in run_index.items():
        expected = raw_run(raw[key])
        for field, value in expected.items():
            if field in integer_fields:
                if int(row[field]) != value:
                    raise AssertionError(f"{key} {field}: expected {value}, found {row[field]}")
            else:
                close(float(row[field]), float(value), f"{key} {field}")
        accounted = int(row["tile_successes"]) + int(row["tile_failures"]) + int(row["unclassified_requests"])
        if accounted != int(row["tile_requests"]):
            raise AssertionError(f"Request accounting failed for {key}")

    total_requests = sum(int(row["tile_requests"]) for row in runs)
    if total_requests != 298_331_670:
        raise AssertionError(f"Unexpected request total: {total_requests}")

    cells = read_csv("cell_summary.csv")
    cell_index: dict[tuple[str, str, int, str], dict[str, str]] = {}
    for row in cells:
        key = (row["case"], row["pattern"], int(row["users"]), row["metric"])
        if key in cell_index:
            raise AssertionError(f"Duplicate cell row: {key}")
        cell_index[key] = row
    expected_cell_keys = set(product(CASES, PATTERNS, USERS, METRICS))
    if len(cells) != 480 or set(cell_index) != expected_cell_keys:
        raise AssertionError("The cell table is not the expected 60 cells x 8 reported metrics")

    for key, row in cell_index.items():
        case, pattern, users, metric = key
        values = [float(run_index[(case, pattern, users, repeat)][metric]) for repeat in REPEATS]
        if int(row["n"]) != 3:
            raise AssertionError(f"Unexpected cell count for {key}")
        close(float(row["mean"]), statistics.mean(values), f"{key} mean")
        close(float(row["sd"]), statistics.stdev(values), f"{key} sample SD")
        close(float(row["minimum"]), min(values), f"{key} minimum")
        close(float(row["maximum"]), max(values), f"{key} maximum")
        for repeat, value in enumerate(values, start=1):
            close(float(row[f"rep{repeat}"]), value, f"{key} repeat {repeat}")

    payload = read_csv("payload_summary.csv")
    if len(payload) != 4 or {row["case"] for row in payload} != set(CASES[:4]):
        raise AssertionError("payload_summary.csv must contain the four coherent-access cases")
    for row in payload:
        case = row["case"]
        metrics = [raw[(case, "coherent", 50, repeat)] for repeat in REPEATS]
        checks = {
            "small_response_share_pct": [metric_value(m, "tile_white_rate", "value") * 100.0 for m in metrics],
            "response_size_p95_kB": [metric_value(m, "tile_size_bytes", "p(95)") / 1000.0 for m in metrics],
            "response_mean_kB": [metric_value(m, "tile_size_bytes", "avg") / 1000.0 for m in metrics],
        }
        for prefix, values in checks.items():
            close(float(row[f"{prefix}_mean"]), statistics.mean(values), f"{case} {prefix} mean")
            close(float(row[f"{prefix}_sd"]), statistics.stdev(values), f"{case} {prefix} SD")

    pool_rows = read_csv("edge_pool_comparison.csv")
    if len(pool_rows) != 12:
        raise AssertionError("edge_pool_comparison.csv must contain 12 pattern-by-load cells")
    for row in pool_rows:
        pattern, users = row["pattern"], int(row["users"])
        case3 = [raw[("Case 3", pattern, users, repeat)] for repeat in REPEATS]
        case4 = [raw[("Case 4", pattern, users, repeat)] for repeat in REPEATS]
        failures3 = [int(metric_value(m, "tile_failures", "count")) for m in case3]
        failures4 = [int(metric_value(m, "tile_failures", "count")) for m in case4]
        if row["case3_failures_by_repeat"] != "|".join(map(str, failures3)):
            raise AssertionError(f"Case 3 failure detail changed for {pattern}, {users}")
        if row["case4_failures_by_repeat"] != "|".join(map(str, failures4)):
            raise AssertionError(f"Case 4 failure detail changed for {pattern}, {users}")
        for case_label, metrics in (("case3", case3), ("case4", case4)):
            p95 = [metric_value(m, "tile_duration_ms", "p(95)") / 1000.0 for m in metrics]
            close(float(row[f"{case_label}_tile_duration_p95_s_min"]), min(p95), f"{case_label} p95 minimum")
            close(float(row[f"{case_label}_tile_duration_p95_s_max"]), max(p95), f"{case_label} p95 maximum")
        p99_case3 = [metric_value(m, "tile_duration_ms", "p(99)") / 1000.0 for m in case3]
        close(float(row["case3_tile_duration_p99_s_min"]), min(p99_case3), "Case 3 p99 minimum")
        close(float(row["case3_tile_duration_p99_s_max"]), max(p99_case3), "Case 3 p99 maximum")
        requests3 = sum(metric_value(m, "tile_requests_total", "count") for m in case3)
        requests4 = sum(metric_value(m, "tile_requests_total", "count") for m in case4)
        close(float(row["case4_over_case3_tile_requests"]), requests4 / requests3, "Case 4 / Case 3 requests")

    probes = read_csv("transport_probe.csv")
    if [int(row["connections"]) for row in probes] != [16, 128, 512]:
        raise AssertionError("transport_probe.csv must contain the three reported connection counts")
    if any(int(row["observations"]) != 1 for row in probes):
        raise AssertionError("Each connection probe must have one observation")

    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in runs:
        grouped[row["case"]].append(row)

    def mean(case: str, pattern: str, users: int, metric: str) -> float:
        return float(cell_index[(case, pattern, users, metric)]["mean"])

    def reductions(metric: str) -> list[float]:
        return [
            100.0 * (1.0 - mean("Case 4", pattern, users, metric) / mean("Case 1", pattern, users, metric))
            for pattern in PATTERNS
            for users in USERS
        ]

    mean_reductions = reductions("viewport_mean_s")
    p95_reductions = reductions("viewport_p95_s")
    coherent_slide_factors = [
        mean("Case 1", "coherent", users, "slide_open_p95_s")
        / mean("Case 4", "coherent", users, "slide_open_p95_s")
        for users in USERS
    ]
    p50_baseline_faster = sum(
        mean("Case 1", pattern, users, "viewport_p50_s")
        < mean("Case 4", pattern, users, "viewport_p50_s")
        for pattern in PATTERNS
        for users in USERS
    )
    failures = {
        case: sum(int(row["tile_failures"]) for row in grouped[case]) for case in CASES
    }
    if failures["Case 3"] != 99_754 or failures["Case 4"] != 530 or failures["Case 5"] != 1:
        raise AssertionError("Reported failure totals changed")

    print(f"Raw k6 summaries: {len(raw)}")
    print(f"Runs: {len(runs)}")
    print(f"Recorded tile outcomes: {total_requests:,}")
    print(f"Case 4 vs Case 1, mean request-batch time reduction: {min(mean_reductions):.2f}% to {max(mean_reductions):.2f}%")
    print(f"Case 4 vs Case 1, request-batch p95 reduction: {min(p95_reductions):.2f}% to {max(p95_reductions):.2f}%")
    print(f"Case 1 / Case 4 coherent slide-open p95: {min(coherent_slide_factors):.2f} to {max(coherent_slide_factors):.2f} times")
    print(f"Cells with a lower Case 1 request-batch p50: {p50_baseline_faster}/12")
    print("Case 3 and Case 4 failures: 99,754 and 530")
    print("Case 5 failures: 1")
    print("All checks passed.")


if __name__ == "__main__":
    main()
