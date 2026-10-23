"""_probe_common.py — the shared plumbing of the two live operand probes.

``probe_operands`` (MeritOperandType) and ``probe_tolerances`` (ToleranceOperandType)
each boot a headless OpticStudio, dump an enum surface to the captures dir, and reap the
session. This module holds what they share: loading pythonnet, resolving the ZOS-API
assemblies without a hardcoded install path, the never-raise ``capture`` helper, and the
JSON writer.

Importing this module (or either probe) needs NO pythonnet: ``clr`` is imported inside
``load_clr`` only, and the ``PYTHONNET_RUNTIME`` selector is set there too, so the
harness publication suite (which imports every shipped module) never touches .NET.
It imports nothing from optivibe_harness (zero cross-package coupling).
"""
import glob
import json
import os

from . import _paths

# The install hint a missing pythonnet names: the probes are an OPTIONAL extra.
PROBE_EXTRA_HINT = 'install optivibe-reference[probe] (pip install "optivibe-reference[probe]")'


class PythonnetMissing(ImportError):
    """pythonnet (``clr``) is not importable, so no live probe can run."""


def load_clr():
    """Select the .NET Framework runtime, then import and return ``clr``.

    The runtime selector MUST be set before ``import clr``. Raises ``PythonnetMissing``
    (an ``ImportError``) naming the ``[probe]`` extra when pythonnet is absent.
    """
    os.environ.setdefault("PYTHONNET_RUNTIME", "netfx")
    try:
        import clr  # noqa: E402  (pythonnet; after the runtime selector is set)
    except ImportError as exc:
        raise PythonnetMissing(
            "pythonnet is not installed, so the live probes cannot run - "
            + PROBE_EXTRA_HINT) from exc
    return clr


def out_dir():
    """Where the probes write: the captures dir (``_paths.captures_dir``)."""
    return _paths.captures_dir()


def find_nethelper():
    """Resolve ZOSAPI_NetHelper.dll without hardcoding an install path."""
    override = os.environ.get("ZOSAPI_NETHELPER")
    if override and os.path.isfile(override):
        return override
    docs = os.path.join(os.path.expanduser("~"), "Documents", "Zemax", "ZOS-API", "Libraries")
    candidate = os.path.join(docs, "ZOSAPI_NetHelper.dll")
    if os.path.isfile(candidate):
        return candidate
    root = os.path.join(os.path.expanduser("~"), "Documents", "Zemax")
    hits = glob.glob(os.path.join(root, "**", "ZOSAPI_NetHelper.dll"), recursive=True)
    if hits:
        return hits[0]
    raise FileNotFoundError(
        "ZOSAPI_NetHelper.dll not found; set ZOSAPI_NETHELPER to its path."
    )


def resolve_zemax_dir(initializer):
    """Resolve the OpticStudio install dir (holding ZOSAPI.dll) without hardcoding."""
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
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        candidates += glob.glob(os.path.join(base, "*Zemax OpticStudio*"))
    candidates = sorted((c for c in candidates if has_dll(c)), reverse=True)
    if candidates:
        d = candidates[0]
        initializer.Initialize(d)
        return d, "program-files-scan"
    raise RuntimeError("Could not resolve an OpticStudio install dir with ZOSAPI.dll.")


def load_zosapi(clr):
    """Reference the NetHelper + ZOS-API assemblies; return the ``ZOSAPI`` namespace."""
    nethelper = find_nethelper()
    print(f"[probe] NetHelper: {nethelper}")
    clr.AddReference(nethelper)
    import ZOSAPI_NetHelper  # noqa: E402

    zemax_dir, how = resolve_zemax_dir(ZOSAPI_NetHelper.ZOSAPI_Initializer)
    print(f"[probe] Zemax directory: {zemax_dir}  (via {how})")
    clr.AddReference(os.path.join(zemax_dir, "ZOSAPI.dll"))
    clr.AddReference(os.path.join(zemax_dir, "ZOSAPI_Interfaces.dll"))
    import ZOSAPI  # noqa: E402
    return ZOSAPI


def enum_members(enum_type):
    """Member names of a .NET enum via System.Enum.GetNames (the live truth)."""
    from System import Enum  # noqa: E402
    return [str(n) for n in Enum.GetNames(enum_type)]


def public_members(obj):
    """Non-underscore attribute names on a live object (defensive discovery)."""
    try:
        return sorted(n for n in dir(obj) if not n.startswith("_"))
    except Exception as exc:  # noqa: BLE001
        return f"<unavailable: {type(exc).__name__}: {exc}>"


def capture(fn):
    """Call fn(), returning its value or a recorded failure string (never raises)."""
    try:
        v = fn()
        # keep JSON-serializable
        if isinstance(v, (str, int, float, bool)) or v is None:
            return v
        return str(v)
    except Exception as exc:  # noqa: BLE001
        return f"<err: {type(exc).__name__}: {exc}>"[:200]


def write_json(name, payload):
    """Write ``payload`` to ``<captures>/<name>`` (dir created on write); return the path."""
    target = out_dir()
    os.makedirs(target, exist_ok=True)
    path = os.path.join(target, name)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")
    print(f"[probe] wrote {path}")
    return path
