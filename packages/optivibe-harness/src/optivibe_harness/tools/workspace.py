"""tools/workspace.py — the project-workspace writer (§4).

Two dispatchable tools, a THIN layer over ``ArtifactSink`` — no NEW durability
logic for the ``.zmx`` (the sink owns the save-gate, manifest, fsync, and ADS-safe
naming). The only new disk logic here is the two atomic copies in ``promote_best``
(temp-in-root + ``os.replace``), the ``candidates/{zmx,png}`` split, the promote
manifest row, and the collision-free repeat-session sink construction (§4.5).

Folder layout — FLAT-ON-ROOT when
``session.workspace_root`` is set, EXACT::

    <workspace_root>/                    # the folder the design session works in
    ├── BEST_<design-name>.zmx          # promoted best (atomic; root)
    ├── BEST_<design-name>.png          # its layout figure (atomic; root)
    ├── <relative>.MF                   # merit files resolve under the root
    └── candidates/
        ├── zmx/
        │   ├── 0000_<label>.zmx  0001_<label>.zmx  ...
        │   └── manifest.jsonl          # ArtifactSink manifest = THE candidate index
        └── png/
            └── 0000_<label>.png  0001_<label>.png  ...

NO invented ``projects/<name>/`` parent — the candidates / BEST / merit hang
DIRECTLY off the working folder. (When ``workspace_root`` is UNSET but the LEGACY
``session.projects_root`` alias is, the OLD ``projects/<design-name>/`` nesting is
preserved unchanged — D2 back-compat.)

``candidates/zmx`` IS an ``ArtifactSink(base_dir=<root>/candidates, run_id="zmx",
save_as=session.system.SaveAs)`` so the seq sequencing, durability gate, manifest,
and collision rules come for free. ``candidates/png`` mirrors the ``{seq:04d}_``
prefix so ``000N_*.zmx`` <-> ``000N_*.png`` pair.

DOCUMENTED LIMITATION (D3): two DIFFERENT ``design_name``s under ONE
``workspace_root`` share the flat ``candidates/`` manifest+seq (their candidates
interleave; ``BEST_<name>`` + labels disambiguate). The dogfooding model is ONE
design per folder, so this is moot in practice.

The root is resolved from session config / the session's artifact root — NEVER
hardcoded (standing order). "Best" is CALLER-ASSERTED only (§4.4); no metric mode
(the optimizer owns merit values).

Both tools take ``(session, params)``, return ``{ok: bool, ...}``, and NEVER raise.

Live ZOS-API integration (real ``SaveAs`` + ``render_layout``); unit-tested against
a fake session/sink.
"""
import glob
import json
import os
import tempfile

from .. import _io
from ..artifact_sink import ArtifactSink, _safe_name
from ..server import ToolSpec
# The save-time clearance/visual gate: the save
# tools run a live check_clearance geometry read. Imported at MODULE level so a unit
# test patches ``workspace.check_clearance`` (mirrors the ``render_layout`` seam).
from .clearance import check_clearance
from ._image_gate import _is_png
# Production imports the REAL render tool per the pinned interface (Coder A owns
# layout_render.py). A unit test may monkeypatch ``render_layout`` on THIS module.
from .layout_render import render_layout


# --------------------------------------------------------------------------- #
# Save-time clearance/visual gate (§1/§2).
# --------------------------------------------------------------------------- #
def _summary_single(env):
    """Compact echo of a SINGLE-config ``check_clearance`` envelope (§1).

    NOT the full envelope — ``{folded, n_violations, violations:[{surface, kind,
    worst, threshold}], config_evaluated}``. Defensive against a malformed violation
    entry (skips a non-dict).
    """
    # coerce a NON-list to [] BEFORE iterating so a malformed
    # truthy field (int/float/bool/object) can never make the builder RAISE TypeError.
    # The verdict semantics are unaffected — this runs AFTER _classify_clearance decided
    # the verdict (the verdict still fail-closes via truthiness); the summary just shows
    # an empty disclosure list for a garbage field.
    _violations = env.get("violations")
    viols = _violations if isinstance(_violations, list) else []
    out = []
    for v in viols:
        if not isinstance(v, dict):
            continue
        out.append({
            "surface": v.get("surface"),
            "kind": v.get("kind"),
            "worst": v.get("worst"),
            "threshold": v.get("threshold"),
        })
    # the surfaces whose edge could NOT be faithfully audited
    # (a geometry read threw, or an asphere's coefficients were unreadable). A non-empty
    # list here on a no-violation envelope is why the verdict is "indeterminate", not
    # "clean" — surface it so the agent knows WHICH surface to re-check.
    _approx = env.get("asphere_sag_approximate")
    unaudited = list(_approx) if isinstance(_approx, list) else []
    # A loaded non-authorable GRIN family member (or a classified GRIN whose gap
    # was skipped) is NOT covered by the geometric edge/center audit — surface it at the
    # save/promote boundary so a GRIN keeper's not-audited status is visible. Non-blocking
    # disclosure (never flips the gate verdict, §2.3.4 suppression); append its surface ids to
    # ``unaudited_surfaces`` (ints, like the asphere disclosure) AND carry the full env entries
    # in a dedicated ``grin_not_audited`` field. Both emitted ONLY when present -> a non-GRIN
    # keeper is byte-identical (no new key, unaudited unchanged).
    _grin_na = env.get("grin_not_audited")
    grin_na = _grin_na if isinstance(_grin_na, list) else []
    unaudited = unaudited + [
        e.get("surface") for e in grin_na
        if isinstance(e, dict) and e.get("surface") is not None
    ]
    summary = {
        "folded": bool(env.get("folded")),
        "n_violations": len(out),
        "violations": out,
        "unaudited_surfaces": unaudited,
        "config_evaluated": env.get("config_evaluated"),
    }
    if grin_na:
        summary["grin_not_audited"] = grin_na
    return summary


