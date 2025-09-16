#!/usr/bin/env python3
"""
LinkedIn Jobs Scraper + StaffSpy Enrichment (One-File Version)
===============================================================
- Scrapes LinkedIn job search results with Selenium.
- After scraping EACH job, clicks ✕ / Hide to force LinkedIn to refill the list (prevents stalling after ~40 pages).
- Optionally enriches companies via StaffSpy if installed (best-effort fallback).

Quick start
-----------
1) Install deps (Chrome + chromedriver must be available in PATH):
    pip install selenium pandas

   (Optional) StaffSpy for enrichment:
    pip install "staffspy[browser]"

2) Provide credentials via ENV:
    export LI_EMAIL="you@example.com"
    export LI_PASSWORD="your_password"

3) Run:
    python linkedin_jobs_plus_staffspy.py \
        --keywords "Computer Science" \
        --location "United States" \
        --nb-jobs 200 \
        --any-time \
        --headless

Notes
-----
- This script is resilient to UI variations and tries multiple selectors for lists, items, description "show more", and dismiss buttons.
- StaffSpy enrichment is OPTIONAL. If the module or APIs are not available, the script will skip enrichment gracefully.
"""

from __future__ import annotations
import os, time, re, logging, sys, argparse
from dataclasses import dataclass
from typing import List, Dict, Optional, Any
import pandas as pd

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException

# ----------------------------
# Driver / Window management
# ----------------------------

def make_driver(headless: bool = False) -> webdriver.Chrome:
    opts = Options()
    if headless:
        # new headless avoids old issues
        opts.add_argument("--headless=new")
    opts.add_argument("--lang=en-US")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    return webdriver.Chrome(options=opts)

_driver = None

def ensure_window(drv: Optional[webdriver.Chrome], headless: bool = False) -> webdriver.Chrome:
    global _driver
    if drv is None:
        _driver = make_driver(headless=headless)
        return _driver
    _driver = drv
    return drv

def safe_get(drv: webdriver.Chrome, url: str, timeout: int = 30) -> webdriver.Chrome:
    try:
        drv.get(url)
    except WebDriverException as e:
        print("driver.get failed:", e)
        time.sleep(1.0)
        drv.get(url)
    WebDriverWait(drv, timeout).until(lambda d: d.execute_script("return document.readyState") == "complete")
    return drv

# ----------------------------
# Auth
# ----------------------------

def login(drv: webdriver.Chrome, email: str, password: str, timeout: int = 20) -> None:
    safe_get(drv, "https://www.linkedin.com/login")
    WebDriverWait(drv, timeout).until(EC.presence_of_element_located((By.ID, "username")))
    u = drv.find_element(By.ID, "username"); u.clear(); u.send_keys(email)
    p = drv.find_element(By.ID, "password"); p.clear(); p.send_keys(password); p.send_keys(Keys.RETURN)
    # Wait until we're either at feed or challenged
    try:
        WebDriverWait(drv, timeout).until(
            EC.any_of(
                EC.presence_of_element_located((By.ID, "global-nav-search")),
                EC.presence_of_element_located((By.CSS_SELECTOR, "[data-test-global-nav]"))
            )
        )
    except TimeoutException:
        print("Login may require additional verification (2FA/captcha). Continuing anyway...")

# ----------------------------
# URL builder for Jobs search
# ----------------------------

class ExperienceFilter:
    ALL = "1,2,3,4,5,6"   # Any exp

def build_job_search_url(keywords: str, location: str,
                         experience: str = ExperienceFilter.ALL,
                         posted: Optional[str] = None) -> str:
    """
    posted:
      - None / ''             -> any time
      - 'past_24_hours'       -> last 24h
      - 'past_week'           -> past week
      - 'past_month'          -> past month
    """
    base = "https://www.linkedin.com/jobs/search/"
    params = {
        "keywords": keywords,
        "location": location,
        "f_E": experience,
        "position": "1",
        "pageNum": "0",
    }
    if posted == "past_24_hours":
        params["f_TPR"] = "r86400"
    elif posted == "past_week":
        params["f_TPR"] = "r604800"
    elif posted == "past_month":
        params["f_TPR"] = "r2592000"

    from urllib.parse import urlencode, quote_plus
    q = "&".join(f"{k}={quote_plus(str(v))}" for k, v in params.items())
    return f"{base}?{q}"

