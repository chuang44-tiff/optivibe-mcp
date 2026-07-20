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
    if not cat:
        return None
    # Append .AGF when the user passed a bare basename (the stored form carries it).
    if not cat.lower().endswith(".agf"):
        cat = cat + ".AGF"
    return cat
