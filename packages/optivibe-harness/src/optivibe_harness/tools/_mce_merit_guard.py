"""tools/_mce_merit_guard.py -- the leading-CONF guard for a multi-config merit function.

The defect: with more than one
configuration, at least one MCE row that is not the ``MOFF`` floor, and MFE row 1 not a
``CONF``, ANY merit evaluation inserts ``CONF 1`` at row 1 and deletes the merit's LAST row,
and the tool reports success. A clean insert exists: an optimizer open/close writes ``CONF 1``
at row 1 with +1 row and every other row unchanged (idempotent, survives a later
``add_configuration``).

``ensure_leading_conf(system)`` triggers that insert when (and only when) the lens is in the
trigger state, and PROVES the result by reading the MFE type list before and after. Route:
``system.MFE.SaveMeritFunction(path)`` into a private ``mkdtemp``
directory, removed in ``finally`` (a removal failure is swallowed and leaves the temp dir
behind). It is the same insert as the open/close (measured), so no
``Tools.Open*`` member is touched (the tools-slot census stays at 16 sites). It never evaluates
the merit (the evaluation is the trigger) and never calls ``InsertNewOperandAt`` (index 1
duplicates a row). A save that silently no-ops reads as ``insert_no_effect``. It does not
restore a lost row.

Never raises (exceptions are rendered through ``safe_exc``).
Returns ``{status, n_operands_before, n_operands_after[, detail]}``.
"""
import os
import tempfile

from .._io import safe_exc

# Status tokens (the ``merit_conf_guard.status`` vocabulary).
NOT_NEEDED = "not_needed"
ALREADY_PRESENT = "already_present"
INSERTED = "inserted"
SAVE_FAILED = "save_failed"
INSERT_NO_EFFECT = "insert_no_effect"
READBACK_MISMATCH = "readback_mismatch"
UNREADABLE = "unreadable"

# The error family a refusal carries (a STRING constant, D10 -- no new error class).
MCE_MERIT_GUARD = "mce_merit_guard"

# Statuses on which the merit can still lose its last row at the next evaluation.
UNSAFE_STATUSES = frozenset(
    {SAVE_FAILED, INSERT_NO_EFFECT, UNREADABLE, READBACK_MISMATCH}
)
# Statuses that are safe to evaluate behind.
CLEAN_STATUSES = frozenset({NOT_NEEDED, ALREADY_PRESENT, INSERTED})


def _type_token(op):
    """The operand TYPE token: ``str(op.Type)`` reduced to its last ``.``-segment."""
    return str(op.Type).split(".")[-1]


def _result(status, before, after, detail=None):
    out = {"status": status, "n_operands_before": before, "n_operands_after": after}
    if detail is not None:
        out["detail"] = detail
    return out


def _mfe_types(system):
    """The MFE row type tokens, 1..NumberOfOperands (raises on any read fault)."""
    mfe = system.MFE
    n = int(mfe.NumberOfOperands)
    return [_type_token(mfe.GetOperandAt(i)) for i in range(1, n + 1)]


def _save_merit(system):
    """Save the merit into a private temp directory (the engine inserts ``CONF 1`` on a save).

    Returns ``(saved, detail)``: ``saved`` is False when ``SaveMeritFunction`` threw;
    ``detail`` names the throw. The file and its directory are removed; a removal failure
    is swallowed and leaves the temp dir behind. No ``Tools.Open*`` member is touched.
    """
    tmpdir = None
    path = None
    try:
        tmpdir = tempfile.mkdtemp(prefix="confguard_")
        path = os.path.join(tmpdir, "guard.MF")
        system.MFE.SaveMeritFunction(path)
        return True, None
    except Exception as exc:  # noqa: BLE001 -- a failed save is non-fatal
        return False, f"SaveMeritFunction() raised {safe_exc(exc, repr_form=True)}"
    finally:
        if path is not None:
            try:
                os.remove(path)
            except Exception:  # noqa: BLE001 -- cleanup must never raise
                pass
        if tmpdir is not None:
            try:
                os.rmdir(tmpdir)
            except Exception:  # noqa: BLE001 -- cleanup must never raise
                pass


def ensure_leading_conf(system):
    """Make sure a multi-config merit leads with a ``CONF`` row; prove it by read-back."""
    before_n = None
    try:
        n_configs = int(system.MCE.NumberOfConfigurations)
        if n_configs <= 1:
            return _result(NOT_NEEDED, None, None)
        mce = system.MCE
        n_mce = int(mce.NumberOfOperands)
        if not any(_type_token(mce.GetOperandAt(i)) != "MOFF" for i in range(1, n_mce + 1)):
            return _result(NOT_NEEDED, None, None)
        mfe = system.MFE
        before_n = int(mfe.NumberOfOperands)
        if before_n == 0:
            return _result(NOT_NEEDED, 0, 0)
        if _type_token(mfe.GetOperandAt(1)) == "CONF":
            return _result(ALREADY_PRESENT, before_n, before_n)
        before = _mfe_types(system)
        before_n = len(before)
    except Exception as exc:  # noqa: BLE001 -- a decision read threw (never raise)
        return _result(UNREADABLE, before_n, None,
                       f"could not read the state needed to decide ({safe_exc(exc, repr_form=True)})")

    saved, save_detail = _save_merit(system)

    try:
        after = _mfe_types(system)
    except Exception as exc:  # noqa: BLE001 -- the post-state read threw
        return _result(UNREADABLE, before_n, None,
                       f"could not read the merit rows after the merit save "
                       f"({safe_exc(exc, repr_form=True)}); a row may have been lost")
    after_n = len(after)

    if after_n == before_n + 1 and after[0] == "CONF" and after[1:] == before:
        return _result(INSERTED, before_n, after_n)
    if after == before:
        if not saved:
            return _result(SAVE_FAILED, before_n, after_n,
                           f"{save_detail}; the leading CONF could not be inserted")
        return _result(INSERT_NO_EFFECT, before_n, after_n,
                       "the merit save ran but the merit rows are unchanged; "
                       "the leading CONF was not inserted")
    return _result(READBACK_MISMATCH, before_n, after_n,
                   f"the merit rows changed but not as a clean CONF insert "
                   f"({before_n} -> {after_n} rows)")
