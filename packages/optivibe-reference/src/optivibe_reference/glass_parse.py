"""glass_parse.py — the .agf catalog parser (lifted verbatim from the probe).

Lifted VERBATIM from ``scripts/probe_glass.py``: ``iter_glass_records`` /
``parse_formula_field`` / ``parse_float`` / ``find_catalogs`` (case-insensitive
``*.agf``, EXCLUDE ``*.bgf`` / ``*.dat``) / ``resolve_glasscat``. Decode via
``_decode_agf`` (BOM sniff -> BOM-less UTF-8 attempt -> latin-1): ~11 of the 56
install catalogs ship UTF-16-LE; some are BOM-less UTF-8 (non-ASCII names like
``ORMOCLEAR®``); the rest are Windows-1252-ish — a hardcoded latin-1 decode both
yielded ZERO records for the UTF-16 set AND mojibake'd the BOM-less-UTF-8 names,
so we sniff the BOM, then try UTF-8, then fall back to latin-1 (lossless);
sci-notation formula field (``int(round(float(tok)))``, NEVER ``int(tok)``);
short / variable-length CD tolerated. See §3.

PROVENANCE: we read only ASCII numeric / name fields from the shipped ``.agf``;
we NEVER emit raw catalog prose, and never vendor/commit a raw ``.agf`` (the
build/test reads it and discards it — only OUR parsed/computed numbers + factual
glass names are committed).

Record grammar (opened by ``NM``):
    name=NM[1], formula=parse_formula_field(NM[2]), nd_stored=NM[4], vd_stored=NM[5];
    density=ED[3], dpgf=ED[4]; cd=CD[1:]; min_wave=LD[1], max_wave=LD[2].
"""
import glob
import os


def parse_formula_field(tok):
    """Parse NM field 3 robustly (handles '1.00000000E+00' sci-notation -> 1).

    Real ``.agf`` formula fields are INTEGERS (sometimes written in sci-notation,
    e.g. ``"1.00000000E+00"``). A non-integral token (e.g. ``"1.6"``) is NOT a
    valid formula id — silently rounding it (``int(round(1.6))==2``) would route a
    malformed line onto the WRONG dispersion formula and emit wrong-but-non-null
    numbers. So we accept a token ONLY when its float value is integral within a
    small epsilon; otherwise we return ``None`` (the row then computes to safe
    nulls, never a wrong formula). Sci-notation integers still round-trip
    (``"1.00000000E+00"`` -> 1, ``"2"`` -> 2, ``"2.0"`` -> 2).
    """
    try:
        val = float(tok)
    except (ValueError, TypeError):
        return None
    nearest = round(val)
    if abs(val - nearest) > 1e-9:
        # Non-integral token (e.g. "1.6"): NOT a valid formula id -> None.
        return None
    return int(nearest)


def parse_float(tok):
    try:
        return float(tok)
    except (ValueError, TypeError):
        return None


