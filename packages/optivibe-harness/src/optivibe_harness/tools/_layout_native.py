"""tools/_layout_native.py — the ENGINE side of the native-layout retool.

Drives OpticStudio's own 2-D cross-section exporter
(``system.Tools.Layouts.OpenCrossSectionExport``) for ``render_layout(renderer=
"native")`` and, since, the two 3-D exporters
(``Open3DViewerExport`` / ``OpenShadedModelExport``) for ``renderer="native_3d"`` /
``"native_shaded"`` (``export_3d``, measured). Every step is fail-closed to a TOKEN the renderer maps
to a fallback (default) or a refusal (explicit); nothing here raises past its own
``finally``.

What the probe measured and this module therefore does:

* **The setters validate NOTHING** (Q1 surprise 4: ``EndSurface`` 8 on a 0..7 system
  and ``LowerPupil`` -1.25 read back as written). So this module validates its OWN
  request first, writes every setting it relies on, and reads EVERY written value
  back; a mismatch is ``native_settings_unverified``.
* **``Configuration`` MUTATES the system** (Q4): ``k > 0`` draws config ``k`` AND
  leaves the system's current configuration at ``k``; ``0`` draws config 1 and resets
  current to 1. So on a multi-config system ``Configuration = k`` is written with
  ``k`` the STRICT current read (never 0), and after ``Close()`` the current
  configuration is re-read, restored once if it moved, and re-read again; a restore
  that does not verify is reported so the renderer can REFUSE. On a
  single-config system ``Configuration`` is NOT written (``0`` is measured to draw the
  only configuration; ``1`` there is unmeasured).
* **The export deletes its target before running** (the probe's own sequence), and
  ``ImageExportData`` throws before a run, so the raster travels through a file.
* ``RunAndWaitForCompletion`` is the only run call measured on this tool;
  ``RunAndWaitWithTimeout``/``Cancel`` are not.
* ``IsValidFileName`` is NOT a gate, contrary to the earlier plan -- MEASURED
  in a live probe (Cooke): it read ``False`` for EVERY absolute target
  -- the 8 that exported a real PNG (backslash, forward-slash, existing, new, ``.png`` /
  ``.bmp`` / ``.jpg`` / no extension, a space in the directory, a trailing space) AND the 5 whose run
  raised -- and ``True`` only for a BARE name, whose run also RAISED. It decides
  nothing. The target is always ``os.path.abspath``; the file is gated after the run
  (magic + IHDR) by the caller.
* **A RAISING run wedges the engine's single tool slot for the session** -- MEASURED
  (same capture, 6 of 6 raising runs: a missing directory, an illegal character, a
  read-only existing file, a directory as the target, a 310-character path, a bare
  name): ``Close()`` then returns ``False``, ``IsRunning`` stays ``True``, and every
  later ``OpenCrossSectionExport`` / ``OpenLocalOptimization`` / ``OpenBatchRayTrace``
  returns ``None``. A second ``Close()``, ``Tools.CurrentTool.Close()``, opening a
  different tool, 1 s and 5 s waits and ``UpdateStatus()`` did not recover it; only a
  NEW session did. So the one trigger production could reach -- an over-long absolute
  target (``mkstemp`` in an existing directory rules the others out; and WITHOUT
  Windows long-path support Python's own ``mkstemp`` already fails at that length,
  measured live, so this guard is for long-path-enabled machines) -- is
  refused BEFORE the tool is opened, a raise is reported as ``run_raised`` (a field, never a
  substring of ``detail``), and NO recovery is attempted in code.

Imports ``_mce_cells`` only; never numpy/matplotlib, never ``layout_render``.
"""
import math
import os
from dataclasses import dataclass

from . import _mce_cells as _mc

