"""tools/_solve_read.py -- the READ DOORS' two additions to ``_solve_cells.emit_solves_block``.

**OWNS:** (D1) the VALUES of a ``SurfacePickup`` cell, beside the field NAMES the block has
always carried; (D2) the Par-cell pickups of a coordinate-break row, and the narrowing of
``par_cell_solves_not_audited`` that follows from reading them.

**DOES NOT OWN:** any reader of solve state. Geometry values come from
``_solve_cells.read_solve_fields`` (the ONE shared reader) and Par values from
``_solve_refs.cb_par_refs`` in its any-target mode (``at=None``) -- there is no second reader
of either. It lives HERE and not in ``_solve_cells`` because that substrate has its size
pinned by a test, and a read-door feature is not a substrate primitive.

Both entry points are NEVER-RAISE and import their siblings lazily (``_solve_cells`` imports
this module at the top, so a top-level import back would be circular).
"""

#: ``values`` key -> the ``SurfacePickup`` view field it reports. The key names are the ones
#: ``set_solve`` takes and ``remove_surface``'s ``solve_refs`` entries carry.
_PICKUP_VALUE_FIELDS = (("surface", "Surface"), ("column", "Column"),
                        ("scale_factor", "ScaleFactor"), ("offset", "Offset"))


def with_values(entry, cell, token):
    """``entry``, plus ``values`` / ``values_unreadable`` when its solve is a ``SurfacePickup``.

    Every ``values`` key is ALWAYS present for a pickup: a value that is not readable, not
    finite or not a wire type is ``None`` and ``values_unreadable[key]`` says which kind
    (``not_representable`` = non-finite, ``mismatched`` = unread / wrong type,
    ``read_back_none`` = read fine and was ``None``) -- never omitted, never defaulted.
    ``Column`` is rendered through ``surface_solve.to_wire``. The values come from
    ``read_solve_fields`` over the view a SECOND ``GetSolveData`` resolves
    (``read_solve_type`` returns a string, so there is no view to reuse: +1 per pickup cell,
    declared in ``test_adv_solve_fetch_budget``). Other solve types pass through untouched.
    """
    from . import _solve_cells as _sc
    from . import surface_solve as _ss
    if entry.get("type") != "SurfacePickup":
        return entry
    try:
        view = _sc.unwrap_or_none(cell.GetSolveData())
        read = {} if view is None else _sc.read_solve_fields(view, token, "SurfacePickup")
    except Exception:  # noqa: BLE001 — an unreadable view is DISCLOSED, never raised
        read = {}
    values, unreadable = {}, {}
    for key, field in _PICKUP_VALUE_FIELDS:
        value, state = read.get(field, (None, "mismatched"))
        try:
            value = _ss.to_wire(field, value)
        except Exception:  # noqa: BLE001 — an unrenderable Column is unread, not fatal
            value, state = None, "mismatched"
        if not (value is None or isinstance(value, (str, bool, int, float))):
            value, state = None, "mismatched"
        values[key] = value
        if value is None:
            unreadable[key] = "read_back_none" if state == "matched" else state
    entry["values"] = values
    if unreadable:
        entry["values_unreadable"] = unreadable
    return entry


def par_keys(system, lde, surface, row):
    """The Par part of the emitted block for a row that is not ``Standard``. NEVER raises.

    Returns a dict to merge into the block: ``par_solves`` (one entry per Par cell that
    carries a ``SurfacePickup``: ``type``, ``fields``, ``values``) when the row is a
    coordinate break, and ``par_cell_solves_not_audited: True`` UNLESS the row is a
    coordinate break whose Par scan left NO gap -- so an asphere / GRIN / grating row, a type
    this package does not recognise, an unreadable row and a CB with an unreadable Par cell
    all keep the flag. A fault anywhere keeps the flag.

    ``par_solves`` is a SIBLING key, not Par names inside ``solves``: ``_cb_solve_guard``
    treats every ``solves`` key as one of the five geometry cell tokens.

    ``cb_par_refs`` is the removal scanner in its any-target mode (``at=None``), handed the
    row the emit already holds so the read door pays no second ``GetSurfaceAt``.
    """
    from . import _cb_cells as _cb
    from . import _solve_cells as _sc
    from . import _solve_refs as _refs
    flag = {"par_cell_solves_not_audited": True}
    try:
        if row is None or not _cb.is_coordinate_break(row):
            return flag
        hits, gaps, _unaudited = _refs.cb_par_refs(
            system, lde, surface, None, int(lde.NumberOfSurfaces), row)
        out = {}
        for hit in hits:
            # ``SurfacePickup`` carries the SAME four fields on every cell, so the catalog's
            # radius entry names a Par cell's fields too (there is no Par catalog).
            values = {key: hit[key] for key in ("column", "scale_factor", "offset")}
            values["surface"] = hit["source"]
            entry = {"type": hit["solve_type"], "values": values,
                     "fields": sorted(_sc.lookup("radius", hit["solve_type"])[1] or ())}
            bad = sorted(key for key, value in values.items() if value is None)
            if bad:
                # ``_hit`` collapses non-finite and unread to ``None``: say so, never guess.
                entry["values_unreadable"] = {key: "non_finite_or_unread" for key in bad}
            out[hit["cell"]] = entry
        result = {"par_solves": out} if out else {}
        if gaps:
            result.update(flag)
        _sc.assert_wire_safe(result)
        return result
    except Exception:  # noqa: BLE001 — a Par read fault keeps the flag
        return flag
