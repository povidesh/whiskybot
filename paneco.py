import sqlite3
import os
import time
import sys
import subprocess
import logging
import logging.handlers
import re
import webbrowser
from pathlib import Path
from contextlib import contextmanager
from collections import defaultdict
from datetime import datetime
from html import escape as h
from urllib.parse import quote_plus

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager
from bs4 import BeautifulSoup

# --- ENCODING SETUP ---
os.system('chcp 65001 >nul')
sys.stdout.reconfigure(encoding='utf-8')

# --- PATHS (anchored to this file, not cwd) ---
SCRIPT_DIR = Path(__file__).resolve().parent
DB_PATH = SCRIPT_DIR / "whiskey_analytics.db"
LOG_PATH = SCRIPT_DIR / "paneco.log"
REPORT_PATH = SCRIPT_DIR / "paneco_report.html"
PUBLISH_PATH = SCRIPT_DIR / "docs" / "index.html"  # GitHub Pages מגיש מתיקיית docs/ ב-main

# --- CONFIGURATION ---
URL = "https://www.paneco.co.il/whiskey"
SITE_ROOT = "https://www.paneco.co.il"

# --- LOGGING ---
def setup_logging():
    logger = logging.getLogger("paneco")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=1_000_000, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger

log = setup_logging()

# --- DATABASE ---
NEW_COLS = {'id', 'product_key', 'product_name',
            'regular_price', 'special_price', 'effective_price', 'timestamp'}

