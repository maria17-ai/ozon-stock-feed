#!/usr/bin/env python3
"""Download the supplier feed and publish Moscow stock in Ozon YML format."""

from datetime import datetime, timezone
from pathlib import Path
import shutil
import urllib.request
import xml.etree.ElementTree as ET

SOURCE_URL = (
    "https://opt.1000size.ru/uploads/yml/"
    "94b6972bed7678c64bcd7de77f25d2b8e2810218/export.yml"
)
WAREHOUSE_NAME = "Москва"
WORK_DIR = Path(".work")
PUBLIC_DIR = Path("public")
SOURCE_FILE = WORK_DIR / "supplier.yml"
OUTPUT_FILE = PUBLIC_DIR / "ozon_stock_moscow.yml"
XML_OUTPUT_FILE = PUBLIC_DIR / "ozon_stock_moscow.xml"
OZON_ARTICLES_FILE = Path("ozon_articles.txt")


def download_source():
    WORK_DIR.mkdir(exist_ok=True)
    request = urllib.request.Request(
        SOURCE_URL,
        headers={"User-Agent": "OzonStockFeed/1.0"},
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        SOURCE_FILE.write_bytes(response.read())


def extract_offers():
    offers = {}
    for _, element in ET.iterparse(SOURCE_FILE, events=("end",)):
        if element.tag != "offer":
            continue

        article = ""
        quantity = 0
        for child in element:
            if child.tag == "param" and child.attrib.get("name") == "articul":
                article = (child.text or "").strip()
            elif child.tag == "quantity" and child.attrib.get("location") == WAREHOUSE_NAME:
                try:
                    quantity = max(0, int(float((child.text or "0").strip())))
                except ValueError:
                    quantity = 0

        if article:
            offers[article] = quantity
        element.clear()
    return offers


def load_ozon_articles():
    if not OZON_ARTICLES_FILE.exists():
        raise FileNotFoundError(
            f"Required Ozon article list is missing: {OZON_ARTICLES_FILE}"
        )
    articles = {
        line.strip()
        for line in OZON_ARTICLES_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    if not articles:
        raise ValueError("Ozon article list is empty; refusing to publish an empty feed")
    return sorted(articles)


def supplier_article_from_ozon(ozon_article):
    """Remove only the literal outer quotes used in numeric Ozon seller SKUs."""
    if len(ozon_article) >= 2 and ozon_article[0] == ozon_article[-1] == '"':
        return ozon_article[1:-1]
    return ozon_article


def write_feed(offers):
    PUBLIC_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    root = ET.Element("yml_catalog", {"date": timestamp})
    shop = ET.SubElement(root, "shop")
    ET.SubElement(shop, "name").text = "1000 размеров — остатки Ozon"
    offers_element = ET.SubElement(shop, "offers")

    ozon_articles = load_ozon_articles()
    matched = 0
    zeroed = 0
    for ozon_article in ozon_articles:
        supplier_article = supplier_article_from_ozon(ozon_article)
        if supplier_article in offers:
            quantity = offers[supplier_article]
            matched += 1
        else:
            # Explicit zero applies only to the Moscow outlet in this offer.
            quantity = 0
            zeroed += 1
        offer = ET.SubElement(offers_element, "offer", {"id": ozon_article})
        outlets = ET.SubElement(offer, "outlets")
        ET.SubElement(
            outlets,
            "outlet",
            {"instock": str(quantity), "warehouse_name": WAREHOUSE_NAME},
        )

    ET.indent(root, space="  ")
    ET.ElementTree(root).write(OUTPUT_FILE, encoding="utf-8", xml_declaration=True)
    shutil.copyfile(OUTPUT_FILE, XML_OUTPUT_FILE)

    positive = sum(
        offers.get(supplier_article_from_ozon(article), 0) > 0
        for article in ozon_articles
    )
    index = f"""<!doctype html>
<html lang=\"ru\"><meta charset=\"utf-8\"><title>Ozon stock feed</title>
<body><h1>Фид остатков Ozon</h1>
<p>Обновлено (UTC): {timestamp}</p>
<p>Артикулов Ozon: {len(ozon_articles)}; найдено у поставщика: {matched}; отсутствует у поставщика и обнулено: {zeroed}; с положительным остатком: {positive}</p>
<p><a href=\"ozon_stock_moscow.xml\">Открыть XML/YML-фид</a></p></body></html>
"""
    (PUBLIC_DIR / "index.html").write_text(index, encoding="utf-8")


if __name__ == "__main__":
    download_source()
    supplier_offers = extract_offers()
    write_feed(supplier_offers)

    # Build the Yandex Kit feed from the same downloaded supplier snapshot.
    from build_kit_feed import write_feed as write_kit_feed

    write_kit_feed(supplier_offers)
