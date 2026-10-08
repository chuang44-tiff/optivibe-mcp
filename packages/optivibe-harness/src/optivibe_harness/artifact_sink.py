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

#: The longest stem ``_safe_name`` returns. Named so the optimize ``run_id`` door states
#: and enforces the SAME limit (tools/_workspace_paths.py RUN_ID_RULE), never a copy.
_MAX_STEM_CHARS = 120


class RunIdCollisionError(Exception):
    """Raised when a run_id's directory already exists and is non-empty."""


@runtime_checkable
class SaveAsFn(Protocol):
    """The injected save seam: ``TheSystem.SaveAs(path) -> None``."""

    def __call__(self, path: str) -> None:  # pragma: no cover - structural protocol
        ...


@dataclass(frozen=True)
class SnapshotResult:
    """Outcome of one ``snapshot`` call.

    ``index_advanced`` / ``requested_index`` disclose the pre-write existence check
    having fired: the target the caller asked for was already on disk, so the index
    was advanced rather than the bytes overwritten. On the nominal path they read
    ``False`` / the index that was used.
    """

    ok: bool
    path: str
    bytes: int
    seq: int
    label: str
    error: Optional[str] = None
    index_advanced: bool = False
    requested_index: Optional[int] = None


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
    - The stem is truncated to <= _MAX_STEM_CHARS chars.

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
    if len(name) > _MAX_STEM_CHARS:
        name = name[:_MAX_STEM_CHARS]
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

    #: Bound on the pre-write existence-check advance loop. A thousand consecutive
    #: occupied names is not a collision any more, it is a broken workspace.
    _MAX_INDEX_ADVANCE = 1000

    def _compose(self, label, index, filename, design_name):
        """The name for ``index`` under this call's scheme. ONE composer per scheme.

        With no caller-supplied name this is the legacy trail scheme and ``index`` is
        the instance counter. With one, it is the v2 candidate scheme and the design
        part is recovered EXACTLY — never by pattern-matching the index token, which
        is ambiguous for a design whose own name ends in a delimiter.
        """
        from . import artifact_naming
        if filename is None:
            return artifact_naming.trail_name(index, label)
        return artifact_naming.candidate_zmx_name(design_name, index, label)

    @staticmethod
    def _design_part(filename, index, label):
        """The design prefix of a v2 basename, or ``None`` when it is not one.

        Recovered by stripping the SUFFIX the composer built from values this call
        already holds (``index`` and ``label``), so it is exact where a search for
        the index token is not: for design ``alpha`` with label ``001_x`` and for
        design ``alpha_001`` with label ``x`` the basename is the same string, and
        only the known label tells them apart.
        """
        from . import artifact_naming
        suffix = (
            f"_{int(index):0{artifact_naming.CANDIDATE_INDEX_DIGITS}d}"
            f"_{_safe_name(label)}.zmx"
        )
        if not filename.endswith(suffix):
            return None
        return filename[: len(filename) - len(suffix)]

    def snapshot(self, label: str, meta: Optional[dict] = None, *,
                 index: Optional[int] = None,
                 filename: Optional[str] = None) -> SnapshotResult:
        """Take one snapshot. ALWAYS appends a manifest row. NEVER raises.

        With ``index`` and ``filename`` BOTH ``None`` this is byte-identical to what
        it has always done: the instance counter names the file and is advanced.
        With BOTH supplied the caller owns the name — ``os.path.basename(filename)``
        is written into ``run_dir``, the recorded index is ``index``, and the instance
        counter is NOT touched. Exactly one supplied is a programmer error and raises
        ``ValueError`` (the call site is inside ``save_candidate``'s existing ``try``).

        **Pre-write existence check, on BOTH paths.** If the target already exists the
        index is advanced, the name recomposed, and the write retried (bounded by
        ``_MAX_INDEX_ADVANCE``; past that the result is ``ok=False``). ``SaveAs``
        silently overwrites and the durability gate would then pass on someone else's
        bytes, which is the silent-overwrite class this closes. It is a BELT: when the
        caller's counter could see every file for this design the computed target does
        not exist and this never fires.

        Builds the name, calls ``save_as`` on the target, then applies the durability
        gate (``isfile AND getsize >= min``).
        A pass yields ``ok=True``; a failure or exception yields ``ok=False`` with
        an ``error`` string. The manifest row is written in both cases. If the
        manifest append itself fails, the audit row is lost: ``snapshot()`` still
        does not raise, but flips the result to ``ok=False`` with
        ``error="manifest_write_failed: ..."``, increments ``dropped_rows``, and
        routes a note to the logger/stderr so the caller can never miss it.
        """
        caller_named = filename is not None
        if caller_named != (index is not None):
            raise ValueError(
                "snapshot(index=, filename=) must be supplied TOGETHER or not at all"
            )

        design_part = None
        if caller_named:
            filename = os.path.basename(filename)
            design_part = self._design_part(filename, index, label)
            seq = index
        else:
            seq = self._seq
            filename = self._compose(label, seq, None, None)

        requested_index = seq
        index_advanced = False
        error = None
        # DECOMPOSABILITY IS A PRECONDITION, NOT A COLLISION HANDLER. Until this fix the
        # ``design_part is None`` refusal lived ONLY inside the advance loop below, so a
        # caller-supplied basename that does not carry its own
        # ``_{index:0Nd}_{label}.zmx`` suffix was ACCEPTED whenever the target happened
        # to be FREE. MEASURED by an audit:
        # ``snapshot("x", index=1, filename="not_our_convention.zmx")`` called
        # ``SaveAs`` and returned ``ok=True, error=None``. The naming contract is that such a
        # name cannot be DECOMPOSED and is refused rather than guessed at -- a property
        # of the name, not of what else is on disk. Checked here, before anything is
        # written, so the guarantee no longer depends on a collision to fire.
        # ``save_candidate`` always composes through ``candidate_zmx_name``, so this
        # refuses a FUTURE caller, never today's.
        # ``not design_part``, NOT ``is None`` [S-5]. The strip yields the EMPTY
        # STRING for a name like ``_001_x.zmx``: it DOES carry the suffix, so the
        # ``is None`` form accepted it and returned ``ok=True``. But the design it
        # decomposes to is the empty string, which ``_design_name_error`` refuses, so
        # no design can ever own that file -- a name nothing can promote, written and
        # certified. Same class as, one value over.
        if caller_named and not design_part:
            error = ("filename does not carry its own index suffix: "
                     f"{filename!r} cannot be decomposed into (design, index, label), "
                     f"so a free index cannot be recomposed for it")
        # Pre-write existence check. A name already on disk is never written through:
        # advance the index and recompose. Recomposition needs the design part, so a
        # caller-supplied name this call cannot decompose refuses instead of guessing.
        attempts = 0
        while error is None and os.path.exists(os.path.join(self.run_dir, filename)):
            if attempts >= self._MAX_INDEX_ADVANCE or (
                    caller_named and not design_part):
                error = (
                    f"target exists and no free index was found within "
                    f"{self._MAX_INDEX_ADVANCE} advances: {filename}"
                )
                break
            attempts += 1
            seq += 1
            index_advanced = True
            filename = self._compose(label, seq, filename if caller_named else None,
                                     design_part)

        target = os.path.join(self.run_dir, filename)

        ok = False
        size = 0

        if error is not None:
            row = {
                "schema_version": MANIFEST_SCHEMA_VERSION,
                "run_id": self.run_id,
                "seq": seq,
                "ts": _io.utc_now_iso(),
                "label": label,
                "filename": filename,
                "bytes": 0,
                "ok": False,
                "error": error,
                "meta": meta,
                "index_advanced": index_advanced,
                "requested_index": requested_index,
            }
            try:
                self._write_manifest_row(row)
            except Exception:  # noqa: BLE001 — never raise out of snapshot
                self.dropped_rows += 1
            if not caller_named:
                self._seq = seq
            return SnapshotResult(
                ok=False, path=target, bytes=0, seq=seq, label=label, error=error,
                index_advanced=index_advanced, requested_index=requested_index,
            )

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
            "index_advanced": index_advanced,
            "requested_index": requested_index,
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

        if not caller_named:
            # The advance loop may have moved PAST the instance counter; the counter
            # follows the name that was actually written, never the one that was not.
            self._seq = seq + 1
        return SnapshotResult(
            ok=ok, path=target, bytes=size, seq=seq, label=label, error=error,
            index_advanced=index_advanced, requested_index=requested_index,
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
