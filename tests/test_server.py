"""Boot checks: the served transcript, the composed manifest, and the harness-only degraded mode."""
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import tomllib

from test_package import (BLOCKED_ROOTS, PACKAGE_DIRS, RECEIPT_ENV,
                          _import_guard_source, _repo_root)

SERVER_NAME = "optivibe"
LIBRARY_DISTRIBUTION = "mcp"
ARTIFACT_DIR_ENV = "OPTIVIBE_TEST_ARTIFACT_DIR"
BOOT_TIMEOUT_SECONDS = 180
CORRELATED_REPLIES = 2

REFERENCE_DOOR_NAMES = frozenset({
    "search_reference",
    "lookup_operand",
    "lookup_glass",
    "find_glasses",
    "find_glass_pair",
})

# Six tools pinned by name so a deleted selector cannot leave the graded universe.
SWEEPING_SELECTOR_TOOLS = ("get_first_order", "analyze_strehl", "check_clearance")
SINGLE_SELECTOR_TOOLS = ("get_mtf", "get_spot", "analyze_axial_color")

SWEEPING_SELECTOR_SCHEMA = {"anyOf": [{"type": "number"}, {"type": "string", "enum": ["all"]}]}
SINGLE_SELECTOR_SCHEMA = {"type": "number"}
SELECTOR = "config"

# The one dependency whose distribution name differs from its import name.
DEPENDENCY_IMPORT_NAMES = {"pythonnet": "clr"}

_BOOTS = {}