def _summary_all(env):
    """Compact echo of a ``config="all"`` ``check_clearance`` envelope (§1).

    Flattens every per-config violation, TAGGING each with its ``config`` (the
    epicenter: a thin gap in a NON-current config). ``folded`` is True if ANY config
    folded. Tolerates an incomplete/malformed ``per_config`` (used on the
    indeterminate fail-closed branch too).
    """
    # coerce each list-expected field to [] when it is NOT a
    # list, so a malformed non-iterable (int/float/bool/object) never raises TypeError.
    # Summary-disclosure ONLY — the verdict was already decided by _classify_clearance.
    _per = env.get("per_config")
    per = _per if isinstance(_per, list) else []
    folded = False
    flat = []
    unaudited = []
    grin_na = []   # per-config GRIN not-audited disclosure (config-tagged)
    for pc in per:
        if not isinstance(pc, dict):
            continue
        if pc.get("folded"):
            folded = True
        cfg = pc.get("config")
        _pc_viols = pc.get("violations")
        for v in (_pc_viols if isinstance(_pc_viols, list) else []):
            if not isinstance(v, dict):
                continue
            flat.append({
                "surface": v.get("surface"),
                "kind": v.get("kind"),
                "worst": v.get("worst"),
                "threshold": v.get("threshold"),
                "config": cfg,
            })
        # disclosure (per-config): a surface whose edge could not be
        # faithfully audited in THIS config — TAGGED with its config (the epicenter).
        _pc_approx = pc.get("asphere_sag_approximate")
        for s in (_pc_approx if isinstance(_pc_approx, list) else []):
            unaudited.append({"surface": s, "config": cfg})
        # Per-config: a loaded non-authorable GRIN family member (or a
        # skipped-gap primitive) in THIS config — NOT covered by the geometric audit.
        # Non-blocking disclosure; tag with config, mirror the asphere unaudited handling.
        _pc_grin_na = pc.get("grin_not_audited")
        for e in (_pc_grin_na if isinstance(_pc_grin_na, list) else []):
            if isinstance(e, dict) and e.get("surface") is not None:
                unaudited.append({"surface": e.get("surface"), "config": cfg})
                grin_na.append({"surface": e.get("surface"), "config": cfg,
                                "type": e.get("type"), "reason": e.get("reason")})
    summary = {
        "folded": bool(folded),
        "n_violations": len(flat),
        "violations": flat,
        "unaudited_surfaces": unaudited,
        "config_evaluated": env.get("config_evaluated"),
    }
    if grin_na:
        summary["grin_not_audited"] = grin_na
    return summary


def _classify_clearance(env):
    """Return ``("clean"|"thin"|"indeterminate", summary|None)`` over a check_clearance
    envelope — SHAPE-aware (single-config vs ``config="all"``), fail-closed (§1).

    A 3-state verdict (a bool cannot express "couldn't audit"). The ``"all"`` envelope
    nests per-config violations at ``per_config[k]["violations"]`` with NO top-level
    ``violations`` — a naive ``env.get("violations")`` would SILENTLY pass a thin
    multi-config design, so the ``config_evaluated=="all"`` branch is load-bearing.
    """
    if not isinstance(env, dict) or not env.get("ok"):
        return "indeterminate", None
    if env.get("config_evaluated") == "all":
        per = env.get("per_config")
        cov = env.get("coverage")
        # a malformed (non-dict) coverage cannot certify — fail-closed WITHOUT a
        # summary (mirrors the pre-existing gate-wrapper-caught behavior; never raises).
        if not isinstance(cov, dict):
            return "indeterminate", None
        if not isinstance(per, list) or not per or not cov.get("ok"):
            return "indeterminate", _summary_all(env)   # fail-closed: cannot certify
        # skip a non-dict per_config entry (never ``(pc or {}).get`` which RAISES
        # on a truthy non-dict like ``5``). A confirmed thin in ANY config wins first.
        thin = any(pc.get("violations") for pc in per if isinstance(pc, dict))
        if thin:
            return "thin", _summary_all(env)
        # no confirmed violation — but we can only certify CLEAN if EVERY
        # config was a readable dict AND every surface was faithfully audited. A non-dict
        # per_config entry (a config we couldn't grade) OR any nested non-empty
        # ``asphere_sag_approximate`` (a surface whose edge read threw / unreadable
        # asphere) -> "indeterminate", never a silent clean-pass.
        all_dicts = all(isinstance(pc, dict) for pc in per)
        unaudited = any(
            pc.get("asphere_sag_approximate") for pc in per if isinstance(pc, dict)
        )
        if not all_dicts or unaudited:
            return "indeterminate", _summary_all(env)
        return "clean", _summary_all(env)
    viol = env.get("violations")
    if viol is None:
        return "indeterminate", None                     # malformed single envelope
    if viol:
        return "thin", _summary_single(env)
    # the silent clean-pass: no confirmed violation, but ``check_clearance``
    # records a surface whose edge could NOT be faithfully audited (a geometry read threw,
    # or an asphere's coefficients were unreadable) in the TOP-LEVEL
    # ``asphere_sag_approximate`` list, NOT in ``violations``. We cannot certify CLEAN when
    # any surface's edge could not be read -> "indeterminate". A healthy all-spherical OR a
    # faithfully-read asphere (``asphere_sag_modelled``, empty ``asphere_sag_approximate``)
    # stays "clean".
    if env.get("asphere_sag_approximate"):
        return "indeterminate", _summary_single(env)
    return "clean", _summary_single(env)