# ----------------------------
# Selectors
# ----------------------------

CONTAINER_SELECTORS = [
    "div.jobs-search-results-list",
    ".scaffold-layout__list > div",
    "div.jobs-search-two-pane__results",
    "div.jobs-search-results-list__container",
    "section.two-pane-serp-page__results-list",
]

ITEM_SELECTORS = [
    "li[data-occludable-job-id]",
    "ul.jobs-search-results__list > li",
    "li.jobs-search-results__list-item",
    "div.jobs-search-results__list-item",
    "ul.jobs-search__results-list li",
]

TITLE_SEL = "h3.base-search-card__title, h2.jobs-unified-top-card__job-title, a.job-card-list__title"
COMPANY_SEL = "h4.base-search-card__subtitle, a.topcard__org-name-link, span.jobs-unified-top-card__company-name"
LOCATION_SEL = "span.job-search-card__location, span.jobs-unified-top-card__bullet"
LINK_SEL = "a.base-card__full-link, a.job-card-list__title"

# Buttons that expand description
EXPAND_SELECTORS = [
    "button.show-more-less-html__button",
    "button[aria-label*='See more']",
    "button[aria-label*='Show more']",
    "button[aria-controls*='description']",
    "button[aria-expanded='false']",
    "button[aria-label*='显示更多']",
    "button[aria-label*='顯示更多']",
]

# Dismiss / Hide job (after scrape)
DISMISS_SELECTORS = [
    "button[aria-label*='Dismiss']",
    "button[aria-label*='Not a fit']",
    "button.jobs-search-two-pane__dismiss",
    "button[aria-label*='关闭']",
    "button[aria-label*='關閉']",
    "button[data-test-job-card-dismiss-button]",
    "button[aria-label*='Hide job']",
    "button[aria-label*='隐藏']",
    "button[aria-label*='隱藏']",
]

OVERFLOW_BUTTON_SELECTORS = [
    "button[aria-label*='More actions']",
    "button[aria-label*='更多操作']",
    "button[aria-label*='更多動作']",
    "button[aria-haspopup='menu']",
]
HIDE_MENU_ITEM_SELECTORS = [
    "div[role='menu'] [aria-label*='Hide job']",
    "div[role='menu'] [aria-label*='隐藏']",
    "div[role='menu'] [aria-label*='隱藏']",
]

# ----------------------------
# Helpers
# ----------------------------

def login_wall_present(drv: webdriver.Chrome) -> bool:
    try:
        # Heuristic banners that block content
        drv.find_element(By.CSS_SELECTOR, "div.sign-in-modal, form.login__form")
        return True
    except Exception:
        return False

def dismiss_login_wall(drv: webdriver.Chrome) -> bool:
    # Best effort; often requires real login
    try:
        btns = drv.find_elements(By.CSS_SELECTOR, "button, a")
        for b in btns:
            txt = (b.get_attribute("innerText") or "").strip().lower()
            if any(k in txt for k in ["sign in", "log in", "登录", "登入"]):
                b.click(); time.sleep(0.6); return True
    except Exception:
        pass
    return False

def expand_all_in(drv: webdriver.Chrome, root=None, max_clicks=10) -> bool:
    did = False
    for _ in range(max_clicks):
        clicked = False
        scope = root if root is not None else drv
        for sel in EXPAND_SELECTORS:
            try:
                for el in scope.find_elements(By.CSS_SELECTOR, sel):
                    if el.is_displayed() and el.is_enabled():
                        el.click(); did = True; clicked = True; time.sleep(0.2)
            except Exception:
                continue
        if not clicked:
            break
    return did

