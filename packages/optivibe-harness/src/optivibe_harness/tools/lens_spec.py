"""tools/lens_spec.py — LensSpec / SurfaceSpec dataclasses + dispatch wrappers.

A ``LensSpec`` is a frozen, serializable snapshot of a whole optical system:
aperture + field type/set + wavelengths + the ordered surface list (OBJECT..
IMAGE). The composition helpers ``LensSpec.read(system)`` / ``LensSpec.apply(
system, spec)`` do the work (testable without dispatch, reusable); the
thin ``read_lens_spec`` / ``apply_lens_spec`` ToolSpecs wrap them so an agent can
round-trip a whole design in one call.

``apply`` reuses the mutator/validation helpers (set_surface / substitute_
glass / set_field / set_wavelength / set_aperture / set_stop_surface), so bad
glass, out-of-range, and the image-stop no-op are caught IDENTICALLY here.

Round-trip contract (§F): ``LensSpec.read(apply(spec)) == spec`` within tolerance
(numeric via ``_readback_ok``, strings canonical/case-insensitive).

Live ZOS-API integration: exercised by the live test; unit-tested here against
the fake LDE/SystemData doubles.
"""
import glob
import math
import os
import tempfile
from dataclasses import dataclass, field, fields as dc_fields
from typing import Optional, Tuple

from .._io import safe_float
from ..errors import (CatalogLoadError, SolveDrivenError, SurfaceWriteError,
                      ToolParamError)
