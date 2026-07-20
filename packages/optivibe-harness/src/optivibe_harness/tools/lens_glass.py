"""tools/lens_glass.py — glass substitution + catalog listing (API-only).

Two dispatchable tools over ``system.SystemData.MaterialCatalogs``:

- ``substitute_glass``   — write a surface's material, VALIDATED first against
  ``GetMaterialsInCatalog`` (the typed API accepts a bad glass name
  SILENTLY, so the harness validates). Membership is case-insensitive and the
  CANONICAL catalog spelling is what gets written + read back.
- ``list_glass_catalog`` — list glass names from the in-use catalogs (or a named
  one). API-only — NO ``.AGF`` parse (do not vendor the
  on-disk catalogs).

Live ZOS-API integration: exercised by the live test; unit-tested here against
a fake MaterialCatalogs double reproducing SCHOTT membership.
"""
from ..errors import CatalogLoadError, ToolParamError
from ..server import ToolSpec
from . import _lens_common as _c


def _catalogs_in_use(catalogs):
    """Return the in-use catalog name list (default ['SCHOTT', ''])."""
    return [c for c in catalogs.GetCatalogsInUse() if c]


def _available_catalogs(catalogs):
    """Return the available-catalog name list, the trailing "" filtered (F2)."""
    return [c for c in catalogs.GetAvailableCatalogs() if c]


def _find_owning_catalogs(catalogs, glass):
    """Find EVERY available catalog whose materials contain ``glass`` (F3/F10).

    Scans ALL ``GetAvailableCatalogs()`` (the trailing "" skipped, F2) via
    ``GetMaterialsInCatalog`` — which works even on a NOT-in-use catalog (F3) —
    matching ``glass`` CASE-INSENSITIVELY (F5). Plastics span MANY catalogs (F10),
    so this collects EVERY owner (not just the first) and preserves the
    ``GetAvailableCatalogs()`` order so the disclosure is DETERMINISTIC.

    Returns ``(owners, scan_failures)``:
    - ``owners`` = ``[(canonical_glass, owning_catalog), ...]`` (canonical = the
      catalog's own spelling), empty on a genuine miss.
    - ``scan_failures`` = the list of catalog names whose ``GetMaterialsInCatalog``
      THREW (in ``GetAvailableCatalogs()`` order). This is the M1 disclosure: a
      thrown OWNING catalog must NOT silently vanish into a generic "unknown glass"
      message — the caller discloses the partial scan so the agent is never told
      "this glass exists nowhere" when in fact a catalog could not be scanned.

    BEST-EFFORT: a ``GetMaterialsInCatalog`` that THROWS for one catalog is recorded
    in ``scan_failures`` and skipped (per-catalog try/except) — one squirrelly
    catalog never aborts the whole scan.
    """
    target = glass.casefold()
    owners = []
    scan_failures = []
    for cat in _available_catalogs(catalogs):
        try:
            members = catalogs.GetMaterialsInCatalog(cat)
        except Exception:  # noqa: BLE001 — best-effort: record + skip a throwing catalog
            scan_failures.append(cat)
            continue
        for name in members:
            if name.casefold() == target:
                owners.append((name, cat))
                break
    return owners, scan_failures


def _load_catalog_proven(catalogs, canonical):
    """Add + ``IsCatalogInUse``-prove a catalog (the F4 firewall). ONE loader (L30).

    The SHARED add+prove core extracted from ``load_catalog`` so the handler AND the
    two auto-load entry points (``substitute_glass``'s auto-load arm + ``apply_lens_spec``'s
    pre-scan) use ONE add+prove implementation, zero divergence.

    Idempotent: an already-in-use catalog SKIPS ``AddCatalog``. A VALIDATED name whose
    ``AddCatalog`` silently no-ops (``IsCatalogInUse`` stays False, gotcha #129) raises
    ``CatalogLoadError`` (``error_family="catalog_load"``) — it NEVER trusts the lying
    bool. ``canonical`` MUST already be a real catalog name (the caller resolved it via
    ``_find_owning_catalogs`` / the union); this core does NOT re-validate membership.

    Returns ``{"canonical_name","in_use","added","already_in_use",
    "catalogs_in_use_before","catalogs_in_use_after"}``.
    """
    already = bool(catalogs.IsCatalogInUse(canonical))
    before = _catalogs_in_use(catalogs)
    if not already:
        catalogs.AddCatalog(canonical)  # IGNORE the bool — F4 lies both ways
    in_use = bool(catalogs.IsCatalogInUse(canonical))
    if not in_use:
        raise CatalogLoadError(
            f"catalog {canonical!r} did not take effect (AddCatalog returned but "
            "IsCatalogInUse is still False — the bool return lies, F4)",
            field="catalog",
            intended=canonical,
            actual=False,
        )
    return {
        "canonical_name": canonical,
        "in_use": in_use,
        # A REAL transition — never the lying bool: a (not-already) name that now
        # reads back in-use.
        "added": (not already) and in_use,
        "already_in_use": already,
        "catalogs_in_use_before": before,
        "catalogs_in_use_after": _catalogs_in_use(catalogs),
    }


