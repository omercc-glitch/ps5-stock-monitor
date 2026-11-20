import copy
import http.server
import json
import os
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor  # noqa: E402

FIX = Path(__file__).parent / "fixtures"


class IsConsoleTests(unittest.TestCase):
    def test_consoles(self):
        for name in [
            "קונסולה PS5 Blu Ray Edition 1TB עם 2 בקרים",
            "קונסולה Sony PS5 Blu Ray Slim 1TB New",
            "סוני פלייסטיישן 5 PlayStation 5 – PS5 Slim Digital Edition 825GB כולל ערכת הטבה",
            "קונסולה לבנה Playstation 5 Slim 1TB Digital פלייסטיישן 5",
            "PlayStation 5 Pro 2TB",
        ]:
            self.assertTrue(monitor.is_console(name), name)

    def test_not_consoles(self):
        for name in [
            "מסך נייד של PlayStation Portal לקונסולת PS5",
            "בקר אלחוטי DualSense לקונסולת PS5",
            "מארז PlayStation VR2",
            "כיסוי מקורי לקונסולה PlayStation 5",
            "קונסולה Nintendo Switch 2",
            "קונסולה Xbox Series S 512GB",
            "God of War PS5 Standard Edition",
        ]:
            self.assertFalse(monitor.is_console(name), name)


class Fake:
    """מחליף את monitor.fetch בתשובות מוכנות."""

    def __init__(self, pages):
        self.pages = pages

    def __call__(self, url, timeout=30):
        for key, val in self.pages.items():
            if key in url:
                if isinstance(val, Exception):
                    raise val
                return val
        raise monitor.FetchError("לא מוכן: " + url)


class RunTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self._orig_fetch, self._orig_notify, self._orig_sleep = monitor.fetch, monitor.notify, monitor._sleep
        monitor._sleep = lambda s: None
        monitor.notify = lambda title, message, **kw: self.sent.append((title, message, kw)) or True
        self.krav = json.loads((FIX / "kravitz_products.json").read_text(encoding="utf-8"))
        self.store = {"name": "קרביץ", "type": "shopify", "url": "https://shop.example/collections/x"}

    def tearDown(self):
        monitor.fetch, monitor.notify, monitor._sleep = self._orig_fetch, self._orig_notify, self._orig_sleep

    def set_stock(self, available):
        data = copy.deepcopy(self.krav)
        for p in data["products"]:
            if "Slim 1TB" in p["title"]:
                for v in p["variants"]:
                    v["available"] = available
        monitor.fetch = Fake({"products.json": json.dumps(data)})

    def test_shopify_parse(self):
        self.set_stock(False)
        total, items = monitor.run_store(self.store)
        self.assertEqual((total, len(items)), (8, 2))
        self.assertFalse(any(i.available for i in items))

    def test_notify_only_on_transition(self):
        state = {"items": {}, "stores": {}, "last_heartbeat": 10**12}
        self.set_stock(False)
        monitor.run_once([self.store], state)
        self.assertEqual(self.sent, [])
        self.set_stock(True)
        monitor.run_once([self.store], state)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("PS5 במלאי", self.sent[0][0])
        monitor.run_once([self.store], state)          # עדיין במלאי: בלי התראה כפולה
        self.assertEqual(len(self.sent), 1)
        self.set_stock(False)
        monitor.run_once([self.store], state)
        self.set_stock(True)
        monitor.run_once([self.store], state)          # חזר למלאי: התראה חדשה
        self.assertEqual(len(self.sent), 2)

    def test_first_sighting_in_stock_notifies(self):
        state = {"items": {}, "stores": {}, "last_heartbeat": 10**12}
        self.set_stock(True)
        monitor.run_once([self.store], state)
        self.assertEqual(len(self.sent), 1)

    def test_failure_alert_once_after_three(self):
        state = {"items": {}, "stores": {}, "last_heartbeat": 10**12}
        monitor.fetch = Fake({"products.json": monitor.Blocked("חסום")})
        for _ in range(5):
            monitor.run_once([self.store], state)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("לא נבדקת", self.sent[0][0])

    def test_experimental_store_never_alerts(self):
        state = {"items": {}, "stores": {}, "last_heartbeat": 10**12}
        store = dict(self.store, experimental=True)
        monitor.fetch = Fake({"products.json": monitor.Blocked("חסום")})
        for _ in range(5):
            monitor.run_once([store], state)
        self.assertEqual(self.sent, [])

    def test_empty_catalog_is_failure(self):
        monitor.fetch = Fake({"products.json": json.dumps({"products": []})})
        with self.assertRaises(monitor.FetchError):
            monitor.run_store(self.store)

    def test_ivory_jsonld(self):
        monitor.fetch = Fake({"ivory": (FIX / "ivory_list.html").read_text(encoding="utf-8")})
        store = {"name": "Ivory", "type": "jsonld_list", "url": "https://www.ivory.co.il/Sony_Playstation_5.html"}
        total, items = monitor.run_store(store)
        self.assertEqual((total, len(items)), (2, 2))
        self.assertTrue(items[0].available)           # הקובץ נשמר כשהדגם הדיגיטלי היה במלאי
        self.assertFalse(items[1].available)

    def test_product_page_regex_fallback(self):
        page = '<title>קונסולה PS5 Slim - באג</title> "availability": "http://schema.org/InStock" "price":"2199.0"'
        item = monitor.parse_product_page("https://x/p", page, "Bug")
        self.assertTrue(item.available)
        self.assertEqual(item.name, "קונסולה PS5 Slim")
        self.assertEqual(item.price, "2199.0")

    def test_block_page_detected(self):
        class R:
            status = 200
            headers = type("H", (), {"get_content_charset": lambda s: "utf-8"})()
            def read(self): return "<html>_Incapsula_Resource</html>".encode()
            def __enter__(self): return self
            def __exit__(self, *a): return False
        orig = monitor.urllib.request.urlopen
        monitor.urllib.request.urlopen = lambda *a, **k: R()
        try:
            with self.assertRaises(monitor.Blocked):
                monitor.fetch("https://x.example/")
        finally:
            monitor.urllib.request.urlopen = orig


