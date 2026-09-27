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
- the line is serialized through ``_json_line``, the WRITE BOUNDARY: it guarantees
  the line is utf-8-encodable, so a LONE SURROGATE in a RAW client-controlled
  ``client.name``/``client.version`` cannot raise inside the never-raise net and
  void every row of that client's session. Rows that were writable before are
  byte-identical to before; only an otherwise-unwritable row is re-dumped ASCII-
  escaped, which changes representation and not value.
- every row carries ``client`` — the live MCP client's identity, resolved PER ROW
  INSIDE ``log()``'s existing ``try`` from mcp's module-level ``request_ctx``
  ContextVar. Exactly four keys (``CLIENT_FIELD_KEYS``); ``status`` is one of
  ``CLIENT_STATUS_TOKENS``: ``"present"`` (identity read from the live session),
  ``"unavailable"`` (the read ran; the session carries no client identity),
  ``"no_request_context"`` (no MCP request context on THIS thread/task — a Timer
  thread, teardown, a non-MCP in-process caller), ``"degraded"`` (the read itself
  failed; ``detail`` is a qualified exception type or ``"name_not_str"``).
  ``name``/``version`` are the client's RAW bytes — never cased, slugged, matched
  or allow-listed — bounded to a 256-char PREFIX. The FACT of a cut is disclosed
  OUT-OF-BAND in ``detail`` (exactly one of ``"truncated:name"`` /
  ``"truncated:version"`` / ``"truncated:name,version"``), never in-band in the
  client-controlled string, so a forged ``...[truncated N chars]`` tail in a name
  lands verbatim with ``detail: null`` and is distinguishable from a real cut.
  ``client is None`` after ``load()`` means the line had no such key — a v1 row.
- every row carries ``coverage`` — the constant ``COVERAGE_NOTE``
  (``"reference_dispatches:lower_bound"``). Its two causes are both set by
  axis 11: (1) reference-only sessions write no ledger at all; (2) reference
  dispatches made before the first dispatchable harness call are neither recorded
  nor counted. ANY reference-dispatch count derived from this ledger is therefore
  a LOWER BOUND over an UNMEASURED denominator. No row of this ledger — of any
  kind — states completeness, and the ABSENCE of the token (a v1 row) is NOT a
  completeness statement either; there is no backfill.
- reference rows are discriminated by their ``call`` suffix ``(conn, params)`` —
  a true statement of the reference arg-0 contract. Harness rows end
  ``(session, params)``.

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

SCHEMA_VERSION = 2

# ---- client identity (Half 1) -------------------------------------------------
CLIENT_STATUS_TOKENS = ("present", "unavailable", "no_request_context", "degraded")
CLIENT_FIELD_KEYS = ("status", "name", "version", "detail")
_CLIENT_TEXT_LIMIT = 256

# ---- coverage qualification (Half 2, axis 11) ---------------------------------
COVERAGE_NOTE = "reference_dispatches:lower_bound"
# Written VERBATIM on every row. Meaning (the two causes, both set by axis 11):
#   (1) reference-only sessions write no ledger at all;
#   (2) reference dispatches made before the first dispatchable harness call are neither
#       recorded nor counted.
# Therefore ANY reference-dispatch count derived from this ledger is a lower bound over an
# UNMEASURED denominator. No row of this ledger — of any kind — states completeness.


def _cap(text: str) -> tuple:
    """Return ``(text, False)`` if it fits ``_CLIENT_TEXT_LIMIT``, else a PREFIX + ``True``.

    A PREFIX of the raw bytes — nothing is inserted, nothing is rewritten. The truncation
    FACT is reported out-of-band in ``detail`` (an OURS field), never in-band in the
    client-controlled string (axis 5; qwen Q4). The boundary is ``>``, not ``>=``: a value
    of EXACTLY ``_CLIENT_TEXT_LIMIT`` chars is untruncated.
    """
    if len(text) <= _CLIENT_TEXT_LIMIT:
        return text, False
    return text[:_CLIENT_TEXT_LIMIT], True


def _json_line(payload: dict) -> str:
    """Serialise one row to a line GUARANTEED to be encodable as utf-8.

    THE WRITE BOUNDARY. ``_io.append_line_fsync`` opens the ledger with
    ``encoding="utf-8"`` and the default STRICT error handler, so a ``str`` holding a
    LONE SURROGATE (U+D800-U+DFFF, which utf-8 cannot represent) raises
    ``UnicodeEncodeError`` there — INSIDE ``log()``'s never-raise net. The row is then
    silently dropped and, because the client identity is re-read PER ROW, every
    subsequent row of that client's session dies identically: a 0-byte ledger that no
    reader can tell apart from an idle session, with the only signal an in-memory
    counter and a stderr note that production redirects into the stdio log.

    That is REACHABLE: JSON permits an unpaired ``\\ud800`` escape and Python's decoder
    yields a lone-surrogate ``str``, so a client can send one in ``initialize``. It is
    also NEW: ``client.name``/``client.version`` are the first RAW client-controlled
    strings written to the line; every other string field goes through
    ``_io.safe_repr``, and ``repr()`` escapes lone surrogates.

    The fix, and its price, stated:

    - The first dump keeps ``ensure_ascii=False``, so every row that was writable
      before is byte-identical to before. Key order is untouched and historical ledgers
      stay byte-comparable. Flipping ``ensure_ascii`` GLOBALLY would instead re-encode
      every non-ASCII byte of every row; this does not.
    - The cost is one extra ``str.encode("utf-8")`` per row — the same work the file
      layer is about to do anyway, used here as the ORACLE so this answer cannot
      disagree with the one the append will give.
    - ONLY a row that would otherwise be UNWRITABLE is re-dumped with
      ``ensure_ascii=True``. That changes the REPRESENTATION, not the value: the lone
      surrogate is emitted as its ASCII escape and ``json.loads`` restores the
      identical ``str``, so ``load()`` hands back the client's bytes exactly (pinned by
      a round-trip assertion). Nothing WE author changes either — so there is
      nothing for the ours-authored ``detail`` field to disclose (axis 4/5): ``detail``
      keeps its closed 3-value truncation vocabulary on ``present`` rows, and no fifth
      ``status`` token is invented.

    ``str.encode("utf-8")`` raises ONLY ``UnicodeEncodeError``, and an ASCII-only dump
    is unconditionally encodable, so this is TOTAL over any row ``json.dumps`` accepts.
    """
    line = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    try:
        line.encode("utf-8")
    except UnicodeEncodeError:
        line = json.dumps(payload, ensure_ascii=True, allow_nan=False)
    return line


