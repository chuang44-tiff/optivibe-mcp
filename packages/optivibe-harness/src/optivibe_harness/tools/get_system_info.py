"""tools/get_system_info.py — proof-of-concept dispatch tool: 8 direct reads.

Surfaces eight cheap, direct typed ``IOpticalSystem`` properties (default
standalone system values in parentheses):

- ``surfaces``        = ``system.LDE.NumberOfSurfaces``                 (int, 3)
- ``title``           = ``system.SystemName``                          (str, "")
- ``mode``            = ``str(system.Mode)``                           ("Sequential")
- ``is_nonsequential``= ``system.IsNonAxial``                          (bool, False)
- ``aperture_type``   = ``str(SystemData.Aperture.ApertureType)``      ("EntrancePupilDiameter")
- ``aperture_value``  = ``safe_float(SystemData.Aperture.ApertureValue)`` (float, 0.0)
- ``field_count``     = ``SystemData.Fields.NumberOfFields``           (int, 1)
- ``wavelength_count``= ``SystemData.Wavelengths.NumberOfWavelengths`` (int, 1)

Reads are STRAIGHT: a raise propagates to the dispatcher (which envelopes it) —
there is NO partial dict on failure.

EFL is DEFERRED: it needs the temporary-MFE-operand evaluation path
(``op.Value`` = 1e10 sentinel on an empty system). The shipped tool does NOT wire
an ``include_efl`` knob; it is intentionally left out of the PoC surface.

Live ZOS-API integration: asserted against the fixture values by the
live test; unit-tested here against a fake system double.
"""
from .._io import safe_float
from ..server import ToolSpec


def get_system_info(session, params):
    """Return a dict of 8 direct typed system reads. A raise propagates."""
    system = session.system

    aperture = system.SystemData.Aperture
    return {
        "surfaces": int(system.LDE.NumberOfSurfaces),
        "title": str(system.SystemName),
        "mode": str(system.Mode),
        "is_nonsequential": bool(system.IsNonAxial),
        "aperture_type": str(aperture.ApertureType),
        "aperture_value": safe_float(aperture.ApertureValue),
        "field_count": int(system.SystemData.Fields.NumberOfFields),
        "wavelength_count": int(system.SystemData.Wavelengths.NumberOfWavelengths),
    }


TOOL_SPEC = ToolSpec(
    name="get_system_info",
    handler=get_system_info,
    required_params=(),
    description=(
        "Read a quick optical-system overview: surface count, title, mode, "
        "sequential/non-sequential, aperture type+value, and field + wavelength "
        "counts."
    ),
)
