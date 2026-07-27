"""Test package.

Present so that test modules can share constants from `conftest` via a relative
import (`from .conftest import DEADLINE_EPOCH`). Without it, pytest imports each
test module as a top-level module and the relative import fails.
"""