def _decode_agf(raw):
    """Decode raw .agf bytes: BOM sniff -> BOM-less UTF-8 attempt -> latin-1.

    ONE source of truth for ALL .agf decoding (build, probe, offline drift gate).
    Order:
      1. BOM sniff: ``\\xef\\xbb\\xbf`` -> utf-8-sig, ``\\xff\\xfe`` -> utf-16-le,
         ``\\xfe\\xff`` -> utf-16-be. ``utf-16`` / ``utf-8-sig`` strip their own
         BOM on decode; for the explicit ``utf-16-le`` / ``-be`` cases we strip
         the 2 BOM bytes ourselves so the first record line is clean.
      2. BOM-less UTF-8 attempt (``raw.decode("utf-8")``): some install catalogs
         (e.g. MICRO_RESIST_TECHNOLOGY.AGF) are valid UTF-8 with NO BOM, where a
         name's registered-sign ``®`` is the two bytes ``\\xc2\\xae``. A blind
         latin-1 fallthrough double-decodes those to the mojibake ``Â®`` and
         commits an UNREACHABLE name — so we TRY UTF-8 first.
      3. latin-1 fallback on ``UnicodeDecodeError``: the Windows-1252-ish single-
         byte catalogs that are NOT valid UTF-8 decode losslessly (latin-1 maps
         every byte 0x00-0xFF, so it never raises).

    ~11 of the 56 install catalogs ship UTF-16-LE; a hardcoded latin-1 decode
    silently yields ZERO records for them (the every-line ``\\x00`` interleave
    means no line ever ``startswith('NM ')``). Non-ASCII glass NAMES DO flow into
    the catalog, decoded to their TRUE codepoints — the decode is NOT latin-1-only.
    We read only ASCII numeric fields + factual glass names and NEVER emit raw
    catalog prose (no vendor prose committed).
    """
    if raw[:3] == b"\xef\xbb\xbf":
        return raw.decode("utf-8-sig")
    if raw[:2] == b"\xff\xfe":
        return raw[2:].decode("utf-16-le")
    if raw[:2] == b"\xfe\xff":
        return raw[2:].decode("utf-16-be")
    # BOM-less: prefer valid UTF-8 (so '(R)' = \xc2\xae round-trips to one
    # codepoint, not the latin-1 mojibake double-decode); fall back to latin-1
    # for the single-byte Windows-1252-ish catalogs that are not valid UTF-8.
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def iter_glass_records(path):
    """Yield parsed glass dicts from one .agf file. Decode via ``_decode_agf``
    (BOM sniff -> BOM-less UTF-8 -> latin-1; .agf is UTF-16 OR UTF-8 OR
    Windows-1252-ish). Non-ASCII glass names decode to their true codepoints; we
    read only ASCII numeric fields + factual names, never emit raw prose."""
    name = formula = nd = vd = None
    density = dpgf = None
    cd = None
    minw = maxw = None
    have = False
    formula_is_sci = False

    def emit():
        return {
            "name": name, "formula": formula, "nd_stored": nd, "vd_stored": vd,
            "density": density, "dpgf": dpgf, "cd": cd,
            "min_wave": minw, "max_wave": maxw, "catalog": os.path.basename(path),
            "formula_is_sci": formula_is_sci,
        }

    with open(path, "rb") as fh:
        text = _decode_agf(fh.read())
    for ln in text.splitlines():
        if ln.startswith("NM "):
            if have:
                yield emit()
            p = ln.split()
            name = p[1] if len(p) > 1 else None
            formula = parse_formula_field(p[2]) if len(p) > 2 else None
            # sci-notation trap: NM field 3 written as e.g. "1.00000000E+00"
            raw_formula_field = p[2] if len(p) > 2 else ""
            formula_is_sci = ("e" in raw_formula_field.lower())
            nd = parse_float(p[4]) if len(p) > 4 else None
            vd = parse_float(p[5]) if len(p) > 5 else None
            density = dpgf = None
            cd = None
            minw = maxw = None
            have = True
        elif ln.startswith("ED ") and have:
            # ED tce1 tce2 density dPgF ...  (dPgF = ED field 4 -> token[4])
            p = ln.split()
            density = parse_float(p[3]) if len(p) > 3 else None
            dpgf = parse_float(p[4]) if len(p) > 4 else None
        elif ln.startswith("CD ") and have:
            cd = [parse_float(x) for x in ln.split()[1:]]
        elif ln.startswith("LD ") and have:
            p = ln.split()
            minw = parse_float(p[1]) if len(p) > 1 else None
            maxw = parse_float(p[2]) if len(p) > 2 else None
    if have:
        yield emit()


def find_catalogs(glasscat_dir):
    """Case-insensitive *.agf only; EXCLUDE *.bgf binaries and *.dat."""
    hits = []
    for f in glob.glob(os.path.join(glasscat_dir, "*")):
        low = f.lower()
        if low.endswith(".agf"):
            hits.append(f)
    return sorted(hits)


def resolve_glasscat():
    """Resolve the install Glasscat dir (env override -> default Documents path)."""
    override = os.environ.get("ZEMAX_GLASSCAT")
    if override and os.path.isdir(override):
        return override
    cand = os.path.join(os.path.expanduser("~"), "Documents", "Zemax", "Glasscat")
    if os.path.isdir(cand):
        return cand
    raise FileNotFoundError("Glasscat dir not found; set ZEMAX_GLASSCAT.")