def _resolve_canonical(catalogs, glass):
    """Find the canonical catalog spelling of ``glass`` across in-use catalogs.

    Builds the membership set from ``GetMaterialsInCatalog(cat)`` for each in-use
    catalog and matches ``glass`` CASE-INSENSITIVELY. Returns
    ``(canonical_name, catalog)`` on a hit, or ``(None, None)`` on a miss. The
    canonical spelling (the catalog's own casing) is what gets written so the
    read-back compares cleanly.
    """
    target = glass.casefold()
    for cat in _catalogs_in_use(catalogs):
        for name in catalogs.GetMaterialsInCatalog(cat):
            if name.casefold() == target:
                return name, cat
    return None, None


def substitute_glass(session, params):
    """Validate ``glass`` against the catalog, then write + read-back.

    Validation happens BEFORE any write (the API silently accepts a bad
    glass, so an unknown name is rejected here with ``ToolParamError`` and the
    Material property is never touched). On a case-insensitive hit, the canonical
    catalog spelling is written and read back (string canonical-compare).
    """
    system = session.system
    lde = system.LDE
    n = int(lde.NumberOfSurfaces)
    surface = _c._require_int_index(params, "surface")
    _c._require_geometry_index(surface, n)

    glass = params.get("glass")

    # B3: the clear-to-air arm. A caller passing ""/"air"/"AIR"
    # (the reverse of a glass substitution — cementing reversible in place) routes
    # to the SHARED ``_clear_material_to_air`` (S-2) which writes canonical "" and
    # read-back proves it (NEVER the "AIR" literal the engine stores verbatim, the
    # #10-class trap). Runs AFTER the surface bounds check (OBJECT / out-of-range
    # already refused above) and BEFORE the non-empty-string rejection below.
    if isinstance(glass, str) and glass.strip().upper() in ("", "AIR"):
        from ._lens_common import _clear_material_to_air  # S-2 shared locus (L30)
        _clear_material_to_air(lde, surface)  # writes "", exact ==""; RAISES on fail
        return {"ok": True, "surface": surface, "material": "", "cleared_to_air": True}

    if not isinstance(glass, str) or glass == "":
        raise ToolParamError(
            f"glass must be a non-empty string, got {glass!r}"
        )

    # auto_load (default ON): a glass in a not-in-use available catalog is
    # auto-loaded (read-back proven) + the side-effect DISCLOSED, then authored in
    # ONE call. A REAL bool only — reject "true"/0/1 — evaluated BEFORE any scan, so
    # a malformed flag is a zero-mutation tool_param refusal.
    auto_load = params.get("auto_load", True)
    if not isinstance(auto_load, bool):
        raise ToolParamError(
            f"auto_load must be a bool (true/false), got {type(auto_load).__name__} "
            f"{auto_load!r}"
        )

    catalogs = system.SystemData.MaterialCatalogs
    canonical, catalog = _resolve_canonical(catalogs, glass)
    auto_loaded = None  # the catalog we auto-loaded (set ONLY on the auto-load arm)
    also_available_in = []
    ambiguous = False
    if canonical is None:
        # MISS: the glass is not in any IN-USE catalog. Before raising the generic
        # "unknown" error, scan ALL AVAILABLE catalogs (F3/F10) — the glass may live
        # in an available-but-not-in-use catalog. With auto_load ON we LOAD the owner
        # (read-back proven) and author in one call; with auto_load OFF we refuse with
        # the named owner. The happy in-use HIT path above is UNCHANGED (no scan on
        # success).
        owners, scan_failures = _find_owning_catalogs(catalogs, glass)
        if owners:
            first_canonical, first_catalog = owners[0]  # GetAvailableCatalogs() order
            also_available_in = [cat for (_g, cat) in owners[1:]]
            ambiguous = len(owners) > 1
            if auto_load:
                # AXIS 4/5: the SHARED read-back-proven loader. On a lying-bool /
                # degraded engine it raises CatalogLoadError(family="catalog_load") ->
                # the dispatch envelope; the material write below NEVER runs on an
                # unproven catalog.
                _load_catalog_proven(catalogs, first_catalog)
                # Re-resolve in the now-in-use set. Defensive: a loaded-but-unresolved
                # glass (owner-scan vs in-use-scan divergence) refuses LOUD, never
                # writes an unresolved material.
                canonical, catalog = _resolve_canonical(catalogs, glass)
                if canonical is None:
                    raise ToolParamError(
                        f"auto-loaded catalog {first_catalog!r} for glass {glass!r} "
                        f"but it still did not resolve in the in-use set "
                        f"({_catalogs_in_use(catalogs)}); refusing rather than writing "
                        "an unresolved material."
                    )
                auto_loaded = first_catalog  # disclosure flag (else None)
                # fall through to the shared write+read-back with disclosure attached.
            else:
                # OPT-OUT: the EXACT current refuse-with-named-owner (no behaviour
                # change) + the load_catalog(name=...) polish. Zero mutation.
                msg = (
                    f"unknown glass {glass!r}; not in any in-use catalog "
                    f"({_catalogs_in_use(catalogs)}). It IS in the not-in-use catalog "
                    f"{first_catalog!r} (available) — run "
                    f"load_catalog(name={first_catalog!r}), then retry (or call "
                    "substitute_glass with auto_load=true)."
                )
                if also_available_in:
                    others = ", ".join(repr(c) for c in also_available_in)
                    msg += f" Also in: {others}."
                raise ToolParamError(msg)
        elif scan_failures:
            # M1: no owner FOUND, but some available catalog(s) could not be scanned —
            # the glass MAY live in one of them. DISCLOSE the incomplete scan rather
            # than asserting "unknown" (which would mislead the agent into "this glass
            # exists nowhere" and erase the feature's primary diagnostic).
            raise ToolParamError(
                f"glass {glass!r} not found in any in-use catalog "
                f"({_catalogs_in_use(catalogs)}); could not scan {len(scan_failures)} "
                f"available catalog(s) ({scan_failures}) so it MAY exist in a "
                "not-in-use catalog — use list_catalogs / load_catalog."
            )
        elif canonical is None:
            # Genuine miss: no owner, no scan failure, and the auto-load arm did NOT
            # resolve it (the auto-load arm sets canonical when it loaded an owner).
            raise ToolParamError(
                f"unknown glass {glass!r}; not found in any in-use catalog "
                f"({_catalogs_in_use(catalogs)}). Use list_glass_catalog to discover "
                "valid names."
            )

    # Validated: write the canonical spelling, then re-fetch + read back.
    row = lde.GetSurfaceAt(surface)
    row.Material = canonical
    row = lde.GetSurfaceAt(surface)
    actual = str(row.Material)
    _c._verify_or_raise("material", canonical, actual, surface=surface)

    result = {"surface": surface, "material": canonical, "catalog": catalog}
    if auto_loaded:  # the catalog we _load_catalog_proven'd (mandatory disclosure)
        result["auto_loaded_catalog"] = auto_loaded
        result["also_available_in"] = also_available_in  # [] for a single owner
        result["auto_load_ambiguous"] = ambiguous        # True iff >1 owner
    return result


