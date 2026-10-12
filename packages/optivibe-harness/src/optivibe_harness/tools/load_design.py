"""tools/load_design.py — the ``load_design`` recovery tool.

ONE dispatchable tool ``load_design(path?, best?)`` — load a saved ``.zmx`` design
from disk so it can be tolerance'd. The recovery enabler for the empty-report bug:
the headless tolerancing report writer blesses ONLY a real saved design loaded fresh
from its own on-disk path (probe-frozen; an in-memory build / API-``SaveAs`` system
yields a 0-byte report). Partner to ``save_candidate`` / ``promote_best``.

- Signature: EXACTLY ONE of ``path`` (absolute ``.zmx``, or relative to
  ``session.workspace_root``) OR ``best`` (a design name -> ``BEST_<safe_name>.zmx``
  under the workspace root). Neither / both -> ``load_param`` (loud).
- Resolution guard: a relative ``..``-escape outside the root is REJECTED (the
  ``resolve_merit_path`` ``os.path.commonpath`` precedent); a non-``.zmx`` suffix, a
  missing file, or an empty (0-byte) file -> ``load_param`` / ``load_not_found``
  (loud, BEFORE touching the engine).
- File SNIFF (before the engine): the file is STREAMED (UTF-16LE when it starts with a
  BOM, else 8-bit) and must open with ``VERS`` then ``MODE``; when it has ``SURF`` records
  they must be unique and contiguous ``0..N-1``, each with a parseable ``DISZ``, and the file
  must carry an ``MNUM``. A file that fails -> ``load_failed`` /
  ``reason:"not_a_zmx"`` with ``replaced_prior_system:false`` (the design is UNTOUCHED).
- Load + READ-BACK-AS-PROOF: ``system.LoadFile(path, False)`` then VERIFY the load
  really happened (``NumberOfSurfaces >= 2`` AND a non-empty ``SystemFile`` that
  resolves to the requested path) else ``load_failed``. A bad file does NOT silently
  no-op (measured live): the
  engine DESTROYS the loaded design, builds a blank 3-surface system and sets
  ``SystemFile`` to the requested path, so both of those read-backs pass. The proof that
  a blank substitute cannot fake is the file's OWN record: the loaded surface count must
  equal the file's ``SURF`` count (``reason:"surface_count_mismatch"``) and, on a
  single-configuration file, each loaded thickness must equal the file's ``DISZ``
  (``reason:"geometry_mismatch"``). Every refusal after ``LoadFile`` says the previous
  design is GONE.
- Disclosure: ``replaced_prior_system:true`` (LoadFile REPLACES the whole system —
  the prior in-memory design is discarded, no rollback), plus ``surfaces``,
  ``system_file``, ``load_verified`` (``"geometry"`` | ``"count_only"`` | ``"path_only"``:
  how much of the load the file's own record proved) and a ``blessed_for_tolerancing``
  hint (the shared classifier).

NEVER raises (the tool-family posture). Families: ``load_param``, ``load_not_found``,
``load_failed``.
"""
import functools
import io
import math
import os
import re

from .._io import safe_exc
from ..server import ToolSpec
from . import _analysis_common as _ac
from . import _lens_common as _lc
from . import _optimize_common as _oc
from . import _tolerance_common as _tc


def _read_config_state(system):
    """Read ``(active_configuration, number_of_configurations, mce_rows)`` guarded.

    Each raw MCE read is THROW-guarded -> ``None`` on a fault (a non-MCE backend / a
    transient wedge must not fail the load — the disclosure is honesty-only). Returns a
    triple of ``int | None``. ``mce_rows`` (GAP 9, S6) = ``MCE.NumberOfOperands`` (the
    operand ROWS), the sibling of ``NumberOfConfigurations`` (the config COLUMNS).
    """
    try:
        active = int(system.MCE.CurrentConfiguration)
    except Exception:  # noqa: BLE001 — a read fault -> null (load is the deliverable)
        active = None
    try:
        n_configs = int(system.MCE.NumberOfConfigurations)
    except Exception:  # noqa: BLE001 — a read fault -> null
        n_configs = None
    try:
        mce_rows = int(system.MCE.NumberOfOperands)
    except Exception:  # noqa: BLE001 — a disclosure read must never break the load
        mce_rows = None
    return active, n_configs, mce_rows


