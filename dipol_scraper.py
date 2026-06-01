#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dipol_scraper.py
================
Stiahne produkty z vybranych kategorii e-shopu www.dipol.sk a vygeneruje
XML feed vo formate, ktory vie naimportovat Shoptet (automaticky import cez URL).

Pouzitie:
    pip install -r requirements.txt
    python dipol_scraper.py                 # pouzije config.json v rovnakom priecinku
    python dipol_scraper.py --config moj.json

Vystupom je subor (podla config "vystup_xml", napr. dipol_feed.xml), ktory
nahras na svoj web/hosting a v Shoptete nastavis ako zdroj automatickeho importu.

POZOR (pravne): Republikovanie popisov a fotiek z dipol.sk si over s dipol.sk
ako ich autorizovany predajca. Skript stahuje len verejne dostupne MO ceny.
"""

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin
from xml.sax.saxutils import escape

import requests
from bs4 import BeautifulSoup

BASE = "https://www.dipol.sk"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("dipol")


# ---------------------------------------------------------------------------
# Pomocne funkcie
# ---------------------------------------------------------------------------
def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def make_session(cfg: dict) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": cfg["siet"]["user_agent"],
                      "Accept-Language": "sk,cs;q=0.8,en;q=0.5"})
    return s


def fetch(session: requests.Session, url: str, cfg: dict) -> str | None:
    """Stiahne stranku s opakovanim pri chybe."""
    net = cfg["siet"]
    for attempt in range(1, net["max_pokusov"] + 1):
        try:
            r = session.get(url, timeout=net["timeout_sekundy"])
            r.raise_for_status()
            r.encoding = r.apparent_encoding or "utf-8"
            return r.text
        except requests.RequestException as exc:
            log.warning("  pokus %d/%d zlyhal (%s): %s",
                        attempt, net["max_pokusov"], url, exc)
            time.sleep(net["pauza_sekundy"] * attempt)
    log.error("  NEPODARILO sa stiahnut: %s", url)
    return None


def to_number(text: str | None):
    if not text:
        return None
    m = re.search(r"(\d[\d\s]*[.,]?\d*)", text.replace("\xa0", " "))
    if not m:
        return None
    return float(m.group(1).replace(" ", "").replace(",", "."))


# ---------------------------------------------------------------------------
# Parsovanie kategorie (vypis produktov)
# ---------------------------------------------------------------------------
CODE_RE = re.compile(r"code=([A-Z0-9]+)")
URL_CODE_RE = re.compile(r"_(N\d+)\.htm|/(N\d+)\.htm")
AVAIL_RE = re.compile(
    r"K dispoz\w+|Na sklade|Skladom|Nedostupn\w+|Na objedn\w+|Vypredan\w+|Dostupn\w+",
    re.IGNORECASE,
)


def parse_category(html: str) -> list[dict]:
    """Vrati zoznam produktov najdenych vo vypise kategorie."""
    soup = BeautifulSoup(html, "html.parser")
    products = []
    for card in soup.select("div.product"):
        # odkaz na produkt
        a = card.select_one('a[href*="_N"]') or card.select_one(".product-image a[href]") \
            or card.select_one("h3 a[href]")
        href = a["href"] if a and a.has_attr("href") else ""
        url = href if href.startswith("http") else urljoin(BASE + "/", href)

        # kod produktu
        code = None
        m = CODE_RE.search(str(card))
        if m:
            code = m.group(1)
        else:
            m = URL_CODE_RE.search(href)
            if m:
                code = m.group(1) or m.group(2)
        if not code:
            continue  # bez kodu nevieme produkt zaradit

        name_el = card.select_one("h3")
        name = name_el.get_text(" ", strip=True) if name_el else ""

        gross = to_number(_text(card.select_one(".product-price")))
        netto = to_number(_text(card.select_one(".product-price-netto")))

        avail_el = card.select_one(".supply")
        avail = _text(avail_el)
        if not avail:
            m = AVAIL_RE.search(card.get_text(" ", strip=True))
            avail = m.group(0) if m else ""

        brand = ""
        b = card.select_one('a[href*="producer"]')
        if b and b.has_attr("href"):
            brand = b["href"].replace(BASE + "/", "").replace("https://www.dipol.sk/", "").split("--")[0]

        img = ""
        img_el = card.select_one(".product-image img") or card.select_one("img")
        if img_el:
            img = img_el.get("src") or img_el.get("data-src") or ""
            if img.startswith("//"):
                img = "https:" + img

        short_desc = _short_description(card, name, brand)

        products.append({
            "code": code,
            "name": name,
            "url": url,
            "gross": gross,
            "netto": netto,
            "availability": avail,
            "manufacturer": brand,
            "image": img,
            "shortDescription": short_desc,
            "images": [],
            "description": "",
        })
    return products


def _text(el):
    return el.get_text(" ", strip=True) if el else ""


# znaky/slova, ktorymi sa zvykne zacinat cast s cenou/kodom -> odtial popis orezeme
_CUT_MARKERS = re.compile(r"(K[oó]d\s*:|€|Do ko[sš][ií]ka|N[aá]h[lľ]ad|Cena\b)")


def _short_description(card, name: str, brand: str) -> str:
    """Vytiahne cisty kratky popis - bez nazvu, znacky, ceny a kodu."""
    # 1) skus samostatny odstavec v ramci .product-desc
    desc_el = card.select_one(".product-desc")
    text = ""
    if desc_el:
        p = desc_el.find("p")
        text = (p.get_text(" ", strip=True) if p else desc_el.get_text(" ", strip=True))
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text)
    # 2) odrez cast s cenou/kodom
    m = _CUT_MARKERS.search(text)
    if m:
        text = text[:m.start()]
    # 3) odstran znacku a nazov zo zaciatku
    for prefix in (brand, name):
        if prefix and text.lower().startswith(prefix.lower()):
            text = text[len(prefix):].strip(" -|:")
    return text.strip()


# ---------------------------------------------------------------------------
# Parsovanie detailu produktu (dlhy popis + galeria)
# ---------------------------------------------------------------------------
def parse_detail(html: str, code: str = "") -> dict:
    """Z detailu produktu vytiahne BOHATY podrobny popis (HTML s tabulkami)
    a plnovelke obrazky galerie.

    Overené na dipol.sk:
    - podrobny popis (text + tabulky parametrov) je v elemente .scrollspy
    - plnovelke obrazky (1200px, pre zoom) su v odkazoch a[data-lightbox]
      s URL /dimages/.../<kod>...jpg  (thumbnaily su len male nahlady)
    """
    soup = BeautifulSoup(html, "html.parser")
    out = {"description": "", "images": []}

    # podrobny popis ako HTML (zachova tabulky a formatovanie)
    block = soup.select_one(".scrollspy")
    if block is not None:
        for bad in block.select("script, style, noscript, iframe, form"):
            bad.decompose()
        for im in block.select("img"):
            src = im.get("src") or im.get("data-src") or ""
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                src = BASE + src
            im["src"] = src
            if im.has_attr("data-src"):
                del im["data-src"]
        for an in block.select("a"):
            href = an.get("href") or ""
            if href.startswith("/"):
                an["href"] = BASE + href
        # tabulky dipolu nemaju vlastne ramovanie -> doplnime inline,
        # aby boli citatelne aj bez dipol CSS
        for tb in block.select("table"):
            tb["style"] = "border-collapse:collapse;border:1px solid #ccc;width:100%;margin:8px 0;"
            tb["border"] = "1"
        for cell in block.select("td, th"):
            cell["style"] = "border:1px solid #ccc;padding:6px;"
        html_desc = block.decode_contents().replace("]]>", "]]&gt;")
        out["description"] = re.sub(r"\s+", " ", html_desc).strip()[:25000]

    # plnovelke obrazky galerie (a[data-lightbox] -> /dimages/...)
    code_l = (code or "").lower()
    imgs = []
    for a in soup.select("a[data-lightbox]"):
        href = a.get("href") or ""
        if "/dimages/" in href and (not code_l or code_l in href.lower()):
            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                href = BASE + href
            imgs.append(href)
    seen = set()
    out["images"] = [x for x in imgs if not (x in seen or seen.add(x))][:10]
    return out


# ---------------------------------------------------------------------------
# Generovanie Shoptet XML
# ---------------------------------------------------------------------------
def round_price(value: float, cfg: dict) -> str:
    dec = cfg["cena"]["zaokruhlit_na_desatiny"]
    return f"{round(value, dec):.{dec}f}"


def compute_price(p: dict, cfg: dict) -> str | None:
    """Vypocita finalnu PRICE hodnotu (string) podla config, alebo None ak chyba cena."""
    if p.get("gross") is None:
        return None
    cena = cfg["cena"]
    dec = cena["zaokruhlit_na_desatiny"]
    vat = float(cena["sadzba_dph"])
    s_dph = int(cena.get("ceny_su_s_dph", 1)) == 1
    base = p["gross"] / (1 + vat / 100) if s_dph else p["gross"]
    price = base * cena["marza_koeficient"]
    return f"{round(price, dec):.{dec}f}"


def build_shopitem(p: dict, cfg: dict) -> str | None:
    """Zostavi jeden <SHOPITEM> blok pre dany produkt. None ak chyba cena.

    Tato funkcia je jediny zdroj pravdy o tom, ako vyzera produkt v Shoptet feede
    - pouziva ju build_xml (cely feed) aj catalog_scraper (katalog pre picker)."""
    price_str = compute_price(p, cfg)
    if price_str is None:
        log.warning("Preskakujem %s (chyba cena)", p.get("code"))
        return None
    cena = cfg["cena"]

    item = ["  <SHOPITEM>"]
    item.append(f"    <CODE>{escape(p['code'])}</CODE>")
    item.append(f"    <NAME>{escape(p['name'])}</NAME>")
    if p.get("shortDescription"):
        item.append(f"    <SHORT_DESCRIPTION>{escape(p['shortDescription'])}</SHORT_DESCRIPTION>")
    if p.get("description"):
        # popis je HTML (tabulky/formatovanie) -> CDATA
        item.append(f"    <DESCRIPTION><![CDATA[{p['description']}]]></DESCRIPTION>")
    if p.get("manufacturer"):
        item.append(f"    <MANUFACTURER>{escape(p['manufacturer'])}</MANUFACTURER>")
    if p.get("categories"):
        item.append("    <CATEGORIES>")
        item.append(f"      <DEFAULT_CATEGORY>{escape(p['categories'][0])}</DEFAULT_CATEGORY>")
        item.append("    </CATEGORIES>")
    item.append(f"    <PRICE>{price_str}</PRICE>")
    item.append(f"    <CURRENCY>{escape(cena['mena'])}</CURRENCY>")
    if p.get("availability"):
        item.append(f"    <AVAILABILITY_IN_STOCK>{escape(p['availability'])}</AVAILABILITY_IN_STOCK>")
    images = p["images"] if p.get("images") else ([p["image"]] if p.get("image") else [])
    if images:
        item.append("    <IMAGES>")
        for src in images:
            item.append(f"      <IMAGE>{escape(src)}</IMAGE>")
        item.append("    </IMAGES>")
    item.append("  </SHOPITEM>")
    return "\n".join(item)


def wrap_shop(items_xml: list[str]) -> str:
    """Obali zoznam <SHOPITEM> blokov do <SHOP> koreňa s XML hlavickou."""
    parts = ['<?xml version="1.0" encoding="utf-8"?>', "<SHOP>"]
    parts.extend(items_xml)
    parts.append("</SHOP>")
    return "\n".join(parts) + "\n"


def build_xml(products: list[dict], cfg: dict) -> str:
    """Generuje XML v Shoptet schema (Relax NG) pre import/automaticky import.

    Pozn.: Shoptet element <PRICE> ocakava cenu BEZ DPH (e-shop si DPH pripocita
    podla svojho nastavenia). Ceny dipolu su s DPH, preto delime (1 + DPH/100).
    Povolena struktura: SHOP > SHOPITEM > CODE, NAME, SHORT_DESCRIPTION,
    DESCRIPTION, MANUFACTURER, CATEGORIES/DEFAULT_CATEGORY, PRICE, CURRENCY,
    IMAGES/IMAGE, AVAILABILITY_IN_STOCK.
    """
    items = [x for x in (build_shopitem(p, cfg) for p in products) if x]
    return wrap_shop(items)


# ---------------------------------------------------------------------------
# Hlavny beh
# ---------------------------------------------------------------------------
def run(cfg: dict, base_dir: Path):
    session = make_session(cfg)
    by_code: dict[str, dict] = {}

    for kat in cfg["kategorie"]:
        nazov = kat["nazov_shoptet"]
        url = kat["url"]
        sep = "&" if "?" in url else "?"
        list_url = f"{url}{sep}showall=1"
        log.info("Kategoria '%s' -> %s", nazov, list_url)

        html = fetch(session, list_url, cfg)
        if not html:
            continue
        found = parse_category(html)
        log.info("  najdenych produktov: %d", len(found))

        for prod in found:
            code = prod["code"]
            if code in by_code:
                # produkt uz mame z inej kategorie -> pridaj kategoriu
                if nazov not in by_code[code]["categories"]:
                    by_code[code]["categories"].append(nazov)
                continue
            prod["categories"] = [nazov]
            by_code[code] = prod
            time.sleep(cfg["siet"]["pauza_sekundy"])

    # detail produktu (dlhy popis + galeria)
    if cfg.get("nacitat_detail_produktu"):
        log.info("Nacitavam detaily %d produktov ...", len(by_code))
        for i, prod in enumerate(by_code.values(), 1):
            html = fetch(session, prod["url"], cfg)
            if html:
                d = parse_detail(html, prod["code"])
                prod["description"] = d["description"]
                if d["images"]:
                    prod["images"] = d["images"]
            if i % 25 == 0:
                log.info("  ... %d/%d", i, len(by_code))
            time.sleep(cfg["siet"]["pauza_sekundy"])

    products = list(by_code.values())
    xml = build_xml(products, cfg)

    out_path = base_dir / cfg["vystup_xml"]
    out_path.write_text(xml, encoding="utf-8")
    log.info("HOTOVO: %d produktov zapisanych do %s", len(products), out_path)


def main():
    ap = argparse.ArgumentParser(description="dipol.sk -> Shoptet XML feed")
    ap.add_argument("--config", default="config.json", help="cesta ku config.json")
    args = ap.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.is_absolute():
        cfg_path = Path(__file__).resolve().parent / cfg_path
    if not cfg_path.exists():
        log.error("Config subor neexistuje: %s", cfg_path)
        sys.exit(1)

    cfg = load_config(cfg_path)
    run(cfg, cfg_path.resolve().parent)


if __name__ == "__main__":
    main()
