#!/usr/bin/env python3
"""
telegram-site-alert-bot: watch web pages and get a Telegram message when something changes.

It alerts on:
  - NEW items     (a new product / listing / post appears)
  - PRICE DROPS   (an item gets cheaper, optionally only below a target price)
  - BACK IN STOCK (an item goes from out of stock to in stock)

Everything is driven by config.json (pages + CSS selectors), so a new site = a new config block.
State is kept in SQLite, so restarts never re-send old alerts.

  python3 alert_bot.py --once          # one check, then exit
  python3 alert_bot.py                 # run forever (see alert-bot.service for systemd)
  python3 alert_bot.py --dry           # print alerts instead of sending them
  python3 alert_bot.py --demo          # pretend a few items are new / cheaper, to see real alerts
"""
import argparse
import html
import json
import logging
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("alert-bot")
HERE = os.path.dirname(os.path.abspath(__file__))


@dataclass
class Item:
    key: str
    title: str
    url: str
    price: Optional[float]
    in_stock: Optional[bool]


# ---------------- scraping ----------------
def parse_price(text: str) -> Optional[float]:
    m = re.search(r"\d[\d,]*(?:\.\d+)?", text or "")
    return float(m.group(0).replace(",", "")) if m else None


def extract_items(page_html: str, page_url: str, sel: dict) -> List[Item]:
    soup = BeautifulSoup(page_html, "html.parser")
    items = []
    for node in soup.select(sel["item"]):
        link = node.select_one(sel["link"]) if sel.get("link") else None
        title_el = node.select_one(sel["title"])
        if not title_el:
            continue
        title = (title_el.get(sel["title_attr"]) if sel.get("title_attr") else None) or title_el.get_text(strip=True)
        url = urljoin(page_url, link["href"]) if link and link.has_attr("href") else page_url
        price = parse_price(node.select_one(sel["price"]).get_text()) if sel.get("price") and node.select_one(sel["price"]) else None
        stock = None
        if sel.get("stock"):
            el = node.select_one(sel["stock"])
            stock = bool(el) and sel.get("in_stock_text", "in stock").lower() in el.get_text().lower()
        items.append(Item(key=url, title=title, url=url, price=price, in_stock=stock))
    return items


# ---------------- state ----------------
class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.execute("""CREATE TABLE IF NOT EXISTS items(
            watch TEXT, key TEXT, title TEXT, price REAL, in_stock INTEGER, first_seen REAL, last_seen REAL,
            PRIMARY KEY(watch, key))""")

    def get(self, watch: str, key: str):
        return self.db.execute("SELECT price, in_stock FROM items WHERE watch=? AND key=?", (watch, key)).fetchone()

    def put(self, watch: str, it: Item):
        now = time.time()
        self.db.execute("""INSERT INTO items VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(watch,key) DO UPDATE SET title=excluded.title, price=excluded.price,
            in_stock=excluded.in_stock, last_seen=excluded.last_seen""",
                        (watch, it.key, it.title, it.price, None if it.in_stock is None else int(it.in_stock), now, now))
        self.db.commit()

    def count(self, watch: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM items WHERE watch=?", (watch,)).fetchone()[0]

    def demo_tamper(self, watch: str, n: int = 3):
        """Delete a few items and raise a few prices so the next check produces real alerts."""
        keys = [r[0] for r in self.db.execute("SELECT key FROM items WHERE watch=? LIMIT ?", (watch, n * 2))]
        for k in keys[:n]:
            self.db.execute("DELETE FROM items WHERE watch=? AND key=?", (watch, k))
        for k in keys[n:]:
            self.db.execute("UPDATE items SET price = price + 5 WHERE watch=? AND key=?", (watch, k))
        self.db.commit()


# ---------------- change detection ----------------
def diff(store: Store, watch: dict, items: List[Item], first_run: bool) -> List[str]:
    name, alerts = watch["name"], []
    target = watch.get("target_price")
    for it in items:
        old = store.get(name, it.key)
        store.put(name, it)
        if first_run:
            continue  # first run only records a baseline, no alert flood
        e = html.escape
        if old is None and watch.get("alert_new", True):
            price = f" · {it.price:.2f}" if it.price is not None else ""
            alerts.append(f"🆕 <b>New</b> on {e(name)}{price}\n{e(it.title)}\n{e(it.url)}")
            continue
        if old is None:
            continue
        old_price, old_stock = old
        if (watch.get("alert_price_drop", True) and it.price is not None and old_price is not None
                and it.price < old_price and (target is None or it.price <= target)):
            pct = (old_price - it.price) / old_price * 100
            alerts.append(f"📉 <b>Price drop</b> {old_price:.2f} → {it.price:.2f} (-{pct:.0f}%)\n{e(it.title)}\n{e(it.url)}")
        if watch.get("alert_back_in_stock", True) and old_stock == 0 and it.in_stock:
            alerts.append(f"✅ <b>Back in stock</b>\n{e(it.title)}\n{e(it.url)}")
    return alerts


# ---------------- telegram ----------------
def send_telegram(token: str, chat_id: str, text: str):
    for attempt in range(3):
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=20, data={
                "chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"})
            if r.status_code == 429:
                time.sleep(r.json().get("parameters", {}).get("retry_after", 5))
                continue
            r.raise_for_status()
            time.sleep(1.1)  # stay under Telegram's per-chat rate limit
            return
        except requests.RequestException as e:
            log.warning("telegram send failed (%s), retrying", e)
            time.sleep(3 * (attempt + 1))