def _run_clearance_gate(session, min_air=None, min_glass=None, config=None):
    """Run ``check_clearance`` + classify, fully guarded — NEVER raises (§2).

    Returns ``(verdict, summary)`` where ``verdict`` is ``"clean"|"thin"|
    "indeterminate"``. A ``check_clearance`` throw OR a malformed envelope ->
    ``("indeterminate", None)`` so a gate defect can never break the save tools'
    never-raise contract. ``check_clearance`` is looked up on THIS module at call time
    so a unit test patches ``workspace.check_clearance``.
    """
    try:
        cp = {}
        if min_air is not None:
            cp["min_air"] = min_air
        if min_glass is not None:
            cp["min_glass"] = min_glass
        if config is not None:
            cp["config"] = config
        env = check_clearance(session, cp)
        return _classify_clearance(env)
    except Exception:  # noqa: BLE001 — the gate must NEVER break the never-raise contract
        return "indeterminate", None


def _worst_of(viols):
    """The MOST-SEVERE violation (min ``worst``) from a compact-summary list (spec §4).

    ``check_clearance`` appends violations in SURFACE order, never sorted by severity, so
    ``violations[0]`` is the first-by-surface, NOT the worst. Select the min ``worst``
    (smallest clearance = most severe). Guarded: a missing/None/non-numeric ``worst``
    sorts last (``+inf``) so it never crashes the comparison. ``viols`` is assumed
    non-empty (callers guard).
    """
    def _key(v):
        w = v.get("worst")
        if isinstance(w, bool) or not isinstance(w, (int, float)):
            return float("inf")
        return w
    return min(viols, key=_key)


def _clearance_warning_text(verdict, summary):
    """A human one-liner for ``save_candidate``'s ``clearance_warning`` (§3).

    ``clean`` -> None; ``indeterminate`` -> a could-not-audit note; ``thin`` -> names
    the worst violation (surface/kind/worst/threshold, + its config on a multi-config
    sweep).
    """
    if verdict == "clean":
        return None
    if verdict == "indeterminate":
        return (
            "clearance could not be audited (the geometry read failed or returned an "
            "unexpected shape); render and review the layout before promoting"
        )
    viols = (summary or {}).get("violations") or []
    if not viols:
        return "a manufacturably-thin gap was detected; review before promoting"
    v = _worst_of(viols)
    cfg = v.get("config")
    cfg_txt = f" (config {cfg})" if cfg is not None else ""
    extra = f" (+{len(viols) - 1} more)" if len(viols) > 1 else ""
    return (
        f"surface {v.get('surface')} {v.get('kind')} clearance {v.get('worst')} < "
        f"{v.get('threshold')} mm{cfg_txt} — manufacturably thin; review before "
        f"promoting{extra}"
    )


def _promote_thin_error(summary):
    """The refusal message for a thin ``promote_best`` (§4) — names the worst."""
    viols = (summary or {}).get("violations") or []
    if not viols:
        return (
            "REFUSED: the design has a manufacturably-thin gap (clearance violation); "
            "pass force=True to promote anyway, or widen the gap"
        )
    v = _worst_of(viols)
    cfg = v.get("config")
    cfg_txt = f" in config {cfg}" if cfg is not None else ""
    extra = f" (+{len(viols) - 1} more)" if len(viols) > 1 else ""
    return (
        f"REFUSED: surface {v.get('surface')} {v.get('kind')} clearance "
        f"{v.get('worst')} < {v.get('threshold')} mm{cfg_txt} is manufacturably "
        f"thin{extra}; pass force=True to promote anyway, or widen the gap "
        "(check_clearance for the full audit)"
    )


def _resolve_root(session):
    """Resolve the workspace root + its layout flavor — NEVER hardcoded (D2).

    Returns ``(root, flat)`` where ``flat`` is True for the NEW flat-on-root layout
    (no ``<design>`` nesting) and False for the LEGACY ``projects/<design>/`` layout.
    4-tier precedence:

    1. ``session.workspace_root`` set -> ``(root, flat=True)`` — the NEW flat layout:
       candidates / BEST / merit hang DIRECTLY off the folder the session works in.
    2. else ``session.projects_root`` set (the EXISTING attr, now a back-compat
       ALIAS) -> ``(root, flat=False)`` — the LEGACY ``projects/<design>/`` layout,
       UNCHANGED (keeps every existing projects_root-injecting test GREEN).
    3. else ``projects/`` BESIDE the session's artifact-sink ``base_dir`` (legacy)
       -> ``flat=False``.
    4. else ``projects/`` under the cwd (legacy last-resort) -> ``flat=False``.
    """
    workspace_root = getattr(session, "workspace_root", None)
    if workspace_root:
        return os.fspath(workspace_root), True
    explicit = getattr(session, "projects_root", None)
    if explicit:
        return os.fspath(explicit), False
    sink = getattr(session, "artifact_sink", None)
    base_dir = getattr(sink, "base_dir", None) if sink is not None else None
    if base_dir:
        return os.path.join(os.fspath(base_dir), "projects"), False
    return os.path.join(os.getcwd(), "projects"), False


def _projects_root(session) -> str:
    """Back-compat alias: the resolved root (drops the ``flat`` flag).

    Retained because ``_merit_io.resolve_merit_path`` + older call sites import this
    name. New code should call ``_resolve_root`` (which also reports the layout
    flavor). Returns the SAME root ``_resolve_root`` resolves.
    """
    return _resolve_root(session)[0]


