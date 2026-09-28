#!/usr/bin/env python3
"""Validate and, when explicitly enabled, sync Kit stocks across warehouses."""

from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

API_BASE = "https://api.kit.yandex.net/v1"
SOURCE_FILE = Path(".work/supplier.yml")
KIT_ARTICLES_FILE = Path("kit_articles.txt")
WAREHOUSE_TITLES = {
    "main": "Основной склад",
    "vladivostok": "Владивосток",
    "moscow_10": "мск 10",
}
MOSCOW_SUPPLIER_LOCATION = "Москва"
VLADIVOSTOK_SUPPLIER_LOCATION = "Владивосток"
MIN_EXPENSIVE_PRICE = Decimal("10000")
PAGE_SIZE = 100
MAX_UPDATE_ITEMS = 5000
REQUEST_DELAY_SECONDS = 0.36


def normalize_article(value):
    value = str(value or "").strip()
    while len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1].strip()
    return value


def api_request(token, method, path, query=None, body=None):
    url = API_BASE + path
    if query:
        url += "?" + urllib.parse.urlencode(query, doseq=True)
    data = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "KitStockSync/1.0",
    }
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            payload = response.read()
            return json.loads(payload) if payload else None
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"Kit API returned HTTP {error.code}: {details}") from error


def load_kit_articles():
    if not KIT_ARTICLES_FILE.exists():
        raise FileNotFoundError(f"Missing {KIT_ARTICLES_FILE}")
    articles = {
        normalize_article(line)
        for line in KIT_ARTICLES_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    articles.discard("")
    if not articles:
        raise ValueError("Kit article list is empty")
    return articles


def load_supplier_rows():
    if not SOURCE_FILE.exists():
        raise FileNotFoundError(
            f"Missing {SOURCE_FILE}; run build_feed.py before this script"
        )
    rows = {}
    for _, element in ET.iterparse(SOURCE_FILE, events=("end",)):
        if element.tag != "offer":
            continue
        article = ""
        price = None
        moscow = 0
        vladivostok = 0
        for child in element:
            text = (child.text or "").strip()
            if child.tag == "param" and child.attrib.get("name") == "articul":
                article = normalize_article(text)
            elif child.tag == "price":
                try:
                    price = Decimal(text)
                except InvalidOperation:
                    price = None
            elif child.tag == "quantity":
                try:
                    quantity = max(0, int(float(text or "0")))
                except ValueError:
                    quantity = 0
                location = child.attrib.get("location")
                if location == MOSCOW_SUPPLIER_LOCATION:
                    moscow = quantity
                elif location == VLADIVOSTOK_SUPPLIER_LOCATION:
                    vladivostok = quantity
        if article:
            rows[article] = {
                "price": price,
                "moscow": moscow,
                "vladivostok": vladivostok,
            }
        element.clear()
    return rows


def get_warehouses(token):
    payload = api_request(
        token,
        "GET",
        "/warehouses",
        {"status": "ACTIVE", "page": 1, "per_page": 100},
    )
    warehouses = payload.get("warehouses", payload.get("items", []))
    by_title = {item["title"].strip(): item["id"] for item in warehouses}
    missing = [title for title in WAREHOUSE_TITLES.values() if title not in by_title]
    print("Активные склады Кита: " + ", ".join(sorted(by_title)))
    if missing:
        raise ValueError("Не найдены склады: " + ", ".join(missing))
    return {key: by_title[title] for key, title in WAREHOUSE_TITLES.items()}


def get_variants(token, wanted_articles):
    matched = {}
    duplicates = set()
    page = 1
    while True:
        payload = api_request(
            token,
            "GET",
            "/variants",
            {"page": page, "per_page": PAGE_SIZE},
        )
        variants = payload.get("variants", [])
        for variant in variants:
            article = normalize_article(variant.get("sku"))
            if article in wanted_articles:
                if article in matched and matched[article] != variant["id"]:
                    duplicates.add(article)
                else:
                    matched[article] = variant["id"]
        total = int(payload.get("total_count", 0))
        if page == 1:
            print(f"Товаров в API Кита: {total}")
        if not variants or page * PAGE_SIZE >= total:
            break
        page += 1
        if page % 20 == 0:
            print(f"Проверено страниц товаров: {page - 1}")
        time.sleep(REQUEST_DELAY_SECONDS)
    if duplicates:
        sample = ", ".join(sorted(duplicates)[:10])
        raise ValueError(f"Повторяющиеся артикулы в Ките ({len(duplicates)}): {sample}")
    return matched


def quantities_for(row):
    if not row or row["price"] is None:
        return {"main": 0, "vladivostok": 0, "moscow_10": 0}
    if row["price"] < MIN_EXPENSIVE_PRICE:
        return {"main": row["moscow"], "vladivostok": 0, "moscow_10": 0}
    if row["moscow"] > 0:
        return {"main": 0, "vladivostok": 0, "moscow_10": row["moscow"]}
    if row["vladivostok"] > 0:
        return {"main": 0, "vladivostok": row["vladivostok"], "moscow_10": 0}
    return {"main": 0, "vladivostok": 0, "moscow_10": 0}


def build_updates(articles, variants, supplier_rows, warehouse_ids):
    updates = []
    positive = {key: 0 for key in WAREHOUSE_TITLES}
    for article in sorted(articles):
        variant_id = variants.get(article)
        if not variant_id:
            continue
        quantities = quantities_for(supplier_rows.get(article))
        for warehouse_key, quantity in quantities.items():
            if quantity > 0:
                positive[warehouse_key] += 1
            updates.append({
                "quantity": quantity,
                "variant_id": variant_id,
                "warehouse_id": warehouse_ids[warehouse_key],
            })
    return updates, positive


def submit_updates(token, updates):
    for start in range(0, len(updates), MAX_UPDATE_ITEMS):
        chunk = updates[start:start + MAX_UPDATE_ITEMS]
        api_request(token, "POST", "/variants/stocks/bulk_update", body={"items": chunk})
        print(f"Обновлено строк остатков: {min(start + len(chunk), len(updates))}/{len(updates)}")
        if start + len(chunk) < len(updates):
            time.sleep(REQUEST_DELAY_SECONDS)


def main():
    token = os.environ.get("YANDEX_KIT_API_TOKEN", "").strip()
    if not token:
        raise RuntimeError("GitHub secret YANDEX_KIT_API_TOKEN is not available")
    live = os.environ.get("KIT_STOCK_SYNC_LIVE", "").strip().lower() == "true"
    articles = load_kit_articles()
    supplier_rows = load_supplier_rows()
    warehouse_ids = get_warehouses(token)
    variants = get_variants(token, articles)
    updates, positive = build_updates(articles, variants, supplier_rows, warehouse_ids)
    missing = articles - variants.keys()
    print(f"Артикулов в списке: {len(articles)}")
    print(f"Сопоставлено по sku: {len(variants)}")
    print(f"Не сопоставлено: {len(missing)}")
    if missing:
        print("Примеры несопоставленных: " + ", ".join(sorted(missing)[:20]))
    print(
        "Положительный остаток: "
        + "; ".join(
            f"{WAREHOUSE_TITLES[key]} — {positive[key]} товаров"
            for key in ("main", "vladivostok", "moscow_10")
        )
    )
    print(f"Подготовлено строк остатков: {len(updates)}")
    if len(variants) < max(1, int(len(articles) * 0.90)):
        raise RuntimeError("Сопоставлено меньше 90% артикулов; обновление остановлено")
    if not live:
        print("ПРОВЕРКА ЗАВЕРШЕНА: остатки не изменялись (KIT_STOCK_SYNC_LIVE=false)")
        return
    submit_updates(token, updates)
    print("БОЕВОЕ ОБНОВЛЕНИЕ ЗАВЕРШЕНО")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ОШИБКА: {error}", file=sys.stderr)
        raise
