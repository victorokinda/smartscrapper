import asyncio
import logging
import re
from typing import Optional, Tuple, List, Dict, Any
from bs4 import BeautifulSoup

from app.config import settings
from app.network import NetworkClient, validate_public_url

logger = logging.getLogger(__name__)


def is_unrendered_spa(html: str) -> bool:
    """
    Heuristic detection for unrendered Single Page Application (SPA) shells
    (Angular, React, Vue, Next.js, etc.) where content is loaded dynamically.
    """
    if not html:
        return True

    spa_indicators = [
        "<app-root></app-root>",
        '<div id="root"></div>',
        '<div id="app"></div>',
        '<div id="__next"></div>',
        '<div id="mount"></div>',
        "<router-outlet></router-outlet>",
    ]
    # Check for direct indicator substrings
    for ind in spa_indicators:
        if ind in html:
            return True

    # Check for SPA root tags containing minimal / loader-only text
    root_patterns = [
        r"<app-root[^>]*>(.*?)</app-root>",
        r'<div[^>]*id=["\']root["\'][^>]*>(.*?)</div>',
        r'<div[^>]*id=["\']app["\'][^>]*>(.*?)</div>',
        r'<div[^>]*id=["\']__next["\'][^>]*>(.*?)</div>',
    ]
    for pattern in root_patterns:
        match = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
        if match:
            inner_content = match.group(1).strip()
            # If inner content is empty or only contains a spinner / loader
            clean_inner = re.sub(r"<[^>]+>", " ", inner_content).strip()
            if len(clean_inner) < 80 or any(k in inner_content.lower() for k in ("loader", "spinner", "loading")):
                return True

    # Check body text length vs script density
    soup = BeautifulSoup(html[:100000], "html.parser")
    body = soup.find("body")
    if body:
        body_text = re.sub(r"\s+", " ", body.get_text()).strip()
        scripts = soup.find_all("script")
        # Empty body with scripts present indicates client-side rendering
        if len(body_text) < 150 and len(scripts) > 0:
            return True

    return False


async def fetch_with_playwright(
    url: str,
    timeout: Optional[float] = None,
    cancel_event: Optional[asyncio.Event] = None
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Renders dynamic SPA pages using Playwright headless Chromium.
    Also sniffs background JSON API responses triggered during page hydration.
    """
    validate_public_url(url)
    to_sec = timeout if timeout is not None else settings.PLAYWRIGHT_TIMEOUT_SECONDS
    captured_apis: List[Dict[str, Any]] = []

    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        raise RuntimeError("Playwright is not installed. Run `pip install playwright && playwright install chromium`.") from e

    if cancel_event and cancel_event.is_set():
        raise asyncio.CancelledError("Crawl cancelled before browser launch")

    async with async_playwright() as p:
        args = [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-software-rasterizer"
        ]
        browser = await p.chromium.launch(
            headless=settings.PLAYWRIGHT_HEADLESS,
            args=args
        )
        try:
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
            page = await context.new_page()

            # Response listener to capture background JSON endpoints
            async def handle_response(response):
                try:
                    ct = response.headers.get("content-type", "")
                    if "application/json" in ct and response.status == 200:
                        resp_url = response.url
                        # Capture payloads for consultation / listing data
                        if any(k in resp_url.lower() for k in ("feedback", "comment", "submission", "item", "record", "initiative", "api")):
                            payload = await response.json()
                            captured_apis.append({"url": resp_url, "data": payload})
                except Exception:
                    pass

            page.on("response", handle_response)

            # Navigate and wait for network or DOM to settle
            try:
                await page.goto(url, wait_until="networkidle", timeout=int(to_sec * 1000))
            except Exception:
                # If networkidle times out (e.g. streaming connections), settle on domcontentloaded
                await page.goto(url, wait_until="domcontentloaded", timeout=int(to_sec * 1000))

            # Buffer wait for front-end frameworks (Angular / React / Vue) to mount components
            await page.wait_for_timeout(1500)

            content = await page.content()
            return content, captured_apis

        finally:
            await browser.close()


class SmartFetcher:
    """
    Hybrid fetcher that combines fast static HTTP (httpx + rate limiting)
    with automatic escalation to headless browser (Playwright) when unrendered
    Single-Page Application (SPA) shells are encountered.
    """

    def __init__(self, net_client: NetworkClient):
        self.net_client = net_client

    async def fetch_html(
        self,
        url: str,
        cancel_event: Optional[asyncio.Event] = None,
        force_browser: bool = False
    ) -> Tuple[str, Dict[str, Any]]:
        """
        Fetches page content, escalating to headless browser if an unrendered SPA is detected.
        Returns:
            (html_content, metadata)
        """
        if force_browser and settings.ENABLE_BROWSER_RENDERING:
            try:
                rendered_html, captured_apis = await fetch_with_playwright(
                    url,
                    timeout=settings.PLAYWRIGHT_TIMEOUT_SECONDS,
                    cancel_event=cancel_event
                )
                return rendered_html, {
                    "engine": "playwright",
                    "spa_detected": True,
                    "captured_apis": captured_apis,
                    "escalated": True
                }
            except Exception as e:
                logger.warning(f"Forced browser fetch failed for {url}: {e}. Falling back to static HTTP.")

        # Attempt fast static fetch first
        resp = await self.net_client.fetch(url, cancel_event=cancel_event)
        html = resp.text

        # Inspect if document is an unrendered SPA shell
        if settings.ENABLE_BROWSER_RENDERING and is_unrendered_spa(html):
            try:
                rendered_html, captured_apis = await fetch_with_playwright(
                    url,
                    timeout=settings.PLAYWRIGHT_TIMEOUT_SECONDS,
                    cancel_event=cancel_event
                )
                return rendered_html, {
                    "engine": "playwright",
                    "spa_detected": True,
                    "captured_apis": captured_apis,
                    "escalated": True
                }
            except Exception as e:
                logger.warning(f"Browser escalation failed for SPA {url}: {e}")
                return html, {
                    "engine": "httpx",
                    "spa_detected": True,
                    "browser_error": str(e),
                    "escalated": False
                }

        return html, {
            "engine": "httpx",
            "spa_detected": False,
            "escalated": False
        }
