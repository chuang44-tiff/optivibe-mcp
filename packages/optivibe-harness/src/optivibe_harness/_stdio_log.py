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


def _open_log_fd() -> int:
    """Open a log file fd for the redirected stdio.  devnull on failure.

    Precedence: ``OPTIVIBE_WORKSPACE_ROOT`` -> ``OPTIVIBE_LOG_DIR`` -> cwd.
    ``O_APPEND`` so a re-open never truncates a prior session's log. When every
    named path fails, the devnull fallback (never blocks, accepts data loss) keeps
    the never-fatal contract — a swap to devnull = no file, degraded-safe.
    """
    for base in (
        os.environ.get("OPTIVIBE_WORKSPACE_ROOT"),
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
