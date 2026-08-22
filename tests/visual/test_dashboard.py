"""
Playwright visual verification tests for the Money Printer web dashboard.

Prerequisites:
    1. Start the web dashboard:  python scripts/run_web_dashboard.py --no-browser
    2. Run tests:  python -m pytest tests/visual/ -v

These tests capture screenshots and verify that all dashboard panels render
correctly. Screenshots are saved to docs/screenshots/.
"""

from pathlib import Path

import pytest

# Skip entire module if playwright is not installed
pw = pytest.importorskip("playwright.sync_api")

from playwright.sync_api import sync_playwright, Page  # noqa: E402

SCREENSHOTS_DIR = Path(__file__).resolve().parents[2] / "docs" / "screenshots"


@pytest.fixture(scope="module")
def browser():
    """Launch a headless Chromium browser for the test module."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture
def page(browser, dashboard_url):
    """Create a new page and navigate to the dashboard."""
    page = browser.new_page(viewport={"width": 1920, "height": 1080})
    try:
        response = page.goto(dashboard_url, wait_until="networkidle", timeout=15000)
    except Exception:
        page.close()
        pytest.skip(
            f"Dashboard not reachable at {dashboard_url}. "
            "Start it with: python scripts/run_web_dashboard.py --no-browser"
        )
    if response is None or not response.ok:
        page.close()
        pytest.skip(f"Dashboard returned non-OK response at {dashboard_url}")
    # Give WebSocket a moment to connect and populate data
    page.wait_for_timeout(2000)
    yield page
    page.close()


# ----------------------------------------------------------------------- #
# Screenshot capture
# ----------------------------------------------------------------------- #


class TestScreenshotCapture:
    """Capture baseline and sprint screenshots."""

    def test_capture_full_page(self, page: Page):
        """Capture a full-page screenshot of the dashboard."""
        SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        path = SCREENSHOTS_DIR / "sprint0_baseline.png"
        page.screenshot(path=str(path), full_page=True)
        assert path.exists(), f"Screenshot not saved to {path}"
        assert path.stat().st_size > 10_000, "Screenshot too small — page may be blank"

    def test_capture_viewport(self, page: Page):
        """Capture a viewport-sized screenshot (what the user sees)."""
        SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        path = SCREENSHOTS_DIR / "sprint0_viewport.png"
        page.screenshot(path=str(path), full_page=False)
        assert path.exists()


# ----------------------------------------------------------------------- #
# Panel existence checks
# ----------------------------------------------------------------------- #


class TestPanelRendering:
    """Verify all dashboard panels are present and visible."""

    def test_header_bar(self, page: Page):
        """Header bar with title, mascot, mode pill."""
        assert page.locator("#header-bar").is_visible()
        title = page.locator("#app-title").inner_text()
        assert title.upper() == "MONEY PRINTER"
        assert page.locator("#mascot-canvas").is_visible()
        assert page.locator("#mode-pill").is_visible()

    def test_control_bar(self, page: Page):
        """Control bar with bot selector and start/stop buttons."""
        assert page.locator("#control-bar").is_visible()
        assert page.locator("#bot-dropdown-btn").is_visible()
        assert page.locator(".ctrl-btn-start").is_visible()
        assert page.locator(".ctrl-btn-stop").is_visible()

    def test_portfolio_bar(self, page: Page):
        """Portfolio summary bar with key metrics."""
        bar = page.locator("#portfolio-bar")
        assert bar.is_visible()
        # Check that all portfolio cards are present
        for card_id in [
            "pf-equity",
            "pf-cash",
            "pf-exposure",
            "pf-realized",
            "pf-unrealized",
        ]:
            assert page.locator(
                f"#{card_id}"
            ).is_visible(), f"Missing portfolio card: {card_id}"

    def test_equity_curve_chart(self, page: Page):
        """Equity curve chart card renders."""
        assert page.locator("#chart-card").is_visible()
        assert page.locator("#pnl-chart").is_visible()

    def test_positions_table(self, page: Page):
        """Open positions table renders with correct headers."""
        assert page.locator("#positions-card").is_visible()
        headers = page.locator("#positions-table thead th").all_text_contents()
        expected = [
            "ID",
            "Symbol",
            "Side",
            "Contract",
            "Qty",
            "Entry",
            "Current",
            "PnL",
            "Strategy",
            "Age",
        ]
        assert headers == expected

    def test_strategy_table(self, page: Page):
        """Strategy performance table renders with correct headers."""
        assert page.locator("#strategy-card").is_visible()
        headers = page.locator("#strategy-table thead th").all_text_contents()
        expected = [
            "Strategy",
            "Signals",
            "Wins",
            "Losses",
            "Win Rate",
            "PnL",
            "Active",
        ]
        assert headers == expected

    def test_market_data_panel(self, page: Page):
        """Market data feed panel renders."""
        assert page.locator("#market-card").is_visible()

    def test_alerts_panel(self, page: Page):
        """Alerts panel renders."""
        assert page.locator("#alerts-card").is_visible()

    def test_system_log(self, page: Page):
        """System log panel renders."""
        assert page.locator("#log-card").is_visible()

    def test_data_log(self, page: Page):
        """Data log panel renders."""
        assert page.locator("#datalog-card").is_visible()

    def test_mascot_renders(self, page: Page):
        """Mascot canvas is present and has non-zero dimensions."""
        canvas = page.locator("#mascot-canvas")
        assert canvas.is_visible()
        box = canvas.bounding_box()
        assert box is not None and box["width"] > 0 and box["height"] > 0


# ----------------------------------------------------------------------- #
# WebSocket connectivity
# ----------------------------------------------------------------------- #


class TestWebSocket:
    """Verify WebSocket connection indicator."""

    def test_ws_indicator_visible(self, page: Page):
        """WebSocket connection dot is visible."""
        assert page.locator("#ws-dot").is_visible()

    def test_mode_displayed(self, page: Page):
        """Mode pill shows a valid mode (SANDBOX or LIVE)."""
        mode_text = page.locator("#mode-text").inner_text()
        assert mode_text in ("SANDBOX", "LIVE")


# ----------------------------------------------------------------------- #
# Screenshot comparison utility
# ----------------------------------------------------------------------- #


def compare_screenshots(baseline: Path, current: Path, threshold: float = 0.95) -> bool:
    """Compare two screenshots using pixel similarity.

    Returns True if images are at least `threshold` similar (0.0-1.0).
    Requires Pillow (already in requirements).
    """
    try:
        from PIL import Image
        import numpy as np

        img1 = np.array(Image.open(baseline).convert("RGB"))
        img2 = np.array(Image.open(current).convert("RGB"))

        if img1.shape != img2.shape:
            return False

        diff = np.abs(img1.astype(float) - img2.astype(float))
        max_diff = 255.0 * 3 * img1.shape[0] * img1.shape[1]
        similarity = 1.0 - (diff.sum() / max_diff)
        return similarity >= threshold
    except Exception:
        return False