def list_glass_catalog(session, params):
    """List glass names from the in-use catalogs, or a named one. API-only.

    Optional ``catalog``: if given it must be a KNOWN catalog — resolved
    case-insensitively (F5) against the UNION of in-use + available (F11:
    ``GetAvailableCatalogs()`` EXCLUDES in-use catalogs, so a named IN-USE catalog
    like SCHOTT is absent from it; validating against available ALONE would wrongly
    reject an in-use catalog). ``GetMaterialsInCatalog`` reads any known catalog
    (F3). Otherwise the in-use catalogs are listed. Names are the canonical spelling.
    """
    system = session.system
    catalogs = system.SystemData.MaterialCatalogs
    requested = params.get("catalog")

    if requested is not None:
        if not isinstance(requested, str):
            raise ToolParamError(
                f"catalog must be a string, got {type(requested).__name__}"
            )
        # F11: validate against the UNION (in-use + available), resolving to the
        # canonical spelling (F5) — an in-use catalog has LEFT GetAvailableCatalogs.
        universe = list(dict.fromkeys(
            _catalogs_in_use(catalogs) + _available_catalogs(catalogs)
        ))
        target = requested.casefold()
        canonical = next((c for c in universe if c.casefold() == target), None)
        if canonical is None:
            raise ToolParamError(
                f"unknown catalog {requested!r}; known catalogs: {universe}"
            )
        names = [canonical]
    else:
        names = _catalogs_in_use(catalogs)

    glasses = {}
    count = 0
    for cat in names:
        members = list(catalogs.GetMaterialsInCatalog(cat))
        glasses[cat] = members
        count += len(members)

    return {"catalogs": names, "glasses": glasses, "count": count}


