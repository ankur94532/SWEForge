"""Shared test configuration.

Makes ``tests/harness`` importable as ``harness.*`` from any test module
regardless of pytest's rootdir inference.
"""

import sys
from pathlib import Path

_TESTS = Path(__file__).parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))
