"""catalog/metrics.py — the shared per-reading firewall predicates.

PURE: zero engine call, zero registry import. Built FIRST.
Every double-envelope ok-gate, every numeric/label coercion, and the config="all"
coverage sub-gate live here in EXACTLY ONE place each, so they can never drift out
of sync:

- ``reading_ok(env)``      — THE single double-envelope acceptance predicate.
- ``coerce_metric(...)``   — THE numeric never-fake-0 firewall.
- ``coerce_label(...)``    — THE enum/index/string label sibling (D1).
- ``config_sweep_ok(...)`` — THE config="all" coverage sub-gate.

Nothing re-implements these — ``registry.profile`` and ``bench.py`` consume them.
``row_status`` (the 3-tier ROW verdict) is deliberately NOT here — it is a
row-level assembly decision and lives in ``bench.py``. ``coerce_metric``
never emits ``row_failed`` (that is ``bench.py``'s direct row-level stamp).

The sentinel threshold (row 6) REUSES ``_measurement_common.suspicious_sentinel``
(``abs >= 1e10``) — an import, never a re-derived literal, so the two definitions
cannot diverge.
"""
import math

from ..tools._measurement_common import suspicious_sentinel

# --------------------------------------------------------------------------- #
# The 9-token status enum (verbatim roadmap axis 2).
# --------------------------------------------------------------------------- #
STATUS_OK = "ok"
STATUS_NULL = "null"
STATUS_SENTINEL = "sentinel"
STATUS_SUSPICIOUS = "suspicious"
STATUS_FAILED_TRACE = "failed_trace"
STATUS_UNVERIFIED = "unverified"
STATUS_NOT_APPLICABLE = "not_applicable"
STATUS_TOOL_ERROR = "tool_error"
STATUS_ROW_FAILED = "row_failed"

ALL_STATUSES = (
    STATUS_OK, STATUS_NULL, STATUS_SENTINEL, STATUS_SUSPICIOUS, STATUS_FAILED_TRACE,
    STATUS_UNVERIFIED, STATUS_NOT_APPLICABLE, STATUS_TOOL_ERROR, STATUS_ROW_FAILED,
)

# The valid tri-state (+ no-concept) encoding a Reading carries:
#   valid is False          -> failed_trace (a proven bad trace)
#   valid == "indeterminate" -> unverified (get_spot budget could-not-disprove; KEEP the number)
#   valid is True / None     -> no validity concern (most tools) -> falls through to ok
VALID_INDETERMINATE = "indeterminate"

# The string forms a non-finite float takes on the JSON wire: ``safe_float`` emits
# ``"nan"/"inf"/"-inf"``; the probe capture additionally encodes ``"__nan__"/"__inf__"``.
_SENTINEL_STRINGS = frozenset({"nan", "inf", "-inf", "__nan__", "__inf__"})


def reading_ok(env):
    """THE single double-envelope acceptance predicate (D11). NEVER raises.

    Returns ``(accepted, status_hint, reason)``:
      - ``accepted is True``  -> the inner ``result`` is trustworthy; adapters extract.
        (``status_hint`` / ``reason`` are ``None``.)
      - ``accepted is False`` -> ``status_hint`` is the coerce-status token to stamp on
        EVERY column the adapter owns; ``reason`` is the message.

    Checks the OUTER ``env.ok`` first, then the INNER ``result.ok`` — STRICT ``is True``
    on the inner (a MISSING ``ok`` REJECTS, D11: this is the two-predicates boundary
    bug — a ``result.get("ok", True)`` default-True form would leak a garbage result that
    merely omitted ``ok``). Consumed by ``registry.profile`` (every metric call) AND
    ``bench.py`` (load / scale / set_* / describe_surfaces).
    """
    # (1) Outer dispatch layer. ``result`` is None on this path — never dereference it.
    if not isinstance(env, dict) or env.get("ok") is not True:
        family = None
        if isinstance(env, dict):
            family = env.get("error_family")
        return (False, STATUS_TOOL_ERROR, family or "dispatch_error")
    # (2) Inner result must be a dict.
    result = env.get("result")
    if not isinstance(result, dict):
        return (False, STATUS_TOOL_ERROR, "empty_result")
    # (3) Inner ok — STRICT ``is True`` (a missing ``ok`` rejects, D11).
    if result.get("ok") is not True:
        family = result.get("error_family")
        if family == "analysis_empty":
            # aspheric_profile on a non-asphere surface -> honest per-cell absence.
            return (False, STATUS_NOT_APPLICABLE, result.get("error") or "analysis_empty")
        # THE double-envelope trap: analyze_axial_color + config="all" reads
        # env.ok=True, result.ok=False, measurement_param — an env.ok-only guard
        # would green-pass this garbage.
        return (False, STATUS_TOOL_ERROR, family or "inner_ok_absent")
    # (4) Accepted — extract may run. Per-reading nullity is decided DOWNSTREAM.
    return (True, None, None)


