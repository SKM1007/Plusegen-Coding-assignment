import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime, date
from typing import List, Dict, Optional

from dateutil.parser import parse as parse_date
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# ----------------------------
# Utilities
# ----------------------------
def ensure_output_dir():
    if os.path.exists("output") and not os.path.isdir("output"):
        print("ERROR: 'output' exists but is not a directory. Delete the file named 'output' and create a folder.")
        sys.exit(1)
    os.makedirs("output", exist_ok=True)


def parse_any_date(text: str) -> Optional[date]:
    if not text:
        return None
    cleaned = re.sub(r"Reviewed on\s+", "", text.strip(), flags=re.IGNORECASE).strip()
    try:
        return parse_date(cleaned, fuzzy=True).date()
    except Exception:
        return None


def in_range(d: Optional[date], start_d: date, end_d: date) -> bool:
    return d is not None and start_d <= d <= end_d


def human_sleep(a=0.6, b=1.4):
    time.sleep(random.uniform(a, b))


def accept_cookies_if_present(page):
    # best effort cookie buttons
    candidates = [
        "button:has-text('Accept all')",
        "button:has-text('Accept All')",
        "button:has-text('Accept')",
        "button:has-text('I Agree')",
        "button:has-text('Agree')",
        "button:has-text('OK')",
        "button:has-text('Got it')",
        "button:has-text('Allow all')",
    ]
    for sel in candidates:
        try:
            loc = page.locator(sel)
            if loc.count() > 0:
                loc.first.click(timeout=1500)
                human_sleep(0.4, 0.9)
                break
        except Exception:
            pass


def maybe_save_debug_html(page, path: str, enabled: bool):
    if not enabled:
        return
    try:
        html = page.content()
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"[debug] Saved HTML to {path}")
    except Exception as e:
        print(f"[debug] Failed to save HTML: {e}")


def detect_blocked(html: str) -> bool:
    """
    Heuristic: detect common block/interstitial pages.
    """
    needles = [
        "captcha",
        "Access Denied",
        "unusual traffic",
        "verify you are human",
        "robot",
        "blocked",
        "Cloudflare",
        "Please enable cookies",
    ]
    low = html.lower()
    return any(n.lower() in low for n in needles)


def setup_stealth(context):
    """
    Lightweight stealth: remove webdriver, add chrome object, fix permissions query.
    This is not perfect, but often enough for basic bot checks.
    """
    context.add_init_script(
        """
        // webdriver flag
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        // chrome object
        window.chrome = { runtime: {} };
        // languages
        Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
        // plugins
        Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
        // permissions
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) => (
          parameters.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : originalQuery(parameters)
        );
        """
    )


def gentle_scroll(page, steps=6):
    # scroll to trigger lazy load/hydration
    for i in range(steps):
        try:
            page.mouse.wheel(0, 900)
        except Exception:
            pass
        human_sleep(0.4, 0.9)


# ----------------------------
# G2 Scraper (more robust posture)
# ----------------------------
def scrape_g2(company_slug: str, start_d: date, end_d: date, max_pages: int, debug_html: bool,
             headless: bool, slow_mo: int) -> List[Dict]:
    collected: List[Dict] = []
    base = f"https://www.g2.com/products/{company_slug}/reviews"

    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, slow_mo=slow_mo)
        context = browser.new_context(
            user_agent=user_agent,
            locale="en-US",
            timezone_id="Asia/Kolkata",
            viewport={"width": 1366, "height": 768},
        )
        setup_stealth(context)
        page = context.new_page()

        stop_due_to_old = False

        for page_no in range(1, max_pages + 1):
            url = base if page_no == 1 else f"{base}?page={page_no}"
            print(f"[G2] Fetching: {url}")

            try:
                page.goto(url, timeout=60000, wait_until="domcontentloaded")
            except PlaywrightTimeoutError:
                print("[G2] Timeout. Retrying once with wait_until=load...")
                page.goto(url, timeout=60000, wait_until="load")

            human_sleep(1.2, 2.2)
            accept_cookies_if_present(page)
            gentle_scroll(page, steps=7)

            # try waiting for any likely review container
            # (if none appear, likely blocked or DOM changed)
            candidate_waits = [
                "div.paper",
                "div[data-testid*='review']",
                "article",
                "[itemprop='review']",
                "div[class*='review']",
            ]
            found_any = False
            for sel in candidate_waits:
                try:
                    if page.locator(sel).count() > 0:
                        found_any = True
                        break
                except Exception:
                    pass

            html = page.content()
            if debug_html:
                maybe_save_debug_html(page, f"output/debug_g2_page_{page_no}.html", True)

            if detect_blocked(html):
                print("[G2] Looks like a bot/interstitial page was served. Try running headful: --headless 0")
                break

            if not found_any:
                print(f"[G2] No review-like containers found on page {page_no}.")
                print("[G2] This usually means G2 changed DOM or blocked rendering in your environment.")
                break

            # Collect cards with broad net
            cards = page.locator("div.paper, article, [itemprop='review'], div[data-testid*='review'], div[class*='review']")
            card_count = min(cards.count(), 60)

            for i in range(card_count):
                card = cards.nth(i)

                # body
                body = None
                body_selectors = [
                    "div[itemprop='reviewBody']",
                    "[data-testid*='review-text']",
                    "div[class*='reviewBody']",
                    "div[class*='review-body']",
                    "p",
                ]
                for sel in body_selectors:
                    try:
                        t = card.locator(sel).first.inner_text(timeout=800).strip()
                        if t and len(t) > 20:
                            body = t
                            break
                    except Exception:
                        pass

                # date
                d = None
                date_text = None
                for sel in ["time", "span:has-text('Reviewed on')", "div:has-text('Reviewed on')"]:
                    try:
                        t = card.locator(sel).first.inner_text(timeout=800).strip()
                        if t:
                            date_text = t
                            break
                    except Exception:
                        pass
                d = parse_any_date(date_text or "")

                # stop condition for older dates
                if d and d < start_d:
                    stop_due_to_old = True
                    continue

                # title
                title = ""
                try:
                    title = card.locator("h3").first.inner_text(timeout=500).strip()
                except Exception:
                    title = ""

                # rating
                rating = None
                for sel in ["span[itemprop='ratingValue']", "meta[itemprop='ratingValue']"]:
                    try:
                        loc = card.locator(sel).first
                        if loc.count() == 0:
                            continue
                        if "meta" in sel:
                            rating = loc.get_attribute("content")
                        else:
                            rating = loc.inner_text(timeout=500).strip()
                        if rating:
                            break
                    except Exception:
                        pass

                if body and in_range(d, start_d, end_d):
                    collected.append({
                        "source": "G2",
                        "title": title,
                        "review": body,
                        "date": d.isoformat(),
                        "rating": rating
                    })

            if stop_due_to_old:
                print("[G2] Reached reviews older than start_date. Stopping pagination.")
                break

            human_sleep(0.8, 1.6)

        context.close()
        browser.close()

    return collected


