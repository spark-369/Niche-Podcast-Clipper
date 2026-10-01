"""Bound the thread pools of CPU numerical libraries so processes don't hang the CPU.

torch (Whisper/Flan-T5), OpenCV (OpenMP), NumPy (OpenBLAS/MKL) and SciPy each
spawn up to ``os.cpu_count()`` worker threads *per process* by default. With the
API (uvicorn ``--workers 2``) and the Celery worker pool (``--concurrency 2``)
all resident on one host, that is several processes each requesting every core,
which oversubscribes the scheduler and presents as a machine pegged at 100% CPU
with no task making progress.

This module centralises the caps so the API process and every Celery worker
child apply the *same* budget. The budget is shared fairly: when explicit
configuration is absent we default each process to the number of *physical*
cores (not hyper-thread siblings), which avoids the GEMM-heavy encoder/detector
passes competing with their own sibling threads.

Call :func:`apply_thread_limits` once per process — from the API startup and
from the Celery ``worker_process_init`` signal — *before* any inference or
frame work runs. Environment overrides (``OMP_NUM_THREADS`` etc.) take
precedence so operators can still pin values in ``docker-compose.yml``.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from core.config import settings

logger = logging.getLogger(__name__)

# Libraries that each honour a distinct environment variable and must be
# bounded together, or they will silently leave one library at full thread
# count and re-create the oversubscription.
_ENV_KEYS = (
    "OMP_NUM_THREADS",       # OpenMP: OpenCV, SciPy, MKL fallback
    "OPENBLAS_NUM_THREADS",  # numpy/scipy via OpenBLAS
    "MKL_NUM_THREADS",       # numpy via Intel MKL
    "VECLIB_MAXIMUM_THREADS",  # Apple Accelerate
    "NUMEXPR_NUM_THREADS",   # numexpr (pandas)
)


def _physical_core_count() -> Optional[int]:
    """Count unique (physical_ id, core id) pairs from /proc/cpuinfo."""
    try:
        unique: set[tuple[str, str]] = set()
        current: dict[str, str] = {}
        with open("/proc/cpuinfo", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    key, _, value = line.partition(":")
                    current[key.strip()] = value.strip()
                    continue
                physical = current.get("physical id")
                core = current.get("core id")
                if physical is not None and core is not None:
                    unique.add((physical, core))
                current = {}
        physical = current.get("physical id")
        core = current.get("core id")
        if physical is not None and core is not None:
            unique.add((physical, core))
        return len(unique) or None
    except OSError:
        return None


def _resolve_budget() -> int:
    """Return the thread budget for this process (>= 1)."""
    # 1) Explicit per-process configuration wins.
    configured = settings.AI_NUM_THREADS if settings is not None else 0
    if configured and configured > 0:
        return configured

    # 2) An operator-set OMP_NUM_THREADS is the common global override; mirror it
    #    across the other libraries so they stay consistent.
    omp = os.getenv("OMP_NUM_THREADS")
    if omp:
        try:
            value = int(omp)
            if value > 0:
                return value
        except ValueError:
            pass

    # 3) Default to physical cores (hyper-thread siblings add contention for
    #    GEMM-heavy work while contributing little CPU-only throughput).
    physical = _physical_core_count()
    if physical:
        return physical

    # 4) Final fallback: the logical core count, bounded to a sane floor.
    return max(1, os.cpu_count() or 1)


def _set_env_threads(budget: int) -> None:
    """Export the budget to every numerical library's own env variable."""
    for key in _ENV_KEYS:
        os.environ[key] = str(budget)


def apply_thread_limits() -> int:
    """Apply the process-wide thread budget to torch and the BLAS libraries.

    Returns the budget applied, so callers can log it. Idempotent and cheap;
    safe to call again after a fork.
    """
    budget = _resolve_budget()

    _set_env_threads(budget)

    # torch reads its thread count lazily and caches it, so set_num_threads is
    # the only reliable way to re-bound it in the current process (and must run
    # *after* the env var is set when torch has not yet initialised its pool).
    try:
        import torch

        torch.set_num_threads(budget)
        try:
            # inter-op parallelism also grows with core count; keep it modest so
            # several in-flight ops don't each try to grab the whole budget.
            torch.set_num_interop_threads(max(1, budget))
        except Exception:  # pragma: no cover - older/limited builds
            pass
    except Exception:  # pragma: no cover - torch optional at runtime
        pass

    # OpenCV caches the OpenMP pool at first use; setting the env var before
    # ``cv2`` is imported/used is sufficient, but re-assert here for safety and
    # also limit the internal threading when a threads build is present.
    try:
        import cv2

        cv2.setNumThreads(budget)
    except Exception:  # pragma: no cover - threads build only
        pass

    try:
        import numpy as np  # noqa: F401  (import side effect initialises BLAS)

        # numpy/OpenBLAS read the env var at import; re-import is unnecessary.
        # But some builds cache the count in a C global, so also try the
        # threadpoolctl path if the package is present.
        try:
            from threadpoolctl import threadpool_limits  # pragma: no cover

            threadpool_limits(limits=budget)
        except Exception:  # pragma: no cover - optional dependency
            pass
    except Exception:  # pragma: no cover
        pass

    logger.info("Numerical thread budget set to %d per process", budget)
    return budget