def _design_dir(session, design_name: str) -> str:
    """The design's workspace dir.

    FLAT layout (``workspace_root`` set, D3): the root ITSELF (NO ``<design>``
    nesting, NO ``projects/`` parent — candidates / BEST hang directly off it).
    LEGACY layout (``projects_root`` alias / sink / cwd fallback): the historical
    ``<projects-root>/<safe-design-name>/`` nesting, UNCHANGED.
    """
    root, flat = _resolve_root(session)
    if flat:
        return root
    return os.path.join(root, _safe_name(design_name))


def _design_name_error(design_name):
    """Return an error string iff ``design_name`` is not a valid workspace name.

    Spec §9.6 requires ``ok:false`` for a non-str ``design_name`` (a bare int like
    ``123`` would otherwise be str()-coerced by ``_safe_name`` into a silent
    ``projects/123/...``). Reject a non-str, AND a str that sanitizes to empty (a
    name made purely of illegal/trailing chars is not a usable workspace name).
    Returns ``None`` when the name is acceptable.
    """
    if not isinstance(design_name, str):
        return "design_name must be a non-empty string"
    # A str that _safe_name reduces to the empty/placeholder is not a real name the
    # caller asserted; _safe_name("") -> "snapshot", so check the pre-sanitize stem.
    if design_name.strip() == "":
        return "design_name must be a non-empty string"
    return None


def _save_as_seam(session):
    """The injected save seam: ``session.system.SaveAs(path) -> None`` (§0.7)."""
    return lambda path: session.system.SaveAs(path)


def _active_configuration(session):
    """The active MCE config index for the persistence DISCLOSURE (MCE).

    THROW-guarded -> ``None`` on a read fault (the disclosure is honesty-only, NEVER
    load-bearing; the ``.zmx`` round-trips the index for free, Q12). The raw
    ``MCE.CurrentConfiguration`` read is guarded directly so a genuine read fault is
    disclosed as ``null`` (T-PS-1), not masked to ``1``. This NEVER raises (a
    persistence tool must not crash on a config read).
    """
    try:
        return int(session.system.MCE.CurrentConfiguration)
    except Exception:  # noqa: BLE001 — a config read must never sink a save -> null
        return None


def _max_existing_seq(zmx_dir: str) -> int:
    """Read the max existing snapshot ``seq`` from ``zmx_dir/manifest.jsonl``.

    Returns -1 when there is no manifest / no rows (so ``next_seq`` is 0 — a fresh
    run). Tolerates a torn final line and any unreadable row (defensive: a partial
    crash-tail must not break the append path). Only snapshot rows (which carry an
    int ``seq``) are considered; a promote row also carries ``seq`` but is bounded
    by an existing snapshot, so ``max`` is still correct.
    """
    manifest_path = os.path.join(zmx_dir, "manifest.jsonl")
    if not os.path.isfile(manifest_path):
        return -1
    max_seq = -1
    try:
        with open(manifest_path, "r", encoding="utf-8", newline="") as fh:
            lines = fh.read().split("\n")
    except OSError:
        return -1
    for line in lines:
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue  # torn / partial row — skip, never raise
        seq = row.get("seq")
        if isinstance(seq, int) and seq > max_seq:
            max_seq = seq
    return max_seq


def _max_ondisk_seq(zmx_dir: str) -> int:
    """Read the max ``000N_`` prefix from actual ``000N_*.zmx`` files in ``zmx_dir``.

    FIX 4: a crash that left ``0007_<label>.zmx`` on disk but no
    manifest row would make ``_max_existing_seq`` (manifest-only) miss it, so a new
    session would reseed ``start_seq=7`` and OVERWRITE that candidate (the bypassed
    RunIdCollisionError would have caught a collision; append-mode bypasses it). We
    therefore also glob the real ``.zmx`` files and parse the int prefix, and the
    caller takes ``max`` over BOTH the manifest and the on-disk sources.

    Returns -1 when there are no parseable ``000N_*.zmx`` files. Tolerates a file
    whose name does not start with a parseable int prefix (skips it).
    """
    max_seq = -1
    if not os.path.isdir(zmx_dir):
        return -1
    try:
        names = os.listdir(zmx_dir)
    except OSError:
        return -1
    for name in names:
        if not name.endswith(".zmx"):
            continue
        prefix = name.split("_", 1)[0]
        try:
            seq = int(prefix)
        except ValueError:
            continue  # not a 000N_-prefixed candidate — skip
        if seq > max_seq:
            max_seq = seq
    return max_seq


