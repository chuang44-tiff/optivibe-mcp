"""probe_operands.py — live probe of the MeritOperandType operand surface.

Probe-first capture for the optivibe-reference operand catalog. Boots a headless
OpticStudio session (NetHelper-first, no hardcoded install path) and records, as
the deterministic spine for both reference layers:

  P1  operand_inventory_<N>.json   — EVERY live MeritOperandType member name via
      System.Enum.GetNames (the catalog's primary-key set). N is the probed count,
      NOT a literal; the build asserts against this file, never a magic 438.
  P2  live-label existence         — actually CALL op.TypeName / op.RowTypeName /
      mfe.AvailableOperandTypes and dump their shape, to learn whether the engine
      exposes any human label at all (decides if 'live_label' is ever a valid
      description_source or whether manual/authored is load-bearing for all rows).
  P3  operand_cell_layout_probe    — a curated family battery (ray / transverse-
      aberration / encircled-energy / material / constraint / boundary / system):
      AddOperand -> ChangeType -> sweep GetCellAt(col), capturing each cell's
      discovered fields + the operand's RowTypeName. Un-probed operands get NO
      inferred layout downstream.
  P4  unit-attr absence (negative) — confirm an operand row / cell object exposes
      NO Unit/Units member, so units/sign are necessarily manual/authored-tagged,
      never live.

Package code since the PyPI packaging change: ``scripts/probe_operands.py`` is a thin
wrapper over ``main`` here, and ``optivibe-build-data --probe`` calls it too. Importing
this module needs NO pythonnet (``clr`` loads inside ``main``). It does not import
optivibe_harness (zero cross-package coupling) and modifies NO saved lens. The session
is reaped in a try/finally (FRU teardown). Captures land in ``_paths.captures_dir()``.

Env:
    ZOSAPI_NETHELPER  — optional explicit path to ZOSAPI_NetHelper.dll
    ZEMAX_DIR         — optional explicit OpticStudio install dir (holds ZOSAPI.dll)
"""
import datetime
import json

from ._probe_common import (  # noqa: F401  (re-exported: the script's historic names)
    capture,
    enum_members,
    find_nethelper,
    load_clr,
    load_zosapi,
    public_members,
    resolve_zemax_dir,
    write_json,
)

# Curated cell-layout battery: one operand per family so the schema + discipline
# are proven against a real capture without sweeping all members this round.
CELL_BATTERY = [
    ("EFFL", "system / focal-length (no surface arg)"),
    ("REAY", "real ray Y (ray family)"),
    ("TRAR", "transverse aberration radius"),
    ("DENC", "diffraction encircled energy"),
    ("CVGT", "glass/material constraint (curvature)"),
    ("MNCG", "min center-thickness glass (constraint)"),
    ("TTHI", "total thickness (boundary/sum)"),
    ("RSCE", "RMS spot radius (centroid)"),
]

# Members we explicitly inspect for a live human label in P2.
LABEL_PROBE = ["EFFL", "REAY", "DIST", "TRAR", "DENC", "MNCG"]


def probe_cell_layout(mfe, enum_type, member_set):
    """Family battery: ChangeType then sweep GetCellAt; discover cell fields."""
    out = {"covered": [], "operands": {}, "cell_member_names": None}
    discovered_cell_api = False
    for name, note in CELL_BATTERY:
        rec = {"note": note, "present_in_enum": name in member_set}
        if name not in member_set:
            out["operands"][name] = rec
            continue
        try:
            op = mfe.AddOperand()
            rec["change_type_ok"] = capture(lambda: bool(op.ChangeType(getattr(enum_type, name))))
            rec["type_name"] = capture(lambda: str(op.TypeName))
            rec["row_type_name"] = capture(lambda: str(op.RowTypeName))
            cells = {}
            for col in range(0, 16):
                cinfo = {}
                try:
                    cell = op.GetCellAt(col)
                except Exception as exc:  # noqa: BLE001
                    cells[col] = f"<GetCellAt err: {type(exc).__name__}: {exc}>"[:120]
                    continue
                if not discovered_cell_api:
                    out["cell_member_names"] = public_members(cell)
                    discovered_cell_api = True
                # Discover-don't-guess: capture whatever the cell exposes.
                for field in ("Col", "Header", "Value", "IntegerValue",
                              "DoubleValue", "DataType"):
                    cinfo[field] = capture(lambda f=field, c=cell: getattr(c, f))
                cells[col] = cinfo
            rec["cells"] = cells
            out["covered"].append(name)
        except Exception as exc:  # noqa: BLE001
            rec["error"] = f"{type(exc).__name__}: {exc}"[:200]
        out["operands"][name] = rec
    return out


