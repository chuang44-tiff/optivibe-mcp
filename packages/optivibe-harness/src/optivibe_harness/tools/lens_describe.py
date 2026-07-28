"""tools/lens_describe.py — describe_surfaces: surface-keyed ground-truth table.

A pure-READ tool (§2): one row per surface (index 0..N-1), each with a
``role`` from the SINGLE shared classifier in ``_layout_geometry`` (so the JSON
table and ``render_layout``'s figure can never disagree), plus the geometry fields
through ``safe_float`` (the JSON sentinel string for the human table — §0.1).

Grounding (§0): ``read_surface`` does NOT expose ``row.Type``, so this tool reads
``row.Type`` itself. The geometry JSON fields use ``safe_float`` (planar ->
``"inf"`` string); the classifier consumes the raw fact strings. ``include_global``
adds each surface's TRUSTED global vertex from ``GetGlobalMatrix(i)[10:13]`` — the
honest channel for fold coordinates (never the lying ``SurfaceData.TiltAbout_*``).

NEVER raises (§0.8): the whole body is wrapped; a catastrophic failure returns
``{ok:false, error_family:"describe_failed", ...}``. A PER-surface read that throws
yields ``{"surface": i, "role": "unreadable", "error": "<msg>"}`` and the scan
CONTINUES — one wedged proxy never sinks the whole table.

Live ZOS-API integration: exercised by the mandatory live test; unit-tested here
against FakeLDE/FakeRow doubles (no backend).
"""
from .._io import safe_float
from ..server import ToolSpec
from . import _asphere_cells as _asph
from . import _config_common as _cfg
from . import _layout_geometry as _geom


def _read_early_facts(lde, i, n):
    """Read the role-determining facts FIRST, BEFORE the geometry reads (§2, FIX 3).

    ``is_stop`` and ``role`` (which depends only on the CB substring of ``row.Type``,
    the mirror test on ``row.Material``, and ``is_stop``) are INDEPENDENT facts: a
    row that later throws on a geometry read (SemiDiameter, or GetGlobalMatrix under
    include_global) must STILL contribute its stop/fold fact if these specific reads
    succeeded. So they are read here, in their own guard, before anything that can
    throw downstream — a real stop or a fold can never silently vanish.

    Returns ``(role, is_stop, type_name, material_raw)``. Raises only to the
    per-surface guard (a row so wedged that even these reads fail has no fact to
    contribute and degrades whole).
    """
    row = lde.GetSurfaceAt(i)
    type_name = str(row.Type)
    material_raw = str(row.Material)
    is_stop = bool(row.IsStop)
    role = _geom.classify_role(i, n, type_name, material_raw, is_stop)
    return role, is_stop, type_name, material_raw


def _read_aspheric_coefficients(system, row, info):
    """Read a Tier-1 asphere's coefficients via the substrate reader (per-type, §3).

    Asphere read path: when a surface is a Tier-1 asphere, report its Par-cell
    coefficients as the agent's ground-truth table (a write-only asphere the agent
    cannot read back is a silent-wrong UX hole). REUSES the SAME substrate readers the
    write path uses. For a GATED (Extended) type it reads the live Max-Term gate, the
    norm radius, and that many cells; for a non-gated (Odd/Even) type the fixed cells.
    A read fault on ANY coefficient propagates to the per-surface guard (the row
    degrades to ``role:"unreadable"`` — never a partial / silently-wrong list).

    Returns ``(coefficients, asphere_surface_type, norm_radius_or_None,
    max_term_or_None)``.
    """
    if info.gated:
        max_terms = _asph.read_gate_cell(system, row, info)
        norm_radius = _asph.read_computed_double_cell(
            system, row, info.norm_par, _asph._NORM_HEADER
        )
        coeffs = [
            _asph.read_computed_double_cell(system, row, info.coeff_par(i), info.header(i))
            for i in range(max_terms)
        ]
        return coeffs, info.surface_type, norm_radius, max_terms
    coeffs = [
        _asph.read_computed_double_cell(system, row, info.coeff_par(i), info.header(i))
        for i in range(info.max_terms)
    ]
    return coeffs, info.surface_type, None, None


