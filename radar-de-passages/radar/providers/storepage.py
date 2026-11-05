"""Polite product-page provider: reads schema.org JSON-LD prices from allow-listed stores.

Deliberate limits (see docs/conectores.md):
  * only domains listed in RADAR_ALLOWED_STORE_DOMAINS are ever fetched (also an SSRF guard);
  * robots.txt is honoured with RFC 9309 semantics (radar.robots: most specific rule wins, `*` and `$`
    wildcards) and its Crawl-delay raises the per-domain gap; a 401/403 or a challenge page in place
    of robots.txt is a refusal, not 'no rules';
  * the User-Agent says what we are; there is no header/fingerprint spoofing, no proxy
    rotation and no CAPTCHA solving. A block (403/429/challenge) raises BlockedError and the
    scheduler pauses the domain with exponential backoff instead of trying to get around it;
  * pages that only render prices with JavaScript are read only when a renderer is configured
    for that domain (radar.providers.rendered, opt-in); otherwise they are reported, not guessed;
  * an "AggregateOffer" low/high range or several different offers on one page is rejected
    as ambiguous rather than guessed ("price from" is never a price for a specific item).
"""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from html.parser import HTMLParser
import hashlib
import ipaddress
import json
import re
import socket
import time
from urllib.parse import urljoin, urlsplit
import httpx
from .. import restrictions, trace
from ..egress import EgressDisabled, SingleClient
from ..robots import Robots
from ..security import strip_credentials
from .base import (
    EgressError,
    EmptyResponseError,
    Offer,
    ProviderError,
    RateLimitedError,
    StructureError,
    TransientError,
    UnexpectedRedirectError,
)

MAX_BYTES = 3 * 1024 * 1024
SITEMAP_MAX_BYTES = 8 * 1024 * 1024  # compressed; discovery caps what it inflates
CONTENT_TYPES = {
    "html": {"text/html", "application/xhtml+xml"},
    "xml": {"application/xml", "text/xml", "application/gzip", "application/x-gzip",
            "application/octet-stream", "binary/octet-stream", "text/plain", "application/rss+xml"},
}
MAX_REDIRECTS = 3
ROBOTS_REDIRECTS = 5  # RFC 9309: crawlers should follow at least five consecutive redirects
NAT64 = ipaddress.ip_network("64:ff9b::/96")
NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")
ROBOTS_TTL = 6 * 3600
FRESHNESS_S = 1200  # a price counts as current for 20 minutes after each confirmation
AVAILABILITY = {
    "instock": "in_stock",
    "limitedavailability": "in_stock",
    "onlineonly": "in_stock",
    "outofstock": "out_of_stock",
    "soldout": "out_of_stock",
    "discontinued": "out_of_stock",
    "preorder": "preorder",
    "presale": "preorder",
    "backorder": "preorder",
    "instoreonly": "in_store_only",
}
CONDITION = {
    "newcondition": "new",
    "usedcondition": "used",
    "refurbishedcondition": "refurbished",
    "damagedcondition": "damaged",
}


def is_public_address(address):
    """Global unicast only. IPv6 forms that embed an IPv4 address are judged by that address
    (IPv4-mapped ::ffff:a.b.c.d and the NAT64 prefix 64:ff9b::/96 would otherwise smuggle
    loopback/private targets past a plain is_global check)."""
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip in NAT64:
            ip = ipaddress.IPv4Address(ip.packed[-4:])
        elif ip in NAT64_LOCAL:
            return False
    return ip.is_global


def default_resolver(host):
    return [info[4][0] for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)]


