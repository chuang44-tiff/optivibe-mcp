"""The published operand semantics ship STRIPPED of manual page pointers and minimize signs."""
import json
import os

import optivibe_reference

MINIMIZE_CODES = ("RSCE", "RSCH", "RSRE", "RSRH", "RWCE", "RWCH", "RWRE", "RWRH")
SEMANTICS = os.path.join(
    os.path.dirname(optivibe_reference.__file__), "data", "operand_semantics.json")


def _rows():
    with open(SEMANTICS, encoding="utf-8") as fh:
        rows = json.load(fh)
    assert len(rows) > len(MINIMIZE_CODES), len(rows)
    return rows


def test_release_semantics_nulled_carries_no_citation_handle():
    carried = sorted(c for c, e in _rows().items() if e.get("citation_handle") is not None)
    assert carried == [], carried[:5]


def test_release_semantics_nulled_minimize_codes_read_as_measurement():
    rows = _rows()
    for code in MINIMIZE_CODES:
        assert rows[code]["sign_convention"] == "measurement", (code, rows[code])
        assert rows[code]["sign_convention_source"] is None, (code, rows[code])


def test_release_semantics_nulled_no_minimize_or_manual_row_anywhere():
    rows = _rows()
    assert [c for c, e in rows.items() if e.get("sign_convention") == "minimize"] == []
    assert [c for c, e in rows.items() if e.get("sign_convention_source") == "manual"] == []
