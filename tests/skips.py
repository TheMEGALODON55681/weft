import os
from typing import NoReturn

import pytest


def skip_or_fail(reason: str) -> NoReturn:
    """Skip a test that needs something this machine may lack. In CI the thing is installed on
    purpose, so a missing one is a broken install and fails the run instead of passing as a skip."""
    if os.environ.get("CI"):
        pytest.fail(reason, pytrace=False)
    pytest.skip(reason)
