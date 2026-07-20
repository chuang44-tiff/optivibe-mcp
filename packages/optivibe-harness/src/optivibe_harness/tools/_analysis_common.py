"""tools/_analysis_common.py — private shared helpers for the analysis tools.

NOT dispatchable (no ``TOOL_SPEC``). These are the building blocks every
results-extraction tool reuses so the probe-grounded marshalling + lifecycle
rules live in exactly one place (mirrors ``_lens_common`` for the lens tools):

- ``_marshal_1d`` — read a .NET ``System.Double[]`` (rank 1) to ``list[float]``
  via ``arr.Length`` + ``arr[i]``; EVERY element through ``_io.safe_float``.
- ``_marshal_2d`` — read a .NET ``System.Double[,]`` (rank 2) to ``list[list]``
  via ``GetLength(0)``/``GetLength(1)`` + ``arr[i, j]`` (TUPLE index, NEVER
  ``arr[i][j]``); EVERY cell through ``safe_float``.
  Convenience ``_marshal_2d_columns`` splits a [rows, 2] array into the
  col0=tangential / col1=sagittal pair the MTF tool returns (fixed ordering).
- ``_marshal_array`` — rank dispatch (Rank==1 -> 1-D, Rank==2 -> 2-D); a None or
  empty array (length 0, OR a rank-2 [rows, 0] / [0, cols]) raises
  ``AnalysisResultError(family="analysis_empty")`` (the canary for a silent void
  result). ``_split_mtf_columns`` splits the marshalled [rows, 2]
  result into the col0=tangential / col1=sagittal pair AFTER the canary fired
  (the path get_mtf uses so XData/YData never bypass the empty guard).
- ``_run_analysis`` — a context manager that runs ``ApplyAndWaitForCompletion`` ->
  ``GetResults`` and ALWAYS ``Close()``s the analysis window in ``finally`` (the
  L22 analog for an analysis window).
- ``_lens_units_string`` — derive the linear/lens-unit string from
  ``system.SystemData.Units.LensUnits`` (SpotData carries NO unit member; the unit
  is derived here, never read off the result object).
- ``error_envelope`` — build the ``{ok:false, error_family, error, tool}``
  failure dict every analysis tool returns for an EXPECTED failure (it does NOT
  raise ``AnalysisResultError`` past its own boundary; it constructs the envelope).

Live ZOS-API integration: exercised by the live test; unit-tested here against
fixture-seeded fakes reproducing the probe shapes.
"""
from contextlib import contextmanager

from .._io import safe_float
from ..errors import AnalysisResultError

# Lens-unit enum member name -> the unit string the analysis tools report. The baseline
# Cooke reads in millimetres (mm) for linear quantities; the spot radius is
# reported in micrometres (um) for that file (§6). LensUnits is the .NET
# enum on ``SystemData.Units`` whose member str() is one of these names.
_LINEAR_UNIT_BY_LENS_UNIT = {
    "Millimeters": "mm",
    "Centimeters": "cm",
    "Inches": "in",
    "Meters": "m",
}
# The spot-radius convention: OpticStudio reports the standard-spot radius in
# micrometres for the baseline mm lens (§6). The spot tool returns the
# matched spot-radius unit string; the tolerance battery asserts against um.
_SPOT_UNIT_BY_LENS_UNIT = {
    "Millimeters": "um",
    "Centimeters": "um",
    "Inches": "um",
    "Meters": "um",
}


def _marshal_1d(arr):
    """Marshal a .NET ``System.Double[]`` (rank 1) to ``list[float]``.

    Uses ``arr.Length`` for the count and ``arr[i]`` element indexing (a 1-D
    managed array indexes positionally). EVERY element passes
    through ``_io.safe_float`` so a non-finite cell (inf/nan) becomes a string
    sentinel and the JSON wire stays strict (§c).
    """
    n = int(arr.Length)
    return [safe_float(arr[i]) for i in range(n)]


def _marshal_2d(arr):
    """Marshal a .NET ``System.Double[,]`` (rank 2) to ``list[list[float]]``.

    Uses ``arr.GetLength(0)`` (rows) / ``arr.GetLength(1)`` (cols) for the shape
    and ``arr[i, j]`` — a TUPLE index, NEVER ``arr[i][j]`` (a 2-D managed array
    has no row sub-arrays to chain-index). EVERY cell passes
    through ``safe_float``.
    """
    rows = int(arr.GetLength(0))
    cols = int(arr.GetLength(1))
    return [[safe_float(arr[i, j]) for j in range(cols)] for i in range(rows)]


def _marshal_2d_columns(arr):
    """Split a rank-2 [rows, 2] array into (col0, col1) lists.

    For the MTF Y array, col0 = Tangential, col1 = Sagittal (fixed ordering).
    Returns ``(col0_list, col1_list)``. Requires exactly 2 columns;
    a different column count raises ``AnalysisResultError(family="analysis")`` (an
    unexpected shape is a parity break, not a silent truncation).
    """
    rows = int(arr.GetLength(0))
    cols = int(arr.GetLength(1))
    if cols != 2:
        raise AnalysisResultError(
            f"expected a 2-column Y array (tangential, sagittal); got {cols} cols",
            family="analysis",
        )
    col0 = [safe_float(arr[i, 0]) for i in range(rows)]
    col1 = [safe_float(arr[i, 1]) for i in range(rows)]
    return col0, col1


