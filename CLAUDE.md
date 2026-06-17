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

Behavior on launch: runs a single scrape via `safe_run_job(auto_open=True)` and exits. It's a one-shot script — schedule externally (Windows Task Scheduler / cron) for recurring runs.

## Architecture notes

- **`run_bot_job(auto_open)`** orchestrates: `init_db()` → `scrape_current_prices()` → per product: `backfill_legacy()` → `save_price()` (upsert today) → `get_product_stats()` → classify (new / historic-low / below-avg / "still above min") → `generate_html_report()` → write `paneco_report.html`. `auto_open=True` opens the report in the default browser after writing it.
- **`safe_run_job()`** wraps `run_bot_job()` so a scrape failure (Chrome crash, DOM change, network blip) logs a traceback and exits cleanly instead of surfacing an unhandled exception.
- **DB schema** (created/migrated by `init_db()`):
  `price_history(id, product_key, product_name, regular_price, special_price, effective_price, timestamp)` with index `idx_product_key`.
  - `product_key` is the **stable identifier**: `id:<data-product-id>` from the price box, falling back to `slug:<URL slug>` if the SKU attribute is missing. Always use this — never the display name — to look up history.
  - `regular_price` / `special_price` track the two prices Magento renders (`.old-price` vs `.special-price`). When the product isn't on sale, both columns equal the single `finalPrice`.
  - `effective_price = min(regular, special)` and drives all stats / drop detection.
  - **One row per product per local day.** `save_price()` upserts: it deletes any existing row for the product on the current local day (`date(timestamp,'localtime')`) before inserting. So re-running on the same day overwrites today's price instead of appending — the previous day's price (the deal baseline) is never disturbed, and same-day reruns don't bloat the table or flush the deals.
  - **Deal baseline = most recent price from an _earlier_ day.** `get_product_stats()` reads `last_price` with `date(timestamp,'localtime') < date('now','localtime')` (not the latest row, which would be today's own price). This is what keeps deals stable across same-day reruns. `min`/`max`/`avg`/`count` are over the full history. In `run_bot_job` the per-product order is `backfill_legacy()` → `save_price()` (upsert today) → `get_product_stats()` (baseline from prior days) → classify; `backfill_legacy` must precede `save_price` since it only copies when the product has no rows yet.
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
- **Publishing**: each run writes the report to two places — `paneco_report.html` (local, git-ignored, used for `auto_open`) and `docs/index.html` (the published copy). `publish_report()` then `git add docs/index.html` → commit → push, best-effort: it silently skips when there's no git remote, and logs a warning (never raises) if the push fails, so publishing can't kill a scrape. The repo is served by **GitHub Pages** from `main` branch `/docs` (`docs/.nojekyll` disables Jekyll). Pushing from the scheduled job relies on cached git credentials, so the first `git push` must be done manually once.
- **Paths are anchored** to `Path(__file__).resolve().parent` (`DB_PATH`, `LOG_PATH`). Launching from any cwd keeps the same DB/log.
- The DB file is the source of truth for all history — deleting it resets every product to "🆕 מוצר חדש במעקב".
