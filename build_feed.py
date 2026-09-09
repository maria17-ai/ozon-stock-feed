#!/usr/bin/env python3
"""Download the supplier feed and publish Ozon prices and Moscow stock."""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
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
MARKUP_RATE = Decimal("0.30")
MIN_MARKUP = Decimal("300")
FULFILMENT_COST = Decimal("246.20")
NET_REVENUE_RATE = Decimal("0.47")
PRICE_STEP = Decimal("10")


def download_source():
    WORK_DIR.mkdir(exist_ok=True)
    request = urllib.request.Request(
        SOURCE_URL,
        headers={"User-Agent": "OzonStockFeed/1.0"},
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        SOURCE_FILE.write_bytes(response.read())


def extract_supplier_data():
    offers = {}
    dealer_prices = {}
    standard_prices = {}
    for _, element in ET.iterparse(SOURCE_FILE, events=("end",)):
        if element.tag != "offer":
            continue

        article = ""
        quantity = 0
        dealer_price_text = ""
        standard_price_text = ""
        for child in element:
            if child.tag == "param" and child.attrib.get("name") == "articul":
                article = (child.text or "").strip()
            elif child.tag == "quantity" and child.attrib.get("location") == WAREHOUSE_NAME:
                try:
                    quantity = max(0, int(float((child.text or "0").strip())))
                except ValueError:
                    quantity = 0
            elif child.tag == "dealer_price":
                dealer_price_text = (child.text or "").strip()
            elif child.tag == "price":
                standard_price_text = (child.text or "").strip()

        if article:
            offers[article] = quantity
            for target, price_text in (
                (dealer_prices, dealer_price_text),
                (standard_prices, standard_price_text),
            ):
                try:
                    price = Decimal(price_text)
                except InvalidOperation:
                    continue
                if price > 0:
                    target[article] = price
        element.clear()
    return offers, dealer_prices, standard_prices


def extract_offers():
    """Backward-compatible quantity-only reader used by build_kit_feed.py."""
    offers, _, _ = extract_supplier_data()
    return offers


def ozon_price(dealer_price):
    """Calculate the Ozon FBS price and round it up to the nearest 10 rubles."""
    dealer_price = Decimal(dealer_price)
    markup = max(dealer_price * MARKUP_RATE, MIN_MARKUP)
    raw_price = (dealer_price + markup + FULFILMENT_COST) / NET_REVENUE_RATE
    return int(
        (raw_price / PRICE_STEP).quantize(
            Decimal("1"), rounding=ROUND_CEILING
        ) * PRICE_STEP
    )


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


def write_feed(offers, dealer_prices=None):
    PUBLIC_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    root = ET.Element("yml_catalog", {"date": timestamp})
    shop = ET.SubElement(root, "shop")
    ET.SubElement(shop, "name").text = "1000 размеров — цены и остатки Ozon"
    offers_element = ET.SubElement(shop, "offers")

    ozon_articles = load_ozon_articles()
    matched = 0
    zeroed = 0
    priced = 0
    dealer_prices = dealer_prices or {}
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
        if supplier_article in dealer_prices:
            ET.SubElement(offer, "price").text = str(
                ozon_price(dealer_prices[supplier_article])
            )
            priced += 1
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
<html lang=\"ru\"><meta charset=\"utf-8\"><title>Фид цен и остатков Ozon</title>
<body><h1>Фид цен и остатков Ozon</h1>
<p>Обновлено (UTC): {timestamp}</p>
<p>Артикулов Ozon: {len(ozon_articles)}; найдено у поставщика: {matched}; отсутствует у поставщика и обнулено: {zeroed}; обновлено цен: {priced}; с положительным остатком: {positive}</p>
<p><a href=\"ozon_stock_moscow.xml\">Открыть XML/YML-фид</a></p></body></html>
"""
    (PUBLIC_DIR / "index.html").write_text(index, encoding="utf-8")


if __name__ == "__main__":
    download_source()
    supplier_offers, dealer_prices, standard_prices = extract_supplier_data()
    write_feed(supplier_offers, dealer_prices)

    # Build the Yandex Kit feed from the same downloaded supplier snapshot.
    from build_kit_feed import write_feed as write_kit_feed

    write_kit_feed(supplier_offers, standard_prices)
