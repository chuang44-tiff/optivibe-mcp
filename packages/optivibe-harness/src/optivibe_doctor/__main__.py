"""``python -m optivibe_doctor`` — argv in, exit code out.

The ``if __name__`` guard is not a convention here, it is a correctness requirement.  The
existing publication suite walks every module of every shipped package with
``pkgutil.walk_packages`` and imports every name it yields — and ``walk_packages`` yields
``__main__``.  An unguarded ``raise SystemExit(main())`` would therefore execute the entire
doctor, spawn its children and open a session, *inside a test collection run*.

Nothing else belongs in this file.  The parent does no filesystem I/O and imports nothing
outside the standard library, so there is no work here that could be done before ``main``.
"""
import sys

from ._runner import main

if __name__ == "__main__":
    sys.exit(main())
