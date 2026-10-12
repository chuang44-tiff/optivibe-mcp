"""Shared PNG publish chain for the analysis plots.

A PARAMETERISED COPY of ``analysis_mtf_field._publish`` + ``_publish_exclusive`` and
``_mtf_field_plot.render_png`` (declared debt): the figure
is built by the caller's ``build_fn(model) -> (fig, plt)`` and the minted name is
``<stem>_%04d.png``. The MTF modules are imported from, never edited. A minted file is created
``O_EXCL`` and never unlinked unless this call created it; an explicit path is the caller's pathname,
disclosed (``overwrote``) and never unlinked.

``write_data_sidecar`` writes the SUCCESS envelope as ``<png stem>.data.json``
beside the PNG, AFTER the PNG, and is the LAST thing that touches the envelope.
"""
import hashlib
import json
import os
import re
import secrets
import shutil
import sys

from .._io import safe_exc
from ..artifact_naming import next_index_in_dir
from ..artifact_sink import _safe_name
from ._image_gate import _is_png
from ._mtf_field_plot import _close, exclusive_temp

MAX_MINT_ATTEMPTS = 3
_RMTREE_HOOK = "onexc" if sys.version_info >= (3, 12) else "onerror"


def _stem_of(stem):
    return _safe_name(stem)


def _index_reader(stem):
    """Reads EXACTLY the names the formatter writes for ``1 <= k < 10**9`` (``%04d``: 4..9 digits),
    case-insensitive; any other digit run is a foreign name (``None``)."""
    rx = re.compile(r"^%s_(\d{4,9})\.png$" % re.escape(stem), re.IGNORECASE)

    def read(name):
        m = rx.match(name)
        return int(m.group(1)) if m else None
    return read


#: what happens to a file that was published and then failed a later stage -- chosen ONCE per route
_LEFT_EXPLICIT = "the file was LEFT at the caller's path"
_LEFT_MINTED = ("the file sits in a private render directory that publish then removes (a removal failure is "
                "listed in cleanup_failures); nothing was published")


def render_png(build_fn, model, out_path, cleanup=None, minted=False):
    """``(path, sha256, None, None)`` | ``(None, None, fault, published)``; NEVER raises.

    ``minted``: ``out_path`` is inside the private render directory ``publish`` removes afterwards, so a
    post-publication fault must NOT claim the file was left anywhere (one sentence, composed here)."""
    fig = plt = tmp = published = stage = None
    left = _LEFT_MINTED if minted else _LEFT_EXPLICIT
    try:
        fig, plt = build_fn(model)
        tmp = exclusive_temp(os.path.dirname(os.path.abspath(out_path)))
        fig.savefig(tmp, dpi=120, format="png", bbox_inches="tight")
        _close(plt, fig)
        if not _is_png(tmp):
            return None, None, "render_failed: the rendered temp failed the PNG magic-byte gate", None
        os.replace(tmp, out_path)
        tmp, published, stage = None, out_path, "re-gate"
        if not _is_png(out_path):
            return None, None, ("render_failed: the published file failed the PNG magic-byte re-gate; "
                                + (left if minted else left + " (not verified)")), published
        stage = "digest read (re-gate PASSED)"
        with open(out_path, "rb") as fh:
            return out_path, hashlib.sha256(fh.read()).hexdigest(), None, None
    except Exception as exc:  # noqa: BLE001 — every fault is a render_failed tuple
        if cleanup is not None and getattr(exc, "left_in_place", None):
            cleanup.append(exc.left_in_place)
        return None, None, f"render_failed: {safe_exc(exc, repr_form=True)}" + (
            f" after publication at stage {stage}; {left}"
            if published else ""), published
    finally:
        _close(plt, fig)
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:  # a failed reap is DISCLOSED to the caller, never swallowed
                if cleanup is not None:
                    cleanup.append(tmp)


