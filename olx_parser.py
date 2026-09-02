"""
Scraper for olx.uz.

Confirmed against a real olx.uz search-page HTML dump (see README): this
site does NOT use Next.js/__NEXT_DATA__ - it's server-rendered HTML with no
embedded JSON blob of listings. So the actual, verified strategy is:

1. HTML/CSS parsing using data-cy="l-card" (listing card), data-cy=
   "ad-card-title", data-testid="ad-price", data-testid="location-date".
   Confirmed against a real page.
2. A `data-testid="total-count"` counter ("Мы нашли N объявлений") is used
   to detect the "0 results, showing recommended ads instead" case and
   short-circuit to an empty list, so recommended/unrelated ads never get
   reported as new matches.
3. For ad photos, the most robust signal turned out to be the CDN URL
   pattern itself (apollo.olxcdn.com/v1/files/<id>/image), which does not
   depend on any particular wrapping markup - see _extract_apollo_photos.

The __NEXT_DATA__/window.__PRERENDERED_STATE__ JSON strategy is kept as a
first attempt in case a future page (or an ad detail page) does embed such
a blob, but on the pages seen so far it finds nothing and falls through to
the HTML strategy.

If parsing still comes up empty, run `python olx_parser.py debug <url>`
(see bottom of this file) to dump the fetched HTML to data/debug.html for
inspection.
"""

import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass, field

import aiohttp
from bs4 import BeautifulSoup

log = logging.getLogger("olx_parser")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

BASE = "https://www.olx.uz"

# every request to olx.uz waits a random 3-5s beforehand, so we never hit
# the site faster than that regardless of how many searches/ads we process
MIN_DELAY = 3.0
MAX_DELAY = 5.0


@dataclass
class AdSummary:
    ad_id: str
    url: str
    title: str = ""
    price: str = ""
    posted: str = ""  # e.g. "Ташкент, Мирзо-Улугбекский район - Сегодня в 18:06"


@dataclass
class AdDetails:
    ad_id: str
    url: str
    title: str = ""
    price: str = ""
    description: str = ""
    location: str = ""
    contact_name: str = ""
    phone: str = ""
    photos: list = field(default_factory=list)


async def _get(session: aiohttp.ClientSession, url: str) -> str:
    await asyncio.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
    async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=30)) as resp:
        resp.raise_for_status()
        return await resp.text()


_APOLLO_RE = re.compile(
    r"(https://[\w.]*apollo\.olxcdn\.com(?::\d+)?/v1/files/[\w-]+)/image"
)


def _extract_apollo_photos(html: str) -> list:
    """
    OLX photos are served from apollo.olxcdn.com/v1/files/<id>/image;s=WxH...
    regardless of the surrounding markup, so this is a robust way to pull
    real photo URLs even if CSS selectors below don't match. We rebuild the
    URL with a larger size so Telegram gets a decent-resolution photo.
    """
    seen = []
    for base in _APOLLO_RE.findall(html):
        if base not in seen:
            seen.append(base)
    return [f"{u}/image;s=1080x1080;q=80" for u in seen]


def _extract_next_data(html: str) -> dict | None:
    """Try to pull the Next.js __NEXT_DATA__ JSON blob out of the page."""
    m = re.search(
        r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S
    )
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # fallback: any script assigning a big JSON object to a window.* var
    for m in re.finditer(
        r"window\.__(?:PRERENDERED_STATE__|INITIAL_STATE__)\s*=\s*(\{.*?\});?\s*</script>",
        html,
        re.S,
    ):
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
    return None


def _find_ads_in_json(obj) -> list:
    """Recursively search a parsed JSON blob for a list of ad-like dicts."""
    found = []

    def looks_like_ad(d: dict) -> bool:
        keys = {k.lower() for k in d.keys()}
        return "id" in keys and ("url" in keys or "title" in keys) and (
            "price" in keys or "photos" in keys or "images" in keys
        )

    def walk(node):
        if isinstance(node, dict):
            if looks_like_ad(node):
                found.append(node)
            else:
                for v in node.values():
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(obj)
    return found


