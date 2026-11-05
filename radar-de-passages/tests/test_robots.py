"""robots.txt: RFC 9309 semantics (the standard library's parser does not have them), and what the
store provider does with a robots.txt that is missing, forbidden, challenged or hostile."""

import time
import pytest
from radar.providers.base import AccessDeniedError, CaptchaError, ProviderError, RateLimitedError, SessionError, TransientError
from radar.robots import Robots, normalise
from test_providers import GOOD, NOW, crit, store

UA = "RadarDePassages/2.0 (price monitor, respects robots.txt; contact: ops@example.org)"


def allowed(text, path, ua=UA):
    return Robots.parse(text).can_fetch(ua, path)


# --- the standard's rules -----------------------------------------------------------------------------------
def test_the_most_specific_rule_wins_regardless_of_the_order_in_the_file():
    text = "User-agent: *\nAllow: /\nDisallow: /cart/\n"
    assert allowed(text, "/cart/1") is False and allowed(text, "/p/1") is True  # urllib would allow both
    reverse = "User-agent: *\nDisallow: /cart/\nAllow: /\n"
    assert allowed(reverse, "/cart/1") is False
    carve_out = "User-agent: *\nDisallow: /p/\nAllow: /p/public/\n"
    assert allowed(carve_out, "/p/secret") is False and allowed(carve_out, "/p/public/x") is True


def test_allow_wins_a_tie_and_no_match_means_allowed():
    text = "User-agent: *\nDisallow: /page\nAllow: /page\n"
    assert allowed(text, "/page") is True
    assert allowed("User-agent: *\nDisallow: /private\n", "/open") is True
    assert allowed("", "/anything") is True and allowed("User-agent: *\n", "/x") is True


def test_wildcards_and_the_end_anchor():
    text = "User-agent: *\nDisallow: /*?sort=\nDisallow: /*.pdf$\nDisallow: /a*b*c\n"
    assert allowed(text, "/laptops?sort=price") is False  # urllib treats '*' literally and would allow it
    assert allowed(text, "/laptops?color=red") is True
    assert allowed(text, "/files/manual.pdf") is False and allowed(text, "/files/manual.pdf?x=1") is True
    assert allowed(text, "/xa--b--c") is True and allowed(text, "/a--b--c") is False and allowed(text, "/a--c--b") is True
    assert allowed("User-agent: *\nDisallow: /exact$\n", "/exact") is False
    assert allowed("User-agent: *\nDisallow: /exact$\n", "/exact/more") is True


def test_a_longer_wildcard_rule_can_beat_a_shorter_plain_one():
    text = "User-agent: *\nDisallow: /shop\nAllow: /shop/*/offers\n"
    assert allowed(text, "/shop/laptops/offers") is True
    assert allowed(text, "/shop/laptops/reviews") is False


def test_empty_disallow_means_nothing_is_forbidden_and_a_root_disallow_forbids_everything():
    assert allowed("User-agent: *\nDisallow:\n", "/x") is True
    assert allowed("User-agent: *\nDisallow: /\n", "/x") is False and allowed("User-agent: *\nDisallow: /\n", "/") is False


def test_group_selection_by_product_token_with_star_as_the_fallback():
    text = (
        "User-agent: *\nDisallow: /\n\n"
        "User-agent: RadarDePassages\nDisallow: /private/\n\n"
        "User-agent: googlebot\nDisallow: /g/\n"
    )
    assert allowed(text, "/p/1") is True and allowed(text, "/private/1") is False  # its own group, not the '*' one
    assert allowed(text, "/p/1", ua="Mozilla/5.0 other") is False  # unknown agents fall back to '*'
    only_google = "User-agent: googlebot\nDisallow: /\n"
    assert allowed(only_google, "/p/1") is True  # no matching group and no '*': no rules apply
    assert allowed("USER-AGENT: radardepassages\nDISALLOW: /x\n", "/x") is False  # field names and tokens are case-insensitive