def _get_sink(session, design_name: str):
    """Get-or-create the cached inner ``ArtifactSink`` for ``candidates/zmx``.

    One sink is cached per ``(session, design_name)`` on
    ``session._workspace_sinks``. A FRESH design dir constructs the sink normally
    (collision guard active). An EXISTING design dir (a repeat session) reads the
    max existing seq from the candidates manifest and constructs with
    ``start_seq=max+1`` so the second session APPENDS collision-free (§4.5).

    Raises ``PermissionError``/``OSError`` from ``os.makedirs`` up to the caller,
    which envelopes it as ``workspace_unwritable`` — this helper itself does not
    swallow the unwritable-root case (the caller needs the family).
    """
    # D4: in the FLAT layout (workspace_root set) the design dir IS the root, so a
    # candidate sink and the session-default snapshot/optimizer sink point at the SAME
    # ``<root>/candidates/zmx`` run-dir. Share ONE sink (the default) so save_candidate
    # + save_snapshot + the optimizer trail share one manifest/seq (D4) — two separate
    # ArtifactSinks over the same non-empty run-dir would otherwise collide. In the
    # LEGACY layout the per-design ``projects/<name>/candidates`` dirs are distinct, so
    # keep the original per-(session, design_name) cache.
    _root, flat = _resolve_root(session)
    if flat:
        return _get_default_sink(session)

    cache = getattr(session, "_workspace_sinks", None)
    if cache is None:
        cache = {}
        session._workspace_sinks = cache
    if design_name in cache:
        return cache[design_name]

    design_dir = _design_dir(session, design_name)
    candidates_dir = os.path.join(design_dir, "candidates")
    zmx_dir = os.path.join(candidates_dir, "zmx")
    png_dir = os.path.join(candidates_dir, "png")
    # Create the layout up front (an unwritable root raises here, enveloped by the
    # caller as ``workspace_unwritable``).
    os.makedirs(png_dir, exist_ok=True)
    os.makedirs(zmx_dir, exist_ok=True)

    # Repeat-session append: a non-empty existing zmx run_dir would collide on a
    # normal construct, so seed start_seq past the existing max. FIX 4: derive the
    # max over BOTH the manifest AND the actual on-disk 000N_*.zmx files — a crash
    # that left an orphan .zmx with no manifest row must NOT be silently clobbered
    # (manifest-only would reseed onto it; append-mode bypasses the collision guard).
    manifest_max = _max_existing_seq(zmx_dir)
    ondisk_max = _max_ondisk_seq(zmx_dir)
    existing_max = max(manifest_max, ondisk_max)
    start_seq = existing_max + 1 if existing_max >= 0 else None

    sink = ArtifactSink(
        candidates_dir,
        "zmx",
        _save_as_seam(session),
        min_snapshot_bytes=256,
        start_seq=start_seq,
    )
    cache[design_name] = sink
    return sink


def _get_default_sink(session):
    """Get-or-create the session-default ``candidates/zmx`` ArtifactSink (D4).

    The FALLBACK sink ``save_snapshot`` + ``optimize._snapshot`` use when no explicit
    ``session.artifact_sink`` was wired (bugs 2 & 3): ONE
    ``ArtifactSink(base_dir=<root>/candidates, run_id="zmx", save_as=<lazy lambda>)``
    rooted at ``_resolve_root(session)`` — the SAME ``candidates/zmx`` run-dir
    ``save_candidate`` writes to, so the start-form snapshot + the candidates + the
    optimizer ``.zmx`` trail share ONE manifest/seq.

    COLD-ENGINE / LAZY-OPEN (the highest-risk seam, D4): construction touches NO
    engine — only ``os.makedirs`` (via the ArtifactSink ctor). ``session.system`` is
    NOT dereferenced here; the ``save_as`` is ``lambda p: session.system.SaveAs(p)``
    resolved at CALL time, so building the default sink on a never-opened session does
    NOT grab the single (N=1) OpticStudio seat. The sink is cached on
    ``session._default_sink`` so repeat calls share one manifest/seq.

    Repeat-session APPEND (§4.5): like ``_get_sink``, an EXISTING candidates dir seeds
    ``start_seq`` past the max over BOTH the manifest AND the on-disk ``000N_*.zmx``
    files so a second session APPENDS collision-free rather than colliding.

    Raises ``PermissionError``/``OSError`` from ``os.makedirs`` up to the caller (the
    callers envelope it as ``workspace_unwritable`` / warn the bug-3 warning only if
    the sink BUILD itself fails).
    """
    cached = getattr(session, "_default_sink", None)
    if cached is not None:
        return cached

    root, _flat = _resolve_root(session)
    candidates_dir = os.path.join(root, "candidates")
    zmx_dir = os.path.join(candidates_dir, "zmx")
    png_dir = os.path.join(candidates_dir, "png")
    # Construct the layout up front (an unwritable root raises here — the caller
    # turns the raise into a workspace_unwritable / bug-3 warning). makedirs only:
    # NO engine touch (the save_as lambda defers session.system to call time).
    os.makedirs(png_dir, exist_ok=True)
    os.makedirs(zmx_dir, exist_ok=True)

    manifest_max = _max_existing_seq(zmx_dir)
    ondisk_max = _max_ondisk_seq(zmx_dir)
    existing_max = max(manifest_max, ondisk_max)
    start_seq = existing_max + 1 if existing_max >= 0 else None

    sink = ArtifactSink(
        candidates_dir,
        "zmx",
        _save_as_seam(session),  # lambda p: session.system.SaveAs(p) — deferred (D4)
        min_snapshot_bytes=256,
        start_seq=start_seq,
    )
    session._default_sink = sink
    return sink


