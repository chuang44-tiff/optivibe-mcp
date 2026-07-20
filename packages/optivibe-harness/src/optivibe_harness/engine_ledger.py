"""engine_ledger.py — persistent dead-parent-gated orphan reap.

Probe-grounded (BINDING). Claude Code's restart kill
is ``TerminateProcess``: it SKIPS the ``finally`` + ``atexit`` reap, so the
only way to reclaim a stranded OpticStudio seat is an on-disk record read at the NEXT
boot. This module owns that on-disk ledger, its cross-process lock, the dead-parent
gate, and the boot-reap orchestration.

POLICY 1 ONLY (this cycle): a persistent, dead-parent-gated boot reap of a hard-kill
orphan. Policy 2 (stale LIVE-session reclaim) is DEFERRED (it would kill
a live-parent engine — the exact L22 catastrophe the dead-parent gate exists to prevent
— and would ship inert/untestable). NO ``design_name`` field is recorded.

Reuses (L30 — the ONLY kill path / the ONLY atomic writer):
- ``process_reaper.terminate_tracked`` — every kill routes through it (its own baseline
  + create_time PID-reuse guards stay in force; the dead-parent gate is the only NEW
  predicate, and it is fail-safe in BOTH directions — AccessDenied -> alive -> spare;
  a torn/corrupt read -> empty -> no-op).
- ``_io.atomic_write_bytes`` — temp-in-same-dir -> fsync -> ``os.replace`` (never a torn
  file).

L22 EXTENDED, NOT WEAKENED: only engines THIS MCP family recorded, only when
the recording parent is provably gone, with the engine create_time PID-reuse guard;
never a name-sweep; never a kill on doubt. Every public function is best-effort and
NEVER raises — a ledger fault degrades to inaction, never a wrong kill or a failed
open/close/boot.

DURABILITY: a read FAULT (torn / non-dict top) or
an un-coercible individual row is NEVER destroyed by a subsequent write —
``_read_ledger_status`` distinguishes FAULT (do-not-rewrite) from EMPTY and carries
un-coercible rows VERBATIM so a rewrite re-emits them untouched; ``boot_reap`` is
TWO-PHASE so the multi-second ``terminate_tracked`` kill runs OUTSIDE the cross-process
lock (no concurrent-``record`` starvation); ``ledger_lock`` fail-safe-skips a
non-float ``timeout_s`` instead of raising; the FIFO bound never evicts a
live-parent survivor.

DEVIATION (forced by live Windows reality — see ``lock_path``): the spec §4.3 holds the
``msvcrt`` lock on the LEDGER file's OWN fd. On Windows ``os.replace`` then fails with
``WinError 5`` (the destination has our open handle). The lock is therefore held on a
companion ``<ledger>.lock`` file (locked, never replaced) while the data path is
atomically replaced — keeping every AXIS-2 non-negotiable (no deadlock; lock-timeout ->
fail-safe SKIP; OS auto-release on death) and NO stamp / remove-recreate stale-break.

Live ZOS-API integration exercises this end-to-end (a real hard-kill orphan is
reaped; a concurrent live engine survives untouched).
"""
import json
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass

import msvcrt
import psutil

from . import process_reaper
from ._io import atomic_write_bytes

# --- Location / constants -------------------------------------------------- #
LEDGER_FILENAME = "optivibe_engine_sessions.json"
LOCK_ACQUIRE_TIMEOUT_S = 2.0   # bounded; never deadlock boot (AXIS 2)
LOCK_POLL_INTERVAL_S = 0.05    # retry cadence under LK_NBLCK contention
LEDGER_MAX_RECORDS = 256       # FIFO file-size backstop (AXIS 3 pruning)


def ledger_path() -> str:
    """The fixed, process-independent ledger path.

    ``tempfile.gettempdir()/optivibe_engine_sessions.json`` — per-user-writable,
    survives a hard-kill (on disk, not in process memory), on the same volume as its
    own atomic-write temp sibling (so ``os.replace`` never crosses volumes). NOT under
    ``workspace_root`` (which can change per launch).
    """
    return os.path.join(tempfile.gettempdir(), LEDGER_FILENAME)


