"""build_operand_semantics.py — generate the committed operand_semantics.json.

Runs the operand-semantics §4 decision tree over ALL 438
``MeritOperandType`` codes and emits the deterministic ``sign_convention`` +
``sign_convention_source`` (+ a ``citation_handle`` for family / overlay rows)
artifact at ``src/optivibe_reference/data/operand_semantics.json``.

Provenance split (spec §1/§4):
- ``suffix`` — the last two letters of the code DETERMINE the one-sided / equality
  semantics: ``*GT`` -> ``boundary_ge``, ``*LT`` -> ``boundary_le``,
  ``*VA`` -> ``equality``. NO citation (the code itself is the evidence).
- ``family`` — an ``MN*`` whose oracle prose says "greater than" -> ``boundary_ge``;
  an ``MX*`` whose oracle prose says "less than" -> ``boundary_le``. Cited to the
  oracle page (``manual:p<page>``). An MN/MX WITHOUT the keyword falls through.
- ``manual`` — the small, cited minimize/maximize overlay (RMS-error figure-of-
  merit family). Each entry is gated behind explicit oracle support + a page cite.
- ``null`` source — a DESCRIBED operand the rule can't otherwise decide gets
  ``sign_convention="measurement"`` (no inherent direction; caller chooses); a
  genuinely UNDESCRIBED, non-suffix, non-MN/MX operand stays
  ``sign_convention=null`` (honest-unknown firewall, preserves OGSS).

Inputs (read-only):
- ``scripts/captures/operand_inventory_438.json`` — the full 438-code set.
- ``scripts/captures/operand_raw_descriptions.json`` — the GITIGNORED oracle
  (raw_text + page); read at build time, only tokens + page cites are emitted.
- ``src/optivibe_reference/data/operand_synonyms.json`` — the committed synonyms
  file; its ``rows`` KEYSET is the DESCRIBED/enriched set (the deleted
  ``operand_descriptions.json`` keyset moved here 1:1). Read via
  ``load_synonyms_rows`` (a ``{schema_version, rows}`` envelope — a bare
  ``set(json.load(...))`` would wrongly yield ``{'schema_version','rows'}``).

Deterministic + re-runnable: same inputs -> byte-identical output (normalized-LF,
indent=2, keys sorted by code).

Provenance (§11): every emitted value is a controlled-vocabulary TOKEN or a
page citation. The minimize/maximize overlay carries a ``# cite:`` paraphrase of
the operand's framing — NEVER long verbatim manual prose.
"""
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.dirname(_HERE)
_CAPTURES = os.path.join(_PKG_ROOT, "scripts", "captures")
_DATA_DIR = os.path.join(_PKG_ROOT, "src", "optivibe_reference", "data")