def _ad_from_json(d: dict) -> AdSummary | None:
    try:
        ad_id = str(d.get("id"))
        url = d.get("url") or d.get("link") or ""
        if url and url.startswith("/"):
            url = BASE + url
        title = d.get("title") or d.get("name") or ""
        price = ""
        p = d.get("price")
        if isinstance(p, dict):
            price = str(p.get("value", {}).get("value", "") if isinstance(p.get("value"), dict) else p.get("value", ""))
            price = price or p.get("displayValue", "") or p.get("label", "")
        elif isinstance(p, (str, int, float)):
            price = str(p)
        if not ad_id or not url:
            return None
        return AdSummary(ad_id=ad_id, url=url, title=title, price=price)
    except Exception:
        return None


def _extract_total_count(html: str) -> int | None:
    """Read the 'Мы нашли N объявлений' counter. Returns None if not found."""
    m = re.search(r'data-testid="total-count"[^>]*>([^<]*)<', html)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    return int(digits) if digits else 0


def _cut_before_recommendations(html: str) -> str:
    """
    OLX shows a banner (data-testid="qa-header-message") when it falls back
    to "no results, but here are some related/recommended ads" - any l-card
    after that banner belongs to that fallback section, not the real search
    results, and must not be treated as new matching ads. Cut the HTML there.
    """
    marker = html.find('data-testid="qa-header-message"')
    if marker != -1:
        return html[:marker]
    return html


def _parse_search_html_fallback(html: str) -> list:
    html = _cut_before_recommendations(html)
    soup = BeautifulSoup(html, "lxml")
    ads = []
    cards = soup.select('[data-cy="l-card"]') or soup.select("div.offer-wrapper") or soup.select("a[href*='/d/']")
    for card in cards:
        link = card if card.name == "a" else card.select_one("a[href]")
        if not link or not link.get("href"):
            continue
        href = link["href"]
        if href.startswith("/"):
            href = BASE + href
        # the card element itself carries the numeric ad id as its id
        # attribute (id="65926385") - prefer that, it's more reliable than
        # parsing the slug out of the URL
        ad_id = card.get("id") or ""
        if not ad_id:
            ad_id_match = re.search(r"ID(\w+)\.html", href) or re.search(r"-(\d{6,})\.html", href)
            ad_id = ad_id_match.group(1) if ad_id_match else href
        title_el = card.select_one('[data-cy="ad-card-title"], h6, h4')
        price_el = card.select_one('[data-testid="ad-price"], p[data-testid="ad-price"]')
        posted_el = card.select_one('[data-testid="location-date"]')
        ads.append(
            AdSummary(
                ad_id=str(ad_id),
                url=href,
                title=title_el.get_text(strip=True) if title_el else "",
                price=price_el.get_text(strip=True) if price_el else "",
                posted=posted_el.get_text(strip=True) if posted_el else "",
            )
        )
    return ads


async def parse_search_page(session: aiohttp.ClientSession, search_url: str) -> list:
    html = await _get(session, search_url)

    total = _extract_total_count(html)
    if total == 0:
        # OLX shows an empty listing-grid plus a "no results, but here are
        # some related ads" block full of unrelated recommended cards - if
        # the real counter says 0, there is nothing to report, full stop.
        log.info("%s: total-count is 0, treating as no results", search_url)
        return []

    data = _extract_next_data(html)
    ads = []
    if data:
        for raw in _find_ads_in_json(data):
            ad = _ad_from_json(raw)
            if ad:
                ads.append(ad)
    if not ads:
        ads = _parse_search_html_fallback(html)
    if not ads:
        log.warning(
            "No ads parsed from %s (html length %d). Selectors likely need "
            "updating - see docstring at top of olx_parser.py",
            search_url,
            len(html),
        )
    # de-duplicate, preserve order
    seen = set()
    unique = []
    for ad in ads:
        if ad.ad_id in seen:
            continue
        seen.add(ad.ad_id)
        unique.append(ad)
    return unique


def _text(el) -> str:
    if not el:
        return ""
    t = el.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", t).strip()


