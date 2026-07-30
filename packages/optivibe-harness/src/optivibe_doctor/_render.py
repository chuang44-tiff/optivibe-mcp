"""The two renderers.

They are deliberately **independent**: the human renderer and the ndjson renderer each
walk their own record set and format it their own way.  A shared loop would make "both
renderers cover the same id set" true by construction and therefore unprovable — the
guard that says so has to be able to fail.

Both flush after every record.  A consumer reading the stream must be able to see the
first finding before the run completes; a buffered document is indistinguishable from a
hang.

Stdlib only, and no filesystem access: this module writes to a stream it is handed and
nothing else.
"""
import json
import textwrap

from ._contract import SCHEMA
from ._model import Status

#: Column 0 holds the 4-character status label, then two spaces, then the check id padded
#: to this width.  Continuation lines align under the message.
#:
#: The label is TRUNCATED to the column, so ``UNKNOWN`` prints as ``UNKN`` — the one status
#: whose human spelling is not its real name.  The machine stream is unaffected (it emits
#: ``"status": "unknown"`` in full), but a reader who greps the human stream for ``UNKNOWN``
#: gets silence and can conclude there were none.  That has already cost one reader a wrong
#: conclusion, so the legend below states it in the report itself: a stream that cannot be
#: searched for one of its own five verdicts is a reporting defect, not a layout detail.
_CHECK_WIDTH = 26
_STATUS_WIDTH = 4
_MESSAGE_COLUMN = _STATUS_WIDTH + 2 + _CHECK_WIDTH
_REMEDY_LABEL = "remedy: "
_REMEDY_COLUMN = 6 + len(_REMEDY_LABEL)
_LINE_WIDTH = 100

_HEADER = "optivibe doctor  schema=" + SCHEMA
#: The second header line, and deliberately part of the OUTPUT rather than a code comment:
#: the reader who needs it is reading the report, not the renderer.  It must not begin with
#: a status token, or the id-coverage guard would count it as a finding.
_LEGEND = ("status column is 4 chars: PASS WARN FAIL SKIP and UNKN, which is UNKNOWN "
           "truncated (the ndjson stream spells it in full)")


def _one_line(text):
    """Force a message onto a single line without disturbing its internal spacing.

    Messages are single-line by rule, and a traceback must never reach the report.  Runs
    of spaces are deliberately preserved: several summaries use them as column separators.
    """
    flattened = str(text or "")
    for control in ("\r\n", "\r", "\n", "\t", "\v", "\f"):
        flattened = flattened.replace(control, " ")
    return flattened.strip()


def _wrap(text, column):
    """Wrap ``text`` to the page width with continuation lines indented to ``column``."""
    width = max(_LINE_WIDTH - column, 20)
    pieces = textwrap.wrap(text, width=width, break_long_words=False,
                           break_on_hyphens=False) or [""]
    return [pieces[0]] + [" " * column + piece for piece in pieces[1:]]


def _message_of(finding):
    """The human-facing text for a finding.

    A SKIP with no message of its own prints its blocker, because a SKIP that does not say
    what blocked it is exactly the hole the SKIP invariant exists to close.
    """
    summary = _one_line(finding.summary)
    if summary:
        return summary
    if finding.status is Status.SKIP:
        return "blocked by %s" % (finding.blocked_by or "?",)
    return ""


def human_finding_lines(finding):
    """Return the human block for one finding as a list of lines."""
    check = str(finding.check)
    label = finding.status.name[:_STATUS_WIDTH].ljust(_STATUS_WIDTH)
    padded = check.ljust(_CHECK_WIDTH) if len(check) < _CHECK_WIDTH else check + " "
    body = _wrap(_message_of(finding), _MESSAGE_COLUMN)
    lines = ["%s  %s%s" % (label, padded, body[0])] + body[1:]
    remedy = str(finding.remedy or "")
    if remedy:
        first = True
        for segment in remedy.split("\n"):
            if segment[:1].isspace():
                # A literal command line: never re-wrapped, never re-indented away.
                lines.append(" " * _REMEDY_COLUMN + segment.strip())
                first = False
                continue
            wrapped = _wrap(_one_line(segment), _REMEDY_COLUMN)
            head = ("      " + _REMEDY_LABEL + wrapped[0]) if first else (
                " " * _REMEDY_COLUMN + wrapped[0])
            lines.append(head)
            lines.extend(wrapped[1:])
            first = False
    return lines