def test_several_user_agent_lines_share_a_group_and_separate_groups_for_one_agent_are_merged():
    shared = "User-agent: a\nUser-agent: RadarDePassages\nDisallow: /shared/\n"
    assert allowed(shared, "/shared/x") is False
    split = "User-agent: RadarDePassages\nDisallow: /one/\n\nUser-agent: other\nDisallow: /\n\nUser-agent: RadarDePassages\nDisallow: /two/\n"
    assert allowed(split, "/one/x") is False and allowed(split, "/two/x") is False and allowed(split, "/three") is True


def test_rules_before_any_user_agent_line_belong_to_no_group():
    assert allowed("Disallow: /\nUser-agent: *\nDisallow: /x\n", "/p") is True


def test_comments_bom_blank_lines_and_junk_are_tolerated():
    text = "\ufeff# robots\n\nUser-agent: * # everyone\nDisallow: /a # not here\nnot a field\n:\nDisallow /missing-colon\n"
    assert allowed(text, "/a/1") is False and allowed(text, "/b") is True and allowed(text, "/missing-colon") is True


def test_percent_encoding_is_compared_on_the_normalised_form():
    assert normalise("/a b/ñ%e2%82%ac") == "/a%20b/%C3%B1%E2%82%AC"
    text = "User-agent: *\nDisallow: /caf%c3%a9/\n"
    assert allowed(text, "/café/menu") is False and allowed(text, "/caf%C3%A9/menu") is False


def test_crawl_delay_is_group_bound_validated_and_bounded():
    text = "User-agent: *\nCrawl-delay: 5\n\nUser-agent: RadarDePassages\nCrawl-delay: 30\nDisallow: /x\n"
    robots = Robots.parse(text)
    assert robots.crawl_delay(UA) == 30.0 and robots.crawl_delay("Other/1.0") == 5.0
    for bad in ("abc", "-3", "nan", "inf", "99999999"):
        assert Robots.parse(f"User-agent: *\nCrawl-delay: {bad}\n").crawl_delay(UA) is None
    assert Robots.parse("User-agent: *\nCrawl-delay: 2.5\n").crawl_delay(UA) == 2.5
    assert Robots.parse("Crawl-delay: 9\nUser-agent: *\nDisallow: /x\n").crawl_delay(UA) is None  # before any group


def test_sitemap_lines_are_global_and_must_be_absolute_urls():
    text = "Sitemap: https://shop.test/a.xml\nUser-agent: *\nDisallow: /x\nSitemap: http://shop.test/b.xml\nSitemap: /relative.xml\nSitemap:\n"
    assert Robots.parse(text).site_maps() == ["https://shop.test/a.xml", "http://shop.test/b.xml"]


def test_a_hostile_robots_txt_cannot_make_the_matcher_slow():
    rules = "".join(f"Disallow: /{'*a' * 60}{i}\n" for i in range(300))
    text = "User-agent: *\n" + rules + "Disallow: /" + "*a" * 200 + "$\n"
    path = "/" + "a" * 2000
    started = time.perf_counter()
    assert Robots.parse(text).can_fetch(UA, path) in (True, False)
    assert time.perf_counter() - started < 1.0


def test_only_the_first_500_kib_are_read():
    filler = "# x\n" * (200 * 1024)  # ~800 KiB of comments
    assert allowed("User-agent: *\n" + filler + "Disallow: /late\n", "/late") is True


# --- through the store provider -----------------------------------------------------------------------------------
def provider_for(robots, page=GOOD):
    """robots: (status, body) or (status, body, headers)."""
    headers = robots[2] if len(robots) > 2 else {}
    return store({"/robots.txt": (robots[0], robots[1], headers), "/p/1": (200, page, {})})


def test_the_provider_applies_rfc_semantics_to_the_page_it_is_about_to_fetch():
    provider, seen = provider_for((200, "User-agent: *\nAllow: /\nDisallow: /p/\n"))
    with pytest.raises(ProviderError, match="robots.txt"):
        provider.fetch(crit(), "x", NOW)
    assert not any(u.endswith("/p/1") for u in seen)  # the forbidden page was never requested
    provider, seen = provider_for((200, "User-agent: *\nDisallow: /*?utm_\n"))
    assert provider.fetch(crit(), "x", NOW)[0].total_cents == 129990
    provider, seen = provider_for((200, "User-agent: *\nDisallow: /*?utm_\n"))
    with pytest.raises(ProviderError, match="robots.txt"):
        provider.fetch(crit("/p/1?utm_source=x"), "x", NOW)


