"""build_vendor_data degrades a missing optional [manual] extra to a SKIP, never a FAILED step."""
import importlib.util
import os

import pytest

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "packages", "optivibe-reference", "scripts", "build_vendor_data.py")
CAPTURES = ("operand_inventory_438.json", "probe_operands_capture.json",
            "tolerance_inventory_62.json", "probe_tolerances_capture.json")


@pytest.fixture
def bvd(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("build_vendor_data_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name in CAPTURES:
        (tmp_path / name).write_text("{}", encoding="utf-8")
    monkeypatch.setattr(mod, "_CAPTURES", str(tmp_path))
    calls = []
    monkeypatch.setattr(mod.build_glass_catalog, "main", lambda: calls.append("glass"))
    monkeypatch.setattr(mod.build_manual_corpus, "main", lambda argv: calls.append("manual"))
    monkeypatch.setattr(mod.catalog_build, "main", lambda: calls.append("operand") or 0)
    monkeypatch.setattr(mod.tolerance_build, "main", lambda: calls.append("tolerance") or 0)
    mod.calls = calls
    return mod


def _missing(name):
    def raise_missing(argv):
        raise ModuleNotFoundError("No module named %r" % name, name=name)
    return raise_missing


def test_vendor_data_healthy_control_runs_every_step(bvd, capsys):
    assert (bvd.main(), bvd.calls) == (0, ["glass", "manual", "operand", "tolerance"])
    assert "SKIPPED" not in capsys.readouterr().out


def test_vendor_data_missing_fitz_skips_with_the_extra_hint(bvd, monkeypatch, capsys):
    monkeypatch.setattr(bvd.build_manual_corpus, "main", _missing("fitz"))
    rc, out = bvd.main(), capsys.readouterr().out
    assert (rc, bvd.calls) == (0, ["glass", "operand", "tolerance"])
    assert "[manual-build] SKIPPED - PyMuPDF (fitz)" in out and "[manual]" in out
    assert "FAILED" not in out


def test_vendor_data_other_missing_module_stays_failed(bvd, monkeypatch, capsys):
    monkeypatch.setattr(bvd.build_manual_corpus, "main", _missing("optivibe_reference.gone"))
    rc, out = bvd.main(), capsys.readouterr().out
    assert (rc, bvd.calls) == (1, ["glass", "operand", "tolerance"])
    assert "[manual-build] FAILED - ModuleNotFoundError" in out and "SKIPPED - PyMuPDF" not in out