class _LdJson(HTMLParser):
    """Collect application/ld+json blocks without third-party dependencies."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks, self._in, self._buf = [], False, []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("type", "").lower() == "application/ld+json":
            self._in, self._buf = True, []

    def handle_data(self, data):
        if self._in:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._in:
            self.blocks.append("".join(self._buf))
            self._in = False


def _as_list(value):
    return value if isinstance(value, list) else [] if value is None else [value]


def _types(node):
    return {str(t).rsplit("/", 1)[-1] for t in _as_list(node.get("@type"))}


def _walk(node):
    """Yield every dict inside a JSON-LD document, including @graph."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def parse_price(raw):
    """Exact price → cents, or None. Refuses formats that could be misread."""
    if isinstance(raw, bool) or raw is None:
        return None
    text = str(raw).strip()
    if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d{1,4})?", text):
        text = text.replace(",", "")
    if not re.fullmatch(r"\d{1,9}(\.\d{1,4})?", text):
        return None
    try:
        cents = int((Decimal(text) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    except InvalidOperation:
        return None
    return cents if cents > 0 else None


def _short(value, mapping):
    if not isinstance(value, str):
        return None
    return mapping.get(value.rsplit("/", 1)[-1].replace(" ", "").lower())


def _brand(value):
    value = value.get("name") if isinstance(value, dict) else value
    return value.strip() if isinstance(value, str) and value.strip() else None


def _ids(node, item):
    """Identifiers published for the product (never invented): gtin, mpn, brand, title."""
    out = {}
    for source in (item, node):
        for key in ("gtin", "gtin14", "gtin13", "gtin12", "gtin8", "isbn"):
            if source.get(key) not in (None, ""):
                out.setdefault("gtin", str(source[key]))
        if source.get("mpn") not in (None, ""):
            out.setdefault("mpn", str(source["mpn"]))
    brand = _brand(node.get("brand")) or _brand(item.get("brand"))
    if brand:
        out["brand"] = brand
    if isinstance(node.get("name"), str) and node["name"].strip():
        out["title"] = " ".join(node["name"].split())[:160]
    return out


def _product_entries(html):
    """[(product_node, [offer dicts], ambiguous)] for every Product in the page's JSON-LD."""
    parser = _LdJson()
    parser.feed(html)
    entries = []
    for block in parser.blocks:
        try:
            document = json.loads(block)
        except ValueError:
            continue
        for node in _walk(document):
            if "Product" not in _types(node):
                continue
            found, ambiguous = [], False
            for offer in _as_list(node.get("offers")):
                if not isinstance(offer, dict):
                    continue
                inner = [o for o in _as_list(offer.get("offers")) if isinstance(o, dict)]
                if "AggregateOffer" in _types(offer) and not inner:
                    ambiguous = True  # only a low/high range: "price from"
                    continue
                for item in inner or [offer]:
                    price = item.get("price")
                    if price is None and isinstance(item.get("priceSpecification"), dict):
                        price = item["priceSpecification"].get("price")
                    seller = item.get("seller")
                    seller = seller.get("name") if isinstance(seller, dict) else seller
                    currency = item.get("priceCurrency") or (
                        item.get("priceSpecification") or {}
                    ).get("priceCurrency")
                    found.append(
                        {
                            "price_cents": parse_price(price),
                            "currency": currency.upper() if isinstance(currency, str) else None,
                            "sku": str(item.get("sku") or node.get("sku") or "") or None,
                            "seller": seller.strip() if isinstance(seller, str) and seller.strip() else None,
                            "availability": _short(item.get("availability"), AVAILABILITY),
                            "condition": _short(item.get("itemCondition"), CONDITION),
                            "ids": _ids(node, item),
                            "url": item.get("url") if isinstance(item.get("url"), str) else None,
                        }
                    )
            entries.append((node, found, ambiguous))
    return entries


def extract_offers(html):
    """Return ([{'price_cents','currency','sku','seller','availability','condition','ids'}], ambiguous)."""
    found, ambiguous = [], False
    for _node, offers, amb in _product_entries(html):
        found.extend(offers)
        ambiguous = ambiguous or amb
    return found, ambiguous


def extract_listing(html, base_url):
    """Products visible on a category/listing page.
    Returns (priced, urls): priced = [{'url', ...offer fields}] with an exact price and an
    absolute product URL; urls = every product URL published (with or without price)."""
    priced, urls, seen = [], [], set()
    for node, offers, _amb in _product_entries(html):
        node_url = node.get("url") if isinstance(node.get("url"), str) else None
        for offer in offers or [{"url": None, "price_cents": None}]:
            raw = offer.get("url") or node_url
            if not raw:
                continue
            try:
                url = urljoin(base_url, raw.strip())
            except ValueError:
                continue
            if urlsplit(url).scheme != "https":
                continue
            if url not in seen:
                seen.add(url)
                urls.append(url)
            if offer.get("price_cents") is not None and offer.get("currency"):
                priced.append(offer | {"url": url})
    return priced, urls


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.hrefs.append(href)


def extract_links(html, base_url):
    parser = _Links()
    parser.feed(html)
    out = []
    for href in parser.hrefs:
        url = urljoin(base_url, href.strip())
        if urlsplit(url).scheme == "https" and url not in out:
            out.append(url)
    return out


class RobotsCache:
    def __init__(self, ttl=ROBOTS_TTL):
        self.ttl, self.items = ttl, {}

    def get(self, host, now):
        entry = self.items.get(host)
        return entry[1] if entry and entry[0] > now else None

    def put(self, host, parser, now):
        self.items[host] = (now + self.ttl, parser)


class StorePageProvider:
    name = "store"
    kinds = ("product",)
    live = True
    min_interval = 60
    monthly_cap = None

    def __init__(
        self,
        allowed_domains,
        *,
        contact="",
        client=None,
        egress=None,
        renderer=None,
        render_domains=(),
        resolver=default_resolver,
        clock=time.time,
        timeout=15,
    ):
        self.allowed = tuple(sorted({d.strip().lower().lstrip(".") for d in allowed_domains if d.strip()}))
        if not self.allowed:
            raise ValueError("Se requiere al menos un dominio de tienda permitido.")
        self.user_agent = (
            f"RadarDePassages/2.0 (price monitor, respects robots.txt; contact: {contact or 'not-set'})"
        )
        self.resolver, self.clock = resolver, clock
        self.robots = RobotsCache()
        if egress is None:
            client = client or httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False)
            egress = SingleClient(client)
        self.egress = egress
        self.client = client
        self.renderer = renderer
        if renderer is not None:
            renderer.allows = self.allows  # the browser may only talk to allow-listed hosts
        self.render_domains = tuple(sorted({d.strip().lower() for d in render_domains if d.strip()}))

    # --- contract -----------------------------------------------------------------
    def domain(self, criteria):
        return criteria.domain

    def min_gap_hint(self, criteria):
        return self.crawl_delay(criteria.domain)

    def crawl_delay(self, host):
        parser = self.robots.get(host, self.clock())
        delay = parser.crawl_delay(self.user_agent) if parser else None
        try:
            return float(delay or 0)
        except (TypeError, ValueError):
            return 0.0

    def make_offer(self, criteria, item, now):
        host = criteria.domain
        conditions = {
            "product_ref": criteria.url,
            "sku": item["sku"],
            "seller": item["seller"] or f"store:{host}",
            "condition": item["condition"] or "unspecified",
            "availability": item["availability"],
        }
        identity = hashlib.sha256(
            "|".join(
                [self.name, criteria.canonical(), str(item["price_cents"]), item["currency"],
                 json.dumps(conditions, sort_keys=True)]
            ).encode()
        ).hexdigest()
        return Offer(
            self.name, identity, now, now + FRESHNESS_S, item["price_cents"], item["currency"],
            criteria, conditions, f"Página de tienda: {criteria.url}", False, item.get("ids") or None,
        )

    def fetch(self, criteria, scenario, now):
        host = criteria.domain
        self._check_allowed(host)
        text = self._get_page(criteria.url, now, render=host in self.render_domains)
        offers, ambiguous = extract_offers(text)
        offers = [o for o in offers if o["price_cents"] is not None]
        if criteria.sku:
            offers = [o for o in offers if o["sku"] == criteria.sku]
        distinct = {(o["price_cents"], o["currency"], o["sku"], o["seller"]) for o in offers}
        if not offers:
            problem = restrictions.classify_response(200, text)
            if isinstance(problem, EmptyResponseError) and "application/ld+json" in text:
                problem = None  # small but structured: it is not empty, it simply has no usable price
            if problem is not None and not isinstance(problem, StructureError):
                raise problem.with_evidence(200, criteria.url)
            raise StructureError(
                "No se encontró un precio exacto en la página (JSON-LD de schema.org). "
                + (
                    "Solo muestra un rango «desde»; no se usa como precio."
                    if ambiguous
                    else "Puede requerir JavaScript, que no se ejecuta."
                )
            ).with_evidence(200, criteria.url)
        if len(distinct) > 1:
            raise ProviderError(
                "La página tiene varias ofertas distintas; indica el SKU del seguimiento."
            )
        item = offers[0]
        if not item["currency"]:
            raise ProviderError("La página no declara la moneda; no se asume ninguna.")
        return [self.make_offer(criteria, item, now)]

    # --- documents for discovery (listings, sitemaps, robots) ------------------------------
    def fetch_document(self, url, now, expect="html", raw=False):
        """Politely fetch any allowed document: allow-list, robots.txt, SSRF check, typed errors."""
        return self._get_page(url, now, expect=expect, raw=raw, max_bytes=SITEMAP_MAX_BYTES if expect == "xml" else MAX_BYTES,
                              render=urlsplit(url).hostname in self.render_domains and expect == "html")

    def allowed_by_robots(self, url, now):
        """False when the host is not allow-listed or robots.txt disallows this URL.
        Raises (typed) if robots.txt itself cannot be read: unknown rules are not 'allowed'."""
        host = urlsplit(url).hostname or ""
        return self.allows(host) and self._robots_ok(url, now)

    def robots_sitemaps(self, host, now):
        """'Sitemap:' lines of the site's robots.txt (loads and caches robots.txt first)."""
        self._check_allowed(host)
        self._robots_ok(f"https://{host}/", now)
        parser = self.robots.get(host, now)
        return list(parser.site_maps() or []) if parser else []

    # --- internals ----------------------------------------------------------------
    def allows(self, host):
        return any(host == d or host.endswith("." + d) for d in self.allowed)

    def _check_allowed(self, host):
        if not self.allows(host):
            raise ProviderError(
                "Dominio no permitido en este despliegue (RADAR_ALLOWED_STORE_DOMAINS)."
            )

    def _check_public(self, host):
        try:
            addresses = self.resolver(host)
        except OSError as exc:
            raise TransientError("No se pudo resolver el dominio de la tienda.") from exc
        if not addresses:
            raise TransientError("El dominio no tiene direcciones.")
        for address in addresses:
            if not is_public_address(address):
                raise ProviderError("El dominio resuelve a una dirección no pública; se rechaza.")

    def _send(self, url, expect="html", raw=False, max_bytes=MAX_BYTES):
        """GET with a size cap and a content-type allow-list. Every request is traced
        (latency, bytes, status, which network exit). Returns (status, headers, text|bytes)."""
        host = urlsplit(url).hostname
        try:
            egress_name, client = self.egress.client_for(host)
        except EgressDisabled as exc:
            raise EgressError(str(exc)) from exc
        started = time.monotonic()
        try:
            with client.stream(
                "GET",
                url,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "text/html,application/xhtml+xml" if expect == "html" else "application/xml,text/xml,application/gzip,*/*;q=0.5",
                    "Accept-Language": "es-PE,es;q=0.9,en;q=0.5",
                },
            ) as response:
                latency = time.monotonic() - started  # time to response headers (AutoThrottle's latency)
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > max_bytes:
                        break
                body = b"".join(chunks)[:max_bytes]
                status, headers = response.status_code, response.headers
                encoding = response.encoding or "utf-8"
        except httpx.HTTPError as exc:
            trace.add(host, strip_credentials(url), None, time.monotonic() - started, 0, egress_name)
            raise restrictions.classify_exception(exc) from exc
        ctype = headers.get("content-type", "").split(";")[0].strip().lower()
        trace.add(host, strip_credentials(url), status, latency, len(body), egress_name, ctype)
        if status == 200 and ctype and expect in CONTENT_TYPES and ctype not in CONTENT_TYPES[expect]:
            if ctype.startswith("text/") or ctype.endswith("html"):
                # A challenge or denial page arrives as HTML where a sitemap or product page was
                # expected. Name it for what it is instead of calling it a structure change.
                problem = restrictions.classify_block(200, body[:20000].decode(encoding, "replace"))
                if problem is not None:
                    raise problem.with_evidence(status, url)
            raise StructureError(
                f"La respuesta no es del tipo esperado ({ctype}); no se interpreta."
            ).with_evidence(status, url)
        return status, headers, body if raw else body.decode(encoding, "replace")

    def _fetch_robots(self, host):
        """robots.txt with redirects followed (inside the allow-list, https only)."""
        url = f"https://{host}/robots.txt"
        for _ in range(ROBOTS_REDIRECTS + 1):
            current = urlsplit(url).hostname
            self._check_allowed(current)
            self._check_public(current)
            status, headers, text = self._send(url, expect="robots")
            if status in (301, 302, 303, 307, 308) and headers.get("location"):
                url = urljoin(url, headers["location"])
                if urlsplit(url).scheme != "https":
                    raise ProviderError("robots.txt redirige a un destino que no es https.")
                continue
            return status, headers, text
        raise TransientError("robots.txt redirige en bucle; se presume prohibido por ahora.")

    def _robots_ok(self, url, now):
        parts = urlsplit(url)
        host = parts.hostname
        parser = self.robots.get(host, now)
        if parser is None:
            status, headers, text = self._fetch_robots(host)
            robots_url = f"https://{host}/robots.txt"
            if status == 429:
                raise RateLimitedError("La tienda limitó la consulta de robots.txt.", _retry_after(headers)).with_evidence(status, robots_url)
            if status in (401, 403):
                # RFC 9309 would allow crawling after a 4xx, but a 401/403 on robots.txt is an explicit
                # refusal of automated access: stop here and let the scheduler pause the domain.
                problem = restrictions.classify_block(status, text, headers, _retry_after(headers))
                raise problem.with_evidence(status, robots_url)
            if status >= 500 or status in (301, 302, 303, 307, 308):
                # 5xx, or a redirect without a usable Location: unknown rules -> do not crawl yet.
                raise TransientError("robots.txt no disponible; se presume prohibido por ahora.")
            if status == 200:
                if _looks_like_html(text):
                    # a verification/denied page served instead of robots.txt is a refusal too
                    problem = restrictions.classify_block(200, text)
                    if problem is not None:
                        raise problem.with_evidence(status, robots_url)
                parser = Robots.parse(text)
            else:
                parser = Robots.parse("")  # other 4xx (404/410...): no robots rules published (RFC 9309)
            self.robots.put(host, parser, now)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        return parser.can_fetch(self.user_agent, path or "/")

    def _get_page(self, url, now, expect="html", raw=False, max_bytes=MAX_BYTES, render=False):
        for _ in range(MAX_REDIRECTS + 1):
            host = urlsplit(url).hostname
            self._check_allowed(host)
            if not self._robots_ok(url, now):
                raise ProviderError("robots.txt de la tienda no permite consultar esta página.")
            self._check_public(host)
            if render and self.renderer is not None and expect == "html":
                return self.renderer.render(url, self.user_agent, self.egress.profile_for(host))
            status, headers, text = self._send(url, expect, raw, max_bytes)
            if status in (301, 302, 303, 307, 308) and headers.get("location"):
                target = urljoin(url, headers["location"])
                if urlsplit(target).scheme != "https":
                    raise ProviderError("Redirección a un destino que no es https.")
                if not self.allows(urlsplit(target).hostname or ""):
                    raise UnexpectedRedirectError(
                        "La tienda redirige a un dominio no permitido en este despliegue."
                    ).with_evidence(status, url)
                problem = restrictions.classify_redirect(url, target, self.allows)
                if problem is not None:
                    raise problem.with_evidence(status, url)
                url = target
                continue
            body_text = text if isinstance(text, str) else ""
            if status != 200:
                problem = restrictions.classify_response(status, body_text, headers, _retry_after(headers))
                if problem is not None:
                    raise problem.with_evidence(status, url)
                raise ProviderError(f"Respuesta inesperada de la tienda (HTTP {status}).").with_evidence(status, url)
            if expect == "html":
                return self._verify_ok(text, url)
            return text
        raise ProviderError("Demasiadas redirecciones.")

    @staticmethod
    def _verify_ok(text, url):
        """A 200 HTML page: block/denied/captcha pages and empty bodies are caught only when the
        page carries no price data (a recaptcha script on a normal product page is not a block)."""
        probe = restrictions.classify_response(200, text, has_data="application/ld+json" in text)
        if probe is not None:
            raise probe.with_evidence(200, url)
        return text


def _looks_like_html(text):
    """By the body, not the header: servers sometimes label a plain robots.txt as text/html, and a
    real robots.txt may mention '/captcha/' in a Disallow line; a challenge page starts with markup."""
    return text.lstrip()[:200].lower().startswith(("<!doctype", "<html", "<head", "<body"))


def _retry_after(headers):
    value = headers.get("retry-after")
    if value and value.strip().isdigit():
        return min(int(value.strip()), 24 * 3600)
    return None