# Import the SINGLE canonical build constants from the package so the
# generator never re-declares a literal that could drift from the catalog gate
# (the audit LOW-5 dedupe + the shared boundary-phrasing helper for guard 4). The
# generator runs both standalone (``python scripts/build_operand_semantics.py``,
# PYTHONPATH=src) and under pytest (scripts/ on sys.path); add src/ defensively so
# the import resolves either way.
_SRC = os.path.join(_PKG_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
from optivibe_reference.catalog_build import (  # noqa: E402
    ALLOWED_SIGN_CONVENTIONS,
    ALLOWED_SIGN_SOURCES,
    SIGN_SOURCE_COMPATIBILITY,
    SUFFIX_COLLISIONS,
    _oracle_boundary_dir,
    load_synonyms_rows,
)

INVENTORY_PATH = os.path.join(_CAPTURES, "operand_inventory_438.json")
ORACLE_PATH = os.path.join(_CAPTURES, "operand_raw_descriptions.json")
# the verbatim-local build: the described/enriched set is the committed synonyms KEYSET (the deleted
# operand_descriptions.json keyset moved here 1:1). Read via load_synonyms_rows —
# the file is a {schema_version, rows} envelope, so ``set(json.load(...))`` would
# wrongly yield {'schema_version','rows'} (Amendment 1a RULING A).
SYNONYMS_PATH = os.path.join(_DATA_DIR, "operand_synonyms.json")
OUT_PATH = os.path.join(_DATA_DIR, "operand_semantics.json")

# ---------------------------------------------------------------------------
# The cited minimize/maximize overlay (spec §4 step 1b).
#
# CONSERVATIVE by mandate: a direction is assigned ONLY where the oracle raw_text
# unambiguously frames the operand as an RMS / figure-of-merit error quantity the
# manual minimizes. Any doubt -> leave it as ``measurement`` (do NOT fabricate a
# direction — the §4 wrong-direction firewall). ``maximize`` requires explicit
# oracle support (none qualified here — empty is fine, per spec).
#
# Each entry is one of the RMS spot-radius / RMS wavefront-error operands. The
# oracle frames every one as "RMS spot radius ..." / "RMS wavefront error ..." (a
# smaller-is-better error magnitude that the merit function drives toward zero).
# The ``# cite:`` comment paraphrases that framing + the oracle page (provenance:
# tokens + page cite, no verbatim prose).
# ---------------------------------------------------------------------------
MINIMIZE_OVERLAY = {
    # cite: manual p1188 — RMS spot radius wrt centroid (Gaussian-quadrature); an
    # RMS error magnitude, smaller is better.
    "RSCE": "minimize",
    # cite: manual p1188 — RMS spot radius wrt chief ray (Gaussian-quadrature).
    "RSCH": "minimize",
    # cite: manual p1188 — RMS spot radius wrt centroid (rectangular-grid).
    "RSRE": "minimize",
    # cite: manual p1188 — RMS spot radius wrt chief ray (rectangular-grid).
    "RSRH": "minimize",
    # cite: manual p1188 — RMS wavefront error wrt centroid (Gaussian-quadrature).
    "RWCE": "minimize",
    # cite: manual p1188 — RMS wavefront error wrt chief ray (Gaussian-quadrature).
    "RWCH": "minimize",
    # cite: manual p1189 — RMS wavefront error wrt centroid (rectangular-grid).
    "RWRE": "minimize",
    # cite: manual p1189 — RMS wavefront error wrt chief ray (rectangular-grid).
    "RWRH": "minimize",
}
# ``maximize`` overlay: intentionally empty (no operand has explicit larger-is-
# better oracle support that the rule can defensibly assign). Spec §4 anticipates
# this. Kept as a named slot so the structure is visible / extensible.
MAXIMIZE_OVERLAY = {}

# Suffix-collision carve-out (spec §4). These codes
# merely END in GT/LT without being boundary operands: LOGT = base-10 LOG transform
# (manual p1168), FCGT = tangential field-curvature aberration (the tangential pair
# of FCGS, p1160), NSLT = non-sequential LightningTrace ray-trace (p1179). An
# independent oracle sweep of all 91 suffix operands confirmed EXACTLY these 3 (NOT
# COLT, which is a genuine "less than the target" boundary). All 3 are DESCRIBED, so
# they resolve to ``measurement`` / source ``null`` (caller chooses direction) —
# NEVER a one-sided boundary the prose does not support (the wrong-direction
# firewall). Handled BEFORE the suffix branch in ``decide``.
#
# SINGLE canonical definition lives in ``catalog_build.SUFFIX_COLLISIONS`` (imported
# above); this is a module-local alias so ``decide`` and the determinism tests
# (which monkeypatch ``_SUFFIX_COLLISIONS``) keep their existing handle.
_SUFFIX_COLLISIONS = SUFFIX_COLLISIONS


def _load(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _norm(text):
    """Whitespace-normalized, lowercased oracle text for keyword scanning."""
    return re.sub(r"\s+", " ", text or "").lower()


def _oracle_says(oracle_entry, phrase):
    """True iff the oracle raw_text contains ``phrase`` (normalized substring)."""
    if oracle_entry is None:
        return False
    return phrase in _norm(oracle_entry.get("raw_text", ""))


def decide(code, oracle, described):
    """Run the §4 decision tree for one code.

    Returns ``(sign_convention, sign_convention_source, citation_handle)`` where
    ``citation_handle`` is ``None`` unless the rule cites the oracle page.

    Order is load-bearing (spec §4):
      0. suffix-collision carve-out (LOGT/FCGT/NSLT) -> measurement (described)
      1. suffix GT/LT/VA  (source=suffix, no cite)
      2. MN* + "greater than" / MX* + "less than"  (source=family, cite=page)
      3. minimize/maximize overlay  (source=manual, cite=page)
      4. DESCRIBED -> measurement  (source=null)
      5. else -> null  (honest unknown)
    """
    if code in _SUFFIX_COLLISIONS:
        # Ends in GT/LT but is NOT a boundary (log / aberration / trace). All 3 are
        # described -> measurement (caller chooses target+direction), source null.
        # Must precede the suffix branch so the mechanical 2-letter rule cannot
        # mislabel them a one-sided boundary (spec §4 carve-out).
        return "measurement", None, None

    suffix = code[-2:]
    if suffix == "GT":
        return "boundary_ge", "suffix", None
    if suffix == "LT":
        return "boundary_le", "suffix", None
    if suffix == "VA":
        return "equality", "suffix", None

    oracle_entry = oracle.get(code)
    if code[:2] == "MN" and _oracle_says(oracle_entry, "greater than"):
        page = oracle_entry["page"]
        return "boundary_ge", "family", "manual:p{}".format(page)
    if code[:2] == "MX" and _oracle_says(oracle_entry, "less than"):
        page = oracle_entry["page"]
        return "boundary_le", "family", "manual:p{}".format(page)

    if code in MINIMIZE_OVERLAY or code in MAXIMIZE_OVERLAY:
        value = MINIMIZE_OVERLAY.get(code) or MAXIMIZE_OVERLAY.get(code)
        # The overlay is cited; an overlay entry without an oracle page would be a
        # fabricated cite, so we require the oracle entry to exist (it always does
        # for the RMS family) and cite its page.
        assert oracle_entry is not None, (
            "{}: overlay entry has no oracle page to cite".format(code)
        )
        page = oracle_entry["page"]
        return value, "manual", "manual:p{}".format(page)

    if code in described:
        # No inherent direction the rule can decide — caller chooses target +
        # direction. The wrong-direction firewall (spec §4): never fabricate one.
        return "measurement", None, None

    # Undescribed + non-suffix + non-MN/MX + no overlay -> honest unknown (OGSS).
    return None, None, None


def build_semantics():
    """Build the semantics dict (code -> row) over all 438 codes."""
    inventory = _load(INVENTORY_PATH)
    oracle = _load(ORACLE_PATH)
    # a build ruling: the DESCRIBED set is the committed synonyms
    # KEYSET. load_synonyms_rows unwraps the {schema_version, rows} envelope + returns
    # the rows dict; its keys are the enriched code set (== the old descriptions
    # keyset 1:1), which drives ONLY the described->measurement decision in decide().
    described = set(load_synonyms_rows(SYNONYMS_PATH))

    codes = [m["code"] for m in inventory["members"]]
    total = inventory["total_members"]
    assert len(codes) == len(set(codes)) == total, (
        "code-set integrity: {} codes, {} unique, {} declared".format(
            len(codes), len(set(codes)), total
        )
    )

    out = {}
    for code in codes:
        sign, source, cite = decide(code, oracle, described)
        row = {
            "sign_convention": sign,
            "sign_convention_source": source,
        }
        if cite is not None:
            row["citation_handle"] = cite
        out[code] = row
    return out


# Directional signs (mirrors catalog_build._DIRECTIONAL_SIGNS) — a sign that
# REQUIRES a non-null source. ``measurement``/None are sourceless.
_DIRECTIONAL_SIGNS = ("boundary_ge", "boundary_le", "equality", "minimize", "maximize")


def _validate_semantics(semantics, oracle):
    """Fail-closed validator over the generator's OWN output (audit HIGH 1 + MED 4).

    The catalog build re-checks these (defense in depth), but the generator writes
    ``operand_semantics.json`` FIRST — so it must reject its own malformed output
    BEFORE ``write_semantics`` ever touches disk. Runs in the build/dev (oracle-
    present) environment where the oracle is a REQUIRED input, so the suffix↔oracle
    no-contradiction check (audit MED 4) is fail-closed here, not oracle-gated.

    Raises ``AssertionError`` on ANY violation:

    - ``sign_convention`` ∈ §4 enum (or None);
    - ``sign_convention_source`` ∈ {suffix, family, manual, None};
    - directional sign ⇒ source non-null; ``measurement``/None sign ⇒ source None;
    - §4 source⇔sign COMPATIBILITY table (suffix→{ge,le,equality};
      family→{ge,le}; manual→{minimize,maximize});
    - family/manual rows ⇒ a non-empty ``citation_handle``;
    - suffix↔oracle no-contradiction over non-collision codes (no genuine ``*GT``
      whose oracle prose documents a "less than the target" boundary, symmetric
      for ``*LT``); the ``_SUFFIX_COLLISIONS`` carve-outs are SKIPPED.
    """
    for code, row in semantics.items():
        sign = row.get("sign_convention")
        ssrc = row.get("sign_convention_source")
        cite = row.get("citation_handle")

        # --- enum closure ----------------------------------------------------
        assert sign is None or sign in ALLOWED_SIGN_CONVENTIONS, (
            f"{code}: sign_convention {sign!r} not in the §4 enum"
        )
        assert ssrc is None or ssrc in ALLOWED_SIGN_SOURCES, (
            f"{code}: sign_convention_source {ssrc!r} not in the §4 source set"
        )

        # --- directional ⇒ source; measurement/None ⇒ source None ------------
        if sign in _DIRECTIONAL_SIGNS:
            assert ssrc is not None, (
                f"{code}: directional sign {sign!r} requires a non-null source"
            )
        else:
            # sign is None or 'measurement' -> source MUST be None.
            assert ssrc is None, (
                f"{code}: sign {sign!r} must be sourceless (got source {ssrc!r})"
            )

        # --- source⇔sign COMPATIBILITY table ---------------------------------
        if ssrc is not None:
            allowed = SIGN_SOURCE_COMPATIBILITY[ssrc]
            assert sign in allowed, (
                f"{code}: sign {sign!r} incompatible with source {ssrc!r} "
                f"(allowed: {sorted(allowed)})"
            )

        # --- family/manual ⇒ a non-empty citation_handle ---------------------
        if ssrc in ("family", "manual"):
            assert cite, f"{code}: {ssrc} row requires a non-empty citation_handle"

        # --- suffix↔oracle no-contradiction (oracle REQUIRED here) -----------
        # Skip the carve-out codes (LOGT/FCGT/NSLT) — they end in GT/LT but are
        # not boundaries. For a genuine *GT, the oracle must not document the
        # opposite ('le') boundary-target direction (symmetric for *LT). Reuses
        # the SAME boundary-target-phrasing detector catalog_build guard 4 uses.
        if code in _SUFFIX_COLLISIONS:
            continue
        entry = oracle.get(code)
        if entry is None:
            continue
        direction = _oracle_boundary_dir(entry.get("raw_text"))
        if code[-2:] == "GT":
            assert direction != "le", (
                f"{code}: *GT but oracle documents a 'less than the target' "
                f"boundary (mis-captured oracle)"
            )
        if code[-2:] == "LT":
            assert direction != "ge", (
                f"{code}: *LT but oracle documents a 'greater than the target' "
                f"boundary (mis-captured oracle)"
            )


def write_semantics(out_path, semantics):
    """Write the semantics dict as normalized-LF, indent=2 JSON, keys sorted.

    Sorting by code makes the artifact diff-stable and the generator byte-
    deterministic (same inputs -> identical bytes).
    """
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    text = json.dumps(semantics, indent=2, ensure_ascii=True, sort_keys=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.write("\n")


def _distribution(semantics):
    """Return (sign-count, source-count) Counter-like dicts for the report."""
    sign = {}
    source = {}
    for row in semantics.values():
        s = row["sign_convention"]
        src = row["sign_convention_source"]
        sign[s] = sign.get(s, 0) + 1
        source[src] = source.get(src, 0) + 1
    return sign, source


def main():
    semantics = build_semantics()
    # Fail closed on the generator's OWN output BEFORE writing (audit HIGH 1 +
    # MED 4). The oracle is a REQUIRED input here (build/dev environment), so the
    # suffix↔oracle contradiction check is fail-closed, not oracle-gated.
    oracle = _load(ORACLE_PATH)
    _validate_semantics(semantics, oracle)
    write_semantics(OUT_PATH, semantics)
    sign, source = _distribution(semantics)
    print("wrote {} codes -> {}".format(len(semantics), OUT_PATH))
    print("sign_convention distribution:")
    for k in sorted(sign, key=lambda x: (x is None, x)):
        print("  {!r:>14}: {}".format(k, sign[k]))
    print("sign_convention_source distribution:")
    for k in sorted(source, key=lambda x: (x is None, x)):
        print("  {!r:>14}: {}".format(k, source[k]))


if __name__ == "__main__":
    main()
