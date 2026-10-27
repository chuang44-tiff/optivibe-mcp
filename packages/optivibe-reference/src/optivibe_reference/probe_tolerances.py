"""probe_tolerances.py — live probe of the ToleranceOperandType surface.

Probe-first capture for the optivibe-reference TOLERANCE-operand catalog. Mirrors
``probe_operands.py`` (the MeritOperandType probe) but targets the Tolerance Data
Editor (``sys.TDE``) and the ``ZOSAPI.Editors.TDE.ToleranceOperandType`` enum.
Boots a headless OpticStudio session (NetHelper-first, no hardcoded install path)
and records, as the deterministic spine for the tolerance reference layer:

  P1  tolerance_inventory_<N>.json  — EVERY live ToleranceOperandType member name
      via System.Enum.GetNames (the catalog's primary-key set). N is the probed
      count, NOT a literal; the build asserts against this file, never a magic 62.

  P2  live-label existence          — for a representative member battery, CALL the
      TDE row's plausible label-bearing members (TypeName / RowTypeName / Comment /
      Description / ...) discovered via dir(), to learn whether the engine exposes
      ANY human label (decides whether description_source must be manual/authored
      for ALL rows, as the merit probe found).

  P3  tolerance_cell_layout_probe   — a curated family battery covering surface tilt
      / surface decenter / element tilt / element decenter / surface irregularity /
      Zernike irregularity / index / Abbe / thickness / radius / curvature / TIR
      roll / control+compensator. AddOperand -> ChangeType -> sweep GetCellAt(col),
      capturing each cell's Header + DataType + Value/IntegerValue/DoubleValue so the
      catalog can author param-slot semantics (Surf vs Surf1/Surf2 range vs Par#
      param-number vs Min/Max perturbation bounds). Crash-class operands (TNPA/TNMA,
      ticket-noted) are authored + cell-dumped ONLY (NO run) so the probe never
      crashes the headless engine; their behavior is recorded, never forced.

  P4  unit-attr absence (negative)  — confirm the TDE row / cell object exposes NO
      Unit/Units member, so units are necessarily provenance/manual-tagged, never
      live (same as the merit probe found).

Package code since the PyPI packaging change: ``scripts/probe_tolerances.py`` is a
thin wrapper over ``main`` here, and ``optivibe-build-data --probe`` calls it too.
Importing this module needs NO pythonnet (``clr`` loads inside ``main``). It does NOT
import optivibe_harness (zero cross-package coupling) and modifies NO saved lens (it
authors TDE rows on an EMPTY in-memory system and never saves). The session is reaped
in a try/finally. Captures land in ``_paths.captures_dir()``.
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

# Curated cell-layout battery: at least one operand per family so the schema +
# param-slot discipline are proven against a real capture. Covers the families the
# Spec calls out: surface tilt/decenter, element tilt/decenter, surface +
# Zernike irregularity, index/abbe/thickness/radius/curvature, TIR roll, and a
# control/compensator operand. Crash-class NSC operands are authored + dumped but
# NEVER run (P3 is author + cell-dump only; there is no Tolerancing run in this probe).
CELL_BATTERY = [
    # surface tilt (sag-tilt single surface).
    ("TSTX", "surface tilt about X (single surface)"),
    ("TSTY", "surface tilt about Y (single surface)"),
    # surface decenter (sag-decenter single surface).
    ("TSDX", "surface decenter X (single surface)"),
    ("TSDY", "surface decenter Y (single surface)"),
    # element tilt (surface-RANGE tilt: Surf1..Surf2).
    ("TETX", "element tilt about X (surface range)"),
    ("TETY", "element tilt about Y (surface range)"),
    # element decenter (surface-RANGE decenter: Surf1..Surf2).
    ("TEDX", "element decenter X (surface range)"),
    ("TEDY", "element decenter Y (surface range)"),
    ("TEDR", "element decenter radial (surface range)"),
    # surface irregularity.
    ("TIRR", "surface irregularity (single surface)"),
    # Zernike / extended irregularity.
    ("TEXI", "extended Zernike irregularity"),
    ("TEZI", "Zernike irregularity"),
    # material.
    ("TIND", "index perturbation (single surface)"),
    ("TABB", "Abbe-number perturbation (single surface)"),
    # geometry scalars.
    ("TTHI", "thickness perturbation (single surface)"),
    ("TRAD", "radius perturbation (single surface)"),
    ("TCUR", "curvature perturbation (single surface)"),
    # TIR roll (surface-range + a roll-surface param).
    ("TRLX", "TIR roll about X (surface range + RollSurf)"),
    ("TRLY", "TIR roll about Y (surface range + RollSurf)"),
    ("TRLR", "TIR roll radial (surface range + RollSurf)"),
    # parameter-targeting (param-number slot).
    ("TPAR", "parameter-value perturbation (Surf + Par#)"),
    # control + compensators.
    ("COMP", "compensator (Surf + Code)"),
    ("CPAR", "compensator on a parameter (Surf + Par#)"),
    ("TWAV", "test-wavelength control"),
    ("TOFF", "offset / inert control"),
    ("SAVE", "save-file control"),
    # crash-class (ticket-noted): authored + cell-dumped only, NEVER run.
    ("TNPA", "NSC parameter (crash-class; author+dump only, no run)"),
    ("TNMA", "NSC max (crash-class; author+dump only, no run)"),
]

# Members we explicitly inspect for a live human label in P2 (one per major family).
LABEL_PROBE = ["TRAD", "TTHI", "TETX", "TEDX", "TIRR", "TIND", "COMP", "TWAV"]


def dotnet_methods(obj):
    """Public .NET method names via reflection (get_/set_ accessors stripped)."""
    try:
        t = obj.GetType()
        names = sorted({str(m.Name) for m in t.GetMethods()})
        return [n for n in names if not n.startswith("get_") and not n.startswith("set_")]
    except Exception as exc:  # noqa: BLE001
        return f"<unavailable: {type(exc).__name__}: {exc}>"


def dotnet_properties(obj):
    """Public .NET property names via reflection."""
    try:
        t = obj.GetType()
        return sorted({str(p.Name) for p in t.GetProperties()})
    except Exception as exc:  # noqa: BLE001
        return f"<unavailable: {type(exc).__name__}: {exc}>"


# ---------------------------------------------------------------------------- #
# TDE row authoring (discover-don't-guess the add method + the enum namespace).
# ---------------------------------------------------------------------------- #
def resolve_add_method(tde):
    """Discover the TDE row-add method (it may differ from the MFE's AddOperand)."""
    for cand in ("AddOperand", "AddTolerance", "InsertNewOperandAt", "AddRow"):
        if hasattr(tde, cand):
            return cand
    return None


def tde_count(tde):
    if hasattr(tde, "NumberOfTolerances"):
        return int(tde.NumberOfTolerances)
    return int(tde.NumberOfOperands)


def add_op(tde, add_method):
    if add_method == "InsertNewOperandAt":
        return tde.InsertNewOperandAt(tde_count(tde) + 1)
    return getattr(tde, add_method)()


def remove_last(tde):
    try:
        if hasattr(tde, "RemoveOperandAt"):
            tde.RemoveOperandAt(tde_count(tde))
    except Exception:  # noqa: BLE001
        pass


def clear_tde(tde):
    try:
        if hasattr(tde, "DeleteAllRows"):
            tde.DeleteAllRows()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------- #
# P2 — live-label existence.
# ---------------------------------------------------------------------------- #
def probe_live_labels(tde, add_method, enum_type, member_set):
    """Does the engine expose any human label for a tolerance operand?"""
    out = {"operands": {}, "op_member_names": None}
    first = True
    clear_tde(tde)
    for name in LABEL_PROBE:
        if name not in member_set:
            out["operands"][name] = {"present_in_enum": False}
            continue
        rec = {}
        try:
            op = add_op(tde, add_method)
            op.ChangeType(getattr(enum_type, name))
            if first:
                out["op_member_names"] = public_members(op)
                out["op_dotnet_properties"] = dotnet_properties(op)
                out["op_dotnet_methods"] = dotnet_methods(op)
                first = False
            for member in ("TypeName", "RowTypeName", "Comment", "Description",
                           "TypeDescription", "Name"):
                rec[member] = capture(lambda m=member, o=op: getattr(o, m))
        except Exception as exc:  # noqa: BLE001
            rec["error"] = f"{type(exc).__name__}: {exc}"[:200]
        out["operands"][name] = rec
        remove_last(tde)
    return out


# ---------------------------------------------------------------------------- #
# P3 — cell-layout family battery.
# ---------------------------------------------------------------------------- #
def probe_cell_layout(tde, add_method, enum_type, member_set):
    """Family battery: ChangeType then sweep GetCellAt; discover Header + DataType +
    value fields per column so param-slot semantics (Surf vs Surf1/Surf2 range vs
    Par# param-number vs Min/Max bounds) can be authored downstream."""
    out = {"covered": [], "operands": {}, "cell_member_names": None}
    discovered_cell_api = False
    clear_tde(tde)
    for name, note in CELL_BATTERY:
        rec = {"note": note, "present_in_enum": name in member_set}
        if name not in member_set:
            out["operands"][name] = rec
            continue
        try:
            op = add_op(tde, add_method)
            rec["change_type_ok"] = capture(lambda: bool(op.ChangeType(getattr(enum_type, name))))
            rec["type_name"] = capture(lambda: str(op.TypeName))
            # Operand-level property reads: Param1/2/3, Comment, Comparator, Min/Max
            # (discover-don't-guess; record whatever is present).
            op_props = {}
            for prop in ("Param1", "Param2", "Param3", "Comment", "Comparator",
                         "Min", "Max", "MinValue", "MaxValue", "Nominal",
                         "Surface", "Surface1", "Surface2"):
                if hasattr(op, prop):
                    op_props[prop] = capture(lambda p=prop, o=op: str(getattr(o, p)))
            rec["op_property_reads"] = op_props
            # Per-column cell dump: Header is the load-bearing param-slot label.
            cells = {}
            headers = []
            for col in range(0, 13):
                cinfo = {}
                try:
                    cell = op.GetCellAt(col)
                except Exception as exc:  # noqa: BLE001
                    cells[col] = f"<GetCellAt err: {type(exc).__name__}: {exc}>"[:120]
                    headers.append(None)
                    continue
                if not discovered_cell_api:
                    out["cell_member_names"] = public_members(cell)
                    out["cell_dotnet_properties"] = dotnet_properties(cell)
                    discovered_cell_api = True
                hdr = capture(lambda c=cell: str(c.Header))
                headers.append(hdr if isinstance(hdr, str) else None)
                for field in ("Header", "DataType", "Value", "IntegerValue",
                              "DoubleValue", "Col"):
                    cinfo[field] = capture(lambda f=field, c=cell: getattr(c, f))
                cells[col] = cinfo
            rec["cells"] = cells
            rec["headers"] = headers  # compact param-slot summary, col-by-col
            out["covered"].append(name)
        except Exception as exc:  # noqa: BLE001
            rec["error"] = f"{type(exc).__name__}: {exc}"[:200]
        out["operands"][name] = rec
        remove_last(tde)
    return out


# ---------------------------------------------------------------------------- #
# P4 — unit-attr absence (negative).
# ---------------------------------------------------------------------------- #
def probe_unit_absence(tde, add_method, enum_type, member_set):
    """Confirm the TDE operand row / cell objects expose NO Unit/Units member, so
    units are necessarily manual/authored-tagged, never live."""
    out = {}
    name = next((n for n, _ in CELL_BATTERY if n in member_set), None)
    if name is None:
        return {"error": "no battery operand present in enum"}
    clear_tde(tde)
    op = add_op(tde, add_method)
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
    remove_last(tde)
    return out


def main():
    """Run the tolerance-operand probe; return 0 on a complete capture, 1 otherwise.

    Raises ``PythonnetMissing`` (an ``ImportError``) before any engine work when
    pythonnet is absent.
    """
    clr = load_clr()
    report = {
        "timestamp_utc": datetime.datetime.utcnow().isoformat() + "Z",
        "probe": "tolerances",
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
        tde = sys_.TDE
        report["tde_type"] = type(tde).__name__
        report["tde_methods"] = dotnet_methods(tde)
        report["tde_properties"] = dotnet_properties(tde)

        # Resolve the enum + add-method (discover-don't-guess).
        enum_type = ZOSAPI.Editors.TDE.ToleranceOperandType
        report["enum_qualified_name"] = "ZOSAPI.Editors.TDE.ToleranceOperandType"
        add_method = resolve_add_method(tde)
        report["add_method"] = add_method
        if add_method is None:
            raise RuntimeError("No TDE row-add method found (AddOperand/InsertNewOperandAt).")

        # --- P1: full live inventory (the primary-key set) ---
        members = enum_members(enum_type)
        member_set = set(members)
        n = len(members)
        print(f"[probe] ToleranceOperandType members: {n}")
        inventory = {
            "timestamp_utc": report["timestamp_utc"],
            "optic_studio_version": version,
            "enum_qualified_name": "ZOSAPI.Editors.TDE.ToleranceOperandType",
            "total_members": n,
            "members": [{"code": c} for c in members],
        }
        report["inventory_count"] = n
        report["inventory_file"] = write_json(f"tolerance_inventory_{n}.json", inventory)
        # Write the capture NOW (ok False = incomplete) so its inventory_count matches the
        # inventory just written even if P2-P4 or the teardown raises; the final write
        # below overwrites it.
        write_json("probe_tolerances_capture.json", report)

        # --- P2: live-label existence ---
        print("[probe] P2 — live-label existence (TypeName/RowTypeName/Comment/...)")
        report["live_labels"] = probe_live_labels(tde, add_method, enum_type, member_set)

        # --- P3: cell-layout family battery ---
        print("[probe] P3 — cell-layout family battery (GetCellAt sweep)")
        report["cell_layout"] = probe_cell_layout(tde, add_method, enum_type, member_set)

        # --- P4: unit-attr absence (negative) ---
        print("[probe] P4 — unit-attr absence (negative)")
        report["unit_absence"] = probe_unit_absence(tde, add_method, enum_type, member_set)

        clear_tde(tde)
        report["ok"] = True
    finally:
        if app is not None:
            app.CloseApplication()  # reap — leave no orphan engine (FRU teardown)
            report["closed"] = True
            print("[probe] CloseApplication() called — session reaped.")

    report_path = write_json("probe_tolerances_capture.json", report)
    print(f"\n=== probe summary (full report at {report_path}) ===")
    summary = {k: report.get(k) for k in (
        "ok", "optic_studio_version", "inventory_count", "inventory_file",
        "add_method", "closed")}
    print(json.dumps(summary, indent=2))
    return 0 if report["ok"] else 1