def _publish_exclusive(tmp_png, out_dir, stem):
    """Copy an ``_is_png``-gated temp into a freshly MINTED name via ``O_CREAT | O_EXCL``.

    A successful open is the OWNERSHIP TOKEN: nothing here unlinks a path this call did not create.
    ``FileExistsError`` -> foreign, never touched, re-mint (<= 3). Returns
    ``(final, sha256, fault, cleanup)``; ``fault`` = ``(family, message)``."""
    with open(tmp_png, "rb") as fh:
        data = fh.read()
    reader = _index_reader(stem)
    for _ in range(MAX_MINT_ATTEMPTS):
        index = next_index_in_dir(out_dir, reader)
        if index is None:
            return None, None, ("workspace_unlistable", f"{out_dir!r} could not be listed"), []
        if index >= 10 ** 9:
            return None, None, ("workspace_unlistable", "the minted-index space is exhausted (a "
                                f"foreign {stem}_<9 digits>.png sits in the output directory); "
                                "pass an explicit path"), []
        final = os.path.join(out_dir, "%s_%04d.png" % (stem, index))
        try:
            fd = os.open(final, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0))
        except FileExistsError:
            continue
        except OSError as exc:
            return None, None, ("render_failed", f"exclusive open of {final!r} failed: "
                                f"{safe_exc(exc)}"), []
        fault, close_fault, cleanup = None, None, []
        try:
            try:
                view = memoryview(data)
                while view:
                    n = os.write(fd, view)
                    if n <= 0:
                        raise OSError(f"zero-progress write into {final!r} (os.write returned {n!r})")
                    view = view[n:]
                os.fsync(fd)
            finally:
                try:
                    os.close(fd)
                except Exception as exc:  # noqa: BLE001 — recorded, never replaces
                    cleanup.append("fd:" + final)
                    close_fault = f"close of {final!r} failed: {safe_exc(exc, repr_form=True)}"
        except Exception as exc:  # noqa: BLE001 — an OWNED file is unlinked
            fault = f"copy into {final!r} failed: {safe_exc(exc, repr_form=True)}"
        fault = fault or close_fault  # the ORIGINAL fault wins by construction
        if fault is None:
            try:  # re-gate + digest while the file is still this call's own
                if not _is_png(final):
                    fault = f"the published {final!r} failed the PNG magic-byte re-gate"
                else:
                    with open(final, "rb") as fh:
                        return final, hashlib.sha256(fh.read()).hexdigest(), None, cleanup
            except Exception as exc:  # noqa: BLE001
                fault = f"re-gate/digest of {final!r} failed: {safe_exc(exc, repr_form=True)}"
        try:
            os.unlink(final)
        except Exception:  # noqa: BLE001 — the owned final is NAMED, never lost
            cleanup.append(final)
        return None, None, ("render_failed", fault), cleanup
    return None, None, ("workspace_unlistable", f"{MAX_MINT_ATTEMPTS} consecutive name "
                        "collisions in the output directory; nothing was published"), []


def publish(build_fn, model, target, minted, stem):
    """``(final, sha256, overwrote, fault, cleanup_failures)``: render, gate, publish.

    ``minted``: ``target`` is a DIRECTORY and the name is ``<stem>_NNNN.png`` (exclusive create).
    Otherwise ``target`` is the caller's ``.png`` pathname (disclosed overwrite, never unlinked)."""
    stem = _stem_of(stem)
    if not minted:
        overwrote, cleanup = os.path.exists(target), []
        final, sha, fault, published = render_png(build_fn, model, target, cleanup=cleanup)
        return final, sha, overwrote, (("render_failed", fault, published) if fault else None), cleanup
    try:  # BOUNDED: an ACL-denied folder returns this fault, never a retry loop
        tmp_dir = exclusive_temp(target, mkdir=True)
    except OSError as exc:
        return None, None, None, ("render_failed", f"no private render directory: {safe_exc(exc)}"), []
    cleanup = []
    try:
        tmp = os.path.join(tmp_dir, "render.png")
        fault = render_png(build_fn, model, tmp, cleanup=cleanup, minted=True)[2]
        if fault or not _is_png(tmp):
            return None, None, None, ("render_failed", fault or "temp failed the PNG gate"), cleanup
        final, sha, fault, owned = _publish_exclusive(tmp, target, stem)
        cleanup += owned
        return final, sha, None, fault, cleanup
    except Exception as exc:  # noqa: BLE001 — the fault RETURNS with the SAME list the finally appends to
        return None, None, None, ("render_failed", safe_exc(exc, repr_form=True)), cleanup
    finally:
        failed = []
        shutil.rmtree(tmp_dir, **{_RMTREE_HOOK: lambda *_: failed.append(1)})
        if failed:
            cleanup.append(tmp_dir)
        cleanup[:] = [p for p in cleanup if not p.startswith(tmp_dir) or os.path.exists(p)]


# --------------------------------------------------------------------------- #
# The data sidecar (owner rulings)
# --------------------------------------------------------------------------- #
#: the CLOSED set of reason tokens in ``data_sidecar_failed:<token>`` -- never exception text
SIDECAR_REASONS = ("not_serialisable", "exists", "link_unsupported", "write_error")
SIDECAR_SUFFIX = ".data.json"

# The sidecar's OWN seams: a test that fails the sidecar patches THESE, so the PNG (which uses
# ``os.replace`` / ``exclusive_temp`` / ``_is_png`` above) is never what fails first.
SIDECAR_TMP_PREFIX = ".data_sidecar_tmp_"