def lock_path() -> str:
    """The companion LOCK file path (``<ledger>.lock``) — locked, NEVER replaced.

    DEVIATION (forced by live Windows reality, see module note): the spec's §4.3 holds
    the byte-range lock on the LEDGER file's OWN fd and then ``os.replace``s a temp onto
    that same path. On Windows ``os.replace`` FAILS with ``WinError 5 (Access denied)``
    when the destination path still has an open handle (our lock fd) — proven live.
    The minimal fix that keeps every AXIS-2 non-negotiable
    (no deadlock on a dead holder — the OS auto-releases a byte-range lock on death; a
    lock-timeout -> fail-safe SKIP) AND the atomic temp-then-replace writer (L30) is to
    hold the lock on a SEPARATE, NEVER-replaced lock file while ``os.replace`` rebinds
    the data path unobstructed. This is NOT the cut "sidecar + 30 s stamp + remove/
    recreate stale-break" (there is NO stamp and NO remove/recreate — the lock file
    persists and the OS self-heals the lock); it is a pure rename-safety separation.
    """
    return ledger_path() + ".lock"


# --- Record dataclass ------------------------------------------------------ #
@dataclass(frozen=True)
class EngineRecord:
    """One ledger row: the (engine, parent) identity quad + an advisory timestamp.

    All four identity fields are load-bearing: ``engine_pid``+``engine_create_time``
    (the engine PID-reuse guard) and ``parent_pid``+``parent_create_time``
    (the dead-parent gate). ``recorded_at`` is ADVISORY — used ONLY for the
    FIFO size bound + the boot-sweep log; NEVER in a kill gate.
    """

    engine_pid: int
    engine_create_time: float
    parent_pid: int
    parent_create_time: float
    recorded_at: float


# --- Predicates (BINDING) -------------------------------------------------- #
def is_parent_dead(parent_pid: int, recorded_parent_ct: float) -> bool:
    """True iff the recording parent is PROVABLY gone (the dead-parent gate).

    - ``not pid_exists(parent)`` -> True (gone).
    - ``NoSuchProcess`` -> True (vanished between the two checks).
    - ``AccessDenied`` -> False (treat ALIVE — NEVER kill on doubt; non-negotiable).
    - else: ``live_create_time != recorded_parent_ct`` -> True (the PID was RECYCLED,
      so the original parent is dead and this is a genuine orphan).

    NEVER raises.
    """
    try:
        if not psutil.pid_exists(int(parent_pid)):
            return True
        live_ct = float(psutil.Process(int(parent_pid)).create_time())
    except psutil.NoSuchProcess:
        return True
    except psutil.AccessDenied:
        return False  # cannot verify -> treat ALIVE -> spare (never kill on doubt)
    except Exception:  # noqa: BLE001 — any unexpected psutil fault -> treat ALIVE (safe)
        return False
    return live_ct != float(recorded_parent_ct)


def engine_identity_ok(engine_pid: int, recorded_engine_ct: float) -> bool:
    """True iff the engine PID still exists AND its create_time matches.

    ``pid_exists`` AND ``Process(pid).create_time() == recorded_engine_ct``. A
    ``NoSuchProcess`` (gone) / ``AccessDenied`` (unverifiable) / a recycled-PID
    create_time mismatch -> False -> the record is PRUNED (dropped, never killed). The
    engine create_time guard mirrors ``terminate_tracked``'s own (a second, independent
    identity check downstream). NEVER raises.
    """
    try:
        if not psutil.pid_exists(int(engine_pid)):
            return False
        live_ct = float(psutil.Process(int(engine_pid)).create_time())
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    except Exception:  # noqa: BLE001 — cannot verify identity -> not eligible (prune)
        return False
    return live_ct == float(recorded_engine_ct)


