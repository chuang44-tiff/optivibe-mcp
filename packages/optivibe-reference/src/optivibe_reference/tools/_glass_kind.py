"""tools/_glass_kind.py - what KIND of material a glass row is (data + two small functions).

The glass database holds more than lens glass: liquids, adhesives, plastics, a
vacuum row, birefringent crystals. ``find_glasses`` and ``find_glass_pair`` leave the
non-lens kinds out by default, say how many they left out, and let the caller clear
the list. This module is the single place that knows which row is which kind.

Resolution order for ``material_kind(catalog, name)``:

1. a ROW exception (two rows whose catalogue says the wrong thing);
2. the CATALOGUE table (upper-cased basename without ``.AGF``): 52 catalogues hold
   one kind and are trusted whole;
3. for the 4 MIXED catalogues (``GL``, ``MISC``, ``MISC1``, ``MISC-CRB``), the NAME
   table, then the name-prefix table (a name beginning ``NOA`` is an adhesive);
4. anything else is ``unclassified``: the search KEEPS it and counts it. The public
   database is user-built, so a catalogue this table has never seen is normal.

The table is hand-maintained; a unit test is its staleness alarm.
"""
from ..errors import ToolParamError

# The six kinds a caller may name in ``exclude``, in the order the messages list them.
# (``lens`` and ``unclassified`` are the other two kinds a row can read; never excludable.)
EXCLUDABLE_KINDS = ("liquid", "adhesive", "plastic", "gas", "birefringent", "other")

# The default ``exclude`` argument: every excludable kind.
DEFAULT_EXCLUDE = ",".join(EXCLUDABLE_KINDS)

_ROW_EXCEPTIONS = {
    # A cover glass (nd 1.518, vd 55.4) in a catalogue of solutions.
    ("SOLUTION", "COVER_G"): "lens",
    # Glycerol (nd 1.47227) in a catalogue of glasses; pg_f 0.384 is impossible for a glass.
    ("2006CHIGLSXH", "GLYC"): "liquid",
}


def _table(kind, names):
    return {n: kind for n in names.split()}


_CATALOG_KIND = {}
_CATALOG_KIND.update(_table("lens", (
    "2006CHIGLSXH AMTIR ARCHER AUER-LIGHTING CDGM CORNING FOCUSLIGHT GBJ HERAEUS "
    "HIKARI HOYA INFRARED IRPHOTONICS ISUZU KOPPGLASS LACROIX LIGHTPATH LZOS NHG "
    "NIHON_KESSHO_KOGAKU NIKON-HIKARI NIKON OHARA OHARARMD OPTIMAX PILKINGTON "
    "PRECIOSA REDWAVE_IR RPO RAD_HARD SALIENCE SCHOTT SCHOTT_IRG SUMITA UMICORE VITAL"
)))
_CATALOG_KIND.update(_table("plastic", (
    "ANGSTROMLINK APEL APOLLO_OPTICAL_SYSTEMS ARTON DOWSIL_MS LUXEXCEL "
    "MICRO_RESIST_TECHNOLOGY OSAKAGASCHEMICAL SABIC_THERMO_OPTICAL_POLYMERS TEIJIN "
    "TOPAS ZEON"
)))
_CATALOG_KIND.update(_table("liquid", "SOLUTION OPTOTUNE"))
_CATALOG_KIND.update(_table("adhesive", "DELO"))
_CATALOG_KIND.update(_table("birefringent", "BIREFRINGENT"))

# Catalogues that mix kinds: the NAME decides.
_MIXED_CATALOGS = frozenset(("GL", "MISC", "MISC1", "MISC-CRB"))

_MIXED_NAME_KIND = {}
_MIXED_NAME_KIND.update(_table("liquid", (
    "GLYCEROL GLYCEROLUV HHMIOIL UVOIL WATERUV WATER SEAWATER TYPEA"
)))
_MIXED_NAME_KIND.update(_table("gas", "VACUUM"))
_MIXED_NAME_KIND.update(_table("adhesive", "ADHESIVE"))
# The UV-cure optical adhesives in these catalogues all share one name prefix
# (with and without an underscore, with suffixes); the prefix decides.
_MIXED_PREFIX_KIND = (("NOA", "adhesive"),)
_MIXED_NAME_KIND.update(_table("plastic", (
    "ACRYLIC COC CR39 PMMA POLYCARB POLYSTYR SAN"
)))
_MIXED_NAME_KIND.update(_table("other", "N15 RETINA"))
# Strongly birefringent crystals (also in BIREFRINGENT.AGF as ordinary/extraordinary pairs);
# the MISC rows carry one index only. LAF3 and QUARTZ (birefringence under 0.01) stay lens.
_MIXED_NAME_KIND.update(_table("birefringent", "CDS KDP TEO2"))
_MIXED_NAME_KIND.update(_table("lens", (
    "B270 BASF5 BASF55 BOROFLOAT BOROFLOAT2 CAF2 CAF2CORNING F_SILICA "
    "HPFS_8650 HPFS_CORNING LAF3 N-BK7 PYREX QUARTZ SILICA TGG"
)))


def catalog_key(catalog):
    """Upper-cased catalogue basename without a trailing ``.AGF``."""
    key = str(catalog).strip().upper()
    if key.endswith(".AGF"):
        key = key[:-4]
    return key


def material_kind(catalog, name):
    """Return the kind of the glass ``name`` in ``catalog`` (see the module docstring).

    ``catalog`` is the stored catalogue string (``SCHOTT.AGF``, any case).
    """
    key = catalog_key(catalog)
    exception = _ROW_EXCEPTIONS.get((key, name))
    if exception is not None:
        return exception
    if key in _MIXED_CATALOGS:
        kind = _MIXED_NAME_KIND.get(name)
        if kind is None and isinstance(name, str):
            kind = next((k for prefix, k in _MIXED_PREFIX_KIND if name.startswith(prefix)),
                        "unclassified")
        return kind or "unclassified"
    return _CATALOG_KIND.get(key, "unclassified")


def parse_exclude(value):
    """Parse the ``exclude`` argument into a frozenset of excludable kinds.

    ``None`` (absent) means ``DEFAULT_EXCLUDE``; ``""`` means leave nothing out.
    Case-insensitive; blank items are ignored; duplicates collapse. Raises
    ``ToolParamError`` for a non-string or an item outside the six kinds.
    """
    if value is None:
        value = DEFAULT_EXCLUDE
    if not isinstance(value, str):
        raise ToolParamError(
            f"'exclude' must be a comma-separated string; got {type(value).__name__}. "
            'Example: exclude="plastic,liquid"'
        )
    kinds = set()
    for item in value.split(","):
        item = item.strip().lower()
        if not item:
            continue
        if item not in EXCLUDABLE_KINDS:
            raise ToolParamError(
                f"'exclude' item '{item}' is not a kind that can be left out; "
                f"the kinds are: {', '.join(EXCLUDABLE_KINDS)}"
            )
        kinds.add(item)
    return frozenset(kinds)
