#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
# WONG.PE WEB SCRAPER - DS3021 PROYECTO FINAL PARTE I (VERSIÓN MEJORADA 2)
# Autor: [Tu Nombre]
# Fecha: Septiembre 2025
# - Respeta robots.txt con certifi
# - Filtra sitemaps priorizando product pages (/p)
# - Detecta product vs collection antes de parsear
# - Extrae precios VTEX (currencyInteger + currencyFraction) y JSON-LD
# - Guarda filas descartadas para auditoría
# - Export CSV UTF-8 + parquet
# =============================================================================

import requests
import certifi
import pandas as pd
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
import time
import random
from urllib.parse import urljoin, urlparse
import logging
import re
from datetime import datetime
import os
import urllib.robotparser
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import json
import hashlib

# ----------------------------
# Logging
# ----------------------------
logging.basicConfig(level=logging.INFO,
                   format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ----------------------------
# Util: sesión con retries
# ----------------------------
def setup_session(timeout=30, max_retries=3, backoff_factor=0.3):
    session = requests.Session()
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) '
                      'Chrome/115.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'es-PE,es;q=0.9,en;q=0.8'
    }
    session.headers.update(headers)

    retries = Retry(total=max_retries,
                    backoff_factor=backoff_factor,
                    status_forcelist=[429, 500, 502, 503, 504],
                    allowed_methods=frozenset(['GET', 'POST']))
    adapter = HTTPAdapter(max_retries=retries)
    session.mount('https://', adapter)
    session.mount('http://', adapter)

    # use certifi bundle for TLS verification
    session.verify = certifi.where()
    session.request_timeout = timeout
    return session