# --- The cross-process LOCK (AXIS 2: msvcrt directly on the ledger fd) ------ #
@contextmanager
def ledger_lock(timeout_s: float = LOCK_ACQUIRE_TIMEOUT_S):
    """Bounded non-blocking exclusive lock via ``msvcrt.locking`` on the lock file (AXIS 2).

    Opens the companion LOCK file (``<ledger>.lock``, ``O_CREAT | O_RDWR``) and tries
    ``msvcrt.locking(LK_NBLCK)`` on byte 0 in a bounded ``timeout_s`` deadline loop.
    Yields ``True`` if the lock is HELD, ``False`` if it could NOT be acquired within
    ``timeout_s`` (the caller MUST check and fail-safe-SKIP on False) OR if the file
    could not be opened. NEVER raises out; always unlocks (LK_UNLCK) + closes the fd in
    ``finally`` (guarded).

    Windows auto-releases a byte-range lock when the holding process dies, so the lock
    self-heals on the exact hard-kill case with NO explicit stale-break. There
    is no unbounded blocking acquire anywhere -> never deadlock boot. The lock is held
    on ``lock_path()`` (never the ledger), so a concurrent writer's ``os.replace`` onto
    the ledger path is never blocked by our held handle (see ``lock_path`` for why).
    """
    fd = None
    held = False
    try:
        # Coerce timeout_s INSIDE the guarded block. A non-float/None arg must
        # NEVER raise out of the context-manager entry (the "NEVER raises out" contract);
        # a bad value -> fail-safe SKIP (yield False), exactly like a lock-timeout.
        try:
            timeout_val = max(0.0, float(timeout_s))
        except (TypeError, ValueError):
            yield False
            return

        try:
            fd = os.open(lock_path(), os.O_CREAT | os.O_RDWR)
        except OSError:
            # Unwritable temp dir / open failure -> fail-safe: do not touch the ledger.
            yield False
            return

        deadline = time.monotonic() + timeout_val
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                held = True
                break
            except OSError:
                # A peer holds byte 0. Retry until the deadline, then fail-safe skip.
                if time.monotonic() >= deadline:
                    break
                time.sleep(LOCK_POLL_INTERVAL_S)

        if not held:
            yield False
            return
        yield True
    finally:
        if held and fd is not None:
            try:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


# --- internal helpers (not public) ----------------------------------------- #
_REQUIRED_FIELDS = (
    "engine_pid",
    "engine_create_time",
    "parent_pid",
    "parent_create_time",
)


def _coerce_record(raw):
    """Build an ``EngineRecord`` from a raw dict, or return ``None`` if malformed.

    Tolerates an absent OR a present-but-unknown EXTRA key (forward-compat with a
    future ``design_name`` field): unknown keys are ignored. A missing required
    identity field or a non-numeric pid/ct -> ``None`` (the caller SKIPS this record).
    ``recorded_at`` is advisory; an absent/bad value defaults to ``0.0`` (never gates a
    kill). NEVER raises.
    """
    try:
        if not isinstance(raw, dict):
            return None
        for field in _REQUIRED_FIELDS:
            if field not in raw:
                return None
        engine_pid = int(raw["engine_pid"])
        engine_ct = float(raw["engine_create_time"])
        parent_pid = int(raw["parent_pid"])
        parent_ct = float(raw["parent_create_time"])
        try:
            recorded_at = float(raw.get("recorded_at", 0.0))
        except (TypeError, ValueError):
            recorded_at = 0.0
        return EngineRecord(
            engine_pid=engine_pid,
            engine_create_time=engine_ct,
            parent_pid=parent_pid,
            parent_create_time=parent_ct,
            recorded_at=recorded_at,
        )
    except (TypeError, ValueError):
        return None
    except Exception:  # noqa: BLE001 — a bad record is SKIPPED, never fatal
        return None


def _atomic_write(records: dict, raw_passthrough: dict = None) -> None:
    """Serialize ``{str(engine_pid): asdict(rec)}`` and atomic-write it.

    Always called UNDER ``ledger_lock`` after a fresh ``read_ledger`` merge (never a
    whole-file overwrite of unread peers). Routes the bytes through
    ``_io.atomic_write_bytes`` (L30 — the ONLY atomic writer).

    ``raw_passthrough`` is ``{str_key: raw_value}`` for rows that were on disk
    but could NOT be coerced into an ``EngineRecord`` (an un-coercible individual row).
    They are re-emitted VERBATIM so a malformed-but-possibly-LIVE row is NEVER destroyed
    by a rewrite (and never frozen out of all future writes). A passthrough key is only
    kept if it does NOT collide with a well-formed record's str-pid (a real record wins;
    the well-formed side is authoritative). Coerced records always win on collision.
    """
    payload_obj = {}
    if raw_passthrough:
        # Lay the salvaged raw rows down first; coerced records overwrite on collision.
        for key, val in raw_passthrough.items():
            payload_obj[str(key)] = val
    for rec in records.values():
        payload_obj[str(int(rec.engine_pid))] = asdict(rec)
    payload = json.dumps(payload_obj)
    atomic_write_bytes(ledger_path(), payload.encode("utf-8"))