#: The refusal the exporter returns on a coordinate break (even all-zero), a Tilted
#: surface or a Gradient9/GridGradient GRIN.   [measured: Q4, 7/7]
NON_AXIAL_MESSAGE = "Cannot perform 2D Layout on non-axial system!"
#: Own sanity bound on OUR request, not on the engine [unmeasured choice]: below the
#: smallest tested height (400) and 2x the largest tested width (2000). The canvas
#: chooser never leaves 400..1600, so this fires only on a harness bug.
EXPORT_PX_MIN = 200
EXPORT_PX_MAX = 4000
#: The exporter's measured default ray count (Q1). 0 removes every ray (Q1 surprise 5).
DEFAULT_N_RAYS = 7
#: Refuse a target whose ABSOLUTE path is at least this long, IN UTF-16 CODE UNITS
#: (``path_length_utf16``), before any tool is opened.
EXPORT_PATH_MAX = 260    # [measured: a live path-length probe, Cooke --
                          #  absolute targets of 200..259 characters exported; 260, 261
                          #  and 270 RAISED and wedged the tool slot (Windows MAX_PATH)]


@dataclass(frozen=True)
class ExportResult:
    """One cross-section export. ``ok`` means the file was written by a successful run;
    the CALLER still gates the file (magic + IHDR). ``token`` is a fallback token from
    ``layout_render._RENDERER_FALLBACK_TOKENS`` when not ok."""

    ok: bool
    token: "str | None"
    detail: "str | None"
    start: "int | None"
    end: "int | None"
    n_rays: "int | None"
    configuration_written: "int | None"
    configuration_after: "int | None"
    restore_verified: bool
    close_failed: bool
    #: The configuration the engine reported AFTER ``Close()`` and BEFORE any restore,
    #: when it differs from the one written (``None`` = it did not move). Captured so a
    #: move the restore then undid is DISCLOSED, never silently erased (R-9).
    configuration_moved_to: "int | None" = None
    #: ``RunAndWaitForCompletion`` RAISED (measured to wedge the tool slot, R-6).
    run_raised: bool = False
    #: ``OpenCrossSectionExport`` returned ``None`` (the slot was not available).
    open_returned_none: bool = False
    #: a tool WAS opened by this call (the slot was usable at the time).
    opened: bool = False
    #: What ``tool.Close()`` RETURNED -- the literal value, ``None`` when
    #: no tool was opened or ``Close()`` raised (then its return is UNKNOWN).
    close_returned: object = None
    #: ``tool.IsRunning`` read ONLY after ``Close()`` returned a non-True
    #: value (never on the healthy path, where the read RAISES RemotingException --
    #: measured 12/12). ``None`` = not read, raised, or absent.
    is_running_after_close: object = None
    #: ``tool.Field`` READ BACK after the run (never written) -- ``0``
    #: draws all fields [measured]; ``None`` = not read / unreadable.
    field_setting: "int | None" = None
    #: ``tool.Wavelength`` READ BACK after the run (never written); the
    #: Cooke default reads 2 [measured]; ``None`` = not read / unreadable.
    wavelength_setting: "int | None" = None


def read_axiality(system):
    """``bool(system.IsNonAxial)``; ``None`` on ANY fault (absent member included).

    ``True`` predicts the exporter's non-axial refusal (Q4, 7/7), so the renderer can
    decline before opening a tool. ``None`` is "could not read" -- the export is then
    attempted and the refusal MESSAGE is the backstop. Never raises.
    """
    try:
        value = system.IsNonAxial
    except Exception:  # noqa: BLE001 — unreadable axiality is not an answer
        return None
    if isinstance(value, bool):
        return value
    try:
        return bool(value)
    except Exception:  # noqa: BLE001
        return None


def strict_current_configuration(system):
    """The STRICT active-configuration read -- RAISES (``mce_cell``) on a fault.

    Never the safe read: ``safe_current_configuration`` turns a wedged read into 1,
    which here would write ``Configuration = 1`` and draw the wrong configuration.
    """
    return _mc.current_configuration(system)


def _result(ok, token, detail, **kw):
    base = dict(start=None, end=None, n_rays=None, configuration_written=None,
                configuration_after=None, restore_verified=True, close_failed=False,
                configuration_moved_to=None, run_raised=False, open_returned_none=False,
                opened=False, close_returned=None, is_running_after_close=None,
                field_setting=None, wavelength_setting=None)
    base.update(kw)
    return ExportResult(ok=ok, token=token, detail=detail, **base)


