"""tools/_by_number_refs.py -- what a surface removal does to rows that name surfaces by number.

NOT dispatchable (no ``TOOL_SPECS``). Measured, OpticStudio 25.1: removing surface k DELETES
the multi-configuration rows whose surface parameter is k (and renumbers the rest), and a
tolerance row whose surface cell reads k SLIDES -- it keeps reading k, so it silently names
the surface that FOLLOWED the removed one. A MERIT row whose surface cell is k has been
measured both ways: deleted on a merit that carried a wizard boundary block and SLID on a hand-built
merit with no block. Its fate
is therefore never predicted: ``report`` re-reads the merit after the removal and says what
it measured. Inserting or removing a surface elsewhere renumbers all three tables correctly.

``snapshot`` reads the three tables BEFORE the removal; ``report`` re-reads the multi-config
table and the merit afterwards and returns the two additive blocks ``remove_surface`` ships
beside ``solve_refs`` (which it does not touch). The merit fate is ``slid`` only when the row
count is unchanged AND every listed row (row, operand, cell) still reads the removed number;
a changed count is reported as ``count_changed`` with both counts (no per-row deletion is
claimed: renumbered twins make row identity unprovable), and a failed re-read as
``unknown``. Tolerance rows come from the pre-removal snapshot only. Every read here is
fail-open: a table that cannot be read is said so, never guessed.

Not covered: a merit or tolerance row whose surface RANGE merely spans the removed surface
(only a surface cell equal to it is reported). Rows are matched on the cells named
``Surf`` / ``Surf1`` / ``Surf2``.
"""
from . import _config_common as _cc
from . import _mce_catalog as _cat
from . import _merit_cells as _mcl

_SURFACE_HEADERS = frozenset({"Surf", "Surf1", "Surf2"})
_TABLE_COLS = range(2, 10)
_MERIT_MEANING = {
    "slid": (
        "each listed merit row still reads surface %(at)d, which is now the surface that "
        "followed the removed one (measured this call: row count unchanged, rows re-read); "
        "re-point or remove it if that is not what you want"),
    "count_changed": (
        "the merit went from %(before)d to %(after)d rows on this removal, so the listed rows "
        "may have been deleted or the merit regenerated; re-read it with dump_merit_function "
        "before relying on it"),
    "unknown": (
        "the merit could not be re-read after the removal, so what happened to the listed "
        "rows is unknown; re-read it with dump_merit_function"),
}
_TOLERANCE_MEANING = (
    "each listed tolerance row still reads surface %d, which is now the surface that followed "
    "the removed one; re-point it if that is not what you want")


def _merit_hits(system, at):
    """Merit rows with a surface cell equal to ``at`` -> ``(hits, rows_unread)``."""
    mfe = system.MFE
    hits, unread = [], 0
    for i in range(1, int(mfe.NumberOfOperands) + 1):
        try:
            op = mfe.GetOperandAt(i)
            token = _mcl._base_token(op.TypeName)
            cells = _mcl.read_param_map(op)
        except Exception:  # noqa: BLE001 - one unreadable row is counted, not fatal
            unread += 1
            continue
        for header, cell in cells.items():
            if header in _SURFACE_HEADERS and cell["kind"] == "int" and cell["value"] == at:
                hits.append({"row": i, "operand": token, "cell": header, "surface": at})
    return hits, unread


def _tolerance_hits(system, at):
    """Tolerance rows with a surface cell equal to ``at`` -> ``(hits, rows_unread)``."""
    tde = system.TDE
    hits, unread = [], 0
    for i in range(1, int(tde.NumberOfOperands) + 1):
        try:
            op = tde.GetOperandAt(i)
            token = _mcl._base_token(op.Type)
        except Exception:  # noqa: BLE001 - one unreadable row is counted, not fatal
            unread += 1
            continue
        for col in _TABLE_COLS:
            try:
                cell = op.GetCellAt(col)
                header = _mcl._base_token(cell.Header)
                if (header in _SURFACE_HEADERS
                        and _mcl._base_token(cell.DataType) == "Integer"
                        and int(cell.IntegerValue) == at):
                    hits.append({"row": i, "operand": token, "cell": header, "surface": at})
            except Exception:  # noqa: BLE001 - a column this row does not have
                continue
    return hits, unread


def _read(table, system, at):
    """One table's ``(hits, unread)``; a table that cannot be opened is ``([], None)``."""
    try:
        return table(system, at)
    except Exception:  # noqa: BLE001 - an unreadable table is reported as unread
        return [], None


def _merit_count(system):
    """The merit's row count, or ``None`` when it cannot be read."""
    try:
        return int(system.MFE.NumberOfOperands)
    except Exception:  # noqa: BLE001 - fail-open
        return None


def _merit_fate(system, before):
    """Measure what the removal did to the listed merit rows -> ``(fate, count_after)``."""
    count_after = _merit_count(system)
    hits_after, unread_after = _read(_merit_hits, system, before["at"])
    if count_after is None or before["merit_count"] is None or unread_after is None:
        return "unknown", count_after
    if count_after != before["merit_count"]:
        return "count_changed", count_after
    key = lambda h: (h["row"], h["operand"], h["cell"])  # noqa: E731
    still = {key(h) for h in hits_after}
    if unread_after == 0 and all(key(h) in still for h in before["merit"]):
        return "slid", count_after
    return "unknown", count_after


def snapshot(system, at):
    """Read the multi-config, merit and tolerance tables before surface ``at`` is removed."""
    merit, merit_unread = _read(_merit_hits, system, at)
    tol, tol_unread = _read(_tolerance_hits, system, at)
    return {"at": at, "mce": _cc.mce_snapshot(system), "merit": merit,
            "merit_count": _merit_count(system),
            "tolerance": tol, "unread": {"merit": merit_unread, "tolerance": tol_unread}}


def _moved_by_removal(at):
    """The parameters an MCE row is left with when surface ``at`` goes: ``None`` = deleted."""
    def moved(row):
        meta = _cat.meta_for(row["operand"])
        if meta is None or not meta.takes_surface or row["param1"] is None:
            return (row["param1"], row["param2"], row["param3"])
        if row["param1"] == at:
            return None
        shift = 1 if row["param1"] > at else 0
        return (row["param1"] - shift, row["param2"], row["param3"])
    return moved


def report(system, before):
    """The two blocks to ship after the removal: ``mce_rows_deleted`` and ``by_number_refs``."""
    unread = before["unread"]
    partly = any(v is None or v > 0 for v in unread.values())
    block = {"merit_rows": before["merit"], "tolerance_rows": before["tolerance"],
             "unread": unread}
    sentences = []
    fate = None
    if before["merit"]:
        fate, count_after = _merit_fate(system, before)
        block["merit_fate"] = fate
        block["merit_count"] = {"before": before["merit_count"], "after": count_after}
        sentences.append(_MERIT_MEANING[fate] % {
            "at": before["at"], "before": before["merit_count"] or 0, "after": count_after or 0})
    if before["tolerance"]:
        sentences.append(_TOLERANCE_MEANING % before["at"])
    if fate is None:
        state = "slid" if before["tolerance"] else ("partly_unread" if partly else "none_found")
    elif fate == "slid" or not before["tolerance"]:
        state = fate
    else:
        state = "mixed"
    block = {"state": state, **block}
    if sentences:
        block["meaning"] = "; ".join(sentences)
    mce = _cc.mce_rows_deleted(system, before["mce"], _moved_by_removal(before["at"]))
    return {"mce_rows_deleted": mce, "by_number_refs": block}
