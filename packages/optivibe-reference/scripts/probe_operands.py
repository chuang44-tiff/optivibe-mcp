#!/usr/bin/env python
"""probe_operands.py — live probe of the MeritOperandType operand surface.

Thin wrapper with the historic CLI: the probe itself is package code,
``optivibe_reference.probe_operands`` (so an installed package runs it via
``optivibe-build-data --probe``). It boots a headless OpticStudio, writes its captures
to ``scripts/captures/`` (a source checkout; ``_paths.captures_dir``), and reaps the
session in a try/finally.

Run from the package root, in the environment where you installed the harness
(it provides pythonnet/clr + the ZOS-API bridge):
    python scripts/probe_operands.py
Env:
    ZOSAPI_NETHELPER  — optional explicit path to ZOSAPI_NetHelper.dll
    ZEMAX_DIR         — optional explicit OpticStudio install dir (holds ZOSAPI.dll)
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from optivibe_reference.probe_operands import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