def probe_live_labels(mfe, enum_type, member_set):
    """P2: does the engine expose any human label for an operand?"""
    out = {
        "mfe_has_AvailableOperandTypes": "AvailableOperandTypes" in (public_members(mfe) or []),
        "AvailableOperandTypes": capture(lambda: str(mfe.AvailableOperandTypes)),
        "operands": {},
        "op_member_names": None,
    }
    first = True
    for name in LABEL_PROBE:
        if name not in member_set:
            out["operands"][name] = {"present_in_enum": False}
            continue
        rec = {}
        try:
            op = mfe.AddOperand()
            op.ChangeType(getattr(enum_type, name))
            if first:
                out["op_member_names"] = public_members(op)
                first = False
            # Probe every plausible label-bearing member; record what each returns.
            for member in ("TypeName", "RowTypeName", "Comment", "Description",
                           "TypeDescription", "Name"):
                rec[member] = capture(lambda m=member, o=op: getattr(o, m))
        except Exception as exc:  # noqa: BLE001
            rec["error"] = f"{type(exc).__name__}: {exc}"[:200]
        out["operands"][name] = rec
    return out


def probe_unit_absence(mfe, enum_type, member_set):
    """P4 negative: confirm operand row / cell objects expose NO Unit/Units member."""
    out = {}
    name = next((n for n, _ in CELL_BATTERY if n in member_set), None)
    if name is None:
        return {"error": "no battery operand present in enum"}
    op = mfe.AddOperand()
    op.ChangeType(getattr(enum_type, name))
    op_members = public_members(op) or []
    out["probe_operand"] = name
    out["op_has_Unit"] = "Unit" in op_members
    out["op_has_Units"] = "Units" in op_members
    out["op_unit_like_members"] = [m for m in op_members if "unit" in m.lower()]
    try:
        cell = op.GetCellAt(0)
        cell_members = public_members(cell) or []
        out["cell_has_Unit"] = "Unit" in cell_members
        out["cell_has_Units"] = "Units" in cell_members
        out["cell_unit_like_members"] = [m for m in cell_members if "unit" in m.lower()]
    except Exception as exc:  # noqa: BLE001
        out["cell_probe_error"] = f"{type(exc).__name__}: {exc}"[:200]
    return out


def main():
    """Run the merit-operand probe; return 0 on a complete capture, 1 otherwise.

    Raises ``PythonnetMissing`` (an ``ImportError``) before any engine work when
    pythonnet is absent.
    """
    clr = load_clr()
    report = {
        "timestamp_utc": datetime.datetime.utcnow().isoformat() + "Z",
        "ok": False,
    }
    ZOSAPI = load_zosapi(clr)  # noqa: N806  (the .NET namespace's own name)

    app = None
    try:
        connection = ZOSAPI.ZOSAPI_Connection()
        app = connection.CreateNewApplication()
        if app is None:
            raise RuntimeError("CreateNewApplication() returned None — no engine acquired.")
        if not bool(app.IsValidLicenseForAPI):
            raise RuntimeError("License is NOT valid for the ZOS-API (entitlement gap).")

        version = capture(lambda: ".".join(str(v) for v in (
            app.ZOSMajorVersion, app.ZOSMinorVersion, app.ZOSSPVersion)))
        report["optic_studio_version"] = version
        print(f"[probe] OpticStudio version: {version}")

        sys_ = app.PrimarySystem
        mfe = sys_.MFE
        enum_type = ZOSAPI.Editors.MFE.MeritOperandType

        # --- P1: full live inventory (the primary-key set) ---
        members = enum_members(enum_type)
        member_set = set(members)
        n = len(members)
        print(f"[probe] MeritOperandType members: {n}")
        inventory = {
            "timestamp_utc": report["timestamp_utc"],
            "optic_studio_version": version,
            "enum_qualified_name": "ZOSAPI.Editors.MFE.MeritOperandType",
            "total_members": n,
            "members": [{"code": c} for c in members],
        }
        report["inventory_count"] = n
        report["inventory_file"] = write_json(f"operand_inventory_{n}.json", inventory)
        # Write the capture NOW (ok False = incomplete) so its inventory_count matches the
        # inventory just written even if P2-P4 or the teardown raises; the final write
        # below overwrites it.
        write_json("probe_operands_capture.json", report)

        # --- P2: live-label existence ---
        print("[probe] P2 — live-label existence (TypeName/RowTypeName/AvailableOperandTypes)")
        report["live_labels"] = probe_live_labels(mfe, enum_type, member_set)

        # --- P3: cell-layout family battery ---
        print("[probe] P3 — cell-layout family battery (GetCellAt sweep)")
        report["cell_layout"] = probe_cell_layout(mfe, enum_type, member_set)

        # --- P4: unit-attr absence (negative) ---
        print("[probe] P4 — unit-attr absence (negative)")
        report["unit_absence"] = probe_unit_absence(mfe, enum_type, member_set)

        report["ok"] = True
    finally:
        if app is not None:
            app.CloseApplication()  # reap — leave no orphan engine (FRU teardown)
            report["closed"] = True
            print("[probe] CloseApplication() called — session reaped.")

    report_path = write_json("probe_operands_capture.json", report)
    print(f"\n=== probe summary (full report at {report_path}) ===")
    summary = {k: report.get(k) for k in (
        "ok", "optic_studio_version", "inventory_count", "inventory_file", "closed")}
    print(json.dumps(summary, indent=2))
    return 0 if report["ok"] else 1
