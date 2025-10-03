import re, time, random, sys
import requests
import pandas as pd
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup

# --- Selenium 4 + webdriver-manager (instala: pip install selenium webdriver-manager bs4 pandas requests) ---
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.common.exceptions import TimeoutException, WebDriverException, NoSuchElementException
from webdriver_manager.chrome import ChromeDriverManager

SITEMAP_INDEX = "https://www.wong.pe/sitemap.xml"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36 +edu-scraper")
BASE = "https://www.wong.pe"

# -------------------------- Utils --------------------------
def jitter(a=0.7, b=1.4):
    time.sleep(random.uniform(a, b))

def mk_driver(headless=True):
    """ChromeDriver con Selenium 4 (service=...)."""
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--window-size=1440,1024")
    opts.add_argument("--lang=es-PE")
    opts.add_argument(f"user-agent={UA}")
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=opts)
    driver.set_page_load_timeout(60)
    driver.implicitly_wait(8)
    return driver

def soup_of(driver):
    return BeautifulSoup(driver.page_source, "html.parser")

def to_price(i_txt, f_txt):
    """Une entero y fracción -> Decimal(2)."""
    if not i_txt or not f_txt:
        return None
    try:
        i = int(re.sub(r"\D", "", str(i_txt)))
        f = int(re.sub(r"\D", "", str(f_txt)))
        val = Decimal(f"{i}.{f:02d}")
        return val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception:
        return None

# ------------------- 1) Descubrir product sitemaps -------------------
def get_product_sitemap_urls(index_url=SITEMAP_INDEX):
    """
    Lee el sitemap index y devuelve URLs tipo .../sitemap/product-*.xml
    """
    r = requests.get(index_url, timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    urls = []
    for sm in root.findall("sm:sitemap", ns):
        loc_el = sm.find("sm:loc", ns)
        if loc_el is None:
            continue
        loc = (loc_el.text or "").strip()
        if "/sitemap/product-" in loc:
            urls.append(loc)
    return urls

# ------------------- 2) Extraer PDP (/p) desde los sitemaps -------------------
def get_pdp_urls_from_sitemaps(sitemap_urls, limit=None):
    """
    Abre cada product-*.xml y devuelve todas las <loc> que contengan '/p'
    (deduplicadas). 'limit' te permite cortar si quieres.
    """
    pdp = set()
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    for smurl in sitemap_urls:
        try:
            r = requests.get(smurl, timeout=45)
            r.raise_for_status()
            root = ET.fromstring(r.content)
            for url in root.findall("sm:url", ns):
                loc_el = url.find("sm:loc", ns)
                if loc_el is None:
                    continue
                loc = (loc_el.text or "").strip()
                if "/p" in loc:
                    pdp.add(loc)
                    if limit and len(pdp) >= limit:
                        return list(pdp)
        except Exception:
            continue
        jitter(0.2, 0.6)
    return list(pdp)

# ------------------- 3) Scrapeo de PDP (7 campos + url + timestamp) -------------------
def extract_from_pdp(driver, url):
    driver.get(url)
    jitter(1.1, 2.0)
    s = soup_of(driver)

    # Selectores validados en tus ejemplos (VTEX Product Price)
    name = s.select_one("span.vtex-store-components-3-x-productBrand")
    wi = s.select_one("span.vtex-product-price-1-x-currencyInteger--PDPPrice")
    wf = s.select_one("span.vtex-product-price-1-x-currencyFraction--PDPPrice")
    si = s.select_one("span.vtex-product-price-1-x-currencyInteger--PDPListPrice")
    sf = s.select_one("span.vtex-product-price-1-x-currencyFraction--PDPListPrice")

    rec = {
        "product_name": name.get_text(strip=True) if name else None,
        "price_web_integer": wi.get_text(strip=True) if wi else None,
        "price_web_fraction": wf.get_text(strip=True) if wf else None,
        "price_store_integer": si.get_text(strip=True) if si else None,
        "price_store_fraction": sf.get_text(strip=True) if sf else None,
        "url": url,
        "timestamp_scrape": datetime.now().isoformat(timespec="seconds"),
        "category_total_products": None  # sólo existe en PLP; aquí lo dejamos nulo
    }

    pw = to_price(rec["price_web_integer"], rec["price_web_fraction"])
    ps = to_price(rec["price_store_integer"], rec["price_store_fraction"])
    rec["price_web"] = float(pw) if pw is not None else None
    rec["price_store"] = float(ps) if ps is not None else None
    return rec

# ------------------- 4) Runner: sitemap → 1000 filas -------------------
def run_from_sitemaps(target_total=1000, headless=True, out_csv="wong_products_sitemap.csv", url_dump_csv="wong_pdp_urls.csv"):
    # 1) índice → product-*.xml
    sm_urls = get_product_sitemap_urls()
    if not sm_urls:
        raise RuntimeError("No se hallaron product sitemaps en el index.")

    # 2) product-* → PDPs /p (sin límite; dedupe)
    pdp_urls = get_pdp_urls_from_sitemaps(sm_urls, limit=None)
    random.shuffle(pdp_urls)  # distribución de carga
    print(f"[DISCOVERY] PDP encontradas: {len(pdp_urls)}")

    # dump opcional de URLs (útil para depurar / reanudar)
    try:
        pd.DataFrame({"url": pdp_urls}).to_csv(url_dump_csv, index=False)
        print(f"[DUMP] {url_dump_csv} (todas las PDP descubiertas)")
    except Exception:
        pass

    # 3) visitar PDPs hasta llegar al target
    rows, seen = [], set()
    driver = mk_driver(headless=headless)
    try:
        for u in pdp_urls:
            if u in seen:
                continue
            try:
                rec = extract_from_pdp(driver, u)
                rows.append(rec)
                seen.add(u)
            except (TimeoutException, WebDriverException):
                continue
            if len(rows) >= target_total:
                break
            jitter(0.8, 1.5)
    finally:
        driver.quit()

    # 4) guardar
    df = pd.DataFrame(rows).drop_duplicates(subset=["url", "product_name"])
    df.insert(0, "source", "wong.pe")
    df.to_csv(out_csv, index=False, encoding="utf-8-sig")
    print(f"[DONE] {out_csv} rows={len(df)}")

# ------------------- main -------------------
if __name__ == "__main__":
    # corre directo desde sitemaps para alcanzar 1000+
    run_from_sitemaps(
        target_total=1000,
        headless=True,
        out_csv="wong_products_sitemap.csv",
        url_dump_csv="wong_pdp_urls.csv"
    )