def _describe_one_surface(
    system, lde, i, n, include_global, role, is_stop, type_name, material_raw
):
    """Build ONE describe row from the EARLY facts + the geometry reads.

    The early facts (role/is_stop/type_name/material) were already read by
    ``_read_early_facts``; here we add the geometry fields that MAY throw. A throw
    propagates to the per-surface guard, but the early facts have already been
    captured by the caller (so stop/fold survive — FIX 3).
    """
    row = lde.GetSurfaceAt(i)
    out = {
        "surface": i,
        "role": role,
        "material": _geom.normalize_material(material_raw),
        "radius": safe_float(row.Radius),
        "thickness": safe_float(row.Thickness),
        "conic": float(row.Conic),
        "semi_diameter": safe_float(row.SemiDiameter),
        "comment": str(row.Comment),
        "is_stop": is_stop,
        "type_name": type_name,
    }

    # Asphere read path: a Tier-1 asphere surface reports its coefficients as the
    # ground-truth table (a non-asphere surface OMITS the field — no false positive).
    # Keyed on the EXACT-token type resolver (NEVER a naive ``in`` — the
    # "OddAsphere"-in-"ExtendedOddAsphere" trap) so we never invoke the asphere reader
    # on a non-asphere row, and the gated types add their norm radius + max term.
    type_key = _asph.asphere_type_of_name(type_name or "")
    if type_key is not None:
        info = _asph.ASPHERE_TYPE_INFO[type_key]
        coeffs, asph_type, norm, max_term = _read_aspheric_coefficients(
            system, row, info
        )
        out["aspheric_coefficients"] = coeffs
        # (S7 #13) Parallel ADDITIVE per-coefficient order/term labels (map-driven from
        # the per-type ORDER MAP); ``aspheric_coefficients`` stays byte-identical.
        out["coefficient_orders"] = _asph.coefficient_order_table(info, coeffs)
        out["asphere_surface_type"] = asph_type
        if norm is not None:
            out["asphere_norm_radius"] = safe_float(norm)
        if max_term is not None:
            out["asphere_max_term"] = max_term

    # GRIN (§6.1) additive read block — keyed on the AUTHORABLE resolver
    # (``grin_type_of_name`` over ``GRIN_TYPE_INFO``): a GRIN surface reports its base
    # index ``n0``, its radial coefficient MAP (Nr2..Nr12), the internal Delta-T trace step,
    # and the wavelength-blind caveat. A non-GRIN surface gets NONE of these keys
    # (byte-unchanged control). Map-driven Par1..Par8 ONLY (Par9+ never fetched); a real 0.0
    # coefficient stays visible (zero != unused). A drifted/wedged coefficient cell AFTER
    # positive type ID -> ``grin_coefficients:null`` + ``grin_coefficients_unreadable:true``
    # (never a fabricated 0.0, never a whole-surface error, never silently non-GRIN). Wrapped
    # so this additive read NEVER crashes the base surface read.
    try:
        from . import _grin_cells as _grin
        grin_key = _grin.grin_type_of_name(type_name or "")
    except Exception:  # noqa: BLE001 — a GRIN resolver hiccup -> treat as non-GRIN (omit block)
        grin_key = None
    if grin_key is not None:
        out["grin_surface_type"] = grin_key
        out["grin_wavelength_blind"] = True
        try:
            info = _grin.GRIN_TYPE_INFO[grin_key]
            coeffs = {}
            n0_val = None
            step_val = None
            # Iterate the RESOLVED type's params (was the module-global Gradient2 table — the
            # false ``grin_coefficients_unreadable`` on a good Gradient3); n0 keyed on
            # the TOKEN, never ``power == 0``.
            for tok, _p, _h, _k, role, _pw in info.params:
                val = _grin.read_grin_cell(system, row, tok, info)
                if role == "step":
                    step_val = val
                elif tok == "n0":
                    n0_val = val
                else:
                    coeffs[tok] = val
            out["grin_n0"] = n0_val
            out["grin_coefficients"] = coeffs
            out["grin_step_size"] = step_val
        except Exception:  # noqa: BLE001 — a drifted/wedged coeff cell -> graceful degrade
            out["grin_coefficients"] = None
            out["grin_coefficients_unreadable"] = True

    if include_global:
        # Trusted global vertex = TheSystem.LDE.GetGlobalMatrix(i)[10:13] (§0.4).
        # GetGlobalMatrix lives on the LDE (the live-proven receiver),
        # NOT on the surface row.
        # Each component through safe_float for JSON-safety. A throw here propagates
        # to the per-surface guard (the row degrades to role:"unreadable"), but the
        # EARLY stop/fold facts have already been recorded.
        matrix = lde.GetGlobalMatrix(i)
        vertex = tuple(matrix)[10:13]
        out["global_vertex"] = [safe_float(float(v)) for v in vertex]

    return out


def _resolve_describe_config(system, config):
    """Resolve a SINGLE-config selector (None|int) for describe/render — NO ``"all"`` (D2).

    Thin wrapper over the shared ``_config_common.resolve_single_config_selector`` (one
    contract). Returns the int config to read at (``None`` -> the active config, no
    switch), or RAISES ``ToolParamError`` on ``"all"`` / a bad value. ``describe_surfaces`` /
    ``render_layout`` show ONE config's geometry; the full per-config matrix is
    ``describe_configurations`` (S1).
    """
    return _cfg.resolve_single_config_selector(system, config, "describe_surfaces")


