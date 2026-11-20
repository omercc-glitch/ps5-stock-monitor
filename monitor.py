#!/usr/bin/env python3
"""ps5-stock-monitor: בודק חנויות ישראליות וממליץ בהתראת ntfy כשקונסולת PS5 חוזרת למלאי.

הסקריפט משתמש בספרייה הסטנדרטית של Python בלבד.
שימוש:
    monitor.py                 ריצה אחת (זה מה שהטיימר מפעיל)
    monitor.py --check         בדיקה ידנית: מדפיס מצב כל חנות, בלי התראות ובלי שמירת מצב
    monitor.py --test-notify   שולח התראת ניסוי
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) ps5-stock-monitor/1.0"
FAILS_BEFORE_ALERT = 3          # כמה ריצות רצופות כושלות לפני התראה על חנות
HEARTBEAT_SECONDS = 7 * 24 * 3600
STORE_DELAY = 2                 # שניות בין חנויות, כדי לא להציף אתרים
PAGE_DELAY = 1                  # שניות בין דפי מוצר באותה חנות
MAX_RETRY_WAIT = 60             # תקרה להמתנה לפי Retry-After
_sleep = time.sleep
BLOCK_MARKERS = ("_incapsula_resource", "captcha", "access denied", "הגישה נחסמה", "cf-chl", "attention required")
AVAILABLE_VALUES = {"instock", "limitedavailability", "onlineonly", "instoreonly"}

# --- זיהוי קונסולה לפי שם מוצר -------------------------------------------------
PS5_RE = re.compile(r"(ps5|playstation ?5|פלייסטיישן ?5)", re.I)
CONSOLE_RE = re.compile(
    r"((?<![א-ת])קונסול|console|slim|\bpro\b|\b(?:825|1|2)\s?tb\b|digital edition|blu[- ]?ray|disc edition|כונן דיסקים)",
    re.I,
)
EXCLUDE_RE = re.compile(
    r"(portal|פורטל|vr2|מסך נייד|כיסוי|cover|stand|מעמד|gift|כרטיס מתנה|headset|אוזני|pulse|charging|תחנת טעינה)",
    re.I,
)
STARTS_WITH_ACCESSORY_RE = re.compile(r"^\s*(בקר|שלט|controller|dualsense|dualshock)", re.I)


def is_console(name: str) -> bool:
    if not PS5_RE.search(name):
        return False
    if EXCLUDE_RE.search(name) or STARTS_WITH_ACCESSORY_RE.search(name):
        return False
    return bool(CONSOLE_RE.search(name))


# --- מבנים ושגיאות --------------------------------------------------------------
@dataclass
class Item:
    store: str
    name: str
    url: str
    available: bool
    price: str | None = None

    @property
    def key(self) -> str:
        return f"{self.store}|{self.url}"


class FetchError(Exception):
    pass


class Blocked(FetchError):
    pass


class RateLimited(Exception):
    def __init__(self, wait: int):
        super().__init__(f"429, wait {wait}s")
        self.wait = wait


def quote_url(url: str) -> str:
    return urllib.parse.quote(url, safe=":/?&=%+,#@!$'()*;~-._[]")


def _retry_after(headers) -> int:
    try:
        return max(1, min(int(headers.get("Retry-After", "")), MAX_RETRY_WAIT))
    except (TypeError, ValueError):
        return 10


def fetch(url: str, timeout: int = 30) -> str:
    """קורא דף. על HTTP 429 ממתין לפי Retry-After ומנסה פעם נוספת."""
    for attempt in (1, 2):
        try:
            return _fetch_once(url, timeout)
        except RateLimited as e:
            if attempt == 2:
                raise Blocked(f"החנות מגבילה קצב (HTTP 429), גם אחרי המתנה של {e.wait} שניות") from e
            print(f"  429 מ-{urllib.parse.urlsplit(url).netloc}, ממתין {e.wait} שניות ומנסה שוב")
            _sleep(e.wait)
    raise AssertionError("unreachable")


def _fetch_once(url: str, timeout: int) -> str:
    req = urllib.request.Request(
        quote_url(url),
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            raw = resp.read()
            charset = resp.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimited(_retry_after(e.headers)) from e
        status, raw, charset = e.code, e.read(), "utf-8"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise FetchError(f"שגיאת רשת: {e}") from e
    text = raw.decode(charset, errors="replace")
    if status == 403:
        raise Blocked("החנות חסמה את הבקשה (HTTP 403)")
    if status >= 400:
        raise FetchError(f"HTTP {status}")
    if len(text) < 30000 and any(m in text.lower() for m in BLOCK_MARKERS):
        raise Blocked("נראה שהחנות הציגה דף חסימה או CAPTCHA")
    return text


# --- פרסרים ---------------------------------------------------------------------
def _offer_availability(offers) -> tuple[bool | None, str | None]:
    """מחזיר (זמין?, מחיר) מתוך שדה offers של schema.org."""
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if not isinstance(offers, dict):
        return None, None
    if "offers" in offers and "availability" not in offers:
        return _offer_availability(offers["offers"])
    price = offers.get("price") or offers.get("lowPrice")
    if price is None:
        spec = offers.get("priceSpecification")
        spec = spec[0] if isinstance(spec, list) and spec else spec
        if isinstance(spec, dict):
            price = spec.get("price")
    avail = offers.get("availability")
    if avail is None:
        return None, str(price) if price is not None else None
    return str(avail).rsplit("/", 1)[-1].lower() in AVAILABLE_VALUES, str(price) if price is not None else None


def _walk_jsonld(node, out: list):
    if isinstance(node, list):
        for n in node:
            _walk_jsonld(n, out)
    elif isinstance(node, dict):
        types = node.get("@type")
        types = types if isinstance(types, list) else [types]
        if "Product" in types and "name" in node:
            out.append(node)
        for k in ("@graph", "itemListElement", "item", "mainEntity"):
            if k in node:
                _walk_jsonld(node[k], out)


def parse_jsonld_products(page: str) -> list[dict]:
    products: list[dict] = []
    for m in re.finditer(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', page, re.S | re.I):
        try:
            data = json.loads(html.unescape(m.group(1)) if "&quot;" in m.group(1) else m.group(1))
        except ValueError:
            continue
        _walk_jsonld(data, products)
    return products


def check_jsonld_list(store: dict) -> tuple[int, list[Item]]:
    page = fetch(store["url"])
    products = parse_jsonld_products(page)
    items = []
    for p in products:
        avail, price = _offer_availability(p.get("offers"))
        if avail is None or not is_console(p["name"]):
            continue
        offers = p.get("offers")
        url = p.get("url") or (offers.get("url") if isinstance(offers, dict) else None) or store["url"]
        items.append(Item(store["name"], p["name"].strip(), url, avail, price))
    return len(products), items


def check_shopify(store: dict) -> tuple[int, list[Item]]:
    parts = urllib.parse.urlsplit(store["url"])
    origin = f"{parts.scheme}://{parts.netloc}"
    base = origin + parts.path.rstrip("/")
    products: list[dict] = []
    for page_no in range(1, 5):
        text = fetch(f"{base}/products.json?limit=250&page={page_no}")
        try:
            batch = json.loads(text)["products"]
        except (ValueError, KeyError) as e:
            raise FetchError("התשובה אינה בפורמט Shopify הצפוי") from e
        products += batch
        if len(batch) < 250:
            break
    items = []
    for p in products:
        if not is_console(p["title"]):
            continue
        variants = p.get("variants", [])
        prices = [float(v["price"]) for v in variants if v.get("price")]
        items.append(
            Item(
                store["name"],
                p["title"].strip(),
                f"{origin}/products/{p['handle']}",
                any(v.get("available") for v in variants),
                f"{min(prices):.0f}" if prices else None,
            )
        )
    return len(products), items


def _title(page: str) -> str:
    m = re.search(r"<title>(.*?)</title>", page, re.S) or re.search(r'<meta property="og:title" content="([^"]+)"', page)
    title = html.unescape(re.sub(r"\s+", " ", m.group(1))).strip() if m else ""
    return re.sub(r"\s+-\s+באג$", "", title)


def parse_product_page(url: str, page: str, store_name: str) -> Item:
    for p in parse_jsonld_products(page):
        avail, price = _offer_availability(p.get("offers"))
        if avail is not None:
            return Item(store_name, _title(page) or p["name"].strip(), url, avail, price)
    m = re.search(r'"availability"\s*:\s*"([^"]+)"', page) or re.search(
        r'itemprop="availability"[^>]*(?:content|href)="([^"]+)"', page
    )
    if not m:
        raise FetchError(f"לא נמצא סימון מלאי בדף {url}")
    avail = m.group(1).rsplit("/", 1)[-1].lower() in AVAILABLE_VALUES
    pm = re.search(r'"price"\s*:\s*"?([0-9][0-9.,]*)', page)
    return Item(store_name, _title(page), url, avail, pm.group(1) if pm else None)


def check_product_pages(store: dict) -> tuple[int, list[Item]]:
    urls = list(store.get("product_urls", []))
    disc = store.get("discover")
    if disc:
        rx = re.compile(disc["link_regex"], re.I)
        skip = re.compile(disc.get("skip_regex", "$^"), re.I)
        origin = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(disc["category_urls"][0]))
        for cat in disc["category_urls"]:
            try:
                page = fetch(cat)
            except FetchError as e:
                print(f"  אזהרה: לא הצלחתי לקרוא קטגוריה {cat}: {e}")
                continue
            for href in re.findall(r'href="([^"#]+)"', page):
                full = href if href.startswith("http") else origin + href
                if rx.search(full) and not skip.search(full) and full not in urls:
                    urls.append(full)
        urls = urls[: store.get("max_pages", 25)]
    items, errors = [], []
    for n, u in enumerate(urls):
        if n:
            _sleep(PAGE_DELAY)
        try:
            item = parse_product_page(u, fetch(u), store["name"])
        except FetchError as e:
            errors.append(f"{u}: {e}")
            continue
        if u in store.get("product_urls", []) or is_console(item.name):
            items.append(item)
    if urls and not items and errors:
        raise FetchError(errors[0])
    for e in errors:
        print(f"  אזהרה: {e}")
    return len(urls), items


def check_auto(store: dict) -> tuple[int, list[Item]]:
    """לחנויות שעדיין אין להן פרסר ייעודי: מנסה Shopify ואז JSON-LD."""
    if "/collections/" in store["url"]:
        try:
            return check_shopify(store)
        except FetchError:
            pass
    total, items = check_jsonld_list(store)
    if total == 0:
        raise FetchError("לא זוהו מוצרים בדף (ייתכן שהאתר נטען ב-JavaScript ודורש פרסר ייעודי)")
    return total, items


CHECKERS = {
    "shopify": check_shopify,
    "jsonld_list": check_jsonld_list,
    "product_pages": check_product_pages,
    "auto": check_auto,
}


def run_store(store: dict) -> tuple[int, list[Item]]:
    total, items = CHECKERS[store["type"]](store)
    if total == 0:
        raise FetchError("הבדיקה לא החזירה אף מוצר")
    return total, items


# --- התראות ---------------------------------------------------------------------
def load_env_file() -> None:
    path = Path(os.environ.get("PS5_MONITOR_ENV", Path.home() / ".config/ps5-monitor/env"))
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def notify(title: str, message: str, click: str | None = None, priority: int = 3, tags: list[str] | None = None) -> bool:
    topic = os.environ.get("NTFY_TOPIC", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", topic):
        print("שגיאה: NTFY_TOPIC לא מוגדר או לא תקין", file=sys.stderr)
        return False
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    payload = {"topic": topic, "title": title, "message": message, "priority": priority}
    if click:
        payload["click"] = click
    if tags:
        payload["tags"] = tags
    req = urllib.request.Request(
        server + "/",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"שליחת ההתראה נכשלה: {e}", file=sys.stderr)
        return False


def notify_in_stock(item: Item, store: dict) -> bool:
    note = store.get("warranty_note", "לפני קנייה: לוודא מול החנות שזו אחריות יבואן רשמי.")
    price = f"\nמחיר: {item.price} ₪" if item.price else ""
    return notify(
        f"PS5 במלאי: {item.store}",
        f"{item.name}{price}\n{note}",
        click=item.url,
        priority=5,
        tags=["video_game", "rotating_light"],
    )


# --- מצב ------------------------------------------------------------------------
def state_path(arg: str | None) -> Path:
    if arg:
        return Path(arg)
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return base / "ps5-monitor" / "state.json"


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"items": {}, "stores": {}, "last_heartbeat": 0}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def load_stores(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["stores"]


# --- הרצה -----------------------------------------------------------------------
def run_once(stores: list[dict], state: dict, send=True) -> None:
    now = int(time.time())
    for n, store in enumerate(stores):
        if n:
            _sleep(STORE_DELAY)
        name = store["name"]
        st = state["stores"].setdefault(name, {"fails": 0, "alerted": False})
        try:
            total, items = run_store(store)
        except FetchError as e:
            st["fails"] += 1
            print(f"[{name}] כשל ({st['fails']}): {e}")
            if send and not store.get("experimental") and st["fails"] >= FAILS_BEFORE_ALERT and not st["alerted"]:
                if notify(f"החנות {name} לא נבדקת", f"{st['fails']} ניסיונות רצופים נכשלו.\n{e}", priority=3, tags=["warning"]):
                    st["alerted"] = True
            continue
        st["fails"], st["alerted"] = 0, False
        seen = set()
        for item in items:
            seen.add(item.key)
            prev = state["items"].get(item.key)
            if item.available and (prev is None or not prev["available"]):
                print(f"[{name}] במלאי: {item.name} ({item.price})")
                if send:
                    notify_in_stock(item, store)
            state["items"][item.key] = {"available": item.available, "name": item.name, "price": item.price, "ts": now}
        for key, rec in state["items"].items():       # מוצר שנעלם מהרשימה נחשב לא זמין
            if key.startswith(name + "|") and key not in seen:
                rec["available"] = False
        n_av = sum(i.available for i in items)
        print(f"[{name}] תקין: {total} מוצרים בדף, {len(items)} קונסולות, {n_av} זמינות")
    if send and now - state.get("last_heartbeat", 0) > HEARTBEAT_SECONDS:
        if notify("ניטור PS5 פעיל", f"נבדקות {len(stores)} חנויות כל 10 דקות.", priority=1, tags=["white_check_mark"]):
            state["last_heartbeat"] = now


def run_check(stores: list[dict]) -> None:
    for n, store in enumerate(stores):
        if n:
            _sleep(STORE_DELAY)
        print(f"\n== {store['name']} ({store['type']})")
        try:
            total, items = run_store(store)
        except FetchError as e:
            kind = "חסומה" if isinstance(e, Blocked) else "נכשלה"
            print(f"   {kind}: {e}")
            continue
        print(f"   תקין: {total} מוצרים נקראו, {len(items)} קונסולות")
        for i in items:
            print(f"   - {'במלאי' if i.available else 'אזל  '} | {i.name} | {i.price or '?'} | {i.url}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stores", default=str(HERE / "stores.json"))
    ap.add_argument("--state")
    ap.add_argument("--check", action="store_true", help="בדיקה ידנית בלי התראות")
    ap.add_argument("--test-notify", action="store_true", help="שולח התראת ניסוי")
    args = ap.parse_args()
    load_env_file()

    if args.test_notify:
        ok = notify("ניסוי: ניטור PS5", "אם ההודעה הזו הגיעה, ההתראות עובדות.", priority=3, tags=["tada"])
        print("נשלח" if ok else "נכשל")
        return 0 if ok else 1

    stores = load_stores(Path(args.stores))
    if args.check:
        run_check(stores)
        return 0

    path = state_path(args.state)
    state = load_state(path)
    state.setdefault("items", {})
    state.setdefault("stores", {})
    state.setdefault("last_heartbeat", 0)
    first_run = not path.exists()
    if first_run:
        state["last_heartbeat"] = int(time.time())     # בלי הודעת "פעיל" מיד בהרצה הראשונה
    run_once(stores, state)
    save_state(path, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