def _coerce_pin_config(value):
    """Coerce ``pin_config`` to an exact int >= 1; reject bool / non-integral / < 1.

    Accepts an exact int OR an integral float (the JSON round-trip rule). A bool, a
    non-integral / non-finite float, a non-number, or a value < 1 -> ``ValueError`` (the
    caller turns it into a ``load_param`` envelope; the LOAD still succeeded). Returns
    the int.
    """
    if isinstance(value, bool):
        raise ValueError(f"pin_config must be an integer >= 1, not a bool ({value!r})")
    if isinstance(value, float):
        if math.isfinite(value) and value == int(value):
            value = int(value)
        else:
            raise ValueError(
                f"pin_config must be an integer >= 1, got non-integral {value!r}"
            )
    if not isinstance(value, int):
        raise ValueError(
            f"pin_config must be an integer >= 1, got {type(value).__name__} {value!r}"
        )
    if value < 1:
        raise ValueError(f"pin_config must be >= 1, got {value}")
    return value


def _never_raise(tool_name):
    """Wrap the handler so it NEVER raises past its boundary (the tool-family posture).

    ROUND-13 -- the render goes through ``_io.safe_exc``. This decorator IS the
    never-raise boundary and it interpolated ``exc`` from INSIDE the ``except``: an
    exception whose ``__str__`` throws made the handler itself raise, losing the typed
    ``load_failed`` family to the generic ``internal`` one. Measured on the two
    sibling decorators (``analysis_measure`` / ``tolerance_run``), both of which
    ESCAPED with ``RuntimeError``; this one is the same shape and is fixed with them
    rather than left to be found separately.
    """
    def _decorate(handler):
        @functools.wraps(handler)
        def _wrapped(session, params):
            try:
                return handler(session, params)
            except Exception as exc:  # noqa: BLE001 — net any throw to a typed envelope
                return _ac.error_envelope(
                    tool_name, "load_failed",
                    f"{tool_name} hit an unexpected error: {safe_exc(exc)}",
                )
        return _wrapped
    return _decorate


def _resolve_design_path(session, params):
    """Resolve the requested ``.zmx`` path from EXACTLY ONE of ``path`` / ``best``.

    Returns ``(resolved_path, error_envelope_or_None)``. ``best`` -> the
    ``BEST_<safe_name>.zmx`` under the workspace root (reusing ``_safe_name`` +
    ``_resolve_root``). ``path`` -> the ``resolve_merit_path`` resolver (the
    relative-under-root + ``..``-escape guard precedent). Neither / both -> ``load_param``.
    """
    if not isinstance(params, dict):
        params = {}
    has_path = params.get("path") is not None
    has_best = params.get("best") is not None
    if has_path == has_best:
        return None, _ac.error_envelope(
            "load_design", "load_param",
            "load_design requires EXACTLY ONE of 'path' (a .zmx path) or 'best' (a "
            f"design name); got path={params.get('path')!r}, best={params.get('best')!r}",
        )

    if has_best:
        best = params.get("best")
        if not isinstance(best, str) or best.strip() == "":
            return None, _ac.error_envelope(
                "load_design", "load_param",
                f"'best' must be a non-empty design name, got {best!r}",
            )
        from .. import artifact_naming as _naming
        from .workspace import _design_name_error, _resolve_root
        # A non-canonical spelling is REFUSED through the SHARED door rather than
        # resolved to a different file: two spellings that sanitize to one stem name
        # the same bytes but are different contract subjects.
        name_err = _design_name_error(best)
        if name_err is not None:
            return None, _ac.error_envelope("load_design", "load_param", name_err)
        root, _flat = _resolve_root(session)
        resolved = os.path.normpath(
            os.path.join(root, _naming.best_zmx_name(best))
        )
        return resolved, None

    # ``path``: reuse the merit-path resolver (relative-under-root + ..-escape guard).
    from ._merit_io import resolve_merit_path
    resolved, err = resolve_merit_path(session, params.get("path"))
    if err is not None:
        return None, _ac.error_envelope("load_design", "load_param", err)
    return resolved, None