from ..server import ToolSpec
from . import _asphere_cells as _asph
from . import _lens_common as _c
from ._lens_common import _clear_material_to_air  # S-2: shared clear-to-air locus (L30)
from . import _optimize_common as _oc
from . import lens_glass, lens_surface, lens_system
from .optimize_merit_io import _unlink_quiet

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class SurfaceSpec:
    """One surface's spec and the per-field "declared write" semantics.

    Each field is either a DECLARED value (apply SETS it + the round-trip
    verifier VERIFIES it) or a don't-touch sentinel (apply leaves the engine
    alone + the verifier does not compare it). The single source of truth for
    which fields a surface declares is ``_declared_writes`` — ``apply`` and the
    round-trip verifier both iterate it, so they can never drift.

    Per-field contract:

    - ``radius`` / ``thickness`` / ``conic`` — always DECLARED (a real number,
      ``inf`` allowed for planar/object). Set + verified on EVERY surface,
      INCLUDING OBJECT index 0 (OBJECT ``thickness`` is the object distance — a
      real settable parameter ``read()`` captures).
    - ``semi_diameter: float | None`` — a real number is DECLARED (set+verify);
      ``None`` means "don't touch / leave the engine's auto/solve" (NOT set, NOT
      verified). ``read()`` ALWAYS captures the engine's resolved numeric
      semi-diameter (never ``None``), so a spec produced by ``read()`` re-applies
      and verifies that value — ``read(apply(spec)) == spec`` holds. ``None`` is
      reserved for HAND-AUTHORED specs that want the engine to own the value;
      apply does not clear-to-auto and the verifier does not flag the surviving
      value.
    - ``material: str`` — ALWAYS DECLARED, including ``""`` (= air). ``""`` means
      "this surface is air": apply does not call ``substitute_glass`` (it has no
      glass to set) but the verifier STILL compares ``material`` so a declared air
      that left a stale glass in place is caught.
    - ``comment: str`` — ALWAYS DECLARED, including ``""`` (= clear the comment).
      ``comment=""`` is a real write (clear) that apply performs and the verifier
      checks.
    - ``is_stop: bool`` — declared placement; verified by
      ``_stop_existence_mismatches`` (exactly one stop on the declared surface),
      not by ``_declared_writes`` (stop semantics are system-wide, not a
      per-surface field write).
    """

    radius: float
    thickness: float
    conic: float = 0.0
    semi_diameter: Optional[float] = None
    material: str = ""
    comment: str = ""
    is_stop: bool = False
    # Asphere: the ordered even-asphere coefficients (α2..α16; index i ->
    # Par(i+1) -> the (2(i+1))th-order term) — a SEPARATE field from radius/conic (an
    # EvenAspheric's Radius/Conic ARE the Standard columns, already carried). ``None`` is
    # the don't-touch sentinel (a non-asphere omits it; ``read()`` returns None on a
    # non-asphere; apply writes nothing; the verify does not compare it) — exactly the
    # ``semi_diameter: Optional`` precedent. A TUPLE of <=8 finite floats is a declared
    # asphere (a short list writes only the leading terms — the set_asphere short-write).
    aspheric_coefficients: Optional[Tuple[float, ...]] = None
    # Asphere: the additive type/norm/max-term siblings. All
    # default ``None`` -> byte-identical for every shipped EvenAspheric/Standard spec
    # (they omit the new keys). ``asphere_surface_type=None`` with coefficients present
    # IMPLICITLY means EvenAspheric (the S1/S2a back-compat carry). The gated Extended
    # types REQUIRE asphere_norm_radius>0 AND asphere_max_term==len(coefficients) (the
    # all-or-nothing invariant, enforced in __post_init__).
    asphere_surface_type: Optional[str] = None
    asphere_norm_radius: Optional[float] = None
    asphere_max_term: Optional[int] = None

    def __post_init__(self):
        for name in ("radius", "thickness", "conic"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ToolParamError(
                    f"SurfaceSpec.{name} must be a number, got {value!r}"
                )
        if self.semi_diameter is not None and (
            isinstance(self.semi_diameter, bool)
            or not isinstance(self.semi_diameter, (int, float))
        ):
            raise ToolParamError(
                f"SurfaceSpec.semi_diameter must be a number or None, got "
                f"{self.semi_diameter!r}"
            )
        # 3a: a semi-diameter is a half-aperture — it must be >= 0, or the +inf-auto
        # sentinel (engine-owned). A NEGATIVE (incl. -inf) or nan semi-diameter is a
        # malformed payload: reject it LOUD here rather than silently dropping it from
        # the declared write (the old ``math.isinf`` exemption swallowed -inf).
        if self.semi_diameter is not None:
            sd = float(self.semi_diameter)
            if math.isnan(sd):
                raise ToolParamError(
                    "SurfaceSpec.semi_diameter must be >= 0 or +inf (auto), got nan"
                )
            if sd < 0:
                raise ToolParamError(
                    "SurfaceSpec.semi_diameter must be >= 0 or +inf (auto); a "
                    f"negative half-aperture is invalid, got {sd!r}"
                )
        if not isinstance(self.material, str):
            raise ToolParamError("SurfaceSpec.material must be a string")
        if not isinstance(self.comment, str):
            raise ToolParamError("SurfaceSpec.comment must be a string")
        if not isinstance(self.is_stop, bool):
            raise ToolParamError("SurfaceSpec.is_stop must be a bool")
        # Asphere: validate a non-None coefficients value via the SINGLE S1
        # validator (asphere_surface._validate_coefficients — a tuple/list of <=8 finite
        # numbers, each bool-screened; len>8, non-number, inf/-inf/nan all RAISE
        # ToolParamError). REUSE it, do NOT re-implement (one source of truth). A frozen
        # dataclass holds a tuple, so we validate the value as-is (the field carries the
        # tuple; from_dict coerces a JSON array -> tuple before construction).
        # Asphere: validate the asphere_surface_type FIRST (it selects the
        # per-type length bound + the gated norm/max-term rules).
        if self.asphere_surface_type is not None:
            if not isinstance(self.asphere_surface_type, str):
                raise ToolParamError(
                    "SurfaceSpec.asphere_surface_type must be a string or None, got "
                    f"{self.asphere_surface_type!r}"
                )
            if self.asphere_surface_type not in _asph.ASPHERE_TYPE_INFO:
                raise ToolParamError(
                    f"SurfaceSpec.asphere_surface_type {self.asphere_surface_type!r} is "
                    f"not an authorable asphere type; valid: "
                    f"{list(_asph.ASPHERE_TYPE_NAMES)}"
                )
            if self.aspheric_coefficients is None:
                raise ToolParamError(
                    "SurfaceSpec.asphere_surface_type is set but "
                    "aspheric_coefficients is None — a declared asphere type requires "
                    "its coefficients"
                )
        # The effective type: an EvenAspheric IMPLICIT carry (coefficients present, type
        # None) uses the EvenAspheric descriptor (the S1/S2a back-compat path).
        if self.aspheric_coefficients is not None:
            from . import asphere_surface as _asph_tool
            type_key = self.asphere_surface_type or "EvenAspheric"
            info = _asph.ASPHERE_TYPE_INFO[type_key]
            # Per-type length + finiteness firewall (reused; a non-list/tuple, a length
            # over the type's max, a bool / non-number / non-finite entry RAISE).
            _asph_tool._validate_coefficients(self.aspheric_coefficients, info)
            # Gated (Extended) types require norm>0 AND max_term==len(coeffs) in [1,240].
            if info.gated:
                if (
                    self.asphere_norm_radius is None
                    or isinstance(self.asphere_norm_radius, bool)
                    or not isinstance(self.asphere_norm_radius, (int, float))
                    or not math.isfinite(float(self.asphere_norm_radius))
                    or float(self.asphere_norm_radius) <= 0.0
                ):
                    raise ToolParamError(
                        f"SurfaceSpec for a {type_key} requires asphere_norm_radius > 0 "
                        f"(the p=r/norm normalization scale), got "
                        f"{self.asphere_norm_radius!r}"
                    )
                n_coeffs = len(self.aspheric_coefficients)
                if n_coeffs < 1:
                    raise ToolParamError(
                        f"a {type_key} requires at least 1 coefficient"
                    )
                if (
                    self.asphere_max_term is None
                    or isinstance(self.asphere_max_term, bool)
                    or not isinstance(self.asphere_max_term, int)
                    or self.asphere_max_term != n_coeffs
                ):
                    raise ToolParamError(
                        f"SurfaceSpec.asphere_max_term must equal "
                        f"len(aspheric_coefficients) ({n_coeffs}) for a {type_key}, got "
                        f"{self.asphere_max_term!r}"
                    )
            else:
                # Non-gated (Odd/Even): norm + max_term must be ABSENT (a wrong-type param).
                if self.asphere_norm_radius is not None:
                    raise ToolParamError(
                        f"SurfaceSpec.asphere_norm_radius is only valid for the "
                        f"normalized Extended asphere types; {type_key} is absolute-r "
                        "(omit asphere_norm_radius)"
                    )
                if self.asphere_max_term is not None:
                    raise ToolParamError(
                        f"SurfaceSpec.asphere_max_term is only carried for the gated "
                        f"Extended types; {type_key} has a fixed cell count (omit it)"
                    )
        else:
            # No coefficients: norm/max-term/type must all be absent (a malformed triple).
            if (
                self.asphere_norm_radius is not None
                or self.asphere_max_term is not None
            ):
                raise ToolParamError(
                    "SurfaceSpec.asphere_norm_radius / asphere_max_term require "
                    "aspheric_coefficients to be declared"
                )

    @classmethod
    def from_dict(cls, d):
        if not isinstance(d, dict):
            raise ToolParamError(f"SurfaceSpec must be a dict, got {type(d).__name__}")
        allowed = {f.name for f in dc_fields(cls)}
        unknown = set(d) - allowed
        if unknown:
            raise ToolParamError(f"unknown SurfaceSpec key(s): {sorted(unknown)}")
        if "radius" not in d or "thickness" not in d:
            raise ToolParamError("SurfaceSpec requires 'radius' and 'thickness'")
        return cls(
            radius=_coerce_inf(d["radius"], label="radius"),
            thickness=_coerce_inf(d["thickness"], label="thickness"),
            # Route conic through ``_coerce_inf`` (like radius/
            # thickness), NOT ``_strict_float``. A coordinate-break surface's Conic
            # reads back the ``"inf"`` STRING sentinel (probe P1 col 9), and a
            # ``_strict_float("inf")`` RAISED ``ToolParamError`` past the
            # ``apply_lens_spec`` boundary on any CB/fold system (the never-raise hole).
            # ``_coerce_inf`` accepts the ``inf``/``-inf``/``nan`` sentinels so the read
            # is self-consistent on a CB surface; the L26/L30 sibling sweep below
            # confirms radius/thickness/conic/semi_diameter ALL route through it.
            conic=_coerce_inf(d.get("conic", 0.0), label="conic"),
            semi_diameter=(
                # D1/3a: route through ``_coerce_inf`` (like radius/thickness) so a
                # legacy/hand-authored ``"inf"`` semi-diameter at least PARSES (closing
                # the F3 asymmetry) instead of being rejected by ``_strict_float``. The
                # inf straggler is then exempted from the WRITE in ``_declared_writes``.
                None
                if d.get("semi_diameter") is None
                else _coerce_inf(d["semi_diameter"], label="semi_diameter")
            ),
            material=_require_str(d.get("material", ""), label="material"),
            comment=_require_str(d.get("comment", ""), label="comment"),
            is_stop=_require_bool(d.get("is_stop", False), label="is_stop"),
            # Asphere: a JSON array -> a tuple of floats (the don't-touch
            # sentinel None when absent/null). __post_init__ validates the shape + each
            # entry via the S1 validator (a non-array / >8 / non-finite RAISES there).
            aspheric_coefficients=_coerce_coefficients(d.get("aspheric_coefficients")),
            # Asphere: the additive type/norm/max-term siblings. Absent ->
            # None (a non-asphere / EvenAspheric implicit carry); __post_init__ validates
            # the triple (a malformed combination RAISES there).
            asphere_surface_type=d.get("asphere_surface_type"),
            asphere_norm_radius=(
                None if d.get("asphere_norm_radius") is None
                else _strict_float(d["asphere_norm_radius"], label="asphere_norm_radius")
            ),
            asphere_max_term=(
                None if d.get("asphere_max_term") is None
                else _require_int(d["asphere_max_term"], label="asphere_max_term")
            ),
        )

    def to_dict(self):
        return {
            "radius": safe_float(self.radius),
            "thickness": safe_float(self.thickness),
            "conic": safe_float(self.conic),
            "semi_diameter": (
                None if self.semi_diameter is None else safe_float(self.semi_diameter)
            ),
            "material": self.material,
            "comment": self.comment,
            "is_stop": self.is_stop,
            # Asphere: None (don't-touch / non-asphere) or a list of
            # safe_floats. Omit-on-None semantics for round-trip parity (a plain Standard
            # surface emits None, so an existing all-Standard spec is byte-identical).
            "aspheric_coefficients": (
                None
                if self.aspheric_coefficients is None
                else [safe_float(c) for c in self.aspheric_coefficients]
            ),
            # Asphere: omit-on-None (a plain Standard/EvenAspheric spec
            # emits None, so an existing all-Standard/Even spec is byte-identical).
            "asphere_surface_type": self.asphere_surface_type,
            "asphere_norm_radius": (
                None if self.asphere_norm_radius is None
                else safe_float(self.asphere_norm_radius)
            ),
            "asphere_max_term": self.asphere_max_term,
        }


@dataclass(frozen=True)
class LensSpec:
    """A whole-system spec: aperture + fields + wavelengths + ordered surfaces."""

    schema_version: int = SCHEMA_VERSION
    aperture_type: str = ""
    aperture_value: float = 0.0
    field_type: str = ""
    fields: Tuple[Tuple[float, float, float], ...] = ()
    wavelength_preset: Optional[str] = None
    wavelengths: Tuple[Tuple[float, float], ...] = ()
    surfaces: Tuple[SurfaceSpec, ...] = ()

    def __post_init__(self):
        if not isinstance(self.aperture_type, str):
            raise ToolParamError("LensSpec.aperture_type must be a string")
        if not isinstance(self.field_type, str):
            raise ToolParamError("LensSpec.field_type must be a string")
        if self.wavelength_preset is not None and not isinstance(
            self.wavelength_preset, str
        ):
            raise ToolParamError("LensSpec.wavelength_preset must be a string or None")
        for s in self.surfaces:
            if not isinstance(s, SurfaceSpec):
                raise ToolParamError("LensSpec.surfaces must be SurfaceSpec instances")
        # Structural minimum: an optical system must have at least OBJECT + IMAGE
        # (BUG3 / attack-surface contract — a 0- or 1-surface spec is degenerate).
        if len(self.surfaces) < 2:
            raise ToolParamError(
                "LensSpec.surfaces requires at least 2 surfaces (OBJECT + IMAGE); "
                f"got {len(self.surfaces)}"
            )
        # At most one aperture stop (BUG4 — an optical system has exactly one stop;
        # multiple is_stop surfaces would be silently resolved to one on apply).
        n_stops = sum(1 for s in self.surfaces if s.is_stop)
        if n_stops > 1:
            raise ToolParamError(
                f"LensSpec.surfaces has {n_stops} is_stop surfaces; at most one "
                "aperture stop is allowed"
            )

    @classmethod
    def from_dict(cls, d):
        if not isinstance(d, dict):
            raise ToolParamError(f"LensSpec must be a dict, got {type(d).__name__}")
        allowed = {f.name for f in dc_fields(cls)}
        unknown = set(d) - allowed
        if unknown:
            raise ToolParamError(f"unknown LensSpec key(s): {sorted(unknown)}")
        surfaces = tuple(
            SurfaceSpec.from_dict(s) for s in d.get("surfaces", ())
        )
        fields_in = _coerce_tuples(
            d.get("fields", ()), length=3, label="fields"
        )
        waves_in = _coerce_tuples(
            d.get("wavelengths", ()), length=2, label="wavelengths"
        )
        preset = d.get("wavelength_preset")
        if preset is not None and not isinstance(preset, str):
            raise ToolParamError(
                f"LensSpec.wavelength_preset must be a string or null, got "
                f"{type(preset).__name__} {preset!r}"
            )
        return cls(
            schema_version=_require_int(
                d.get("schema_version", SCHEMA_VERSION), label="schema_version"
            ),
            aperture_type=_require_str(
                d.get("aperture_type", ""), label="aperture_type"
            ),
            aperture_value=_strict_float(
                d.get("aperture_value", 0.0), label="aperture_value"
            ),
            field_type=_require_str(d.get("field_type", ""), label="field_type"),
            fields=fields_in,
            wavelength_preset=preset,
            wavelengths=waves_in,
            surfaces=surfaces,
        )

    def to_dict(self):
        return {
            "schema_version": self.schema_version,
            "aperture_type": self.aperture_type,
            "aperture_value": safe_float(self.aperture_value),
            "field_type": self.field_type,
            "fields": [list(t) for t in self.fields],
            "wavelength_preset": self.wavelength_preset,
            "wavelengths": [list(t) for t in self.wavelengths],
            "surfaces": [s.to_dict() for s in self.surfaces],
        }

    # ------------------------------------------------------------------ #
    # Composition helpers (the logic; the ToolSpecs are thin wrappers).
    # ------------------------------------------------------------------ #
    @classmethod
    def read(cls, system):
        """Serialize the live ``system`` into a ``LensSpec``."""
        sess = _SystemSession(system)
        lde = system.LDE
        n = int(lde.NumberOfSurfaces)

        surfaces = []
        for i in range(n):
            d = lens_surface.read_surface(sess, {"surface": i})
            surfaces.append(
                SurfaceSpec(
                    radius=d["radius"] if not isinstance(d["radius"], str) else _from_sentinel(d["radius"]),
                    thickness=d["thickness"] if not isinstance(d["thickness"], str) else _from_sentinel(d["thickness"]),
                    # Conic now arrives as the
                    # ``safe_float`` output — a finite float OR the ``"inf"``/``"-inf"``/
                    # ``"nan"`` string sentinel for a CB/inf surface. Route the sentinel
                    # through ``_from_sentinel`` exactly like radius/thickness, so a CB
                    # surface's inf conic never crashes the read (``float("inf")`` would
                    # have worked, but ``float("nan")`` on a string would not — all four
                    # numerics handled identically).
                    conic=d["conic"] if not isinstance(d["conic"], str) else _from_sentinel(d["conic"]),
                    semi_diameter=_read_semi_diameter(d["semi_diameter"]),
                    material=d["material"],
                    comment=d["comment"],
                    is_stop=bool(d["is_stop"]),
                    # Asphere: the S1 read_surface path returns
                    # ``aspheric_coefficients`` (a list of 8 floats) on an EvenAspheric
                    # surface, OMITS the key on a non-asphere, and returns ``None`` +
                    # ``coefficients_unreadable:true`` on a degraded asphere row. Map a
                    # present LIST -> a tuple (a declared asphere); absent/None ->
                    # ``None`` (the don't-touch sentinel — a non-asphere, OR a degraded
                    # read that the S1 M7 graceful-degrade leaves uncarried, NOT a silent
                    # zero).
                    aspheric_coefficients=_read_coefficients(
                        d.get("aspheric_coefficients")
                    ),
                    # Asphere: read_surface returns asphere_surface_type
                    # (omitted on a non-asphere / left absent for an EvenAspheric — the
                    # implicit carry), asphere_norm_radius + asphere_max_term on a gated
                    # Extended surface. A degraded read drops them (coeffs -> None), so the
                    # spec is byte-indistinguishable from a non-asphere (the guard refuses
                    # such a surface up front so it never round-trips silently).
                    asphere_surface_type=d.get("asphere_surface_type"),
                    asphere_norm_radius=_read_asphere_norm(
                        d.get("aspheric_coefficients"), d.get("asphere_norm_radius")
                    ),
                    asphere_max_term=_read_asphere_max_term(
                        d.get("aspheric_coefficients"), d.get("asphere_max_term")
                    ),
                )
            )

        aperture = system.SystemData.Aperture
        fields = system.SystemData.Fields
        wavelengths = system.SystemData.Wavelengths

        field_list = []
        for i in range(1, int(fields.NumberOfFields) + 1):
            f = fields.GetField(i)
            field_list.append((float(f.X), float(f.Y), float(f.Weight)))

        wave_list = []
        for i in range(1, int(wavelengths.NumberOfWavelengths) + 1):
            w = wavelengths.GetWavelength(i)
            wave_list.append((float(w.Wavelength), float(w.Weight)))

        return cls(
            schema_version=SCHEMA_VERSION,
            aperture_type=str(aperture.ApertureType),
            aperture_value=float(safe_float(aperture.ApertureValue))
            if not isinstance(safe_float(aperture.ApertureValue), str)
            else _from_sentinel(safe_float(aperture.ApertureValue)),
            field_type=str(fields.GetFieldType()),
            fields=tuple(field_list),
            wavelength_preset=None,
            wavelengths=tuple(wave_list),
            surfaces=tuple(surfaces),
        )

    @classmethod
    def _reconcile_count(cls, system, target_count):
        """Grow/shrink the LDE to ``target_count`` via the firewall-guarded
        insert/remove tools (never silently skip surfaces, never
        reach an out-of-range engine call).

        Surfaces are inserted/removed at an INTERIOR position (before IMAGE /
        the last interior surface) so OBJECT 0 and IMAGE N-1 are preserved and
        every index stays within the ``insert_surface``/``remove_surface`` bounds
        (firewall reused verbatim — no new engine-call path).
        """
        sess = _SystemSession(system)
        lde = system.LDE
        n = int(lde.NumberOfSurfaces)
        if target_count < 2:
            raise ToolParamError(
                "cannot apply a LensSpec with fewer than 2 surfaces "
                f"(OBJECT + IMAGE); got {target_count}"
            )
        # Grow: insert before IMAGE (at = N-1), which is always a valid interior
        # insert (1 <= N-1) once N >= 2.
        while int(lde.NumberOfSurfaces) < target_count:
            lens_surface.insert_surface(sess, {"at": int(lde.NumberOfSurfaces) - 1})
        # Shrink: remove the last interior surface (at = N-2). The stop guard in
        # remove_surface refuses removing a stop. Clearing IsStop=False on the sole
        # stop is a SILENT NO-OP on the real engine (it keeps exactly one stop —
        # probe_stop_mechanism.py), so we can NOT just clear-then-remove. Instead,
        # if the surface we are about to remove is the stop, MOVE the stop to a
        # DIFFERENT interior surface first (setting IsStop=True there auto-clears
        # the old stop, per real-engine semantics), then remove the now-non-stop
        # surface. If there is no other interior surface to host the stop, refuse —
        # never leave the system stop-less.
        while int(lde.NumberOfSurfaces) > target_count:
            cur = int(lde.NumberOfSurfaces)
            at = cur - 2  # last interior surface (1 <= at <= N-2)
            row = lde.GetSurfaceAt(at)
            if bool(row.IsStop):
                # Find another interior surface (1..cur-2, excluding `at`) to host
                # the stop. After removal `at` disappears, so the new host must be
                # one of the surfaces that survive this step.
                host = next(
                    (i for i in range(1, cur - 1) if i != at), None
                )
                if host is None:
                    raise ToolParamError(
                        "cannot shrink below a stop-bearing minimum: the only "
                        f"interior surface is the stop at {at} and there is no "
                        "other interior surface to host the aperture stop "
                        "(removing it would leave the system stop-less)"
                    )
                # Move the stop to `host` (auto-clears the old stop at `at`), with
                # the set_stop read-back proving the move took effect.
                lens_surface.set_stop_surface(sess, {"surface": host})
            lens_surface.remove_surface(sess, {"at": at})

    @classmethod
    def apply(cls, system, spec):
        """Apply ``spec`` to the live ``system`` via the mutator helpers.

        Reuses set_aperture / set_field / set_wavelength / set_surface /
        substitute_glass / set_stop_surface so bad glass / out-of-range / the
        image-stop no-op raise IDENTICALLY here. The LDE is first grown/shrunk to
        ``len(spec.surfaces)`` via the firewall-guarded insert/remove tools (a
        spec with a different surface count is reconciled, never
        silently truncated).

        The per-surface writes are driven by ``_declared_writes`` — the SINGLE
        source of truth the round-trip verifier ALSO iterates — so apply and
        verify can never diverge on which fields are handled. Every
        ``(surface_index, field, value)`` this yields is written;
        nothing else is touched.
        """
        if not isinstance(spec, LensSpec):
            raise ToolParamError("apply() requires a LensSpec instance")
        sess = _SystemSession(system)

        # Reconcile the surface count FIRST so no surface is silently
        # skipped and no extra-surface index ever reaches an out-of-range insert.
        if spec.surfaces:
            cls._reconcile_count(system, len(spec.surfaces))

        if spec.aperture_type:
            lens_system.set_aperture(
                sess,
                {"aperture_type": spec.aperture_type, "value": spec.aperture_value},
            )
        if spec.field_type:
            field_params = {"field_type": spec.field_type}
            if spec.fields:
                field_params["fields"] = [list(t) for t in spec.fields]
            lens_system.set_field(sess, field_params)
        if spec.wavelength_preset is not None:
            lens_system.set_wavelength(sess, {"preset": spec.wavelength_preset})
        elif spec.wavelengths:
            lens_system.set_wavelength(
                sess, {"wavelengths": [list(t) for t in spec.wavelengths]}
            )

        lde = system.LDE
        n = int(lde.NumberOfSurfaces)
        # Group the declared per-surface writes by surface so geometry fields go
        # through ONE set_surface call (its read-back-as-proof is per-call).
        geom_by_surface = {}
        material_by_surface = {}
        asphere_by_surface = {}
        for i, fname, value in _declared_writes(spec):
            if i > n - 1:
                # The reconcile bounds this, but stay defensive: never index past
                # the live LDE (out-of-range firewall).
                continue
            if fname == "material":
                material_by_surface[i] = value
            elif fname == "asphere":
                # Asphere: NOT a set_surface geometry field nor a material —
                # its OWN arm, delegated to set_asphere (the authoring spine: ChangeType ->
                # the resolved type + per-cell read-back-as-proof). Runs BEFORE the geometry
                # write for this surface (set_surface on an asphere row writes Radius/Conic
                # fine). ``value`` is the (surface_type, coefficients,
                # norm_radius) triple; keyed by surface so the ChangeType happens once.
                asphere_by_surface[i] = value
            else:
                geom_by_surface.setdefault(i, {})[fname] = value

        # S1 GAP-1a: the per-surface HARD-RESET pre-pass — AFTER the declared-writes
        # grouping/count-reconcile, BEFORE the asphere-author + geometry writes, INSIDE the
        # apply's EXISTING SaveAs/LoadFile checkpoint (a reset failure rolls back the whole
        # apply, fail-closed). It (A) reverts an OMITTED-asphere surface (a surface the spec
        # does NOT declare aspheric) from a stale EvenAspheric/Odd/Extended back to Standard
        # (the ChangeType(Standard) clears the coefficient cells for free — probe C), and
        # (B) Fixes any radius/thickness/conic Variable solve the spec does not re-declare
        # (the spec carries no solve state; the caller re-applies set_variable after — P74).
        # Reuses the ``asphere_by_surface`` mapping the apply loop already built (the spec's
        # DECLARED-asphere set — those surfaces are SKIPPED by the reset). Returns the
        # additive disclosure stamped on the SUCCESS envelope.
        reset_disclosure = cls._reset_surfaces_to_spec(system, spec, asphere_by_surface)

        # Asphere FIRST (ChangeType -> the resolved type + author the coefficient cells)
        # so the later set_surface radius/conic write lands on an already-retyped row.
        for i in sorted(asphere_by_surface):
            cls._apply_asphere(sess, i, asphere_by_surface[i])

        for i in sorted(geom_by_surface):
            fields = geom_by_surface[i]
            if i == 0:
                # OBJECT (index 0): set_surface refuses index-0 geometry, so write
                # the declared OBJECT thickness (the object distance) DIRECTLY to row 0
                # with read-back proof (OBJECT thickness is real). External
                # audit: mirror the set_surface OBJECT "thickness-only" firewall
                # on the apply door, but ROUND-TRIP SAFE — a benign read->modify->apply
                # of the OBJECT's DEFAULT geometry/comment (inf radius / 0 conic / auto
                # semi-diameter / "" comment) must STILL apply cleanly. So write ONLY
                # thickness; SKIP radius/conic/semi_diameter/comment entirely (they are
                # not meaningful OBJECT fields and the engine's auto values must be left
                # intact). A SKIPPED field carrying a MEANINGFUL non-default value is
                # surfaced via the additive ``warnings`` envelope (scanned read-only in
                # the success envelope below) — never a rollback.
                if "thickness" in fields:
                    cls._apply_object_fields(
                        system, lde, {"thickness": fields["thickness"]})
            else:
                lens_surface.set_surface(sess, {"surface": i, **fields})

        for i in sorted(material_by_surface):
            glass = material_by_surface[i]
            if not (1 <= i <= n - 1):
                continue
            if glass:
                # A non-empty glass goes through the single validated write path
                # (substitute_glass validates against the catalog).
                lens_glass.substitute_glass(sess, {"surface": i, "glass": glass})
            else:
                # GAP-4: a DECLARED ``material==""`` (air) ACTIVELY clears any
                # pre-existing glass to canonical air (P2: ``Material=""``). The
                # old skip left stale glass in place (a verifier mismatch -> a
                # spurious rollback). A failed clear raises SurfaceWriteError into
                # the EXISTING atomic rollback.
                _clear_material_to_air(lde, i)

        for i, surf in enumerate(spec.surfaces):
            if surf.is_stop and 1 <= i <= n - 2:
                lens_surface.set_stop_surface(sess, {"surface": i})

        # S1 GAP-1a: hand the reset disclosure to the caller (apply_lens_spec) so it can
        # stamp the additive ``surfaces_reset`` / ``solves_reset_to_fixed`` keys on the
        # SUCCESS envelope (present only when non-empty; NEVER on a rollback). A pre-S1
        # caller that ignores the return is unaffected (apply previously returned None).
        return reset_disclosure

    @staticmethod
    def _apply_asphere(sess, i, asphere):
        """Author surface ``i``'s asphere via set_asphere (S2/S3 CARRY).

        DELEGATES to the ``asphere_surface.set_asphere`` authoring spine (ChangeType ->
        the resolved type, RE-FETCH the row, read-back-as-proof per cell, the never-raise
        envelope) — NOT a re-implementation. ``set_asphere`` OWNS the strict write-order
        (norm -> gate -> cells for a gated type). ``asphere`` is the ``(surface_type,
        coefficients, norm_radius)`` triple. A FAILURE is converted to a
        ``SurfaceWriteError`` so it routes into the EXISTING atomic-rollback boundary in
        ``apply_lens_spec``. Passes ``surface`` + ``surface_type`` + ``coefficients`` +
        (gated) ``norm_radius``; the radius/conic land via the subsequent set_surface arm.
        """
        from . import asphere_surface
        surface_type, coefficients, norm_radius = asphere
        call = {
            "surface": i,
            "surface_type": surface_type,
            "coefficients": list(coefficients),
        }
        if norm_radius is not None:
            call["norm_radius"] = norm_radius
        result = asphere_surface.set_asphere(sess, call)
        if not isinstance(result, dict) or not result.get("ok"):
            raise SurfaceWriteError(
                f"apply_lens_spec could not author the {surface_type} coefficients on "
                f"surface {i}: {result!r}",
                field="aspheric_coefficients",
                intended=list(coefficients),
                actual=None,
                surface=i,
            )

    @staticmethod
    def _reset_surfaces_to_spec(system, spec, asphere_by_surface):
        """The S1 GAP-1a per-surface HARD-RESET pre-pass (probe A/C). Inside the apply
        checkpoint; a failure RAISES (-> the existing atomic rollback, fail-closed).

        For EVERY interior/IMAGE surface the spec does NOT declare aspheric (i NOT in
        ``asphere_by_surface``):
          (A) TYPE revert: if the live row IS a Tier-1 asphere
              (``asphere_type_of(row) is not None``) -> ``_revert_to_standard_proven``
              (ChangeType(Standard) + read-back Type==Standard; clears the coefficient
              cells for free — probe C), re-fetch the row, record
              ``{surface, from_type, to_type:"Standard"}``. ``asphere_type_of`` does NOT
              catch a Type-read THROW -> propagates ``AsphereWriteError`` -> rollback.
          (B) SOLVE Fix: for ``RadiusCell``/``ThicknessCell``/``ConicCell`` that read
              ``_cell_is_variable`` -> ``_clear_solve_to_fixed_proven`` + record
              ``{surface, cell}``. NO thickness exclusion here (the spec carries no solve
              state; the caller re-applies set_variable after — P74).

        OBJECT (surface 0) is skipped (never a lens surface). The DECLARED-asphere surfaces
        (``asphere_by_surface`` keys) are SKIPPED — the asphere-author arm re-authors them.
        Returns ``{"surfaces_reset": [...], "solves_reset_to_fixed": [...]}`` (possibly
        empty lists — the caller stamps them only when non-empty).
        """
        from . import asphere_surface as _asph_tool
        lde = system.LDE
        n = int(lde.NumberOfSurfaces)
        variable_member = _oc._solve_type_variable_enum(system)

        surfaces_reset = []
        solves_reset_to_fixed = []
        # Interior + IMAGE surfaces (1..n-1); OBJECT (0) is never an asphere/lens surface.
        # M-1 (DELIBERATE, not a bug): the range INCLUDES the IMAGE surface (n-1), unlike
        # _scan_inert_dofs / _enumerate_lde_variables which stop at n-2. The reset's JOB is to
        # Fix EVERY stale solve so the caller's subsequent set_variable is authoritative (the
        # "spec carries no solve state — P74" contract). A clean IMAGE surface is a pure no-op
        # (no Variable solve -> _cell_is_variable_failclosed returns False); only an IMAGE
        # surface that ACTUALLY carries a Variable solve is touched, and a MakeSolveFixed throw
        # on it routes to the same atomic rollback. The per-cell guards + rollback neutralize
        # the engine-owned-IMAGE-solve hazard. The inert GATE legitimately differs (it refuses
        # only air-air interior radius/conic DOFs).
        for i in range(1, n):
            if i in asphere_by_surface:
                continue  # a DECLARED asphere — the asphere-author arm owns it
            row = lde.GetSurfaceAt(i)
            # (A) TYPE revert: a stale asphere the spec does NOT declare -> Standard.
            # asphere_type_of RAISES AsphereWriteError on a Type read throw (propagates
            # -> the apply rollback, fail-closed — never silently skip a wedged surface).
            type_key = _asph.asphere_type_of(row)
            if type_key is not None:
                _asph_tool._revert_to_standard_proven(system, i)
                surfaces_reset.append(
                    {"surface": i, "from_type": type_key, "to_type": "Standard"}
                )
                row = lde.GetSurfaceAt(i)  # re-fetch (never hold across ChangeType)
            # (B) SOLVE Fix: Fix any radius/thickness/conic Variable solve the spec does
            # not re-declare (Fixes ALL THREE — no thickness exclusion; the spec carries no
            # solve state, the caller re-applies set_variable after).
            for cell_attr, token in (
                ("RadiusCell", "radius"),
                ("ThicknessCell", "thickness"),
                ("ConicCell", "conic"),
            ):
                cell = getattr(row, cell_attr, None)
                if cell is None:
                    continue
                # Detection fails CLOSED here: a GetSolveData throw on a reset surface
                # must propagate (-> rollback), NEVER be swallowed to not-Variable (which would
                # silently skip a stale Variable -> the curved-stop silent-wrong). The
                # NON-mutating inventory/count callers keep _cell_is_variable's fail-OPEN.
                if _oc._cell_is_variable_failclosed(cell, variable_member, i, token):
                    # Read-back PROOF re-fetches the cell from a fresh row (H-1) —
                    # thread lde + cell_attr so the proof never reads a stale pre-mutation proxy.
                    _oc._clear_solve_to_fixed_proven(
                        cell, variable_member, i, token, lde=lde, cell_attr=cell_attr
                    )
                    solves_reset_to_fixed.append({"surface": i, "cell": token})

        return {
            "surfaces_reset": surfaces_reset,
            "solves_reset_to_fixed": solves_reset_to_fixed,
        }

    @staticmethod
    def _apply_object_fields(system, lde, fields):
        """Write the declared OBJECT (index 0) fields with read-back proof.

        ``set_surface`` refuses index-0 geometry (OBJECT is not a lens surface you
        write radii on), so the OBJECT declared writes — chiefly ``thickness`` (the
        object distance) — are written DIRECTLY to row 0 here, with the same
        read-back-as-proof gate every mutator uses (a dropped OBJECT write
        must raise, never silently succeed). Geometry/semi-diameter are floats;
        ``comment`` is a string.

        REFUSAL SITE 2, IN THE FUNCTION BODY. The guard lives HERE
        and not at either call site, and the difference is a shipped-once HIGH: this
        function has TWO callers — apply's OBJECT branch, and ``set_surface(surface=0)``
        by way of ``lens_surface._set_object_surface`` — so guarding a call site leaves
        the other door open. The body is the only place both pass through.

        ``system`` is a REQUIRED parameter with NO default. A defaulted one would make an
        un-updated caller silently skip the probe, which is the same bypass one level
        down; both shipped call sites and the probe script were updated in the same
        commit, and an AST census pins their arity.

        EVERY DECLARED FIELD IS PROBED BEFORE THE FIRST SETATTR — a multi-field OBJECT
        write carrying one driven cell mutates nothing.
        """
        from . import _solve_cells as _sc
        for fname in fields:
            token = fname if fname in _sc.CELL_TOKENS else None
            if token is None:
                continue
            probe = _sc.refuse_if_driven(system, lde, 0, token)
            if probe.get("driven") is not False:
                raise SolveDrivenError.from_probe(
                    probe, cell_token=token, surface=0,
                    tool="apply_lens_spec/set_surface(surface=0)",
                    intended=fields.get(fname))
        for fname, intended in fields.items():
            if fname in _OBJECT_GEOMETRY_TO_PROP:
                prop = _OBJECT_GEOMETRY_TO_PROP[fname]
                row = lde.GetSurfaceAt(0)
                setattr(row, prop, float(intended))
                row = lde.GetSurfaceAt(0)
                actual = float(getattr(row, prop))
                _c._verify_or_raise(fname, float(intended), actual, surface=0)
            elif fname in _OBJECT_STRING_TO_PROP:
                prop = _OBJECT_STRING_TO_PROP[fname]
                row = lde.GetSurfaceAt(0)
                setattr(row, prop, str(intended))
                row = lde.GetSurfaceAt(0)
                actual = str(getattr(row, prop))
                _c._verify_or_raise(fname, str(intended), actual, surface=0)


class _SystemSession:
    """Minimal session adapter exposing ``.system`` for the mutator helpers.

    The tool handlers take ``(session, params)`` and only touch
    ``session.system``; the composition helpers operate on a bare ``system``, so
    this thin adapter lets ``read``/``apply`` reuse them verbatim (no logic fork).
    """

    def __init__(self, system):
        self.system = system


# ILDERow property each writable OBJECT (index-0) field maps to (OBJECT
# fields are written directly to the row, set_surface refuses index 0).
# Geometry fields are floats; comment is a string.
_OBJECT_GEOMETRY_TO_PROP = {
    "radius": "Radius",
    "thickness": "Thickness",
    "conic": "Conic",
    "semi_diameter": "SemiDiameter",
}
_OBJECT_STRING_TO_PROP = {
    "comment": "Comment",
}


def _object_skipped_field_warnings(object_surf):
    """Apply door: the OBJECT (surface 0) accepts thickness only.

    ``apply_lens_spec`` writes ONLY the OBJECT thickness; radius / conic /
    semi_diameter / comment are SKIPPED (the engine's auto values stay intact so a
    benign read->modify->apply round-trip of the OBJECT's defaults is never broken).
    When a SKIPPED field carries a MEANINGFUL non-default value the caller declared,
    surface it as an additive ``warnings`` entry (never a rollback) so the footgun is
    visible, not silent.

    "Meaningful non-default" matches how ``LensSpec.read`` reports each OBJECT field
    (see ``read`` / ``_read_semi_diameter``):
      - ``radius``: a FINITE value (default/round-trip = ``inf``) is meaningful.
      - ``conic``: ``!= 0`` (default = ``0``).
      - ``semi_diameter``: a FINITE POSITIVE non-zero value. The read path emits
        ``None`` for an auto/inf OBJECT semi-diameter and a finite ``0.0`` for an
        engine-resolved/unset one — BOTH are the auto default (a benign round-trip).
        A real caller-set OBJECT aperture is a positive number, so only ``> 0`` is
        meaningful (``None``/``0.0`` stay silent so a pure round-trip never warns).
      - ``comment``: a non-empty string (default = ``""``).
    A default value is silently skipped (no warning — the benign round-trip).
    """
    if object_surf is None:
        return []
    warnings = []
    radius = object_surf.radius
    if isinstance(radius, (int, float)) and not isinstance(radius, bool) and math.isfinite(radius):
        warnings.append(
            f"surface 0 (OBJECT): radius={radius!r} ignored — the OBJECT surface "
            "accepts thickness only (the object distance); a radius is not a "
            "meaningful object-surface field and was NOT written."
        )
    conic = object_surf.conic
    if isinstance(conic, (int, float)) and not isinstance(conic, bool) and conic != 0:
        warnings.append(
            f"surface 0 (OBJECT): conic={conic!r} ignored — the OBJECT surface "
            "accepts thickness only; a conic was NOT written."
        )
    semi = object_surf.semi_diameter
    if (
        semi is not None
        and isinstance(semi, (int, float))
        and not isinstance(semi, bool)
        and math.isfinite(semi)
        and semi > 0
    ):
        warnings.append(
            f"surface 0 (OBJECT): semi_diameter={semi!r} "
            "ignored — the OBJECT surface accepts thickness only; a semi-diameter "
            "was NOT written (the engine resolves it automatically)."
        )
    comment = object_surf.comment
    if isinstance(comment, str) and comment != "":
        warnings.append(
            f"surface 0 (OBJECT): comment={comment!r} ignored — the OBJECT surface "
            "accepts thickness only; a comment was NOT written (use a comment on a "
            "real lens surface)."
        )
    return warnings


# NOTE (S-2): ``_clear_material_to_air`` moved to ``_lens_common`` (the shared L30
# locus consumed by BOTH ``apply_lens_spec`` and ``substitute_glass``'s air arm). It
# is re-exported into this module via the top-level import so the call site above and
# any ``lens_spec._clear_material_to_air`` reference stay valid (back-compat).


#: The pre-scan's entry cap. A design with hundreds of driven cells produces a list no
#: agent reads; the count is what matters past that point, and the truncation is DECLARED
#: (``driven_cells_truncated``) rather than silent.
_DRIVEN_CELLS_CAP = 40

#: Which cells ``_reset_surfaces_to_spec`` can set Fixed before the writes: the three
#: GEOMETRY cells, and NOT ``semi_diameter`` / ``material``.
_RESET_BY_APPLY = ("radius", "thickness", "conic")

#: ...AND ONLY WHEN THE SOLVE IS ``Variable``. MEASURED against the function's own body,
#: which is the correction: its (B) arm tests ``_cell_is_variable`` and clears THAT — so a
#: ``SurfacePickup`` on a re-declared radius SURVIVES the reset and is then refused by the
#: write-time guard, which routes to the atomic rollback.
#:
#: THE WRITTEN RULE IS NARROWER IN FACT THAN IT READS. It says the reset "sets
#: every non-re-declared radius/thickness/conic SOLVE to Fixed"; the code sets every
#: non-re-declared VARIABLE one. Stamping ``will_be_reset_by_apply`` from that wording
#: would have shipped a FALSE promise inside a disclosure — the annotation would
#: have told the agent the solve is about to be cleared while the apply was in fact about
#: to roll back on it. Annotated here, beside the code, rather than upstream.
#:
#: (This makes the DISCLOSURE-not-refusal decision for the pre-scan STRONGER, not weaker:
#: refusing here would refuse designs whose Variable solves apply really does clear.)
_RESET_CLEARS_SOLVE_TYPES = ("Variable",)

#: THE RESET'S ARM (A), MEASURED, AFTER A REVIEW SAID THE ANNOTATION ABOVE
#: WAS STILL INCOMPLETE. THE FINDING IS **FALSIFIED**; THE ANNOTATION ABOVE IS COMPLETE.
#:
#: The claim: ``_reset_surfaces_to_spec`` has a SECOND arm — (A) reverts an omitted-asphere
#: surface to ``Standard`` — and since a probe recorded that "a ``SurfacePickup`` does
#: not survive a retype", that ``ChangeType`` must clear the solve too. On that reading the
#: entry below is inverted: it would promise a rollback while the apply silently deleted a
#: relationship and ran to completion. That is a serious enough shape that the fix was
#: built before measuring, and the LIVE GATE then refused it.
#:
#: MEASURED DIRECTLY by a live probe, driving the reset's OWN
#: ``_revert_to_standard_proven`` call and reading the solve back::
#:
#:     radius     EvenAspheric+SurfacePickup --revert--> Standard+SurfacePickup  SURVIVED
#:     thickness  EvenAspheric+SurfacePickup --revert--> Standard+SurfacePickup  SURVIVED
#:     conic      EvenAspheric+SurfacePickup --revert--> Standard+SurfacePickup  SURVIVED
#:
#: An asphere->Standard revert PRESERVES a driving geometry solve on all three cells. So
#: arm (A) clears NOTHING here, the entry below is CORRECT as written, and the end-to-end
#: apply behaves exactly as its note says: the write door refuses and the whole apply rolls
#: back (the LIVE GATE observes that too, `applied:false, rolled_back:true`).
#:
#: WHY THE QUOTED PROBE RESULT DOES NOT GENERALISE, and this is the transferable part.
#: It was measured while building the non-Standard fixture, on the OTHER direction —
#: Standard -> CB/asphere, where ``add_coordinate_break`` retypes in place. Arm (A) only
#: ever performs asphere-family -> Standard. A retype is not one behaviour; "solves do not
#: survive a retype" was true of the transition that was measured and was applied to a
#: transition that was not.
#:
#: CONSEQUENCE FOR THE REMEDY, and it makes the served text SIMPLER rather than more
#: qualified: since arm (B) clears only ``Variable`` and ``Variable`` is in
#: ``NON_DRIVING``, and arm (A) now measurably clears nothing, **apply's reset can never
#: clear a solve that caused a refusal.** There is no exception to name.
_RESET_ARM_B_NOTE = (
    "apply's reset clears only Variable solves, so this one SURVIVES "
    "it and the write door then refuses — the whole apply rolls back")


def _driven_cells_prescan(system, spec):
    """``(entries, scan_failed)`` — which declared cells carry a driving solve.

    DISCLOSURE ONLY. It changes no decision; it tells the agent WHY a write may behave
    unexpectedly, on the SUCCESS path and — more importantly — on the failing one.

    Consumes ``_declared_writes`` (the SAME generator apply writes from and the
    verifier compares against, so the scan can never drift on which fields exist), and
    probes ALL FIVE ``CELL_TOKENS``. Disclosure is safe for ``material``; its
    divergence hazard is a REFUSAL-semantics hazard only.

    THE FAULT CHANNEL IS THE POINT. A scan-level failure returns ``(None, True)`` so the
    envelope can carry ``driven_cells: null`` + ``driven_scan_failed: true`` — NEVER a
    silently SHORT list, which would read exactly like a clean design. That is the
    variable-lifecycle discipline: a swallowed discovery fault that ships
    as an empty finding is a false clean.
    """
    from . import _solve_cells as _sc
    try:
        lde = system.LDE
        n = int(lde.NumberOfSurfaces)
        entries, seen = [], set()
        for i, fname, _value in _declared_writes(spec):
            if fname not in _sc.CELL_TOKENS or (i, fname) in seen:
                continue
            # A growing spec's rows are UNKNOWABLE, not UNKNOWN-refusable — skip an index
            # the live system does not have rather than reporting a probe fault for it.
            if not (0 <= i < n):
                continue
            # Mirror apply's OBJECT branch: surface 0 takes ``_apply_object_fields``,
            # which writes THICKNESS only, so probing its other cells would disclose a
            # constraint on a write that never happens.
            if i == 0 and fname != "thickness":
                continue
            seen.add((i, fname))
            probe = _sc.refuse_if_driven(system, lde, i, fname)
            driven = probe.get("driven")
            if driven is False:
                continue
            entry = {"surface": i, "cell": fname, "driven": driven,
                     "solve_type": probe.get("solve_type"),
                     "reason": probe.get("reason")}
            if fname in _RESET_BY_APPLY:
                # Keyed on the solve TYPE, not merely the cell — see
                # ``_RESET_CLEARS_SOLVE_TYPES``, and the arm-(A) measurement beside it
                # (a retype PRESERVES a driving geometry solve, so the surface's live
                # type does not enter this prediction). A driving solve apply will NOT
                # clear is told the truth: the write door refuses and the apply rolls back.
                will_reset = probe.get("solve_type") in _RESET_CLEARS_SOLVE_TYPES
                entry["will_be_reset_by_apply"] = will_reset
                if not will_reset:
                    entry["note"] = _RESET_ARM_B_NOTE
            elif fname == "semi_diameter":
                entry["note"] = ("apply's reset does NOT cover semi_diameter; the write "
                                 "door may still refuse and roll the whole apply back")
            elif fname == "material":
                entry["no_guarded_write_door"] = True
                entry["note"] = ("material flows through substitute_glass, which has no "
                                 "driven-cell guard")
            entries.append(entry)
            if len(entries) >= _DRIVEN_CELLS_CAP:
                break
        return entries, False
    except Exception:  # noqa: BLE001 — a scan fault DISCLOSES; it never fails the apply
        return None, True


def _driven_cells_keys(driven_cells, driven_scan_failed):
    """The additive envelope keys, built ONCE and merged into every apply return.

    Wrapped: a malformed probe dict must not convert a clean disclosure into an opaque
    ``internal``, and this helper runs on the SUCCESS path too.
    """
    try:
        if driven_scan_failed:
            return {"driven_cells": None, "driven_scan_failed": True}
        out = {"driven_cells": list(driven_cells or [])}
        if len(out["driven_cells"]) >= _DRIVEN_CELLS_CAP:
            out["driven_cells_truncated"] = True
        return out
    except Exception:  # noqa: BLE001 — a disclosure must never break the envelope
        return {"driven_cells": None, "driven_scan_failed": True}


def _declared_writes(spec):
    """The SINGLE source of truth for per-surface declared writes.

    Yields ``(surface_index, field_name, intended_value)`` for EVERY per-surface
    field the ``spec`` declares as "to set", for EVERY surface INCLUDING OBJECT
    index 0. ``apply`` writes exactly these; the round-trip verifier compares
    exactly these. Because both iterate this one generator, they can never drift
    on which fields are handled (the apply/verify asymmetry this closes).

    Per the SurfaceSpec field contract:
    - ``radius`` / ``thickness`` / ``conic`` — always declared (real number).
    - ``semi_diameter`` — declared ONLY when not ``None`` (``None`` = don't touch).
    - ``material`` — always declared, INCLUDING ``""`` (air): verified even though
      apply has no glass call to make for air.
    - ``comment`` — always declared, INCLUDING ``""`` (clear).

    ``is_stop`` is intentionally NOT yielded: stop placement is a system-wide
    invariant verified by ``_stop_existence_mismatches``, not a per-surface field
    write.
    """
    for i, surf in enumerate(spec.surfaces):
        yield i, "radius", surf.radius
        yield i, "thickness", surf.thickness
        # The slipped-CB-conic tripwire. A coordinate break reads its Conic as a
        # real ``math.inf`` (a self-consistent round-trip). The CB/MIRROR guard
        # (``_unsupported_surfaces``) refuses such a system UP FRONT, so a finite conic
        # is all that should ever reach this write generator. If an ``inf`` conic DOES
        # reach here, the surface-type was lost (the fail-open, or a future CB
        # spelling drift) and writing it into a plain ``set_surface`` / OBJECT Conic
        # would silently clobber a fold. Refuse LOUD rather than declare it for write —
        # a non-finite conic is itself the canary that the CB guard was bypassed.
        if isinstance(surf.conic, float) and not math.isfinite(surf.conic):
            raise ToolParamError(
                f"surface {i} declares a non-finite conic ({surf.conic!r}); a finite "
                "conic is required for a plain-surface write. A coordinate break reads "
                "an inf conic — the CB/MIRROR guard should have refused this system "
                "first; an inf conic reaching the write path means the surface type was "
                "lost — refusing rather than clobbering a fold."
            )
        yield i, "conic", surf.conic
        # Asphere (the LOAD-BEARING anti-silent-pass yield): the even-asphere
        # coefficients are declared ONLY when not None (the don't-touch sentinel — a
        # non-asphere omits them, apply writes nothing, the verify does not compare). BOTH
        # ``_spec_roundtrip_mismatches`` AND ``_post_restore_mismatches`` iterate this one
        # generator, so a coefficient NOT yielded here is certified applied:true /
        # rolled_back:true WITHOUT comparison (the silent-pass hole both audits flagged). The
        # non-finite-coefficient canary (mirroring the inf-conic tripwire above): a non-finite
        # element reaching this write generator RAISES — names a wedged cell read NaN / a
        # non-physical hand-authored coefficient that slipped from_dict, NOT a silent skip and
        # NOT a generic mismatch.
        if surf.aspheric_coefficients is not None:
            for j, c in enumerate(surf.aspheric_coefficients):
                if isinstance(c, float) and not math.isfinite(c):
                    raise ToolParamError(
                        f"surface {i} declares a non-finite asphere coefficient "
                        f"#{j} ({c!r}); a finite coefficient is required for the write. "
                        "A non-finite coefficient means a wedged cell read NaN or a "
                        "non-physical value slipped from_dict — refusing rather than "
                        "writing a non-physical asphere term."
                    )
            # Asphere: the non-finite-NORM canary (the inf-conic / wedged-
            # cell tripwire sibling) — a non-finite norm radius reaching the write
            # generator RAISES rather than silently dropping the normalization.
            if surf.asphere_norm_radius is not None and (
                isinstance(surf.asphere_norm_radius, float)
                and not math.isfinite(surf.asphere_norm_radius)
            ):
                raise ToolParamError(
                    f"surface {i} declares a non-finite asphere_norm_radius "
                    f"({surf.asphere_norm_radius!r}); a finite positive norm radius is "
                    "required — a non-finite norm means a wedged cell read NaN, refusing "
                    "rather than writing a non-physical normalization."
                )
            # Yield the COMBINED asphere write as ONE per-surface entry keyed "asphere"
            # carrying (surface_type_or_EvenAspheric, coefficients, norm_radius) — so
            # apply AND both verifiers iterate the SAME generator (the structural
            # anti-drift fix). An EvenAspheric/Odd carry yields norm=None.
            asph_type = surf.asphere_surface_type or "EvenAspheric"
            yield i, "asphere", (
                asph_type, surf.aspheric_coefficients, surf.asphere_norm_radius
            )
        # ``None`` = don't touch. A numeric ``inf`` straggler (a hand-authored
        # dict that still declares an inf semi-diameter, after ``from_dict`` coerces the
        # ``"inf"`` sentinel) is ALSO skipped (D1/3a, probe F2: writing the engine-owned
        # OBJECT/auto semi-diameter is a SILENT no-op that reads back inf -> the
        # ``intended=0.0 actual=inf`` trap). A FINITE semi-diameter STILL round-trips
        # (set + verified on EVERY surface) — the exemption keys on ``inf``, never on
        # the index / OBJECT/IMAGE role.
        # 3a: exempt ONLY a real POSITIVE-inf auto semi-diameter (engine-owned, the
        # unwritable F2 trap). ``math.isinf`` alone also matches ``-inf``, which would
        # SILENTLY drop a ``semi_diameter:"-inf"`` write; a negative/nan semi-diameter
        # is invalid and is rejected LOUD up front in ``SurfaceSpec`` (so it never
        # reaches here), but the exemption is still keyed on ``v > 0`` for correctness.
        if surf.semi_diameter is not None and not (
            isinstance(surf.semi_diameter, float)
            and math.isinf(surf.semi_diameter)
            and surf.semi_diameter > 0
        ):
            yield i, "semi_diameter", surf.semi_diameter
        yield i, "material", surf.material   # "" = air, still declared
        yield i, "comment", surf.comment     # "" = clear, still declared


def _strict_float(value, *, label):
    """Coerce a numeric JSON value to float, REJECTING bools and non-numbers.

    A bare ``float(...)`` silently turns a JSON ``true`` into ``1.0`` and a numeric
    string into a number; both are malformed payloads we must reject rather than
    coerce. ``bool`` is an ``int`` subclass, so it is screened first.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolParamError(
            f"{label} must be a number, got {type(value).__name__} {value!r}"
        )
    return float(value)


def _require_bool(value, *, label):
    """Require a REAL ``bool``: reject "true"/"false" strings and 0/1 ints."""
    if not isinstance(value, bool):
        raise ToolParamError(
            f"{label} must be a bool (true/false), got {type(value).__name__} "
            f"{value!r}"
        )
    return value


def _require_str(value, *, label):
    """Require a ``str``: reject numbers/bools silently stringifying."""
    if not isinstance(value, str):
        raise ToolParamError(
            f"{label} must be a string, got {type(value).__name__} {value!r}"
        )
    return value


def _require_int(value, *, label):
    """Require an integer: accept int (not bool), reject everything else."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolParamError(
            f"{label} must be an integer, got {type(value).__name__} {value!r}"
        )
    return value