def _same_path(got, wrote):
    """The read-back ``OutputFileName`` equals what was written. Only a real ``str``
    compares (never a ``str()`` of an engine value); separator/case differences of the
    same absolute path are tolerated."""
    if type(got) is not str or type(wrote) is not str:
        return False
    if got == wrote:
        return True
    try:
        return (os.path.normcase(os.path.normpath(got))
                == os.path.normcase(os.path.normpath(wrote)))
    except Exception:  # noqa: BLE001
        return False


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def path_length_utf16(path):
    """The length of ``path`` in UTF-16 code units -- the unit Windows and .NET count a
    path in (``MAX_PATH``), NOT Python's code points: a supplementary-plane character
    (e.g. U+1F600) is ONE code point but TWO units (round 3). Measured on the
    EXACT string written to ``OutputFileName``. ``EXPORT_PATH_MAX`` was measured with
    ASCII targets only (where the two counts agree); that the engine limit is in UTF-16
    units for non-ASCII paths is INFERRED from the platform, not measured."""
    return len(path.encode("utf-16-le", "surrogatepass")) // 2


def _read_int_setting(tool, name):
    """A READ-BACK of one exporter setting production never writes.
    A built-in ``int`` (not a bool) or ``None``; never raises."""
    try:
        value = getattr(tool, name)
    except Exception:  # noqa: BLE001 -- an unreadable setting is not an answer
        return None
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, int) or (isinstance(value, float) and value.is_integer()):
            return int(value)
    except Exception:  # noqa: BLE001
        return None
    return None


def _config_count(value):
    """``config_count`` as a positive ``int``, or ``None`` when it is not one (a bool, a
    string, a non-finite or non-integral number, < 1). ``None`` -> the caller's
    ``configuration_unreadable`` path; never raises (round 3)."""
    try:
        # EXACT built-in types only (round 4): a bool, or an int/float SUBCLASS
        # whose comparison operators are caller code, is refused -- the later
        # comparisons then run on a built-in int only.
        if type(value) is int:
            n = value
        elif type(value) is float:
            if not (math.isfinite(value) and value.is_integer()):
                return None
            n = int(value)
        else:
            return None
        return n if n >= 1 else None
    except Exception:  # noqa: BLE001
        return None