def _sc_create_temp(d, data):
    """A private temp in ``d`` holding ``data``, written THROUGH the descriptor the exclusive create returned (no
    close-and-reopen by name, so nothing can be swapped in between the create and the write). Returns its path.
    Three attempts: ``FileExistsError`` moves on, any other ``OSError`` stops at once (``tempfile.mkstemp`` would
    retry an ACL denial up to ``TMP_MAX`` times). ``O_BINARY`` keeps LF as LF on Windows. The fd is CLOSED before
    return (a link / replace of an open file fails on Windows). A fault after the create unlinks THIS temp."""
    for _ in range(3):
        name = os.path.join(d, SIDECAR_TMP_PREFIX + secrets.token_hex(6))
        try:
            fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0))
        except FileExistsError:
            continue
        break
    else:
        raise FileExistsError("3 consecutive sidecar temp name collisions in " + d)
    try:
        try:
            view = memoryview(data)
            while view:
                n = os.write(fd, view)
                if n <= 0:
                    raise OSError("zero-progress write into the sidecar temp")
                view = view[n:]
            os.fsync(fd)
        finally:
            os.close(fd)
    except BaseException:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise
    return name


_sc_make_temp = _sc_create_temp
_sc_link = os.link
_sc_replace = os.replace
_sc_unlink = os.unlink


def sidecar_path(png_path):
    """``foo_0003.png`` -> ``foo_0003.data.json`` (same directory, any ``.png`` case)."""
    return os.path.splitext(png_path)[0] + SIDECAR_SUFFIX


def canonical_bytes(env):
    """The STATED canonical form: ``json.dumps(env, allow_nan=False, ensure_ascii=True, indent=2)`` as
    ASCII (so any reader decodes it, whatever its locale) with a trailing newline. Raises ``ValueError``/``TypeError`` on NaN/inf or a non-JSON value."""
    return (json.dumps(env, allow_nan=False, ensure_ascii=True, indent=2) + "\n").encode("ascii")


def _sidecar_fail(env, reason, minted=True):
    """Failure exit: ``data_path`` null, the flag appended. The explicit route's ``data_overwrote`` stays PRESENT
    (as ``overwrote`` is) and reads False -- nothing landed under the final name, so nothing was overwritten."""
    env["data_path"] = None
    if minted:
        env.pop("data_overwrote", None)
    else:
        env["data_overwrote"] = False
    flags = env.get("flags")
    if not isinstance(flags, list):
        flags = env["flags"] = []
    flags.append("data_sidecar_failed:" + reason)


def write_data_sidecar(env, minted):
    """Write ``env`` (the success envelope, ``env["path"]`` = the published PNG) beside the PNG; MUTATES ``env``
    to carry ``data_path`` (absolute path, or ``None`` on a failure), ``data_overwrote`` (explicit-path PNG
    only) and, on a failure, the flag ``data_sidecar_failed:<reason>``. NEVER raises, never refuses.

    The sidecar holds EXACTLY the dict this leaves in ``env``: the keys are set first, the bytes are
    serialised once, and nothing touches ``env`` after a successful write. Exits:
    serialise fault -> ``not_serialisable`` | minted: ``os.link(temp, final)`` (atomic AND exclusive;
    ``FileExistsError`` -> ``exists``, a foreign file is never touched; any other ``OSError`` ->
    ``link_unsupported``) | explicit: ``os.replace(temp, final)`` (the caller owns the name; overwrite
    disclosed in ``data_overwrote``) | any other fault -> ``write_error``. The temp this call created is reaped in a
    ``finally`` AFTER the link / replace (any ``Exception`` from the reap is swallowed); a temp that cannot be
    reaped is a residual dot-file (``.data_sidecar_tmp_*``) that cannot be disclosed without breaking envelope
    equality. The temp's BYTES go through the exclusive descriptor; link / replace / reap still go by NAME.
    """
    tmp = None
    try:
        final = sidecar_path(env["path"])  # the PNG's own spelling: abspath would collapse a literal ``..``
        if not os.path.isabs(final):
            final = os.path.abspath(final)
        env["data_path"] = final
        if not minted:
            env["data_overwrote"] = os.path.exists(final)
        try:
            data = canonical_bytes(env)
        except (ValueError, TypeError, RecursionError):
            return _sidecar_fail(env, "not_serialisable", minted)
        try:
            tmp = _sc_make_temp(os.path.dirname(final), data)
        except OSError:
            return _sidecar_fail(env, "write_error", minted)
        if minted:
            try:
                _sc_link(tmp, final)
            except FileExistsError:
                return _sidecar_fail(env, "exists", minted)
            except OSError:
                return _sidecar_fail(env, "link_unsupported", minted)
        else:
            try:
                _sc_replace(tmp, final)
                tmp = None
            except OSError:
                return _sidecar_fail(env, "write_error", minted)
    except Exception:  # noqa: BLE001 -- the sidecar never refuses the call
        return _sidecar_fail(env, "write_error", minted)
    finally:
        if tmp is not None:
            try:
                _sc_unlink(tmp)
            except Exception:  # noqa: BLE001 -- a reap fault never refuses the call
                pass
