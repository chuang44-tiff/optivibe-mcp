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
accepts the other accepts.

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

from .artifact_sink import _safe_name

#: Minimum width of the per-design candidate index. A MINIMUM, not a maximum:
#: ``f"{1000:03d}"`` is ``"1000"`` and every parser below accepts ``\d{3,}``.
CANDIDATE_INDEX_DIGITS = 3

#: Width the legacy workspace-global counter is formatted at (also a minimum).
LEGACY_INDEX_DIGITS = 4

_LEGACY_RE = re.compile(r"^\d{4,}_.*\.(zmx|png)$")
_LEADING_DIGITS_RE = re.compile(r"^(\d+)")
#: A LOOKAHEAD, so every START POSITION is counted: ``_001_002_`` has delimiters at
#: two positions even though the two runs share an underscore.
_DELIMITER_RE = re.compile(r"(?=_\d{3,}_)")


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
    except ValueError:  # pragma: no cover — see the reachability note below
        # THE REASON THIS IS EXEMPTED IS REACHABILITY, NOT ARITHMETIC. It used to
        # read "a digit run always parses", which is FALSE on this interpreter:
        # ``sys.get_int_max_str_digits()`` is 4300 and ``int("9" * 4301)`` raises
        # ValueError ("Exceeds the limit (4300 digits)"). A universal was doing the
        # work a measurement should do.
        #
        # MEASURED instead: both production callers -- ``workspace.py:3190`` and
        # ``:3238`` -- pass basenames taken from ``os.listdir`` / ``os.walk``, so
        # the string is always a real path COMPONENT, and a component of even 255
        # digits cannot be created on this filesystem (measured: FileNotFoundError
        # at 255, 256 and 300). No production path can supply 4301 digits.
        #
        # That is a checkable claim about the CALL SITES, so it goes stale loudly:
        # a caller that feeds this a manifest- or envelope-supplied string rather
        # than a directory entry invalidates it, and the branch then needs a test
        # rather than a pragma.
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
    return int(match.group(1))


def delimiter_count(basename: str) -> int:
    """How many ``_\\d{3,}_`` delimiters the STEM carries. PURELY SYNTACTIC.

    Every START POSITION counts, so ``_001_002_`` is 2. No legality judgement, no
    validator, no length rule — this makes no claim about which readings are legal
    designs.

    Used ONLY on the orphan path of ``promote_best``: a v2
    orphan with NO manifest row whose stem carries more than one delimiter is refused
    as ambiguous, whichever design asks. ``alpha_001_001_x.zmx`` could be
    ``(alpha, 1, "001_x")`` or ``(alpha_001, 1, "x")`` and with no row nothing can
    tell them apart, so nothing is published for either claimant.

    This OVER-REFUSES relative to "exactly one LEGAL reading" — some second readings
    would not be legal designs at all. That is DELIBERATE, and it is NOT YET TICKETED:
    an earlier draft of this docstring cited a ``TICKET-`` id for a DesignTicket file
    that does not exist, which ``::
    test_no_dangling_ticket_pointer_in_src`` caught. A pointer to a ticket nobody filed
    is worse than none — it reads as "handled elsewhere". The sound enumerator would
    need the producer's validator rules and overlapping-delimiter handling; filing that
    is open work. The remedy for a refused orphan is to RE-SAVE, which restores the
    manifest row, and the row is the identity source.
    """
    if not isinstance(basename, str):
        return 0
    stem = os.path.splitext(basename)[0]
    return len(_DELIMITER_RE.findall(stem))


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