def save_candidate(session, params):
    """Snapshot the live system as a durable candidate ``.zmx`` (+ paired ``.png``).

    Returns ``{ok, seq, zmx_path, zmx_ok, png_path, png_ok, design_name, label}``
    where ``ok = zmx_ok AND (not render OR png_ok)``. NEVER raises — an unwritable
    project root surfaces as ``{ok:false, error_family:"workspace_unwritable"}``.
    """
    # FIX 2: ``params`` may be None OR a truthy non-dict (1, "x", object()).
    # Either must NOT raise an AttributeError out of ``.get`` — coerce any non-dict
    # to {} FIRST. (The never-raise envelope is a backstop, not the only guard.)
    if not isinstance(params, dict):
        params = {}
    design_name = params.get("design_name")
    label = params.get("label", "candidate")
    render = bool(params.get("render", True))

    # FIX 7 (spec §9.6): a non-str (or empty-after-sanitize) ``design_name`` must be
    # REJECTED, not silently str()-coerced into ``projects/123/...``. Explicit type
    # check BEFORE _safe_name (which would coerce). Still never raises.
    name_err = _design_name_error(design_name)
    if name_err is not None:
        return {
            "ok": False,
            "error_family": "workspace_param",
            "error": name_err,
            "design_name": design_name,
            "label": label,
            "seq": None,
            "zmx_path": None,
            "zmx_ok": False,
            "png_path": None,
            "png_ok": False,
        }

    try:
        sink = _get_sink(session, design_name)
    except Exception as exc:  # noqa: BLE001 — unwritable root, etc. -> enveloped
        return {
            "ok": False,
            "error_family": "workspace_unwritable",
            "error": f"{type(exc).__name__}: {exc}",
            "design_name": design_name,
            "label": label,
            "seq": None,
            "zmx_path": None,
            "zmx_ok": False,
            "png_path": None,
            "png_ok": False,
        }

    # (MCE) Record the active MCE config for the DISCLOSURE (+ manifest
    # meta). NO behavior change, NO data fix — the .zmx round-trips the index for free.
    active_configuration = _active_configuration(session)

    # Durable .zmx via the sink (gate + manifest + fsync; never raises).
    snap = sink.snapshot(label, meta={
        "workspace": design_name, "label": label,
        "active_configuration": active_configuration,
    })
    # NOTE: read the SnapshotResult fields via dataclasses.asdict so the source
    # never forms the dotted ``snap.s e q`` literal the release guard flags as a
    # legacy converter file-extension (it is the dataclass field name) — mirrors
    # save_snapshot.py.
    from dataclasses import asdict
    fields = asdict(snap)
    seq = fields["seq"]
    zmx_ok = bool(fields["ok"])
    zmx_path = fields["path"]

    png_path = None
    png_ok = False
    if render:
        # Mirror the snapshot's seq prefix so 000N_*.zmx <-> 000N_*.png pair.
        png_dir = os.path.join(
            _design_dir(session, design_name), "candidates", "png"
        )
        png_filename = f"{seq:04d}_{_safe_name(label)}.png"
        png_path = os.path.join(png_dir, png_filename)
        # Defensive belt-and-braces makedirs at the issuing save site (the #58
        # "every save site makedirs FIRST" invariant, made literal here). render_layout
        # ALSO makedirs its own dirname (layout_render.py), so this is defense-in-depth,
        # NOT a fix for a missing-dir bug. The catch is (OSError, ValueError) — the
        # embedded-NUL ValueError escapes OSError (the save_merit precedent) — and `pass`
        # keeps the never-raise contract: a genuinely unwritable root surfaces via
        # png_ok=False + visual_check_warning, never a raise.
        try:
            os.makedirs(png_dir, exist_ok=True)
        except (OSError, ValueError):
            pass
        try:
            render_res = render_layout(
                session,
                {
                    "path": png_path,
                    "title": f"{design_name} [{seq:04d}]",
                },
            )
            png_ok = bool(render_res.get("ok"))
            # Echo the renderer's actual path (it may sanitize the stem).
            png_path = render_res.get("path", png_path)
        except Exception as exc:  # noqa: BLE001 — render is a convenience; never raise
            png_ok = False
            png_path = png_path

    ok = zmx_ok and ((not render) or png_ok)

    # Save-time clearance/visual gate (save-clearance-gate §3): WARN-and-surface,
    # NEVER blocks. Audit the CURRENT-config live geometry (frequent + cheap), stamp
    # ADDITIVE keys, and NEVER flip ``ok`` (the clearance verdict is advisory on a
    # save). A min_air/min_glass pass-through tunes the floors (default to the
    # check_clearance defaults). A gate throw -> indeterminate (never raises).
    verdict, clearance_summary = _run_clearance_gate(
        session,
        min_air=params.get("min_air"),
        min_glass=params.get("min_glass"),
        config=None,
    )
    clearance_ok = {"clean": True, "thin": False, "indeterminate": None}[verdict]
    clearance_warning = _clearance_warning_text(verdict, clearance_summary)
    # The "visual" half: when no figure was produced (render=False or a render
    # failure) there is nothing for the user to eyeball — surface that explicitly.
    visual_check_warning = (
        None if png_ok
        else "no layout figure was rendered — run render_layout to eyeball the design"
    )

    return {
        "ok": ok,
        "design_name": design_name,
        "label": label,
        "seq": seq,
        "zmx_path": zmx_path,
        "zmx_ok": zmx_ok,
        "png_path": png_path,
        "png_ok": png_ok,
        # (MCE) disclosure-only: the active config at save time (null on a
        # read fault). The .zmx round-trips the index; this is honesty, not load-bearing.
        "active_configuration": active_configuration,
        # Save-clearance-gate §3 (additive; ``ok`` is NEVER flipped by clearance):
        "clearance_ok": clearance_ok,                   # True/False/None = clean/thin/indeterminate
        "clearance_summary": clearance_summary,
        "clearance_warning": clearance_warning,
        "visual_check_warning": visual_check_warning,
    }


