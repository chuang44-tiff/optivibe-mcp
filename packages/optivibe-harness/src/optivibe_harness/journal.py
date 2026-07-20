"""journal.py — per-run optimization journal (header-line + entry-per-line JSONL).

The on-disk format is a header line (``kind="journal_header"``) followed by one
entry per line. Each entry records an optimization iteration: an optional merit
snapshot, a free-text note, and/or an artifact reference. Like the interaction
log, ``add()`` NEVER aborts a run on a logging failure.

Live ZOS-API integration: N/A this tier (no backend; merit values are supplied
by the caller, not read here).
"""
import dataclasses
import json
import sys
from dataclasses import asdict, dataclass
from typing import Optional

from . import _io

SCHEMA_VERSION = 1


class JournalSchemaError(Exception):
    """Raised on load when a header or entry carries an unknown schema_version."""


@dataclass(frozen=True)
class MeritSnapshot:
    """A single merit-function reading at an optimization checkpoint."""

    merit_value: float
    cycles: int
    wall_clock_s: float
    verdict: str


@dataclass(frozen=True)
class JournalEntry:
    """One journal entry (one optimization iteration)."""

    iteration: int
    ts: str
    merit: Optional[dict] = None
    note: Optional[str] = None
    artifact_ref: Optional[str] = None
    schema_version: int = SCHEMA_VERSION


class Journal:
    """Header-line + entry-per-line JSONL optimization journal."""

    def __init__(self, path, run_id: str, *, fsync: bool = True):
        self.path = path
        self.run_id = run_id
        self._fsync = fsync
        self.dropped_records = 0
        self._write_header()

    def _write_line(self, line: str) -> None:
        prev = _io.FSYNC_ENABLED
        _io.FSYNC_ENABLED = self._fsync
        try:
            _io.append_line_fsync(self.path, line)
        finally:
            _io.FSYNC_ENABLED = prev

    def _write_header(self) -> None:
        header = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "created_ts": _io.utc_now_iso(),
            "kind": "journal_header",
        }
        self._write_line(json.dumps(header, ensure_ascii=False))

    def add(
        self,
        *,
        iteration: int,
        merit=None,
        note: Optional[str] = None,
        artifact_ref: Optional[str] = None,
    ) -> Optional[JournalEntry]:
        """Append one journal entry. NEVER raises.

        ``merit`` may be a ``MeritSnapshot`` or ``None``. On any failure the
        entry is dropped: a stderr note is emitted, ``dropped_records`` is
        incremented, and ``None`` is returned.
        """
        try:
            merit_dict = asdict(merit) if isinstance(merit, MeritSnapshot) else merit
            if isinstance(merit_dict, dict):
                # A diverged/failed optimization step can legitimately yield
                # nan/inf merit; sanitize the float fields to string sentinels so
                # the JSONL corpus stays strict-JSON (allow_nan=False below).
                merit_dict = dict(merit_dict)
                if "merit_value" in merit_dict:
                    merit_dict["merit_value"] = _io.safe_float(merit_dict["merit_value"])
                if "wall_clock_s" in merit_dict:
                    merit_dict["wall_clock_s"] = _io.safe_float(merit_dict["wall_clock_s"])
            entry = JournalEntry(
                iteration=iteration,
                ts=_io.utc_now_iso(),
                merit=merit_dict,
                note=note,
                artifact_ref=artifact_ref,
            )
            self._write_line(json.dumps(asdict(entry), ensure_ascii=False, allow_nan=False))
            return entry
        except Exception as exc:  # noqa: BLE001 — substrate must not abort a run
            self.dropped_records += 1
            # The stderr note itself must never escape (closed/broken stderr must
            # not break the NEVER-raises contract).
            try:
                print(
                    f"[journal] dropped entry (iteration~{iteration}): "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
            except Exception:  # noqa: BLE001 — never let the drop-note raise
                pass
            return None


def load(path):
    """Load a journal: returns ``(header_dict, list[JournalEntry])``.

    Skips a torn final line only. An unknown ``schema_version`` on either the
    header or any entry raises ``JournalSchemaError``.
    """
    with open(path, "r", encoding="utf-8", newline="") as fh:
        lines = fh.read().split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    # Drop any other trailing empties from buffering.
    while lines and lines[-1] == "":
        lines.pop()

    if not lines:
        raise JournalSchemaError("empty journal: no header line")

    last = len(lines) - 1
    header = None
    entries = []

    for i, line in enumerate(lines):
        if line == "":
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            if i == last:
                break  # torn final line — tolerate the crash-tail
            raise

        if header is None:
            if obj.get("schema_version") != SCHEMA_VERSION:
                raise JournalSchemaError(
                    f"unknown header schema_version: {obj.get('schema_version')!r}"
                )
            header = obj
            continue

        if obj.get("schema_version") != SCHEMA_VERSION:
            raise JournalSchemaError(
                f"unknown entry schema_version: {obj.get('schema_version')!r}"
            )
        # Forward-compat: at the SAME schema_version, tolerate additive fields
        # from a newer writer by filtering to the known dataclass fields. (A
        # higher schema_version already raised JournalSchemaError above.)
        known = {f.name for f in dataclasses.fields(JournalEntry)}
        entries.append(JournalEntry(**{k: v for k, v in obj.items() if k in known}))

    if header is None:
        raise JournalSchemaError("journal has no parseable header line")
    return header, entries
