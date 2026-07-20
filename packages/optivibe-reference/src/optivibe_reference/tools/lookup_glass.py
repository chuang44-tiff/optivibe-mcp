"""tools/lookup_glass.py — exact glass lookup (session-free, never-raise).

The typed, DB-backed, never-raise glass tool. Handler signature
``lookup_glass(db_conn, params)`` — arg-0 is the GLASS catalog connection the ref
``Dispatcher`` threads positionally by ``spec.kind=='glass'`` (the forward analog
of the operand tool). It answers from THAT connection, never a module-global.

Mirrors ``lookup_operand``'s never-raise contract (§7):
parameterized SQL, byte-exact name match (NO ``COLLATE NOCASE`` — ``'n-bk7'`` is
``glass_unknown``), ``ToolParamError`` raised with the bool-before-float trap
checked FIRST (``True == 1.0`` would otherwise slip a bool ``wavelength`` through).

Resolution (§7):
- ``name`` + ``catalog`` -> exact PK (miss -> ``glass_unknown``);
- ``name`` only, unique -> resolve;
- ``name`` only, multi-catalog -> ``ambiguous_glass`` (candidates list, NEVER a
  silent vendor pick);
- empty / blank ``name`` -> ``glass_unknown``.

Wavelength (§7): an optional ``wavelength`` (float µm) OUTSIDE ``[min_wave,
max_wave]`` -> ``wavelength_out_of_range`` (refuse to extrapolate); in-band ->
``index_at_wavelength`` added to the success payload. If no index can be
computed (``index_at`` -> None for an in-band wavelength — the formula number is
unknown or the coefficient data is invalid) the tool returns a typed
``index_unavailable`` envelope rather than an
``ok:True`` payload carrying a null index (GLASS-2 item 1). Therefore a success
payload's ``index_at_wavelength`` is ALWAYS non-null when ``wavelength`` is given.

Out-of-band rows return the FULL key set with null/false validity fields (shape
parity) — the cardinal-failure defense surfaces at the tool boundary too.
"""
import json
import math

from .._envelope import error_envelope
from ..errors import ToolParamError
from ..glass_build import in_band
from ..glass_dispersion import index_at
from ..server import ToolSpec
from ._glass_common import normalize_catalog

_TOOL = "lookup_glass"

# The glass-row columns selected on a hit, in a stable order. ``_columns`` (the
# SELECT list) and ``_row_to_dict`` (the keys it zips) BOTH derive from this tuple
# so the SELECT order and the dict keys can never drift (mirror lookup_operand L-1).
GLASS_COLUMNS = (
    "catalog",
    "name",
    "formula",
    "formula_name",
    "cd",
    "min_wave",
    "max_wave",
    "nd_stored",
    "vd_stored",
    "delta_pg_f",
    "nd",
    "ng",
    "nf",
    "nc",
    "vd",
    "pg_f",
    "nd_valid",
    "pg_f_valid",
)


def _columns():
    return ", ".join(GLASS_COLUMNS)


def _row_to_dict(row):
    return dict(zip(GLASS_COLUMNS, row))


