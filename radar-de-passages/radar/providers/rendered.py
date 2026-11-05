"""Optional JavaScript rendering with Playwright, for the few stores whose price only exists after
their scripts run. Off by default: enable it per domain with RADAR_RENDER_DOMAINS=shop.example.

What it does NOT do (on purpose): no stealth plugins, no masking of the automation flag, no
fingerprint or header spoofing, no CAPTCHA solving. It sends the monitor's own honest User-Agent
(the same one as plain HTTP requests), obeys the same robots.txt / allow-list / per-domain gap /
circuit breaker (it is called by the store provider AFTER those checks), and a block page is
reported as a block exactly like a 403 would be.

It is a heavier tool (a browser per render, ~150-300 MB RAM while open), so use plain HTTP wherever
the page already carries JSON-LD. Install: `pip install playwright && playwright install chromium`.
"""

from .. import restrictions, trace
from ..security import strip_credentials
from urllib.parse import urlsplit
import time

BLOCKED_RESOURCES = {"image", "media", "font"}  # prices do not need them; saves bandwidth and load


class PlaywrightRenderer:
    def __init__(self, allows=lambda host: True, timeout_ms=20000, settle_ms=1500, fulfill=None):
        self.allows = allows
        self.timeout_ms = timeout_ms
        self.settle_ms = settle_ms
        self.fulfill = fulfill  # test hook: callable(route) that answers requests without a network

    def render(self, url, user_agent, egress_profile=None):
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import TimeoutError as PlaywrightTimeout
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - depends on the deployment
            from .base import ProviderError

            raise ProviderError(
                "Renderizado pedido pero Playwright no está instalado (pip install playwright)."
            ) from exc
        host = urlsplit(url).hostname
        started = time.monotonic()
        egress = getattr(egress_profile, "name", "direct")
        proxy = None
        if egress_profile is not None and getattr(egress_profile, "url", None):
            proxy = {"server": egress_profile.url.reveal()}
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                try:
                    context = browser.new_context(user_agent=user_agent, proxy=proxy, java_script_enabled=True)
                    page = context.new_page()
                    page.route("**/*", self._route)
                    response = page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                    page.wait_for_timeout(self.settle_ms)  # let price scripts run; no polling tricks
                    html = page.content()
                    status = response.status if response else 200
                finally:
                    browser.close()
        except PlaywrightTimeout as exc:
            trace.add(host, strip_credentials(url), None, time.monotonic() - started, 0, egress)
            raise restrictions.classify_exception_text("timeout") from exc
        except PlaywrightError as exc:
            trace.add(host, strip_credentials(url), None, time.monotonic() - started, 0, egress)
            raise restrictions.classify_exception_text("network") from exc
        trace.add(host, strip_credentials(url), status, time.monotonic() - started, len(html), egress, "text/html")
        if status != 200:
            problem = restrictions.classify_response(status, html)
            if problem is not None:
                raise problem.with_evidence(status, url)
        probe = restrictions.classify_response(200, html, has_data="application/ld+json" in html)
        if probe is not None:
            raise probe.with_evidence(200, url)
        return html

    def _route(self, route):
        request = route.request
        if self.fulfill is not None:
            return self.fulfill(route)
        host = urlsplit(request.url).hostname or ""
        if urlsplit(request.url).scheme != "https" or not self.allows(host):
            return route.abort()  # only the allow-listed store itself: no third-party or internal hosts
        if request.resource_type in BLOCKED_RESOURCES:
            return route.abort()
        return route.continue_()
