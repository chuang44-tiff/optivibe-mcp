"""_bootstrap.py — ZOS-API assembly bootstrap, factored out of boot_smoke.py.

Mirrors ``scripts/boot_smoke.py`` (the probe) so session + live test share ONE
NetHelper-first bootstrap: resolve ``ZOSAPI_NetHelper.dll`` without hardcoding,
resolve the OpticStudio install dir (env -> registry -> Program-Files scan),
``AddReference`` the three assemblies, and ``import ZOSAPI``.

The pythonnet runtime selector (``PYTHONNET_RUNTIME=netfx``) must be set BEFORE
``import clr`` because the ZOS-API assemblies target the .NET Framework. So this
module does NOT import ``clr`` at top level — a clr-less unit run can import this
module (and call the pure-path-resolution helpers) freely; ``load_zosapi()`` does
the ``clr`` work LAZILY when first called, and is idempotent (a 2nd call returns
the already-loaded ``ZOSAPI`` module without re-referencing assemblies).

Live ZOS-API integration: ``load_zosapi()`` is exercised by the live test;
the path-resolution helpers are unit-tested with a mocked filesystem / env.
"""
import glob
import os

# Set the pythonnet runtime selector at import time so it is in place before any
# later ``import clr`` (mirrors boot_smoke.py). ``setdefault`` respects an
# explicit override and is import-safe even without pythonnet installed.
os.environ.setdefault("PYTHONNET_RUNTIME", "netfx")

# Memo for idempotent load: once ZOSAPI is referenced + imported, reuse it.
_ZOSAPI_MODULE = None


def find_nethelper() -> str:
    """Resolve ``ZOSAPI_NetHelper.dll`` without hardcoding an install path.

    Order: explicit ``ZOSAPI_NETHELPER`` env override -> the documented per-user
    ZOS-API Libraries folder under Documents/Zemax -> a bounded recursive fallback
    search there. Raises ``FileNotFoundError`` if none is found.
    """
    override = os.environ.get("ZOSAPI_NETHELPER")
    if override and os.path.isfile(override):
        return override
    docs = os.path.join(
        os.path.expanduser("~"), "Documents", "Zemax", "ZOS-API", "Libraries"
    )
    candidate = os.path.join(docs, "ZOSAPI_NetHelper.dll")
    if os.path.isfile(candidate):
        return candidate
    root = os.path.join(os.path.expanduser("~"), "Documents", "Zemax")
    hits = glob.glob(os.path.join(root, "**", "ZOSAPI_NetHelper.dll"), recursive=True)
    if hits:
        return hits[0]
    raise FileNotFoundError(
        "ZOSAPI_NetHelper.dll not found; set the ZOSAPI_NETHELPER env var to its path."
    )


def resolve_zemax_dir(initializer):
    """Resolve the OpticStudio install dir (holding ZOSAPI.dll) without hardcoding.

    Order: explicit ``ZEMAX_DIR`` override -> registry auto-detect (no-arg
    ``Initialize``) -> scan Program Files for the newest-named install containing
    ``ZOSAPI.dll``, then ``Initialize(path)`` with it. Returns ``(dir, how)``.
    Raises ``RuntimeError`` if no install dir with ``ZOSAPI.dll`` is found.
    """
    def has_dll(d):
        return bool(d) and os.path.isfile(os.path.join(d, "ZOSAPI.dll"))

    override = os.environ.get("ZEMAX_DIR")
    if has_dll(override):
        initializer.Initialize(override)
        return override, "env:ZEMAX_DIR"

    if initializer.Initialize():
        d = initializer.GetZemaxDirectory()
        if has_dll(d):
            return d, "registry-autodetect"

    candidates = []
    for base in (
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    ):
        candidates += glob.glob(os.path.join(base, "*Zemax OpticStudio*"))
    candidates = sorted((c for c in candidates if has_dll(c)), reverse=True)
    if candidates:
        d = candidates[0]
        initializer.Initialize(d)
        return d, "program-files-scan"

    raise RuntimeError(
        "Could not resolve an OpticStudio install dir containing ZOSAPI.dll."
    )


def load_zosapi():
    """Load + return the ``ZOSAPI`` module (idempotent; lazy ``clr`` import).

    First call: ``import clr`` (after the runtime selector is set), AddReference
    NetHelper -> resolve the install dir -> AddReference ZOSAPI.dll +
    ZOSAPI_Interfaces.dll -> ``import ZOSAPI``. Subsequent calls return the
    already-loaded module without re-referencing assemblies.
    """
    global _ZOSAPI_MODULE
    if _ZOSAPI_MODULE is not None:
        return _ZOSAPI_MODULE

    # Imported lazily so a clr-less unit run can still import this module.
    import clr  # noqa: E402 — import after PYTHONNET_RUNTIME is set

    nethelper = find_nethelper()
    clr.AddReference(nethelper)
    import ZOSAPI_NetHelper  # noqa: E402

    zemax_dir, _how = resolve_zemax_dir(ZOSAPI_NetHelper.ZOSAPI_Initializer)
    clr.AddReference(os.path.join(zemax_dir, "ZOSAPI.dll"))
    clr.AddReference(os.path.join(zemax_dir, "ZOSAPI_Interfaces.dll"))
    import ZOSAPI  # noqa: E402

    _ZOSAPI_MODULE = ZOSAPI
    return ZOSAPI