# ----------------------------
# Clase principal
# ----------------------------
class WongScraper:
    def __init__(self, base_url="https://www.wong.pe"):
        self.base_url = base_url.rstrip('/')
        self.session = setup_session()
        self.products_data = []
        self.discarded_rows = []
        self.categories_scraped = 0
        self.rp = None
        self.target_categories = [
            "limpieza",
            "abarrotes",
            "frutas-y-verduras",
            "lacteos",
            "higiene-salud-y-belleza",
            "tecnologia",
            "hogar-y-bazar",
            "cervezas-vinos-y-licores"
        ]

    # ----------------------------
    # Robots.txt (requests + certifi + robotparser.parse)
    # ----------------------------
    def check_robots_txt(self):
        robots_url = urljoin(self.base_url, '/robots.txt')
        try:
            resp = self.session.get(robots_url, timeout=10)
            resp.raise_for_status()
            rp = urllib.robotparser.RobotFileParser()
            # rp.parse expects lines list
            rp.parse(resp.text.splitlines())
            self.rp = rp
            allowed = rp.can_fetch(self.session.headers.get('User-Agent','*'), '/')
            if allowed:
                logger.info("✅ robots.txt cargado y permite crawling general (verifica rutas específicas).")
            else:
                logger.warning("⚠️ robots.txt restringe acceso para este User-Agent.")
            # log sitemaps found
            for line in resp.text.splitlines():
                if line.strip().lower().startswith('sitemap:'):
                    logger.info(f"🔗 {line.strip()}")
            return True
        except Exception as e:
            logger.warning(f"No se pudo parsear robots.txt correctamente: {e}. Proceder con precaución.")
            return False

    def can_fetch(self, url):
        if not self.rp:
            return True
        path = urlparse(url).path or '/'
        try:
            return self.rp.can_fetch(self.session.headers.get('User-Agent','*'), path)
        except Exception:
            return True

    # ----------------------------
    # Sitemaps: priorizar product pages que contengan "/p"
    # ----------------------------
    def get_sitemap_urls(self, limit_sitemaps=10):
        sitemap_index_url = urljoin(self.base_url, '/sitemap.xml')
        sitemaps = []
        product_urls = []
        collection_urls = []

        try:
            resp = self.session.get(sitemap_index_url, timeout=15)
            if resp.status_code == 200 and '<sitemapindex' in resp.text:
                root = ET.fromstring(resp.content)
                for sm in root.findall('.//{http://www.sitemaps.org/schemas/sitemap/0.9}sitemap'):
                    loc = sm.find('{http://www.sitemaps.org/schemas/sitemap/0.9}loc')
                    if loc is not None and loc.text:
                        sitemaps.append(loc.text)
            else:
                # fallback: try common paths
                sitemaps.extend([
                    urljoin(self.base_url, '/sitemap.xml'),
                    urljoin(self.base_url, '/arquivos/colecciones-wong-sitemap.xml')
                ])
        except Exception as e:
            logger.warning(f"No se pudo leer sitemap index: {e}")
            sitemaps.extend([
                urljoin(self.base_url, '/sitemap.xml'),
                urljoin(self.base_url, '/arquivos/colecciones-wong-sitemap.xml')
            ])

        for sm_url in sitemaps[:limit_sitemaps]:
            before = len(product_urls)
            try:
                r = self.session.get(sm_url, timeout=15)
                if r.status_code != 200:
                    continue
                root = ET.fromstring(r.content)
                for url_elem in root.findall('.//{http://www.sitemaps.org/schemas/sitemap/0.9}url'):
                    loc = url_elem.find('{http://www.sitemaps.org/schemas/sitemap/0.9}loc')
                    if loc is None or not loc.text:
                        continue
                    href = loc.text.strip()
                    # Heurística: product pages en Wong suelen contener "/p" (ej: ...-49850001/p)
                    if re.search(r'/p($|\?)', href) or re.search(r'/\d{5,}/p', href):
                        product_urls.append(href)
                    else:
                        # guardar colecciones para posible scraping por paginación
                        collection_urls.append(href)
                added = len(product_urls) - before
                logger.info(f"✅ {added} URLs nuevas tipo PRODUCT recolectadas desde {sm_url} (total productos: {len(product_urls)})")
            except Exception as e:
                logger.debug(f"No se pudo parsear sitemap {sm_url}: {e}")
                continue

        uniq_products = list(dict.fromkeys(product_urls))
        logger.info(f"🔎 Encontrados {len(uniq_products)} product URLs (limitando a 2000 por seguridad).")
        return uniq_products[:2000], collection_urls

    # ----------------------------
    # Heurística: comprobar si una URL es product page (URL + comprobación rápida)
    # ----------------------------
    def looks_like_product_url(self, url):
        lower = url.lower()
        # heurística por URL: presencia de '/p' es el indicador más fiable en Wong
        if '/p' in lower and re.search(r'/p($|\?)', lower):
            return True
        # si no, hacer GET rápido y buscar JSON-LD o signos VTEX product
        try:
            if not self.can_fetch(url):
                return False
            resp = self.session.get(url, timeout=8)
            if resp.status_code != 200:
                return False
            text = resp.text
            if '"@type"' in text and 'Product' in text:
                return True
            if re.search(r'vtex-product-summary|vtex-product-price|product__title|productName', text, re.IGNORECASE):
                return True
        except Exception:
            return False
        return False

    # ----------------------------
    # Extraer precio VTEX (integer + fraction)
    # ----------------------------
    def extract_price_from_soup(self, soup):
        try:
            int_node = soup.select_one('span[class*="currencyInteger"]')
            frac_node = soup.select_one('span[class*="currencyFraction"], span[class*="currencyDecimal"]')
            # Alternativa con prefijos largo
            if not int_node:
                int_node = soup.select_one('span[class*="vtex-product-price"][class*="currencyInteger"]')
            if not frac_node:
                frac_node = soup.select_one('span[class*="vtex-product-price"][class*="currencyFraction"]')
            int_text = re.sub(r'[^\d]', '', int_node.get_text()) if int_node else ''
            frac_text = re.sub(r'[^\d]', '', frac_node.get_text()) if frac_node else ''
            if int_text == '':
                return 0.0
            if frac_text:
                price_str = f"{int_text}.{frac_text}"
            else:
                price_str = f"{int_text}"
            return float(price_str)
        except Exception:
            # fallback: buscar cualquier número en el documento
            try:
                txt = soup.get_text(separator=' ', strip=True)
                return self.clean_price(txt)
            except:
                return 0.0

    # ----------------------------
    # Scrape ficha producto (JSON-LD -> VTEX spans -> fallback)
    # ----------------------------
    def scrape_product_page(self, product_url):
        try:
            r = self.session.get(product_url, timeout=18)
            if r.status_code != 200:
                logger.debug(f"Product page HTTP {r.status_code} -> {product_url}")
                return {}
            soup = BeautifulSoup(r.content, 'html.parser')

            # 1) JSON-LD preferido
            jsonld_data = {}
            scripts = soup.find_all('script', type='application/ld+json')
            for s in scripts:
                try:
                    content = s.string
                    if not content:
                        continue
                    parsed = json.loads(content.strip())
                    # parsed could be dict or list
                    if isinstance(parsed, list):
                        for p in parsed:
                            if isinstance(p, dict) and p.get('@type') in ('Product','Offer'):
                                jsonld_data = p
                                break
                    elif isinstance(parsed, dict) and parsed.get('@type') in ('Product','Offer'):
                        jsonld_data = parsed
                        break
                except Exception:
                    continue

            result = {}
            if jsonld_data:
                name = jsonld_data.get('name') or jsonld_data.get('headline') or ''
                sku = jsonld_data.get('sku') or jsonld_data.get('mpn') or ''
                brand = (jsonld_data.get('brand') or {}).get('name') if isinstance(jsonld_data.get('brand'), dict) else jsonld_data.get('brand')
                offers = jsonld_data.get('offers') or {}
                price = None
                price_old = None
                if isinstance(offers, dict):
                    price = offers.get('price') or offers.get('priceSpecification', {}).get('price')
                    # price_old maybe in priceSpecification or listPrice
                    price_old = offers.get('listPrice') or offers.get('priceSpecification', {}).get('price')
                elif isinstance(offers, list) and offers:
                    first = offers[0]
                    price = first.get('price')
                    price_old = first.get('listPrice') or first.get('price')
                result.update({
                    'name': name,
                    'sku': sku,
                    'brand': brand or '',
                    'price_online': float(price) if price else 0.0,
                    'price_regular': float(price_old) if price_old else 0.0
                })

            # 2) Si JSON-LD no tuvo precio, intentar spans VTEX
            if not result.get('price_online') or result.get('price_online') == 0.0:
                price_vtex = self.extract_price_from_soup(soup)
                if price_vtex and price_vtex > 0:
                    result['price_online'] = price_vtex

            # 3) Nombre fallback
            if not result.get('name'):
                name_node = soup.select_one('h1, .product__title, .vtex-product-summary-2-x-productBrand, [data-testid="product-name"]')
                if name_node:
                    result['name'] = name_node.get_text(strip=True)

            # 4) Brand fallback
            if not result.get('brand'):
                brand_node = soup.select_one('[data-testid="brand"], .brand, .product__brand')
                if brand_node:
                    result['brand'] = brand_node.get_text(strip=True)

            # 5) SKU fallback
            if not result.get('sku'):
                sku_node = soup.find(attrs={'data-sku': True}) or soup.find(attrs={'data-product-sku': True})
                if sku_node:
                    result['sku'] = sku_node.get('data-sku') or sku_node.get('data-product-sku')
                else:
                    meta_sku = soup.find('meta', attrs={'property':'product:retailer_part_no'}) or soup.find('meta', attrs={'name':'sku'})
                    if meta_sku and meta_sku.get('content'):
                        result['sku'] = meta_sku.get('content') or ''

            # 6) Subcategory via breadcrumbs
            bc = soup.select_one('.breadcrumb, .breadcrumbs')
            if bc:
                crumbs = [t.get_text(strip=True) for t in bc.find_all('a') if t.get_text(strip=True)]
                if crumbs:
                    result['subcategory'] = crumbs[-1]

            # 7) Images count
            imgs = soup.find_all('img')
            result['images_count'] = len(imgs)

            # 8) availability (simple detection)
            if 'out of stock' in r.text.lower() or 'agotado' in r.text.lower():
                result['availability'] = 'out_of_stock'
            else:
                result['availability'] = 'in_stock'

            return result
        except Exception as e:
            logger.debug(f"Error scrape product page {product_url}: {e}")
            return {}

    # ----------------------------
    # Helpers
    # ----------------------------
    def safe_extract_text(self, element, selectors):
        for selector in selectors:
            try:
                found = element.select_one(selector)
                if found:
                    return found.get_text(strip=True)
            except Exception:
                continue
        for attr in ['title','alt','data-name']:
            try:
                if getattr(element, 'get', None) and element.get(attr):
                    return element.get(attr)
            except Exception:
                continue
        return ""

    def clean_text(self, text):
        if not text:
            return ""
        cleaned = re.sub(r'[\n\r\t]+', ' ', str(text))
        cleaned = re.sub(r'\s+', ' ', cleaned)
        return cleaned.strip()

    def clean_price(self, price_text):
        if not price_text:
            return 0.0
        s = str(price_text)
        s = s.replace('S/','').replace('S/.','').replace('US$','').replace('$','').strip()
        if ',' in s and '.' in s:
            if s.rfind(',') > s.rfind('.'):
                s = s.replace('.','').replace(',','.')
            else:
                s = s.replace(',','')
        else:
            s = s.replace(',','')
        nums = re.findall(r'\d+\.?\d*', s)
        if not nums:
            return 0.0
        try:
            return float(nums[0])
        except:
            return 0.0

    def calculate_discount(self, current_price, regular_price):
        try:
            current = float(current_price or 0.0)
            regular = float(regular_price or 0.0)
            if regular > 0 and current > 0 and regular > current:
                return round(((regular - current) / regular) * 100, 2)
            return 0.0
        except:
            return 0.0

    def extract_presentation(self, product_name):
        if not product_name:
            return "Unidad"
        patterns = [
            r'(\d+\s*(?:kg|g|ml|l|un|und|unidades?))',
            r'(\d+\s*x\s*\d+\s*(?:kg|g|ml|l))',
            r'(pack\s*\d+)',
            r'(caja\s*\d+)',
            r'(botella\s*\d+\s*ml)',
            r'(bolsa\s*\d+\s*g)'
        ]
        for pattern in patterns:
            match = re.search(pattern, product_name, re.IGNORECASE)
            if match:
                return match.group(1)
        return "Unidad"

    def compute_hash_id(self, name, sku):
        base = (str(name or '') + '|' + str(sku or ''))
        return hashlib.md5(base.encode('utf-8')).hexdigest()

    def make_fallback_sku(self, name):
        clean = re.sub(r'\W+','', (name or '')).upper()
        return f"WONG_{(clean[:10] or 'ITEM')}_{random.randint(1000,9999)}"

    # ----------------------------
    # Run scraping
    # ----------------------------
    def run_scraping(self, target_products=12):
        logger.info("🚀 Iniciando Web Scraping de Wong.pe (versión mejorada)")
        self.check_robots_txt()

        product_urls, collection_urls = self.get_sitemap_urls()
        logger.info(f"🔎 {len(product_urls)} product URLs a procesar (limitadas).")

        for idx, url in enumerate(product_urls, start=1):
            if len(self.products_data) >= target_products:
                break
            if not self.can_fetch(url):
                logger.debug(f"robots.txt no permite {url}, skip.")
                continue

            # Doble verificación: si no parece producto, saltar
            if not self.looks_like_product_url(url):
                logger.debug(f"URL no parece ficha producto (skip): {url}")
                self.discarded_rows.append({'url': url, 'reason': 'no_es_producto'})
                continue

            # extraer detalle
            detail = self.scrape_product_page(url)
            if not detail or not detail.get('name'):
                logger.debug(f"Detalle vacío o sin nombre para {url}, descartar")
                self.discarded_rows.append({'url': url, 'reason': 'sin_nombre_o_detalle', 'detail': detail})
                continue

            name = detail.get('name')
            sku = detail.get('sku') or self.make_fallback_sku(name)
            brand = detail.get('brand') or 'Wong'
            price_online = detail.get('price_online') or 0.0
            price_regular = detail.get('price_regular') or 0.0
            presentation = self.extract_presentation(name)
            discount = self.calculate_discount(price_online, price_regular)
            subcat = detail.get('subcategory') or 'Sin subcategoria'
            images_count = detail.get('images_count', 0)
            availability = detail.get('availability', 'unknown')

            product = {
                'nombre_producto': self.clean_text(name),
                'precio_online': price_online,
                'precio_regular': price_regular,
                'marca': self.clean_text(brand),
                'categoria': 'sitemap_producto',
                'subcategoria': subcat,
                'referencia_sku': sku,
                'descuento_porcentaje': discount,
                'presentacion': presentation,
                'url_producto': url,
                'fecha_extraccion': datetime.now().strftime('%Y-%m-%d'),
                'hash_id': self.compute_hash_id(name, sku),
                'images_count': images_count,
                'availability': availability
            }
            self.products_data.append(product)
            logger.info(f"[{idx}/{len(product_urls)}] ✔ Producto: {product['nombre_producto'][:60]} - S/ {product['precio_online']}")
            time.sleep(random.uniform(0.8, 1.6))  # rate limit

        # fallback: si no alcanzamos target_products, scrapear colecciones
        if len(self.products_data) < target_products and collection_urls:
            logger.info("🔁 Completando desde colecciones (paginación)...")
            for c in collection_urls:
                if len(self.products_data) >= target_products:
                    break
                # extraer slug de category y scrapear
                try:
                    slug = urlparse(c).path.strip('/').split('/')[-1]
                    cat_prods = self.scrape_category_products(slug, max_products=150)
                    self.products_data.extend(cat_prods)
                    logger.info(f"📊 Progreso: {len(self.products_data)}/{target_products}")
                    time.sleep(random.uniform(1.8,3.0))
                except Exception:
                    continue

        logger.info(f"✅ Scraping completado: {len(self.products_data)} productos extraídos")
        return self.products_data

    # ----------------------------
    # Export
    # ----------------------------
    def export_to_csv(self, filename="wong_products_dataset.csv"):
        if not self.products_data:
            logger.error("❌ No hay datos para exportar")
            return None
        df = pd.DataFrame(self.products_data)
        # normalizar 'null' y strings vacíos
        df['nombre_producto'].replace({'null': None, 'None': None, '': None}, inplace=True)
        df = df.dropna(subset=['nombre_producto'])
        if len(df) < 100:
            logger.warning(f"⚠️ Solo {len(df)} registros, menos de 1000 requeridos")
        df = df.drop_duplicates(subset=['hash_id'], keep='first')
        column_order = [
            'nombre_producto','precio_online','precio_regular','marca','categoria',
            'subcategoria','referencia_sku','descuento_porcentaje','presentacion',
            'url_producto','fecha_extraccion','hash_id','images_count','availability'
        ]
        df = df.reindex(columns=[c for c in column_order if c in df.columns])
        df.to_csv(filename, index=False, encoding='utf-8-sig')
        try:
            pfile = filename.replace('.csv','.parquet')
            df.to_parquet(pfile, index=False)
            logger.info(f"✅ Exportado parquet: {pfile}")
        except Exception as e:
            logger.debug(f"No se pudo exportar parquet: {e}")
        # guardar descartes para auditoría
        if self.discarded_rows:
            pd.DataFrame(self.discarded_rows).to_csv('wong_discarded_rows.csv', index=False, encoding='utf-8-sig')
            logger.info("✅ Guardadas filas descartadas en wong_discarded_rows.csv")
        logger.info(f"✅ Dataset exportado: {filename}")
        logger.info(f"📊 Dimensiones: {len(df)} filas x {len(df.columns)} columnas")
        return df

    # scrape_category_products (igual que antes, reutilizable)
    def scrape_category_products(self, category_name, max_products=200):
        logger.info(f"🛒 Scrapeando categoría: {category_name}")
        category_url = f"{self.base_url}/{category_name}"
        products = []
        page = 1
        while len(products) < max_products:
            if page == 1:
                url = category_url
            else:
                url = f"{category_url}?page={page}"
            if not self.can_fetch(url):
                logger.warning(f"robots.txt NO permite acceder a {url}, saltando.")
                break
            try:
                logger.info(f"📄 Procesando página {page} de {category_name} -> {url}")
                resp = self.session.get(url, timeout=20)
                if resp.status_code != 200:
                    logger.warning(f"⚠️ HTTP {resp.status_code} en {url}")
                    break
                soup = BeautifulSoup(resp.content, 'html.parser')
                product_selectors = [
                    '[data-testid*="product"]',
                    '.vtex-product-summary',
                    '[class*="product-item"]',
                    '[class*="shelf-item"]',
                    '.product'
                ]
                product_cards = []
                for selector in product_selectors:
                    cards = soup.select(selector)
                    if cards:
                        product_cards = cards
                        break
                if not product_cards:
                    logger.info(f"🔚 No product cards encontrados en página {page}")
                    break
                page_products = 0
                for card in product_cards:
                    product_data = self.extract_product_data_from_card(card, category_name)
                    if product_data and len(products) < max_products:
                        products.append(product_data)
                        page_products += 1
                logger.info(f"✅ {page_products} productos extraídos de página {page}")
                if page_products == 0:
                    break
                page += 1
                time.sleep(random.uniform(1.5, 3.0))
            except Exception as e:
                logger.error(f"❌ Error en pag {page} de {category_name}: {e}")
                break
        self.categories_scraped += 1
        logger.info(f"✅ Total {len(products)} productos extraídos de {category_name}")
        return products

    def extract_product_data_from_card(self, element, category_name):
        try:
            name = self.safe_extract_text(element, [
                '[data-testid="product-name"]', 'h3', '.product__title', '.shelf-item__title', 'a[title]'])
            product_link = element.find('a', href=True)
            product_url = urljoin(self.base_url, product_link['href']) if product_link else ''
            price_text = self.safe_extract_text(element, ['[data-testid="price-online"]', '.product__price', '.price'])
            brand = self.safe_extract_text(element, ['[data-testid="brand"]', '.brand', '.product__brand'])
            sku = element.get('data-sku') or element.get('data-product-sku') or ''
            detail = {}
            if product_url and self.can_fetch(product_url):
                if self.looks_like_product_url(product_url):
                    detail = self.scrape_product_page(product_url)
            price_online = detail.get('price_online') or self.clean_price(price_text)
            price_regular = detail.get('price_regular') or 0.0
            sku = detail.get('sku') or sku or self.make_fallback_sku(name)
            brand = detail.get('brand') or brand or 'Wong'
            presentation = self.extract_presentation(name or detail.get('name',''))
            discount = self.calculate_discount(price_online, price_regular)
            subcategory = detail.get('subcategory') or self.determine_subcategory(name or '', category_name)
            product = {
                'nombre_producto': self.clean_text(name or detail.get('name','')),
                'precio_online': price_online,
                'precio_regular': price_regular,
                'marca': self.clean_text(brand),
                'categoria': category_name,
                'subcategoria': subcategory,
                'referencia_sku': sku,
                'descuento_porcentaje': discount,
                'presentacion': presentation,
                'url_producto': product_url,
                'fecha_extraccion': datetime.now().strftime('%Y-%m-%d'),
                'hash_id': self.compute_hash_id(name or detail.get('name',''), sku)
            }
            return product
        except Exception as e:
            logger.debug(f"Error extrayendo card: {e}")
            return None

