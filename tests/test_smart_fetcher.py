import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from app.fetcher import is_unrendered_spa, SmartFetcher
from app.parser import HtmlParser


def test_spa_detection_heuristics():
    # Empty Angular shell
    angular_html = """
    <!doctype html>
    <html>
    <head><script src="runtime.js"></script><script src="main.js"></script></head>
    <body>
      <app-root>
        <div id="loader"><div class="spinner"></div></div>
      </app-root>
    </body>
    </html>
    """
    assert is_unrendered_spa(angular_html) is True

    # Empty React shell
    react_html = """
    <!doctype html>
    <html>
    <head><script src="/bundle.js"></script></head>
    <body>
      <div id="root"></div>
    </body>
    </html>
    """
    assert is_unrendered_spa(react_html) is True

    # Empty Next.js shell
    next_html = """
    <!doctype html>
    <html>
    <head><script src="/_next/static/chunks/main.js"></script></head>
    <body>
      <div id="__next"></div>
    </body>
    </html>
    """
    assert is_unrendered_spa(next_html) is True

    # Static HTML with content should NOT be flagged as unrendered SPA
    static_html = """
    <!doctype html>
    <html>
    <body>
      <h1>California Air Resources Board Public Comments</h1>
      <table border="1">
        <tr><th>Name</th><th>Subject</th><th>Files</th><th>Comment</th></tr>
        <tr><td>John Citizen</td><td>SB 253 greenhouse gas reporting requirements</td><td><a href="/file.pdf">file.pdf</a></td><td><a href="/comm/1">View</a></td></tr>
      </table>
      <p>Detailed discussion and information for public participation under rule 41096.</p>
    </body>
    </html>
    """
    assert is_unrendered_spa(static_html) is False


def test_diagnose_empty_listing():
    angular_html = """
    <!doctype html>
    <html>
    <body>
      <app-root><div class="loading">Loading...</div></app-root>
      <script src="main.js"></script>
    </body>
    </html>
    """
    diag = HtmlParser.diagnose_empty_listing(angular_html, "https://example.com/angular-app")
    assert "Angular (<app-root>)" in diag
    assert "Client-side SPA framework detected" in diag
    assert "0 records found" in diag

    react_html = """
    <!doctype html>
    <html><body><div id="root"></div></body></html>
    """
    diag_react = HtmlParser.diagnose_empty_listing(react_html, "https://example.com/react-app")
    assert "React (#root)" in diag_react

    empty_html = ""
    diag_empty = HtmlParser.diagnose_empty_listing(empty_html, "https://example.com/empty")
    assert "Empty response" in diag_empty


@pytest.mark.asyncio
async def test_smart_fetcher_static_flow():
    mock_net = MagicMock()
    mock_resp = MagicMock()
    mock_resp.text = "<html><body><h1>Hello World with plenty of text content to look like static page</h1></body></html>"
    mock_resp.status_code = 200
    mock_net.fetch = AsyncMock(return_value=mock_resp)

    fetcher = SmartFetcher(mock_net)
    html, meta = await fetcher.fetch_html("https://example.com/static")
    assert "Hello World" in html
    assert meta["engine"] == "httpx"
    assert meta["spa_detected"] is False
    assert meta["escalated"] is False


@pytest.mark.asyncio
async def test_smart_fetcher_browser_escalation():
    mock_net = MagicMock()
    spa_resp = MagicMock()
    spa_resp.text = "<html><head><script src='app.js'></script></head><body><app-root></app-root></body></html>"
    spa_resp.status_code = 200
    mock_net.fetch = AsyncMock(return_value=spa_resp)

    rendered_html = """
    <html><body>
      <div class="feedback-card">
        <h3>Public Feedback 101</h3>
        <p>Rendered dynamically by Playwright Chromium.</p>
        <a href="/download/sample.pdf">sample.pdf</a>
      </div>
    </body></html>
    """

    fetcher = SmartFetcher(mock_net)
    with patch("app.config.settings.ENABLE_BROWSER_RENDERING", True), \
         patch("app.fetcher.fetch_with_playwright", new=AsyncMock(return_value=(rendered_html, [{"url": "/api/feedbacks", "data": []}]))):
        html, meta = await fetcher.fetch_html("https://example.com/spa")
        assert "Playwright Chromium" in html
        assert meta["engine"] == "playwright"
        assert meta["spa_detected"] is True
        assert meta["escalated"] is True
        assert len(meta["captured_apis"]) == 1


@pytest.mark.asyncio
async def test_smart_fetcher_browser_failure_fallback():
    mock_net = MagicMock()
    spa_resp = MagicMock()
    spa_resp.text = "<html><head><script src='app.js'></script></head><body><app-root></app-root></body></html>"
    spa_resp.status_code = 200
    mock_net.fetch = AsyncMock(return_value=spa_resp)

    fetcher = SmartFetcher(mock_net)
    with patch("app.config.settings.ENABLE_BROWSER_RENDERING", True), \
         patch("app.fetcher.fetch_with_playwright", side_effect=RuntimeError("Browser launch binary missing")):
        # Should NOT crash with an uncaught exception
        html, meta = await fetcher.fetch_html("https://example.com/spa")
        assert html == spa_resp.text
        assert meta["engine"] == "httpx"
        assert meta["spa_detected"] is True
        assert meta["escalated"] is False
        assert "Browser launch binary missing" in meta["browser_error"]
