"""Playwright fixtures for visual dashboard testing."""

import pytest

DASHBOARD_URL = "http://127.0.0.1:8050"


def pytest_addoption(parser):
    parser.addoption(
        "--dashboard-url",
        default=DASHBOARD_URL,
        help="URL of the running web dashboard",
    )


@pytest.fixture(scope="session")
def dashboard_url(request):
    return request.config.getoption("--dashboard-url")