def _parse_cd(cd_json):
    """Parse the stored ``cd`` JSON to a list (``[]`` when NULL/garbage)."""
    if not cd_json:
        return []
    try:
        parsed = json.loads(cd_json)
    except (ValueError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _validate_wavelength(wavelength):
    """Validate the optional ``wavelength`` param (bool-before-float trap FIRST).

    A ``bool`` raises ``ToolParamError`` BEFORE the float check (``True == 1.0``
    would otherwise slip through — isinstance(bool) FIRST). A non-numeric value
    raises too. Returns the float (or None when absent).
    """
    if wavelength is None:
        return None
    if isinstance(wavelength, bool):
        raise ToolParamError("'wavelength' must be a number, not a bool")
    if not isinstance(wavelength, (int, float)):
        raise ToolParamError(
            f"'wavelength' must be a number; got {type(wavelength).__name__}"
        )
    # L-1: NaN/inf are structurally invalid input, NOT an out-of-band wavelength. A
    # non-finite value passes the isinstance(float) check and would later fail the
    # band test, misclassifying as wavelength_out_of_range. Reject it as tool_param.
    if not math.isfinite(wavelength):
        raise ToolParamError("'wavelength' must be a finite number")
    return float(wavelength)


def _select_rows(db_conn, name, catalog):
    """Select glass rows by byte-exact ``name`` (+ optional case-insensitive catalog).

    Name match is byte-exact (NO COLLATE NOCASE): ``'n-bk7' != 'N-BK7'``. The
    catalog (when given) is matched case-insensitively against the stored value so
    ``'SCHOTT.agf'`` resolves ``'SCHOTT.AGF'``. Parameterized SQL throughout.
    """
    if catalog is not None:
        rows = db_conn.execute(
            f"SELECT {_columns()} FROM glass "
            "WHERE name = ? AND LOWER(catalog) = LOWER(?) ORDER BY catalog",
            (name, catalog),
        ).fetchall()
    else:
        rows = db_conn.execute(
            f"SELECT {_columns()} FROM glass WHERE name = ? ORDER BY catalog",
            (name,),
        ).fetchall()
    # Defensive byte-exactness on the name: SQLite ``=`` on a TEXT column is
    # case-sensitive by default, but assert the exact byte match so a future
    # COLLATE NOCASE on the column can never silently route 'n-bk7' to 'N-BK7'.
    return [r for r in rows if r[1] == name]


def _success_payload(d):
    """Build the FULL-key-set success payload (out-of-band rows carry nulls)."""
    return {
        "ok": True,
        "catalog": d["catalog"],
        "name": d["name"],
        "formula": d["formula"],
        "formula_name": d["formula_name"],
        "cd": _parse_cd(d["cd"]),
        "min_wave": d["min_wave"],
        "max_wave": d["max_wave"],
        "nd_stored": d["nd_stored"],
        "vd_stored": d["vd_stored"],
        "delta_pg_f": d["delta_pg_f"],
        "nd": d["nd"],
        "ng": d["ng"],
        "nf": d["nf"],
        "nc": d["nc"],
        "vd": d["vd"],
        "pg_f": d["pg_f"],
        "nd_valid": bool(d["nd_valid"]),
        "pg_f_valid": bool(d["pg_f_valid"]),
        "match_kind": "exact",
    }


def lookup_glass(db_conn, params):
    """Resolve a glass name onto its recomputed dispersion row (never raises).

    See module docstring for the resolution + wavelength contract.
    """
    # --- param validation (raises -> dispatch classifies as tool_param) ---
    if "name" not in params:
        raise ToolParamError("missing required param 'name'")
    name = params["name"]
    if not isinstance(name, str):
        raise ToolParamError(f"'name' must be a string; got {type(name).__name__}")

    catalog = normalize_catalog(params.get("catalog"))
    wavelength = _validate_wavelength(params.get("wavelength"))

    # An empty / blank name can never be a stored glass name (§7).
    if not name.strip():
        return error_envelope(_TOOL, "glass_unknown", "empty name", name=name)

    rows = _select_rows(db_conn, name, catalog)
    if not rows:
        return error_envelope(
            _TOOL, "glass_unknown", "no glass matched the name",
            name=name, catalog=catalog,
        )

    # Multi-catalog collision with no catalog disambiguation -> NEVER a silent
    # vendor pick; return the candidate list (§7 / D).
    if catalog is None and len(rows) > 1:
        candidates = [
            {"catalog": r[0], "name": r[1]} for r in rows
        ]
        return error_envelope(
            _TOOL, "ambiguous_glass",
            "name resolves in multiple catalogs; pass a catalog",
            name=name, candidates=candidates, candidate_count=len(candidates),
        )

    d = _row_to_dict(rows[0])

    # Wavelength: refuse to extrapolate outside the formula's valid band (§7).
    if wavelength is not None and not in_band(d["min_wave"], d["max_wave"], wavelength):
        return error_envelope(
            _TOOL, "wavelength_out_of_range",
            "wavelength outside the catalog [min_wave, max_wave] band",
            name=d["name"], catalog=d["catalog"], wavelength=wavelength,
            min_wave=d["min_wave"], max_wave=d["max_wave"],
        )

    payload = _success_payload(d)
    if wavelength is not None:
        # Compute the index FIRST: index_at returns None when the row's dispersion
        # formula number is unknown (not in the DISPERSION dispatch) or the
        # n(lambda) compute fails (e.g. a too-short CD coefficient list = malformed
        # data, where the formula IS implemented). Returning ok:True carrying
        # index_at_wavelength:None is a silent null inside a success envelope
        # (GLASS-2 item 1) — refuse it with a TYPED index_unavailable envelope
        # instead. When the index is non-None the success payload carries it
        # unchanged.
        index = index_at(d["formula"], _parse_cd(d["cd"]), wavelength)
        if index is None:
            return error_envelope(
                _TOOL, "index_unavailable",
                "in-band wavelength but no index could be computed for this glass "
                "— the dispersion formula is unknown or the coefficient data is "
                "invalid",
                name=d["name"], catalog=d["catalog"],
                formula=d["formula"], formula_name=d["formula_name"],
                wavelength=wavelength,
            )
        payload["wavelength"] = wavelength
        payload["index_at_wavelength"] = index
    return payload


LOOKUP_GLASS_SPEC = ToolSpec(
    name="lookup_glass",
    handler=lookup_glass,
    required_params=("name",),
    description=(
        "Get a glass's optical data by name - refractive index Nd, Abbe number "
        "Vd, cardinal-line indices, and relative partial dispersion Pg,F (with "
        "manufacturer delta-Pg,F), plus per-field validity flags. Pass an optional "
        "wavelength in um for the index at that wavelength. Gotcha: a bare name "
        "that collides across catalogs returns an ambiguous_glass candidate list "
        "(qualify it), an out-of-band wavelength is refused rather than "
        "extrapolated, and an in-band wavelength with no computable index returns "
        "a typed index_unavailable envelope - never a silent value. See "
        "find_glasses and find_glass_pair to select glasses by property."
    ),
    kind="glass",
    param_types={
        "name": "string",
        "catalog": "string",
        "wavelength": "number",
    },
)

TOOL_SPECS = (LOOKUP_GLASS_SPEC,)
