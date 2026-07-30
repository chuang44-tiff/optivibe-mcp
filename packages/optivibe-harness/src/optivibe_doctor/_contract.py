"""The pinned release contract.

Every value here is a **release contract input**, not a derived value.  Tests duplicate
these literally and must never import this module: a test that imports its own oracle
moves with the thing it grades.

Stdlib only — this module imports nothing at all.
"""

SCHEMA = "optivibe.doctor/v1"

DISTRIBUTIONS = ("optivibe-harness", "optivibe-reference")

CRITICAL_DEPS = ("mcp", "psutil", "pythonnet")   # missing critical -> FAIL; other declared -> WARN
IMPORT_NAME = {"pythonnet": "clr"}               # dist name -> import name; the only divergence

MCP_MIN = (1, 28)               # pinned INDEPENDENTLY of the wheel's own Requires-Dist.
MCP_MAX_EXCLUSIVE = (2, 0)      # Never read this range from installed metadata (test D-12).

# plane -> (build module, path-constant name, opener attr, dispatcher conn property,
#           identity probe: (table, pinned row token) or None => use PRAGMA database_list)
PLANES = {
    "merit_operand": ("catalog_build", "CATALOG_JSON_PATH", "conn",
                      ("operand", "EFFL")),
    "tolerance_operand": ("tolerance_build", "TOLERANCE_CATALOG_JSON_PATH", "tolerance_conn",
                          ("operand", "TRAD")),
    "glass": ("glass_build", "GLASS_CATALOG_JSON", "glass_conn",
              ("glass", "N-BK7")),
    "manual": ("manual_build", "MANUAL_DB_PATH", "manual_conn",
               None),
}

TOOL_PLANE = {                     # pinned tool -> plane map
    "lookup_operand": "merit_operand",
    "lookup_glass": "glass",
    "find_glasses": "glass",
    "find_glass_pair": "glass",
    "search_reference": "manual",
}

# Sentinel: this param's VALUE is resolved from the plane's own data at probe time, never
# pinned.  See the pinned-literal rule below — a pinned data VALUE is the defect class that
# bit this sprint three times.
FROM_PLANE = object()

# The value a FROM_PLANE param takes when the plane is ABSENT, so the door is still CALLED.
#
# Measured against the real public tree (the glass catalog is gitignored there, so this is
# the ordinary state of the most common install in the world): the tool's plane gate fires
# BEFORE name resolution, so on an absent plane the argument's CONTENT cannot affect the
# outcome —
#
#   lookup_glass    {"name": "ZZZ-NONSENSE"}                  -> outer=True inner=False
#                                                                glass_catalog_unavailable
#   lookup_glass    {}                                        -> outer=False inner=None
#
# — but the argument must be PRESENT, because omitting a required param is rejected at
# DISPATCH level and produces no inner envelope at all.  Hence a sentinel rather than an
# omission, and hence not SKIP: doctor CAN form the question here, the answer simply does
# not depend on the value, and reporting "didn't check" for two of five doors would hollow
# out the one scenario that proves all five advertised doors are dead.
#
# It is deliberately NOT a plausible glass name.  If the reference layer ever resolved the
# name before consulting the plane, this would surface as a loud ``glass_not_found`` rather
# than as an accidental pass.  That ordering is an assumption about SOMEONE ELSE'S code, so
# it is pinned by guard D-24 rather than trusted.
PROBE_SENTINEL = "__optivibe_doctor_probe__"

REFERENCE_CASES = {                # one canned, read-only call per public door
    "lookup_operand": {"query": "chief ray"},
    "lookup_glass": {"name": FROM_PLANE, "catalog": FROM_PLANE},
    "search_reference": {"query": "chief ray", "limit": 1},
    "find_glasses": {"nd_min": 1.50, "nd_max": 1.60, "limit": 1},
    "find_glass_pair": {"anchor": FROM_PLANE, "anchor_catalog": FROM_PLANE, "limit": 1},
}
# EVERY param NAME above is asserted in CI to be a member of that tool's advertised
# param_types / required_params (test D-4) — names stay statically pinned; only VALUES
# marked FROM_PLANE are derived.  The advertised parameter of find_glass_pair is "anchor",
# not "glass".
#
# THE PINNED-LITERAL RULE — pin IDENTITIES, derive VALUES.
#
#   Pin the IDENTITY set: the doors, the planes, the param NAMES, the critical dep names,
#   the supported mcp range.  DERIVE any value that is DATA.
#
# A pinned identity is a contract the artifact must honour; a pinned data value is a bet
# about someone else's content.  Measured live: a bare {"name": "N-BK7"} returns
# ambiguous_glass on a HEALTHY machine, because the glass catalog is built from the user's
# own .agf files and three catalogs carry that name.  Qualifying it with a pinned
# {"catalog": "SCHOTT"} fixes the observed failure and MOVES the risk onto a machine whose
# catalog set lacks SCHOTT — that is how a defect class survives a point fix.