def _boot(block_extra=()):
    """Boot the server in a child over real stdio and return its transcript, receipt and traces."""
    key = tuple(sorted(block_extra))
    if key in _BOOTS:
        return _BOOTS[key]

    from mcp.types import LATEST_PROTOCOL_VERSION
    frames = (
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": LATEST_PROTOCOL_VERSION, "capabilities": {},
                    "clientInfo": {"name": "engine-free", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )

    guard_dir = tempfile.mkdtemp()
    work_dir = tempfile.mkdtemp()
    receipt_path = os.path.join(guard_dir, "guard-receipt.json")
    with open(os.path.join(guard_dir, "sitecustomize.py"), "w", newline="\n") as handle:
        handle.write(_import_guard_source(block_extra))

    env = dict(os.environ)
    env.pop("OPTIVIBE_WORKSPACE_ROOT", None)
    env.pop("OPTIVIBE_LOG_DIR", None)
    env["PYTHONPATH"] = guard_dir
    env[RECEIPT_ENV] = receipt_path

    # Reading the correlated replies before closing stdin is load-bearing: end of
    # input cancels a request still in flight, dropping its reply about one boot in
    # sixteen while the child still exits zero. The kill timer is the hang guard.
    before = sorted(os.listdir(work_dir))
    child = subprocess.Popen(
        [sys.executable, "-m", "optivibe_harness"],
        cwd=work_dir, env=env, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    watchdog = threading.Timer(BOOT_TIMEOUT_SECONDS, child.kill)
    watchdog.start()
    transcript = []
    try:
        for frame in frames:
            child.stdin.write(json.dumps(frame) + "\n")
        child.stdin.flush()
        while len(transcript) < CORRELATED_REPLIES:
            line = child.stdout.readline()
            if not line:
                break
            transcript.append(line)
        child.stdin.close()
        trailing, errors = child.communicate()
    finally:
        watchdog.cancel()
    stdout_text = "".join(transcript) + (trailing or "")
    errors = errors or ""
    after = sorted(os.listdir(work_dir))

    parse_error = None
    replies = {}
    for line in stdout_text.splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            parse_error = "a line reaching the transport is not valid JSON: %r" % (line[:200],)
            break
        identifier = message.get("id")
        if identifier not in (1, 2):
            parse_error = "an unexpected reply id reached the transport: %r" % (identifier,)
            break
        if "error" in message or "result" not in message:
            parse_error = "reply %r carried no clean result: %r" % (identifier, message)
            break
        replies[identifier] = message["result"]
    if parse_error is None and set(replies) != {1, 2}:
        parse_error = ("the transport carried reply ids %r, not exactly the two correlated "
                       "replies" % (sorted(replies),))

    server_info, instructions, names, schemas = {}, "", [], {}
    if parse_error is None:
        server_info = replies[1].get("serverInfo") or {}
        instructions = replies[1].get("instructions") or ""
        served = replies[2].get("tools") or []
        names = [entry.get("name") for entry in served]
        schemas = {entry.get("name"): (entry.get("inputSchema") or {}) for entry in served}

    receipt = None
    if os.path.isfile(receipt_path):
        with open(receipt_path, encoding="utf-8") as handle:
            receipt = json.load(handle)

    def _explain():
        manifest = os.path.join(_repo_root(), "packages",
                                PACKAGE_DIRS["optivibe_harness"], "pyproject.toml")
        with open(manifest, "rb") as handle:
            declared = tomllib.load(handle)["project"]["dependencies"]
        wanted = ["optivibe_harness.__main__"]
        for item in declared:
            bare = re.split(r"[\s\[<>=!~;]", item, maxsplit=1)[0].strip()
            module = DEPENDENCY_IMPORT_NAMES.get(bare, bare)
            if bare and bare not in BLOCKED_ROOTS and module not in BLOCKED_ROOTS:
                wanted.append(module)
        probe = subprocess.run(
            [sys.executable, "-c",
             "import importlib, sys\nfor name in %r:\n    importlib.import_module(name)\n"
             % (wanted,)],
            capture_output=True, text=True, timeout=180)
        return ("the server child exited %d writing %d stderr bytes; importing the entry "
                "point and its declared runtime dependencies %r exited %d\n%s"
                % (child.returncode, len(errors.encode("utf-8")),
                   wanted, probe.returncode, probe.stderr[-3000:]))

    result = {
        "rc": child.returncode,
        "names": names,
        "instructions": instructions,
        "server_info": server_info,
        "receipt": receipt,
        "cwd_before": before,
        "cwd_after": after,
        "stderr_bytes": len(errors.encode("utf-8")),
        "schemas": schemas,
        "parse_error": parse_error,
        "diagnostic": _explain() if child.returncode != 0 else "",
    }

    label = "degraded" if key else "composed"
    keep = os.environ.get(ARTIFACT_DIR_ENV)
    if keep:
        os.makedirs(keep, exist_ok=True)
        with open(os.path.join(keep, label + "-receipt.json"), "w", newline="\n") as handle:
            json.dump(receipt, handle, indent=2)
        summary = dict(result)
        summary.pop("schemas")
        summary["names"] = sorted(names)
        with open(os.path.join(keep, label + "-summary.json"), "w", newline="\n") as handle:
            json.dump(summary, handle, indent=2)
    else:
        shutil.rmtree(guard_dir, ignore_errors=True)
        shutil.rmtree(work_dir, ignore_errors=True)

    _BOOTS[key] = result
    return result


def test_server_boots_and_serves_its_manifest():
    boot = _boot()
    assert boot["rc"] == 0, boot["diagnostic"]
    assert boot["parse_error"] is None, boot["parse_error"]
    assert boot["names"], "the served manifest is empty"
    assert len(boot["names"]) == len(set(boot["names"])), (
        "the served manifest repeats a name: %r" % (sorted(boot["names"]),))
    assert boot["server_info"].get("name") == SERVER_NAME, (
        "the server announced itself as %r" % (boot["server_info"].get("name"),))
    assert boot["server_info"].get("version") == importlib.metadata.version(
        LIBRARY_DISTRIBUTION), (
        "the announced version %r is not the installed protocol library version %r"
        % (boot["server_info"].get("version"),
           importlib.metadata.version(LIBRARY_DISTRIBUTION)))
    assert boot["instructions"].strip(), "the server served no instructions"
    absent = sorted(REFERENCE_DOOR_NAMES - set(boot["names"]))
    assert absent == [], "the composed manifest is missing %r" % (absent,)
    assert boot["receipt"] is not None, "the import guard wrote no receipt"
    assert boot["receipt"].get("armed") is True, "the import guard was not armed"
    assert boot["receipt"].get("attempted") == [], (
        "booting reached a backend import: %r" % (boot["receipt"].get("attempted"),))
    assert boot["cwd_before"] == [], "the working directory was not empty before the boot"
    assert boot["cwd_after"] == [], (
        "booting left files behind: %r" % (boot["cwd_after"],))
    assert boot["stderr_bytes"] == 0, (
        "the child wrote %d bytes outside the transport" % boot["stderr_bytes"])

    served = boot["schemas"]
    for name in SWEEPING_SELECTOR_TOOLS + SINGLE_SELECTOR_TOOLS:
        properties = (served.get(name) or {}).get("properties") or {}
        assert SELECTOR in properties, (
            "%r no longer advertises a %r property" % (name, SELECTOR))
    sweeping, single, unrecognised = [], [], []
    for name in sorted(served):
        properties = (served[name] or {}).get("properties") or {}
        if SELECTOR not in properties:
            continue
        shape = properties[SELECTOR]
        if shape == SWEEPING_SELECTOR_SCHEMA:
            sweeping.append(name)
        elif shape == SINGLE_SELECTOR_SCHEMA:
            single.append(name)
        else:
            unrecognised.append((name, shape))
    assert unrecognised == [], (
        "these tools advertise neither declared selector shape: %r" % (unrecognised,))
    assert sweeping, "no tool advertises the sweeping selector shape"
    assert single, "no tool advertises the single-value selector shape"
    assert served["get_first_order"]["properties"][SELECTOR] == SWEEPING_SELECTOR_SCHEMA, (
        "get_first_order advertises %r"
        % (served["get_first_order"]["properties"][SELECTOR],))
    assert served["get_mtf"]["properties"][SELECTOR] == SINGLE_SELECTOR_SCHEMA, (
        "get_mtf advertises %r" % (served["get_mtf"]["properties"][SELECTOR],))


def test_server_degrades_to_harness_only():
    degraded = _boot(block_extra=("optivibe_reference",))
    composed = _boot()
    assert degraded["rc"] == 0, degraded["diagnostic"]
    assert degraded["parse_error"] is None, degraded["parse_error"]
    assert degraded["names"], "the degraded manifest is empty"
    assert composed["names"], "the composed manifest is empty"

    assert degraded["receipt"] is not None, "the import guard wrote no receipt"
    attempted = degraded["receipt"].get("attempted")
    assert attempted == ["optivibe_reference"], (
        "the degradation was not induced; the guard recorded %r" % (attempted,))

    present = sorted(REFERENCE_DOOR_NAMES & set(degraded["names"]))
    assert present == [], "the degraded manifest still serves %r" % (present,)
    named = sorted(name for name in REFERENCE_DOOR_NAMES if name in degraded["instructions"])
    assert named == [], "the degraded instructions still name %r" % (named,)

    harness_only = set(degraded["names"])
    composite = set(composed["names"])
    assert harness_only < composite, (
        "the degraded manifest is not a proper subset; it adds %r"
        % (sorted(harness_only - composite),))
    assert composite - harness_only == set(REFERENCE_DOOR_NAMES), (
        "composing added %r, not exactly the reference doors"
        % (sorted(composite - harness_only),))

    assert degraded["instructions"].strip(), "the degraded server served no instructions"
    assert len(degraded["instructions"]) < len(composed["instructions"]), (
        "the degraded instructions are %d characters against %d composed"
        % (len(degraded["instructions"]), len(composed["instructions"])))
    assert degraded["cwd_before"] == [], "the working directory was not empty before the boot"
    assert degraded["cwd_after"] == [], (
        "the degraded boot left files behind: %r" % (degraded["cwd_after"],))
    assert degraded["stderr_bytes"] == 0, (
        "the degraded child wrote %d bytes outside the transport" % degraded["stderr_bytes"])
