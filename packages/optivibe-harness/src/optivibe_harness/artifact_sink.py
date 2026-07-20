"""artifact_sink.py — durable per-run snapshot sink for OpticStudio ``.zmx`` files.

A snapshot is taken by calling an injected ``save_as`` (the ZOS-API
``TheSystem.SaveAs(path)`` seam). Probe A1 showed that ``SaveAs`` returns
``None`` and NEVER raises on a bad path — a nonexistent dir is a silent no-op and
an illegal filename gets ADS-truncated to a 0-byte file. So the on-disk
durability gate (``isfile AND getsize >= min_snapshot_bytes``) is the ONLY truth
source, and ``_safe_name`` rejecting ``:`` is what blocks the ADS 0-byte trap.
A real trivial ``.zmx`` is ~4358 bytes, so the gate default is 256 bytes.

Every snapshot ALWAYS appends a manifest row (including ``ok=False``) and
``snapshot()`` NEVER raises.

Live ZOS-API integration: N/A this tier (no backend; ``SaveAs`` is injected).
"""
import json
import os
import sys
from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

from . import _io

MANIFEST_SCHEMA_VERSION = 1

# Windows reserved device names (case-insensitive), with or without an extension.
_RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
)

# Illegal-on-Windows filename characters (the colon also blocks the ADS trap).
_ILLEGAL_CHARS = '<>:"/\\|?*'


class RunIdCollisionError(Exception):
    """Raised when a run_id's directory already exists and is non-empty."""


@runtime_checkable
class SaveAsFn(Protocol):
    """The injected save seam: ``TheSystem.SaveAs(path) -> None``."""

    def __call__(self, path: str) -> None:  # pragma: no cover - structural protocol
        ...


@dataclass(frozen=True)
class SnapshotResult:
    """Outcome of one ``snapshot`` call."""

    ok: bool
    path: str
    bytes: int
    seq: int
    label: str
    error: Optional[str] = None


def _safe_name(label: str) -> str:
    """Sanitize ``label`` into a safe filename STEM (no extension).

    - A Windows reserved device name (CON/PRN/AUX/NUL/COM1-9/LPT1-9, case
      insensitive, with or without an extension) is prefixed with ``snapshot_``.
    - Illegal chars (``<>:"/\\|?*``) and control chars become ``_``.
    - Trailing dots/spaces are stripped (AFTER truncation, so truncation can
      never re-introduce a trailing dot/space that Windows would silently trim).
    - An empty result becomes the literal stem ``snapshot``. Uniqueness across
      empty-label snapshots is carried by the ``{seq:04d}_`` filename prefix that
      ``snapshot()`` prepends, not by the stem itself.
    - The stem is truncated to <= 120 chars.

    The colon is among the illegal chars, so an ADS path can never be produced.
    """
    name = label if isinstance(label, str) else str(label)

    # Replace illegal + control chars with underscore.
    cleaned = []
    for ch in name:
        if ch in _ILLEGAL_CHARS or ord(ch) < 32:
            cleaned.append("_")
        else:
            cleaned.append(ch)
    name = "".join(cleaned)

    # Strip trailing dots/spaces (Windows trims these and would orphan the ext).
    name = name.rstrip(". ")

    # Reserved-name check on the base (ignore any extension the label carried).
    base = name.split(".", 1)[0]
    if base.upper() in _RESERVED_NAMES:
        name = f"snapshot_{name}"

    # Truncate FIRST, then strip again: truncation can cut back into a run of
    # dots/spaces and re-expose a trailing one, so the rstrip MUST run after it.
    if len(name) > 120:
        name = name[:120]
    name = name.rstrip(". ")

    if name == "":
        name = "snapshot"

    return name


def _sanitize_nonfinite(value):
    """Recursively replace non-finite floats with string sentinels.

    ``meta`` is an arbitrary caller-supplied dict, so a non-finite float
    (``inf``/``-inf``/``nan``) can sit anywhere — top level, nested dict values,
    or list/tuple items. Each is converted to ``"inf"``/``"-inf"``/``"nan"`` via
    ``_io.safe_float`` so the manifest row stays strict JSON
    (``json.dumps(..., allow_nan=False)`` would otherwise raise). All other
    values pass through untouched. Tuples are normalized to lists (json.dumps
    already emits tuples as arrays, so this preserves the on-disk shape).
    """
    if isinstance(value, dict):
        return {k: _sanitize_nonfinite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_nonfinite(v) for v in value]
    return _io.safe_float(value)


