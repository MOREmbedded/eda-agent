# SPDX-License-Identifier: Apache-2.0
"""Which implementation runs the hot loops: Python, or the Rust wheel.

eda-agent is pure Python and stays that way. The optional
``eda-agent-compute`` wheel (``rust/eda_agent_compute``) carries compiled
ports of the loops that measured as CPU-bound; today that is the grid A*
of :mod:`eda_agent.route.router`. See docs/RUST_COMPUTE.md.

The choice is ``EDA_AGENT_COMPUTE`` (or ``eda-agent --compute``):

``python`` (default)
    Always the Python code. Nothing changes for anyone who does not ask.
``rust``
    Always the compiled code. If the wheel is missing, too old or too new,
    the call fails and says why. Asking for Rust and silently getting
    Python would make every timing and every comparison a lie.
``auto``
    The compiled code when a matching wheel is installed, Python otherwise.

The setting is read on every call, not cached at import, so a test or a
tool can switch it per call and a child process inherits it.

Both implementations must return the same answer. That is not a hope:
tests/route/test_compute_parity.py runs the router both ways and compares
paths, verdicts and expansion counts.
"""

from __future__ import annotations

import os
from typing import Any

ENV_VAR = "EDA_AGENT_COMPUTE"
MODES = ("python", "rust", "auto")
DEFAULT_MODE = "python"

#: The kernel contract this Python code was written against. Must equal
#: the wheel's ``API_VERSION``; anything else is treated as not installed.
EXPECTED_API = 1


class ComputeUnavailable(RuntimeError):
    """``rust`` was asked for and the wheel cannot serve it."""


def requested_mode() -> str:
    """The mode in force. An unknown value is an error, not a default:
    a typo in ``rust`` must not quietly benchmark Python."""
    raw = os.environ.get(ENV_VAR, "").strip().lower()
    if not raw:
        return DEFAULT_MODE
    if raw not in MODES:
        raise ValueError(
            f"{ENV_VAR}={raw!r}: expected one of {', '.join(MODES)}")
    return raw


_probe: tuple[Any, str] | None = None


def _load() -> tuple[Any, str]:
    """(module or None, reason it is unusable or ''). Probed once."""
    global _probe
    if _probe is None:
        try:
            import eda_agent_compute as mod  # type: ignore[import-not-found]
        except ImportError as exc:
            _probe = (None, f"eda-agent-compute is not installed ({exc})")
        else:
            api = getattr(mod, "API_VERSION", None)
            if api != EXPECTED_API:
                _probe = (None, (
                    f"eda-agent-compute {getattr(mod, '__version__', '?')} "
                    f"speaks API {api}, this eda-agent needs {EXPECTED_API}; "
                    "rebuild it from rust/eda_agent_compute"))
            else:
                _probe = (mod, "")
    return _probe


def native() -> Any | None:
    """The compiled module to use for this call, or None for Python.

    Raises :class:`ComputeUnavailable` when ``rust`` is requested and the
    wheel cannot serve it.
    """
    mode = requested_mode()
    if mode == "python":
        return None
    mod, why = _load()
    if mod is None and mode == "rust":
        raise ComputeUnavailable(
            f"{ENV_VAR}=rust but {why}. Install it with "
            "`pip install ./rust/eda_agent_compute` (needs a Rust toolchain), "
            "or set the mode to auto or python.")
    return mod


def active() -> str:
    """'rust' or 'python': what :func:`native` would pick now. For reports,
    so a result says which implementation produced it."""
    return "rust" if native() is not None else "python"


def status() -> dict[str, Any]:
    """Everything a person needs to see why a mode does what it does."""
    try:
        mode = requested_mode()
        mode_error = ""
    except ValueError as exc:
        mode, mode_error = "", str(exc)
    mod, why = _load()
    out: dict[str, Any] = {
        "requested": mode,
        "rust_available": mod is not None,
        "rust_version": getattr(mod, "__version__", None) if mod else None,
        "expected_api": EXPECTED_API,
    }
    if why:
        out["rust_unavailable_reason"] = why
    if mode_error:
        out["error"] = mode_error
    else:
        if mode == "rust" and mod is None:
            # Nothing runs: rust refuses rather than fall back.
            out["active"] = "none"
            out["error"] = f"{ENV_VAR}=rust but {why}"
        else:
            out["active"] = "rust" if (mode != "python" and mod) else "python"
    return out