def export_cross_section(system, out_path, *, W, H, start, end, n_rays, config_count,
                         n_surfaces):
    """Run ONE native cross-section export to ``out_path``. Never raises.

    Order, every step fail-closed to a token:
      1. own validation of the request -> ``native_settings_unverified``;
      2. configuration: ``config_count`` None -> ``configuration_unreadable``; > 1 ->
         the STRICT current read ``k`` (a raise, OR a value outside ``1..config_count``
         -> ``configuration_unreadable``, no tool opened, nothing written); == 1 ->
         ``Configuration`` is never written;
      3. open the tool (absent ``Tools.Layouts`` / ``None`` / a raise ->
         ``native_unavailable``);
      4. delete the target, write every relied-on setting; 5. read every one back ->
         ``native_settings_unverified`` (``IsValidFileName`` is measured useless);
      6. ``RunAndWaitForCompletion`` (a raise -> ``native_export_failed``);
      7. ``Succeeded`` False -> ``non_axial`` on the measured message, else
         ``native_export_failed`` with the message verbatim;
      8. ``finally``: ``Close()`` (a raise sets ``close_failed``; the result stands),
         then, iff ``Configuration`` was written: re-read (a move is kept as
         ``configuration_moved_to``), restore once, re-read -> ``restore_verified``.
    """
    # 1. own validation -------------------------------------------------------- #
    try:
        n_s = int(n_surfaces)
        ok_range = (_is_int(start) and _is_int(end) and 0 <= start < end <= n_s - 1)
        ok_size = (_is_int(W) and _is_int(H) and EXPORT_PX_MIN <= W <= EXPORT_PX_MAX
                   and EXPORT_PX_MIN <= H <= EXPORT_PX_MAX)
        ok_rays = _is_int(n_rays) and n_rays >= 0
    except Exception:  # noqa: BLE001
        ok_range = ok_size = ok_rays = False
    if not (ok_range and ok_size and ok_rays):
        return _result(False, "native_settings_unverified",
                       f"own request out of bounds (start={start!r}, end={end!r}, "
                       f"W={W!r}, H={H!r}, n_rays={n_rays!r}, n_surfaces={n_surfaces!r})")
    # Round 4: the target is resolved ONCE; the string counted here is the
    # string written to OutputFileName (a stateful PathLike cannot swap it between).
    try:
        target = os.path.abspath(os.fspath(out_path))
        if type(target) is not str:
            raise TypeError("the export target is not a text path")
        target_len = path_length_utf16(target)
    except Exception as exc:  # noqa: BLE001 — an unresolvable target is no target
        return _result(False, "native_settings_unverified",
                       "the export target could not be resolved "
                       f"({type(exc).__name__})")
    if target_len >= EXPORT_PATH_MAX:
        return _result(False, "native_settings_unverified",
                       f"the export target path is {target_len} UTF-16 units; an export "
                       f"to a path of {EXPORT_PATH_MAX} or more RAISES and was measured "
                       "to leave the engine's tool slot unusable for the session")

    # 2. configuration ----------------------------------------------------------- #
    if config_count is None:
        return _result(False, "configuration_unreadable",
                       "the configuration count could not be read")
    n_cfg = _config_count(config_count)
    if n_cfg is None:
        return _result(False, "configuration_unreadable",
                       "the configuration count is not a positive integer "
                       "(a built-in int, or an integral finite float)")
    config_count = n_cfg
    k = None
    if config_count > 1:
        try:
            k = int(strict_current_configuration(system))
        except Exception as exc:  # noqa: BLE001 — never guess the configuration
            return _result(False, "configuration_unreadable",
                           f"the current configuration could not be read: {exc}")
        # A 1-based index outside 1..N is corrupt by definition: written as it stands,
        # 0 would draw config 1 AND move current to 1 (Q4), and N+1 draws nothing the
        # system has. Refused BEFORE any tool is opened or any value written (R-4).
        if not 1 <= k <= int(config_count):
            return _result(False, "configuration_unreadable",
                           f"the current-configuration read {k} is outside "
                           f"1..{int(config_count)}")

    # 3. open ------------------------------------------------------------------ #
    try:
        layouts = system.Tools.Layouts
    except AttributeError:
        return _result(False, "native_unavailable", "Tools.Layouts absent")
    except Exception as exc:  # noqa: BLE001
        return _result(False, "native_unavailable",
                       f"Tools.Layouts unreadable: {type(exc).__name__}: {exc}")
    try:
        tool = layouts.OpenCrossSectionExport()
    except AttributeError:
        return _result(False, "native_unavailable", "Tools.Layouts absent")
    except Exception as exc:  # noqa: BLE001 — a busy tool slot raises or returns None
        return _result(False, "native_unavailable",
                       f"OpenCrossSectionExport raised {type(exc).__name__}: {exc}")
    if tool is None:
        return _result(False, "native_unavailable",
                       "OpenCrossSectionExport returned None (the tool slot is busy)",
                       open_returned_none=True)

    common = dict(start=start, end=end, n_rays=n_rays, configuration_written=k)
    outcome = None
    close_failed = False
    after = None
    moved_to = None
    restore_verified = True
    close_returned = None
    is_running = None
    field_setting = None
    wavelength_setting = None
    try:
        # 4. the measured sequence deletes the target first (the target resolved
        # and counted in step 1 -- never re-resolved here) ----------------------- #
        if os.path.exists(target):
            os.remove(target)
        settings = [
            ("SaveImageAsFile", True), ("OutputFileName", target),
            ("OutputPixelWidth", int(W)), ("OutputPixelHeight", int(H)),
            ("StartSurface", int(start)), ("EndSurface", int(end)),
            ("NumberOfRays", int(n_rays)), ("YStretch", 1.0),
            ("UpperPupil", 1.0), ("LowerPupil", -1.0),
            ("MarginalAndChiefRayOnly", False), ("DeleteVignetted", False),
        ]
        if k is not None:
            settings.append(("Configuration", int(k)))
        for name, value in settings:
            setattr(tool, name, value)
        # 5. read EVERY written value back (IsValidFileName is not consulted -- see
        # the module docstring: measured to read False on every working path) ---- #
        for name, value in settings:
            got = getattr(tool, name)
            if name == "OutputFileName":
                same = _same_path(got, value)
            elif isinstance(value, bool):
                same = bool(got) is value
            elif isinstance(value, float):
                try:
                    same = math.isfinite(float(got)) and float(got) == value
                except (TypeError, ValueError):
                    same = False
            else:
                try:
                    same = int(got) == value and not isinstance(got, bool)
                except (TypeError, ValueError):
                    same = False
            if not same:
                outcome = _result(False, "native_settings_unverified",
                                  f"{name} wrote {value!r} but read back {got!r}",
                                  **common)
                break
        # 6. run ----------------------------------------------------------------- #
        if outcome is None:
            try:
                tool.RunAndWaitForCompletion()
            except Exception as exc:  # noqa: BLE001
                # the first line only: the engine appends a multi-line server stack
                first = (f"{exc}".strip().splitlines() or [""])[0].strip()
                outcome = _result(False, "native_export_failed",
                                  f"RunAndWaitForCompletion raised "
                                  f"{type(exc).__name__}: {first}", run_raised=True,
                                  **common)
        # 7. verdict --------------------------------------------------------------- #
        if outcome is None:
            try:
                succeeded = bool(tool.Succeeded)
            except Exception as exc:  # noqa: BLE001
                succeeded = False
                message = f"Succeeded unreadable: {exc}"
            else:
                try:
                    message = str(tool.ErrorMessage)
                except Exception:  # noqa: BLE001
                    message = ""
            if not succeeded:
                token = ("non_axial" if message.strip() == NON_AXIAL_MESSAGE
                         else "native_export_failed")
                outcome = _result(False, token, message or "Succeeded is False",
                                  **common)
            else:
                outcome = _result(True, None, None, **common)
            # The exporter's field / wavelength choice, READ BACK after a
            # run that did not raise (reads, never writes -- production writes neither).
            field_setting = _read_int_setting(tool, "Field")
            wavelength_setting = _read_int_setting(tool, "Wavelength")
    except Exception as exc:  # noqa: BLE001 — a write/read fault on the tool
        outcome = _result(False, "native_settings_unverified",
                          f"{type(exc).__name__}: {exc}", **common)
    finally:
        # 8. Close, then observe the configuration ----------------------------- #
        try:
            close_returned = tool.Close()
        except Exception:  # noqa: BLE001 — the export result still stands
            close_failed = True
            close_returned = None       # a raised Close() has NO known return
        # IsRunning is read ONLY after a Close() that returned a non-True
        # value and did not raise -- on the healthy path the read RAISES (12/12).
        if not close_failed and close_returned is not True:
            try:
                is_running = tool.IsRunning
            except Exception:  # noqa: BLE001 -- unreadable -> the UNKNOWN band
                is_running = None
        if k is not None:
            try:
                after = int(strict_current_configuration(system))
                if after != k:
                    moved_to = after      # kept BEFORE the restore overwrites `after`
                    system.MCE.SetCurrentConfiguration(k)
                    after = int(strict_current_configuration(system))
                restore_verified = after == k
            except Exception:  # noqa: BLE001 — an unobservable restore is unverified
                restore_verified = False
    return ExportResult(
        ok=outcome.ok, token=outcome.token, detail=outcome.detail,
        start=outcome.start, end=outcome.end, n_rays=outcome.n_rays,
        configuration_written=outcome.configuration_written,
        configuration_after=after, restore_verified=restore_verified,
        close_failed=close_failed, configuration_moved_to=moved_to,
        run_raised=outcome.run_raised, open_returned_none=False, opened=True,
        close_returned=close_returned, is_running_after_close=is_running,
        field_setting=field_setting, wavelength_setting=wavelength_setting,
    )