# How a FROM_PLANE value resolves, per (tool, param): (plane, query, column index).
# ONE query, no fallback needed — a (name, catalog) pair taken from the catalog's own first
# row ALWAYS resolves, because qualifying by catalog is exactly what disambiguates.
FROM_PLANE_SOURCE = {
    ("lookup_glass", "name"): ("glass", "SELECT name, catalog FROM glass LIMIT 1", 0),
    ("lookup_glass", "catalog"): ("glass", "SELECT name, catalog FROM glass LIMIT 1", 1),
    ("find_glass_pair", "anchor"): ("glass", "SELECT name, catalog FROM glass LIMIT 1", 0),
    ("find_glass_pair", "anchor_catalog"): ("glass",
                                            "SELECT name, catalog FROM glass LIMIT 1", 1),
}

# Per-plane content fingerprint inputs, for the routing proof.  (table, key column) — the
# key column is hashed, sorted.  This REPLACES the pinned-token identity rule, which was
# unsound: TRAD is present in the MERIT catalog as well as the tolerance catalog, so a rule
# built on "this plane's token is present and the other planes' tokens are not" reported a
# healthy merit plane as misrouted — cry-wolf on every provisioned install.
PLANE_FINGERPRINT = {
    "merit_operand": ("operand", "code"),
    "tolerance_operand": ("operand", "code"),
    "glass": ("glass", "name"),
    "manual": None,          # file-backed: PRAGMA database_list is decisive
}

# enrichment domain -> the plane whose catalog carries its build report.
#
# The tier's ONLY source is that plane's own file, so it carries that plane's boundary.  The
# baked operand catalogs are user-built and are NOT published (measured: only the synonym
# and semantics tables ship), so under a rule that graded an absent plane's tier UNKNOWN,
# every fresh public install would read UNKNOWN twice and exit 3 while the contract says 1.
# Absent plane -> SKIP naming it (the target's configuration, already reported on the plane
# check); present plane with an unreadable report -> UNKNOWN (doctor could not measure).
# Pinned by guard D-25.
ENRICHMENT_PLANE = {
    "merit": "merit_operand",
    "tolerance": "tolerance_operand",
}

#: The precondition BOTH missing-data remedies share, and the one the build script cannot
#: state for itself.  The script honestly pre-discloses which steps it will SKIP, but every
#: plane is ultimately derived from vendor data that only a licensed OpticStudio
#: installation carries — the glass catalogs, the product manual, and the recorded live
#: readings the operand steps consume.  Without that installation a reader can follow the
#: command exactly, watch every step skip, and never reach a built plane; a remedy whose
#: precondition it never states is a remedy a stranger cannot complete, which is the same
#: diagnostic failure as a command that is not on disk, one step further out.
VENDOR_DATA_LICENCE_CLAUSE = (
    "Every plane is derived from vendor data that only a licensed OpticStudio installation "
    "carries — the glass catalogs, the product manual, and the recorded live readings the "
    "operand steps consume — so on a machine without OpticStudio the build reports those "
    "steps SKIPPED and no amount of rerunning will produce the data.")

REMEDY = {   # pinned strings; asserted verbatim
    "unwired": ("rebuilding will not help; the valid manual corpus is present but is not wired "
                "into the reference Dispatcher (server.py:204 does not mirror server.py:215). "
                "Upgrade to a release containing the wiring fix."),
    "vendor_data": ("expected on a fresh install — no vendor data is shipped. From the repository "
                    "root:\n"
                    "  python packages/optivibe-reference/scripts/build_vendor_data.py\n"
                    "It builds every plane it can, and names the live-probe scripts to run "
                    "first for any operand step it reports as SKIPPED. "
                    + VENDOR_DATA_LICENCE_CLAUSE),
    # Emitted INSTEAD of "vendor_data" when no source checkout is locatable (manifest is None).
    # packages/optivibe-reference/scripts/ is OUTSIDE src/ and ships in neither wheel (measured),
    # so on a checkout-less install the command above does not exist on disk.  A remedy the user
    # cannot run is a diagnostic failure, not a formatting detail.  (Test D-16.)
    "vendor_data_no_checkout": (
        "expected on a fresh install — no vendor data is shipped. The build scripts ship only in "
        "the source checkout, not in the installed package: clone the repository and run, from "
        "its root:\n"
        "  python packages/optivibe-reference/scripts/build_vendor_data.py\n"
        "It builds every plane it can, and names the live-probe scripts to run first for any "
        "operand step it reports as SKIPPED. " + VENDOR_DATA_LICENCE_CLAUSE),
    "mcp_range": 'pip install "mcp>=1.28,<2"',
    "runtime_override": ("PYTHONNET_RUNTIME is set to a value other than netfx. The ZOS-API "
                         "assemblies target the .NET Framework and the bootstrap only "
                         "setdefault()s netfx, so a pre-set value wins. Unset it, or set it to "
                         "netfx."),
    "nethelper": ("install OpticStudio, or set ZOSAPI_NETHELPER to the path of "
                  "ZOSAPI_NetHelper.dll. The reference tools still work without it."),
    "reinstall": "pip install -e . in packages/optivibe-harness (metadata is frozen at install time)",
    "reference_missing": "pip install -e ../optivibe-reference (the README's first install step)",
}

