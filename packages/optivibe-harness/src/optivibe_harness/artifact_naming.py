"""artifact_naming.py — THE ONE authority on artifact filenames.

Pure stdlib. NO engine, NO import from ``tools/``. Every name a saved design
artifact carries is composed here and parsed here, so a name the writer emits and
a name the reader accepts cannot drift apart.

Two schemes exist on disk:

* **v2** — ``<design>_<NNN>_<label>.<ext>``, ``ext in {zmx, png, scorecard.json}``.
  ``<NNN>`` is ``f"{index:03d}"`` and 3 is a MINIMUM width: 999 is followed by
  ``1000``, so every parser here accepts ``\\d{3,}``. The index is PER DESIGN.
* **legacy** — ``<NNNN>_<label>.<ext>``, a four-digit-MINIMUM workspace-global
  counter (the only scheme that existed before this cycle). Still written by the
  forensic trail, where there is no design identity to name.

THE ONE PREDICATE is :func:`candidate_index_of`. The counter uses it (max over the
names for which it answers an int) and the resolver uses it (a hit is a name for
which it answers N). There is deliberately no second acceptance rule, so a name one
accepts the other accepts. The ORPHAN rule (a v2 file with no manifest row) is
:func:`legal_readings`: every legal ``(design, index, label)`` reading of the stem, the
producer's own validator injected, so the orphan path and the save-time disclosure count
the same thing.

NO GLOB PATTERN IS EVER BUILT FROM A DESIGN NAME, here or anywhere in the package.
``_safe_name`` admits ``[`` and ``]`` (its illegal set is ``<>:"/\\|?*``), so
``a[bc]`` is a canonical design name and ``glob("a[bc]_001_*")`` would match
``ab_001_seed.zmx`` and NOT its own file. Matching is LITERAL (``str.startswith``)
throughout. Measured: 0 of 66 real design names carry a metacharacter, so the
sanitizer is NOT narrowed — the matching primitive was the defect, not the sanitizer.

Live ZOS-API integration: N/A (no backend; pure string/filesystem work).
"""
import os
import re

from .artifact_sink import _RESERVED_NAMES, _safe_name

#: Minimum width of the per-design candidate index. A MINIMUM, not a maximum:
#: ``f"{1000:03d}"`` is ``"1000"`` and every parser below accepts ``\d{3,}``.
CANDIDATE_INDEX_DIGITS = 3

#: Width the legacy workspace-global counter is formatted at (also a minimum).
LEGACY_INDEX_DIGITS = 4

_LEGACY_RE = re.compile(r"^\d{4,}_.*\.(zmx|png)$")
_LEADING_DIGITS_RE = re.compile(r"^(\d+)")
#: One capturing lookahead so every START POSITION is a reading: ``_001_002_`` yields two.
_READING_RE = re.compile(r"(?=_(\d{3,})_)")


def candidate_stem(design_name: str, index: int, label: str) -> str:
    """``f"{design_name}_{index:03d}_{_safe_name(label)}"``.

    ``design_name`` is used RAW. The caller has already passed
    ``workspace._design_name_error``, whose fixed-point clause guarantees
    ``_safe_name(d) == d``, so re-sanitising here would be a second resolution of a
    value resolved elsewhere.
    """
    return f"{design_name}_{int(index):0{CANDIDATE_INDEX_DIGITS}d}_{_safe_name(label)}"


def candidate_zmx_name(design_name: str, index: int, label: str) -> str:
    """The v2 ``.zmx`` basename."""
    return candidate_stem(design_name, index, label) + ".zmx"


def candidate_png_name(design_name: str, index: int, label: str) -> str:
    """The v2 ``.png`` basename — the SAME stem as the ``.zmx``."""
    return candidate_stem(design_name, index, label) + ".png"


def candidate_scorecard_name(design_name: str, index: int, label: str) -> str:
    """The v2 scorecard basename."""
    return candidate_stem(design_name, index, label) + ".scorecard.json"