# ---------------- main loop ----------------
def check_all(cfg: dict, store: Store, session: requests.Session, dry: bool) -> int:
    sent = 0
    for watch in cfg["watches"]:
        first_run = store.count(watch["name"]) == 0
        items: List[Item] = []
        for url in watch["urls"]:
            try:
                r = session.get(url, timeout=20)
                r.raise_for_status()
                r.encoding = "utf-8"
                items += extract_items(r.text, url, watch["selectors"])
            except Exception as e:
                log.warning("%s: failed to fetch %s (%s)", watch["name"], url, e)
            time.sleep(cfg.get("delay_seconds", 1))
        alerts = diff(store, watch, items, first_run)
        log.info("%s: %d items, %d alerts%s", watch["name"], len(items), len(alerts), " (baseline saved)" if first_run else "")
        for a in alerts[: cfg.get("max_alerts_per_check", 20)]:
            if dry:
                print(a.replace("<b>", "").replace("</b>", ""), "\n")
            else:
                send_telegram(cfg["telegram_token"], cfg["chat_id"], a)
            sent += 1
    return sent


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--demo", action="store_true", help="make the next check produce sample alerts")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    with open(args.config) as f:
        cfg = json.load(f)
    cfg["telegram_token"] = os.environ.get("TELEGRAM_TOKEN", cfg.get("telegram_token", ""))
    cfg["chat_id"] = os.environ.get("TELEGRAM_CHAT_ID", cfg.get("chat_id", ""))
    if not args.dry and not (cfg["telegram_token"] and cfg["chat_id"]):
        sys.exit("Set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID (env or config.json), or use --dry")

    store = Store(os.path.join(HERE, cfg.get("db", "alerts.db")))
    session = requests.Session()
    session.headers["User-Agent"] = cfg.get("user_agent", "Mozilla/5.0 (site-alert-bot)")

    if args.demo:
        if any(store.count(w["name"]) == 0 for w in cfg["watches"]):
            check_all(cfg, store, session, dry=True)  # build the baseline first
        for w in cfg["watches"]:
            store.demo_tamper(w["name"])
        log.info("demo: next check will report a few 'new' items and price drops")
        args.once = True

    while True:
        check_all(cfg, store, session, args.dry)
        if args.once:
            break
        time.sleep(cfg.get("interval_seconds", 600))


if __name__ == "__main__":
    main()
