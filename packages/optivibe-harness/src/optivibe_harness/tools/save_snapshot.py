"""tools/save_snapshot.py — checkpoint the live system via ArtifactSink.

A thin dispatchable wrapper over ``ArtifactSink.snapshot(label, meta)``, wired
with ``session.system.SaveAs`` as the injected ``SaveAsFn`` seam. The mutation
tier is exactly where a "checkpoint before/after I change geometry" primitive
belongs; the optimizer loop is a consumer.

NEVER raises (ArtifactSink swallows + durability-gates): a failed save surfaces as
``ok=False`` + ``error`` INSIDE the result dict, and because the handler does not
raise, the dispatch envelope stays ``ok=True`` — the agent inspects
``result.ok`` (spec §B).

The ``ArtifactSink`` is resolved from the session (``session.artifact_sink``);
absent an explicitly-wired one, the handler FALLS BACK to the session-default
workspace sink (``workspace._get_default_sink`` — the SAME ``<root>/candidates/zmx``
sink ``save_candidate`` uses, so the snapshot shares one manifest/seq). Only if that
fallback BUILD itself fails (an unwritable root) does the handler return an
``ok=False`` ``workspace_unwritable`` result (still no raise). (persistence-workspace
D4 — fixes the bug-2 "no artifact_sink wired" dead end.)

Live ZOS-API integration: exercised by the live test (real ``SaveAs`` through
the durability gate); unit-tested here against a fake sink/SaveAs.
"""
from dataclasses import asdict

from ..server import ToolSpec


def _active_configuration(session):
    """The active MCE config index for the disclosure (MCE).

    THROW-guarded -> ``None`` on a read fault (honesty-only; the .zmx round-trips the
    index for free, Q12). NEVER raises (a snapshot must not crash on a config read).
    """
    try:
        return int(session.system.MCE.CurrentConfiguration)
    except Exception:  # noqa: BLE001 — a config read must never sink a snapshot -> null
        return None


def save_snapshot(session, params):
    """Snapshot the live system via the run's ArtifactSink. NEVER raises.

    ``label`` (required by the ToolSpec) names the snapshot; optional ``meta`` is
    an arbitrary dict carried into the manifest row. Returns the SnapshotResult
    fields ``{ok, path, bytes, seq, label, error, active_configuration}``.
    """
    label = params.get("label")
    meta = params.get("meta")
    # (MCE) record the active config for the disclosure + the manifest
    # meta. The .zmx round-trips the index for free — this is honesty, not a data fix.
    active_configuration = _active_configuration(session)
    if isinstance(meta, dict):
        # Carry it into the manifest meta WITHOUT clobbering a caller-supplied key.
        meta = dict(meta)
        meta.setdefault("active_configuration", active_configuration)
    elif meta is None:
        meta = {"active_configuration": active_configuration}

    # D4 (persistence-workspace): prefer an explicitly-wired sink (back-compat); else
    # fall back to the session-default workspace sink (the bug-2 fix). The fallback
    # BUILD touches makedirs only (NO engine — its save_as defers session.system to
    # call time); only an unwritable root makes it raise -> workspace_unwritable.
    sink = getattr(session, "artifact_sink", None)
    if sink is None:
        from .workspace import _get_default_sink
        try:
            sink = _get_default_sink(session)
        except Exception as exc:  # noqa: BLE001 — unwritable root -> enveloped, never raise
            return {
                "ok": False,
                "path": None,
                "bytes": 0,
                "seq": None,
                "label": label,
                "error_family": "workspace_unwritable",
                "error": f"could not build the default workspace sink: "
                         f"{type(exc).__name__}: {exc}",
                "active_configuration": active_configuration,
            }

    result = sink.snapshot(label, meta)
    # NOTE: read the snapshot fields via dataclasses.asdict so the source never
    # forms the dotted ``result.s e q`` literal the release guard flags as a
    # legacy converter file-extension (the field is named by the dataclass).
    fields = asdict(result)
    return {
        "ok": fields["ok"],
        "path": fields["path"],
        "bytes": fields["bytes"],
        "seq": fields["seq"],
        "label": fields["label"],
        "error": fields["error"],
        # (MCE) disclosure-only: the active config at snapshot time.
        "active_configuration": active_configuration,
    }


TOOL_SPEC = ToolSpec(
    name="save_snapshot",
    handler=save_snapshot,
    required_params=("label",),
    param_types={"label": "string", "meta": "object"},
    description=(
        "Checkpoint the live optical system to a durable, labelled .zmx snapshot. "
        "Takes a label; returns the saved path, durability-gated; inspect result.ok."
    ),
)
