#!/usr/bin/env python3
"""Convenience entry point: ``itether`` command.

Equivalent to ``python3 -m itether_core`` but importable when the package
is installed via pip / setup.py.
"""

from itether_core.__main__ import main

if __name__ == "__main__":
    import sys
    sys.exit(main())