def _parse_ad_html_fallback(html: str, url: str, ad_id: str) -> AdDetails:
    soup = BeautifulSoup(html, "lxml")

    # confirmed against a real olx.uz ad page dump
    title_el = soup.select_one('[data-cy="offer_title"], [data-testid="offer_title"], h1')
    price_el = soup.select_one('[data-testid="ad-price-container"], [data-cy="ad-price-container"] h3')
    desc_el = soup.select_one('[data-cy="ad_description"], [data-testid="ad_description"]')
    loc_el = soup.select_one('[data-cy="ad-posted-at"], [data-testid="ad-posted-at"], [data-testid="location-date"]')
    contact_el = soup.select_one('[data-testid="trader-title"], [data-testid="user-profile-user-name"], [data-cy="seller_name"]')

    photos = []
    for img in soup.select(
        '[data-testid="swiper-image"], [data-testid="swiper-image-lazy"], '
        '[data-testid="image-galery-container"] img, [data-cy="adPhotos-swiperSlide"] img'
    ):
        src = img.get("src") or img.get("data-src")
        # guard against relative/non-http src (icons, sprites, lazy
        # placeholders) - a bad URL here breaks sendMediaGroup entirely
        if src and src.startswith("http") and src not in photos:
            photos.append(src)

    phone = ""
    phone_match = re.search(r"(\+?998[\s\-]?\d{2}[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2})", html)
    if phone_match:
        phone = phone_match.group(1)

    return AdDetails(
        ad_id=ad_id,
        url=url,
        title=_text(title_el),
        price=_text(price_el),
        description=desc_el.get_text("\n", strip=True) if desc_el else "",
        location=_text(loc_el),
        contact_name=_text(contact_el),
        phone=phone,
        photos=photos[:10],
    )


async def parse_ad_details(session: aiohttp.ClientSession, ad: AdSummary) -> AdDetails:
    html = await _get(session, ad.url)
    details = None

    data = _extract_next_data(html)
    if data:
        candidates = _find_ads_in_json(data)
        raw = next((c for c in candidates if str(c.get("id")) == ad.ad_id), None)
        if raw:
            photos = []
            for key in ("photos", "images"):
                for p in raw.get(key, []) or []:
                    if isinstance(p, str):
                        u = p
                    elif isinstance(p, dict):
                        u = p.get("url") or p.get("link") or p.get("src")
                    else:
                        u = None
                    if u and u.startswith("http") and u not in photos:
                        photos.append(u)
            phone = raw.get("phone", "") or raw.get("contact", {}).get("phone", "") if isinstance(raw.get("contact"), dict) else raw.get("phone", "")
            candidate = AdDetails(
                ad_id=ad.ad_id,
                url=ad.url,
                title=raw.get("title", ad.title),
                price=ad.price,
                description=raw.get("description", "") or raw.get("body", ""),
                location=raw.get("location", {}).get("city", {}).get("name", "") if isinstance(raw.get("location"), dict) else "",
                contact_name=raw.get("contact", {}).get("name", "") if isinstance(raw.get("contact"), dict) else "",
                phone=phone or "",
                photos=photos[:10],
            )
            if candidate.title or candidate.description or candidate.photos:
                details = candidate

    if details is None:
        # fallback to html scraping (also used to fill gaps like phone
        # number, since OLX often loads the phone only after a "show phone"
        # click / API call that a plain HTML fetch will not trigger)
        details = _parse_ad_html_fallback(html, ad.url, ad.ad_id)

    if not details.photos:
        # CDN-URL-pattern extraction is more robust than any CSS selector
        # guess and works regardless of the surrounding markup
        details.photos = _extract_apollo_photos(html)[:10]

    return details


def short_query_label(url: str) -> str:
    """Extract the human-readable query token between 'q-' and the next
    '/' or '?' in an olx.uz search URL, e.g. '.../q-2-komnaty-chilanzar/?...'
    -> '2-komnaty-chilanzar'. Falls back to the full URL if not found."""
    m = re.search(r"/q-([^/?]+)", url)
    return m.group(1) if m else url


async def debug_dump(url: str, out_path: str = "data/debug.html"):
    async with aiohttp.ClientSession() as session:
        html = await _get(session, url)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"saved {len(html)} bytes to {out_path}")


if __name__ == "__main__":
    import sys

    if len(sys.argv) == 3 and sys.argv[1] == "debug":
        asyncio.run(debug_dump(sys.argv[2]))
    else:
        print("usage: python olx_parser.py debug <url>")
