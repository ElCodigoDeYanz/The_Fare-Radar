"""robots.txt as RFC 9309 defines it.

The standard library's parser is not enough: it takes the FIRST matching rule instead of the most
specific one, and it has no `*` / `$` wildcards, so `Allow: /` followed by `Disallow: /cart/` or
`Disallow: /*?sort=` would be read as "everything allowed". Here:

  * the group is chosen by the crawler's product token (case-insensitive); with no such group the
    `*` group applies; several groups naming the same agent are merged; no group means no rules;
  * the MOST SPECIFIC (longest) matching rule wins and Allow wins a tie; no match means allowed;
  * `*` matches any run of characters and a final `$` anchors the end of the URL path;
  * an empty Disallow/Allow is ignored (nothing is forbidden by it); rules that appear before any
    User-agent line belong to no group and are ignored; at most 500 KiB are read.

Crawl-delay (seconds) and Sitemap lines are not part of the RFC but are widely published, and the
monitor honours both. Matching never uses a regular expression built from the site's text, so a
hostile robots.txt cannot make it slow.
"""

import re
from urllib.parse import quote

MAX_BYTES = 500 * 1024
MAX_DELAY = 86400.0
SAFE = "/%:@!$&'()*+,;=-._~?[]"
UA_FIELDS = {"user-agent", "useragent", "user agent"}


def normalise(path):
    """Percent-encode what must be encoded and upper-case the hex digits of existing escapes."""
    path = quote(path, safe=SAFE)
    return re.sub(r"%[0-9a-fA-F]{2}", lambda m: m.group(0).upper(), path)


def _matches(pattern, path):
    """Prefix match with `*` wildcards and an optional trailing `$` anchor (leftmost, so linear)."""
    anchored = pattern.endswith("$")
    if anchored:
        pattern = pattern[:-1]
    parts = pattern.split("*")
    if len(parts) == 1:
        return path == pattern if anchored else path.startswith(pattern)
    if not path.startswith(parts[0]):
        return False
    pos = len(parts[0])
    for middle in parts[1:-1]:
        found = path.find(middle, pos)
        if found < 0:
            return False
        pos = found + len(middle)
    last = parts[-1]
    if anchored:
        return len(path) - pos >= len(last) and path.endswith(last)
    return path.find(last, pos) >= 0


class _Group:
    def __init__(self):
        self.agents, self.rules, self.delay = [], [], None


class Robots:
    def __init__(self, groups, sitemaps):
        self.groups, self.sitemaps = groups, sitemaps

    @classmethod
    def parse(cls, text):
        groups, sitemaps = [], []
        group, collecting_agents = None, False
        for raw in (text or "")[:MAX_BYTES].lstrip("﻿").splitlines():
            line = raw.split("#", 1)[0].strip()
            field, sep, value = line.partition(":")
            if not sep:
                continue
            field, value = field.strip().lower(), value.strip()
            if field in UA_FIELDS:
                if group is None or not collecting_agents:
                    group = _Group()
                    groups.append(group)
                collecting_agents = True
                if value:
                    group.agents.append(value.split("/", 1)[0].strip().lower())
            elif field in ("allow", "disallow"):
                collecting_agents = False
                if group is not None and value:
                    group.rules.append((field == "allow", normalise(value)))
            elif field == "crawl-delay":
                collecting_agents = False
                if group is not None:
                    try:
                        delay = float(value)
                    except ValueError:
                        continue
                    if 0 <= delay <= MAX_DELAY:  # also rejects nan and inf
                        group.delay = delay
            elif field == "sitemap" and re.match(r"(?i)https?://", value):
                sitemaps.append(value)
        return cls(groups, sitemaps)

    # --- the interface the store provider uses (same names as urllib's parser) ----------------
    def _selected(self, user_agent):
        token = re.split(r"[/\s]", (user_agent or "").strip(), maxsplit=1)[0].lower()
        specific = [g for g in self.groups if token and token in g.agents]
        return specific or [g for g in self.groups if "*" in g.agents]

    def can_fetch(self, user_agent, target):
        """target: the URL path plus '?query' when there is one."""
        path = normalise(target or "/")
        best = None
        for group in self._selected(user_agent):
            for allow, pattern in group.rules:
                if _matches(pattern, path):
                    score = (len(pattern), allow)  # longest wins; on a tie Allow beats Disallow
                    if best is None or score > best:
                        best = score
        return True if best is None else best[1]

    def crawl_delay(self, user_agent):
        delays = [g.delay for g in self._selected(user_agent) if g.delay is not None]
        return max(delays) if delays else None

    def site_maps(self):
        return list(self.sitemaps)