def test_missing_robots_means_no_rules_but_a_refusal_is_never_read_as_permission():
    provider, _ = provider_for((404, ""))
    assert provider.fetch(crit(), "x", NOW)
    provider, _ = provider_for((410, ""))
    assert provider.fetch(crit(), "x", NOW)
    for status, error in ((403, AccessDeniedError), (401, SessionError), (429, RateLimitedError)):
        provider, seen = provider_for((status, "no"))
        with pytest.raises(error):
            provider.fetch(crit(), "x", NOW)
        assert not any(u.endswith("/p/1") for u in seen)  # nothing is asked of a site that refused robots.txt
    provider, seen = provider_for((503, ""))
    with pytest.raises(TransientError, match="robots.txt"):
        provider.fetch(crit(), "x", NOW)
    assert not any(u.endswith("/p/1") for u in seen)


def test_a_challenge_page_served_instead_of_robots_txt_is_a_refusal_not_no_rules():
    challenge = "<html><head><title>Just a moment...</title></head><body>Verify you are human</body></html>"
    provider, seen = provider_for((200, challenge, {"content-type": "text/html"}))
    with pytest.raises(CaptchaError):
        provider.fetch(crit(), "x", NOW)
    assert not any(u.endswith("/p/1") for u in seen)


def test_a_real_robots_txt_that_mentions_captcha_paths_is_not_mistaken_for_a_challenge():
    robots = "User-agent: *\nDisallow: /captcha/\nDisallow: /recaptcha-verify\nAllow: /\n"
    for label in ("text/plain", "text/html"):  # some servers label robots.txt as html: the body decides
        provider, _ = provider_for((200, robots, {"content-type": label}))
        assert provider.fetch(crit(), "x", NOW)[0].total_cents == 129990
    html_homepage = "<!doctype html><html><body>" + "<p>sitio</p>" * 100 + "</body></html>"  # SPA fallback for /robots.txt
    provider, _ = provider_for((200, html_homepage, {"content-type": "text/html"}))
    assert provider.fetch(crit(), "x", NOW)  # garbage is "no rules", not a block


def test_a_challenge_page_where_a_sitemap_was_expected_is_named_as_a_block():
    from radar.providers.base import StructureError
    from test_discovery import site

    challenge = "<html><head><title>Attention Required</title></head><body>Please complete the CAPTCHA to continue</body></html>"
    provider, _ = site({"/robots.txt": (404, "", {}), "/sitemap.xml": (200, challenge, {"content-type": "text/html"})})
    with pytest.raises(CaptchaError):
        provider.fetch_document("https://shop.test/sitemap.xml", NOW, expect="xml", raw=True)
    ordinary = "<html><body>" + "<p>nuestra tienda</p>" * 30 + "</body></html>"
    provider, _ = site({"/robots.txt": (404, "", {}), "/sitemap.xml": (200, ordinary, {"content-type": "text/html"})})
    with pytest.raises(StructureError, match="tipo esperado"):
        provider.fetch_document("https://shop.test/sitemap.xml", NOW, expect="xml", raw=True)


def test_robots_txt_is_cached_and_not_fetched_for_every_page():
    provider, seen = provider_for((200, "User-agent: *\nAllow: /\n"))
    provider.fetch(crit(), "x", NOW)
    provider.fetch(crit(), "x", NOW + 600)
    assert sum(u.endswith("/robots.txt") for u in seen) == 1


def test_crawl_delay_from_robots_txt_reaches_the_scheduler_as_a_gap_hint():
    provider, _ = provider_for((200, "User-agent: *\nCrawl-delay: 20\nAllow: /\n"))
    provider.fetch(crit(), "x", time.time())
    assert provider.min_gap_hint(crit()) == 20.0 and provider.crawl_delay("shop.test") == 20.0