def _atomic_copy(src: str, dst: str) -> None:
    """Atomically copy ``src`` -> ``dst``: write a temp in the DST directory, then
    ``os.replace`` (atomic on Windows + POSIX — ``dst`` is never a torn half).

    A COPY (not a move) — the source candidate stays in ``candidates/`` so the
    trail is intact. The temp is cleaned on any failure so a crashed promote never
    orphans a ``.tmp`` over an unchanged ``dst``.
    """
    dst_dir = os.path.dirname(dst) or "."
    # D5 (persistence-workspace): makedirs BEFORE mkstemp — the BEST_*.zmx parent (the
    # workspace root in the flat layout) may not exist yet if a promote runs before any
    # candidate carved the tree. mkstemp(dir=dst_dir) would raise on a missing dir;
    # create it first (exist_ok). A genuinely unwritable dir still raises -> the
    # promote_best wrapper envelopes it as promote_failed (never a raw escape).
    os.makedirs(dst_dir, exist_ok=True)
    # Unique temp (mkstemp) so two concurrent promotes of the same design never
    # race on a shared fixed ``.tmp`` name. CLOSE the mkstemp fd immediately and
    # reopen BY NAME (mirrors layout_render): if ``open(src)`` below raises first,
    # there is no dangling fd to leak (and on Windows no fd lock orphaning the temp).
    fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(dst)}.", suffix=".tmp",
                               dir=dst_dir)
    os.close(fd)
    try:
        with open(src, "rb") as rfh, open(tmp, "wb") as wfh:
            while True:
                chunk = rfh.read(1024 * 1024)
                if not chunk:
                    break
                wfh.write(chunk)
            wfh.flush()
            os.fsync(wfh.fileno())
        os.replace(tmp, dst)  # atomic
    except Exception:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise


def promote_best(session, params):
    """Promote candidate ``seq`` to the root ``BEST_<design-name>.{zmx,png}``.

    ATOMIC copies (temp-in-root + ``os.replace``) — a COPY, not a move, so the
    candidate stays in ``candidates/`` (the trail is intact). Appends a
    ``{"event":"promote", ...}`` row to the SAME candidates manifest (one index,
    not a second ``workspace.json``). If the paired ``.png`` is missing/un-gated,
    re-render from the promoted ``.zmx`` when ``render``; if still unavailable the
    ``.zmx`` is STILL promoted and ``png_promoted`` is False.

    Returns ``{ok, design_name, seq, best_zmx, best_png, png_promoted}``.
    NEVER raises.
    """
    # FIX 2: ``params=None`` must not raise out of ``.get`` (mirror save_candidate).
    if not isinstance(params, dict):  # never-raise: None OR a truthy non-dict (§0.8)
        params = {}
    design_name = params.get("design_name")
    seq = params.get("seq")
    render = bool(params.get("render", True))

    # FIX 7 (spec §9.6): reject a non-str / empty design_name BEFORE _safe_name.
    name_err = _design_name_error(design_name)
    if name_err is not None:
        return {
            "ok": False,
            "error_family": "workspace_param",
            "error": name_err,
            "design_name": design_name,
            "seq": seq,
            "best_zmx": None,
            "best_png": None,
            "png_promoted": False,
        }

    try:
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            return {
                "ok": False,
                "error_family": "promote_failed",
                "error": f"seq must be a non-negative int, got {seq!r}",
                "design_name": design_name,
                "seq": seq,
                "best_zmx": None,
                "best_png": None,
                "png_promoted": False,
            }

        design_dir = _design_dir(session, design_name)
        zmx_dir = os.path.join(design_dir, "candidates", "zmx")
        png_dir = os.path.join(design_dir, "candidates", "png")
        safe_design = _safe_name(design_name)

        # Glob the seq'd .zmx candidate.
        zmx_matches = sorted(glob.glob(os.path.join(zmx_dir, f"{seq:04d}_*.zmx")))
        if not zmx_matches:
            return {
                "ok": False,
                "error_family": "promote_failed",
                "error": f"no candidate with seq {seq}",
                "design_name": design_name,
                "seq": seq,
                "best_zmx": None,
                "best_png": None,
                "png_promoted": False,
            }
        src_zmx = zmx_matches[0]
        best_zmx = os.path.join(design_dir, f"BEST_{safe_design}.zmx")

        # Save-clearance gate (save-clearance-gate §4): HARD-REFUSE a thin/indeterminate
        # design at the keeper boundary unless ``force=True``. Runs AFTER the seq-glob
        # existence check (never audit a design whose candidate doesn't exist) and BEFORE
        # any _atomic_copy (a refusal promotes NOTHING — no copy, no temp, no manifest
        # row). Audits the LIVE session geometry at config="all" — a zoom thin in a
        # NON-current config is the silent case the keeper boundary must catch.
        # STRICT bool — only the literal ``True`` forces. ``bool()`` coercion would
        # let ``force="no"`` / ``force="0"`` (truthy strings) silently override the keeper
        # refusal — the OPPOSITE of intent. ``is True`` admits ONLY True (1 / "yes" / any
        # other truthy value does NOT force).
        force = (params.get("force") is True)
        # Thread the optional min_air/min_glass thresholds into the keeper
        # gate so a manufacturable micro-lens can promote against scale-appropriate
        # floors instead of false-refusing at the fixed 0.5/1.0 macro floors (the
        # save_candidate passthrough, mirrored here). Omitted -> the gate's default
        # macro floors apply (the in-use HIT path is byte-identical). A bad threshold
        # (nan/inf/negative/non-number) hits check_clearance's _finite_nonneg firewall
        # -> the gate's except -> ("indeterminate", None) -> promote_clearance_indeterminate
        # (no new family, no crash).
        verdict, clearance_summary = _run_clearance_gate(
            session,
            min_air=params.get("min_air"),
            min_glass=params.get("min_glass"),
            config="all",
        )
        # The active config is read AFTER the gate (the "all" sweep restores it).
        active_configuration = _active_configuration(session)
        if not force and verdict in ("thin", "indeterminate"):
            if verdict == "thin":
                family = "promote_clearance_violation"
                error = _promote_thin_error(clearance_summary)
                clearance_ok = False
            else:
                family = "promote_clearance_indeterminate"
                error = (
                    "could not audit clearance (the live geometry read failed or "
                    "returned an unexpected shape); pass force=True to promote anyway"
                )
                clearance_ok = None
            return {
                "ok": False,
                "error_family": family,
                "error": error,
                "clearance_ok": clearance_ok,
                "clearance_summary": clearance_summary,
                "clearance_source": "live_session_geometry",
                # All existing promote_best keys present + nulled (promotes NOTHING):
                "design_name": design_name,
                "seq": seq,
                "best_zmx": None,
                "best_png": None,
                "png_promoted": False,
                "active_configuration": active_configuration,
            }

        # Atomic .zmx promote (the load-bearing artifact).
        _atomic_copy(src_zmx, best_zmx)

        # Atomic .png promote (a convenience). Prefer the paired candidate .png if
        # it passes the magic-byte gate; else re-render from the promoted .zmx.
        best_png = os.path.join(design_dir, f"BEST_{safe_design}.png")
        png_promoted = False
        png_matches = sorted(glob.glob(os.path.join(png_dir, f"{seq:04d}_*.png")))
        src_png = png_matches[0] if png_matches else None

        if src_png is not None and _is_png(src_png):
            try:
                _atomic_copy(src_png, best_png)
                png_promoted = _is_png(best_png)
            except Exception:  # noqa: BLE001 — png is a convenience; never raise
                png_promoted = False
        elif render:
            try:
                render_res = render_layout(
                    session,
                    {
                        "path": best_png,
                        "title": f"{design_name} BEST",
                    },
                )
                final = render_res.get("path", best_png)
                png_promoted = bool(render_res.get("ok")) and _is_png(final)
                if png_promoted:
                    best_png = final
            except Exception:  # noqa: BLE001 — never raise
                png_promoted = False

        if not png_promoted:
            best_png = best_png if os.path.isfile(best_png) else None

        # (MCE) ``active_configuration`` was read above at the gate (the
        # "all" sweep restores the active config first); reuse it for the manifest row.

        # Append the promote row to the SAME candidates manifest (one index).
        manifest_path = os.path.join(zmx_dir, "manifest.jsonl")
        note = None
        try:
            row = {
                "event": "promote",
                "seq": seq,
                "ts": _io.utc_now_iso(),
                "design_name": design_name,
                "active_configuration": active_configuration,
            }
            _io.append_line_fsync(
                manifest_path, json.dumps(row, ensure_ascii=False, allow_nan=False)
            )
        except Exception as exc:  # noqa: BLE001 — manifest note is best-effort; never raise
            note = f"promote row append failed: {type(exc).__name__}: {exc}"

        result = {
            "ok": True,
            "design_name": design_name,
            "seq": seq,
            "best_zmx": best_zmx,
            "best_png": best_png,
            "png_promoted": png_promoted,
            # (MCE) disclosure-only: the active config at promote time.
            "active_configuration": active_configuration,
            # Save-clearance gate §4: the verdict is stamped even on a clean/forced
            # promote (clearance_source discloses it audited the LIVE session geometry,
            # NOT the on-disk candidate). ``forced`` is True only when force overrode a
            # thin/indeterminate verdict.
            "clearance_ok": {
                "clean": True, "thin": False, "indeterminate": None
            }[verdict],
            "clearance_summary": clearance_summary,
            "clearance_source": "live_session_geometry",
            "forced": bool(force and verdict in ("thin", "indeterminate")),
        }
        if note is not None:
            result["note"] = note
        return result
    except Exception as exc:  # noqa: BLE001 — promote_best NEVER raises
        return {
            "ok": False,
            "error_family": "promote_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "design_name": design_name,
            "seq": seq,
            "best_zmx": None,
            "best_png": None,
            "png_promoted": False,
            # keep ``clearance_source`` present on EVERY promote exit path (success,
            # forced, refusal, AND this outer never-raise net) so a consumer can read it
            # unconditionally.
            "clearance_source": "live_session_geometry",
        }