def _coerce_tuples(seq, *, length, label):
    """Coerce a sequence of fixed-length numeric tuples, rejecting malformed entries."""
    if not isinstance(seq, (list, tuple)):
        raise ToolParamError(
            f"{label} must be a list of {length}-number lists, got "
            f"{type(seq).__name__}"
        )
    out = []
    for entry in seq:
        if not isinstance(entry, (list, tuple)) or len(entry) != length:
            raise ToolParamError(
                f"{label} entries must be {length}-number lists, got {entry!r}"
            )
        out.append(
            tuple(_strict_float(v, label=f"{label} entry") for v in entry)
        )
    return tuple(out)


def _coerce_inf(value, *, label):
    """Coerce a JSON value to float, accepting only the 'inf'/'-inf'/'nan' sentinels.

    A string that is not a recognized sentinel is REJECTED (not passed to
    ``float``), and a bool is rejected — only real numbers and the
    sentinels are accepted.
    """
    if isinstance(value, str):
        return _from_sentinel(value)
    return _strict_float(value, label=label)


def _read_semi_diameter(value):
    """Map a read-back semi-diameter to the spec value, exempting ``inf`` (D1/3a).

    ``value`` is the ``safe_float`` output of ``read_surface`` — a finite number, or
    the string sentinel ``"inf"``/``"-inf"``/``"nan"`` for a non-finite engine value.

    An ``inf`` semi-diameter is ENGINE-OWNED/auto (probe F1/F2: a non-zero angle field
    with an object at infinity makes the engine auto-resolve the OBJECT — and any
    field-tracing — semi-diameter to ``inf``, and writing ANY value to it is a SILENT
    no-op). Declaring it on apply is doomed to the ``intended=0.0 actual=inf`` read-back
    trap. So we emit the don't-touch sentinel ``None`` for an inf-resolving surface —
    keyed on the VALUE (covers OBJECT, IMAGE, any inf surface), NEVER on the index. A
    FINITE semi-diameter is captured verbatim and STILL round-trips (a real caller-set
    aperture is preserved — the exemption is inf-only, not "it's the OBJECT/IMAGE").
    """
    if isinstance(value, str):
        coerced = _from_sentinel(value)
    else:
        coerced = value
    if isinstance(coerced, float) and math.isinf(coerced):
        return None  # inf = engine-owned/auto -> don't-touch (the unwritable trap, F2)
    return coerced