def describe_surfaces(session, params):
    """Read a surface-number-keyed ground-truth table with a role per surface.

    Returns ``{ok:true, count, stop_surface, folded, surfaces:[...], config_evaluated}``.
    NEVER raises: a catastrophic failure -> ``{ok:false, error_family:"describe_failed",
    error, count:0, surfaces:[]}``; a per-surface read throw degrades that one row
    to ``role:"unreadable"`` and the scan continues.

    ``config`` (None|int) reads the table at that configuration
    (inside a ``with_configuration`` wrap that ALWAYS restores). ``"all"`` is NOT offered
    (the full per-config matrix is ``describe_configurations``); NO per-config value
    stamps (D2). A bad ``config`` -> ``describe_failed``.
    """
    if not isinstance(params, dict):  # never-raise: None OR a truthy non-dict (§0.8, L26)
        params = {}
    include_global = bool(params.get("include_global", False))
    try:
        system = session.system
        cfg_idx = _resolve_describe_config(system, params.get("config"))
        if cfg_idx is None:
            result = _describe_at(session, include_global)
            if isinstance(result, dict) and result.get("ok"):
                result.setdefault(
                    "config_evaluated", _cfg.safe_current_configuration(system)
                )
            return result
        with _cfg.with_configuration(system, cfg_idx) as ctx:
            result = _describe_at(session, include_global)
        if isinstance(result, dict) and result.get("ok"):
            result.setdefault("config_evaluated", cfg_idx)
            if not ctx["restore_verified"]:
                result["mutation_warning"] = ctx["mutation_warning"]
            if not ctx["switched"] and ctx["mutation_warning"]:
                result.setdefault("config_switch_warning", ctx["mutation_warning"])
        return result
    except BaseException as exc:  # noqa: BLE001 — describe never raises into dispatch
        return {
            "ok": False,
            "error_family": "describe_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "count": 0,
            "surfaces": [],
        }


def _describe_at(session, include_global):
    """The pure per-config describe body (reads at the ACTIVE config). See ``describe_surfaces``."""
    try:
        system = session.system
        lde = system.LDE
        n = int(lde.NumberOfSurfaces)

        surfaces = []
        stop_surface = None
        folded = False

        for i in range(n):
            # Read the role-determining facts FIRST (FIX 3): is_stop and role are
            # INDEPENDENT of the geometry reads. Set the top-level stop/folded from
            # these EARLY facts BEFORE the geometry reads that may throw — even a row
            # that later degrades to "unreadable" still contributes its stop/fold
            # fact when these specific reads succeeded.
            try:
                role, is_stop, type_name, material_raw = _read_early_facts(lde, i, n)
            except BaseException as exc:  # noqa: BLE001 — early reads failed -> no fact to keep
                surfaces.append(
                    {"surface": i, "role": "unreadable", "error": f"{type(exc).__name__}: {exc}"}
                )
                continue

            if stop_surface is None and is_stop:
                stop_surface = i
            if role in ("coordinate-break", "mirror"):
                folded = True

            try:
                row_out = _describe_one_surface(
                    system, lde, i, n, include_global, role, is_stop, type_name,
                    material_raw
                )
            except BaseException as exc:  # noqa: BLE001 — one wedged row never sinks the table
                # The geometry reads threw, but the EARLY stop/fold facts above are
                # already recorded; only this row's full geometry is lost.
                surfaces.append(
                    {"surface": i, "role": "unreadable", "error": f"{type(exc).__name__}: {exc}"}
                )
                continue

            surfaces.append(row_out)

        return {
            "ok": True,
            "count": n,
            "stop_surface": stop_surface,
            "folded": folded,
            "surfaces": surfaces,
        }
    except BaseException as exc:  # noqa: BLE001 — describe never raises into dispatch
        return {
            "ok": False,
            "error_family": "describe_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "count": 0,
            "surfaces": [],
        }


DESCRIBE_SURFACES_SPEC = ToolSpec(
    name="describe_surfaces",
    handler=describe_surfaces,
    required_params=(),
    param_types={"include_global": "boolean", "config": "number"},
    description=(
        "Get the surface-number -> role ground-truth table (object/image/"
        "coordinate-break/mirror/stop/glass/air, material, radius, thickness, conic, "
        "semi-diameter, comment, is_stop, type_name) to talk about the design by "
        "surface number. Set include_global=True to add each surface's global vertex. "
        "config (int) reads the table at that multi-config configuration (the full "
        "per-config matrix is describe_configurations; 'all' is not offered here). "
        "Pure-read; inspect result.ok. See render_layout, describe_configurations."
    ),
)

TOOL_SPECS = (DESCRIBE_SURFACES_SPEC,)