@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    """יוצר את הסכמה החדשה; אם נמצאה סכמה ישנה - שומר אותה בשם price_history_legacy למיגרציה."""
    with db() as conn:
        c = conn.cursor()
        c.execute("PRAGMA table_info(price_history)")
        cols = {row[1] for row in c.fetchall()}
        if cols and cols != NEW_COLS:
            c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='price_history_legacy'")
            if not c.fetchone():
                c.execute("ALTER TABLE price_history RENAME TO price_history_legacy")
                log.info("Renamed old price_history -> price_history_legacy for migration")
        c.execute('''CREATE TABLE IF NOT EXISTS price_history
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      product_key TEXT NOT NULL,
                      product_name TEXT,
                      regular_price REAL,
                      special_price REAL,
                      effective_price REAL,
                      timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_product_key ON price_history (product_key)')
        c.execute('''CREATE TABLE IF NOT EXISTS product_meta
                     (product_key TEXT PRIMARY KEY,
                      english_name TEXT,
                      updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')

def get_known_english_names():
    """מפה product_key -> english_name עבור מוצרים שכבר שמרנו (cache קבוע)."""
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT product_key, english_name FROM product_meta WHERE english_name IS NOT NULL AND english_name != ''")
        return {row[0]: row[1] for row in c.fetchall()}

def save_english_name(product_key, english_name):
    """upsert של השם האנגלי שחולץ מעמוד המוצר."""
    with db() as conn:
        c = conn.cursor()
        c.execute("""INSERT INTO product_meta (product_key, english_name, updated_at)
                     VALUES (?, ?, CURRENT_TIMESTAMP)
                     ON CONFLICT(product_key) DO UPDATE SET
                       english_name=excluded.english_name, updated_at=CURRENT_TIMESTAMP""",
                  (product_key, english_name))

def backfill_legacy(product_key, product_name):
    """מעביר היסטוריה ישנה (לפי product_name) לטבלה החדשה תחת ה-product_key היציב. רץ פעם אחת לכל מוצר."""
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='price_history_legacy'")
        if not c.fetchone():
            return 0
        c.execute("SELECT 1 FROM price_history WHERE product_key=? LIMIT 1", (product_key,))
        if c.fetchone():
            return 0
        c.execute("""INSERT INTO price_history
                     (product_key, product_name, regular_price, special_price, effective_price, timestamp)
                     SELECT ?, product_name, price, price, price, timestamp
                     FROM price_history_legacy WHERE product_name = ?""",
                  (product_key, product_name))
        n = c.rowcount
        if n:
            log.info(f"Backfilled {n} legacy rows for {product_key} ({product_name!r})")
        return n

def save_price(product_key, product_name, regular_price, special_price):
    """שומר את מחיר היום (upsert לפי יום מקומי): מחליף רשומה קיימת של אותו יום.
    כך ריצות חוזרות באותו יום לא מוסיפות רשומות ולא מזיזות את הבסיס (היום הקודם).
    effective_price = הנמוך בין רגיל למבצע."""
    candidates = [p for p in (regular_price, special_price) if p is not None]
    effective = min(candidates) if candidates else None
    with db() as conn:
        c = conn.cursor()
        c.execute("""DELETE FROM price_history
                     WHERE product_key=?
                       AND date(timestamp,'localtime')=date('now','localtime')""",
                  (product_key,))
        c.execute("""INSERT INTO price_history
                     (product_key, product_name, regular_price, special_price, effective_price)
                     VALUES (?, ?, ?, ?, ?)""",
                  (product_key, product_name, regular_price, special_price, effective))

def get_product_stats(product_key):
    """מחזיר סטטיסטיקות על המוצר לפי effective_price.
    last_price = המחיר האחרון מ*יום קודם* (לא היום) - זהו הבסיס לזיהוי ירידות,
    כך שהוא יציב לאורך כל הריצות של אותו יום. min/max/avg/count על כל ההיסטוריה."""
    with db() as conn:
        c = conn.cursor()
        c.execute("""SELECT effective_price FROM price_history
                     WHERE product_key = ?
                       AND date(timestamp,'localtime') < date('now','localtime')
                     ORDER BY timestamp DESC LIMIT 1""", (product_key,))
        last = c.fetchone()
        last_price = last[0] if last else None
        c.execute("""SELECT MIN(effective_price), MAX(effective_price), AVG(effective_price), COUNT(*)
                     FROM price_history WHERE product_key = ?""", (product_key,))
        min_p, max_p, avg_p, count = c.fetchone()
    return {"last_price": last_price, "min": min_p, "max": max_p, "avg": avg_p, "count": count}

def get_recent_prices(product_key, limit=30):
    """מחירים אחרונים בסדר כרונולוגי עולה (לסגנון sparkline)."""
    with db() as conn:
        c = conn.cursor()
        c.execute("""SELECT effective_price FROM price_history
                     WHERE product_key=? AND effective_price IS NOT NULL
                     ORDER BY timestamp DESC LIMIT ?""", (product_key, limit))
        rows = c.fetchall()
    return list(reversed([r[0] for r in rows]))

# --- PARSING HELPERS ---
def _parse_price_text(text):
    if not text:
        return None
    clean = text.replace(',', '').replace('₪', '')
    m = re.search(r"(\d+\.?\d*)", clean)
    return float(m.group(1)) if m else None

def _extract_amount(el):
    """עדיפות ל-data-price-amount; נופלים לטקסט אם אין."""
    if el is None:
        return None
    amount = el.get('data-price-amount')
    if amount:
        try:
            return float(amount)
        except ValueError:
            pass
    wrapper = el.select_one('[data-price-amount]')
    if wrapper is not None:
        try:
            return float(wrapper.get('data-price-amount'))
        except (ValueError, TypeError):
            pass
    return _parse_price_text(el.get_text())

def _fallback_price_from_text(item):
    """ה-fallback הישן: סורקים את כל הטקסט ובוחרים מספר בטווח סביר."""
    all_text = item.get_text()
    nums = re.findall(r"(\d+\.?\d*)", all_text.replace(',', ''))
    valid = [float(n) for n in nums if 30 < float(n) < 10000]
    return max(valid) if valid else None

def _absolute_url(href):
    if not href:
        return ''
    if href.startswith(('http://', 'https://')):
        return href
    if href.startswith('//'):
        return 'https:' + href
    if href.startswith('/'):
        return SITE_ROOT + href
    return SITE_ROOT + '/' + href

def extract_product(item):
    """מחלץ (product_key, name, url, regular, special) או None."""
    title_tag = item.select_one('.product-item-link')
    if not title_tag:
        return None
    title = title_tag.get_text(strip=True)
    url = _absolute_url(title_tag.get('href') or '')

    price_box = item.select_one('.price-box[data-product-id]')
    product_id = price_box.get('data-product-id') if price_box else None
    if product_id:
        product_key = f"id:{product_id}"
    else:
        slug = (url.rstrip('/').rsplit('/', 1)[-1] or title) if url else title
        product_key = f"slug:{slug}"

    old_el = item.select_one('.old-price')
    special_el = item.select_one('.special-price')
    final_el = item.select_one('[data-price-type="finalPrice"]')

    regular = _extract_amount(old_el)
    special = _extract_amount(special_el)
    final = _extract_amount(final_el)

    if regular is not None and special is not None:
        pass
    elif special is not None:
        regular = special
    elif regular is not None:
        special = regular
    elif final is not None:
        regular = final
        special = final
    else:
        return None

    if (regular is None or regular < 30) and (special is None or special < 30):
        fallback = _fallback_price_from_text(item)
        if fallback is None:
            return None
        regular = special = fallback
    else:
        if regular is not None and regular < 30:
            regular = special
        if special is not None and special < 30:
            special = regular

    return product_key, title, url, regular, special

# --- SCRAPING ---
def scrape_current_prices():
    log.info("Starting web scrape")
    print("🤖 Starting web scrape...")

    options = webdriver.ChromeOptions()
    # options.add_argument("--headless")
    options.add_argument("--disable-gpu")
    options.add_argument("--log-level=3")

    driver = None
    products = {}

    try:
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        driver.get(URL)

        try:
            time.sleep(2)
            close_btn = driver.find_element(
                By.XPATH, "//button[contains(text(), 'אישור') or contains(text(), 'מעל')]"
            )
            close_btn.click()
        except Exception:
            pass

        last_height = driver.execute_script("return document.body.scrollHeight")
        while True:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(3)
            new_height = driver.execute_script("return document.body.scrollHeight")
            if new_height == last_height:
                break
            last_height = new_height

        soup = BeautifulSoup(driver.page_source, 'html.parser')

        items = soup.select('.product-item') or soup.select('li.item.product.product-item')
        log.info(f"Scanned {len(items)} items from website")
        print(f"Scanned {len(items)} items from website.")

        for item in items:
            try:
                extracted = extract_product(item)
                if extracted is None:
                    continue
                product_key, title, url, regular, special = extracted
                products[product_key] = {"name": title, "url": url, "regular": regular, "special": special}
            except Exception:
                log.warning("Failed to parse a product item", exc_info=True)
                continue

        # שם אנגלי מעמוד המוצר - מביאים רק למה שעוד לא במטמון (cache קבוע ב-product_meta)
        fetch_english_names(driver, products)
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                log.warning("driver.quit() failed", exc_info=True)

    return products

def fetch_english_names(driver, products):
    """ממלא products[key]['english_name'] מהשדה <strong class="product-english-title">.
    משתמש במטמון product_meta; פותח עמוד מוצר רק כשהשם חסר. כתיבה מיידית למטמון."""
    known = get_known_english_names()
    for key, info in products.items():
        if known.get(key):
            info["english_name"] = known[key]

    to_fetch = [(k, v) for k, v in products.items() if v.get("url") and not v.get("english_name")]
    if not to_fetch:
        return
    log.info(f"Fetching English names for {len(to_fetch)} new products")
    print(f"🔤 Fetching English names for {len(to_fetch)} new products...")
    for key, info in to_fetch:
        try:
            driver.get(info["url"])
            time.sleep(1.5)
            page = BeautifulSoup(driver.page_source, 'html.parser')
            el = page.select_one('strong.product-english-title')
            english = el.get_text(strip=True) if el else ''
            if english:
                info["english_name"] = english
                save_english_name(key, english)
        except Exception:
            log.warning(f"Failed to fetch English name for {key} ({info.get('url')!r})", exc_info=True)
            continue

# --- REPORT FORMATTING ---
CATEGORY_LABELS = {
    "low":   ("🔥", "שפל היסטורי",      "low"),
    "atlow": ("💎", "במחיר הנמוך בכל הזמנים", "atlow"),
    "good":  ("✅", "מתחת לממוצע",       "good"),
    "warn":  ("⚠️", "ירידה - אך עדיין גבוה", "warn"),
    "new":   ("🆕", "מוצרים חדשים",      "new"),
}
CATEGORY_ORDER = ["low", "atlow", "good", "warn", "new"]

def _fmt_money(p):
    if p is None:
        return "—"
    if abs(p - round(p)) < 0.01:
        return f"{int(round(p))}₪"
    return f"{p:.1f}₪"

def _wb_search_url(name, url, english_name=None):
    """קישור Google שמצמצם את החיפוש ל-whiskybase.com.
    עדיפות: שם אנגלי מעמוד המוצר -> סלאג אנגלי מה-URL -> שם עברי."""
    query_term = ''
    if english_name and english_name.strip():
        query_term = english_name.strip()
    if not query_term and url:
        slug = url.rstrip('/').rsplit('/', 1)[-1]
        if slug.endswith('.html'):
            slug = slug[:-5]
        slug = slug.replace('-', ' ').replace('_', ' ').strip()
        # רק סלאג אנגלי שימושי לחיפוש; סלאג עברי לא עדיף על שם המוצר
        if slug and re.search(r'[a-zA-Z]', slug):
            query_term = slug
    if not query_term:
        query_term = name
    full_query = f"site:whiskybase.com {query_term}"
    return f"https://www.google.com/search?q={quote_plus(full_query)}"

def sparkline_svg(prices, w=110, height=26, stroke=1.75):
    if not prices or len(prices) < 2:
        return ''
    pmin, pmax = min(prices), max(prices)
    pad = stroke + 1
    y_range = height - 2 * pad
    if pmin == pmax:
        y = height / 2
        return (f'<svg class="sparkline" viewBox="0 0 {w} {height}" aria-hidden="true">'
                f'<line x1="0" y1="{y:.1f}" x2="{w}" y2="{y:.1f}" '
                f'stroke="currentColor" stroke-width="{stroke}" opacity="0.5"/></svg>')
    n = len(prices)
    pts = []
    for i, p in enumerate(prices):
        x = (i / (n - 1)) * w
        y = pad + (1 - (p - pmin) / (pmax - pmin)) * y_range
        pts.append(f"{x:.1f},{y:.1f}")
    last_x = w
    last_y = pad + (1 - (prices[-1] - pmin) / (pmax - pmin)) * y_range
    return (f'<svg class="sparkline" viewBox="0 0 {w} {height}" aria-hidden="true">'
            f'<polyline points="{" ".join(pts)}" fill="none" stroke="currentColor" '
            f'stroke-width="{stroke}" stroke-linecap="round" stroke-linejoin="round" opacity="0.85"/>'
            f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="2.25" fill="currentColor"/></svg>')

REPORT_CSS = """
:root {
  --bg: #fafaf9; --fg: #1c1917; --muted: #78716c;
  --card: #fff; --border: #e7e5e4; --soft: #f5f5f4;
  --low: #dc2626; --good: #16a34a; --warn: #ca8a04; --new: #2563eb; --gem: #0891b2;
  --shadow: 0 1px 2px rgba(0,0,0,0.04), 0 1px 3px rgba(0,0,0,0.04);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0c0a09; --fg: #fafaf9; --muted: #a8a29e;
    --card: #1c1917; --border: #292524; --soft: #141210;
    --low: #f87171; --good: #4ade80; --warn: #fbbf24; --new: #60a5fa; --gem: #22d3ee;
    --shadow: 0 1px 2px rgba(0,0,0,0.35);
  }
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  background: var(--bg); color: var(--fg);
  font-family: "Segoe UI", "Heebo", "Assistant", "Rubik", system-ui, -apple-system, sans-serif;
  font-size: 16px; line-height: 1.5;
  padding: 2rem 1rem;
}
main { max-width: 880px; margin: 0 auto; }
header {
  display: flex; justify-content: space-between; align-items: baseline; gap: 1rem;
  margin-bottom: 2rem; padding-bottom: 1rem; border-bottom: 1px solid var(--border);
  flex-wrap: wrap;
}
h1 { margin: 0; font-size: 1.625rem; font-weight: 700; letter-spacing: -0.01em; }
.meta { color: var(--muted); font-size: 0.875rem; font-variant-numeric: tabular-nums; }
.summary { display: flex; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 2rem; }
.chip {
  display: inline-flex; align-items: center; gap: 0.375rem;
  padding: 0.25rem 0.625rem; border-radius: 999px;
  font-size: 0.8125rem; font-weight: 500;
  background: var(--soft); border: 1px solid var(--border);
}
.chip-low   { color: var(--low); }
.chip-atlow { color: var(--gem); }
.chip-good  { color: var(--good); }
.chip-warn  { color: var(--warn); }
.chip-new   { color: var(--new); }
section { margin-bottom: 2rem; }
section h2 {
  font-size: 1rem; font-weight: 600; margin: 0 0 0.875rem;
  display: flex; align-items: center; gap: 0.5rem;
}
section h2 .emoji { font-size: 1.125rem; }
section h2 .count {
  color: var(--muted); font-size: 0.8125rem; font-weight: 500;
  background: var(--soft); border: 1px solid var(--border);
  padding: 0.0625rem 0.5rem; border-radius: 999px;
  margin-inline-start: auto;
}
.grid { display: grid; grid-template-columns: 1fr; gap: 0.5rem; }
.card {
  background: var(--card); border: 1px solid var(--border); border-radius: 10px;
  padding: 0.875rem 1rem; display: grid;
  grid-template-columns: 1fr auto; gap: 1rem; align-items: center;
  box-shadow: var(--shadow);
  border-inline-end-width: 4px;
}
.card-low   { border-inline-end-color: var(--low); }
.card-atlow { border-inline-end-color: var(--gem); }
.card-good  { border-inline-end-color: var(--good); }
.card-warn  { border-inline-end-color: var(--warn); }
.card-new   { border-inline-end-color: var(--new); }
.card-main { min-width: 0; }
.name { font-weight: 500; font-size: 1rem; word-break: break-word; }
.name a { color: var(--fg); text-decoration: none; }
.name a:hover { text-decoration: underline; }
.name-en { color: var(--muted); font-size: 0.8125rem; margin-top: 0.125rem; direction: ltr; text-align: start; word-break: break-word; }
.stats { color: var(--muted); font-size: 0.8125rem; margin-top: 0.25rem; }
.price-chg { color: var(--good); font-weight: 500; white-space: nowrap; }
.card-side { text-align: end; }
.price-now {
  font-size: 1.375rem; font-weight: 600;
  font-variant-numeric: tabular-nums; line-height: 1.2;
}
.card-low   .price-now { color: var(--low); }
.card-atlow .price-now { color: var(--gem); }
.card-good  .price-now { color: var(--good); }
.card-warn  .price-now { color: var(--warn); }
.card-new   .price-now { color: var(--new); }
.sparkline { width: 100px; height: 24px; display: block; margin: 0.375rem 0 0 auto; opacity: 0.7; }
.card-low   .sparkline { color: var(--low); }
.card-atlow .sparkline { color: var(--gem); }
.card-good  .sparkline { color: var(--good); }
.card-warn  .sparkline { color: var(--warn); }
.card-new   .sparkline { color: var(--new); }
.wb-link {
  display: inline-block; margin-top: 0.5rem;
  font-size: 0.75rem; color: var(--muted); text-decoration: none;
  padding: 0.1875rem 0.625rem; border: 1px solid var(--border); border-radius: 999px;
  background: var(--soft); white-space: nowrap; line-height: 1.3;
  transition: color 0.15s, background 0.15s;
}
.wb-link:hover { color: var(--fg); background: var(--card); border-color: var(--muted); }
.all-row .wb-link {
  margin: 0; padding: 0.0625rem 0.375rem; font-size: 0.6875rem;
}
.empty { text-align: center; padding: 3rem 1rem; color: var(--muted); }
.empty p:first-child { font-size: 1.5rem; margin: 0 0 0.5rem; }
bdi { unicode-bidi: plaintext; }
details { margin-top: 3rem; }
details summary {
  cursor: pointer; padding: 0.75rem 0;
  font-weight: 600; color: var(--muted); border-top: 1px solid var(--border);
  user-select: none;
}
details[open] summary { color: var(--fg); }
.all-controls {
  display: flex; gap: 0.5rem; flex-wrap: wrap; align-items: center;
  margin-top: 1rem;
}
.all-controls input, .all-controls select {
  font: inherit; font-size: 0.9375rem; color: var(--fg); background: var(--card);
  border: 1px solid var(--border); border-radius: 8px; padding: 0.45rem 0.6rem;
}
.all-controls input {
  flex: 1 1 12rem; min-width: 0;
}
.all-controls input:focus, .all-controls select:focus {
  outline: none; border-color: var(--muted);
}
.all-empty { color: var(--muted); font-size: 0.9375rem; text-align: center; margin: 1.25rem 0; }
.all-list { list-style: none; padding: 0; margin: 1rem 0 0; }
.all-row {
  display: grid; grid-template-columns: 1fr auto auto auto; align-items: center;
  padding: 0.5rem 0; border-bottom: 1px solid var(--border);
  gap: 0.75rem;
}
.all-row:last-child { border-bottom: none; }
.all-name { font-size: 0.9375rem; min-width: 0; }
.all-name .nm { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.all-name a { color: var(--fg); text-decoration: none; }
.all-name a:hover { text-decoration: underline; }
.all-stats { color: var(--muted); font-size: 0.75rem; margin-top: 0.15rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.all-list .sparkline { width: 80px; height: 20px; margin: 0; color: var(--muted); }
.all-price { font-variant-numeric: tabular-nums; font-size: 0.9375rem; min-width: 4em; text-align: end; }
footer {
  color: var(--muted); font-size: 0.8125rem; text-align: center;
  margin-top: 2rem; padding-top: 1rem; border-top: 1px solid var(--border);
}
"""

# סינון לפי שם + מיון מחיר עבור "כל המוצרים במעקב" (vanilla, רץ בדפדפן)
ALL_LIST_JS = r"""
(function () {
  var inp = document.getElementById('all-filter');
  var sel = document.getElementById('all-sort');
  var list = document.getElementById('all-list');
  var count = document.getElementById('all-count');
  var empty = document.getElementById('all-empty');
  if (!list) return;
  var rows = Array.prototype.slice.call(list.querySelectorAll('.all-row'));
  function price(r) { var v = parseFloat(r.getAttribute('data-price')); return isNaN(v) ? Infinity : v; }
  function apply() {
    var q = (inp.value || '').trim().toLowerCase();
    var mode = sel.value;
    var ordered = rows.slice();
    if (mode === 'price-asc') ordered.sort(function (a, b) { return price(a) - price(b); });
    else if (mode === 'price-desc') ordered.sort(function (a, b) { return price(b) - price(a); });
    else ordered.sort(function (a, b) {
      return (a.getAttribute('data-name') || '').localeCompare(b.getAttribute('data-name') || '', 'he');
    });
    var visible = 0;
    ordered.forEach(function (r) {
      var show = !q || (r.getAttribute('data-name') || '').indexOf(q) >= 0;
      r.style.display = show ? '' : 'none';
      if (show) visible++;
      list.appendChild(r);
    });
    if (count) count.textContent = visible;
    if (empty) empty.hidden = visible !== 0;
  }
  inp.addEventListener('input', apply);
  sel.addEventListener('change', apply);
})();
"""

def _stats_line(mn, mx, count):
    """שורת סטטיסטיקות: מינ׳ · מקס׳ · מדידות. משותפת לכרטיסים ולרשימת הכל."""
    bits = []
    if mn is not None:
        bits.append(f'מינ׳ <bdi>{_fmt_money(mn)}</bdi>')
    if mx is not None:
        bits.append(f'מקס׳ <bdi>{_fmt_money(mx)}</bdi>')
    if count:
        bits.append(f'<bdi>{count}</bdi> מדידות')
    return " · ".join(bits)

def _card_html(it):
    emoji, _, cls = CATEGORY_LABELS[it["category"]]
    name_h = h(it["name"])
    if it.get("url"):
        name_node = f'<a href="{h(it["url"])}" target="_blank" rel="noopener">{name_h}</a>'
    else:
        name_node = name_h
    spark = sparkline_svg(it.get("history", []))
    en = it.get("english_name") or ""
    name_en_node = f'<div class="name-en">{h(en)}</div>' if en else ''
    wb_href = h(_wb_search_url(it["name"], it.get("url") or '', en))
    wb_node = f'<a class="wb-link" href="{wb_href}" target="_blank" rel="noopener">🔍 whiskybase</a>'
    parts = []
    if it["category"] in ("low", "good", "warn") and it.get("last") is not None:
        parts.append(f'היה <bdi>{_fmt_money(it["last"])}</bdi>')
        cur = it.get("current")
        if cur is not None and it["last"]:
            diff = it["last"] - cur
            if diff > 0:
                pct = it.get("drop_pct", 0) * 100
                parts.append(
                    f'<span class="price-chg">▼ <bdi>{_fmt_money(diff)}</bdi> '
                    f'(<bdi>{pct:.0f}%</bdi>)</span>'
                )
    base_stats = _stats_line(it.get("min"), it.get("max"), it.get("count"))
    if base_stats:
        parts.append(base_stats)
    stats_node = " · ".join(parts)
    return (
        f'<article class="card card-{cls}">'
        f'<div class="card-main">'
        f'<div class="name">{name_node}</div>'
        f'{name_en_node}'
        f'<div class="stats">{stats_node}</div>'
        f'</div>'
        f'<div class="card-side">'
        f'<div class="price-now"><bdi>{_fmt_money(it["current"])}</bdi></div>'
        f'{spark}'
        f'{wb_node}'
        f'</div>'
        f'</article>'
    )

def _all_row_html(p):
    name_h = h(p["name"])
    url = p.get("url") or ""
    en = p.get("english_name") or ""
    title_attr = f' title="{h(en)}"' if en else ''
    name_node = f'<a href="{h(url)}" target="_blank" rel="noopener"{title_attr}>{name_h}</a>' if url else name_h
    spark = sparkline_svg(p.get("history", []))
    wb_href = h(_wb_search_url(p["name"], url, en))
    wb_node = f'<a class="wb-link" href="{wb_href}" target="_blank" rel="noopener" title="חפש ב-whiskybase">🔍</a>'
    search_blob = h(f'{p["name"]} {en}'.strip().lower())  # לסינון לפי שם (עברי+אנגלי)
    price_attr = f'{p["current"]:.2f}' if p.get("current") is not None else ''
    stats_node = _stats_line(p.get("min"), p.get("max"), p.get("count"))
    stats_html = f'<div class="all-stats">{stats_node}</div>' if stats_node else ''
    return (
        f'<li class="all-row" data-name="{search_blob}" data-price="{price_attr}">'
        f'<div class="all-name"><span class="nm">{name_node}</span>{stats_html}</div>'
        f'{wb_node}'
        f'{spark}'
        f'<bdi class="all-price">{_fmt_money(p["current"])}</bdi>'
        f'</li>'
    )

def generate_html_report(items, all_products, scanned_count, started_at, finished_at):
    """דו"ח HTML עצמאי. items=שינויי מחיר, all_products=הכל מהסריקה הנוכחית."""
    by_cat = defaultdict(list)
    for it in items:
        by_cat[it["category"]].append(it)
    for cat, lst in by_cat.items():
        if cat == "new":
            lst.sort(key=lambda x: x["name"])
        elif cat == "atlow":
            lst.sort(key=lambda x: x.get("below_max_pct", 0), reverse=True)
        else:
            lst.sort(key=lambda x: x.get("drop_pct", 0), reverse=True)

    summary_chips = []
    for cat in CATEGORY_ORDER:
        n = len(by_cat.get(cat, []))
        if n:
            emoji, label, cls = CATEGORY_LABELS[cat]
            summary_chips.append(
                f'<span class="chip chip-{cls}">{emoji} <bdi>{n}</bdi> {h(label)}</span>'
            )

    sections = []
    for cat in CATEGORY_ORDER:
        if cat not in by_cat:
            continue
        emoji, label, cls = CATEGORY_LABELS[cat]
        cards = "".join(_card_html(it) for it in by_cat[cat])
        sections.append(
            f'<section class="section-{cls}">'
            f'<h2><span class="emoji">{emoji}</span> {h(label)}'
            f'<span class="count"><bdi>{len(by_cat[cat])}</bdi></span></h2>'
            f'<div class="grid">{cards}</div>'
            f'</section>'
        )
    if not sections:
        sections.append(
            '<section class="empty">'
            '<p>😴 אין שינויים מעניינים</p>'
            f'<p class="meta">נסרקו <bdi>{scanned_count}</bdi> מוצרים, אף אחד לא ירד.</p>'
            '</section>'
        )

    all_sorted = sorted(all_products, key=lambda x: x["name"])
    all_rows = "".join(_all_row_html(p) for p in all_sorted)
    duration_s = max(0, (finished_at - started_at).total_seconds())

    title = "דו\"ח הזדמנויות — Paneco"
    return (
        '<!DOCTYPE html>\n'
        '<html lang="he" dir="rtl">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<title>{h(title)}</title>\n'
        f'<style>{REPORT_CSS}</style>\n'
        '</head>\n<body>\n<main>\n'
        '<header>\n'
        f'<h1>{h(title.split(" — ")[0])}</h1>\n'
        f'<div class="meta">Paneco · <bdi>{finished_at.strftime("%d/%m/%Y %H:%M")}</bdi></div>\n'
        '</header>\n'
        f'<div class="summary">{"".join(summary_chips) if summary_chips else ""}</div>\n'
        f'{"".join(sections)}\n'
        '<details>\n'
        f'<summary>כל המוצרים במעקב '
        f'<bdi dir="ltr">(<span id="all-count">{len(all_sorted)}</span> / {len(all_sorted)})</bdi>'
        '</summary>\n'
        '<div class="all-controls">\n'
        '<input type="search" id="all-filter" placeholder="חיפוש לפי שם…" aria-label="חיפוש לפי שם" autocomplete="off">\n'
        '<select id="all-sort" aria-label="מיון">\n'
        '<option value="name">מיון: שם (א׳–ת׳)</option>\n'
        '<option value="price-asc">מחיר: מהנמוך לגבוה</option>\n'
        '<option value="price-desc">מחיר: מהגבוה לנמוך</option>\n'
        '</select>\n'
        '</div>\n'
        f'<ul class="all-list" id="all-list">{all_rows}</ul>\n'
        '<p class="all-empty" id="all-empty" hidden>אין מוצרים תואמים</p>\n'
        '</details>\n'
        '<footer>'
        f'נסרקו <bdi>{scanned_count}</bdi> מוצרים · עודכן ב-<bdi>{duration_s:.0f}s</bdi>'
        '</footer>\n'
        '</main>\n'
        f'<script>{ALL_LIST_JS}</script>\n'
        '</body>\n</html>\n'
    )

# --- PUBLISH ---
def _git(*args):
    """מריץ git בתיקיית הפרויקט; מחזיר CompletedProcess. לא מעלה חריגה על קוד שגיאה."""
    return subprocess.run(
        ["git", *args], cwd=str(SCRIPT_DIR),
        capture_output=True, text=True, timeout=120,
    )

def publish_report():
    """מפרסם את docs/index.html ל-remote (GitHub Pages מתעדכן אוטומטית על push).
    Best-effort: אם אין remote מוגדר או ש-push נכשל - רושם אזהרה וממשיך."""
    if not PUBLISH_PATH.exists():
        return
    try:
        if not _git("rev-parse", "--is-inside-work-tree").returncode == 0:
            return  # לא repo - דלג בשקט
        if not _git("remote").stdout.strip():
            log.info("No git remote configured; skipping publish")
            return
        _git("add", "--", str(PUBLISH_PATH))
        if _git("diff", "--cached", "--quiet").returncode == 0:
            return  # אין שינוי לפרסם
        _git("commit", "-m", f"report: {datetime.now():%Y-%m-%d %H:%M}")
        push = _git("push")
        if push.returncode != 0:
            log.warning("git push failed: %s", (push.stderr or push.stdout).strip())
            print("⚠️  Publish (git push) failed - see paneco.log")
        else:
            log.info("Published report to git remote")
            print("🌐 Report published")
    except Exception:
        log.warning("publish_report failed", exc_info=True)

# --- MAIN LOGIC ---
def run_bot_job(auto_open=False):
    started = datetime.now()
    log.info("--- Running analytics job ---")
    print(f"\n⏰ {started.strftime('%H:%M')} - Running analytics job...")

    init_db()
    products = scrape_current_prices()

    items = []
    all_products = []
    counts = defaultdict(int)

    for key, info in products.items():
        title = info["name"]
        url = info.get("url", "")
        english_name = info.get("english_name", "")
        regular = info["regular"]
        special = info["special"]
        candidates = [p for p in (regular, special) if p is not None]
        if not candidates:
            continue
        effective = min(candidates)

        backfill_legacy(key, title)                # חייב לרוץ לפני save_price (בודק אם אין רשומות)
        save_price(key, title, regular, special)    # upsert של מחיר היום (מחליף רשומת אותו יום)
        stats = get_product_stats(key)              # הבסיס = המחיר מהיום הקודם, יציב לאורך היום
        history = get_recent_prices(key, limit=30)  # כולל כבר את היום שנשמר

        all_products.append({"name": title, "english_name": english_name, "url": url,
                             "current": effective, "history": history,
                             "min": stats["min"], "max": stats["max"], "count": stats["count"]})

        category = None
        if stats["last_price"] is None:
            category = "new"
        elif effective < stats["last_price"]:
            if effective <= (stats["min"] or float('inf')):
                category = "low"
            elif stats["avg"] is not None and effective < stats["avg"]:
                category = "good"
            else:
                category = "warn"

        if category:
            counts[category] += 1
            drop_pct = 0.0
            if stats["last_price"]:
                drop_pct = (stats["last_price"] - effective) / stats["last_price"]
            items.append({
                "category": category,
                "key": key, "name": title, "english_name": english_name, "url": url,
                "current": effective,
                "last": stats["last_price"],
                "min": stats["min"], "max": stats["max"], "avg": stats["avg"],
                "count": stats["count"],
                "history": history,
                "drop_pct": drop_pct,
            })

        # 💎 במחיר הנמוך בכל הזמנים: מחיר נוכחי = השפל ההיסטורי, ובתנאי שהיתה תנודה (min!=max).
        # חופף בכוונה עם 🔥 - בקבוק שירד היום לשפל יופיע בשני המקומות.
        if (stats["min"] is not None and stats["max"] is not None
                and effective <= stats["min"] and stats["min"] != stats["max"]):
            counts["atlow"] += 1
            below_max = (stats["max"] - effective) / stats["max"] if stats["max"] else 0.0
            items.append({
                "category": "atlow",
                "key": key, "name": title, "english_name": english_name, "url": url,
                "current": effective,
                "last": stats["last_price"],
                "min": stats["min"], "max": stats["max"], "avg": stats["avg"],
                "count": stats["count"],
                "history": history,
                "below_max_pct": below_max,
            })

    finished = datetime.now()
    html_str = generate_html_report(items, all_products, len(products), started, finished)
    REPORT_PATH.write_text(html_str, encoding="utf-8")
    PUBLISH_PATH.parent.mkdir(exist_ok=True)
    PUBLISH_PATH.write_text(html_str, encoding="utf-8")
    publish_report()

    summary_bits = []
    if counts["low"]:  summary_bits.append(f"🔥 {counts['low']} historic low")
    if counts["good"]: summary_bits.append(f"✅ {counts['good']} below avg")
    if counts["warn"]: summary_bits.append(f"⚠️  {counts['warn']} above min")
    if counts["new"]:  summary_bits.append(f"🆕 {counts['new']} new")
    summary = " · ".join(summary_bits) if summary_bits else "no opportunities"

    print(f"✅ {len(products)} products scanned · {summary}")
    print(f"   report → {REPORT_PATH.name}")
    log.info(f"Job complete: {len(products)} products, {summary}")

    if auto_open:
        try:
            webbrowser.open(REPORT_PATH.as_uri())
        except Exception:
            log.warning("Could not auto-open browser", exc_info=True)

def safe_run_job(auto_open=False):
    """עוטף את run_bot_job כך שכישלון נרשם ללוג במקום להפיל את התהליך."""
    try:
        run_bot_job(auto_open=auto_open)
    except Exception:
        log.exception("run_bot_job crashed")
        print("⚠️  Job failed - see paneco.log for traceback")

# --- ENTRY POINT ---
if __name__ == "__main__":
    safe_run_job(auto_open=True)