def _read_coefficients(value):
    """Map a ``read_surface`` ``aspheric_coefficients`` value -> the spec field (S2).

    ``read_surface`` returns a LIST of 8 floats on an EvenAspheric surface, ``None`` on a
    degraded asphere (``coefficients_unreadable``), and the key is ABSENT (``.get`` ->
    ``None``) on a non-asphere. A list -> a tuple (a declared asphere round-trips); a
    ``None`` / absent -> ``None`` (the don't-touch sentinel — a non-asphere OR a degraded
    read carries None, NEVER a silent zero). Each entry is coerced to float (the live read
    already gives floats; this is defensive).
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return tuple(float(c) for c in value)
    return None


def _read_asphere_norm(coeffs_value, norm_value):
    """Map a read-back norm radius to the spec field (S3 CARRY).

    Returns ``None`` if the coefficients did not read back (a degraded asphere -> the
    whole triple drops, keeping ``__post_init__`` consistent) or if the norm is absent
    (a non-gated Odd/Even surface). Otherwise the float (the read-back value, possibly
    a ``safe_float`` string sentinel for a non-finite read -> coerced back).
    """
    if not isinstance(coeffs_value, (list, tuple)) or norm_value is None:
        return None
    if isinstance(norm_value, str):
        return _from_sentinel(norm_value)
    return float(norm_value)


def _read_asphere_max_term(coeffs_value, max_term_value):
    """Map a read-back Max-Term to the spec field (S3 CARRY).

    Returns ``None`` if the coefficients did not read back (degraded) or the gate is
    absent (a non-gated surface); otherwise the int.
    """
    if not isinstance(coeffs_value, (list, tuple)) or max_term_value is None:
        return None
    return int(max_term_value)


def _coerce_coefficients(value):
    """Coerce a JSON ``aspheric_coefficients`` value -> a tuple of floats, or None (S2).

    ``None`` / absent -> ``None`` (the don't-touch sentinel — a non-asphere surface).
    A list/tuple -> a tuple of floats (shape + finiteness validated downstream in
    ``SurfaceSpec.__post_init__`` via the S1 ``_validate_coefficients`` — a non-array /
    >8 / non-finite RAISES there, so this helper only normalizes the container). A
    non-list/tuple value passes through to ``__post_init__`` which RAISES ``ToolParamError``
    via ``_validate_coefficients`` (the carrier is an ORDERED array).
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple)) and not isinstance(value, str):
        return tuple(value)
    # A non-array (e.g. a number / dict / str): hand it through unchanged so
    # __post_init__'s _validate_coefficients raises the proper ToolParamError.
    return value


def _from_sentinel(value):
    """Map the string sentinels back to float infinities (planar)."""
    if value == "inf":
        return math.inf
    if value == "-inf":
        return -math.inf
    if value == "nan":
        return math.nan
    raise ToolParamError(f"unrecognized numeric sentinel {value!r}")


# --------------------------------------------------------------------------- #
# The special-surface fail-closed guard.
# FLIPPED from a deny-list to a POSITIVE ALLOW-LIST.
# --------------------------------------------------------------------------- #

# The POSITIVE allow-set (the guard flip). A surface is
# representable by the flat ``SurfaceSpec`` ONLY if its ``row.Type`` is one of these —
# the surface types whose data is EXACTLY the Standard column set the flat schema
# carries (``radius / thickness / conic / semi_diameter / material / comment /
# is_stop``). The default is fail-CLOSED: ``Standard`` ONLY. A genuinely
# column-only type (e.g. ``Paraxial``/``ParaxialXY``) may be ADDED here ONLY with a
# per-type proof that the flat schema carries all its data — never speculatively.
# Asphere (the GUARD PROMOTION, refuse->carry): EXACTLY ``"EvenAspheric"``
# is added (set-membership, NEVER a ``"Asphere"`` substring — that would silently admit
# ExtendedAsphere/OddAsphere/ExtendedOddAsphere whose coefficients live in a DIFFERENT
# table (Extended Data) the flat ``aspheric_coefficients`` field does NOT carry, and FLATTEN
# them on read->apply). An EvenAspheric surface now ROUND-TRIPS through apply_lens_spec —
# but ONLY because S2a's schema+verify now CARRY the 8 Par coefficient cells (the
# ``aspheric_coefficients`` field, ``_declared_writes`` yield, the per-index asphere-floor
# verify, and the set_asphere-delegating apply arm). Representable IFF Standard-column-only
# OR EvenAspheric-with-coefficients-carry. EvenAspheric+MIRROR stays REFUSED (the MIRROR arm
# runs first); a degraded-Type EvenAspheric stays REFUSED (the S1 H-2 fail-closed — a thrown
# Type cannot be PROVEN EvenAspheric, it could be an Extended/Odd asphere).
_REPRESENTABLE_TYPES = frozenset({
    "Standard", "EvenAspheric", "OddAsphere", "ExtendedAsphere", "ExtendedOddAsphere",
})


