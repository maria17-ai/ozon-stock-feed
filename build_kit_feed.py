#!/usr/bin/env python3
"""Build a full Yandex KIT YML feed from a KIT export and supplier YML.

Identity is matched by article. Prices and product names are kept from KIT.
Only Moscow and Vladivostok quantities are emitted; the local warehouse is
intentionally omitted. Supplier photos are normalized through an image proxy
to a 1200 x 1200 white canvas without cropping.
"""
from __future__ import annotations

import argparse
import csv
import io
import re
import urllib.parse
import urllib.request
import zipfile
from collections import OrderedDict
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

try:
    from kit_fallback_prices import FALLBACK_PRICES
except ImportError:
    FALLBACK_PRICES = {}


DEFAULT_SUPPLIER = "https://opt.1000size.ru/uploads/yml/94b6972bed7678c64bcd7de77f25d2b8e2810218/export.yml"


def col_index(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref).group(0)
    n = 0
    for c in letters:
        n = n * 26 + ord(c) - 64
    return n - 1


def read_xlsx(path: Path) -> list[dict[str, str]]:
    """Read the first worksheet directly (handles KIT's broken dimension)."""
    with zipfile.ZipFile(path) as zf:
        shared = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            shared = ["".join(si.itertext()) for si in root]
        sheets = sorted(n for n in zf.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
        if not sheets:
            raise ValueError("В XLSX не найден лист с товарами")
        data = zf.read(sheets[0])

    rows: list[list[str]] = []
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    for _, elem in ET.iterparse(io.BytesIO(data), events=("end",)):
        if elem.tag != ns + "row":
            continue
        vals: dict[int, str] = {}
        for cell in elem.findall(ns + "c"):
            idx = col_index(cell.attrib["r"])
            typ = cell.attrib.get("t")
            if typ == "inlineStr":
                node = cell.find(ns + "is")
                val = "" if node is None else "".join(node.itertext())
            else:
                node = cell.find(ns + "v")
                val = "" if node is None or node.text is None else node.text
                if typ == "s" and val:
                    val = shared[int(val)]
            vals[idx] = val
        width = max(vals, default=-1) + 1
        rows.append([vals.get(i, "") for i in range(width)])
        elem.clear()
    if len(rows) < 3:
        raise ValueError("В выгрузке KIT не найдена таблица товаров")
    headers = rows[1]
    result = []
    for row in rows[2:]:
        row += [""] * (len(headers) - len(row))
        item = dict(zip(headers, row))
        if item.get("KIT ID*") or item.get("ID") or item.get("Артикул"):
            result.append(item)
    return result


def norm_article(value: str) -> str:
    value = (value or "").strip()
    # Source files may already contain one or more pairs of quotes.
    # Internally keep the bare value; KIT output is quoted exactly once.
    while len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1].strip()
    return value.strip()


def download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "KIT-feed-builder/1.0"})
    with urllib.request.urlopen(req, timeout=180) as response:
        return response.read()


def parse_supplier(source: bytes | Path) -> dict[str, dict]:
    stream = io.BytesIO(source) if isinstance(source, bytes) else str(source)
    products: dict[str, dict] = {}
    for _, elem in ET.iterparse(stream, events=("end",)):
        if elem.tag.rsplit("}", 1)[-1] != "offer":
            continue
        params = []
        article = ""
        for p in elem.findall("param"):
            name = (p.attrib.get("name") or "").strip()
            value = (p.text or "").strip()
            if name.lower() in {"articul", "артикул"}:
                article = norm_article(value)
            elif name.lower() not in {"barcode", "штрихкод"} and value:
                params.append((name, p.attrib.get("group"), value))
        if not article:
            article = norm_article(elem.attrib.get("id", ""))
        quantities = {q.attrib.get("location", ""): int(float((q.text or "0").strip() or 0))
                      for q in elem.findall("quantity")}
        pictures = [(p.text or "").strip() for p in elem.findall("picture") if (p.text or "").strip()]
        products[article] = {
            "price": (elem.findtext("price") or "").strip(),
            "vendor": (elem.findtext("vendor") or "").strip(),
            "pictures": pictures,
            "params": params,
            "weight_gramm": (elem.findtext("weight-gramm") or "").strip(),
            "moscow": quantities.get("Москва", 0),
            "vladivostok": quantities.get("Владивосток", 0),
        }
        elem.clear()
    return products


def number(value: str, default: str = "0") -> str:
    value = (value or "").strip().replace(" ", "").replace(",", ".")
    try:
        return ("%.2f" % float(value)).rstrip("0").rstrip(".")
    except ValueError:
        return default


