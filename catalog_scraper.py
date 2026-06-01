#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
catalog_scraper.py
==================
Pre kazdy oddiel dipol.sk (zo suboru picker_config.json) vyscrapuje produkty
a ulozi KATALOG vo formate JSON, ktory nacita HTML picker (picker.html).

Pre kazdy produkt katalog obsahuje:
  - lahke zobrazovacie polia (kod, nazov, thumb, cena, kategoria)
  - hotovy <SHOPITEM> XML blok (plny popis s tabulkami, plnovelke obrazky,
    znacka) - presne v rovnakom tvare ako nasadeny dipol feed

Picker potom z vybranych (zaskrtnutych) produktov len pospaja ich shopitem_xml
do <SHOP> ... </SHOP> a vznikne Shoptet feed len s vybranymi produktmi.

Pouzitie:
    pip install -r requirements.txt
    python catalog_scraper.py                       # picker_config.json
    python catalog_scraper.py --config moj.json

Vystup:
    catalog_<slug>.json   pre kazdu kategoriu
    catalog_index.json    zoznam kategorii pre dropdown v pickeri
"""

import argparse
import json
import re
import sys
import time
import unicodedata
from pathlib import Path

# znovupouzitie overenej logiky z dipol scraperu
import dipol_scraper as ds


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or "kategoria"


def thumb_of(prod: dict) -> str:
    """Maly nahlad pre tabulku v pickeri (rychle nacitanie)."""
    if prod.get("image"):
        return prod["image"]
    if prod.get("images"):
        return prod["images"][0]
    return ""


def scrape_category(session, kat: dict, cfg: dict) -> list[dict]:
    nazov = kat["nazov_shoptet"]
    url = kat["url"]
    sep = "&" if "?" in url else "?"
    list_url = f"{url}{sep}showall=1"
    ds.log.info("Kategoria '%s' -> %s", nazov, list_url)

    html = ds.fetch(session, list_url, cfg)
    if not html:
        return []
    found = ds.parse_category(html)
    ds.log.info("  najdenych produktov: %d", len(found))

    limit = cfg.get("limit_produktov")
    if limit:
        found = found[:int(limit)]
        ds.log.info("  limit_produktov=%s -> spracujem %d", limit, len(found))

    items = []
    for i, prod in enumerate(found, 1):
        prod["categories"] = [nazov]
        # detail: plny popis + plnovelke obrazky
        if cfg.get("nacitat_detail_produktu", True):
            dhtml = ds.fetch(session, prod["url"], cfg)
            if dhtml:
                d = ds.parse_detail(dhtml, prod["code"])
                prod["description"] = d["description"]
                if d["images"]:
                    prod["images"] = d["images"]
        shopitem = ds.build_shopitem(prod, cfg)
        if not shopitem:
            continue  # bez ceny produkt preskocime
        items.append({
            "code": prod["code"],
            "name": prod["name"],
            "thumb": thumb_of(prod),
            "price": ds.compute_price(prod, cfg),
            "currency": cfg["cena"]["mena"],
            "category": nazov,
            "url": prod["url"],
            "shopitem_xml": shopitem,
        })
        if i % 25 == 0:
            ds.log.info("  ... %d/%d", i, len(found))
        time.sleep(cfg["siet"]["pauza_sekundy"])
    return items


def run(cfg: dict, base_dir: Path):
    session = ds.make_session(cfg)
    index = []
    for kat in cfg["kategorie"]:
        nazov = kat["nazov_shoptet"]
        slug = kat.get("slug") or slugify(nazov)
        items = scrape_category(session, kat, cfg)
        fname = f"catalog_{slug}.json"
        (base_dir / fname).write_text(
            json.dumps({"category": nazov, "slug": slug, "products": items},
                       ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        ds.log.info("ulozene %s (%d produktov)", fname, len(items))
        index.append({"nazov": nazov, "slug": slug, "subor": fname, "pocet": len(items)})

    (base_dir / "catalog_index.json").write_text(
        json.dumps({"kategorie": index}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    ds.log.info("HOTOVO: %d kategorii, index v catalog_index.json", len(index))


def main():
    ap = argparse.ArgumentParser(description="dipol.sk -> katalog JSON pre picker")
    ap.add_argument("--config", default="picker_config.json")
    args = ap.parse_args()
    cfg_path = Path(args.config)
    if not cfg_path.is_absolute():
        cfg_path = Path(__file__).resolve().parent / cfg_path
    if not cfg_path.exists():
        ds.log.error("Config subor neexistuje: %s", cfg_path)
        sys.exit(1)
    cfg = ds.load_config(cfg_path)
    run(cfg, cfg_path.resolve().parent)


if __name__ == "__main__":
    main()
