"""interaction_log.py — append-per-line JSONL log of agent⇄ZOS-API interactions.

Each interaction (an intent + a typed ZOS-API call + its result or exception) is
appended as one JSON object on its own line. The log is the durability substrate
for the training/audit corpus, so:

- ``result_repr`` is a STRING (via ``safe_repr``) so ``inf``/``nan`` from typed
  returns survive (probe A2) — JSON cannot hold a raw float ``inf``.
- the ``exception`` dict carries ``qualified_type = "{module}.{name}"`` so a
  ``builtins.TypeError`` (we called pythonnet wrong) is distinguishable from a
  ``System.ArgumentException`` (the engine rejected it), with CRLF normalized to
  LF (probe A3).
- ``log()`` and ``record_call()`` NEVER let a logging failure abort a run; they
  bump ``dropped_records`` and write a note to stderr instead.

Live ZOS-API integration: N/A this tier (no backend; the call/result/exception
shapes are grounded by the captured probe fixture, not a live connection).
"""
import dataclasses
import json
import sys
import time
import traceback as _tb
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Optional

from . import _io

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class InteractionRecord:
    """One appended interaction-log row."""

    seq: int
    ts: str
    intent: str
    call: str
    args: str
    result_repr: Optional[str] = None
    state_before: Optional[str] = None
    state_after: Optional[str] = None
    exception: Optional[dict] = None
    dur_ms: Optional[float] = None
    raw_output_file: Optional[str] = None
    schema_version: int = SCHEMA_VERSION


