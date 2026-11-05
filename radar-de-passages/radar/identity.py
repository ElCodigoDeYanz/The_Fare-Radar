"""Product identity across stores. Two pages are 'the same product' only with hard evidence:
a valid GTIN/EAN/UPC (check digit verified), or the same brand + manufacturer part number.
Titles are never compared: similar names are not the same product."""

import re


def gtin_key(raw):
    """'gtin:' + the 14-digit form, or None when it is not a valid GTIN-8/12/13/14."""
    if isinstance(raw, bool) or raw is None:
        return None
    digits = re.sub(r"[\s-]", "", str(raw))
    if not digits.isdigit() or len(digits) not in (8, 12, 13, 14) or not any(c != "0" for c in digits):
        return None
    body, check = digits[:-1], int(digits[-1])
    # GS1 mod-10: from the right of the body, weights 3,1,3,1...
    total = sum(int(c) * (3 if i % 2 == 0 else 1) for i, c in enumerate(reversed(body)))
    if (10 - total % 10) % 10 != check:
        return None
    return "gtin:" + digits.zfill(14)


def _norm(text):
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def mpn_key(brand, mpn):
    """'mpn:brand|part' when both are present and the part number is specific enough."""
    if isinstance(brand, dict):
        brand = brand.get("name")
    if not isinstance(brand, str) or not isinstance(mpn, (str, int)) or isinstance(mpn, bool):
        return None
    b, m = _norm(brand), _norm(mpn)
    if len(b) < 2 or len(m) < 4:
        return None
    return f"mpn:{b}|{m}"


def match_key(ids):
    """Best stable key from extracted identifiers, or None. GTIN beats MPN."""
    if not ids:
        return None
    return gtin_key(ids.get("gtin")) or mpn_key(ids.get("brand"), ids.get("mpn"))