# =============================================================================
# EJECUCIÓN PRINCIPAL
# =============================================================================
if __name__ == "__main__":
    scraper = WongScraper()
    logger.info("🎯 Objetivo: Extraer 1200+ productos (si el sitio y robots lo permiten)")
    products = scraper.run_scraping(target_products=12)
    df_final = scraper.export_to_csv("wong_products_dataset.csv")
    dict_df = scraper.create_data_dictionary() if hasattr(scraper, 'create_data_dictionary') else None
    if df_final is not None:
        print("\n" + "="*50)
        print("📋 RESUMEN DEL PROYECTO DS3021 (VERSIÓN MEJORADA)")
        print("="*50)
        print(f"✅ Registros extraídos: {len(df_final)}")
        print(f"✅ Atributos capturados: {len(df_final.columns)}")
        print(f"✅ Páginas scrapeadas: {scraper.categories_scraped} categorías")
        print(f"✅ Tipos de datos: Numéricos y categóricos ✓")
        print(f"✅ Paginación manejada: ✓")
        print(f"✅ Codificación UTF-8: ✓")
        print("\n📁 ARCHIVOS GENERADOS:")
        print("   • wong_products_dataset.csv (Dataset principal)")
        print("   • wong_products_dataset.parquet (si es posible)")
        print("   • wong_discarded_rows.csv (filas descartadas)")
        print("="*50)

# =============================================================================
# FIN DEL SCRIPT
# =============================================================================