def scroll_until_all_jobs_load(drv: webdriver.Chrome, container, pause=0.8, max_tries=30, item_selector="li.jobs-search-results__list-item"):
    tries = 0
    last_height = drv.execute_script("return arguments[0].scrollHeight", container)
    while tries < max_tries:
        drv.execute_script("arguments[0].scrollTop = arguments[0].scrollHeight", container)
        time.sleep(pause)
        new_height = drv.execute_script("return arguments[0].scrollHeight", container)
        if new_height == last_height:
            break
        last_height = new_height; tries += 1
    items = drv.find_elements(By.CSS_SELECTOR, item_selector)
    if not items:
        items = drv.find_elements(By.CSS_SELECTOR, "ul.jobs-search__results-list li, li[data-occludable-job-id], ul.jobs-search-results__list > li")
    return items

def _click_first_present(drv: webdriver.Chrome, selectors: List[str], timeout: float = 3.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        for sel in selectors:
            try:
                els = drv.find_elements(By.CSS_SELECTOR, sel)
                for el in els:
                    if el.is_displayed() and el.is_enabled():
                        el.click()
                        return True
            except Exception:
                pass
        time.sleep(0.2)
    return False

def dismiss_current_job(drv: webdriver.Chrome, settle_wait: float = 0.8) -> bool:
    # Try direct dismiss
    if _click_first_present(drv, DISMISS_SELECTORS, timeout=2.5):
        time.sleep(settle_wait)
        return True
    # Try menu fallback
    if _click_first_present(drv, OVERFLOW_BUTTON_SELECTORS, timeout=2.0):
        time.sleep(0.3)
        if _click_first_present(drv, HIDE_MENU_ITEM_SELECTORS, timeout=2.0):
            time.sleep(settle_wait)
            return True
    # Last attempt: ESC then direct
    try:
        drv.switch_to.active_element.send_keys(Keys.ESCAPE)
        time.sleep(0.2)
    except Exception:
        pass
    if _click_first_present(drv, DISMISS_SELECTORS, timeout=1.5):
        time.sleep(settle_wait)
        return True
    return False

# ----------------------------
# Core scraping
# ----------------------------

def wait_for_job_items(drv: webdriver.Chrome, timeout: int = 20) -> None:
    WebDriverWait(drv, timeout).until(
        EC.presence_of_any_elements_located((By.CSS_SELECTOR, ",".join(ITEM_SELECTORS)))
    )

def collect_job_cards(drv: webdriver.Chrome):
    container = None
    for sel in CONTAINER_SELECTORS:
        try:
            container = drv.find_element(By.CSS_SELECTOR, sel); break
        except Exception:
            continue

    # smooth load
    for _ in range(2):
        drv.execute_script("window.scrollTo(0, document.body.scrollHeight);"); time.sleep(0.3)
    if container:
        for _ in range(2):
            drv.execute_script("arguments[0].scrollTop = arguments[0].scrollHeight;", container); time.sleep(0.3)

    items = []
    for sel in ITEM_SELECTORS:
        found = drv.find_elements(By.CSS_SELECTOR, sel)
        if found:
            items = found; break
    if container and not items:
        items = scroll_until_all_jobs_load(drv, container)
    return container, items

def scrape_jobs_from_current_page(drv: webdriver.Chrome, target_remaining: int) -> pd.DataFrame:
    if login_wall_present(drv):
        if not dismiss_login_wall(drv):
            print("Login wall present; cannot dismiss. Try logging in.")
            return pd.DataFrame()

    try:
        wait_for_job_items(drv, timeout=20)
    except TimeoutException:
        print("⚠️ No job cards visible (timeout).")
        return pd.DataFrame()

    container, items = collect_job_cards(drv)
    if not items:
        print("No items on this page.")
        return pd.DataFrame()

    data: Dict[str, List[Any]] = {
        "Title": [], "Company": [], "Location": [], "Link": [], "Description": []
    }

    list_handle = drv.current_window_handle

    for idx, it in enumerate(items):
        if target_remaining <= 0:
            break
        try:
            # Focus the card / open details
            drv.execute_script("arguments[0].scrollIntoView({block:'center'});", it); time.sleep(0.25)
            it.click(); time.sleep(0.4)

            # Extract core fields from either card or details
            title, company, location, link = "", "", "", ""

            # link direct
            try:
                link_el = it.find_element(By.CSS_SELECTOR, LINK_SEL)
                link = link_el.get_attribute("href") or ""
            except Exception:
                pass

            # title/company/location attempts
            for _scope in (it, drv):
                if not title:
                    try:
                        title = _scope.find_element(By.CSS_SELECTOR, TITLE_SEL).text.strip()
                    except Exception:
                        pass
                if not company:
                    try:
                        company = _scope.find_element(By.CSS_SELECTOR, COMPANY_SEL).text.strip()
                    except Exception:
                        pass
                if not location:
                    try:
                        for cand in _scope.find_elements(By.CSS_SELECTOR, LOCATION_SEL):
                            txt = (cand.text or "").strip()
                            if txt:
                                location = txt; break
                    except Exception:
                        pass

            # Description from details pane
            desc_root = None
            for sel in ["div.jobs-description-content__text", "div.show-more-less-html", "div.jobs-description__container"]:
                try:
                    desc_root = drv.find_element(By.CSS_SELECTOR, sel); break
                except Exception:
                    continue
            if desc_root is not None:
                expand_all_in(drv, desc_root, max_clicks=6)
            description = desc_root.text.strip() if desc_root is not None else ""

            data["Title"].append(title)
            data["Company"].append(company)
            data["Location"].append(location)
            data["Link"].append(link)
            data["Description"].append(description)

            # ---- NEW: Dismiss current job to refresh the list ----
            try:
                if dismiss_current_job(drv, settle_wait=0.8):
                    print(f"✓ Dismissed job #{idx+1} on this page.")
            except Exception as de:
                print("Dismiss error (ignored):", de)

            target_remaining -= 1

        except Exception as e:
            logging.warning(f"[job-scrape] card {idx} skipped: {e}")
        finally:
            # Close extra tabs if any
            try:
                if len(drv.window_handles) > 1:
                    drv.close()
                    drv.switch_to.window(list_handle)
            except Exception:
                pass

    df = pd.DataFrame(data)
    # de-dup by Link, keep first
    if not df.empty and "Link" in df.columns:
        df.drop_duplicates(subset=["Link"], keep="first", inplace=True, ignore_index=True)
    return df

def move_to_next_page(drv: webdriver.Chrome, page_number: int) -> bool:
    # Try numbered pagination
    try:
        # aria-label example: "Page 2"
        btn = drv.find_element(By.CSS_SELECTOR, f"button[aria-label*='Page {page_number}']")
        drv.execute_script("arguments[0].scrollIntoView({block:'center'});", btn); time.sleep(0.2)
        btn.click(); time.sleep(0.8)
        return True
    except Exception:
        pass
    # Try next button
    for sel in [
        "button[aria-label*='Next']",
        "button[aria-label*='下一頁']",
        "button[aria-label*='下一页']",
    ]:
        try:
            btn = drv.find_element(By.CSS_SELECTOR, sel)
            drv.execute_script("arguments[0].scrollIntoView({block:'center'});", btn); time.sleep(0.2)
            btn.click(); time.sleep(0.8)
            return True
        except Exception:
            continue
    return False

def scrape_linkedin_jobs(drv: webdriver.Chrome, url: str, nb_jobs: int, max_pages: int = 200) -> pd.DataFrame:
    safe_get(drv, url)
    total_df = pd.DataFrame()
    current_page = 1

    while len(total_df) < nb_jobs and current_page <= max_pages:
        page_remaining = nb_jobs - len(total_df)
        page_df = scrape_jobs_from_current_page(drv, target_remaining=page_remaining)
        if not page_df.empty:
            total_df = pd.concat([total_df, page_df], ignore_index=True)
            if "Link" in total_df.columns:
                total_df.drop_duplicates(subset=["Link"], keep="first", inplace=True, ignore_index=True)

        if len(total_df) >= nb_jobs:
            break

        moved = move_to_next_page(drv, current_page + 1)
        if not moved:
            print("No further pages found; stopping.")
            break
        current_page += 1

    total_df.reset_index(drop=True, inplace=True)
    return total_df

# ----------------------------
# Description bucketing (Requirements / Preferred / Responsibilities)
# ----------------------------

_HEADING_MAP = {
    "responsibilities": "resp",
    "what you will do": "resp",
    "what you'll do": "resp",
    "what you do": "resp",
    "duties": "resp",
    "key duties": "resp",
    "role & responsibilities": "resp",
    "role and responsibilities": "resp",
    "requirements": "genreq",
    "must have": "genreq",
    "you have": "genreq",
    "required qualifications": "req",
    "minimum qualifications": "req",
    "basic qualifications": "req",
    "preferred qualifications": "pref",
    "nice to have": "pref",
    "preferred": "pref",
}

_heading_regex = re.compile("|".join([re.escape(k) for k in _HEADING_MAP.keys()]))

def parse_description_buckets(text: str) -> Dict[str, List[str]]:
    buckets = {"resp": [], "req": [], "pref": [], "genreq": []}
    if not text:
        return buckets
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    current = None
    for ln in lines:
        lnl = ln.lower()
        if _heading_regex.search(lnl):
            # pick the first matching key
            for key, bucket in _HEADING_MAP.items():
                if key in lnl:
                    current = bucket; break
            continue
        item = ln.lstrip("-•· ").strip()
        if not item:
            continue
        if current == "resp" and len(buckets["resp"]) < 60: buckets["resp"].append(item)
        elif current == "req" and len(buckets["req"]) < 60: buckets["req"].append(item)
        elif current == "pref" and len(buckets["pref"]) < 60: buckets["pref"].append(item)
        elif current == "genreq" and len(buckets["genreq"]) < 60: buckets["genreq"].append(item)
    # fallback: if nothing parsed, take leading bullets
    if not any(buckets.values()):
        bullets = [ln.lstrip("-•· ").strip() for ln in lines if ln.startswith(("-", "•", "·"))]
        buckets["req"] = bullets[:30]
    return buckets

def split_description_sections(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["Responsibilities"] = [[] for _ in range(len(out))]
    out["QualificationsRequired"] = [[] for _ in range(len(out))]
    out["QualificationsPreferred"] = [[] for _ in range(len(out))]
    out["Requirements"] = [[] for _ in range(len(out))]
    for i, desc in enumerate(out["Description"].fillna("").tolist()):
        buckets = parse_description_buckets(desc)
        out.at[i, "Responsibilities"] = buckets["resp"]
        out.at[i, "QualificationsRequired"] = buckets["req"]
        out.at[i, "QualificationsPreferred"] = buckets["pref"]
        out.at[i, "Requirements"] = buckets["genreq"]
    return out

# ----------------------------
# Optional: StaffSpy enrichment (best-effort, skip if unavailable)
# ----------------------------

def enrich_with_staffspy(df: pd.DataFrame, email: str, password: str) -> pd.DataFrame:
    try:
        # Try common import paths
        try:
            from staffspy import LinkedInAccount, scrape_companies
        except Exception:
            from staffspy.core import LinkedInAccount  # type: ignore
            from staffspy import scrape_companies      # type: ignore
    except Exception as e:
        print("StaffSpy not installed or API not found; skipping enrichment.", e)
        return df

    try:
        print("Starting StaffSpy enrichment (companies)...")
        acc = LinkedInAccount(email=email, password=password, browser=True)
        companies = sorted(set([c for c in df["Company"].fillna("").tolist() if c.strip()]))
        if not companies:
            print("No companies to enrich.")
            return df

        # Best-effort call signature; may vary by StaffSpy version.
        try:
            comp_rows = scrape_companies(acc, companies=companies, max_results_per_company=1)  # type: ignore
        except TypeError:
            # fallback alternate signature
            comp_rows = scrape_companies(acc, companies)  # type: ignore

        # Normalize into a name->info map
        normalized = {}
        for row in comp_rows:
            name = (row.get("name") or row.get("company_name") or "").strip()
            if not name:
                continue
            key = name.lower()
            normalized[key] = {
                "CompanyIndustry": row.get("industry"),
                "CompanySize": row.get("size"),
                "CompanyWebsite": row.get("website"),
                "CompanyHQ": row.get("headquarters"),
                "CompanyFounded": row.get("founded"),
                "CompanyAbout": row.get("about") or row.get("description") or "",
            }

        # Attach to df
        def mget(cname: str, field: str):
            if not cname:
                return None
            info = normalized.get(cname.lower())
            return None if info is None else info.get(field)

        for field in ["CompanyIndustry","CompanySize","CompanyWebsite","CompanyHQ","CompanyFounded","CompanyAbout"]:
            df[field] = df["Company"].apply(lambda x: mget(x, field))

        print(f"StaffSpy enrichment done for {len(normalized)} companies.")
        return df
    except Exception as e:
        print("StaffSpy enrichment failed; continuing without it. Reason:", e)
        return df

# ----------------------------
# Master pipeline
# ----------------------------

def scrape_and_parse_linkedin_jobs(nb_jobs: int,
                                   keywords: str = "Computer Science",
                                   location: str = "United States",
                                   experience: str = ExperienceFilter.ALL,
                                   posted: Optional[str] = None,
                                   headless: bool = False,
                                   email: Optional[str] = None,
                                   password: Optional[str] = None,
                                   enrich_staffspy: bool = False) -> pd.DataFrame:

    drv = ensure_window(None, headless=headless)
    if email and password:
        login(drv, email, password)

    url = build_job_search_url(keywords=keywords, location=location, experience=experience, posted=posted)
    df = scrape_linkedin_jobs(drv, url=url, nb_jobs=nb_jobs)
    if df.empty:
        return df

    df = split_description_sections(df)

    if enrich_staffspy and email and password:
        df = enrich_with_staffspy(df, email=email, password=password)

    return df

# ----------------------------
# CLI
# ----------------------------

def parse_args():
    ap = argparse.ArgumentParser(description="LinkedIn job scraper + StaffSpy enrichment")
    ap.add_argument("--keywords", type=str, default="Computer Science", help="Search keywords")
    ap.add_argument("--location", type=str, default="United States", help="Search location")
    ap.add_argument("--nb-jobs", type=int, default=200, help="Number of jobs to scrape")
    timef = ap.add_mutually_exclusive_group()
    timef.add_argument("--past-24h", dest="past24", action="store_true", help="Only last 24 hours")
    timef.add_argument("--past-week", dest="pastweek", action="store_true", help="Only last week")
    timef.add_argument("--past-month", dest="pastmonth", action="store_true", help="Only last month")
    timef.add_argument("--any-time", dest="anytime", action="store_true", help="Any time (default)")
    ap.add_argument("--headless", action="store_true", help="Run Chrome headless")
    ap.add_argument("--enrich-staffspy", action="store_true", help="Try to enrich companies using StaffSpy")
    ap.add_argument("--out", type=str, default="linkedin_jobs_enriched.csv", help="Output CSV path")
    return ap.parse_args()

def main():
    args = parse_args()
    posted = None
    if args.past24:   posted = "past_24_hours"
    elif args.pastweek: posted = "past_week"
    elif args.pastmonth: posted = "past_month"
    else: posted = None

    email = os.getenv("LI_EMAIL") or ""
    password = os.getenv("LI_PASSWORD") or ""
    if not email or not password:
        print("⚠️ LI_EMAIL / LI_PASSWORD not found in environment. Proceeding without login (may hit limits/login wall).")

    df = scrape_and_parse_linkedin_jobs(
        nb_jobs=args.nb_jobs,
        keywords=args.keywords,
        location=args.location,
        experience=ExperienceFilter.ALL,
        posted=posted,
        headless=args.headless,
        email=email or None,
        password=password or None,
        enrich_staffspy=args.enrich_staffspy
    )

    if df.empty:
        print("No data scraped.")
        return

    df.to_csv(args.out, index=False)
    print(f"Saved {len(df)} rows to {args.out}")

if __name__ == "__main__":
    main()
