import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "e2e: end-to-end tests requiring podman and GH_TOKEN")


def pytest_addoption(parser):
    parser.addoption("--run-e2e", action="store_true", default=False,
                     help="Run e2e tests (requires GH_TOKEN env var and podman)")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-e2e"):
        skip = pytest.mark.skip(reason="needs --run-e2e")
        for item in items:
            if "e2e" in item.keywords:
                item.add_marker(skip)