# =========================================================================== #
# -- the native 3-D views (``export_3d``, measured)
# =========================================================================== #
#: The 3-D captures' size.   [measured: every 3-D capture 1200 x 800]
EXPORT_3D_SIZE = (1200, 800)
#: kind -> the exporter's measured default ray count (3-D viewer 3, shaded 45).
DEFAULT_N_RAYS_3D = {"viewer": 3, "shaded": 45}
EXPORT_3D_KINDS = ("viewer", "shaded")


@dataclass(frozen=True)
class Export3DResult:
    """One 3-D export. ``ok`` means a successful run wrote the file; the identity proof
    (``identity_verified``: the strict current read was ``k`` before AND after) and the
    file gate (magic + IHDR) are the CALLER's to enforce."""

    ok: bool
    token: "str | None"
    detail: "str | None"
    start: "int | None" = None
    end: "int | None" = None
    n_rays: "int | None" = None
    #: ``k`` -- the strict current read on EVERY call (single-config included).
    configuration_drawn: "int | None" = None
    configuration_before: "int | None" = None
    #: the strict re-read after ``Close()`` (after the one restore when it was needed).
    configuration_after: "int | None" = None
    #: the configuration read after ``Close()`` and BEFORE any restore, when it moved.
    configuration_moved_to: "int | None" = None
    #: before == after == k, re-read after Close() (and after one restore if needed).
    identity_verified: bool = False
    close_failed: bool = False
    #: INC-2b: the slot evidence, captured exactly as ``export_cross_section`` does.
    run_raised: bool = False
    open_returned_none: bool = False
    opened: bool = False
    close_returned: object = None
    is_running_after_close: object = None