def _unsupported_surfaces(system):
    """Scan the live LDE for surfaces the flat ``SurfaceSpec`` schema cannot carry.

    POSITIVE ALLOW-LIST. The flat ``SurfaceSpec`` carries
    EXACTLY the Standard column set (``radius / thickness / conic / semi_diameter /
    material / comment / is_stop``). ANY surface type with data BEYOND those columns —
    Par-cell coefficients (EvenAspheric/OddAsphere), Extended-Data arrays
    (ExtendedAsphere/QType), tilt/decenter (CoordinateBreak), Lines-µm/Order
    (DiffractionGrating), a norm radius, etc. — is NOT faithfully representable: a
    ``read -> apply`` round-trip would SILENTLY drop the extra data and flatten the
    surface to a plain Standard. So "Standard-representable" IS "Standard-column-only",
    and the honest invariant is a POSITIVE test for membership in
    ``_REPRESENTABLE_TYPES``, NOT a hand-maintained deny-list of the few special types
    the harness happens to author today (L32 — enforce the invariant directly, not a
    proxy). The deny-list was a proxy that silently admitted EVERY type nobody
    remembered to add (EvenAspheric was the proof: it round-tripped silently-wrongly).
    The flip closes the WHOLE silent-flatten class in one change.

    This is strictly SAFER, never a regression: the ONLY non-Standard types that passed
    the old deny-list AND were faithfully representable are NONE (every non-Standard
    powered type carries extra data the flat schema drops; a normal refractive/reflective
    lens is built entirely from ``Standard`` surfaces plus the already-refused CB/mirror/
    grating folds). So a ``Standard``-only allow-list refuses NO system that today
    round-trips faithfully; it only newly-refuses the systems that today round-trip
    SILENTLY-WRONGLY (EvenAspheric and its siblings). Verified by the false-positive
    baseline anchor (a plain all-Standard glass/air system passes with zero refusals).

    The two facts are read INDEPENDENTLY (``Type`` and ``Material``):
      - MIRROR (the ``Material`` fact) is refused FIRST — a non-Type fact: a MIRROR on
        an otherwise-Standard surface is still not a catalog glass (``substitute_glass``
        rejects it), so it is unrepresentable regardless of Type.
      - Then ``type_name in _REPRESENTABLE_TYPES`` -> REPRESENTABLE.
      - Else (any other READABLE Type) -> REFUSE. The two harness-authored special types
        keep their SPECIFIC, honest reasons (``"coordinate_break"`` /
        ``"diffraction_grating"``); every OTHER readable non-Standard type (EvenAspheric,
        OddAsphere, ExtendedAsphere, Biconic, Zernike, Toroidal, …) is refused with
        ``reason:"unsupported_surface_type"`` and the ``type_name`` surfaced so the
        envelope NAMES it (e.g. ``"EvenAspheric"``).
      - A Type read that THREW -> FAIL CLOSED: a degraded Type can
        no longer be proven Standard-column-only by a catalog glass, because an aspheric
        LENS element carries a real catalog glass (an aspheric N-BK7) and would otherwise be
        SILENTLY FLATTENED on a read->apply round-trip. The surface is REFUSED; the reason
        is enriched to ``unsupported_surface_type`` (``EvenAspheric``) when the coefficient
        cells positively read an authored asphere, else the generic ``unreadable``.

    Returns a list of ``{surface, reason, type_name, material}`` rows (one per
    unrepresentable surface). NEVER raises: a per-surface read throw degrades that one
    row to a ``"unreadable"`` reason (fail closed — we cannot prove it is representable).
    An empty list means the system is representable.

    FULL TRUTH TABLE (the allow-list flip). The guard reads two
    facts INDEPENDENTLY (``Type`` and ``Material``). ONLY a POSITIVELY-proven plain
    surface is representable; everything else fails CLOSED.

      Type READABLE (the specific Type arms run FIRST, then MIRROR, then the allow-list):
        Type contains "CoordinateBreak"          -> REFUSE coordinate_break (specific reason).
        Type contains "DiffractionGrating"       -> REFUSE diffraction_grating (specific reason,
                                                    runs BEFORE MIRROR so a REFLECTIVE grating
                                                    reports as a grating — the committed CB
                                                    grating-before-mirror precedent).
        Material == "MIRROR" (Type not CB/grating) -> REFUSE mirror (a non-Type fact: a MIRROR
                                                    is not a catalog glass on any Type; placed
                                                    before the allow-list so an EvenAspheric-
                                                    MIRROR reads as mirror — spec M3).
        Type in _REPRESENTABLE_TYPES ({Standard}) & Material readable & not MIRROR
                                                 -> REPRESENTABLE (the POSITIVE allow-list).
        Type readable, NOT in _REPRESENTABLE_TYPES (EvenAspheric/OddAsphere/Extended/
          Biconic/Zernike/Toroidal/…)            -> REFUSE unsupported_surface_type, the
                                                    type_name surfaced so the envelope names it.
        Type representable (Standard) & Material UNREADABLE -> REFUSE unreadable (a wedged
                                                    Material on an otherwise-plain row).

      Type UNREADABLE (the ``row.Type`` read THREW — FAIL CLOSED):
        A thrown Type read means we CANNOT prove the surface is Standard-column-only. The
        PRIOR rule admitted a catalog-glass Material as a positive plain proof ("a
        grating/CB never reads a glass name; only a plain glass surface does") — but that is
        now FALSE: an aspheric LENS element (EvenAspheric and siblings) carries a real
        catalog glass, so a Type-throwing asphere-with-glass would be admitted and SILENTLY
        FLATTENED on read->apply (the coefficients dropped). So a degraded Type is now
        always REFUSED (over-refusing a live-rare wedged plain surface, which is acceptable
        — a normal surface never throws on its Type read):
          Material is "MIRROR"                     -> REFUSE mirror (the MIRROR arm above
                                                     runs before this degraded path).
          coefficient cells read a NON-ZERO asphere term -> REFUSE unsupported_surface_type
                                                     (EvenAspheric — the enriched reason).
          else (any Material — glass / blank air / dash / unreadable / non-glass token)
                                                  -> REFUSE unreadable (fail closed).

    Rationale (not over-refusing): on the live engine ``row.Type`` NEVER throws for a normal
    surface (every row answers Type — the live gates). A thrown Type is a wedged-row
    ANOMALY, so refusing a system with a wedged interior row is correct — far better than
    silently flattening a special surface. A normal all-glass/air ``Standard`` system (Type
    reads fine) is in ``_REPRESENTABLE_TYPES`` and is representable (the false-positive guard).
    """
    # MCE guard arm: a multi-config system is a WHOLE-SYSTEM
    # property (the per-config cell matrix has no home in the flat SurfaceSpec), so this
    # is a SYSTEM-LEVEL pre-check that runs BEFORE the per-surface walk. A read->apply of
    # an N-config system would SILENTLY FLATTEN every config to config 1 (the CB/asphere
    # silent-flatten class). Refuse fail-closed (an unreadable NumberOfConfigurations ->
    # fail closed, never assume single-config). Returns the diagnostic row that routes to
    # the existing ``lens_spec_unsupported`` envelope (reason "multi_config", a NEW value
    # of the existing family — NOT a new family). Default REFUSE (no carry path in S1).
    try:
        n_cfg = int(system.MCE.NumberOfConfigurations)
    except Exception:  # noqa: BLE001 — an unreadable MCE -> fail closed
        return [{"surface": None, "reason": "multi_config_unreadable",
                 "error": "could not read MCE.NumberOfConfigurations"}]
    if n_cfg > 1:
        return [{"surface": None, "reason": "multi_config", "type_name": None,
                 "material": None, "n_configs": n_cfg,
                 "error": (f"system has {n_cfg} configurations; the flat LensSpec carries "
                           "only one config's values and a read->apply would SILENTLY "
                           "FLATTEN them to config 1. Refusing. Use "
                           "describe_configurations / set_config_* to read/author "
                           "per-config values")}]

    try:
        # The ``system.LDE`` deref is INSIDE the try so a
        # deref throw is contained here (one diagnostic row) rather than relying on the
        # caller's wrapper — _unsupported_surfaces must never raise.
        lde = system.LDE
        n = int(lde.NumberOfSurfaces)
    except Exception as exc:  # noqa: BLE001 — an unreadable LDE/count -> one diagnostic row
        return [{"surface": None, "reason": "unreadable",
                 "error": f"could not read the LDE / NumberOfSurfaces ({exc!r})"}]
    found = []
    for i in range(n):
        try:
            row = lde.GetSurfaceAt(i)
        except Exception as exc:  # noqa: BLE001 — a wedged row fetch -> fail closed
            found.append({
                "surface": i, "reason": "unreadable",
                "error": f"could not fetch surface {i} ({exc!r})",
            })
            continue
        # Read the two unsupported-surface facts INDEPENDENTLY (a row that throws on
        # Type may still answer Material, and vice-versa). The CB fact comes from Type,
        # the mirror fact from Material — neither read should suppress the other.
        type_read_failed = False
        try:
            type_name = str(row.Type)
        except Exception:  # noqa: BLE001 — Type unreadable: the CB fact is unknown
            type_name = None
            type_read_failed = True
        material_read_failed = False
        try:
            material = str(row.Material)
        except Exception:  # noqa: BLE001 — Material unreadable: the mirror fact is unknown
            material = None
            material_read_failed = True

        # The two harness-authored special Type arms keep their SPECIFIC, honest reasons
        # (a coordinate break / a diffraction grating) and run FIRST — BEFORE the MIRROR
        # (Material) arm — so a REFLECTIVE grating (Type=DiffractionGrating, Material=MIRROR)
        # reports as a GRATING, the more specific + actionable reason (the committed CB
        # grating-before-mirror precedent — a reflective grating's recovery is
        # set_diffraction_grating, not "it's a mirror"). The allow-list refusal below is a
        # GENERAL net; these two are its named refinements.
        if type_name is not None and "CoordinateBreak" in type_name:
            found.append({
                "surface": i, "reason": "coordinate_break",
                "type_name": type_name, "material": material,
            })
            continue
        if type_name is not None and "DiffractionGrating" in type_name:
            found.append({
                "surface": i, "reason": "diffraction_grating",
                "type_name": type_name, "material": material,
            })
            continue
        # MIRROR (the Material fact) — a non-Type fact: a MIRROR on a surface whose Type is
        # NOT a more-specific special type (CB/grating handled above) is still not a catalog
        # glass (substitute_glass rejects it), so it is unrepresentable regardless of Type.
        # Placed BEFORE the allow-list so an EvenAspheric-MIRROR (a reflective asphere) is
        # named by its more-actionable MIRROR reason rather than the generic
        # unsupported_surface_type (spec M3). A plain Standard + MIRROR also lands
        # here (the existing set_mirror round-trip refusal, unchanged).
        if material is not None and material.strip().upper() == "MIRROR":
            found.append({
                "surface": i, "reason": "mirror",
                "type_name": type_name, "material": material,
            })
            continue
        # THE POSITIVE ALLOW-LIST. A readable Type is
        # representable ONLY if it is in _REPRESENTABLE_TYPES ({Standard}). EVERY OTHER
        # readable non-Standard type — EvenAspheric, OddAsphere, ExtendedAsphere, Biconic,
        # Zernike, Toroidal, … (the probe enumerated 82 members; nobody has to remember to
        # deny-list each one) — carries data beyond the flat schema's columns, so a
        # read->apply would SILENTLY flatten it. Refuse with the type_name surfaced so the
        # envelope NAMES the offending type (e.g. "EvenAspheric"). This is the L32 direct
        # invariant: representable IFF Standard-column-only, tested positively.
        if type_name is not None and type_name not in _REPRESENTABLE_TYPES:
            found.append({
                "surface": i, "reason": "unsupported_surface_type",
                "type_name": type_name, "material": material,
            })
            continue
        # The DEGRADED-CELL fail-closed arm (S2a H-1 — symmetric to the degraded-TYPE
        # arm below). An ``EvenAspheric`` surface is NEWLY admitted by the allow-list
        # (the S2a guard promotion), but it is faithfully representable ONLY IF its 8
        # Par coefficient cells can actually be READ — the carry (``LensSpec.read`` ->
        # ``_read_coefficients``) needs them. If the Type reads cleanly as EvenAspheric
        # but the coefficient cells THROW / are unreadable (a wedged Par-cell read),
        # ``LensSpec.read`` graceful-degrades coeffs -> ``None`` and DROPS the
        # ``coefficients_unreadable`` flag, so the emitted spec is byte-indistinguishable
        # from a plain Standard surface — a read->apply would SILENTLY FLATTEN the asphere
        # (surface stays Standard, set_asphere never called) yet certify applied:true with
        # zero mismatch. The degraded-TYPE arm already fails CLOSED; this closes the
        # symmetric degraded-CELL hole. REFUSE rather than admit->flatten — the S1+S2a
        # invariant: admit an EvenAspheric ONLY if it can be FAITHFULLY round-tripped. A
        # HEALTHY EvenAspheric (cells read fine) is still admitted (falls through). The
        # coefficient probe is fully THROW-GUARDED (``_even_asphere_coefficients_unreadable``
        # never raises) so this arm never breaks the never-raise boundary.
        s3_key = _asph.asphere_type_of_name(type_name) if type_name is not None else None
        if s3_key is not None and _s3_asphere_unreadable(system, row, s3_key):
            found.append({
                "surface": i, "reason": "unsupported_surface_type",
                "type_name": type_name, "material": material,
                "error": (
                    f"surface {i} reads as {type_name} but its asphere coefficient cells "
                    "(or the Max-Term gate / Norm Radius for a gated type) could not be "
                    "read (a wedged/drifted Par cell). The flat LensSpec carries these, "
                    "but an unreadable cell would be carried as None with NO disclosure "
                    "and SILENTLY FLATTENED to a plain Standard surface on a read->apply "
                    "round-trip. Refusing rather than admitting a surface the round-trip "
                    "cannot faithfully carry. Re-author the asphere with set_asphere"
                ),
            })
            continue
        # The degraded-Type fail-closed arm (H-2 — TIGHTENED for the asphere class).
        # When the ``row.Type`` read THREW we CANNOT prove the surface is Standard-column-
        # only, so we FAIL CLOSED. The PRIOR rule admitted a degraded-Type surface as plain
        # IF its Material was a recognized catalog glass — the premise being "a grating/CB
        # never reads a catalog glass name; only a plain glass surface does." That premise
        # is now FALSE: once even aspheres are authorable, an aspheric
        # LENS element carries a REAL catalog glass (e.g. an aspheric N-BK7). So a
        # Type-throwing EvenAspheric-with-glass would be admitted as plain by glass alone and
        # SILENTLY FLATTENED on a read->apply round-trip (the coefficients dropped). CB reads
        # ``Material == "-"`` and a transmissive grating reads ``Material == ""`` so they were
        # already caught; the asphere is the new case that breaks "catalog glass proves
        # plain." A catalog glass NO LONGER proves a degraded-Type surface is Standard-only.
        #
        # The DECISION is therefore always REFUSE on a degraded Type (a degraded Type can
        # never be proven Standard-column-only — it could be an EvenAspheric / OddAsphere /
        # ExtendedAsphere / … carrying glass, all of which the flat schema would flatten).
        # This over-refuses a genuinely-plain glass lens whose Type read happens to throw —
        # but a normal surface NEVER throws on its Type read (every live gate confirms
        # it), so a thrown Type is a wedged-row ANOMALY: refusing it is correct, far better
        # than silently flattening a special surface. We ENRICH the reason when the
        # coefficient cells positively read an authored asphere (a more actionable message),
        # else the generic degraded refusal.
        if type_read_failed:
            if _degraded_row_detected_asphere(system, row):
                found.append({
                    "surface": i, "reason": "unsupported_surface_type",
                    "type_name": "EvenAspheric", "material": material,
                    "error": (
                        f"could not read surface {i} Type, but its coefficient cells read "
                        "back a non-zero even-asphere term — it is an authored EvenAspheric "
                        "carrying catalog glass; the flat LensSpec would SILENTLY FLATTEN it "
                        "on a read->apply round-trip (the coefficients dropped). Refusing. "
                        "Re-author the asphere with set_asphere after the round-trip"
                    ),
                })
            else:
                found.append({
                    "surface": i, "reason": "unreadable",
                    "type_name": None, "material": material,
                    "error": (
                        f"could not read surface {i} Type; with the Type unreadable the "
                        f"surface cannot be proven Standard-column-only (its Material "
                        f"{material!r} does not prove plain — once aspheres are authorable a "
                        "catalog glass no longer proves a plain surface, and a transmissive "
                        "grating reads blank air / a coordinate break reads the dash, none "
                        "of which can be ruled out). Refusing rather than risking a silent "
                        "flatten on a read->apply round-trip"
                    ),
                })
            continue
        # Type read OK and is not a CB. Fail closed only when the Material fact is ALSO
        # unreadable (a genuinely wedged row we cannot prove is a plain lens surface).
        # A readable non-CB Type is itself sufficient proof the surface is representable.
        if material_read_failed:
            found.append({
                "surface": i, "reason": "unreadable",
                "type_name": type_name, "material": None,
                "error": f"could not read surface {i} Material",
            })
    return found


