#!/usr/bin/env python3
"""CPU-only synthetic profiler for the installed SciPy linkage path."""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import resource
import sys
import time
import tracemalloc
from pathlib import Path

import numpy as np
import scipy
from scipy.cluster import hierarchy
from scipy.cluster.hierarchy import fcluster
from scipy.spatial.distance import pdist


def hwm_kib() -> int:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


def rss_kib() -> int:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return hwm_kib()


def normalized(n: int, dimension: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    values = rng.standard_normal((n, dimension), dtype=np.float32)
    return values / np.linalg.norm(values, axis=1, keepdims=True)


def sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--phase",
        choices=("normalize", "linkage", "pdist", "fast_linkage", "cut"),
        required=True,
    )
    parser.add_argument("--n", type=int, required=True)
    parser.add_argument("--dimension", type=int, default=256)
    parser.add_argument("--seed", type=int, default=97)
    args = parser.parse_args()
    if not 2 <= args.n <= 8000:
        parser.error("--n must be between 2 and 8000 for this bounded diagnostic")
    if not 1 <= args.dimension <= 1024:
        parser.error("--dimension must be between 1 and 1024")

    gc.collect()
    start_hwm = hwm_kib()
    tracemalloc.start()
    started = time.perf_counter()
    extra: dict[str, object] = {}

    if args.phase == "normalize":
        observations = normalized(args.n, args.dimension, args.seed)
        keepalive = float(observations[0, 0])
        extra["observation_shape"] = list(observations.shape)

    elif args.phase == "linkage":
        observations = normalized(args.n, args.dimension, args.seed)
        dendrogram = hierarchy.linkage(observations, method="centroid", metric="euclidean")
        keepalive = float(observations[0, 0] + dendrogram[0, 2])
        extra["observation_shape"] = list(observations.shape)
        extra["dendrogram_shape"] = list(dendrogram.shape)

    elif args.phase == "pdist":
        observations = normalized(args.n, args.dimension, args.seed)
        distances = pdist(observations, metric="euclidean")
        keepalive = float(observations[0, 0] + distances[0])
        extra["observation_shape"] = list(observations.shape)
        extra["distance_shape"] = list(distances.shape)

    elif args.phase == "cut":
        observations = normalized(args.n, args.dimension, args.seed)
        dendrogram = hierarchy.linkage(observations, method="centroid", metric="euclidean")
        before_hwm = hwm_kib()
        before_rss = rss_kib()
        tracemalloc.reset_peak()
        operation_started = time.perf_counter()
        labels = fcluster(dendrogram, 0.6, criterion="distance") - 1
        _, labels = np.unique(labels, return_inverse=True)
        extra["cut_elapsed_s"] = round(time.perf_counter() - operation_started, 6)
        extra["operation_hwm_before_kib"] = before_hwm
        extra["operation_rss_before_kib"] = before_rss
        extra["operation_hwm_delta_kib"] = max(0, hwm_kib() - before_hwm)
        keepalive = float(labels.sum() + dendrogram[0, 2])
        extra["cluster_count"] = int(np.max(labels) + 1)
        extra["dendrogram_shape"] = list(dendrogram.shape)

    else:
        # Precompute the condensed vector outside the operation interval. The
        # process-level peak remains cumulative, but before/after HWM and RSS
        # show whether fast_linkage adds a larger peak than its input vector.
        observations = normalized(args.n, args.dimension, args.seed)
        distances = pdist(observations, metric="euclidean")
        before_hwm = hwm_kib()
        before_rss = rss_kib()
        tracemalloc.reset_peak()
        operation_started = time.perf_counter()
        dendrogram = hierarchy._hierarchy.fast_linkage(distances, args.n, 3)
        extra["fast_linkage_elapsed_s"] = round(time.perf_counter() - operation_started, 6)
        extra["operation_hwm_before_kib"] = before_hwm
        extra["operation_rss_before_kib"] = before_rss
        extra["operation_hwm_delta_kib"] = max(0, hwm_kib() - before_hwm)
        keepalive = float(distances[0] + dendrogram[0, 2])
        extra["distance_shape"] = list(distances.shape)
        extra["dendrogram_shape"] = list(dendrogram.shape)

    traced_current, traced_peak = tracemalloc.get_traced_memory()
    elapsed = time.perf_counter() - started
    peak = hwm_kib()
    extra["tracemalloc_current_bytes"] = traced_current
    extra["tracemalloc_peak_bytes"] = traced_peak
    extra["peak_hwm_kib"] = peak
    extra["ru_maxrss_kib"] = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    extra["ru_start_hwm_kib"] = start_hwm
    extra["elapsed_s"] = round(elapsed, 6)
    extra["pair_count"] = args.n * (args.n - 1) // 2
    extra["pair_bytes_float64"] = args.n * (args.n - 1) // 2 * 8
    extra["linkage_bytes_float64"] = max(0, args.n - 1) * 4 * 8
    extra["dimension"] = args.dimension
    extra["n"] = args.n
    extra["phase"] = args.phase
    extra["numpy_version"] = np.__version__
    extra["scipy_version"] = scipy.__version__
    extra["hierarchy_source"] = hierarchy.__file__
    extra["hierarchy_impl_source"] = str(Path(hierarchy.__file__).with_name("_hierarchy_impl.py"))
    extra["hierarchy_impl_sha256"] = sha256(extra["hierarchy_impl_source"])
    extra["python_version"] = sys.version.split()[0]
    extra["keepalive"] = keepalive
    print(json.dumps(extra, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