# Appended to EVERY refusal issued after ``LoadFile`` ran: the caller must not be left
# believing the design it had loaded survived the refused load.
_PREVIOUS_GONE = (
    "the previous in-memory design is GONE (LoadFile replaced it and cannot be undone); "
    "load_design a known-good .zmx to recover"
)

# The grammar is ASCII-only on purpose: int()/float() also accept fullwidth digits and underscores,
# which are corruption here, never a spelling the engine writes.
_VERS_RE = re.compile(r"VERS( \S+)+\s*")
_INT_RE = re.compile(r"[0-9]+")
_DISZ_RE = re.compile(r"-?INFINITY|[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_SNIFF_LINE_MAX = 65536   # bound one read; the longest line in the corpus is ~100 chars


def _sniff_zmx(path):
    """STREAM ``path`` and return ``(refusal_text_or_None, record)``.

    UTF-16LE when the file starts with the ``FF FE`` BOM, else 8-bit (an older ANSI save
    still sniffs). Measured on 7622 engine saves, every one has: ``VERS ...`` as record 1,
    ``MODE <token>`` as record 2, one ``MNUM n ...``, and a ``SURF k`` + ``DISZ`` pair per
    surface with ``k`` contiguous ``0..N-1``. A file that deviates -- a missing/unparseable
    ``MNUM`` or ``DISZ``, a duplicate or gapped ``SURF`` id, a line longer than one read -- is
    refused rather than loaded with a weaker proof; a merit-function file shares the ``VERS``
    header but has no ``MODE``. ``record`` = ``{"n": N | None, "disz": {k: token}, "multi":
    bool}`` -- ``n`` is ``None`` for a file with no ``SURF`` record (not count-checkable);
    ``multi`` is true when ANY ``MNUM`` record has ``n > 1``. A decode fault raises
    ``UnicodeDecodeError`` and an open fault ``OSError`` (the caller maps both to
    ``load_not_found``).
    """
    ids, disz, mnums, n_rec = [], {}, [], 0
    with open(path, "rb") as raw:
        bom = raw.read(2) == b"\xff\xfe"
        raw.seek(2 if bom else 0)
        with io.TextIOWrapper(raw, encoding="utf-16-le" if bom else "latin-1",
                              errors="strict") as text:
            while True:
                line = text.readline(_SNIFF_LINE_MAX)
                if line == "":
                    break
                if len(line) >= _SNIFF_LINE_MAX and not line.endswith("\n"):
                    return "line too long", None
                if line.strip() == "":
                    continue
                n_rec += 1
                tok = line.split()
                if n_rec == 1:
                    if not _VERS_RE.fullmatch(line.rstrip("\r\n")):
                        return line.strip(), None
                elif n_rec == 2:
                    if tok[0] != "MODE" or len(tok) < 2:
                        return line.strip(), None
                elif tok[0] in ("VERS", "MODE"):   # a header record repeated after the header
                    return "repeated %s" % tok[0], None
                elif tok[0] == "SURF":
                    if len(tok) < 2 or not _INT_RE.fullmatch(tok[1]):
                        return "bad SURF id", None
                    ids.append(int(tok[1]))
                elif tok[0] == "DISZ" and not ids:   # a DISZ owned by no surface
                    return "DISZ before SURF", None
                elif tok[0] == "DISZ":
                    if len(tok) < 2 or not _DISZ_RE.fullmatch(tok[1]):
                        return "bad DISZ", None
                    if ids[-1] in disz:   # one DISZ per SURF (corpus 7622/7622); never pick one
                        return "duplicate DISZ", None
                    disz[ids[-1]] = tok[1]
                elif tok[0] == "MNUM":
                    if len(tok) < 2 or not _INT_RE.fullmatch(tok[1]) or int(tok[1]) < 1:
                        return "bad MNUM", None
                    if mnums:   # exactly one MNUM (corpus 7622/7622); never pick one of two
                        return "repeated MNUM", None
                    mnums.append(int(tok[1]))
    if n_rec < 2:
        return "no VERS/MODE", None
    if ids and (len(set(ids)) != len(ids) or set(ids) != set(range(len(ids)))):
        return "bad SURF ids", None
    if ids and not mnums:
        return "no MNUM", None
    if len(disz) != len(ids):
        return "SURF without DISZ", None
    return None, {"n": len(ids) or None, "disz": disz, "multi": any(n > 1 for n in mnums)}


def _precheck_file(path):
    """Pre-engine file checks: non-``.zmx`` -> ``load_param``; missing/0-byte ->
    ``load_not_found``; a file that is not an OpticStudio ``.zmx`` -> ``load_failed`` /
    ``not_a_zmx`` (loud, BEFORE the engine is touched). Returns ``(error_envelope | None,
    sniffed_record | None)``.
    """
    if not isinstance(path, str) or path.strip() == "":
        return _ac.error_envelope(
            "load_design", "load_param", f"resolved path is not usable: {path!r}"
        ), None
    if os.path.splitext(path)[1].lower() != ".zmx":
        return _ac.error_envelope(
            "load_design", "load_param",
            f"load_design only loads a .zmx design file, got {path!r}",
        ), None
    try:
        if not os.path.isfile(path):
            return _ac.error_envelope(
                "load_design", "load_not_found",
                f"no design file at {path!r} (it does not exist or is not a file)",
            ), None
        if os.path.getsize(path) <= 0:
            return _ac.error_envelope(
                "load_design", "load_not_found",
                f"the design file at {path!r} is empty (0 bytes); refusing to load it",
            ), None
        # The SNIFF: a garbage .zmx handed to LoadFile DESTROYS the loaded design and the
        # engine builds a blank one, so reject it here while the design is still intact.
        refusal, record = _sniff_zmx(path)
    except (OSError, ValueError) as exc:
        return _ac.error_envelope(
            "load_design", "load_not_found",
            f"could not stat or read the design file at {path!r} "
            f"({safe_exc(exc, repr_form=True)})",
        ), None
    if refusal is not None:
        shown = "".join(c for c in refusal if c.isprintable())[:16]
        return _ac.error_envelope(
            "load_design", "load_failed",
            f"{path!r} is not an OpticStudio .zmx design (sniff: {shown!r}); it was NOT "
            "loaded and the current design is UNCHANGED",
            reason="not_a_zmx",
            replaced_prior_system=False,
        ), None
    return None, record


def _first_thickness_mismatch(system, disz, count):
    """``(surface, loaded, declared)`` for the first loaded thickness that differs from the
    file's ``DISZ``, else ``None``. ``INFINITY`` <-> ``inf``. The sniff already guaranteed
    every surface carries a parseable token, so NOTHING is skipped here; a loaded thickness that
    cannot be read is a mismatch (``loaded`` is ``None``), never a pass.
    """
    for k in range(count):
        declared = float(disz[k])
        try:
            loaded = float(system.LDE.GetSurfaceAt(k).Thickness)
        except Exception:  # noqa: BLE001 — an unreadable thickness proves nothing
            return k, None, declared
        # The engine pins the IMAGE surface's thickness to +inf (the file records 0):
        # the shared image-only oracle exempts exactly that, on the last surface only.
        if k == count - 1 and _lc._image_thickness_ok(k, count, declared, loaded):
            continue
        if loaded != declared and not math.isclose(
                loaded, declared, rel_tol=1e-9, abs_tol=1e-12):
            return k, loaded, declared
    return None


def _paths_match(requested, loaded):
    """True iff ``loaded`` (the engine's SystemFile) resolves to ``requested`` (#59)."""
    if not isinstance(loaded, str) or loaded.strip() == "":
        return False
    try:
        a = os.path.normcase(os.path.normpath(requested))
        b = os.path.normcase(os.path.normpath(loaded))
    except (TypeError, ValueError):
        return False
    return a == b


def _apply_pin(system, pin, n_configs, result):
    """Apply the OPTIONAL ``pin_config`` after a load (MCE). Never fails the load.

    Validates ``pin`` (1..N), switches ``SetCurrentConfiguration(pin)`` read-back-proven,
    and updates ``result`` IN PLACE: ``pinned_configuration`` (the proven new active
    config) + a refreshed ``active_configuration``. A bad pin VALUE / an out-of-range pin
    / a switch that does not read-back-prove (a silent no-op, the S1 lever's guard) sets a
    ``pin_warning`` and leaves ``pinned_configuration:None`` — the load stays ``ok:true``
    (the pin is a convenience, never load-bearing). All guarded — NEVER raises.
    """
    try:
        cfg = _coerce_pin_config(pin)
    except ValueError as exc:
        result["pin_warning"] = (
            f"pin_config not applied ({safe_exc(exc)}); the design loaded fine but the active "
            "configuration was left as loaded"
        )
        return
    if n_configs is not None and not (1 <= cfg <= n_configs):
        result["pin_warning"] = (
            f"pin_config {cfg} out of range (valid 1..{n_configs}); the design loaded "
            "fine but the active configuration was left as loaded"
        )
        return
    try:
        system.MCE.SetCurrentConfiguration(cfg)
    except Exception as exc:  # noqa: BLE001 — a switch throw -> warn, load still ok
        result["pin_warning"] = (
            f"pin_config {cfg} could not be applied (SetCurrentConfiguration threw: "
            f"{safe_exc(exc, repr_form=True)}); the design loaded fine but the active config "
            "was left as loaded"
        )
        return
    # READ-BACK-AS-PROOF (the S1 lever's guard): a silent no-op switch reads back wrong.
    try:
        active_now = int(system.MCE.CurrentConfiguration)
    except Exception as exc:  # noqa: BLE001 — unreadable read-back -> unproven
        result["pin_warning"] = (
            f"pin_config {cfg} applied but the active config read-back is unreadable "
            f"({safe_exc(exc, repr_form=True)}); the pin is unproven"
        )
        return
    if active_now != cfg:
        result["pin_warning"] = (
            f"pin_config {cfg} did not take (SetCurrentConfiguration read back "
            f"{active_now}); a silent no-op switch — the active config is "
            f"{active_now}, not {cfg}"
        )
        return
    result["pinned_configuration"] = cfg
    result["active_configuration"] = active_now


@_never_raise("load_design")
def load_design(session, params):
    """Load a saved .zmx design from disk, with read-back-as-proof. See the docstring.

    Pass EXACTLY ONE of ``path`` (an absolute .zmx, or relative to the workspace root)
    OR ``best`` (a design name -> BEST_<name>.zmx under the workspace root). REPLACES
    the whole in-memory system (replaced_prior_system:true — no rollback). Returns the
    surface count + the loaded system_file + a blessed_for_tolerancing hint. Gotcha: a
    corrupt .zmx does NOT no-op — the engine destroys the loaded design and substitutes a
    blank one whose path matches, so the file is sniffed first (not_a_zmx, design
    untouched) and the load is proven against the file's own SURF count / DISZ (else
    load_failed with the previous design GONE). NEVER raises — inspect result.ok. See
    tolerance, save_candidate, promote_best.
    """
    resolved, err_env = _resolve_design_path(session, params)
    if err_env is not None:
        return err_env

    pre_err, record = _precheck_file(resolved)
    if pre_err is not None:
        return pre_err

    system = session.system
    # The engine's headless tolerancing report writer ONLY blesses a design loaded via
    # a FORWARD-SLASH path (probe-proven live: same file, fwd-slash -> ok report,
    # backslash -> 0-byte empty report). ``resolve_merit_path`` runs the path through
    # ``os.path.normpath`` -> BACKSLASHES on Windows, which the engine will NOT bless. So
    # the OS-native ``resolved`` is used ONLY for the local file pre-checks above; the
    # engine LoadFile is given a forward-slash path. (Idempotent on an already-fwd-slash
    # path; a UNC ``\\server\share`` -> ``//server/share``, which LoadFile accepts.)
    fwd = resolved.replace(os.sep, "/").replace("\\", "/")
    # The MOMENT ``system.LoadFile`` is called it destroys the prior in-memory design
    # REGARDLESS of whether the load proves out — so EVERY exit from this point on (an
    # explicit read-back load_failed, OR a throw from LoadFile / the read-back / classify
    # / the dict-build) MUST disclose ``replaced_prior_system:true`` (the engine WAS
    # touched + the prior system is gone). The pre-engine rejects above (load_param /
    # load_not_found / not_a_zmx, NO LoadFile call) leave the design intact: not_a_zmx says
    # ``replaced_prior_system:false`` explicitly, the other two omit the key.
    # This try/except is the disclosure firewall: it nets ANY throw in the post-LoadFile
    # region into a load_failed THAT CARRIES THE FLAG, so the outer ``_never_raise`` net
    # (which cannot know LoadFile already ran) is unreachable for this region. The except
    # body builds only a plain envelope -> it cannot itself raise. (H-2, L26 sibling.)
    try:
        # LoadFile REPLACES the whole system (the prior in-memory design is discarded).
        system.LoadFile(fwd, False)

        # S3 preserve_custom (§2.3): the new design has its OWN merit — the prior
        # wizard-block boundary is meaningless. Invalidate it (defense-in-depth; the
        # §2.4 sig-hash staleness check is authoritative, but S3 runs load_design ->
        # preserve loops and a spurious cross-design refuse is a papercut). Two trivial
        # lines, never raises.
        if hasattr(session, "_merit_wizard_boundary"):
            delattr(session, "_merit_wizard_boundary")

        # READ-BACK-AS-PROOF: a bad file does NOT silently no-op -- the engine can replace the
        # design with a blank one whose path still matches -- so verify a usable
        # surface count AND a SystemFile that resolves to the requested path, then (below) the
        # file's own SURF count and DISZ thicknesses.
        try:
            surfaces = int(system.LDE.NumberOfSurfaces)
        except Exception as exc:  # noqa: BLE001 — an unreadable count -> load unproven
            return _ac.error_envelope(
                "load_design", "load_failed",
                f"LoadFile returned but the surface count is unreadable "
                f"({safe_exc(exc, repr_form=True)}); the "
                f"load is unproven for {resolved!r}; {_PREVIOUS_GONE}",
                replaced_prior_system=True,
            )
        loaded_file = _tc._safe_system_file(system)
        if surfaces < 2 or not _paths_match(resolved, loaded_file):
            return _ac.error_envelope(
                "load_design", "load_failed",
                f"LoadFile did not faithfully load {resolved!r} (read-back: "
                f"NumberOfSurfaces={surfaces}, SystemFile={loaded_file!r}) — refusing to "
                f"claim a load that did not happen; {_PREVIOUS_GONE}",
                system_file=loaded_file,
                surfaces=surfaces,
                replaced_prior_system=True,
            )

        # What a blank substitute cannot fake is the file's OWN record (see the module
        # docstring): the loaded count must equal the file's SURF count and, on a
        # single-configuration file, each loaded thickness its DISZ.
        load_verified = "path_only"
        declared = record["n"] if record else None
        if declared is not None:
            if surfaces != declared:
                return _ac.error_envelope(
                    "load_design", "load_failed",
                    f"LoadFile of {resolved!r} produced {surfaces} surfaces but the file "
                    f"declares {declared}; {_PREVIOUS_GONE}",
                    reason="surface_count_mismatch",
                    system_file=loaded_file,
                    surfaces=surfaces,
                    replaced_prior_system=True,
                )
            load_verified = "count_only"
            if not record["multi"]:
                bad = _first_thickness_mismatch(system, record["disz"], declared)
                if bad is not None:
                    return _ac.error_envelope(
                        "load_design", "load_failed",
                        f"LoadFile of {resolved!r} loaded a different geometry than the "
                        f"file records: surface {bad[0]} thickness is {bad[1]!r} but the "
                        f"file's DISZ is {bad[2]!r}; {_PREVIOUS_GONE}",
                        reason="geometry_mismatch",
                        system_file=loaded_file,
                        surfaces=surfaces,
                        replaced_prior_system=True,
                    )
                load_verified = "geometry"

        blessed_hint, _warn = _tc.classify_system_file(loaded_file)

        # (MCE) disclose the active config + the config count AFTER the
        # read-back-as-proof block (the .zmx round-trips the active index for free, Q12).
        active_configuration, number_of_configurations, mce_rows = _read_config_state(system)

        # (S1 variable-lifecycle) DISCLOSE the inherited optimizer variables: a loaded .zmx
        # silently carries in its Variable solves (they SURVIVE a load), so a
        # later optimize would run them without the agent knowing. Guarded -> ([], None) on a
        # read fault (a disclosure must NEVER break a successful load), with a warning so
        # None is distinguishable from "read, zero inherited".
        inherited, n_inherited = _oc._disclose_inherited_variables(system)

        result = {
            "ok": True,
            "tool": "load_design",
            "replaced_prior_system": True,
            "surfaces": surfaces,
            "system_file": loaded_file,
            # How much of the load the file's own record proved: "geometry" (count + every
            # DISZ), "count_only" (multi-config), "path_only" (no SURF record).
            "load_verified": load_verified,
            "blessed_for_tolerancing": blessed_hint,
            "active_configuration": active_configuration,
            "number_of_configurations": number_of_configurations,
            "mce_rows": mce_rows,          # GAP 9 (S6): MCE.NumberOfOperands, throw-guarded -> None
            # The optional pin echo (None when no pin was requested).
            "pinned_configuration": None,
            # (S1) the inherited-variable disclosure (additive; byte-identical default).
            "inherited_variables": inherited,
            "n_inherited_variables": n_inherited,
        }
        if n_inherited is None:
            result["inherited_variables_warning"] = (
                "could not enumerate the inherited optimizer variables after the load "
                "(the disclosure read faulted); the load itself succeeded"
            )

        # (S1) OPTIONAL reset_variables: clear EVERY inherited variable AFTER the load.
        # STRICT ``is True`` (the force/pin precedent — a "no"/1/None does NOT reset). The
        # LOAD is the deliverable: a reset that returns a residual does NOT
        # fail the load — it stamps the reset_result + a reset_warning, result stays
        # ok:true. load_design has NO atomic checkpoint, so the reset is a straight
        # post-disclosure step (no rollback interaction) via the SAME shared core (L30).
        reset_requested = (
            isinstance(params, dict) and params.get("reset_variables") is True
        )
        if reset_requested:
            reset_result = _oc._clear_all_variables_core(system)
            result["reset_result"] = reset_result
            # Re-disclose post-reset (now 0 on a clean clear).
            result["inherited_variables"], result["n_inherited_variables"] = \
                _oc._disclose_inherited_variables(system)
            # The re-disclosure overwrites n_inherited_variables, so a FAULTING
            # re-disclosure (None) must carry the SAME warning the first disclosure stamps —
            # otherwise n_inherited_variables:None ships with NO warning (the exact None-vs-0
            # ambiguity the helper prevents). Keep the warning <-> None invariant in sync.
            if result["n_inherited_variables"] is None:
                result["inherited_variables_warning"] = (
                    "could not enumerate the inherited optimizer variables after the reset "
                    "(the re-disclosure read faulted); the load + reset themselves succeeded"
                )
            else:
                result.pop("inherited_variables_warning", None)
            if not reset_result.get("ok"):
                result["reset_warning"] = (
                    "reset_variables did not fully clear the inherited variables "
                    f"({reset_result.get('error')}); the load itself succeeded — see "
                    "reset_result.unclear_residual"
                )

        # (MCE) OPTIONAL pin_config: re-assert a specific active config
        # after the load, read-back-proven. The LOAD is the deliverable — a bad / unmet
        # pin NEVER fails the whole load (it leaves a pin_warning; result stays ok:true).
        pin = params.get("pin_config") if isinstance(params, dict) else None
        if pin is not None:
            _apply_pin(system, pin, number_of_configurations, result)

        return result
    except Exception as exc:  # noqa: BLE001 — LoadFile / classify / dict-build throw
        # The prior system was already destroyed by LoadFile (or LoadFile itself threw
        # mid-replace) — disclose it so the caller is NOT falsely told the prior is intact.
        return _ac.error_envelope(
            "load_design", "load_failed",
            f"load_design hit an error AFTER LoadFile was issued for {resolved!r} "
            f"({safe_exc(exc, repr_form=True)}); the prior in-memory design was already "
            f"replaced — {_PREVIOUS_GONE}",
            replaced_prior_system=True,
        )


LOAD_DESIGN_SPEC = ToolSpec(
    name="load_design",
    handler=load_design,
    required_params=(),
    param_types={"path": "string", "best": "string", "pin_config": "number",
                 "reset_variables": "boolean"},
    description=(
        "Load a saved .zmx design from disk (the recovery partner of save_candidate / "
        "promote_best). Pass EXACTLY ONE of path (an absolute .zmx, or relative to the "
        "workspace root) OR best (a design name -> BEST_<name>.zmx under the workspace "
        "root). REPLACES the whole in-memory system (replaced_prior_system:true — no "
        "rollback). Returns surfaces + system_file + a blessed_for_tolerancing hint + "
        "active_configuration + number_of_configurations + the inherited optimizer "
        "variables a loaded .zmx carries in (inherited_variables / n_inherited_variables — "
        "they survive a load; list_variables / clear_all_variables manage them). Optional "
        "reset_variables=true clears every inherited variable after the load (freeze-at-"
        "current, not a value reset). Optional pin_config (int) re-asserts a specific "
        "active configuration after the load (read-back-proven; a bad/unmet pin leaves a "
        "pin_warning, the load still succeeds). "
        "Gotcha: tolerancing blesses ONLY a saved design loaded fresh from disk — a "
        "freshly-built in-memory system produces an empty tolerance report, so "
        "load_design your saved .zmx before tolerance. A corrupt file does NOT no-op: "
        "OpticStudio destroys the loaded design and builds a blank one, so the file is "
        "sniffed first (not_a_zmx — design untouched) and the load is proven against the "
        "file's own surface count and thicknesses (load_verified: geometry | count_only | "
        "path_only; a mismatch is load_failed and the previous design is GONE — load a "
        "known-good .zmx). NEVER raises — inspect result.ok. See tolerance."
    ),
)

TOOL_SPECS = (LOAD_DESIGN_SPEC,)