def _even_asphere_coefficients_unreadable(system, row):
    """True iff ANY of ``row``'s 8 even-asphere coefficient cells cannot be read (S2a H-1).

    The fail-closed discriminator for a NEWLY-admitted EvenAspheric surface (Type reads
    cleanly): the carry (``LensSpec.read`` -> ``_read_coefficients``) reads all 8 Par
    cells via ``read_asphere_cell``. If even ONE cell THROWS (a wedged / Header-drifted
    Par cell), the read graceful-degrades to ``None`` and the asphere is INDISTINGUISHABLE
    from a plain Standard surface — a read->apply would silently flatten it. This probe
    reads the SAME cells with the SAME reader ``LensSpec.read`` uses, so an
    unreadable-here cell is exactly the unreadable-there case that would flatten.

    Returns True (-> REFUSE) if any cell read raises; False (-> the asphere is admittable)
    when ALL 8 cells read fine. Fully THROW-GUARDED: ``_unsupported_surfaces`` must never
    raise, and this is a pure read (no mutation — ``read_asphere_cell`` only reads
    ``cell.DoubleValue``), so probing here does not perturb the later ``LensSpec.read``.
    """
    try:
        for param in _asph._PARAM_NAMES:
            _asph.read_asphere_cell(system, row, param)
        return False
    except Exception:  # noqa: BLE001 — ANY coefficient cell unreadable -> refuse (fail closed)
        return True


def _s3_asphere_unreadable(system, row, type_key):
    """True iff a Tier-1 asphere's carry cells cannot ALL be read (S3 degraded-cell HIGH).

    The type-aware sibling of ``_even_asphere_coefficients_unreadable``: for a GATED
    (Extended) type it probes the Max-Term gate (Par13), the Norm Radius (Par14), AND the
    ``max_term`` materialized coefficient cells — the SAME cells ``LensSpec.read`` reads.
    For a non-gated (Odd/Even) type it probes the fixed Par1..Par(max_terms) cells. If
    ANY throws, the carry would graceful-degrade coeffs -> ``None`` and the asphere would
    be SILENTLY FLATTENED on a read->apply — so REFUSE (fail closed). Returns True
    (-> REFUSE) if any cell read raises; False (-> admittable) when all read fine. Fully
    THROW-GUARDED (``_unsupported_surfaces`` must never raise; this is a pure read).
    """
    info = _asph.ASPHERE_TYPE_INFO[type_key]
    try:
        if info.gated:
            max_terms = _asph.read_gate_cell(system, row, info)
            _asph.read_computed_double_cell(system, row, info.norm_par, _asph._NORM_HEADER)
            for k in range(max_terms):
                _asph.read_computed_double_cell(
                    system, row, info.coeff_par(k), info.header(k)
                )
        else:
            for k in range(info.max_terms):
                _asph.read_computed_double_cell(
                    system, row, info.coeff_par(k), info.header(k)
                )
        return False
    except Exception:  # noqa: BLE001 — ANY gate/norm/coeff cell unreadable -> refuse
        return True


def _degraded_row_detected_asphere(system, row):
    """True iff ``row`` can be POSITIVELY detected as an authored even asphere (H-2).

    Probes the eight even-asphere coefficient cells (Par1..Par8) on a row whose ``Type``
    read THREW, to ENRICH the refusal reason. Returns True ONLY when a coefficient cell
    is present with the expected asphere Header AND reads back a NON-ZERO value (an
    authored EvenAspheric). Returns False otherwise (a plain Standard surface's Par cells
    have non-asphere "Par N(unused)" Headers so ``read_asphere_cell`` RAISES —
    section G; that raise is caught here and yields False, i.e. "no positive asphere
    detected"). This is ONLY a reason-enricher: the degraded-Type DECISION is always
    fail-closed (see ``_unsupported_surfaces``), regardless of this probe's result.

    Fully THROW-GUARDED: ``_unsupported_surfaces`` must never raise.
    """
    try:
        for param in _asph._PARAM_NAMES:
            value = _asph.read_asphere_cell(system, row, param)
            if value is not None and value != 0.0:
                return True
        return False
    except Exception:  # noqa: BLE001 — non-asphere/wedged Par cell -> not positively detected
        return False


# NOTE (dead-code removal): a former helper
# ``_material_proves_plain_surface`` lived here. It encoded the SUPERSEDED PRIOR rule that a
# degraded-Type surface could be admitted as plain IF its Material was a recognized catalog
# glass. That rule is no longer current behavior: once even aspheres are authorable, an
# aspheric LENS carries a real catalog glass, so "catalog glass proves plain" would silently
# flatten a Type-throwing EvenAspheric. The H-2 fix made the degraded-Type DECISION
# always-refuse (see ``_unsupported_surfaces`` line ~899), which routed around the helper and
# left it with zero production callers. It is therefore removed; the always-refuse arm is the
# live rule. (The prior catalog-glass-proves-plain reasoning is preserved as history in the
# truth-table comment above the degraded-Type arm.)


def _lens_spec_unsupported_envelope(tool, surfaces):
    """Build the structured ``lens_spec_unsupported`` refusal (never raises).

    The guard flip: the flat LensSpec carries EXACTLY the Standard
    column set, so it refuses ANY surface type with extra data — coordinate breaks,
    diffraction gratings, MIRROR materials, AND aspheres (EvenAspheric/… Par-cell
    coefficients) + every other non-Standard powered type. The ``surfaces`` list names
    each offending surface with its ``reason`` and ``type_name``.
    """
    return {
        "ok": False,
        "error_family": "lens_spec_unsupported",
        "error": (
            "this system contains surface types the flat LensSpec schema cannot "
            "represent (it carries only the Standard columns: radius / thickness / conic "
            "/ semi-diameter / material / comment / is_stop). Refusing rather than "
            "silently flattening the design. The offending surfaces are listed with a "
            "reason + type_name: a coordinate break, a diffraction grating, a MIRROR "
            "material, an EvenAspheric/aspheric surface (Par-cell coefficients), or any "
            "other non-Standard powered type. Use describe_surfaces to read the system; "
            "author folds with the coordinate-break tools (add_coordinate_break / "
            "add_return_cb / set_cb_variable), a grating with set_diffraction_grating, "
            "and an aspheric surface with set_asphere — each carries the extra data the "
            "flat schema cannot."
        ),
        "tool": tool,
        "surfaces": surfaces,
    }


# --------------------------------------------------------------------------- #
# Thin dispatchable wrappers.
# --------------------------------------------------------------------------- #
def read_lens_spec(session, params):
    """Return the live system serialized as a LensSpec dict.

    A system containing a coordinate break OR a MIRROR material
    is REFUSED fail-closed (``lens_spec_unsupported``) — the flat schema cannot carry
    it, so serializing would silently mangle the fold. NEVER raises past the boundary:
    the CB/MIRROR scan runs first, then the (now CB-conic-safe) read.
    """
    system = session.system
    try:
        unsupported = _unsupported_surfaces(system)
        if unsupported:
            return _lens_spec_unsupported_envelope("read_lens_spec", unsupported)
        spec = LensSpec.read(system)
        return {"lens_spec": spec.to_dict()}
    except Exception as exc:  # noqa: BLE001 — never-raise: a read fault -> structured (L26)
        return {
            "ok": False,
            "error_family": "lens_read",
            "error": f"could not serialize the system into a LensSpec ({exc!r})",
            "tool": "read_lens_spec",
        }


def _glass_catalog_prescan(system, spec, auto_load):
    """Resolve + auto-load every declared material's owning catalog BEFORE the checkpoint.

    The hoisted glass-catalog pre-scan (§2.3). Runs AFTER the CB/MIRROR guard +
    ``pre_snapshot`` and BEFORE the SaveAs, so:
      (a) a glass in NO catalog / a scan failure refuses with ZERO mutation, ZERO
          checkpoint, ZERO rollback (no partial apply);
      (b) the auto-loads happen UP FRONT so the in-checkpoint per-material loop hits
          the in-use HIT path for every material;
      (c) the catalogs load BEFORE the snapshot, so a later rollback is self-consistent.

    Uses ONLY the SHARED ``lens_glass`` helpers (``_resolve_canonical`` /
    ``_find_owning_catalogs`` / ``_load_catalog_proven``) so the pre-scan loads exactly
    the catalogs that make the per-material author resolve (L30, no second resolver).

    Returns either:
      - ``{"refused": True, "envelope": <_lens_apply_failure dict>}`` (a no-catalog
        glass / a scan-failure / an opt-out refuse / a CatalogLoadError) — the caller
        returns the envelope (NO checkpoint, NO mutation), OR
      - ``{"refused": False, "auto_loaded": [<catalog>, ...]}`` (deduped, first-seen
        order; ``[]`` when nothing needed loading) — the caller proceeds to checkpoint.
    """
    catalogs = system.SystemData.MaterialCatalogs
    n = int(system.LDE.NumberOfSurfaces)
    needed = []        # catalogs to auto-load (deduped, first-seen order)
    auto_loaded = []   # disclosure (the catalogs actually loaded)

    for i, surf in enumerate(spec.surfaces):
        glass = surf.material
        if not glass or not (1 <= i <= n - 1):  # mirror the apply gate (LS air/range)
            continue
        canonical, _cat = lens_glass._resolve_canonical(catalogs, glass)
        if canonical is not None:
            continue  # already in use — no action
        owners, scan_failures = lens_glass._find_owning_catalogs(catalogs, glass)
        if not owners:
            # PRE-mutation refuse: a glass in NO in-use OR available catalog (or its
            # only owner could not be scanned) — no SaveAs, no partial apply.
            env = _lens_apply_failure(
                f"material {glass!r} (surface {i}) is not in any in-use OR available "
                f"catalog ({lens_glass._catalogs_in_use(catalogs)})"
                + (f"; could not scan {scan_failures}" if scan_failures else "")
                + "; refusing pre-mutation (no partial apply).",
                checkpoint=False, rolled_back=False, partial_state=False,
                mismatches=[], auto_loaded_catalogs=auto_loaded,
            )
            return {"refused": True, "envelope": env}
        if auto_load:
            first_catalog = owners[0][1]
            if first_catalog not in needed and not catalogs.IsCatalogInUse(
                first_catalog
            ):
                needed.append(first_catalog)
        else:
            # OPT-OUT: refuse naming the owner + alts (the load_catalog(name=...) polish).
            env = _lens_apply_failure(
                f"material {glass!r} (surface {i}) needs catalog "
                f"{owners[0][1]!r} loaded "
                f"(also in {[c for (_g, c) in owners[1:]]}); pass auto_load=true or run "
                f"load_catalog(name={owners[0][1]!r}) first.",
                checkpoint=False, rolled_back=False, partial_state=False,
                mismatches=[], auto_loaded_catalogs=auto_loaded,
            )
            return {"refused": True, "envelope": env}

    # Auto-load ALL needed catalogs UP FRONT through the SHARED proven loader.
    for cat in needed:
        try:
            lens_glass._load_catalog_proven(catalogs, cat)  # read-back proven (F4)
        except CatalogLoadError as exc:
            # PRE-checkpoint, fail-closed: the system was NOT mutated. Disclose any
            # catalog ALREADY loaded by this pre-scan (the side-effect is real).
            env = _lens_apply_failure(
                f"could not load catalog {cat!r} for the declared materials ({exc}); "
                "the system was NOT mutated — nothing was applied."
                + _auto_loaded_note(auto_loaded),
                checkpoint=False, rolled_back=False, partial_state=False,
                mismatches=[], auto_loaded_catalogs=auto_loaded,
            )
            return {"refused": True, "envelope": env}
        auto_loaded.append(cat)

    return {"refused": False, "auto_loaded": auto_loaded}


