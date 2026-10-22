import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(autouse=True)
def _fast_public_pacing():
    """The fakes answer instantly; don't pace them like the real Public API."""
    from swingbot import data
    data.set_public_rate(100000)
    yield
    data.set_public_rate(8)
