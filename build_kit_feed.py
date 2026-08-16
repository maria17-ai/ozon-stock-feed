#!/usr/bin/env python3
"""Publish Yandex Kit stock for every product linked to this YML feed."""

from datetime import datetime, timezone
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET

import build_feed

PUBLIC_DIR = Path("public")
KIT_ARTICLES_FILE = Path("kit_articles.txt")
OUTPUT_FILE = PUBLIC_DIR / "yandex_kit_stock.yml"
XML_OUTPUT_FILE = PUBLIC_DIR / "yandex_kit_stock.xml"


def load_kit_articles():
    if not KIT_ARTICLES_FILE.exists():
        raise FileNotFoundError(
            f"Required Yandex Kit YML ID list is missing: {KIT_ARTICLES_FILE}"
        )
    articles = {
        line.strip()
        for line in KIT_ARTICLES_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    if not articles:
        raise ValueError("Yandex Kit YML ID list is empty; feed was not published")
    return sorted(articles)


def supplier_article_from_kit(kit_article):
    """Remove only literal outer quotes used in the linked YML identifier."""
    if len(kit_article) >= 2 and kit_article[0] == kit_article[-1] == '"':
        return kit_article[1:-1]
    return kit_article


def write_feed(supplier_offers):
    PUBLIC_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    root = ET.Element("yml_catalog", {"date": timestamp})
    shop = ET.SubElement(root, "shop")
    ET.SubElement(shop, "name").text = "1000 размеров — остатки Яндекс Кит"
    offers_element = ET.SubElement(shop, "offers")

    kit_articles = load_kit_articles()
    matched = 0
    zeroed = 0
    positive = 0
    for kit_article in kit_articles:
        supplier_article = supplier_article_from_kit(kit_article)
        if supplier_article in supplier_offers:
            quantity = supplier_offers[supplier_article]
            matched += 1
        else:
            quantity = 0
            zeroed += 1
        if quantity > 0:
            positive += 1
        offer = ET.SubElement(offers_element, "offer", {"id": kit_article})
        ET.SubElement(offer, "count").text = str(quantity)

    ET.indent(root, space="  ")
    ET.ElementTree(root).write(OUTPUT_FILE, encoding="utf-8", xml_declaration=True)
    shutil.copyfile(OUTPUT_FILE, XML_OUTPUT_FILE)

    index = f"""<!doctype html>
<html lang=\"ru\"><meta charset=\"utf-8\"><title>Фиды остатков</title>
<body><h1>Автоматические фиды остатков</h1>
<p>Обновлено (UTC): {timestamp}</p>
<ul>
  <li><a href=\"ozon_stock_moscow.xml\">Ozon — склад Москва</a></li>
  <li><a href=\"yandex_kit_stock.xml\">Яндекс Кит — Основной склад</a></li>
</ul>
<p>Яндекс Кит: товаров {len(kit_articles)}; найдено у поставщика {matched}; отсутствует у поставщика и обнулено {zeroed}; положительный остаток {positive}.</p>
</body></html>
"""
    (PUBLIC_DIR / "index.html").write_text(index, encoding="utf-8")


if __name__ == "__main__":
    if not build_feed.SOURCE_FILE.exists():
        build_feed.download_source()
    write_feed(build_feed.extract_offers())
