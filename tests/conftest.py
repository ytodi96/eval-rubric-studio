from pathlib import Path

import pytest

from evalstudio.store import Store

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


@pytest.fixture
def demo_dir() -> Path:
    return EXAMPLES / "support-email"


def pytest_addoption(parser):
    parser.addoption("--live", action="store_true", help="also run tests that call real (paid) AI APIs")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--live"):
        return
    skip = pytest.mark.skip(reason="calls a paid API; run with --live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)
