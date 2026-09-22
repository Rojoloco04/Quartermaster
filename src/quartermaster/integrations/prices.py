"""Wishlist price checks.

The wishlist is a plain Notion page the owner already maintains by hand - a
mix of to-do items, bulleted items, and bookmark blocks, some linked to a
product and some not yet. No separate list, no per-retailer parser. Price is
read from whatever JSON-LD ``Product`` markup the page itself publishes, the
same structured data most storefronts already emit for search engines. A
page that publishes none of that reports "couldn't check", never "no
change" - inventing a lack of change from a parse failure would be worse
than saying nothing.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

import httpx

from .. import db
from ..config import Settings
from .notion import NotionClient

USER_AGENT = "Mozilla/5.0 (compatible; Quartermaster/0.1; +personal price watch)"
MAX_BODY_BYTES = 2_000_000  # generous for a product page; stops a runaway download

_SCRIPT_LD_JSON = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.IGNORECASE | re.DOTALL
)


class PriceError(RuntimeError):
    pass


# --- Reading the wishlist from Notion -------------------------------------------

# Block types that can carry an item's text and, once the owner links it, a URL.
_TEXT_BLOCK_TYPES = ("to_do", "bulleted_list_item", "numbered_list_item", "paragraph", "toggle")


def _item_from_block(block: dict) -> dict | None:
    """One wishlist item from one top-level block, or None if it has no link
    yet - an item is only checkable once the owner has attached a URL,
    whether that's a bookmark block or a hyperlink on a checklist line."""
    block_id = str(block.get("id", "")).replace("-", "")
    block_type = block.get("type")

    if block_type == "bookmark":
        url = (block.get("bookmark") or {}).get("url")
        if not url:
            return None
        caption = "".join(t.get("plain_text", "") for t in (block.get("bookmark") or {}).get("caption") or [])
        return {"block_id": block_id, "name": caption or url, "url": url}

    if block_type in _TEXT_BLOCK_TYPES:
        rich_text = (block.get(block_type) or {}).get("rich_text") or []
        url = next((rt["href"] for rt in rich_text if rt.get("href")), None)
        if not url:
            return None
        name = "".join(rt.get("plain_text", "") for rt in rich_text)
        return {"block_id": block_id, "name": name, "url": url}

    return None


def list_wishlist_items(settings: Settings) -> list[dict]:
    """Every linked item on the wishlist page, as ``{block_id, name, url}``.

    Only the page's direct children are read - the real page is flat today,
    and this isn't trying to be a general block-tree walker.
    """
    page_id = (settings.prefs.get("wishlist") or {}).get("page_id") or ""
    if not page_id:
        raise PriceError("No wishlist page configured. Set wishlist.page_id in config.toml.")
    settings.require("notion_token")

    items: list[dict] = []
    with NotionClient(settings.notion_token or "") as client:
        for block in client.retrieve_block_children(page_id):
            item = _item_from_block(block)
            if item:
                items.append(item)
    return items


# --- Extracting a price from a page ----------------------------------------------


def _candidate_products(payload: Any) -> list[dict]:
    """Every object in a JSON-LD payload whose @type mentions Product,
    walking @graph and top-level arrays without assuming which shape a site used."""
    found: list[dict] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        type_ = node.get("@type")
        types = type_ if isinstance(type_, list) else [type_]
        if any(isinstance(t, str) and "product" in t.lower() for t in types):
            found.append(node)
        if "@graph" in node:
            walk(node["@graph"])

    walk(payload)
    return found


def _offer_price(offers: Any) -> tuple[float, str] | None:
    """The lowest numeric price out of an Offer, an AggregateOffer, or a list of either."""
    if isinstance(offers, list):
        prices = [p for o in offers if (p := _offer_price(o)) is not None]
        return min(prices, key=lambda p: p[0]) if prices else None
    if not isinstance(offers, dict):
        return None

    currency = offers.get("priceCurrency") or "USD"
    raw = offers.get("price")
    if raw is None:
        raw = offers.get("lowPrice")
    if raw is None:
        return None
    try:
        return float(str(raw).replace(",", "")), currency
    except ValueError:
        return None


def extract_price(html: str) -> dict:
    """``{ok: True, price_cents, currency}`` or ``{ok: False, note}``.

    Never guesses: a page with no recognizable ``Product`` JSON-LD is a
    failed check, not a reported price of "no change".
    """
    for match in _SCRIPT_LD_JSON.finditer(html):
        raw = match.group(1).strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except ValueError:
            continue
        for product in _candidate_products(payload):
            found = _offer_price(product.get("offers"))
            if found is not None:
                price, currency = found
                return {"ok": True, "price_cents": round(price * 100), "currency": currency}

    return {"ok": False, "note": "no Product JSON-LD found on the page"}


def check_price(url: str) -> dict:
    # The URL comes from a Notion page, so only plain web links are fetched -
    # never file://, or whatever else a stray paste might hold.
    if not url.lower().startswith(("http://", "https://")):
        return {"ok": False, "note": "not an http(s) link"}
    try:
        # Streamed, so MAX_BODY_BYTES bounds the download itself rather than
        # truncating a page already held in memory.
        with httpx.stream(
            "GET", url, headers={"User-Agent": USER_AGENT}, timeout=15, follow_redirects=True
        ) as resp:
            if resp.status_code >= 400:
                return {"ok": False, "note": f"http {resp.status_code}"}
            chunks: list[bytes] = []
            size = 0
            for chunk in resp.iter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size >= MAX_BODY_BYTES:
                    break
            body = b"".join(chunks)[:MAX_BODY_BYTES].decode(resp.encoding or "utf-8", "replace")
    except httpx.HTTPError as exc:
        return {"ok": False, "note": f"request failed: {exc}"}

    return extract_price(body)


# --- Orchestration ----------------------------------------------------------------


def check_all(settings: Settings, conn: sqlite3.Connection) -> dict:
    """Check every wishlist item, record every observation, and report drops
    and failures separately. Returns ``{drops: [...], failures: [...]}``."""
    drops: list[dict] = []
    failures: list[dict] = []

    for item in list_wishlist_items(settings):
        previous = db.latest_price(conn, item["url"])
        result = check_price(item["url"])

        db.record_price_check(
            conn,
            url=item["url"],
            item_name=item["name"],
            price_cents=result.get("price_cents"),
            currency=result.get("currency", "USD"),
            ok=result["ok"],
            note=result.get("note", ""),
        )

        if not result["ok"]:
            failures.append({"item_name": item["name"], "url": item["url"]})
            continue

        if previous is not None and result["price_cents"] < previous["price_cents"]:
            drops.append(
                {
                    "block_id": item["block_id"],
                    "item_name": item["name"],
                    "url": item["url"],
                    "old_cents": previous["price_cents"],
                    "new_cents": result["price_cents"],
                    "currency": result.get("currency", "USD"),
                }
            )

    return {"drops": drops, "failures": failures}