def _client_blank(status: str, detail: Optional[str] = None) -> dict:
    """The canonical 4-key dict for every non-``present`` outcome.

    ``{"status": status, "name": None, "version": None, "detail": detail}`` — never a
    plausible client name on failure (axis 4), so "row predates recording" and "client
    unresolved" stay distinguishable.
    """
    return {"status": status, "name": None, "version": None, "detail": detail}


def _read_client() -> dict:
    """Return the live MCP client's identity as a 4-key dict. TOTAL on ``Exception``.

    NEVER returns None. NEVER raises ``Exception`` (``KeyboardInterrupt`` / ``SystemExit``
    PROPAGATE — the repo-wide asymmetry: a deliberate abort is never swallowed). NEVER
    returns a plausible client name on failure (axis 4). Values are RAW (axis 5): no
    casing, slugging, matching or allow-list; the only transformation is a length-bounded
    PREFIX, disclosed in ``detail``.

    Resolution order (every step inside this function's single ``try``):

    1. ``from mcp.server.lowlevel.server import request_ctx`` — IN-FUNCTION and lazy, so
       importing this module never imports ``mcp`` (the boundary C16 pins);
    2. ``request_ctx.get()`` — ``LookupError`` -> ``no_request_context``;
    3. ``ctx.session.client_params`` is None, or its ``clientInfo`` is None ->
       ``unavailable`` (a rejected ``initialize``; also ``stateless=True``);
    4. ``clientInfo.name`` not a ``str`` -> ``degraded`` / ``"name_not_str"``;
    5. cap both values; ``detail`` is None, or exactly one of ``"truncated:name"`` /
       ``"truncated:version"`` / ``"truncated:name,version"``; status ``present``.

    Any other ``Exception`` anywhere (incl. ``ImportError`` / ``ModuleNotFoundError`` at
    step 1) -> ``degraded`` with the qualified exception type as ``detail``.
    """
    try:
        from mcp.server.lowlevel.server import request_ctx

        try:
            ctx = request_ctx.get()
        except LookupError:
            return _client_blank("no_request_context")
        params = ctx.session.client_params
        if params is None or params.clientInfo is None:
            return _client_blank("unavailable")
        ci = params.clientInfo
        if not isinstance(ci.name, str):
            return _client_blank("degraded", "name_not_str")
        name, tn = _cap(ci.name)
        version, tv = _cap(ci.version) if isinstance(ci.version, str) else (None, False)
        cut = [k for k, t in (("name", tn), ("version", tv)) if t]
        detail = ("truncated:" + ",".join(cut)) if cut else None
        return {"status": "present", "name": name, "version": version, "detail": detail}
    except Exception as exc:  # noqa: BLE001 — TOTAL: the reader must never abort a row
        return _client_blank("degraded", f"{type(exc).__module__}.{type(exc).__name__}")


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
    # Declared LAST, in this order (door 5). The defaults are MANDATORY (axis 6 / door 1):
    # without them ``load()``'s ``InteractionRecord(**filtered)`` raises TypeError on line 1
    # of every historical ledger. ``None`` after ``load()`` == "the line had no such key"
    # == a v1 row. A v2 writer NEVER writes ``None`` for either.
    client: Optional[dict] = None
    coverage: Optional[str] = None


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
                # axis 3: the identity read sits INSIDE this ``try``. Hoisted above it, a
                # reader that raised would bypass the never-raise net entirely — the row
                # would be lost with dropped_records still 0 and the AttributeError would
                # escape as error_family "internal" on a call that otherwise succeeded.
                client=_read_client(),
                coverage=COVERAGE_NOTE,
            )
            # The WRITE BOUNDARY (see ``_json_line``): a lone surrogate in the RAW
            # client-controlled name/version would otherwise raise UnicodeEncodeError
            # in the utf-8 append — inside THIS ``try`` — and silently void every row
            # of that client's session.
            line = _json_line(asdict(record))
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
        # raise TypeError.
        #
        # VERSION POLICY — STATED, and deliberately ZERO-CHECK: this reader accepts
        # EVERY ``schema_version`` (older, equal, newer) and filters to the fields it
        # knows. Do NOT paste the sibling ``journal.py``'s version guard here: it raises
        # on any ``!=`` — OLDER INCLUDED — so copying it while SCHEMA_VERSION is 2 would
        # make every historical v1 row in the corpus unreadable. The absence of a check
        # is the policy, not an omission; it is pinned in BOTH directions (a v1 row and
        # a v99 row must both load).
        known = {f.name for f in dataclasses.fields(InteractionRecord)}
        records.append(InteractionRecord(**{k: v for k, v in obj.items() if k in known}))
    return records