def trail_name(index: int, label: str, ext: str = ".zmx") -> str:
    """The LEGACY ``<NNNN>_<label><ext>`` name, for artifacts with no design identity.

    The forensic trail (``optimize``'s per-pass snapshots, ``save_snapshot``'s
    checkpoints) has no design to name, so none is invented; it keeps this scheme.
    """
    return f"{int(index):0{LEGACY_INDEX_DIGITS}d}_{_safe_name(label)}{ext}"


def sibling(path: str, ext: str) -> str:
    """The same directory, the same stem, a new extension. NO re-sanitising.

    Sidecars (the paired ``.png``, the scorecard) are derived from the ACTUAL saved
    ``.zmx`` path rather than recomposed from ``(index, label)`` — a recomposition is
    a second chance to disagree with the file that is really on disk.
    """
    directory = os.path.dirname(path)
    stem = os.path.splitext(os.path.basename(path))[0]
    return os.path.join(directory, stem + ext) if directory else stem + ext


def best_zmx_name(design_name: str) -> str:
    """``BEST_<safe design>.zmx`` — the promoted keeper."""
    return f"BEST_{_safe_name(design_name)}.zmx"


def best_png_name(design_name: str) -> str:
    """``BEST_<safe design>.png`` — the promoted keeper's figure."""
    return f"BEST_{_safe_name(design_name)}.png"


def is_legacy_name(basename: str) -> bool:
    """True for ``<NNNN>_<anything>.(zmx|png)``, four digits being the MINIMUM.

    The legacy writer formats at four digits, so past 9999 it emits five — a
    fixed-width test would classify ``10000_seed.zmx`` as v2 and let it advance a
    per-design counter it has nothing to do with.
    """
    if not isinstance(basename, str):
        return False
    return _LEGACY_RE.match(basename) is not None


def legacy_index(basename: str):
    """The leading digit run of a legacy name as an int, else ``None``."""
    if not isinstance(basename, str):
        return None
    match = _LEADING_DIGITS_RE.match(basename)
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:  # see the reachability note below (exercised directly by a test)
        # THE REASON THIS IS EXEMPTED IS REACHABILITY, NOT ARITHMETIC. It used to
        # read "a digit run always parses", which is FALSE on this interpreter:
        # ``sys.get_int_max_str_digits()`` is 4300 and ``int("9" * 4301)`` raises
        # ValueError ("Exceeds the limit (4300 digits)"). A universal was doing the
        # work a measurement should do.
        #
        # MEASURED instead: both production callers -- ``_resolve_candidate``'s orphan
        # listing (``os.listdir``) and its trail-note ``os.walk`` -- pass basenames, so
        # the string is always a real path COMPONENT, and a component of even 255
        # digits cannot be created on this filesystem (measured: FileNotFoundError
        # at 255, 256 and 300). No production path can supply 4301 digits.
        #
        # That is a checkable claim about the CALL SITES, so it goes stale loudly:
        # a caller that feeds this a manifest- or envelope-supplied string rather
        # than a directory entry invalidates it. The branch is now exercised by a
        # direct test (0.1.13 batch E1, T8-b) instead of excluded by a pragma, and
        # ``candidate_index_of`` answers the same ``None`` (the parsers are symmetric).
        return None


def candidate_index_of(basename: str, design_name: str, ext: str = "zmx"):
    """THE ONE PREDICATE: the per-design index this basename carries, else ``None``.

    LITERAL matching (``str.startswith``), never glob/fnmatch: the basename must
    start with ``f"{design_name}_"`` and the REMAINDER must match
    ``^(\\d{3,})_.*\\.<ext>$``; the answer is that digit run as an int.

    The counter and the resolver both call this and nothing else, so
    ``D_0001_seed.zmx`` is index 1 for BOTH — a spelling the writer never emits is
    either accepted by both readers or by neither.
    """
    if not isinstance(basename, str) or not isinstance(design_name, str):
        return None
    prefix = design_name + "_"
    if not basename.startswith(prefix):
        return None
    remainder = basename[len(prefix):]
    match = re.match(r"^(\d{3,})_.*\." + re.escape(ext) + r"$", remainder)
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        # Symmetric with ``legacy_index``:
        # ``int`` refuses a run past ``sys.get_int_max_str_digits`` (4300). No production caller can supply
        # one -- every name is a directory entry -- but ``legal_readings`` parses the same runs and the
        # module must be total over strings regardless of caller, so the answer is ``None``, not a raise.
        return None