def _split_mtf_columns(rows):
    """Split an already-marshalled rank-2 [rows, 2] list into (col0, col1).

    For the MTF Y array, col0 = Tangential, col1 = Sagittal (fixed ordering).
    ``rows`` is the ``list[list[float]]`` from ``_marshal_array``
    (which already carries the None/empty ``analysis_empty`` canary). Requires
    exactly 2 columns; a different column count raises
    ``AnalysisResultError(family="analysis_malformed")`` (an unexpected shape is a
    parity break, not a silent truncation) — the SAME family the length-mismatch
    guard in ``get_mtf`` emits, so a malformed Y surfaces as one structured
    ``analysis_malformed`` envelope regardless of which shape canary fired.
    The caller wraps this in the same ``try/except AnalysisResultError``
    as the marshalling calls so it can never escape past the tool boundary. An
    empty ``rows`` yields two empty lists (the empty canary already fired upstream
    in ``_marshal_array``).
    """
    cols = len(rows[0]) if rows else 2
    if cols != 2:
        raise AnalysisResultError(
            f"expected a 2-column Y array (tangential, sagittal); got {cols} cols",
            family="analysis_malformed",
        )
    col0 = [r[0] for r in rows]
    col1 = [r[1] for r in rows]
    return col0, col1


def _rank_of(arr):
    """Best-effort .NET array rank (``arr.Rank``); None if not introspectable."""
    rank = getattr(arr, "Rank", None)
    if rank is None:
        return None
    try:
        return int(rank)
    except Exception:  # noqa: BLE001 — a non-int Rank is not a usable rank
        return None


def _marshal_array(arr):
    """Rank-dispatch marshal of a 1-D or 2-D managed array; canary on empty.

    - ``arr is None`` -> ``AnalysisResultError(family="analysis_empty")`` (the
      silent-void canary, §d).
    - rank 1 -> ``_marshal_1d``; rank 2 -> ``_marshal_2d``.
    - a rank-1 array of length 0 -> ``analysis_empty`` (an empty data array is a
      void result the canary intercepts).
    - a rank-2 array with 0 rows OR 0 cols -> ``analysis_empty`` (a ``[rows, 0]``
      void result is rejected too).
    - any other rank -> ``AnalysisResultError(family="analysis")``.
    """
    if arr is None:
        raise AnalysisResultError(
            "analysis produced no data array (None)", family="analysis_empty"
        )
    rank = _rank_of(arr)
    if rank == 1:
        n = int(arr.Length)
        if n == 0:
            raise AnalysisResultError(
                "analysis produced an empty data array (length 0)",
                family="analysis_empty",
            )
        return _marshal_1d(arr)
    if rank == 2:
        rows = int(arr.GetLength(0))
        cols = int(arr.GetLength(1))
        # An empty array slips through if it has 0 rows OR 0 cols ([rows, 0] is a
        # void result that a rows-only guard would marshal as N empty rows). Reject
        # BOTH dimensions.
        if rows == 0 or cols == 0:
            raise AnalysisResultError(
                f"analysis produced an empty data array ([{rows}, {cols}])",
                family="analysis_empty",
            )
        return _marshal_2d(arr)
    raise AnalysisResultError(
        f"unsupported array rank {rank!r} (expected 1 or 2)", family="analysis"
    )


@contextmanager
def _run_analysis(analysis):
    """Run an analysis window and ALWAYS Close() it (the L22 analog).

    Drives ``ApplyAndWaitForCompletion()`` (returns void — NOT a success signal,
    §d) then yields ``GetResults()``. The analysis window is ``Close()``d
    in a ``finally`` so a window is never leaked even on a mid-handler raise. The
    Close() is guarded so a teardown failure never masks the handler's outcome.
    """
    try:
        analysis.ApplyAndWaitForCompletion()
        results = analysis.GetResults()
        yield results
    finally:
        try:
            analysis.Close()
        except Exception:  # noqa: BLE001 — window teardown must never raise
            pass


def _units_member(system):
    """Return the live ``SystemData.Units.LensUnits`` enum member (or None).

    The lens unit is the single source of truth for every linear/spot unit string
    (§6: SpotData has NO unit member). Guarded so a missing
    member degrades to ``None`` (the caller maps that to an ``"unknown"`` string)
    rather than raising — a unit-derivation gap must not crash a results read.
    """
    try:
        return system.SystemData.Units.LensUnits
    except Exception:  # noqa: BLE001 — degrade to unknown rather than crash
        return None


def _lens_units_string(system):
    """Derive the LINEAR (X/Y/Z/opd, frequency-base) unit string from the lens unit.

    Reads ``SystemData.Units.LensUnits`` (§6); maps its
    member name (Millimeters/Centimeters/Inches/Meters) to mm/cm/in/m. An
    unknown/absent member yields ``"unknown"`` (never raises).
    """
    member = _units_member(system)
    if member is None:
        return "unknown"
    name = str(member)
    return _LINEAR_UNIT_BY_LENS_UNIT.get(name, name)


def _spot_units_string(system):
    """Derive the SPOT-RADIUS unit string from the lens unit (§6).

    SpotData exposes no unit member, so the spot-radius unit is
    derived from ``SystemData.Units.LensUnits`` here — micrometres (``"um"``) for
    the baseline mm lens. An unknown/absent member yields ``"unknown"``.
    """
    member = _units_member(system)
    if member is None:
        return "unknown"
    name = str(member)
    return _SPOT_UNIT_BY_LENS_UNIT.get(name, name)


def error_envelope(tool, family, message, **extra):
    """Build the expected-failure envelope (never raised past boundary).

    ``{"ok": False, "error_family": family, "error": message, "tool": tool}`` plus
    any tool-specific diagnostic fields in ``extra`` (the §a error-return
    convention). A handler constructs this for an EXPECTED failure class; it does
    NOT raise ``AnalysisResultError`` into dispatch for those.
    """
    env = {"ok": False, "error_family": family, "error": message, "tool": tool}
    env.update(extra)
    return env
