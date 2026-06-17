# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Single-file Python bot (`paneco.py`) that scrapes whiskey prices from `https://www.paneco.co.il/whiskey`, stores every observed price in a local SQLite database (`whiskey_analytics.db`), and renders a Hebrew-language "opportunity report" as an HTML file (`paneco_report.html`) comparing the current effective price against each product's recorded history.

Logs go to `paneco.log` (rotating, 1 MB × 5). No test suite, no CI.

## Run

```powershell
pip install -r requirements.txt
python paneco.py
```

Behavior on launch:
1. Runs one scrape immediately via `safe_run_job()`.
2. Enters `schedule` loop, re-running daily at 10:00, polling every 60s.

There is no CLI flag for a single-shot run — Ctrl+C after the initial job to abort the schedule.

## Architecture notes

- **`run_bot_job(auto_open)`** orchestrates: `init_db()` → `scrape_current_prices()` → per product: `backfill_legacy()` → `get_product_stats()` → classify (new / historic-low / below-avg / "still above min") → `save_price()` → `generate_html_report()` → write `paneco_report.html`. `auto_open=True` only on the immediate startup run (opens the report in the default browser); scheduled runs just overwrite the file. The HTML has `<meta http-equiv="refresh" content="3600">` so an open tab self-refreshes hourly to pick up new data.
- **`safe_run_job()`** wraps `run_bot_job()` so a scrape failure (Chrome crash, DOM change, network blip) logs a traceback and returns instead of killing the scheduler loop. The `while True` loop also catches `schedule.run_pending()` failures.
- **DB schema** (created/migrated by `init_db()`):
  `price_history(id, product_key, product_name, regular_price, special_price, effective_price, timestamp)` with index `idx_product_key`.
  - `product_key` is the **stable identifier**: `id:<data-product-id>` from the price box, falling back to `slug:<URL slug>` if the SKU attribute is missing. Always use this — never the display name — to look up history.
  - `regular_price` / `special_price` track the two prices Magento renders (`.old-price` vs `.special-price`). When the product isn't on sale, both columns equal the single `finalPrice`.
  - `effective_price = min(regular, special)` and drives all stats / drop detection.
  `product_meta(product_key, english_name, updated_at)` is a permanent cache of the English product name scraped from each product page (`<strong class="product-english-title">`, located in `div.product-info-main`). Keyed by the same stable `product_key`.
- **English name fetch**: `fetch_english_names(driver, products)` runs at the end of `scrape_current_prices` (reusing the live Selenium driver, before it quits). It loads cached names from `product_meta` and only visits a product page (`driver.get(url)`) for products whose English name isn't cached yet — write-through to `product_meta` as each is found. So the **first** run fetches ~every product page once (slow, ~minutes); later runs only fetch genuinely new products. Per-product failures are caught and skipped. The English name feeds the whiskybase search link and a subtitle (`.name-en`) under the Hebrew name on each report card (and a hover `title` in the compact all-products list).
- **Legacy migration**: the old schema (`id, product_name, price, timestamp`) is auto-renamed to `price_history_legacy` on first run of the new code. `backfill_legacy(product_key, name)` is then called per-product per-scrape; it's a one-shot copy keyed by the legacy `product_name` (matches the title at the time the legacy rows were written). Products whose titles have since drifted stay orphaned in the legacy table — acceptable, since the stable `product_key` prevents new drift.
- **Headless mode is intentionally off** (commented out near the top of `scrape_current_prices`). The site's age-gate / scroll behavior is unreliable headless. Don't re-enable `--headless` without retesting the age popup XPath and the infinite-scroll height loop.
- **Price extraction** in `extract_product()`:
  1. Prefer the `data-price-amount` attribute (clean float), fall back to the rendered text.
  2. Cascade: `.old-price` + `.special-price` → `[data-price-type="finalPrice"]`.
  3. If extracted numbers are below 30 ₪, run `_fallback_price_from_text()` (scan all numbers in the item, pick the max in `30..10000`). This guards against picking up bottle size ("70cl", "5L") as price.
  When changing selectors, preserve the `< 30 ₪` guard.
- **Hebrew / RTL handling**: DB stores logical-order text. The HTML report (`paneco_report.html`) sets `<html dir="rtl">` and uses `<bdi>` around numbers so prices stay LTR within the RTL flow — no character reversal anywhere. The console output is now English-only (one summary line), so `bidi.algorithm.get_display` is no longer needed and was removed. The rotating logger writes logical order — never inject anything reversed into `log.info`, the DB, selectors, or the HTML.
- **Report layout**: `generate_html_report()` returns a single self-contained HTML string with embedded CSS (`REPORT_CSS`). One section per category (`low` / `good` / `warn` / `new`), color-coded card border, sparkline SVG of last 30 effective prices via `sparkline_svg()`, and a "🔍 whiskybase" chip per card whose href is a Google `site:whiskybase.com` search. `_wb_search_url(name, url, english_name)` builds the search term with this priority: (1) the **English product name** scraped from the product page, (2) the English URL slug — last path segment, trailing `.html` stripped, used only if it contains Latin letters, (3) the Hebrew product name. The English name is the primary source and works even for Hebrew-slug products (`…/גוני-ווקר-דאבל-בלאק-ליטר` → `Johnnie Walker Double Black 1LT`). Dark mode follows `prefers-color-scheme`.
- **Encoding setup** (`chcp 65001`, `sys.stdout.reconfigure(encoding='utf-8')`) at the top of the file is Windows-specific and required for Hebrew console output.
- **Paths are anchored** to `Path(__file__).resolve().parent` (`DB_PATH`, `LOG_PATH`). Launching from any cwd keeps the same DB/log.
- The DB file is the source of truth for all history — deleting it resets every product to "🆕 מוצר חדש במעקב".