def legal_readings(basename, design_ok):
    """Every LEGAL ``(design, index, label)`` reading of a v2 stem, in position order.

    A v2 stem ``<design>_<NNN>_<label>`` can be split at EVERY overlapping ``_\\d{3,}_``
    position. A reading is legal iff ``design_ok(design)`` -- the PRODUCER's own validator,
    injected (``workspace._design_name_error is None``); this module must not re-state that
    rule -- AND the label is in the IMAGE of ``_safe_name`` (``candidate_stem`` emits
    ``_safe_name(label)``, so the image IS the set of labels this writer can produce). The
    image is NOT the set of fixed points: ``_safe_name`` is NOT idempotent. Its reserved-device
    check runs BEFORE truncation, and truncation plus the second ``rstrip`` can expose a bare
    reserved word nothing checks again -- ``_safe_name("CON" + " " * 117 + "xx") == "CON"``
    while ``_safe_name("CON") == "snapshot_CON"``. Measured (targeted + 300k random fuzz,
    0.1.13 batch E1 round 2): the non-fixed part of the image is exactly the bare reserved
    words, so the rule is ``_safe_name(label) == label or label.upper() in _RESERVED_NAMES``
    (the set imported from where ``_safe_name`` reads it, never re-typed). It excludes the
    empty label, which sanitises to ``snapshot``. A digit run ``int`` refuses
    (``sys.get_int_max_str_digits``) is not a reading. The orphan path of ``promote_best``
    publishes iff exactly ONE reading is legal AND that reading names the claimant's
    ``(design, number)`` -- a sole reading that belongs to another design refuses for this
    claimant; the save-time
    ``label_ambiguous_without_row`` disclosure is the count alone (the saving design's own
    reading is legal because its label is in the image -- a fixed-point rule broke exactly
    that, and let ``alpha`` publish ``alpha_001_y``'s produced ``alpha_001_y_001_CON.zmx``),
    so the two cannot drift.

    MEASURED, so nobody expects more than this rescues: ``alpha_001_note_002_x`` has TWO legal
    readings (``alpha_001_note`` is a legal design) and stays refused; what this admits over
    the old syntactic count is an alternate design that is not a fixed point (``alpha_001_x.``,
    a trailing dot) or over 120 chars, and an alternate label that is empty.
    """
    if not isinstance(basename, str):
        return []
    stem = os.path.splitext(basename)[0]
    out = []
    for match in _READING_RE.finditer(stem):
        design = stem[:match.start()]
        digits = match.group(1)
        label = stem[match.start() + len(digits) + 2:]
        try:
            index = int(digits)
        except ValueError:
            continue
        if design_ok(design) and (_safe_name(label) == label
                                  or label.upper() in _RESERVED_NAMES):
            out.append((design, index, label))
    return out


def has_numeric_leading_segment(design_name: str) -> bool:
    """True when the first underscore segment of the name is all digits.

    Such a name would re-enter the legacy namespace: ``0376`` would compose
    ``0376_001_seed.zmx``, which ``is_legacy_name`` reads as legacy index 376.
    Measured cost of refusing it: 0 of 66 real design names.
    """
    if not isinstance(design_name, str):
        return False
    first = design_name.split("_", 1)[0]
    return first.isdigit()


def next_index_in_dir(directory: str, match):
    """``1 + max(match(name))`` over the LISTING, 1 when empty/absent, ``None`` on OSError.

    ``match`` answers an int for a name it recognises and ``None`` otherwise.

    **The ``None`` return is the whole point.** A directory that EXISTS but cannot be
    LISTED yields ``None`` and the caller REFUSES rather than writing: a refusal
    cannot clobber anything, so "an existing figure is never destroyed" holds with no
    reservation, no descriptor and no cleanup semantics. Falling back to 1 here is
    exactly the mutant G7b's first function catches.
    """
    if not os.path.isdir(directory):
        return 1
    try:
        names = os.listdir(directory)
    except OSError:
        return None
    best = 0
    for name in names:
        value = match(name)
        if isinstance(value, int) and not isinstance(value, bool) and value > best:
            best = value
    return best + 1