def apply_lens_spec(session, params):
    """Validate + apply a LensSpec dict ATOMICALLY, read back, and verify the round-trip.

    ``applied:True`` is returned ONLY when the read-back spec matches
    the INTENDED spec (surface count + every written field within tolerance / canonical
    string-compare).

    D2/D3 (3b atomicity + envelope): the whole apply is wrapped in a ``SaveAs`` temp-
    ``.zmx`` checkpoint + ``LoadFile`` rollback (mirroring ``apply_merit_recipe``'s
    temp-``.MF`` pattern). On ANY exception inside the boundary — a ``SurfaceWriteError``,
    a round-trip verify mismatch, OR a bare ``.NET``/generic engine throw (caught by a
    broad ``except Exception`` so a generic throw does NOT bypass rollback, the merit-path
    H-2 lesson) — the system is restored from the checkpoint, then a POST-RESTORE verify
    confirms the restore actually landed. The FAILURE path RETURNS a structured envelope
    ``{ok:false, error_family:"lens_apply", applied:false, checkpoint, rolled_back,
    partial_state, mismatches}`` (the tool-family never-raise posture — like
    ``normalize_stop`` / ``apply_merit_recipe``), NOT a raise. Success is unchanged:
    ``{applied:true, lens_spec:read_back}``.
    """
    if "lens_spec" not in params:
        raise ToolParamError("apply_lens_spec requires 'lens_spec'")
    # Validate FIRST (D2 step 1): a malformed dict (unknown keys / bad types) raises
    # ToolParamError BEFORE any snapshot or mutation — it never even checkpoints.
    spec = LensSpec.from_dict(params["lens_spec"])

    # auto_load (default ON): the pre-scan auto-loads every owning catalog the declared
    # materials need (read-back proven) BEFORE the checkpoint. A REAL bool only — reject
    # "true"/0/1 — evaluated BEFORE any scan/mutation.
    auto_load = _require_bool(params.get("auto_load", True), label="auto_load")

    system = session.system

    # If the LIVE system already contains a coordinate break or a
    # MIRROR material, FAIL-CLOSED REFUSE rather than clobber it. The flat reconcile /
    # set_surface / substitute_glass apply path would silently destroy a fold (a CB
    # re-applies as a plain AIR surface; a MIRROR is rejected as an unknown glass mid-
    # apply -> a rolled-back partial). The atomic rollback below still protects the
    # mutation path, but refusing UP FRONT (no checkpoint, no mutation) is the honest
    # fail-closed posture. NEVER raises — a structured envelope, like the read guard.
    try:
        unsupported = _unsupported_surfaces(system)
    except Exception as exc:  # noqa: BLE001 — never-raise: an unreadable scan -> refuse
        return {
            "ok": False, "error_family": "lens_apply", "error": (
                f"could not scan the system for unsupported (CB/MIRROR) surfaces "
                f"before applying ({exc!r}); refusing rather than risking a clobber"
            ),
            "tool": "apply_lens_spec", "applied": False, "checkpoint": False,
            "rolled_back": False, "partial_state": False, "mismatches": [],
        }
    if unsupported:
        env = _lens_spec_unsupported_envelope("apply_lens_spec", unsupported)
        # Mirror the apply failure-envelope shape so a caller branches uniformly: the
        # system was NOT mutated (no checkpoint, no apply, no clobber).
        env.update({
            "applied": False, "checkpoint": False, "rolled_back": False,
            "partial_state": False, "mismatches": [],
        })
        return env

    # Pre-apply snapshot (a read BEFORE the SaveAs) — the oracle the POST-RESTORE
    # verify compares against to prove the rollback actually restored (D2 step 5).
    pre_snapshot = LensSpec.read(system)

    # ---- Glass-catalog PRE-SCAN (auto-load owning catalogs BEFORE the checkpoint) ----
    # Resolve EVERY declared material; a no-catalog material refuses PRE-mutation (no
    # SaveAs, no partial apply). Collect the not-in-use owning catalogs (deduped) and
    # auto-load them UP FRONT through the SHARED proven loader so the in-checkpoint
    # per-material loop hits the in-use HIT path for every material — AND the catalogs
    # are loaded BEFORE the snapshot, so a later rollback is self-consistent (the
    # rollback-strands-a-load hole is mooted). MIRROR/CB/grating systems never reach
    # here (refused by _unsupported_surfaces above). ``auto_loaded`` is threaded into
    # every post-pre-scan success/failure/rollback return (the side-effect disclosure).
    prescan = _glass_catalog_prescan(system, spec, auto_load)
    if prescan.get("refused"):
        return prescan["envelope"]
    auto_loaded = prescan["auto_loaded"]

    # ---- THE DRIVEN-CELL PRE-SCAN. DISCLOSURE, NOT REFUSAL. ----
    # Positioned with the glass pre-scan, BEFORE ``SaveAs(checkpoint_path)``.
    #
    # IT DOES NOT REFUSE, AND THE REASON IS DECISIVE ENOUGH TO STATE ONCE SO NO LATER
    # ROUND RE-LITIGATES IT. ``_reset_surfaces_to_spec`` — called INSIDE the checkpoint —
    # sets every non-re-declared radius/thickness/conic SOLVE to Fixed BEFORE the writes.
    # So a refusing pre-scan would (i) FALSE-REFUSE the ordinary read -> modify -> apply
    # round-trip on any design carrying a geometry solve, and (ii) be CIRCULAR: apply was
    # the only shipped door that cleared a non-Variable solve before ``clear_solve``
    # existed, so its refusal would name itself as the remedy.
    #
    # TWO MECHANISMS, STATED AS TWO. This is the DISCLOSURE. Write-time ENFORCEMENT is
    # the guard raising into the ":1902 ANY throw routes to rollback" broad-except.
    driven_cells, driven_scan_failed = _driven_cells_prescan(system, spec)

    # ---- Atomic checkpoint (fail-closed): SaveAs(tmp.zmx) BEFORE mutating. ----
    checkpoint_path = None
    try:
        fd, checkpoint_path = tempfile.mkstemp(
            suffix=".zmx", prefix="optivibe_lens_ckpt_"
        )
        os.close(fd)
        system.SaveAs(checkpoint_path)
    except Exception as exc:  # noqa: BLE001 — a checkpoint SaveAs throw -> fail-closed
        # We never mutated, so the system is unchanged (D2 step 2). Reap the temp —
        # AND any engine companion (.ZDA) a partial SaveAs may have begun writing
        # (prefix-glob, HIGH leak fix).
        _reap_checkpoint(checkpoint_path)
        return _lens_apply_failure(
            f"could not checkpoint the system before applying ({exc!r}); the system "
            "was NOT mutated — nothing was applied"
            + _auto_loaded_note(auto_loaded),
            checkpoint=False,
            rolled_back=False,
            partial_state=False,
            mismatches=[],
            auto_loaded_catalogs=auto_loaded,
            driven_cells=driven_cells, driven_scan_failed=driven_scan_failed,
        )

    # The success env is BUILT inside the atomic try but RETURNED in the
    # OUTER scope (after the checkpoint is reaped), so the opt-in ``reset_variables`` clear
    # is the LAST step of a SUCCESSFUL apply, OUTSIDE the checkpoint try. A FAILED apply
    # (round-trip mismatch -> rollback) returns directly inside the except and NEVER reaches
    # the reset (the disclosure keys are a SUCCESS-PATH-ONLY stamp).
    success_env = None
    try:
        try:
            # The atomic boundary covers the MUTATION and the VERDICT (D2 step 3). S1
            # GAP-1a: apply returns the hard-reset disclosure (surfaces_reset /
            # solves_reset_to_fixed) — stamped on the SUCCESS envelope below, NEVER on a
            # rollback (a failed apply returns inside the except and never reaches it).
            reset_disclosure = LensSpec.apply(system, spec)
            read_back = LensSpec.read(system)
            mismatches = _spec_roundtrip_mismatches(spec, read_back)
            # After apply/reconcile, the system must not be left stop-less. If
            # the spec declares a stop, assert EXACTLY ONE stop exists on the declared
            # surface (a stop-less / mismoved result is a firewall gap).
            mismatches.extend(_stop_existence_mismatches(spec, read_back))
            if mismatches:
                raise SurfaceWriteError(
                    "apply_lens_spec round-trip verification failed; the applied "
                    f"design does not match the intended spec: {mismatches}",
                    field="lens_spec",
                    intended=spec.to_dict(),
                    actual=read_back.to_dict(),
                    surface=None,
                )
            env = {
                "applied": True, "lens_spec": read_back.to_dict(),
                "auto_loaded_catalogs": auto_loaded,
            }
            # Threaded into SUCCESS, FAILURE and ROLLBACK alike (the
            # ``auto_loaded`` precedent). The agent needs to learn WHY on the failing
            # path most of all, so a success-only stamp would disclose it exactly where
            # it is least useful.
            env.update(_driven_cells_keys(driven_cells, driven_scan_failed))
            # GAP-2: an additive, read-only WARN scan of over-long comments — the
            # engine truncated them to the 32-char clean prefix. NEVER affects the
            # apply/rollback decision (cannot introduce a rollback regression).
            comment_warnings = [
                f"surface {idx}: comment truncated by the engine to "
                f"{_c.COMMENT_MAX_CHARS} chars (intended "
                f"{len(surf.comment or '')} chars)."
                for idx, surf in enumerate(spec.surfaces)
                # The OBJECT (surface 0) comment is SKIPPED (warned below) —
                # do not also emit a truncation warning for a field never written.
                if idx != 0 and len(surf.comment or "") > _c.COMMENT_MAX_CHARS
            ]
            # Apply door: a MEANINGFUL non-default OBJECT geometry/comment was
            # skipped (thickness-only) — surface it (additive, never a rollback).
            object_warnings = _object_skipped_field_warnings(
                spec.surfaces[0] if spec.surfaces else None
            )
            warnings = comment_warnings + object_warnings
            if warnings:
                env["warnings"] = warnings
            # S1 GAP-1a: stamp the additive hard-reset disclosure — present ONLY when
            # non-empty (a clean build with nothing to reset is byte-identical), and ONLY
            # on the SUCCESS path (a rollback returns inside the except, never here).
            if isinstance(reset_disclosure, dict):
                reset_surfaces = reset_disclosure.get("surfaces_reset") or []
                solves_reset = reset_disclosure.get("solves_reset_to_fixed") or []
                if reset_surfaces:
                    env["surfaces_reset"] = reset_surfaces
                if solves_reset:
                    env["solves_reset_to_fixed"] = solves_reset
            # Do NOT return here — hand the committed env to the outer scope so
            # the reset runs OUTSIDE the checkpoint try (after the reap).
            success_env = env
        except Exception as exc:  # noqa: BLE001 — D2 step 4: ANY throw routes to rollback
            # A SurfaceWriteError (a dropped write / verify mismatch) OR a bare
            # generic/.NET engine throw — both route to rollback (broad except so a
            # generic throw does NOT bypass it, the merit-path H-2 lesson). The
            # exception text is carried into the envelope so the broad except never
            # silently swallows a logic bug. The rolled-back envelope stamps NEITHER
            # disclosure key (the reset is a success-path-only step).
            mismatches = _mismatches_from_exc(exc)
            return _rollback_lens_apply(
                system, checkpoint_path, pre_snapshot, reason=repr(exc),
                mismatches=mismatches, auto_loaded_catalogs=auto_loaded,
                driven_cells=driven_cells, driven_scan_failed=driven_scan_failed,
            )
    finally:
        # D2 step 7: reaped on EVERY path. The LIVE engine writes its native ``.ZDA``
        # binary (NOT the ``.zmx`` placeholder mkstemp made) on SaveAs, so unlinking
        # only the ``.zmx`` leaks the real ``.ZDA`` on every call (HIGH leak, live-
        # caught). Glob the mkstemp TOKEN prefix and remove the placeholder AND the
        # engine's ``.ZDA``/companion output — all guarded (never raises).
        _reap_checkpoint(checkpoint_path)

    # The committed-apply post-processing — OUTSIDE the checkpoint try, after
    # the reap, gated on the apply having committed. ``success_env`` is None ONLY when the
    # inner except returned (a failed apply), so this region is reached ONLY on success.
    # 1) DISCLOSE the inherited optimizer variables (the apply round-trip preserves the
    #    Variable solves), guarded -> ([], None) so a disclosure fault never
    #    breaks a committed apply.
    inherited, n_inherited = _oc._disclose_inherited_variables(system)
    success_env["inherited_variables"] = inherited
    success_env["n_inherited_variables"] = n_inherited
    if n_inherited is None:
        success_env["inherited_variables_warning"] = (
            "could not enumerate the inherited optimizer variables after the apply "
            "(the disclosure read faulted); the apply itself succeeded"
        )
    # 2) OPTIONAL reset_variables (STRICT is True): clear every inherited variable as the
    #    LAST step of the committed apply. A reset that itself faults / returns a residual
    #    NEVER downgrades the apply's ok nor triggers a rollback (it is post-commit) — it
    #    stamps reset_result + a reset_warning. SAME shared core as load_design (L30).
    if isinstance(params, dict) and params.get("reset_variables") is True:
        reset_result = _oc._clear_all_variables_core(system)
        success_env["reset_result"] = reset_result
        # Re-disclose post-reset (now 0 on a clean clear).
        re_inherited, re_n = _oc._disclose_inherited_variables(system)
        success_env["inherited_variables"] = re_inherited
        success_env["n_inherited_variables"] = re_n
        # The re-disclosure overwrites n_inherited_variables, so a FAULTING
        # re-disclosure (None) must carry the SAME warning the first disclosure stamps —
        # otherwise n_inherited_variables:None ships with NO warning (the None-vs-0
        # ambiguity the helper prevents). Keep the warning <-> None invariant in sync.
        if re_n is None:
            success_env["inherited_variables_warning"] = (
                "could not enumerate the inherited optimizer variables after the reset "
                "(the re-disclosure read faulted); the apply + reset themselves succeeded"
            )
        else:
            success_env.pop("inherited_variables_warning", None)
        if not reset_result.get("ok"):
            success_env["reset_warning"] = (
                "reset_variables did not fully clear the inherited variables "
                f"({reset_result.get('error')}); the apply itself succeeded — see "
                "reset_result.unclear_residual"
            )
    return success_env


def _reap_checkpoint(checkpoint_path):
    """Remove the checkpoint placeholder AND the engine's companion output (HIGH leak).

    ``mkstemp(suffix=".zmx", ...)`` makes a ``.zmx`` PLACEHOLDER, but the LIVE
    OpticStudio engine's ``SaveAs`` writes its native ``.ZDA`` binary (same stem,
    different extension) — so ``_unlink_quiet`` on the ``.zmx`` alone leaks the real
    ``.ZDA`` on EVERY apply_lens_spec call (live-caught). This globs the mkstemp TOKEN
    stem (``optivibe_lens_ckpt_<token>``) in the temp dir and removes EVERY file that
    matches — the ``.zmx`` placeholder, the ``.ZDA``, and any companion the engine
    emitted. The glob is SCOPED to the unique mkstemp token, so it can never delete a
    foreign/unrelated file. NEVER raises (a reap failure must not mask the apply
    outcome) — both the glob and each unlink are guarded.
    """
    if not checkpoint_path:
        return
    # Always reap the exact placeholder first (covers a dir/glob hiccup).
    _unlink_quiet(checkpoint_path)
    try:
        directory = os.path.dirname(checkpoint_path)
        base = os.path.basename(checkpoint_path)
        # Strip the ``.zmx`` suffix to the unique token stem; glob ``<stem>*`` so the
        # engine's ``.ZDA``/companion (same stem) is swept, scoped to this token.
        stem, _ext = os.path.splitext(base)
        for path in glob.glob(os.path.join(directory, stem + "*")):
            _unlink_quiet(path)
    except Exception:  # noqa: BLE001 — a reap glob failure never masks the outcome
        pass


def _mismatches_from_exc(exc):
    """Extract a structured ``mismatches`` list from a verify failure (D3).

    A round-trip ``SurfaceWriteError`` carries ``actual`` = the read-back dict; the
    headline message already names the per-field diffs. We surface the exception string
    as the single mismatch entry so the envelope is diagnostic without re-deriving the
    diff (a generic engine throw has no field diff — its repr is the mismatch).
    """
    return [str(exc)]


def _rollback_lens_apply(system, checkpoint_path, pre_snapshot, *, reason, mismatches,
                         auto_loaded_catalogs=None, driven_cells=(),
                         driven_scan_failed=None):
    """Restore from the checkpoint + POST-RESTORE verify (D2 steps 4-6 / D3 envelope).

    - ``LoadFile(ckpt, False)`` restores the pre-apply ``.zmx``.
    - A ``LoadFile`` THROW (the restore itself failed) -> ``partial_state:true,
      rolled_back:false, checkpoint:true`` + "the system is in an UNKNOWN state —
      reload your design .zmx" (D2 step 6).
    - POST-RESTORE verify (D2 step 5): read back the count + geometry and compare to
      the pre-apply snapshot. A rollback that LOADED but did not restore is the worst
      silent-wrong, so a mismatch -> ``rolled_back:false, partial_state:true`` + a LOUD
      warning. A faithful restore -> ``rolled_back:true``.
    """
    try:
        system.LoadFile(checkpoint_path, False)
    except Exception as exc:  # noqa: BLE001 — a rollback LoadFile throw -> partial state
        return _lens_apply_failure(
            f"apply_lens_spec failed ({reason}); the ROLLBACK restore itself threw "
            f"({exc!r}) — the system is in an UNKNOWN state. Reload your design .zmx "
            "to recover.",
            checkpoint=True,
            rolled_back=False,
            partial_state=True,
            mismatches=mismatches,
            auto_loaded_catalogs=auto_loaded_catalogs,
            driven_cells=driven_cells, driven_scan_failed=driven_scan_failed,
        )

    # POST-RESTORE verify: did the LoadFile actually bring the system back?
    restore_problems = _post_restore_mismatches(system, pre_snapshot)
    if restore_problems:
        return _lens_apply_failure(
            f"apply_lens_spec failed ({reason}); the checkpoint LOADED but the "
            "post-restore read-back does NOT match the pre-apply snapshot "
            f"({restore_problems}) — the rollback did not faithfully restore. The "
            "system may be in a PARTIAL state; reload your design .zmx to recover.",
            checkpoint=True,
            rolled_back=False,
            partial_state=True,
            mismatches=mismatches,
            auto_loaded_catalogs=auto_loaded_catalogs,
            driven_cells=driven_cells, driven_scan_failed=driven_scan_failed,
        )

    return _lens_apply_failure(
        f"apply_lens_spec failed ({reason}); the system was ROLLED BACK to its "
        "pre-apply state via the temp checkpoint."
        + _auto_loaded_note(auto_loaded_catalogs),
        checkpoint=True,
        rolled_back=True,
        partial_state=False,
        mismatches=mismatches,
        auto_loaded_catalogs=auto_loaded_catalogs,
        driven_cells=driven_cells, driven_scan_failed=driven_scan_failed,
    )


