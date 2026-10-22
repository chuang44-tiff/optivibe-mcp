"""tools/_glass_common.py — shared glass-tool helpers (one source of truth).

Extracted so the three glass tools (``lookup_glass`` / ``find_glasses`` /
``find_glass_pair``) normalize a user-supplied catalog string IDENTICALLY. The
prior split (``lookup_glass`` stripped path components + appended ``.AGF`` while
``find_glasses`` / ``find_glass_pair`` only ``.strip()``-ed) meant a path-like
catalog like ``C:\\tmp\\FAKECAT`` resolved differently across the three tools.
``normalize_catalog`` is now the single normalizer all three ride.
"""
import os

from ..errors import ToolParamError


def normalize_catalog(catalog, param_name="catalog"):
    """Normalize an optional catalog param to the stored bare-basename ``.AGF`` form.

    Returns ``None`` for an absent / blank catalog, else the catalog as the STORED
    form would carry it: path components stripped (so ``C:\\tmp\\FAKECAT`` ->
    ``FAKECAT.AGF``, not the whole path), and a ``.agf`` suffix appended when the
    user typed a bare basename. The downstream SQL match is case-insensitive, so
    casing of the suffix is irrelevant; this normalizer's job is to collapse the
    path + suffix variation to the basename a stored catalog value carries.

    Raises ``ToolParamError`` for a non-string catalog (the never-raise contract
    classifies that as ``tool_param`` at dispatch).
    """
    if catalog is None:
        return None
    if not isinstance(catalog, str):
        raise ToolParamError(
            f"'{param_name}' must be a string; got {type(catalog).__name__}"
        )
    cat = catalog.strip()
    if not cat:
        return None
    # Strip path components first. A path-like catalog ("C:\\tmp\\FAKECAT" or
    # "/x/y/FAKECAT") would otherwise carry the full path and never match the
    # stored BASENAME. Normalize Windows backslashes to '/' so os.path.basename
    # works on POSIX hosts too (os.path.basename keeps "\\" on POSIX).
    cat = cat.replace("\\", "/").rstrip("/")
    cat = os.path.basename(cat)
    stem = cat[:-4] if cat.lower().endswith(".agf") else cat
    if not stem.strip("."):
        # A non-blank value with no basename ("/", "\\", "//"), or whose stem is empty
        # or only dots (".", "..", ".AGF"), names no catalog. Reading it as "no catalog
        # given" would silently drop the restriction.
        raise ToolParamError(
            f"'{param_name}' item {catalog.strip()!r} names no catalog (only a path, dots or "
            "separator); give a catalog name such as 'SCHOTT', or leave it out"
        )
    # Append .AGF when the user passed a bare basename (the stored form carries it).
    if not cat.lower().endswith(".agf"):
        cat = cat + ".AGF"
    return cat


class _Catalog(str):
    """A normalized catalogue string that remembers the item as the caller typed it."""

    typed = ""


def normalize_catalogs(value, param_name="catalog"):
    """Normalize a comma-separated catalog whitelist to a list of stored ``.AGF`` forms.

    Returns ``None`` for an absent value or one with no item left (blank string, only
    commas). Each item goes through ``normalize_catalog``; duplicates collapse
    case-insensitively (first spelling kept) and blank items are ignored. Raises
    ``ToolParamError`` for a non-string (a JSON list, a number) with the comma form.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolParamError(
            f"'{param_name}' must be a comma-separated string; got "
            f'{type(value).__name__}. Example: {param_name}="SCHOTT,OHARA"'
        )
    seen = set()
    items = []
    for part in value.split(","):
        cat = normalize_catalog(part, param_name)
        if cat is None or cat.lower() in seen:
            continue
        seen.add(cat.lower())
        item = _Catalog(cat)
        item.typed = part.strip()
        items.append(item)
    return items or None


def check_catalogs_exist(db_conn, catalogs):
    """Refuse (``ToolParamError``) a whitelist item that matches no catalogue in the database."""
    if not catalogs:
        return
    known = [r[0] for r in db_conn.execute("SELECT DISTINCT catalog FROM glass")]
    known_lower = {k.lower() for k in known}
    for cat in catalogs:
        if cat.lower() not in known_lower:
            names = sorted(
                (k[:-4] if k.lower().endswith(".agf") else k) for k in known
            )
            raise ToolParamError(
                f"catalog '{getattr(cat, 'typed', cat)}' matches no catalog in the database; the catalogs "
                f"are: {', '.join(names)}. Example: catalog=\"SCHOTT,OHARA\""
            )


# Physical-glass identity tolerances: two rows sharing a NAME are the SAME
# physical glass (a cross-catalog redistributed copy) only when ALL THREE optical
# properties also match within these tolerances. Any property beyond tolerance =>
# a DIFFERENT glass that happens to share the name (which must still pair / stay
# distinct). These are the audit-specified bounds.
_IDENT_ND_TOL = 1e-3
_IDENT_VD_TOL = 0.5
_IDENT_PG_F_TOL = 1e-3


def _same_physical_glass(row_a, row_b):
    """True when two eligible rows are the SAME physical glass (name + props in tol).

    Rows are ``(catalog, name, vd, pg_f, nd)``. Same physical glass = same NAME AND
    ``|Δnd| <= 1e-3`` AND ``|Δvd| <= 0.5`` AND ``|Δpg_f| <= 1e-3`` — i.e. a
    cross-catalog redistributed copy. A None property (shouldn't occur for
    pair-eligible rows, but be defensive) is treated as a non-match on that axis.
    """
    if row_a[1] != row_b[1]:
        return False
    vd_a, pgf_a, nd_a = row_a[2], row_a[3], row_a[4]
    vd_b, pgf_b, nd_b = row_b[2], row_b[3], row_b[4]
    if None in (vd_a, pgf_a, nd_a, vd_b, pgf_b, nd_b):
        return False
    return (
        abs(nd_a - nd_b) <= _IDENT_ND_TOL
        and abs(vd_a - vd_b) <= _IDENT_VD_TOL
        and abs(pgf_a - pgf_b) <= _IDENT_PG_F_TOL
    )