class RateLimitTests(unittest.TestCase):
    def setUp(self):
        self.sleeps = []
        self._orig = (monitor._sleep, monitor.urllib.request.urlopen)
        monitor._sleep = self.sleeps.append

    def tearDown(self):
        monitor._sleep, monitor.urllib.request.urlopen = self._orig

    @staticmethod
    def _err(retry_after):
        import email.message
        import urllib.error
        h = email.message.Message()
        if retry_after is not None:
            h["Retry-After"] = retry_after
        return urllib.error.HTTPError("https://x.example/", 429, "Too Many", h, None)

    @staticmethod
    def _ok(body="<html>ok</html>"):
        class R:
            status = 200
            headers = type("H", (), {"get_content_charset": lambda s: "utf-8"})()
            def read(self): return body.encode()
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return R()

    def test_retries_once_then_succeeds(self):
        calls = iter([self._err("7"), self._ok()])
        def fake(*a, **k):
            r = next(calls)
            if isinstance(r, Exception):
                raise r
            return r
        monitor.urllib.request.urlopen = fake
        self.assertIn("ok", monitor.fetch("https://x.example/"))
        self.assertEqual(self.sleeps, [7])

    def test_gives_up_after_second_429(self):
        def fake(*a, **k):
            raise self._err(None)
        monitor.urllib.request.urlopen = fake
        with self.assertRaises(monitor.Blocked):
            monitor.fetch("https://x.example/")
        self.assertEqual(self.sleeps, [10])             # ברירת מחדל כשאין Retry-After

    def test_retry_after_is_capped(self):
        calls = iter([self._err("99999"), self._ok()])
        def fake(*a, **k):
            r = next(calls)
            if isinstance(r, Exception):
                raise r
            return r
        monitor.urllib.request.urlopen = fake
        monitor.fetch("https://x.example/")
        self.assertEqual(self.sleeps, [monitor.MAX_RETRY_WAIT])

    def test_price_from_price_specification(self):
        page = (
            '<title>קונסולה PS5 Slim</title><script type="application/ld+json">'
            '{"@type":"Product","name":"PS5","offers":[{"@type":"Offer","availability":"https://schema.org/InStock",'
            '"priceSpecification":[{"@type":"UnitPriceSpecification","price":"2499.00"}]}]}</script>'
        )
        item = monitor.parse_product_page("https://x/p", page, "CWC")
        self.assertTrue(item.available)
        self.assertEqual(item.price, "2499.00")


class NtfyTests(unittest.TestCase):
    def test_json_publish_with_hebrew(self):
        received = {}

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                received["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8"))
                received["ctype"] = self.headers["Content-Type"]
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")
            def log_message(self, *a): pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.handle_request, daemon=True).start()
        os.environ["NTFY_SERVER"] = f"http://127.0.0.1:{srv.server_port}"
        os.environ["NTFY_TOPIC"] = "test-topic_1"
        try:
            ok = monitor.notify("כותרת", "הודעה בעברית", click="https://x/y", priority=5, tags=["a"])
        finally:
            del os.environ["NTFY_SERVER"], os.environ["NTFY_TOPIC"]
            srv.server_close()
        self.assertTrue(ok)
        self.assertEqual(received["body"]["title"], "כותרת")
        self.assertEqual(received["body"]["topic"], "test-topic_1")
        self.assertEqual(received["body"]["click"], "https://x/y")

    def test_invalid_topic_rejected(self):
        os.environ["NTFY_TOPIC"] = "bad topic!"
        try:
            self.assertFalse(monitor.notify("a", "b"))
        finally:
            del os.environ["NTFY_TOPIC"]


if __name__ == "__main__":
    unittest.main()