def _is_number(value):
    """A real (non-bool) int/float."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_sentinel_value(raw):
    """True iff ``raw`` is a wire/live non-finite/1e10 sentinel (row 6). NEVER raises.

    A string form (``"nan"/"inf"/"-inf"/"__nan__"/"__inf__"``) OR a live non-finite /
    ``abs >= 1e10`` number (via ``_measurement_common.suspicious_sentinel`` — the
    reused threshold, imported rather than re-derived). A number that is finite and
    below the threshold is NOT a sentinel.
    """
    if isinstance(raw, str):
        return raw in _SENTINEL_STRINGS
    if _is_number(raw):
        return suspicious_sentinel(raw)
    return False


def coerce_metric(raw, *, env_ok=True, result_ok=True, suspicious=False,
                  valid=None, applicable=True):
    """THE numeric never-fake-0 firewall. Returns ``(cell, status)``.

    ``cell`` is a finite float OR ``None`` (the empty CSV cell); ``status`` is one of the
    9 tokens. A ``None`` cell is ALWAYS the empty cell; a numeric cell is ALWAYS a real
    measured value (``0.0`` is a real zero). NEVER raises. Precedence (first match wins):

      1 env_ok is False                                        -> None, tool_error
      2 result_ok is False AND applicable is False             -> None, not_applicable
      3 result_ok is False                                     -> None, tool_error
      4 applicable is False                                    -> None, not_applicable
      5 valid is False                                         -> None, failed_trace
      6 raw is None                                            -> None, null
      7 raw is a string/live sentinel (abs>=1e10 / non-finite) -> None, sentinel
      8 suspicious is True                                     -> None, suspicious   (raw -> manifest)
      9 valid == "indeterminate"                               -> the number, unverified (KEEP)
     10 finite number                                          -> the float, ok

    Resolved a spec ambiguity (the precedence table vs the get_spot shape): the spec
    table listed ``raw is None`` (row 5) BEFORE ``valid is False`` (row 8), but the
    get_spot failed-trace null-encoding is ``rms:null, valid:false`` (raw IS None) — so
    that order makes the ``failed_trace`` token UNREACHABLE (it would read plain
    ``null``), yet the 2-axis matrix + the live probe evidence REQUIRE
    ``valid:false -> failed_trace``. ``valid is False`` is only
    ever set by get_spot (every other adapter passes ``valid=None`` = no concept), so
    checking it FIRST is safe and matches the probe intent — a plain ``coerce_metric(None)``
    (no validity concept) still reads ``null``.

    Locks: (a) suspicious -> null (raw preserved in the manifest reason, never the
    cell); (b) unverified KEEPS the number (could-not-disprove; the ranker discounts).
    """
    if env_ok is False:
        return (None, STATUS_TOOL_ERROR)
    if result_ok is False:
        if applicable is False:
            return (None, STATUS_NOT_APPLICABLE)
        return (None, STATUS_TOOL_ERROR)
    if applicable is False:
        return (None, STATUS_NOT_APPLICABLE)
    if valid is False:
        return (None, STATUS_FAILED_TRACE)
    if raw is None:
        return (None, STATUS_NULL)
    if _is_sentinel_value(raw):
        return (None, STATUS_SENTINEL)
    if suspicious is True:
        return (None, STATUS_SUSPICIOUS)
    if valid == VALID_INDETERMINATE:
        # Could-not-disprove (get_spot budget tri-state) — KEEP the number if finite.
        if _is_number(raw) and math.isfinite(raw):
            return (float(raw), STATUS_UNVERIFIED)
        return (None, STATUS_UNVERIFIED)
    if _is_number(raw) and math.isfinite(raw):
        return (float(raw), STATUS_OK)
    # A non-numeric, non-sentinel raw that reached here is unreadable -> empty cell.
    return (None, STATUS_NULL)


def coerce_label(raw, *, env_ok=True, result_ok=True, applicable=True, allowed=None):
    """THE enum/index/string label sibling (D1). Returns ``(cell, status)``.

    For ``kind in {"enum","index","string"}`` columns. Precedence rows 1-5 of the
    numeric table apply verbatim (tool_error / not_applicable / null); then:
      - ``allowed`` non-None AND ``raw not in allowed`` -> ``(None, null)`` (the raw goes
        to ``notes`` upstream; an unreadable/foreign enum is an EMPTY cell, NEVER a
        fabricated token).
      - else -> ``(raw, ok)``.
    No sentinel/suspicious/valid rows (labels carry none). NEVER raises.
    """
    if env_ok is False:
        return (None, STATUS_TOOL_ERROR)
    if result_ok is False:
        if applicable is False:
            return (None, STATUS_NOT_APPLICABLE)
        return (None, STATUS_TOOL_ERROR)
    if applicable is False:
        return (None, STATUS_NOT_APPLICABLE)
    if raw is None:
        return (None, STATUS_NULL)
    if allowed is not None and raw not in allowed:
        return (None, STATUS_NULL)
    return (raw, STATUS_OK)


def config_sweep_ok(result):
    """THE config="all" coverage sub-gate. NEVER raises.

    Returns ``(coverage_ok, per_config_list, reason)``.
    - ``result.config_evaluated == "all"`` -> ``(coverage.ok is True AND not
      coverage.missing, per_config, reason)``; a malformed/absent ``coverage`` OR a
      non-list ``per_config`` fails CLOSED -> ``(False, per_config_or_[], reason)``.
    - else (single-config shape at the top level) -> wrap ``result`` as a one-element
      list with ``coverage_ok=True``.

    Coverage-not-ok -> the runner stamps ``unverified`` on the affected cells (D4) and
    never certifies a clean verdict (a firewall the probe work established).
    """
    if not isinstance(result, dict):
        return (False, [], "malformed_result")
    if result.get("config_evaluated") == "all":
        per = result.get("per_config")
        cov = result.get("coverage")
        if not isinstance(per, list):
            return (False, [], "per_config_unreadable")
        if not isinstance(cov, dict):
            return (False, per, "coverage_unreadable")
        cov_ok = cov.get("ok") is True and not cov.get("missing")
        reason = None if cov_ok else "coverage_incomplete"
        return (cov_ok, per, reason)
    # Single-config shape — wrap as a one-element sweep, always covered.
    return (True, [result], None)


__all__ = [
    "STATUS_OK", "STATUS_NULL", "STATUS_SENTINEL", "STATUS_SUSPICIOUS",
    "STATUS_FAILED_TRACE", "STATUS_UNVERIFIED", "STATUS_NOT_APPLICABLE",
    "STATUS_TOOL_ERROR", "STATUS_ROW_FAILED", "ALL_STATUSES", "VALID_INDETERMINATE",
    "reading_ok", "coerce_metric", "coerce_label", "config_sweep_ok",
]