def _post_restore_mismatches(system, pre_snapshot):
    """FULL compare of the restored system against the pre-apply snapshot.

    The post-restore verify is a read-vs-read of the SAME system: ``pre_snapshot``
    was produced by ``LensSpec.read`` BEFORE the apply, and we read the system AGAIN
    after ``LoadFile``. A faithful restore makes the two reads identical, so this
    compares EVERYTHING — not just radius/thickness/conic/material + counts (the
    earlier shallow version falsely passed a restore that left semi_diameter /
    aperture VALUE / field-wavelength VALUES / is_stop placement / comment wrong as
    ``rolled_back:true``, the worst silent-wrong).

    It REUSES the same comparators the round-trip verdict uses, treating
    ``pre_snapshot`` as the "intended":
    - ``_spec_roundtrip_mismatches`` — surface count + per-surface fields (every
      field ``_declared_writes`` yields, incl. semi_diameter / comment; both reads
      are post-inf->None so two faithful reads of an inf-resolving surface match) +
      ``_system_level_mismatches`` (aperture VALUE + field/wavelength VALUES, not
      just counts).
    - ``_stop_existence_mismatches`` — the stop placement (a rollback that loaded but
      left the stop on the WRONG surface is caught).

    Because ``pre_snapshot`` is a ``read()`` result, its ``aperture_type`` /
    ``field_type`` are populated, so the system-level blocks ARE compared (they gate
    on a declared type). A read failure during the verify is treated as a mismatch
    (we cannot prove the restore landed, so we must NOT claim ``rolled_back:true``).
    """
    try:
        restored = LensSpec.read(system)
    except Exception as exc:  # noqa: BLE001 — an unreadable restore cannot be proven faithful
        return [f"post-restore read threw ({exc!r}); restore is unverifiable"]

    # Full read-vs-read compare: pre_snapshot is the "intended", restored is the
    # "actual". Identical reads of a faithful restore yield ZERO mismatches.
    problems = _spec_roundtrip_mismatches(pre_snapshot, restored)
    problems.extend(_stop_existence_mismatches(pre_snapshot, restored))
    return problems


def _auto_loaded_note(auto_loaded):
    """Prose clause naming any catalog(s) the pre-scan auto-loaded and left in use.

    The "system NOT mutated / ROLLED BACK" failure prose understated a REAL surviving
    side-effect: a pre-scan that auto-loaded a catalog (read-back proven) leaves it in
    use — the safe, spec-intended direction (no auto-unload; loading only widens the
    authorable set). The structured ``auto_loaded_catalogs`` envelope key already
    discloses this; this appends a matching human-prose clause. Returns ``""`` when the
    pre-scan loaded nothing (so the unchanged prose carries no dangling clause).
    """
    loaded = list(auto_loaded or [])
    if not loaded:
        return ""
    return f"; note: catalog(s) auto-loaded and left in use: {loaded}"


def _lens_apply_failure(message, *, checkpoint, rolled_back, partial_state, mismatches,
                        auto_loaded_catalogs=None, driven_cells=(), driven_scan_failed=None):
    """Build the structured ``lens_apply`` failure envelope (D3 — never raises).

    ``auto_loaded_catalogs`` (default ``[]``) discloses any catalog the pre-scan
    auto-loaded BEFORE this failure — the side-effect is real (the catalog stays in
    use; loading only widens the authorable set, it cannot corrupt a design), so it
    is disclosed on EVERY post-pre-scan failure/rollback path (§2.4).
    """
    return {
        "ok": False,
        "error_family": "lens_apply",
        "error": message,
        "tool": "apply_lens_spec",
        "applied": False,
        "checkpoint": checkpoint,
        "rolled_back": rolled_back,
        "partial_state": partial_state,
        "mismatches": mismatches,
        "auto_loaded_catalogs": list(auto_loaded_catalogs or []),
        # The driven-cell disclosure on the FAILING path. Defaults
        # make every PRE-pre-scan caller (a malformed spec, a refused material) emit an
        # empty list rather than a misleading null: nothing was scanned because nothing
        # could be, and ``driven_scan_failed`` is reserved for a scan that RAN and broke.
        **_driven_cells_keys(driven_cells, driven_scan_failed),
    }


def _asphere_mismatch(i, intended_triple, actual_surface):
    """Compare a declared asphere TRIPLE (type, coeffs, norm) vs the read-back (S3 §2.3).

    ``intended_triple`` is ``(surface_type, coefficients, norm_radius)`` (the
    ``_declared_writes`` "asphere" yield). ``actual_surface`` is the read-back
    ``SurfaceSpec``. Compares:
      - TYPE: the read-back ``asphere_surface_type`` (None ⇒ EvenAspheric) == declared.
        A silent ChangeType no-op reads back a non-asphere (coeffs None) -> mismatch.
      - NORM (gated only): the read-back ``asphere_norm_radius`` == declared (asphere
        ``_readback_ok``, abs floor 1e-15).
      - MAX-TERM (gated only): the read-back ``asphere_max_term`` == len(declared coeffs).
      - COEFFICIENTS: per-INDEX, declared-length only, via the asphere ``_readback_ok``.
    Returns a diff string, or ``None`` when everything matched.
    """
    intended_type, intended_coeffs, intended_norm = intended_triple
    intended_type = intended_type or "EvenAspheric"
    info = _asph.ASPHERE_TYPE_INFO[intended_type]
    actual_type = (
        getattr(actual_surface, "asphere_surface_type", None) or "EvenAspheric"
    )
    actual_coeffs = getattr(actual_surface, "aspheric_coefficients", None)
    if actual_coeffs is None:
        return (
            f"surface {i} asphere: declared {intended_type} but the read-back carries no "
            "coefficients (the asphere did not carry — a silent ChangeType no-op)"
        )
    if actual_type != intended_type:
        return (
            f"surface {i} asphere_surface_type: intended={intended_type!r} "
            f"actual={actual_type!r}"
        )
    if info.gated:
        actual_norm = getattr(actual_surface, "asphere_norm_radius", None)
        if not _asph._readback_ok(intended_norm, actual_norm):
            return (
                f"surface {i} asphere_norm_radius: intended={intended_norm!r} "
                f"actual={actual_norm!r}"
            )
        actual_max = getattr(actual_surface, "asphere_max_term", None)
        if actual_max != len(intended_coeffs):
            return (
                f"surface {i} asphere_max_term: intended={len(intended_coeffs)} "
                f"actual={actual_max!r}"
            )
    return _coefficient_mismatch(i, intended_coeffs, actual_coeffs)


def _coefficient_mismatch(i, intended, actual):
    """Compare declared asphere coefficients per-INDEX (S2/S3 CARRY §2.3). Returns a
    diff string, or ``None`` when the declared coefficients matched.

    - ``intended`` is the spec's declared coefficients (a tuple of floats); ``actual``
      is the read-back surface's ``aspheric_coefficients`` (a list of floats from
      ``read()`` on an asphere surface, or ``None`` if the read-back surface is NOT an
      asphere — the carry did not take -> a mismatch).
    - Compares ONLY the declared length: ``intended[j]`` vs ``actual[j]`` for
      ``j in range(len(intended))``, each via the asphere ``_readback_ok`` (abs floor
      1e-15). The read-back's trailing terms beyond ``len(intended)`` are NOT compared (the
      short-write "rest untouched" semantics).
    """
    intended_seq = list(intended) if isinstance(intended, (list, tuple)) else None
    if intended_seq is None:
        # An intended that is not a sequence should never reach here (validated in
        # __post_init__), but be defensive: a non-sequence intended is a mismatch.
        return (
            f"surface {i} aspheric_coefficients: intended is not a sequence "
            f"({intended!r})"
        )
    if not isinstance(actual, (list, tuple)):
        # The read-back surface carries no coefficients (it is not an EvenAspheric, or the
        # read degraded to None) -> the declared asphere did NOT carry. A mismatch.
        return (
            f"surface {i} aspheric_coefficients: intended={intended_seq!r} but the "
            f"read-back carries no coefficients (actual={actual!r}) — the asphere did "
            "not carry"
        )
    if len(actual) < len(intended_seq):
        return (
            f"surface {i} aspheric_coefficients: declared {len(intended_seq)} term(s) "
            f"but the read-back has only {len(actual)} (actual={list(actual)!r})"
        )
    for j in range(len(intended_seq)):
        if not _asph._readback_ok(intended_seq[j], actual[j]):
            return (
                f"surface {i} aspheric_coefficients[{j}]: intended="
                f"{intended_seq[j]!r} actual={actual[j]!r}"
            )
    return None


# GAP-6 (finite-conjugate build robustness): the image-only inf-pin exemption now
# lives in ``_lens_common`` (``_c._image_thickness_ok``) so set_surface + the apply
# verifier share ONE oracle. Kept as a module alias for the verifier call site below
# and any existing reference.
_image_thickness_ok = _c._image_thickness_ok


def _spec_roundtrip_mismatches(intended, actual):
    """Return a list of human-readable mismatches between two ``LensSpec``s.

    Compares the surface count, the SYSTEM-LEVEL intent, and EXACTLY the
    per-surface fields ``_declared_writes`` yields — the SAME generator ``apply``
    iterates, so the two can never diverge on which fields are handled (the
    structural fix). Numeric compare uses the
    tier-wide ``_readback_ok`` oracle (tolerance + inf/nan rules); strings compare
    canonical case-insensitive. An empty list means the round-trip matched.

    A system-level field is only compared when the spec DECLARES it (``apply``
    writes a field only if the spec carries it), so a declared-but-unwritten intent
    — e.g. ``field_type`` changed while ``fields`` left stale — is caught as a
    mismatch, while an undeclared field (apply intentionally leaves it alone) is
    not spuriously flagged.
    """
    problems = []
    if len(intended.surfaces) != len(actual.surfaces):
        problems.append(
            f"surface count intended={len(intended.surfaces)} "
            f"actual={len(actual.surfaces)}"
        )
        return problems  # count mismatch dominates; per-surface diff is moot

    problems.extend(_system_level_mismatches(intended, actual))

    # Verify EXACTLY the per-surface fields apply declared, iterating the SAME
    # _declared_writes source of truth (apply and verify
    # cannot drift because they share this generator). This covers OBJECT index 0
    # (OBJECT thickness = object distance), semi_diameter only when declared
    # (None = don't touch, never compared), material including "" air,
    # and comment including "" clear.
    actual_surfaces = actual.surfaces
    for i, fname, intended_value in _declared_writes(intended):
        # Apply door: the OBJECT (surface 0) accepts thickness ONLY. apply
        # writes the OBJECT thickness and SKIPS radius/conic/semi_diameter/comment
        # (the engine's auto values are left intact; a meaningful non-default is
        # WARNed, never written). So the verifier must NOT compare those skipped
        # OBJECT fields — comparing intended=50 vs the unchanged actual=inf would
        # flag a false mismatch and roll back a benign round-trip. Only OBJECT
        # thickness is verified (it IS written). is_stop/material on the OBJECT are
        # already handled elsewhere (material loop skips i==0; stop is system-wide).
        if i == 0 and fname != "thickness":
            continue
        if fname == "asphere":
            # Asphere: compare the full TRIPLE (type + norm + max-term +
            # per-index coefficients) against the read-back surface, via the asphere
            # ``_readback_ok`` (abs floor 1e-15 — NOT the generic float tol, which would
            # PASS a coefficient collapsed to 0.0). A silent ChangeType / gate no-op reads
            # back a different type / a non-asphere -> a TYPE mismatch (NOT a silent pass).
            diff = _asphere_mismatch(i, intended_value, actual_surfaces[i])
            if diff is not None:
                problems.append(diff)
            continue
        actual_value = getattr(actual_surfaces[i], fname)
        if fname == "comment":
            # GAP-2: the engine truncates Comment to a 32-char clean prefix; accept
            # ``actual == intended[:32]`` (a wrong/dropped comment still mismatches).
            ok = _c._comment_readback_ok(intended_value, actual_value)
        elif fname == "thickness":
            # GAP-6: an IMAGE thickness pinned to +inf by the engine is a match.
            ok = _image_thickness_ok(
                i, len(intended.surfaces), intended_value, actual_value
            )
        else:
            ok = _c._readback_ok(intended_value, actual_value)
        if not ok:
            problems.append(
                f"surface {i} {fname}: intended={intended_value!r} "
                f"actual={actual_value!r}"
            )
    # is_stop is verified by _stop_existence_mismatches, not per-surface
    # here: a spec with NO declared stop means "do not touch the stop" (apply never
    # calls set_stop_surface), so the engine's existing stop must NOT be flagged as
    # an unexpected stop. Only a spec that DECLARES a stop pins the placement, which
    # _stop_existence_mismatches asserts (exactly one stop, on the declared surface).
    return problems


def _system_level_mismatches(intended, actual):
    """Round-trip the SYSTEM-LEVEL intent: aperture / field / wavelengths.

    Each block is compared ONLY when the spec declares it, mirroring ``apply``
    (which writes a system field only when the spec carries it). A preset can't be
    recovered by ``read`` (read returns ``wavelength_preset=None``), so a declared
    preset is verified by its EFFECT — the read-back wavelength set must be
    non-empty — rather than by the preset string.
    """
    problems = []

    # Aperture: apply writes it only when aperture_type is declared (non-empty).
    if intended.aperture_type:
        if not _c._readback_ok(intended.aperture_type, actual.aperture_type):
            problems.append(
                f"aperture_type: intended={intended.aperture_type!r} "
                f"actual={actual.aperture_type!r}"
            )
        if not _c._readback_ok(intended.aperture_value, actual.aperture_value):
            problems.append(
                f"aperture_value: intended={intended.aperture_value!r} "
                f"actual={actual.aperture_value!r}"
            )

    # Field: apply writes it only when field_type is declared. When it is, the
    # declared field SET is compared too (an intended-empty vs live-non-empty set
    # — the stale-fields repro — is caught here).
    if intended.field_type:
        if not _c._readback_ok(intended.field_type, actual.field_type):
            problems.append(
                f"field_type: intended={intended.field_type!r} "
                f"actual={actual.field_type!r}"
            )
        if not _fields_match(intended.fields, actual.fields):
            problems.append(
                f"fields: intended={intended.fields!r} actual={actual.fields!r}"
            )

    # Wavelengths: a preset is verified by effect (non-empty live set); an explicit
    # declared list is compared value-for-value.
    if intended.wavelength_preset is not None:
        if len(actual.wavelengths) < 1:
            problems.append(
                f"wavelength_preset {intended.wavelength_preset!r} produced an "
                f"empty wavelength set (count={len(actual.wavelengths)})"
            )
    elif intended.wavelengths:
        if not _pairs_match(intended.wavelengths, actual.wavelengths):
            problems.append(
                f"wavelengths: intended={intended.wavelengths!r} "
                f"actual={actual.wavelengths!r}"
            )
    return problems


def _fields_match(intended, actual):
    """True if two field tuples (``(x, y, weight)`` triples) match within tol."""
    if len(intended) != len(actual):
        return False
    for ti, ta in zip(intended, actual):
        if not all(_c._readback_ok(a, b) for a, b in zip(ti, ta)):
            return False
    return True


def _pairs_match(intended, actual):
    """True if two wavelength tuples (``(value, weight)`` pairs) match within tol."""
    if len(intended) != len(actual):
        return False
    for pi, pa in zip(intended, actual):
        if not all(_c._readback_ok(a, b) for a, b in zip(pi, pa)):
            return False
    return True


def _stop_existence_mismatches(intended, actual):
    """A non-trivial spec must leave the system with EXACTLY ONE stop.

    Only enforced when the spec DECLARES a stop (one surface with ``is_stop=True``
    on an interior surface where a stop can take effect). In that case the live
    read-back must report exactly one stop and it must sit on the declared surface
    — a stop-less or mismoved result is a SurfaceWriteError-grade firewall gap.
    """
    problems = []
    n = len(intended.surfaces)
    declared = [
        i for i, s in enumerate(intended.surfaces)
        if s.is_stop and 1 <= i <= n - 2
    ]
    if not declared:
        return problems  # spec does not declare a (placeable) stop — nothing to assert
    want = declared[0]
    live_stops = [i for i, s in enumerate(actual.surfaces) if s.is_stop]
    if live_stops != [want]:
        problems.append(
            f"stop surface: spec declares exactly one stop on surface {want}, but "
            f"the applied system reports stop surface(s) {live_stops} "
            "(the system must have exactly one stop matching the spec)"
        )
    return problems


READ_LENS_SPEC_SPEC = ToolSpec(
    name="read_lens_spec",
    handler=read_lens_spec,
    required_params=(),
    description="Serialize the whole optical system into a LensSpec dict.",
)

APPLY_LENS_SPEC_SPEC = ToolSpec(
    name="apply_lens_spec",
    handler=apply_lens_spec,
    required_params=("lens_spec",),
    param_types={"lens_spec": "object", "auto_load": "boolean",
                 "reset_variables": "boolean"},
    description=(
        "Apply a whole-system LensSpec dict at once (the round-trip partner of "
        "read_lens_spec). Validated before write (bad glass / out-of-range / "
        "image-stop are caught); returns the read-back spec as proof. auto_load "
        "defaults ON: every declared not-in-use-catalog glass is pre-scanned + "
        "auto-loaded BEFORE the checkpoint (disclosed via auto_loaded_catalogs); a "
        "no-catalog material refuses pre-mutation (no partial apply)."
    ),
)

TOOL_SPECS = (READ_LENS_SPEC_SPEC, APPLY_LENS_SPEC_SPEC)