def _fifo_bound(records: dict, max_records: int, protected_pids=None) -> dict:
    """Drop the OLDEST records (by ``recorded_at``) beyond ``max_records`` (AXIS 3).

    The FIFO size backstop against hard-killed-parent residue accumulating without
    bound. Keys are ``engine_pid``; ``recorded_at`` is the advisory FIFO ordering key
    (older = lower). Returns a new dict with the most-recent ``max_records`` kept.

    ``protected_pids`` (the live-parent / spared set) are NEVER evicted — a
    live-seat record must survive the bound so a later hard-kill of its parent stays
    reclaimable. Eviction happens ONLY among the non-protected residue (refused /
    other), oldest first. Protected records are always kept even past the cap (under
    N=1 the protected set self-cleans when each live process closes — see L26 note);
    the bound is best-effort, and never trades a live-seat record for an old dead one.
    """
    if max_records is None or len(records) <= max_records:
        return records
    protected = {int(p) for p in (protected_pids or ())}
    kept = {pid: rec for pid, rec in records.items() if int(pid) in protected}
    # Evict only among the non-protected, oldest-by-recorded_at first.
    evictable = [
        (pid, rec) for pid, rec in records.items() if int(pid) not in protected
    ]
    slots_left = max_records - len(kept)
    if slots_left <= 0:
        # The protected set alone already meets/exceeds the cap -> keep ONLY protected
        # (never drop a live-seat record to satisfy the bound).
        return kept
    ordered = sorted(evictable, key=lambda kv: kv[1].recorded_at)
    for pid, rec in ordered[len(ordered) - slots_left:]:
        kept[pid] = rec
    return kept


# --- Tolerant read / locked read-modify-write (FACTS 4, 8, 9, 10) ---------- #
def _read_ledger_status():
    """Read the ledger -> ``(records, raw_passthrough, fault)``. FAIL-SAFE. NEVER raises.

    The fault-vs-empty discriminator. Distinguishes a *fault* (the file existed
    but was UNPARSEABLE — ``JSONDecodeError`` / a non-dict top — so its on-disk bytes
    are unknown and MUST NOT be rewritten) from a true *empty* (missing file / empty
    object). Returns:

    - ``records``: ``{engine_pid(int): EngineRecord}`` of every WELL-FORMED row.
    - ``raw_passthrough``: ``{str_key: raw_value}`` of every row that PARSED as JSON but
      could NOT be coerced into an ``EngineRecord`` (an un-coercible individual row).
      These are carried verbatim so a subsequent ``_atomic_write`` re-emits them
      UNTOUCHED — a malformed-but-possibly-LIVE row is never destroyed, and one bad row
      never freezes all future writes. An un-coercible row is NEVER a kill candidate
      (it is not an ``EngineRecord``), so it can never reach a reap path.
    - ``fault``: ``True`` iff the file existed but the TOP level was unparseable
      (JSONDecodeError / non-dict). On a top-level fault, callers must NOT rewrite —
      degrade to inaction (the on-disk bytes are preserved as-is).

    A missing file -> ``({}, {}, False)`` (clean empty, safe to write a fresh ledger).
    A top-level fault -> ``({}, {}, True)`` (do NOT write). NEVER raises.
    """
    try:
        with open(ledger_path(), "rb") as fh:
            raw_bytes = fh.read()
    except FileNotFoundError:
        return {}, {}, False
    except OSError:
        # Cannot even read -> treat as a fault: do not rewrite over an unknown file.
        return {}, {}, True

    try:
        parsed = json.loads(raw_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}, {}, True  # torn / corrupt top -> FAULT -> no rewrite, no reap

    if not isinstance(parsed, dict):
        return {}, {}, True  # non-dict top -> FAULT -> no rewrite

    records = {}
    raw_passthrough = {}
    for key, raw in parsed.items():
        rec = _coerce_record(raw)
        if rec is None:
            # keep the un-coercible row VERBATIM (do not destroy, do not freeze).
            raw_passthrough[str(key)] = raw
            continue
        records[int(rec.engine_pid)] = rec
    return records, raw_passthrough, False


