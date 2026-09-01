"""Root test configuration."""

collect_ignore_glob = [
    "fixtures/*",
]


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live: hits the real Kalshi API over the network; skipped unless "
        "MP_RUN_LIVE_TESTS=1 (deselect explicitly with -m 'not live')",
    )
