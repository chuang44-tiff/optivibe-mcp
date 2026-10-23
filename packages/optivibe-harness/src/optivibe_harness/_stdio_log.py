"""_stdio_log.py — the ONE resolver for the redirected-stdio log file (L30).

lazy-boot-logging (LOCKED 2026-06-26): the OptiVibe MCP launches on EVERY Claude
Code session (registered globally), so it must NOT litter the launch cwd with a log
file when the user never touches OpticStudio. The fd-1/2 redirect at boot now targets
``devnull`` (pipe protection without a file); the real log FILE is
opened LAZILY, only when the engine is first opened ("Zemax pinged").

Both call sites share this ONE resolver so the file-path logic never drifts:
- ``__main__`` re-exports these names (existing tests import them from ``__main__``);
- ``lazy.LazyHarnessDispatcher._activate_logging`` opens the file via ``_open_log_fd``
  on the first engine-open.

Live ZOS-API integration: N/A (pure stdlib fd/path resolution; no backend).
"""
import os

_LOG_FILENAME = "optivibe_stdio.log"

# The absolute workspace root ``__main__`` anchored once at boot (None until pinned). The
# log is opened lazily at the first engine-open, possibly after a chdir, so a relative
# ``OPTIVIBE_WORKSPACE_ROOT`` must not be re-resolved against the cwd of that later moment.
_ANCHORED_ROOT = None


def pin_workspace_root(root) -> None:
    """Record the boot-anchored absolute workspace root (a non-empty ``str``; else ignored)."""
    global _ANCHORED_ROOT
    if isinstance(root, str) and root:
        _ANCHORED_ROOT = root


def _workspace_base():
    """The boot-anchored root, else the env value made absolute now; ``None`` if neither
    is usable (unset, or a relative value with a deleted cwd). NEVER raises. With the env
    var unset the pin (boot's cwd) is not used, so ``OPTIVIBE_LOG_DIR`` keeps its precedence."""
    raw = os.environ.get("OPTIVIBE_WORKSPACE_ROOT")
    if not raw:
        return None
    if _ANCHORED_ROOT:
        return _ANCHORED_ROOT
    try:
        return os.path.abspath(raw)
    except (OSError, ValueError):  # a deleted cwd makes a relative abspath raise
        return None


def _open_log_fd() -> int:
    """Open a log file fd for the redirected stdio.  devnull on failure.

    Precedence: the boot-anchored ``OPTIVIBE_WORKSPACE_ROOT`` -> ``OPTIVIBE_LOG_DIR`` -> cwd.
    ``O_APPEND`` so a re-open never truncates a prior session's log. When every
    named path fails, the devnull fallback (never blocks, accepts data loss) keeps
    the never-fatal contract — a swap to devnull = no file, degraded-safe.
    """
    for base in (
        _workspace_base(),
        os.environ.get("OPTIVIBE_LOG_DIR"),
    ):
        if base:
            try:
                path = os.path.join(base, _LOG_FILENAME)
                return os.open(
                    path,
                    os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                    0o644,
                )
            except OSError:
                pass
    # cwd fallback
    try:
        return os.open(
            _LOG_FILENAME,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o644,
        )
    except OSError:
        pass
    # devnull: never block, accept data loss
    return os.open(os.devnull, os.O_WRONLY)