# The three *.coverage checks share ONE classifier — three copies of one rule is three
# rules that can drift.  Per kind: the reason tokens, and whether the pin is an EXACT
# identity set or a subset FLOOR.  That distinction is load-bearing, not cosmetic:
#   - dependency: CRITICAL_DEPS is a FLOOR.  matplotlib and numpy are legitimately in
#     Requires-Dist and not in the pin, so "a universe member the pin does not claim" is
#     NORMAL here and must NOT warn.  Every derived member is probed regardless.
#   - plane / tool: the pin IS the exact expected identity set, so a universe member the
#     pin does not claim is a real, ungraded new thing and must be named.
COVERAGE = {
    "dependency": {"pin_mode": "floor",
                   "missing": "critical_dep_undeclared", "unpinned": None},
    "plane": {"pin_mode": "exact",
              "missing": "pinned_plane_missing", "unpinned": "unpinned_plane"},
    "tool": {"pin_mode": "exact",
             "missing": "pinned_door_missing", "unpinned": "unpinned_door"},
}


def normalise_dist_name(raw):
    """PEP 503 name normalisation: ``lower()``, and any run of ``[-_.]`` becomes ``-``.

    Applied to BOTH sides of every dependency comparison before any set operation.  A
    raw-string comparison is an acceptance-set divergence waiting to happen: ``PSUtil`` in
    one distribution's ``Requires-Dist`` and ``psutil`` in the pin are the same dependency,
    and reporting the pin as missing would be a fabricated failure.

    Written without ``re`` so this module keeps importing nothing at all.
    """
    text = str(raw).strip().lower()
    out = []
    previous_sep = False
    for char in text:
        if char in "-_.":
            if not previous_sep:
                out.append("-")
            previous_sep = True
        else:
            out.append(char)
            previous_sep = False
    return "".join(out)


BASE_CHECKS = (                    # the SPINE: each appears exactly once, or exit 3
    "env.python", "env.cwd_shadow", "env.pythonnet_runtime",
    "version.optivibe-harness", "version.optivibe-reference",
    "dependency.mcp", "dependency.psutil", "dependency.pythonnet",
    "dependency.mcp_range",
    "dependency.coverage",
    "reference.import",
    # Importing the package and CONSTRUCTING its Dispatcher are different claims, and the
    # second is the one every plane's wiring and every door's answer depends on.  Without
    # its own id a Dispatcher that raises on construction leaves every downstream check
    # reading "doctor could not measure" — INCOMPLETE/3 — for what is a definitive TARGET
    # defect, and with no line anywhere naming the exception doctor already caught.
    "reference.dispatcher",
    "plane.merit_operand", "plane.tolerance_operand", "plane.glass", "plane.manual",
    "plane.coverage", "enrichment.merit", "enrichment.tolerance",
    "tool.lookup_operand", "tool.lookup_glass", "tool.search_reference",
    "tool.find_glasses", "tool.find_glass_pair", "tool.coverage",
    "harness.import", "harness.manifest", "composed.manifest", "mcp.construct",
    "zemax.nethelper", "zemax.dir",
    "boot.exit",                     # SKIP/not_requested unless --boot
    "engine.license",                # SKIP/not_requested unless --engine
    # The other half of --engine, and the half that gates on doctor's OWN footprint: a run
    # that proves the licence and then leaks the engine it opened has taken the single seat
    # away from the user it was helping.  PASS requires a post-close OS re-read showing the
    # owned process is gone — never the close call's own return value.
    "engine.cleanup",                # SKIP/not_requested unless --engine
)