def _3d_result(ok, token, detail, **kw):
    return Export3DResult(ok=ok, token=token, detail=detail, **kw)


def _color_rays_by_config(system):
    """The live ``ColorRaysByOptions.Config`` member (a fake injects
    ``system._enum_types["ColorRaysByOptions"]``). RAISES on a resolution failure."""
    injected = getattr(system, "_enum_types", None)
    if injected is not None and "ColorRaysByOptions" in injected:
        return injected["ColorRaysByOptions"].Config
    import ZOSAPI.Tools.Layouts as _layouts  # type: ignore  # pragma: no cover - live
    return _layouts.ColorRaysByOptions.Config  # pragma: no cover - live backend path


def export_3d(system, kind, out_path, *, size=EXPORT_3D_SIZE, config_count, start, end,
              n_rays, n_surfaces):
    """Run ONE native 3-D export (``kind`` ``"viewer"`` / ``"shaded"``). Never raises.

    The order (``export_3d``), every step fail-closed to a token:
      1. own validation (kind, size, range, rays, target) -> ``native_settings_unverified``;
         ``config_count`` None / not a positive int -> ``configuration_unreadable``;
      2. ``k`` := the STRICT current read on EVERY call (single-config included); a raise
         or a value outside ``1..config_count`` -> ``configuration_unreadable``, NO tool
         opened. ``before := k``;
      3. open ``Open3DViewerExport`` / ``OpenShadedModelExport`` (``None`` / a raise ->
         ``native_unavailable``);
      4. settings do NOT persist between tool instances, so every relied-on
         setting is written on every call: the output, ``StartSurface``/``EndSurface``,
         ``NumberOfRays``; iff ``config_count > 1`` ``ConfigurationAll=False``,
         ``ConfigurationCurrent=False`` and ``SetConfigurationEnabled(c, c == k)`` for every
         ``c`` (byte-identical to "current = k", current NOT moved); viewer only
         ``ColorRaysBy=Config``. There is NO integer ``Configuration`` member and NO
         camera member is written or read (the shaded angle read-back does not describe
         the drawn camera). Every written member is read back;
      5. ``RunAndWaitForCompletion`` (a raise -> ``native_export_failed``,
         ``run_raised``); ``Succeeded`` False -> ``native_export_failed`` (the probe exported
         every folded sample, so there is no ``non_axial`` mapping here);
      6. ``finally``: ``Close()`` (the INC-2b slot evidence is captured exactly as the
         cross-section captures it), then the strict re-read ``after``; ``after != k``
         -> ONE ``SetCurrentConfiguration(k)`` + re-read; ``identity_verified =
         (after == k)`` -- the caller REFUSES the whole render when it is False.
    """
    # 1. own validation --------------------------------------------------------- #
    W = H = None
    try:
        W, H = size
        n_s = int(n_surfaces)
        ok_kind = kind in EXPORT_3D_KINDS
        ok_range = (_is_int(start) and _is_int(end) and 0 <= start < end <= n_s - 1)
        ok_size = (_is_int(W) and _is_int(H) and EXPORT_PX_MIN <= W <= EXPORT_PX_MAX
                   and EXPORT_PX_MIN <= H <= EXPORT_PX_MAX)
        ok_rays = _is_int(n_rays) and n_rays >= 0
    except Exception:  # noqa: BLE001
        ok_kind = ok_range = ok_size = ok_rays = False
    if not (ok_kind and ok_range and ok_size and ok_rays):
        return _3d_result(False, "native_settings_unverified",
                          f"own request out of bounds (kind={kind!r}, start={start!r}, "
                          f"end={end!r}, size={size!r}, n_rays={n_rays!r}, "
                          f"n_surfaces={n_surfaces!r})")
    try:
        target = os.path.abspath(os.fspath(out_path))
        if type(target) is not str:
            raise TypeError("the export target is not a text path")
        target_len = path_length_utf16(target)
    except Exception as exc:  # noqa: BLE001 -- an unresolvable target is no target
        return _3d_result(False, "native_settings_unverified",
                          "the export target could not be resolved "
                          f"({type(exc).__name__})")
    # The raise-wedge was measured on the cross-section exporter (R-6); whether a 3-D
    # run raises on such a target is UNMEASURED, so the same pre-open bound is applied
    # rather than learnt.
    if target_len >= EXPORT_PATH_MAX:
        return _3d_result(False, "native_settings_unverified",
                          f"the export target path is {target_len} UTF-16 units; an "
                          f"export to a path of {EXPORT_PATH_MAX} or more RAISES and was "
                          "measured to leave the engine's tool slot unusable")
    # 2. configuration: k on EVERY call ------------------------------------------ #
    if config_count is None:
        return _3d_result(False, "configuration_unreadable",
                          "the configuration count could not be read")
    n_cfg = _config_count(config_count)
    if n_cfg is None:
        return _3d_result(False, "configuration_unreadable",
                          "the configuration count is not a positive integer")
    try:
        k = int(strict_current_configuration(system))
    except Exception as exc:  # noqa: BLE001 -- never guess the configuration
        return _3d_result(False, "configuration_unreadable",
                          f"the current configuration could not be read: {exc}")
    if not 1 <= k <= n_cfg:
        return _3d_result(False, "configuration_unreadable",
                          f"the current-configuration read {k} is outside 1..{n_cfg}")
    # 3. open ------------------------------------------------------------------- #
    try:
        layouts = system.Tools.Layouts
    except AttributeError:
        return _3d_result(False, "native_unavailable", "Tools.Layouts absent")
    except Exception as exc:  # noqa: BLE001
        return _3d_result(False, "native_unavailable",
                          f"Tools.Layouts unreadable: {type(exc).__name__}: {exc}")
    opener = "Open3DViewerExport" if kind == "viewer" else "OpenShadedModelExport"
    try:
        if kind == "viewer":
            tool = layouts.Open3DViewerExport()
        else:
            tool = layouts.OpenShadedModelExport()
    except AttributeError:
        return _3d_result(False, "native_unavailable",
                          f"Tools.Layouts.{opener} absent")
    except Exception as exc:  # noqa: BLE001 -- a busy tool slot raises or returns None
        return _3d_result(False, "native_unavailable",
                          f"{opener} raised {type(exc).__name__}: {exc}")
    if tool is None:
        return _3d_result(False, "native_unavailable",
                          f"{opener} returned None (the tool slot is busy)",
                          open_returned_none=True)

    common = dict(start=start, end=end, n_rays=n_rays)
    outcome = None
    close_failed = False
    close_returned = None
    is_running = None
    after = None
    moved_to = None
    identity = False
    try:
        # 4. delete the target, write every relied-on setting, read every one back -- #
        if os.path.exists(target):
            os.remove(target)
        settings = [
            ("SaveImageAsFile", True), ("OutputFileName", target),
            ("OutputPixelWidth", int(W)), ("OutputPixelHeight", int(H)),
            ("StartSurface", int(start)), ("EndSurface", int(end)),
            ("NumberOfRays", int(n_rays)),
        ]
        if n_cfg > 1:
            # T-CFG6: on a single-config system NOTHING config-related is written (the
            # default draws the current configuration).
            settings += [("ConfigurationAll", False), ("ConfigurationCurrent", False)]
        if kind == "viewer":
            settings.append(("ColorRaysBy", _color_rays_by_config(system)))
        for name, value in settings:
            setattr(tool, name, value)
        if n_cfg > 1:
            for c in range(1, n_cfg + 1):
                tool.SetConfigurationEnabled(c, c == k)
        for name, value in settings:
            got = getattr(tool, name)
            if name == "OutputFileName":
                same = _same_path(got, value)
            elif name == "ColorRaysBy":
                # an enum member: compared as a MEMBER, never a str() of an engine
                # value (the base-slot normalization rule)
                try:
                    same = bool(got == value)
                except Exception:  # noqa: BLE001 -- an uncomparable read-back is unproven
                    same = False
            elif isinstance(value, bool):
                same = bool(got) is value
            else:
                try:
                    same = int(got) == value and not isinstance(got, bool)
                except (TypeError, ValueError):
                    same = False
            if not same:
                outcome = _3d_result(False, "native_settings_unverified",
                                     f"{name} wrote {value!r} but read back {got!r}",
                                     **common)
                break
        # 5. run + verdict ------------------------------------------------------- #
        if outcome is None:
            try:
                tool.RunAndWaitForCompletion()
            except Exception as exc:  # noqa: BLE001
                first = (f"{exc}".strip().splitlines() or [""])[0].strip()
                outcome = _3d_result(False, "native_export_failed",
                                     f"RunAndWaitForCompletion raised "
                                     f"{type(exc).__name__}: {first}", run_raised=True,
                                     **common)
        if outcome is None:
            try:
                succeeded = bool(tool.Succeeded)
                message = ""
                if not succeeded:
                    try:
                        message = str(tool.ErrorMessage)
                    except Exception:  # noqa: BLE001
                        message = ""
            except Exception as exc:  # noqa: BLE001
                succeeded = False
                message = f"Succeeded unreadable: {exc}"
            outcome = (_3d_result(True, None, None, **common) if succeeded else
                       _3d_result(False, "native_export_failed",
                                  message or "Succeeded is False", **common))
    except Exception as exc:  # noqa: BLE001 -- a write/read fault on the tool
        outcome = _3d_result(False, "native_settings_unverified",
                             f"{type(exc).__name__}: {exc}", **common)
    finally:
        # 6. Close, the slot evidence, then the ONE identity proof --------------- #
        try:
            close_returned = tool.Close()
        except Exception:  # noqa: BLE001 -- the export result still stands
            close_failed = True
            close_returned = None
        # IsRunning only after a Close() that returned non-True.
        if not close_failed and close_returned is not True:
            try:
                is_running = tool.IsRunning
            except Exception:  # noqa: BLE001 -- unreadable -> the UNKNOWN band
                is_running = None
        # before == after == k, single-config included (N6: the observation runs even
        # when nothing config-related was written).
        try:
            after = int(strict_current_configuration(system))
            if after != k:
                moved_to = after      # kept BEFORE the restore overwrites `after`
                system.MCE.SetCurrentConfiguration(k)
                after = int(strict_current_configuration(system))
            identity = after == k
        except Exception:  # noqa: BLE001 -- an unobservable identity is not proven
            identity = False
    return Export3DResult(
        ok=outcome.ok, token=outcome.token, detail=outcome.detail,
        start=outcome.start, end=outcome.end, n_rays=outcome.n_rays,
        configuration_drawn=k, configuration_before=k, configuration_after=after,
        configuration_moved_to=moved_to, identity_verified=identity,
        close_failed=close_failed, run_raised=outcome.run_raised,
        open_returned_none=False, opened=True, close_returned=close_returned,
        is_running_after_close=is_running,
    )