def kit_price(supplier_price: str, fallback_price: str) -> str:
    """Supplier price + max(18%, 450 RUB); preserve KIT price if unavailable."""
    raw = (supplier_price or "").strip().replace(" ", "").replace(",", ".")
    try:
        base = Decimal(raw)
        if base <= 0:
            raise InvalidOperation
        markup = max(base * Decimal("0.18"), Decimal("450"))
        return str((base + markup).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        return number(fallback_price, "1")


def proxy_picture(url: str) -> str:
    encoded = urllib.parse.quote(url, safe="")
    return f"https://wsrv.nl/?url={encoded}&w=1200&h=1200&fit=contain&bg=white&output=jpg&q=90"


def xtext(value) -> str:
    return escape(str(value or ""), {'"': "&quot;"})


def clean_description(value: str) -> str:
    """Remove service identifiers, links and image filenames from a description."""
    text = str(value or "").replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    # Technical identifiers are already stored in their own KIT fields.
    text = re.sub(r"(?i)(?:^|[\s.])(?:articul|артикул|barcode|штрихкод)\s*:\s*[^.\n]*\.?", " ", text)
    # Web addresses and local/remote image paths do not belong in customer text.
    text = re.sub(r"(?i)https?://[^\s)]+", "", text)
    text = re.sub(r"(?i)(?:[\w%+@.,~_-]+[/\\])*[\w%+@,~_-]+\.(?:jpe?g|png|gif|webp|avif)(?:\?[^\s]*)?\.?", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s+([,.;:])", r"\1", text)
    text = re.sub(r"(?:\.\s*){2,}", ". ", text)
    text = re.sub(r"\(\s*\)", "", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip(" .;\n\t")


def positive_weight(*values: str) -> int:
    for value in values:
        try:
            weight = int(round(float(str(value or "").strip().replace(" ", "").replace(",", "."))))
            if weight > 0:
                return weight
        except ValueError:
            pass
    return 3000


def ensure_description_weight(description: str, weight_gramm: int) -> str:
    # Zero is treated as an unspecified weight. Existing positive values remain intact.
    text = re.sub(r"(?i)\bве[сc]\s*:\s*0(?:[.,]0+)?\s*(?:г|гр|грамм(?:а|ов)?)?\b", f"Вес: {weight_gramm} г", description)
    if not re.search(r"(?i)\bве[сc]\s*:\s*[1-9]\d*(?:[.,]\d+)?", text):
        suffix = "." if text and not text.endswith((".", "!", "?")) else ""
        text = f"{text}{suffix} Вес: {weight_gramm} г".strip()
    return text


def build_categories(items: list[dict[str, str]]):
    nodes: OrderedDict[tuple[str, ...], int] = OrderedDict()
    for item in items:
        path = []
        for key in ("Категория 1-го уровня*", "Категория 2-го уровня", "Категория 3-го уровня"):
            val = (item.get(key) or "").strip()
            if val:
                path.append(val)
                nodes.setdefault(tuple(path), len(nodes) + 1)
    return nodes


def build_feed(items, supplier, output: Path, report: Path):
    cats = build_categories(items)
    matched = absent = normalized_photos = 0
    with output.open("w", encoding="utf-8", newline="\n") as fh, report.open("w", encoding="utf-8-sig", newline="") as rf:
        writer = csv.writer(rf, delimiter=";")
        writer.writerow(["Артикул", "ID KIT", "Найден у поставщика", "Москва", "Владивосток", "Фото", "Характеристики"])
        fh.write('<?xml version="1.0" encoding="UTF-8"?>\n<yml_catalog date="%s">\n<shop>\n' % datetime.now().strftime("%Y-%m-%d %H:%M"))
        fh.write("<name>ТД МК — обновление KIT</name><company>ТД МК</company><url>https://td-mk.ru/</url>\n")
        fh.write('<currencies><currency id="RUR" rate="1"/></currencies>\n<categories>\n')
        for path, cid in cats.items():
            parent = cats.get(path[:-1]) if len(path) > 1 else None
            parent_attr = f' parentId="{parent}"' if parent else ""
            fh.write(f'<category id="{cid}"{parent_attr}>{xtext(path[-1])}</category>\n')
        fh.write("</categories>\n<offers>\n")
        for item in items:
            article_raw = item.get("Артикул", "")
            article = norm_article(article_raw)
            kit_article = f'"{article}"'
            ext_id = (item.get("Внешний ID: YML") or article_raw or article).strip()
            src = supplier.get(article)
            if src:
                matched += 1
                moscow, vlad = src["moscow"], src["vladivostok"]
                pictures = [proxy_picture(x) for x in src["pictures"]]
                if not pictures:
                    pictures = [x.strip() for x in (item.get("Изображения и видео") or "").split(",") if x.strip()]
                params = src["params"]
                vendor = src["vendor"] or item.get("Бренд", "")
                weight = positive_weight(src.get("weight_gramm"), item.get("Вес с упаковкой, г"))
                normalized_photos += len(pictures)
            else:
                absent += 1
                moscow = vlad = 0
                pictures = [x.strip() for x in (item.get("Изображения и видео") or item.get("Ссылки на фото") or "").split(",") if x.strip()]
                params = []
                for key in ("Цвет", "Характеристика (Задайте название)", "Размер", "Пример характеристики"):
                    if (item.get(key) or "").strip():
                        params.append((key, None, item[key].strip()))
                vendor = item.get("Бренд", "")
                weight = positive_weight(item.get("Вес с упаковкой, г"))
            cat_path = tuple(v.strip() for v in [item.get("Категория 1-го уровня*", ""), item.get("Категория 2-го уровня", ""), item.get("Категория 3-го уровня", "")] if v.strip())
            export_price = item.get("Цена со скидкой, руб.") or item.get("Цена со скидкой") or item.get("Цена до скидки, руб.") or item.get("Цена")
            fallback_price = FALLBACK_PRICES.get(kit_article, export_price)
            # For matched products, update from supplier with an 18% markup,
            # but never less than 450 RUB. For absent products, preserve the
            # current KIT export price so it never disappears.
            price = kit_price(src.get("price", ""), fallback_price) if src else number(fallback_price, "1")
            fh.write(f'<offer id={quoteattr(ext_id)} available="true">\n')
            fh.write(f'<name>{xtext(item.get("Название товара*") or item.get("Название") or article)}</name><price>{price}</price><currencyId>RUR</currencyId>\n')
            if cat_path:
                fh.write(f'<categoryId>{cats[cat_path]}</categoryId>\n')
            for pic in pictures:
                fh.write(f'<picture>{xtext(pic)}</picture>\n')
            if vendor:
                fh.write(f'<vendor>{xtext(vendor)}</vendor>\n')
            # KIT uses the standard YML vendorCode field for cross-source
            # matching by its fixed "Артикул" parameter.
            # Diagnostic for KIT cross-source matching: KIT may map the YML
            # model field to its fixed "Артикул" field. Keep exactly the
            # original one-pair-of-quotes representation used in KIT.
            fh.write(f'<model>{xtext(kit_article)}</model>\n')
            desc = clean_description(item.get("Описание товара") or item.get("Описание", ""))
            desc = ensure_description_weight(desc, weight)
            if desc:
                fh.write(f'<description>{xtext(desc)}</description>\n')
            fh.write(f'<param name="articul">{xtext(kit_article)}</param>\n')
            for pname, group, pvalue in params:
                group_attr = f' group={quoteattr(group)}' if group else ""
                fh.write(f'<param name={quoteattr(pname)}{group_attr}>{xtext(pvalue)}</param>\n')
            fh.write(f'<quantity location="Основной склад">{moscow}</quantity><quantity location="Владивосток">{vlad}</quantity>\n</offer>\n')
            writer.writerow([article, item.get("KIT ID*") or item.get("ID", ""), "да" if src else "нет", moscow, vlad, len(pictures), len(params)])
        fh.write("</offers>\n</shop>\n</yml_catalog>\n")
    return {"kit": len(items), "matched": matched, "absent": absent, "supplier": len(supplier), "photos": normalized_photos}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kit", required=True, type=Path, help="Свежая XLSX-выгрузка товаров из KIT")
    ap.add_argument("--supplier", default=DEFAULT_SUPPLIER, help="URL или локальный supplier YML")
    ap.add_argument("--output", default="kit_feed.yml", type=Path)
    ap.add_argument("--report", default="kit_feed_report.csv", type=Path)
    args = ap.parse_args()
    items = read_xlsx(args.kit)
    supplier_source = download(args.supplier) if str(args.supplier).startswith(("http://", "https://")) else Path(args.supplier)
    supplier = parse_supplier(supplier_source)
    stats = build_feed(items, supplier, args.output, args.report)
    print("Готово:", stats)


if __name__ == "__main__":
    main()