def _normalize(text: str) -> str:
    """Normalize CRLF (and lone CR) to LF (probe A3)."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _exc_to_dict(e: BaseException) -> dict:
    """Build the structured exception dict (probe A3).

    ``qualified_type`` joins ``type(e).__module__`` and ``type(e).__name__`` so
    the corpus distinguishes pythonnet interop errors (``builtins.*``) from
    wrapped .NET engine errors (``System.*``). ``message`` is the full ``str(e)``
    (multi-line for ``System.*`` remoting traces), ``message_head`` is its first
    line, and ``traceback`` is the joined ``format_exception`` — all CRLF→LF.
    """
    etype = type(e)
    module = getattr(etype, "__module__", "builtins")
    name = getattr(etype, "__name__", "?")
    message = _normalize(str(e))
    message_head = message.split("\n", 1)[0]
    tb_text = _normalize("".join(_tb.format_exception(etype, e, e.__traceback__)))
    return {
        "type": name,
        "qualified_type": f"{module}.{name}",
        "message": message,
        "message_head": message_head,
        "traceback": tb_text,
    }


class InteractionLog:
    """Append-per-line JSONL interaction log."""

    def __init__(self, path, *, fsync: bool = True):
        self.path = path
        self._fsync = fsync
        self._seq = 0
        self.dropped_records = 0

    def log(
        self,
        *,
        intent: str,
        call: str,
        args,
        result=None,
        state_before=None,
        state_after=None,
        exception: Optional[dict] = None,
        dur_ms: Optional[float] = None,
        raw_output_file: Optional[str] = None,
    ) -> Optional[InteractionRecord]:
        """Append one interaction row. NEVER raises.

        ``args``/``result``/``state_*`` are stringified via ``safe_repr`` so any
        object (incl. typed .NET handles, ``inf``) is JSON-safe. On any failure
        the row is dropped: a note is written to stderr, ``dropped_records`` is
        incremented, and ``None`` is returned.
        """
        try:
            seq = self._seq
            record = InteractionRecord(
                seq=seq,
                ts=_io.utc_now_iso(),
                intent=intent,
                call=call,
                args=_io.safe_repr(args),
                result_repr=None if result is None else _io.safe_repr(result),
                state_before=None if state_before is None else _io.safe_repr(state_before),
                state_after=None if state_after is None else _io.safe_repr(state_after),
                exception=exception,
                dur_ms=_io.safe_float(dur_ms),
                raw_output_file=raw_output_file,
            )
            line = json.dumps(asdict(record), ensure_ascii=False, allow_nan=False)
            self._write_line(line)
            self._seq += 1
            return record
        except Exception as exc:  # noqa: BLE001 — the substrate must not abort a run
            self.dropped_records += 1
            # The stderr note itself must never escape (a closed/broken stderr
            # under nohup/redirect/service supervision would otherwise re-raise
            # and break the NEVER-raises contract).
            try:
                print(
                    f"[interaction_log] dropped record (seq~{self._seq}): "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
            except Exception:  # noqa: BLE001 — never let the drop-note raise
                pass
            return None

    def _write_line(self, line: str) -> None:
        """Append one line, honoring this log's fsync setting."""
        prev = _io.FSYNC_ENABLED
        _io.FSYNC_ENABLED = self._fsync
        try:
            _io.append_line_fsync(self.path, line)
        finally:
            _io.FSYNC_ENABLED = prev

    @contextmanager
    def record_call(self, *, intent: str, call: str, args, state_before=None):
        """Time a block and log its result (or exception, then re-raise).

        Yields a mutable holder dict; set ``holder["result"]`` and
        ``holder["state_after"]`` inside the block. On normal exit the result is
        logged with the elapsed duration. On exception the exception dict is
        logged (the call still succeeds at the logging layer) and the original
        exception is RE-RAISED.
        """
        holder = {"result": None, "state_after": None}
        start = time.perf_counter()
        try:
            yield holder
        except BaseException as e:
            dur_ms = (time.perf_counter() - start) * 1000.0
            self.log(
                intent=intent,
                call=call,
                args=args,
                state_before=state_before,
                state_after=holder.get("state_after"),
                exception=_exc_to_dict(e),
                dur_ms=dur_ms,
            )
            raise
        else:
            dur_ms = (time.perf_counter() - start) * 1000.0
            self.log(
                intent=intent,
                call=call,
                args=args,
                result=holder.get("result"),
                state_before=state_before,
                state_after=holder.get("state_after"),
                dur_ms=dur_ms,
            )

    def write_raw_output(self, seq: int, data: bytes) -> str:
        """Durably write raw output bytes to a sidecar ``<stem>.raw.<seq>``.

        Uses ``atomic_write_bytes`` and MAY raise (opt-in, unlike ``log``).
        Returns the sidecar path.
        """
        import os

        path = os.fspath(self.path)
        stem, _ = os.path.splitext(path)
        sidecar = f"{stem}.raw.{seq}"
        prev = _io.FSYNC_ENABLED
        _io.FSYNC_ENABLED = self._fsync
        try:
            _io.atomic_write_bytes(sidecar, data)
        finally:
            _io.FSYNC_ENABLED = prev
        return sidecar


def load(path) -> list:
    """Load interaction records from a JSONL file.

    Skips a torn FINAL line only (a crash mid-append). A malformed
    non-final line raises ``json.JSONDecodeError`` (real corruption, not a
    crash-tail).
    """
    with open(path, "r", encoding="utf-8", newline="") as fh:
        lines = fh.read().split("\n")
    # A trailing newline yields a final empty element — drop it; it is not a row.
    if lines and lines[-1] == "":
        lines.pop()

    records = []
    last = len(lines) - 1
    for i, line in enumerate(lines):
        if line == "":
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            if i == last:
                break  # torn final line — tolerate the crash-tail
            raise
        # Forward-compat: a newer writer may add fields. Filter to the known
        # dataclass fields so an additive key from a future schema does not
        # raise TypeError. (A bumped schema_version is a separate concern handled
        # by the reader's version policy.)
        known = {f.name for f in dataclasses.fields(InteractionRecord)}
        records.append(InteractionRecord(**{k: v for k, v in obj.items() if k in known}))
    return records