def human_summary_lines(summary):
    """Return the human summary block as a list of lines (a leading blank, then one line)."""
    counts = summary.counts
    return ["", "SUMMARY %s complete=%s pass=%d warn=%d fail=%d unknown=%d skip=%d  exit=%d" % (
        summary.state.value,
        "true" if summary.complete else "false",
        counts.get("pass", 0), counts.get("warn", 0), counts.get("fail", 0),
        counts.get("unknown", 0), counts.get("skip", 0), summary.exit_code)]


def _jsonable(value):
    """Last-resort coercion so a stray object can never break the NDJSON contract."""
    return repr(value)[:240]


def _dumps(payload):
    """Serialise one NDJSON record.  ``ensure_ascii`` is **True**, and load-bearing.

    The machine stream must stay parseable on the console doctor exists for.  With
    ``ensure_ascii=False`` a non-BMP character — an emoji in a vendor path or an exception
    message — reaches a ``cp437``/``cp850`` stdout, the stream's ``backslashreplace``
    fallback renders it ``\\U0001f4a5``, and that is **not** a JSON escape: JSON has only
    ``\\uXXXX``.  The consumer then gets a record it cannot parse, which is the em-dash
    finding again with the damage moved from the human report to the machine one.

    ``ensure_ascii=True`` emits the surrogate pair ``\\ud83d\\udca5`` instead — valid JSON,
    pure ASCII, so the fallback never fires at all.
    """
    return json.dumps(payload, default=_jsonable, ensure_ascii=True, separators=(",", ":"))


def ndjson_finding(finding, seq):
    """Return the single NDJSON line for one finding."""
    return _dumps({
        "schema": SCHEMA,
        "seq": seq,
        "type": "finding",
        "check": str(finding.check),
        "status": finding.status.value,
        "reason": str(finding.reason or ""),
        "summary": _one_line(finding.summary),
        "facts": dict(finding.facts or {}),
        "remedy": str(finding.remedy or ""),
        "blocked_by": str(finding.blocked_by or ""),
    })


def ndjson_summary(summary, seq):
    """Return the single NDJSON line for the run summary."""
    return _dumps({
        "schema": SCHEMA,
        "seq": seq,
        "type": "summary",
        "state": summary.state.value,
        "complete": bool(summary.complete),
        "expected": len(summary.expected),
        "received": len(summary.received),
        "counts": dict(summary.counts),
        "exit_code": summary.exit_code,
    })


class HumanRenderer:
    """Streams the human report.  One flushed block per finding, then one summary."""

    format_name = "human"

    def __init__(self, out):
        self._out = out
        self._seq = 0

    def _write(self, lines):
        for line in lines:
            self._out.write(line + "\n")
        self._out.flush()

    def start(self):
        self._write([_HEADER, _LEGEND])

    def finding(self, finding):
        self._seq += 1
        self._write(human_finding_lines(finding))

    def summary(self, summary):
        self._seq += 1
        self._write(human_summary_lines(summary))


class NdjsonRenderer:
    """Streams the machine report.  Nothing but one JSON object per line reaches stdout."""

    format_name = "ndjson"

    def __init__(self, out):
        self._out = out
        self._seq = 0

    def start(self):
        # The schema travels on every record, so the machine stream has no banner.
        return None

    def finding(self, finding):
        self._seq += 1
        self._out.write(ndjson_finding(finding, self._seq) + "\n")
        self._out.flush()

    def summary(self, summary):
        self._seq += 1
        self._out.write(ndjson_summary(summary, self._seq) + "\n")
        self._out.flush()


RENDERERS = {"human": HumanRenderer, "ndjson": NdjsonRenderer}


def make_renderer(fmt, out):
    """Return the renderer for ``fmt``.  An unknown format is a usage error upstream."""
    return RENDERERS[fmt](out)
