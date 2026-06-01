#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
napad_scraper.py
================
Stiahne produkty z napad.pl (polsky velkoobchod zabezpecovacej techniky),
PRELOZI nazov + popis + tabulku parametrov z polstiny do slovenciny cez DeepL,
prepocita ceny PLN -> EUR a vygeneruje XML feed pre Shoptet.

Pouzitie:
    pip install -r requirements.txt
    export DEEPL_API_KEY="tvoj-kluc"          # alebo nastav v config / GitHub secret
    python napad_scraper.py

Vystup: napad_feed.xml (Shoptet schema, rovnaka ako dipol).

POZN.: napad.pl ma popisy v polstine -> preto preklad. Plny popis je v
#nav-description, tabulka parametrov v #nav-specification. Velke obrazky su
/duze/..._l_.jpg (thumbnaily /male/..._s_.jpg su len male nahlady).
"""

import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin
from xml.sax.saxutils import escape

import requests
from bs4 import BeautifulSoup

BASE = "https://www.napad.pl"
log = logging.getLogger("napad")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")


# ---------------------------------------------------------------------------
# Konfiguracia
# ---------------------------------------------------------------------------
def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# DeepL preklad (PL -> SK), HTML rezim zachova tagy/tabulky
# ---------------------------------------------------------------------------
class DeepL:
    def __init__(self, api_key: str):
        self.key = api_key
        # free kluce koncia na ":fx" a pouzivaju api-free, platene api.deepl.com
        self.url = ("https://api-free.deepl.com/v2/translate"
                    if api_key.endswith(":fx")
                    else "https://api.deepl.com/v2/translate")
        self.cache: dict[str, str] = {}

    def translate(self, text: str, tag_handling: str | None = None) -> str:
        if not text or not text.strip():
            return text
        if text in self.cache:
            return self.cache[text]
        data = {
            "auth_key": self.key,
            "text": text,
            "source_lang": "PL",
            "target_lang": "SK",
        }
        if tag_handling:
            data["tag_handling"] = tag_handling  # "html"
        for attempt in range(3):
            try:
                r = requests.post(self.url, data=data, timeout=30)
                r.raise_for_status()
                out = r.json()["translations"][0]["text"]
                self.cache[text] = out
                return out
            except requests.RequestException as exc:
                log.warning("DeepL pokus %d zlyhal: %s", attempt + 1, exc)
                time.sleep(2 * (attempt + 1))
        log.error("DeepL preklad zlyhal, vraciam original")
        return text


# ---------------------------------------------------------------------------
# Kurz PLN -> EUR (ECB), s fallbackom
# ---------------------------------------------------------------------------
def pln_to_eur_rate(fallback: float = 4.27) -> float:
    """Vrati kolko PLN je za 1 EUR (z ECB denneho kurzu)."""
    try:
        r = requests.get(
            "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml", timeout=20)
        m = re.search(r"currency='PLN'\s+rate='([\d.]+)'", r.text)
        if m:
            return float(m.group(1))
    except requests.RequestException:
        pass
    log.warning("Pouzivam fallback kurz %.2f PLN/EUR", fallback)
    return fallback


# ---------------------------------------------------------------------------
# Scraping napad.pl
# ---------------------------------------------------------------------------
def make_session(cfg: dict) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": cfg["siet"]["user_agent"],
                      "Accept-Language": "pl,sk;q=0.6"})
    return s


def num(text: str | None):
    if not text:
        return None
    m = re.search(r"(\d[\d\s]*[.,]?\d*)", text.replace("\xa0", " "))
    return float(m.group(1).replace(" ", "").replace(",", ".")) if m else None


def thumb_to_full(src: str) -> str:
    """/male/..._s_.jpg  ->  /duze/..._l_.jpg (plnovelky obrazok)."""
    if not src:
        return ""
    if src.startswith("//"):
        src = "https:" + src
    return src.replace("/male/", "/duze/").replace("_s_.jpg", "_l_.jpg")


def parse_category(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for card in soup.select("div.product"):
        a = card.select_one('a[href*="/produkty-"]')
        if not a:
            continue
        href = a.get("href", "")
        url = href if href.startswith("http") else urljoin(BASE + "/", href)
        name = (card.select_one("[class*=product-name], [class*=name]") or a)
        name_txt = re.sub(r"\s+", " ", name.get_text(" ", strip=True))
        name_txt = re.sub(r"\s*Kod:.*$", "", name_txt, flags=re.I).strip()
        code = re.split(r"\s+-\s+", name_txt)[0]
        code = re.sub(r"\s+\d?\.?\d*\s*mm.*$", "", code, flags=re.I)
        code = re.sub(r"\s+PL$", "", code).strip()
        price_el = card.select_one("[class*=price]")
        gross = num(price_el.get_text() if price_el else "")
        img_el = card.select_one("img")
        thumb = (img_el.get("src") or img_el.get("data-src") or "") if img_el else ""
        out.append({"code": code, "namePL": name_txt, "url": url,
                    "grossPLN": gross, "thumb": thumb})
    return out


def style_tables(soup_fragment: BeautifulSoup):
    for tb in soup_fragment.select("table"):
        tb["style"] = "border-collapse:collapse;border:1px solid #ccc;width:100%;margin:8px 0;"
        tb["border"] = "1"
    for cell in soup_fragment.select("td, th"):
        cell["style"] = "border:1px solid #ccc;padding:6px;"


def parse_detail(html: str) -> dict:
    """Vrati surovy (polsky) HTML popis a HTML tabulku specifikacie + obrazky."""
    soup = BeautifulSoup(html, "html.parser")
    out = {"descHtmlPL": "", "specHtmlPL": "", "images": []}

    desc = soup.select_one("#nav-description")
    if desc:
        for bad in desc.select("script, style, noscript, iframe, form"):
            bad.decompose()
        for im in desc.select("img"):
            s = im.get("src") or im.get("data-src") or ""
            if s.startswith("//"):
                s = "https:" + s
            elif s.startswith("/"):
                s = BASE + s
            im["src"] = s
            if im.has_attr("data-src"):
                del im["data-src"]
        out["descHtmlPL"] = desc.decode_contents()

    spec = soup.select_one("#nav-specification")
    if spec:
        style_tables(spec)
        out["specHtmlPL"] = spec.decode_contents()

    # plnovelke obrazky z galerie (a[data-lightbox] -> /duze/..._l_.jpg)
    imgs = []
    for a in soup.select('a[data-lightbox], a[href*="/duze/"]'):
        h = a.get("href") or ""
        if "/duze/" in h:
            imgs.append("https:" + h if h.startswith("//") else h)
    if not imgs:  # fallback z hlavneho obrazka
        mi = soup.select_one('img[src*="/produkty/"]')
        if mi:
            imgs.append(thumb_to_full(mi.get("src") or ""))
    seen = set()
    out["images"] = [x for x in imgs if not (x in seen or seen.add(x))][:10]
    return out


# ---------------------------------------------------------------------------
# Shoptet XML
# ---------------------------------------------------------------------------
def build_xml(products: list[dict], cfg: dict) -> str:
    cena = cfg["cena"]
    parts = ['<?xml version="1.0" encoding="utf-8"?>', "<SHOP>"]
    for p in products:
        if p.get("priceEUR") is None:
            continue
        item = ["  <SHOPITEM>"]
        item.append(f"    <CODE>{escape(p['code'])}</CODE>")
        item.append(f"    <NAME>{escape(p['nameSK'])}</NAME>")
        if p.get("shortSK"):
            item.append(f"    <SHORT_DESCRIPTION>{escape(p['shortSK'])}</SHORT_DESCRIPTION>")
        if p.get("descSK"):
            item.append(f"    <DESCRIPTION><![CDATA[{p['descSK']}]]></DESCRIPTION>")
        if p.get("manufacturer"):
            item.append(f"    <MANUFACTURER>{escape(p['manufacturer'])}</MANUFACTURER>")
        item.append("    <CATEGORIES>")
        item.append(f"      <DEFAULT_CATEGORY>{escape(p['category'])}</DEFAULT_CATEGORY>")
        item.append("    </CATEGORIES>")
        item.append(f"    <PRICE>{p['priceEUR']:.2f}</PRICE>")
        item.append(f"    <CURRENCY>{escape(cena['mena'])}</CURRENCY>")
        if p.get("images"):
            item.append("    <IMAGES>")
            for s in p["images"]:
                item.append(f"      <IMAGE>{escape(s)}</IMAGE>")
            item.append("    </IMAGES>")
        item.append("  </SHOPITEM>")
        parts.append("\n".join(item))
    parts.append("</SHOP>")
    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# Hlavny beh
# ---------------------------------------------------------------------------
def run(cfg: dict, base_dir: Path):
    api_key = os.environ.get("DEEPL_API_KEY") or cfg.get("deepl_api_key", "")
    if not api_key:
        log.error("Chyba DEEPL_API_KEY (env premenna alebo config). Bez neho nemozem prekladat.")
        sys.exit(1)
    dl = DeepL(api_key)
    sess = make_session(cfg)
    rate = pln_to_eur_rate(cfg["cena"].get("fallback_kurz", 4.27))
    log.info("Kurz: %.3f PLN/EUR", rate)
    marza = cfg["cena"]["marza_koeficient"]
    limit = cfg.get("limit_produktov", 0)  # 0 = bez limitu (na test napr. 10)

    by_code: dict[str, dict] = {}
    for kat in cfg["kategorie"]:
        url = kat["url"]
        sep = "&" if "?" in url else "?"
        html = sess.get(f"{url}{sep}showall=1", timeout=cfg["siet"]["timeout_sekundy"]).text
        found = parse_category(html)
        log.info("Kategoria '%s': %d produktov", kat["nazov_shoptet"], len(found))
        for prod in found:
            if prod["code"] in by_code:
                continue
            prod["category"] = kat["nazov_shoptet"]
            by_code[prod["code"]] = prod
            if limit and len(by_code) >= limit:
                break
        if limit and len(by_code) >= limit:
            break

    products = list(by_code.values())
    log.info("Spracovavam %d produktov (detail + preklad) ...", len(products))
    for i, p in enumerate(products, 1):
        try:
            dhtml = sess.get(p["url"], timeout=cfg["siet"]["timeout_sekundy"]).text
            d = parse_detail(dhtml)
            # preklad PL -> SK
            p["nameSK"] = dl.translate(p["namePL"])
            desc_pl = d["descHtmlPL"]
            spec_pl = d["specHtmlPL"]
            desc_sk = dl.translate(desc_pl, tag_handling="html") if desc_pl else ""
            spec_sk = dl.translate(spec_pl, tag_handling="html") if spec_pl else ""
            p["descSK"] = (desc_sk + ("<h3>Špecifikácia</h3>" + spec_sk if spec_sk else "")).replace("]]>", "]]&gt;")
            p["shortSK"] = re.sub("<[^>]+>", " ", desc_sk)[:250].strip()
            p["images"] = d["images"]
            p["priceEUR"] = round(p["grossPLN"] / rate * marza, 2) if p["grossPLN"] else None
            # znacka z kodu
            p["manufacturer"] = ("IPOX" if p["code"].startswith("PX") else
                                 "Hikvision" if p["code"].startswith("DS") else
                                 "Dahua" if p["code"].startswith("IPC") else "")
        except Exception as exc:  # noqa
            log.warning("Chyba pri %s: %s", p["code"], exc)
        if i % 10 == 0:
            log.info("  ... %d/%d", i, len(products))
        time.sleep(cfg["siet"]["pauza_sekundy"])

    xml = build_xml(products, cfg)
    out_path = base_dir / cfg["vystup_xml"]
    out_path.write_text(xml, encoding="utf-8")
    log.info("HOTOVO: %d produktov -> %s", len(products), out_path)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="napad_config.json")
    args = ap.parse_args()
    cfg_path = Path(args.config)
    if not cfg_path.is_absolute():
        cfg_path = Path(__file__).resolve().parent / cfg_path
    cfg = load_config(cfg_path)
    run(cfg, cfg_path.resolve().parent)


if __name__ == "__main__":
    main()