def read_ledger() -> dict:
    """Read the ledger -> ``{engine_pid(int): EngineRecord}``. FAIL-SAFE. NEVER raises.

    The public read-only view (returns just the well-formed records dict). Returns
    ``{}`` on a missing file, a ``JSONDecodeError`` (torn / half-written), or a non-dict
    top level. A malformed INDIVIDUAL record (missing a required identity field,
    non-numeric pid/ct) is SKIPPED from this view while well-formed siblings are kept.
    A bad ledger degrades to INACTION, never a wrong kill. Writers use
    ``_read_ledger_status`` (fault-aware, passthrough-preserving) — this view is for
    read-only callers/tests.
    """
    records, _raw, _fault = _read_ledger_status()
    return records


def record(rec: EngineRecord) -> None:
    """Persist ``rec`` (keyed read-modify-write). Best-effort; NEVER raises.

    Takes the cross-process lock, reads the current ledger, MERGES ``rec`` keyed by its
    ``engine_pid`` (so a concurrent peer's record is never dropped), and atomic-writes.
    A failed write means at worst a future MISSED reap, never a failed open.
    """
    try:
        with ledger_lock() as held:
            if not held:
                return  # could not lock -> skip (best-effort durability)
            current, raw_passthrough, fault = _read_ledger_status()
            if fault:
                # a torn / non-dict file -> do NOT rewrite (would destroy
                # unknown, possibly-LIVE bytes). Degrade to inaction: the new seat is
                # not persisted THIS round (best-effort; a future clean read re-records).
                return
            current[int(rec.engine_pid)] = rec
            _atomic_write(current, raw_passthrough)
    except Exception:  # noqa: BLE001 — recording is best-effort; never break the open
        pass


def unrecord(engine_pids) -> None:
    """Drop each PID in ``engine_pids`` from the ledger. Idempotent. NEVER raises.

    Keyed read-modify-write under the lock (drops only the named keys; peers survive).
    Idempotent on an absent key. A clean close calls this so a clean restart finds
    nothing to reclaim (hygiene, not a correctness dependency — the boot gate would
    refuse a dead engine via ``engine_identity_ok`` anyway).
    """
    try:
        pids = {int(p) for p in engine_pids}
    except (TypeError, ValueError):
        return
    if not pids:
        return
    try:
        with ledger_lock() as held:
            if not held:
                return
            current, raw_passthrough, fault = _read_ledger_status()
            if fault:
                # torn / non-dict file -> do NOT rewrite (preserve unknown
                # bytes). The unrecord is hygiene; skip this pass (the boot gate would
                # refuse a dead engine via engine_identity_ok anyway).
                return
            changed = False
            for pid in pids:
                if pid in current:
                    del current[pid]
                    changed = True
            # Always rewrite when there is salvaged raw passthrough to re-emit (so a
            # malformed sibling is never silently dropped on a subsequent clean write),
            # else only when a key was actually removed.
            if changed or raw_passthrough:
                _atomic_write(current, raw_passthrough)
    except Exception:  # noqa: BLE001 — unrecord is best-effort hygiene; never raise
        pass


# --- Boot reap orchestration (§5) ------------------------------------------ #
@dataclass(frozen=True)
class BootReapReport:
    """The outcome of one boot sweep (all advisory; surfaced via the boot-sweep log)."""

    reaped: list                 # engine PIDs killed (dead-parent orphans)
    spared_live_parent: list     # live-parent records, kept untouched
    pruned: list                 # engine gone/recycled -> dropped from the ledger
    refused: list                # [(engine_pid, action)] eligible-but-kill-refused (kept)
    lock_skipped: bool           # could not take the lock -> whole pass skipped (fail-safe)
    read_faulted: bool = False   # torn/non-dict ledger -> ZERO work, file preserved