# ----------------------------
# TrustRadius (bonus) - kept
# ----------------------------
def scrape_trustradius(product_slug: str, start_d: date, end_d: date, max_pages: int, debug_html: bool,
                       headless: bool, slow_mo: int) -> List[Dict]:
    collected: List[Dict] = []
    base = f"https://www.trustradius.com/products/{product_slug}/reviews"

    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, slow_mo=slow_mo)
        context = browser.new_context(
            user_agent=user_agent,
            locale="en-US",
            timezone_id="Asia/Kolkata",
            viewport={"width": 1366, "height": 768},
        )
        setup_stealth(context)
        page = context.new_page()

        for page_no in range(1, max_pages + 1):
            url = base if page_no == 1 else f"{base}?page={page_no}"
            print(f"[TrustRadius] Fetching: {url}")
            page.goto(url, timeout=60000, wait_until="domcontentloaded")
            human_sleep(1.2, 2.2)
            accept_cookies_if_present(page)
            gentle_scroll(page, steps=5)

            if debug_html:
                maybe_save_debug_html(page, f"output/debug_trustradius_page_{page_no}.html", True)

            cards = page.locator("article, div.review, [data-testid*='review']")
            if cards.count() == 0:
                break

            card_count = min(cards.count(), 60)
            for i in range(card_count):
                card = cards.nth(i)

                body = None
                for sel in ["p", "div[class*='review']", "div[class*='content']"]:
                    try:
                        t = card.locator(sel).first.inner_text(timeout=800).strip()
                        if t and len(t) > 20:
                            body = t
                            break
                    except Exception:
                        pass

                date_text = None
                for sel in ["time", "span:has-text(', 20')", "div:has-text(', 20')"]:
                    try:
                        t = card.locator(sel).first.inner_text(timeout=800).strip()
                        if t:
                            date_text = t
                            break
                    except Exception:
                        pass

                d = parse_any_date(date_text or "")
                if body and in_range(d, start_d, end_d):
                    title = ""
                    try:
                        title = card.locator("h3").first.inner_text(timeout=500).strip()
                    except Exception:
                        pass
                    collected.append({
                        "source": "TrustRadius",
                        "title": title,
                        "review": body,
                        "date": d.isoformat(),
                        "rating": None
                    })

            human_sleep(0.8, 1.6)

        context.close()
        browser.close()

    return collected


# ----------------------------
# Capterra note: often requires numeric product id; keep as placeholder for interface completeness
# ----------------------------
def scrape_capterra(company_id: str, start_d: date, end_d: date, debug_html: bool,
                    headless: bool, slow_mo: int) -> List[Dict]:
    # Many Capterra pages require product numeric IDs and are bot-protected.
    # Keep interface; expand later if needed.
    return []


# ----------------------------
# Main
# ----------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--company", required=True, help="G2/TrustRadius product slug (e.g., 'slack'). Capterra often needs numeric id.")
    parser.add_argument("--start_date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end_date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--source", required=True, choices=["g2", "capterra", "trustradius"])
    parser.add_argument("--max_pages", type=int, default=10)
    parser.add_argument("--debug_html", type=int, default=0)
    parser.add_argument("--headless", type=int, default=1, help="1=headless, 0=headful (recommended for G2)")
    parser.add_argument("--slow_mo", type=int, default=0, help="Slow down actions in ms (e.g., 50-150)")

    args = parser.parse_args()

    start_d = datetime.strptime(args.start_date, "%Y-%m-%d").date()
    end_d = datetime.strptime(args.end_date, "%Y-%m-%d").date()

    ensure_output_dir()
    debug_html = bool(args.debug_html)
    headless = bool(args.headless)
    slow_mo = int(args.slow_mo)

    if args.source == "g2":
        data = scrape_g2(args.company, start_d, end_d, args.max_pages, debug_html, headless, slow_mo)
    elif args.source == "trustradius":
        data = scrape_trustradius(args.company, start_d, end_d, args.max_pages, debug_html, headless, slow_mo)
    else:
        data = scrape_capterra(args.company, start_d, end_d, debug_html, headless, slow_mo)

    output_file = f"output/{args.company}_{args.source}_reviews.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"Saved {len(data)} reviews to {output_file}")


if __name__ == "__main__":
    main()