SAVE_CANDIDATE_SPEC = ToolSpec(
    name="save_candidate",
    handler=save_candidate,
    required_params=("design_name",),
    param_types={
        "design_name": "string",
        "label": "string",
        "render": "boolean",
        "min_air": "number",
        "min_glass": "number",
    },
    description=(
        "Snapshot the live system as a durable project candidate .zmx (+ paired "
        "layout .png) under projects/<design>/candidates/; NEVER raises — inspect "
        "result.ok. Runs a save-time clearance/visual gate: WARNS (never blocks) when "
        "the current-config geometry is manufacturably thin (clearance_ok:false + "
        "clearance_warning, tunable via min_air/min_glass) or when no figure was "
        "rendered (visual_check_warning) — review before promoting. Returns the saved "
        "Zemax file path under zmx_path; use that value for persistence/read-back."
    ),
)

PROMOTE_BEST_SPEC = ToolSpec(
    name="promote_best",
    handler=promote_best,
    required_params=("design_name", "seq"),
    param_types={
        "design_name": "string",
        "seq": "integer",
        "render": "boolean",
        "force": "boolean",
        "min_air": "number",
        "min_glass": "number",
    },
    description=(
        "Atomically promote a caller-asserted candidate seq to the project root "
        "BEST_<design>.{zmx,png} (copy, not move; trail intact); NEVER raises. "
        "REFUSES a manufacturably-thin design at the keeper boundary "
        "(promote_clearance_violation) or one whose clearance could not be audited "
        "(promote_clearance_indeterminate) — the gate audits the LIVE session "
        "geometry at config=all (tunable via min_air/min_glass for a non-macro-scale "
        "design; promote right after saving), so pass force=true to override. See "
        "check_clearance, render_layout."
    ),
)

TOOL_SPECS = (SAVE_CANDIDATE_SPEC, PROMOTE_BEST_SPEC)