def load_catalog(session, params):
    """Enable (load) a material catalog, validated + read-back proven (F4).

    The ``AddCatalog`` bool return LIES (F4: a bogus name returns ``True`` yet
    no-ops; an already-in-use catalog returns ``False`` though it IS in use), so
    the sequence is VALIDATE-then-prove:

    1. ``name`` must be a non-empty string.
    2. Resolve it case-insensitively (F5) against the UNION of in-use + available
       catalogs (F11: ``GetAvailableCatalogs()`` EXCLUDES the in-use catalogs, so
       the full catalog universe is ``GetCatalogsInUse() ∪ GetAvailableCatalogs()``;
       both filter the trailing "", F2) to the CANONICAL spelling. A name in
       NEITHER set is refused HERE — and ``AddCatalog`` is NEVER called on a name
       that was not in ``available`` (the F4 firewall: ``AddCatalog("BOGUS")``
       returns ``True`` but no-ops).
    3. If already in use, skip ``AddCatalog`` (the idempotent path). Else (in
       ``available``) ``AddCatalog(canonical)`` — IGNORING the bool return.
    4. Prove with ``IsCatalogInUse(canonical)`` read-back; else ``CatalogLoadError``.

    Idempotent: re-loading an in-use catalog (SCHOTT, or a MISC already loaded)
    returns ``added:false, already_in_use:true`` (not an error, F6/F11).
    """
    catalogs = session.system.SystemData.MaterialCatalogs

    name = params.get("name")
    if not isinstance(name, str) or name == "":
        raise ToolParamError(f"name must be a non-empty string, got {name!r}")

    # F11 (live): GetAvailableCatalogs() returns ONLY the NOT-in-use catalogs (it
    # EXCLUDES in-use ones), so the full catalog universe is the UNION of in-use +
    # available. Resolve against the union (de-dup, preserving order) — else an
    # already-in-use catalog (SCHOTT, or a re-loaded MISC) would be rejected as
    # "unknown" because it has LEFT the available set.
    in_use = _catalogs_in_use(catalogs)
    available = _available_catalogs(catalogs)
    universe = list(dict.fromkeys(in_use + available))  # ordered, de-duped
    target = name.casefold()
    canonical = next((c for c in universe if c.casefold() == target), None)
    if canonical is None:
        # Near-matches over the UNION (so an in-use catalog never mis-suggests a
        # not-in-use look-alike): a name whose casefold contains or is contained by
        # the requested token (a substring match, either direction), capped at 5.
        near = [
            c for c in universe
            if target in c.casefold() or c.casefold() in target
        ][:5]
        if near:
            hint = f"Did you mean: {near}?"
        else:
            head = universe[:5]
            hint = f"Known catalogs (first {len(head)} of {len(universe)}): {head}."
        raise ToolParamError(
            f"unknown catalog {name!r}; not a known catalog. {hint}"
        )

    # Delegate the add+prove to the SHARED read-back-proven loader (L30: one loader,
    # zero divergence). It raises CatalogLoadError on the F4 lying-bool no-op. The
    # handler keeps its own param-validation + union-resolution + near-match-hint
    # above; it adds only the ``requested`` echo to the proven loader's envelope.
    proven = _load_catalog_proven(catalogs, canonical)
    proven["requested"] = name
    return proven