def boot_reap() -> BootReapReport:
    """The single dead-parent-gated boot pass (§5). Runs BEFORE the first engine open.

    TWO-PHASE (the kill must NOT hold the cross-process lock, or a concurrent
    ``record()`` of a new live seat is starved into silent loss):

    - **Phase A (UNDER the lock):** read + classify every record into prune / reap-
      candidate / spare; gather the live-parent (spared) set; capture any salvaged raw
      passthrough; RELEASE the lock. NO kill runs here. A torn/non-dict file (FAULT)
      returns ``read_faulted=True`` and does ZERO work (the on-disk bytes are
      preserved). A lock-timeout returns ``lock_skipped=True`` (fail-safe).
    - **Phase B (NO lock):** run ``terminate_tracked`` (up to ~5+5 s on a stuck orphan,
      L30) on each reap-candidate OUTSIDE the lock, so a concurrent ``record()`` is never
      blocked.
    - **Phase C (RE-acquire the lock briefly):** RE-READ the ledger (a concurrent
      ``record()`` may have landed between A and C — it MUST be merged, never clobbered);
      drop the successfully-reaped + pruned PIDs **IDENTITY-AWARE** (a reaped/pruned pid is
      dropped ONLY when the re-read row's ``engine_create_time`` MATCHES the identity we
      actually handled in A/B — a create_time MISMATCH means the OS recycled the pid for a
      BRAND-NEW live engine that a concurrent ``record()`` landed, so KEEP it; NEW-HIGH R3),
      KEEP refused, KEEP newly-recorded rows, exempt live-parent records from the FIFO bound,
      write survivors. A FAULT or lock-timeout on the re-read -> skip the write
      (preserve the bytes; the kills in B already happened and are idempotent next boot).

    The kill conjunction (§5 / §7) is UNCHANGED: a kill fires iff in-ledger ∧
    ``engine_identity_ok`` ∧ parent provably dead ∧ ``terminate_tracked.ok``. Captures
    NOTHING about "own design" (policy-1 only). NEVER raises — an internal fault returns
    a best-effort report.
    """
    reaped, spared, pruned, refused = [], [], [], []
    # NEW-HIGH R3 (L30 — identity is always (pid, create_time), never pid alone): the
    # IDENTITY of every record we actually prune/reap in Phase A/B, keyed by engine_pid ->
    # the engine_create_time we handled. Phase C drops a reaped/pruned pid ONLY when the
    # re-read row's create_time MATCHES this — a recycled pid naming a fresh live engine
    # (different create_time) is KEPT, never destroyed (a stranded-seat durability gap).
    handled_create_time = {}
    try:
        # -- Phase A: classify under the lock; collect candidates; RELEASE the lock. --
        with ledger_lock(timeout_s=LOCK_ACQUIRE_TIMEOUT_S) as held:
            if not held:
                return BootReapReport(
                    reaped=[], spared_live_parent=[], pruned=[], refused=[],
                    lock_skipped=True,
                )

            records, _raw_a, fault_a = _read_ledger_status()
            if fault_a:
                # torn/non-dict ledger -> ZERO work, do NOT rewrite (preserve
                # the unknown, possibly-LIVE bytes). Like lock_skipped: fail-safe.
                return BootReapReport(
                    reaped=[], spared_live_parent=[], pruned=[], refused=[],
                    lock_skipped=False, read_faulted=True,
                )

            reap_candidates = []   # [(engine_pid, rec)] dead-parent + identity-OK
            for engine_pid, rec in records.items():
                # 5a. PRUNE a gone/recycled engine FIRST — never act on a
                #     record whose engine identity cannot be verified as the SAME
                #     process. A recycled PID / AccessDenied / gone -> drop, no reap.
                if not engine_identity_ok(rec.engine_pid, rec.engine_create_time):
                    pruned.append(engine_pid)
                    # NEW-HIGH R3: remember the IDENTITY we pruned (pid, create_time), so a
                    # recycled pid (different create_time) re-recorded by Phase C survives.
                    handled_create_time[engine_pid] = float(rec.engine_create_time)
                    continue
                # 5b. DEAD-PARENT GATE (the core orphan reap) — CLASSIFY only.
                if is_parent_dead(rec.parent_pid, rec.parent_create_time):
                    reap_candidates.append((engine_pid, rec))
                    # NEW-HIGH R3: remember the IDENTITY we will reap (pid, create_time).
                    handled_create_time[engine_pid] = float(rec.engine_create_time)
                    continue
                # 5c. LIVE-PARENT (a concurrent session) -> SPARE.
                spared.append(engine_pid)
            # Lock released here (Phase A done) — the slow kill runs OUTSIDE it.

        # -- Phase B: run the kills OUTSIDE the lock. --
        for engine_pid, rec in reap_candidates:
            result = process_reaper.terminate_tracked(
                rec.engine_pid, rec.engine_create_time, baseline_pids=set()
            )  # L30 — the ONLY kill path; its own create_time guard re-checks
            if result is not None and getattr(result, "ok", False):
                reaped.append(engine_pid)  # orphan reclaimed -> DROP record
            else:
                action = getattr(result, "action", "unknown")
                refused.append((engine_pid, action))  # could not prove safe -> KEEP

        # -- Phase C: re-acquire briefly; RE-READ + merge; write survivors. --
        # refused + live-parent records are dropped from NEITHER set below, so they
        # survive the merge naturally (kept in the fresh `current` read).
        reaped_set = set(reaped)
        pruned_set = set(pruned)
        spared_set = set(spared)
        with ledger_lock(timeout_s=LOCK_ACQUIRE_TIMEOUT_S) as held_c:
            if not held_c:
                # Could not re-lock to rewrite -> leave the on-disk ledger as-is. The
                # kills in B already happened; the reaped rows are stale but harmless
                # (engine gone -> pruned next boot, idempotent).
                return BootReapReport(
                    reaped=reaped, spared_live_parent=spared, pruned=pruned,
                    refused=refused, lock_skipped=False,
                )
            current, raw_passthrough, fault_c = _read_ledger_status()
            if fault_c:
                # the ledger became torn/non-dict between A and C -> do NOT
                # rewrite (preserve the unknown bytes). The kills already happened.
                return BootReapReport(
                    reaped=reaped, spared_live_parent=spared, pruned=pruned,
                    refused=refused, lock_skipped=False, read_faulted=True,
                )
            # Merge the FRESH ledger (a concurrent record() between A and C survives):
            # drop reaped + pruned; KEEP refused, KEEP live-parent, KEEP newly-recorded.
            #
            # NEW-HIGH R3 (IDENTITY-AWARE drop): a reaped/pruned pid is dropped ONLY when the
            # re-read row's engine_create_time MATCHES the identity we handled in A/B. If the
            # OS recycled that pid for a BRAND-NEW live engine (different create_time) and a
            # concurrent record() landed it between A and C, the create_times MISMATCH -> KEEP
            # the fresh live-seat row (dropping it would strand its seat — a MISSED reap). The
            # comparison is exact == on psutil create_time values round-tripped through JSON,
            # the same identity discipline as engine_identity_ok / terminate_tracked (L30).
            handled_set = reaped_set | pruned_set
            survivors = {}
            for pid, rec in current.items():
                if pid in handled_set and (
                    float(rec.engine_create_time)
                    == handled_create_time.get(pid, rec.engine_create_time)
                ):
                    continue  # the SAME identity we reaped/pruned -> drop (orphan gone)
                survivors[pid] = rec  # fresh/recycled identity OR untouched row -> KEEP
            # the FIFO bound NEVER evicts a live-parent (spared) survivor.
            survivors = _fifo_bound(
                survivors, LEDGER_MAX_RECORDS, protected_pids=spared_set
            )
            _atomic_write(survivors, raw_passthrough)
    except Exception:  # noqa: BLE001 — boot reap is best-effort; never block boot
        return BootReapReport(
            reaped=reaped, spared_live_parent=spared, pruned=pruned,
            refused=refused, lock_skipped=False,
        )

    return BootReapReport(
        reaped=reaped, spared_live_parent=spared, pruned=pruned,
        refused=refused, lock_skipped=False,
    )
