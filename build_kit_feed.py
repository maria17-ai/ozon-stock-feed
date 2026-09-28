#!/usr/bin/env python3
"""Publish Yandex Kit stock for every product linked to this YML feed."""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET

import build_feed

PUBLIC_DIR = Path("public")
KIT_ARTICLES_FILE = Path("kit_articles.txt")
OUTPUT_FILE = PUBLIC_DIR / "yandex_kit_stock.yml"
XML_OUTPUT_FILE = PUBLIC_DIR / "yandex_kit_stock.xml"
VLADIVOSTOK_OUTPUT_FILE = PUBLIC_DIR / "yandex_kit_stock_vladivostok.yml"
VLADIVOSTOK_XML_OUTPUT_FILE = PUBLIC_DIR / "yandex_kit_stock_vladivostok.xml"
NINO_OUTPUT_FILE = PUBLIC_DIR / "yandex_kit_stock_nino.yml"
NINO_XML_OUTPUT_FILE = PUBLIC_DIR / "yandex_kit_stock_nino.xml"
PRICE_RATE = Decimal("0.18")
MIN_MARKUP = Decimal("450")
VLADIVOSTOK_MIN_SUPPLIER_PRICE = Decimal("10000")
MOSCOW_KIT_LOCATION = "Основной склад"
VLADIVOSTOK_SUPPLIER_LOCATION = "Владивосток"
VLADIVOSTOK_KIT_LOCATION = "Владивосток"
LOW_PRICE_MOSCOW_KIT_LOCATION = "Склад №1нино"


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


def load_fallback_prices():
    try:
        from kit_fallback_prices import FALLBACK_PRICES
    except ImportError as error:
        raise FileNotFoundError(
            "Required Kit fallback price module is missing: kit_fallback_prices.py"
        ) from error
    return {
        article: Decimal(price_text)
        for article, price_text in FALLBACK_PRICES.items()
        if article and Decimal(price_text) > 0
    }


def supplier_article_from_kit(kit_article):
    """Remove only literal outer quotes used in the linked YML identifier."""
    if len(kit_article) >= 2 and kit_article[0] == kit_article[-1] == '"':
        return kit_article[1:-1]
    return kit_article


def extract_supplier_prices():
    prices = {}
    for _, element in ET.iterparse(build_feed.SOURCE_FILE, events=("end",)):
        if element.tag != "offer":
            continue
        article = ""
        price_text = ""
        for child in element:
            if child.tag == "param" and child.attrib.get("name") == "articul":
                article = (child.text or "").strip()
            elif child.tag == "price":
                price_text = (child.text or "").strip()
        if article and price_text:
            try:
                price = Decimal(price_text)
            except InvalidOperation:
                price = Decimal("0")
            if price > 0:
                prices[article] = price
        element.clear()
    return prices


def extract_supplier_vladivostok_stock():
    """Read Vladivostok stock from the same supplier snapshot as Moscow."""
    stock = {}
    for _, element in ET.iterparse(build_feed.SOURCE_FILE, events=("end",)):
        if element.tag != "offer":
            continue
        article = ""
        quantity = 0
        for child in element:
            if child.tag == "param" and child.attrib.get("name") == "articul":
                article = (child.text or "").strip()
            elif (
                child.tag == "quantity"
                and child.attrib.get("location") == VLADIVOSTOK_SUPPLIER_LOCATION
            ):
                try:
                    quantity = max(0, int(float((child.text or "0").strip())))
                except ValueError:
                    quantity = 0
        if article:
            stock[article] = quantity
        element.clear()
    return stock


def kit_price(supplier_price):
    markup = max(supplier_price * PRICE_RATE, MIN_MARKUP)
    return int(
        (supplier_price + markup).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )


def write_warehouse_feed(rows, quantity_key, output_file, xml_output_file, name, include_prices=False):
    """Write one Kit-compatible source containing stock for exactly one warehouse."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    root = ET.Element("yml_catalog", {"date": timestamp})
    shop = ET.SubElement(root, "shop")
    ET.SubElement(shop, "name").text = name
    offers_element = ET.SubElement(shop, "offers")
    for row in rows:
        offer = ET.SubElement(offers_element, "offer", {"id": row["article"]})
        ET.SubElement(offer, "count").text = str(row[quantity_key])
        if include_prices and row["price"] is not None:
            ET.SubElement(offer, "price").text = row["price"]
        ET.SubElement(offer, "vendorCode").text = row["article"]
        ET.SubElement(offer, "param", {"name": "articul"}).text = row["article"]
    ET.indent(root, space="  ")
    ET.ElementTree(root).write(output_file, encoding="utf-8", xml_declaration=True)
    shutil.copyfile(output_file, xml_output_file)


def write_feed(supplier_offers, supplier_prices=None):
    PUBLIC_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    kit_articles = load_kit_articles()
    fallback_prices = load_fallback_prices()
    if supplier_prices is None:
        supplier_prices = extract_supplier_prices()
    matched = 0
    zeroed = 0
    positive = 0
    priced = 0
    fallback_priced = 0
    rows = []
    for kit_article in kit_articles:
        supplier_article = supplier_article_from_kit(kit_article)
        if supplier_article in supplier_offers:
            # The single Kit YML source updates the Main warehouse only.
            # Always publish the supplier's Moscow stock here.
            moscow_quantity = supplier_offers[supplier_article]
            matched += 1
        else:
            moscow_quantity = 0
            zeroed += 1
        if moscow_quantity > 0:
            positive += 1
        price_text = None
        if supplier_article in supplier_prices:
            price = kit_price(supplier_prices[supplier_article])
            price_text = str(price)
            priced += 1
        elif kit_article in fallback_prices:
            price = fallback_prices[kit_article]
            price_text = format(price.normalize(), "f")
            fallback_priced += 1
        rows.append({
            "article": supplier_article,
            "moscow": moscow_quantity,
            "price": price_text,
        })

    # Kit accepts one YML source. It updates the Main warehouse with <count>.
    write_warehouse_feed(
        rows, "moscow", OUTPUT_FILE, XML_OUTPUT_FILE,
        "1000 размеров — Основной склад", include_prices=True,
    )

    index = f"""<!doctype html>
<html lang=\"ru\"><meta charset=\"utf-8\"><title>Фиды остатков</title>
<body><h1>Автоматические фиды остатков</h1>
<p>Обновлено (UTC): {timestamp}</p>
<ul>
  <li><a href=\"ozon_stock_moscow.xml\">Ozon — склад Москва</a></li>
  <li><a href=\"yandex_kit_stock.xml\">Яндекс Кит — Основной склад</a></li>
</ul>
<p>Яндекс Кит: товаров {len(kit_articles)}; найдено у поставщика {matched}; отсутствует у поставщика и обнулено на Основном складе {zeroed}; в Основной склад передаётся остаток поставщика Москва без ограничения по цене; обновлено цен с наценкой 18%, но не менее 450 ₽: {priced}; сохранено цен из выгрузки Кита: {fallback_priced}; положительный остаток {positive}.</p>
</body></html>
"""
    (PUBLIC_DIR / "index.html").write_text(index, encoding="utf-8")


if __name__ == "__main__":
    if not build_feed.SOURCE_FILE.exists():
        build_feed.download_source()
    write_feed(build_feed.extract_offers())