def list_catalogs(session, params):
    """List the material catalogs: in-use vs not-in-use vs all (read-only).

    F11: ``GetAvailableCatalogs()`` returns ONLY the NOT-in-use catalogs (it
    EXCLUDES the in-use ones), so it IS the ``not_in_use`` set directly — and the
    full catalog universe ``all`` is the UNION ``in_use ∪ not_in_use``.
    ``not_in_use`` is the set you can ``load_catalog`` to make their glasses
    authorable. No mutation, no ``GetMaterialsInCatalog`` scan — the trailing ""
    is filtered everywhere (F2).
    """
    catalogs = session.system.SystemData.MaterialCatalogs
    in_use = _catalogs_in_use(catalogs)
    not_in_use = _available_catalogs(catalogs)  # F11: available == not-in-use
    all_catalogs = list(dict.fromkeys(in_use + not_in_use))  # ordered union
    return {
        "in_use": in_use,
        "not_in_use": not_in_use,
        "all": all_catalogs,
        "all_count": len(all_catalogs),
    }


SUBSTITUTE_GLASS_SPEC = ToolSpec(
    name="substitute_glass",
    handler=substitute_glass,
    required_params=("surface", "glass"),
    param_types={"surface": "number", "glass": "string", "auto_load": "boolean"},
    description=(
        "Assign a glass material to a surface. Takes surface + glass; validated "
        "case-insensitively against the in-use catalogs first, then written in the "
        "canonical catalog spelling and read-back verified. auto_load defaults ON: a "
        "glass in a not-in-use catalog (plastics/exotics) is auto-loaded + disclosed "
        "(auto_loaded_catalog / also_available_in / auto_load_ambiguous), then "
        "authored in one call. Pass 'AIR' or '' to clear the surface back to air "
        "(cementing reversible in place). Gotcha: the API accepts a bad/unknown "
        "glass SILENTLY — trust the read-back, not the clean call."
    ),
)

LIST_GLASS_CATALOG_SPEC = ToolSpec(
    name="list_glass_catalog",
    handler=list_glass_catalog,
    required_params=(),
    param_types={"catalog": "string"},
    description=(
        "List available glass names from the in-use material catalogs (or a named "
        "catalog) to choose a material from."
    ),
)

LOAD_CATALOG_SPEC = ToolSpec(
    name="load_catalog",
    handler=load_catalog,
    required_params=("name",),
    param_types={"name": "string"},
    description=(
        "Enable (load) a material catalog so its glasses become authorable. Takes "
        "name (e.g. 'MISC' for optical plastics PMMA/POLYSTYR/POLYCARB, 'OHARA', "
        "'HOYA'); validated against the known-catalog set (in-use + not-in-use), "
        "then loaded and read-back proven via IsCatalogInUse. Returns the in-use "
        "set before/after. "
        "Gotcha: a fresh system has only SCHOTT in use, so a plastic/exotic material "
        "needs its catalog loaded FIRST; the AddCatalog return value LIES — trust the "
        "read-back."
    ),
)

LIST_CATALOGS_SPEC = ToolSpec(
    name="list_catalogs",
    handler=list_catalogs,
    required_params=(),
    param_types={},
    description=(
        "List the material catalogs: in_use vs not_in_use (plus all = their union). "
        "not_in_use is the set you can load_catalog to make their glasses "
        "authorable. Pair with load_catalog after find_glasses/lookup_glass "
        "suggests a material whose catalog isn't loaded."
    ),
)

TOOL_SPECS = (
    SUBSTITUTE_GLASS_SPEC,
    LIST_GLASS_CATALOG_SPEC,
    LOAD_CATALOG_SPEC,
    LIST_CATALOGS_SPEC,
)
