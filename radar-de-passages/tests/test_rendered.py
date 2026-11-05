"""Optional JavaScript renderer. Skipped when Playwright/Chromium are not installed (they are not
required to run the monitor). No network is used: requests are answered by the test through Playwright's
route hook, so what is checked is OUR code: honest User-Agent, allow-list, typed block pages, no stealth."""

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

from radar.providers.base import AccessDeniedError, CaptchaError, NetworkFailure, TimeoutFailure  # noqa: E402
from radar.providers.rendered import BLOCKED_RESOURCES, PlaywrightRenderer  # noqa: E402

UA = "RadarDePassages/2.0 (price monitor, respects robots.txt; contact: ops@example.org)"
PRICE_BY_SCRIPT = """<!doctype html><html><head><title>Laptop</title></head><body><p>cargando...</p>
<script>
  document.addEventListener('DOMContentLoaded', function () {
    var s = document.createElement('script'); s.type = 'application/ld+json';
    s.text = JSON.stringify({"@context":"https://schema.org","@type":"Product","name":"Laptop X","sku":"S1",
      "offers":{"@type":"Offer","price":"1299.90","priceCurrency":"PEN","availability":"https://schema.org/InStock"}});
    document.head.appendChild(s);
  });
</script>""" + "<p>catálogo</p>" * 40 + "</body></html>"


def fulfilling(status=200, body=PRICE_BY_SCRIPT, seen=None):
    def handler(route):
        if seen is not None:
            seen.append({"url": route.request.url, "ua": route.request.headers.get("user-agent"), "webdriver_hint": route.request.headers.get("sec-ch-ua-platform")})
        route.fulfill(status=status, content_type="text/html; charset=utf-8", body=body)

    return handler


@pytest.fixture(scope="module", autouse=True)
def chromium_available():
    try:
        with sync_api.sync_playwright() as pw:
            pw.chromium.launch(headless=True).close()
    except Exception as exc:  # noqa: BLE001 - no browser on this machine
        pytest.skip(f"Chromium no disponible: {exc}")


def test_a_price_that_only_exists_after_javascript_runs_is_read():
    seen = []
    html = PlaywrightRenderer(fulfill=fulfilling(seen=seen)).render("https://shop.test/p/1", UA)
    assert "application/ld+json" in html and "1299.90" in html  # the script ran and built the JSON-LD
    from radar.providers.storepage import extract_offers

    offers, _ = extract_offers(html)
    assert offers[0]["price_cents"] == 129990 and offers[0]["currency"] == "PEN"
    assert seen and seen[0]["ua"] == UA  # the honest, identifying User-Agent, the same as plain HTTP requests


def test_a_challenge_page_is_reported_as_a_block_never_solved_or_worked_around():
    page = "<html><head><title>Just a moment...</title></head><body>Verify you are human " + "x" * 400 + "</body></html>"
    with pytest.raises(CaptchaError):
        PlaywrightRenderer(fulfill=fulfilling(body=page)).render("https://shop.test/p/1", UA)


def test_http_refusals_keep_their_meaning_in_the_browser_too():
    with pytest.raises(AccessDeniedError):
        PlaywrightRenderer(fulfill=fulfilling(status=403, body="forbidden")).render("https://shop.test/p/1", UA)


def test_a_normal_page_with_a_recaptcha_widget_and_a_price_is_not_a_block():
    page = PRICE_BY_SCRIPT.replace("</head>", "<script src='https://x/recaptcha.js' async></script></head>")
    assert "1299.90" in PlaywrightRenderer(fulfill=fulfilling(body=page)).render("https://shop.test/p/1", UA)


def test_the_browser_may_only_talk_to_https_allow_listed_hosts_and_skips_heavy_resources():
    renderer = PlaywrightRenderer(allows=lambda host: host == "shop.test")
    aborted = []

    class Route:
        def __init__(self, url, kind="document"):
            self.request = type("R", (), {"url": url, "resource_type": kind})()

        def abort(self):
            aborted.append(self.request.url)
            return "aborted"

        def continue_(self):
            return "continued"

    assert renderer._route(Route("https://shop.test/p/1")) == "continued"
    assert renderer._route(Route("https://evil.test/track.js", "script")) == "aborted"  # third party
    assert renderer._route(Route("http://shop.test/p/1")) == "aborted"  # not https
    assert renderer._route(Route("https://127.0.0.1/admin")) == "aborted"  # internal address
    assert renderer._route(Route("https://shop.test/hero.jpg", "image")) == "aborted"
    assert BLOCKED_RESOURCES == {"image", "media", "font"}


def test_no_stealth_options_are_ever_set():
    """Looks at what the code DOES (names, attributes, keyword arguments, string literals), not at the
    docstring that explains why it must not."""
    import ast
    import inspect
    from radar.providers import rendered

    tree = ast.parse(inspect.getsource(rendered))
    docstrings = {id(n.body[0].value) for n in ast.walk(tree) if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)) and n.body
                  and isinstance(n.body[0], ast.Expr) and isinstance(getattr(n.body[0], "value", None), ast.Constant)}
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, ast.keyword) and node.arg:
            used.add(node.arg)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            used.add(node.value)
    text = " ".join(sorted(used)).lower()
    for forbidden in ("add_init_script", "webdriver", "stealth", "navigator", "set_extra_http_headers", "extra_http_headers",
                      "fingerprint", "evaluate", "args=", "--disable-blink", "automationcontrolled", "ignore_default_args"):
        assert forbidden not in text, forbidden
    assert "user_agent" in used  # the one identity option it does set: the honest, identifying User-Agent


def test_a_navigation_that_never_finishes_is_a_timeout_not_a_hang():
    def never(route):
        route.abort("timedout")

    with pytest.raises((TimeoutFailure, NetworkFailure)):
        PlaywrightRenderer(fulfill=never, timeout_ms=1500).render("https://shop.test/p/1", UA)
