# SPDX-License-Identifier: Apache-2.0
"""One BLAS thread for the sparse solves.

SciPy's sparse LU calls BLAS on small blocks. OpenBLAS sizes its thread
pool from the machine's cores, not from the cores the process may use, so
with several solves at once (the test suite with -n 4, the dashboard and
an assistant side by side) the threads fight and a two-second solve took
ten minutes. One thread per solve is both faster and predictable.
"""
from __future__ import annotations

import contextlib


def single_blas_thread():
    try:
        from threadpoolctl import threadpool_limits
    except ImportError:  # pragma: no cover - a core dependency
        return contextlib.nullcontext()
    return threadpool_limits(limits=1, user_api="blas")