class ArtifactSink:
    """Durable per-run sink for OpticStudio ``.zmx`` snapshots."""

    def __init__(
        self,
        base_dir,
        run_id: str,
        save_as,
        *,
        min_snapshot_bytes: int = 256,
        logger=None,
        fsync: bool = True,
        start_seq: Optional[int] = None,
    ):
        self.base_dir = os.fspath(base_dir)
        self.run_id = run_id
        self.save_as = save_as
        self.min_snapshot_bytes = min_snapshot_bytes
        self.logger = logger
        self._fsync = fsync
        # ``start_seq`` is the collision-free APPEND seam (§4.5): when
        # not None the caller is EXPLICITLY opting into append mode — it seeds the
        # sequence counter (so new snapshots get fresh ``{seq:04d}_`` prefixes PAST
        # the existing max) AND BYPASSES the RunIdCollisionError guard below by
        # design (appending to a non-empty run_dir is the intent, not a collision).
        # When None the original behavior is unchanged BYTE-FOR-BYTE (counter starts
        # at 0, the non-empty-run_dir collision guard is active).
        #
        # A negative ``start_seq`` would yield a malformed ``-005_`` filename
        # prefix (``f"{-5:04d}"`` -> ``"-005"``), and is a programmer error — reject
        # it at construction. (The workspace tool wraps construction in try, so this
        # ValueError surfaces as ``workspace_unwritable`` and never reaches dispatch.)
        # Note: ``start_seq=0`` is a VALID explicit append-mode opt-in (it bypasses
        # the collision guard by design — the caller asked to append at seq 0).
        if start_seq is not None and start_seq < 0:
            raise ValueError("start_seq must be >= 0")
        self._seq = int(start_seq) if start_seq is not None else 0
        # Count of snapshots whose manifest audit row was lost (the durability
        # gate passed but the manifest append then failed). Such a snapshot is
        # reported with ok=False so the caller always knows the row is gone.
        self.dropped_rows = 0

        self.run_dir = os.path.join(self.base_dir, run_id)
        self.manifest_path = os.path.join(self.run_dir, "manifest.jsonl")

        # Collision: run_dir exists AND is non-empty. BYPASSED when the caller
        # passed an explicit start_seq (append mode, §4.5) — appending to an
        # existing run_dir is the intent there, not a collision.
        if (
            start_seq is None
            and os.path.isdir(self.run_dir)
            and os.listdir(self.run_dir)
        ):
            raise RunIdCollisionError(
                f"run_dir already exists and is non-empty: {self.run_dir}"
            )

        # mkdir (creates base_dir too); an unwritable base_dir raises here.
        os.makedirs(self.run_dir, exist_ok=True)

    def _write_manifest_row(self, row: dict) -> None:
        # Mirror the hardening applied to interaction_log/journal: a
        # non-finite float anywhere in the row (especially in caller-supplied
        # ``meta``, recursively) becomes a string sentinel, and the row is
        # serialized with ``allow_nan=False`` so a bare ``NaN``/``Infinity``
        # token can never reach manifest.jsonl and corrupt the corpus.
        sanitized = _sanitize_nonfinite(row)
        prev = _io.FSYNC_ENABLED
        _io.FSYNC_ENABLED = self._fsync
        try:
            _io.append_line_fsync(
                self.manifest_path,
                json.dumps(sanitized, ensure_ascii=False, allow_nan=False),
            )
        finally:
            _io.FSYNC_ENABLED = prev

    def snapshot(self, label: str, meta: Optional[dict] = None) -> SnapshotResult:
        """Take one snapshot. ALWAYS appends a manifest row. NEVER raises.

        Builds ``f"{seq:04d}_{_safe_name(label)}.zmx"``, calls ``save_as`` on the
        target, then applies the durability gate (``isfile AND getsize >= min``).
        A pass yields ``ok=True``; a failure or exception yields ``ok=False`` with
        an ``error`` string. The manifest row is written in both cases. If the
        manifest append itself fails, the audit row is lost: ``snapshot()`` still
        does not raise, but flips the result to ``ok=False`` with
        ``error="manifest_write_failed: ..."``, increments ``dropped_rows``, and
        routes a note to the logger/stderr so the caller can never miss it.
        """
        seq = self._seq
        filename = f"{seq:04d}_{_safe_name(label)}.zmx"
        target = os.path.join(self.run_dir, filename)

        ok = False
        size = 0
        error = None

        try:
            self.save_as(target)
        except Exception as exc:  # noqa: BLE001 — SaveAs failures must not abort
            error = f"{type(exc).__name__}: {exc}"

        if error is None:
            try:
                if os.path.isfile(target) and os.path.getsize(target) >= self.min_snapshot_bytes:
                    size = os.path.getsize(target)
                    ok = True
                else:
                    size = os.path.getsize(target) if os.path.isfile(target) else 0
                    error = (
                        f"durability gate failed: isfile="
                        f"{os.path.isfile(target)} bytes={size} "
                        f"min={self.min_snapshot_bytes}"
                    )
            except OSError as exc:
                error = f"durability gate error: {type(exc).__name__}: {exc}"

        row = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "run_id": self.run_id,
            "seq": seq,
            "ts": _io.utc_now_iso(),
            "label": label,
            "filename": filename,
            "bytes": size,
            "ok": ok,
            "error": error,
            "meta": meta,
        }
        try:
            self._write_manifest_row(row)
        except Exception as exc:  # noqa: BLE001 — never raise out of snapshot
            # The manifest row is the audit trail; a swallowed append means the
            # row is permanently lost. The caller MUST be able to tell, so flip
            # the result to ok=False, attach a distinct error, bump the counter,
            # and route a note to the logger (or stderr, guarded).
            self.dropped_rows += 1
            ok = False
            error = f"manifest_write_failed: {type(exc).__name__}: {exc}"
            if self.logger is not None:
                try:
                    self.logger.error("manifest append failed: %s", exc)
                except Exception:  # noqa: BLE001
                    pass
            else:
                try:
                    print(
                        f"[artifact_sink] manifest row LOST (seq={seq}): {error}",
                        file=sys.stderr,
                    )
                except Exception:  # noqa: BLE001 — never let the note raise
                    pass

        self._seq += 1
        return SnapshotResult(
            ok=ok, path=target, bytes=size, seq=seq, label=label, error=error
        )

    def load_manifest(self) -> list:
        """Load manifest rows; skip a torn final line only."""
        if not os.path.isfile(self.manifest_path):
            return []
        with open(self.manifest_path, "r", encoding="utf-8", newline="") as fh:
            lines = fh.read().split("\n")
        if lines and lines[-1] == "":
            lines.pop()

        rows = []
        last = len(lines) - 1
        for i, line in enumerate(lines):
            if line == "":
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                if i == last:
                    break  # torn final line — tolerate the crash-tail
                raise
        return rows
