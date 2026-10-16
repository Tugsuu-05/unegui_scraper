"""unegui.mn дээрх нэг марк, загварын нийтийн заруудыг бүгдийг нь татаж, медианаар зээлийн дээд дүн гаргана.

Жагсаалтын хуудсыг энгийн HTTP хүсэлтээр уншина. Хамгаалалт тойрохгүй.
Хүсэлт хооронд завсар авна. Нэг загварын хамгийн ихдээ 40 хуудас.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse
from collections import defaultdict
from datetime import date
from pathlib import Path
from statistics import median

BASE_URL = "https://www.unegui.mn/avto-mashin/-avtomashin-zarna/"
ORIGIN = "https://www.unegui.mn"
CAR_CATEGORY_SLUG = "-avtomashin-zarna"
DEFAULT_OUT = Path(__file__).resolve().parent / "data" / "car_listings.csv"
DEFAULT_GROUPS = Path(__file__).resolve().parent / "data" / "price_groups.csv"
REQUEST_DELAY_SEC = 2.0
MAX_SEARCH_PAGES = 40
MIN_SAMPLE = 8
DEFAULT_LTV = 0.65
MIN_PRICE_MNT = 1_000_000
MAX_PRICE_MNT = 3_000_000_000
MILEAGE_BANDS = (
    (0, 50_000, "0-50000"),
    (50_000, 100_000, "50000-100000"),
    (100_000, 150_000, "100000-150000"),
    (150_000, 200_000, "150000-200000"),
    (200_000, 300_000, "200000-300000"),
    (300_000, 10**9, "300000+"),
)
FUELS = ("хайбрид", "бензин", "дизель", "цахилгаан", "газ")
GEARS = ("автомат", "механик", "вариатор", "робот")
VAGUE_MODELS = {"бусад", "other", "бусад загвар"}

LISTING_FIELDS = [
    "listing_id",
    "brand",
    "model",
    "manufacture_year",
    "listing_price_mnt",
    "previous_price_mnt",
    "mileage_km",
    "engine_liter",
    "fuel_type",
    "transmission",
    "location",
    "source_url",
    "collected_date",
    "negotiable",
    "usable_for_average",
    "notes",
]


class SearchError(Exception):
    def __init__(self, message: str, options: list[str] | None = None):
        super().__init__(message)
        self.options = options or []


def slugify(text: str) -> str:
    lowered = (text or "").strip().casefold().replace("&", " ")
    return re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")


def listing_title(html: str) -> str:
    match = re.search(
        r'data-component="ListingTitle"[^>]*>\s*<span[^>]*>\s*([^<]+)',
        html,
    )
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).strip()


def max_page(html: str, page_url: str) -> int:
    path = urlparse(page_url).path.rstrip("/")
    numbers = [int(item) for item in re.findall(re.escape(path) + r"/?\?page=(\d+)", html)]
    return max(numbers) if numbers else 1


def split_heading(title: str, brand: str) -> tuple[str, str]:
    heading = re.sub(r"\s*зарна\s*$", "", title, flags=re.IGNORECASE).strip()
    if brand and heading.casefold().startswith(brand.casefold()):
        return heading[: len(brand)], heading[len(brand) :].strip(" -")
    return brand.strip(), heading


def parse_categories(html: str) -> list[dict]:
    pattern = re.compile(
        r'data-marker="listing-category-slug:([^"]+)"[^>]*href="([^"]+)"[^>]*>\s*([^<]+)'
    )
    found: dict[str, dict] = {}
    for _marker, href, name in pattern.findall(html):
        clean_href = href.split("?")[0]
        if not clean_href.endswith("/"):
            clean_href += "/"
        clean_name = re.sub(r"\s+", " ", name).replace("\xa0", " ").strip()
        parts = [part for part in clean_href.split("/") if part]
        if clean_name:
            found[clean_href] = {"name": clean_name, "href": clean_href, "parts": parts}
    return list(found.values())


def _pick_category(items: list[dict], query: str, label: str) -> dict:
    exact = [item for item in items if _norm(item["name"]) == _norm(query)]
    if len(exact) == 1:
        return exact[0]
    partial = [item for item in items if _norm(query) and _norm(query) in _norm(item["name"])]
    if len(exact) > 1:
        raise SearchError(f"{label} хэд хэдэн ангилалд таарч байна.", [item["name"] for item in exact])
    if len(partial) == 1:
        return partial[0]
    if partial:
        raise SearchError(
            f"{label} хэд хэдэн ангилалд таарч байна. Нэгийг нь яг бичнэ үү.",
            [item["name"] for item in partial],
        )
    raise SearchError(f"{label} олдсонгүй.", [item["name"] for item in items[:30]])


def resolve_search(brand: str, model: str) -> dict:
    brand_slug = slugify(brand)
    model_slug = slugify(model)
    if not brand_slug or not model_slug:
        raise SearchError("Марк ба загварыг сайтын латин нэрээр бичнэ үү. Жишээ: Toyota Harrier")
    direct = f"{ORIGIN}/avto-mashin/{CAR_CATEGORY_SLUG}/{brand_slug}/{model_slug}/"
    try:
        html = fetch_html(direct)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        html = ""
    title = listing_title(html) if html else ""
    if title and _norm(model) in _norm(title):
        brand_name, model_name = split_heading(title, brand)
        return {
            "brand": brand_name or brand.strip(),
            "model": model_name or model.strip(),
            "url": direct,
            "html": html,
        }
    return _resolve_from_catalog(brand, model)


def _resolve_from_catalog(brand: str, model: str) -> dict:
    brands = [
        item for item in parse_categories(fetch_html(BASE_URL))
        if item["parts"][:2] == ["avto-mashin", CAR_CATEGORY_SLUG] and len(item["parts"]) == 3
    ]
    brand_item = _pick_category(brands, brand, "Марк")
    models = [
        item for item in parse_categories(fetch_html(ORIGIN + brand_item["href"]))
        if len(item["parts"]) == 4 and item["parts"][2] == brand_item["parts"][2]
    ]
    model_item = _pick_category(models, model, "Загвар")
    html = fetch_html(ORIGIN + model_item["href"])
    return {
        "brand": brand_item["name"],
        "model": model_item["name"],
        "url": ORIGIN + model_item["href"],
        "html": html,
    }


def _matches_search(row: dict, brand: str, model: str) -> bool:
    if _norm(row.get("brand") or "") != _norm(brand) and _norm(brand) not in _norm(row.get("brand") or ""):
        return False
    row_model = _norm(row.get("model") or "")
    wanted = _norm(model)
    return row_model == wanted or wanted in row_model or row_model in wanted


def fetch_html(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; car-price-study/1.0)",
            "Accept-Language": "mn",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", "replace")


def js_unescape(value: str) -> str:
    out: list[str] = []
    index = 0
    simple = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\", "/": "/"}
    while index < len(value):
        if value[index] == "\\" and index + 1 < len(value):
            nxt = value[index + 1]
            if nxt == "u" and index + 6 <= len(value):
                hexpart = value[index + 2 : index + 6]
                try:
                    out.append(chr(int(hexpart, 16)))
                    index += 6
                    continue
                except ValueError:
                    pass
            out.append(simple.get(nxt, nxt))
            index += 2
            continue
        out.append(value[index])
        index += 1
    return "".join(out)


def _match_json_array(text: str) -> str | None:
    if not text.startswith("["):
        return None
    depth = 0
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return text[: index + 1]
    return None


def extract_adverts(html: str) -> list[dict]:
    needle = '\\"adverts\\":['
    start = 0
    while True:
        found = html.find(needle, start)
        if found < 0:
            return []
        raw = html[found + len('\\"adverts\\":') :]
        array_text = _match_json_array(js_unescape(raw))
        start = found + len(needle)
        if not array_text:
            continue
        try:
            data = json.loads(array_text)
        except json.JSONDecodeError:
            continue
        if data and isinstance(data[0], dict) and "title" in data[0]:
            return data
    return []


def _to_int(value) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(float(text.replace(",", "").replace(" ", "")))
    except ValueError:
        return None


def parse_display_price(text: str) -> int | None:
    if not text:
        return None
    match = re.search(r"([\d]+(?:[.,]\d+)?)\s*(сая|тэрбум)?", text, re.IGNORECASE)
    if not match:
        return None
    number = float(match.group(1).replace(",", "."))
    unit = (match.group(2) or "").lower()
    if unit == "сая":
        number *= 1_000_000
    elif unit == "тэрбум":
        number *= 1_000_000_000
    return int(round(number))


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).casefold()


def mileage_band(km: int | None) -> str:
    if km is None:
        return ""
    for low, high, label in MILEAGE_BANDS:
        if low <= km < high:
            return label
    return ""


def mileage_note(km: int | None, year: int | None, today_year: int) -> str:
    if km is None:
        return "гүйлт байхгүй"
    if km > 1_000_000:
        return "гүйлт хэт өндөр"
    if km < 500 and year is not None and today_year - year >= 3:
        return "гүйлт хэт бага, магадгүй буруу нэгж"
    return ""


def _features(ad: dict) -> tuple[int | None, str, str, str]:
    mileage = None
    engine = ""
    fuel = ""
    gear = ""
    for feature in ad.get("features") or []:
        text = str(feature.get("text") or "").strip()
        lowered = text.casefold()
        km_match = re.search(r"([\d\s,]+)\s*км", lowered)
        if km_match and mileage is None:
            mileage = _to_int(km_match.group(1))
            continue
        liter = re.search(r"(\d+(?:[.,]\d+)?)\s*л\b", lowered)
        if liter and not engine:
            engine = liter.group(1).replace(",", ".")
            continue
        if not fuel:
            for name in FUELS:
                if name in lowered:
                    fuel = text
                    break
            if fuel:
                continue
        if not gear:
            for name in GEARS:
                if name in lowered:
                    gear = text
                    break
    return mileage, engine, fuel, gear


def _brand(ad: dict) -> str:
    ancestors = (ad.get("rubric") or {}).get("ancestors") or []
    for ancestor in ancestors:
        slug = ancestor.get("slug") or ""
        if slug in {"avto-mashin", CAR_CATEGORY_SLUG}:
            continue
        name = (ancestor.get("name") or "").strip()
        if name:
            return name
    return ""


def _is_car_for_sale(ad: dict) -> bool:
    ancestors = (ad.get("rubric") or {}).get("ancestors") or []
    return any((item.get("slug") or "") == CAR_CATEGORY_SLUG for item in ancestors)


def _model_name(title: str, brand: str) -> str:
    head = title.split(",")[0].strip()
    if brand and head.casefold().startswith(brand.casefold()):
        head = head[len(brand) :].strip(" -")
    return head


def _year(title: str) -> int | None:
    match = re.search(r"(\d{4})\s*/\s*(\d{4})", title)
    if match:
        return int(match.group(1))
    match = re.search(r"(19|20)\d{2}", title)
    if match:
        return int(match.group(0))
    return None


def listing_from_advert(ad: dict, collected_on: str | None = None) -> dict | None:
    if not _is_car_for_sale(ad):
        return None
    title = str(ad.get("title") or "").strip()
    brand = _brand(ad)
    model = _model_name(title, brand)
    year = _year(title)
    price = _to_int(ad.get("price_without_currency"))
    if price is None:
        price = parse_display_price(str(ad.get("price") or ""))
    mileage, engine, fuel, gear = _features(ad)
    path = str(ad.get("url") or "")
    if path.startswith("http"):
        source = path
    elif path.startswith("/"):
        source = ORIGIN + path
    else:
        source = ""
    row = {
        "listing_id": str(ad.get("id") or ""),
        "brand": brand,
        "model": model,
        "manufacture_year": "" if year is None else str(year),
        "listing_price_mnt": "" if price is None else str(price),
        "previous_price_mnt": "",
        "mileage_km": "" if mileage is None else str(mileage),
        "engine_liter": engine,
        "fuel_type": fuel,
        "transmission": gear,
        "location": str(ad.get("location") or ""),
        "source_url": source,
        "collected_date": collected_on or date.today().isoformat(),
        "negotiable": "тийм" if "тохирно" in str(ad.get("price_description") or "").casefold() else "үгүй",
        "usable_for_average": "",
        "notes": "",
    }
    previous = parse_display_price(str(ad.get("start_price") or ""))
    if previous:
        row["previous_price_mnt"] = str(previous)
    return apply_usability(row)


def apply_usability(row: dict, today_year: int | None = None) -> dict:
    today_year = today_year or date.today().year
    brand = (row.get("brand") or "").strip()
    model = (row.get("model") or "").strip()
    year = _to_int(row.get("manufacture_year"))
    price = _to_int(row.get("listing_price_mnt"))
    mileage = _to_int(row.get("mileage_km"))
    reasons: list[str] = []
    if not brand or not model:
        reasons.append("марк эсвэл загвар байхгүй")
    if _norm(model) in VAGUE_MODELS:
        reasons.append("загвар тодорхойгүй")
    if year is None:
        reasons.append("он байхгүй")
    if price is None or price < MIN_PRICE_MNT or price > MAX_PRICE_MNT:
        reasons.append("үнэ тохирохгүй")
    km_note = mileage_note(mileage, year, today_year)
    if km_note:
        reasons.append(km_note)
    row["usable_for_average"] = "үгүй" if reasons else "тийм"
    row["notes"] = "; ".join(reasons)
    return row


def search_output_paths(brand: str, model: str) -> tuple[Path, Path]:
    slug = slugify(f"{brand}-{model}") or "search"
    folder = Path(__file__).resolve().parent / "data"
    return folder / f"{slug}_listings.csv", folder / f"{slug}_groups.csv"


def collect_search(
    found: dict,
    page_limit: int | None = None,
    delay_sec: float = REQUEST_DELAY_SEC,
) -> list[dict]:
    base_url = found["url"]
    if not base_url.endswith("/"):
        base_url += "/"
    last = max_page(found["html"], base_url)
    if page_limit is not None:
        last = min(last, page_limit)
    capped = last > MAX_SEARCH_PAGES
    last = min(last, MAX_SEARCH_PAGES)
    print(f"{found['brand']} {found['model']}: {last} хуудас татна. {base_url}")
    if capped:
        print(f"Хуудас {MAX_SEARCH_PAGES}-оос цааш татахгүй.")
    collected: dict[str, dict] = {}
    collected_on = date.today().isoformat()
    skipped = 0
    for page in range(1, last + 1):
        if page == 1:
            html = found["html"]
            url = base_url
        else:
            url = f"{base_url}?page={page}"
            print(f"Хуудас {page}: {url}")
            html = fetch_html(url)
        adverts = extract_adverts(html)
        if not adverts:
            print("Энэ хуудсаас зар олдсонгүй. Энд зогслоо.")
            break
        kept = 0
        for advert in adverts:
            row = listing_from_advert(advert, collected_on)
            if row is None or not _matches_search(row, found["brand"], found["model"]):
                skipped += 1
                continue
            if row["listing_id"]:
                collected[row["listing_id"]] = row
                kept += 1
        print(f"  хуудас {page}: {kept} зар")
        if page < last:
            time.sleep(delay_sec)
    print(f"Өөр машин эсвэл автомашин биш мөр алгассан: {skipped}")
    return list(collected.values())


def write_listings(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=LISTING_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def read_listings(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return [apply_usability(row) for row in rows]


def _usable_number(row: dict) -> tuple[int, int, int] | None:
    if row.get("usable_for_average") != "тийм":
        return None
    year = _to_int(row.get("manufacture_year"))
    price = _to_int(row.get("listing_price_mnt"))
    mileage = _to_int(row.get("mileage_km"))
    if year is None or price is None or mileage is None:
        return None
    return year, price, mileage


def build_groups(rows: list[dict], min_sample: int = MIN_SAMPLE, ltv: float = DEFAULT_LTV) -> list[dict]:
    buckets: dict[tuple, list[int]] = defaultdict(list)
    labels: dict[tuple, tuple[str, str, str]] = {}
    for row in rows:
        parsed = _usable_number(row)
        if parsed is None:
            continue
        year, price, mileage = parsed
        key = (
            _norm(row.get("brand") or ""),
            _norm(row.get("model") or ""),
            year,
            mileage_band(mileage),
            _norm(row.get("fuel_type") or ""),
        )
        buckets[key].append(price)
        labels[key] = (row.get("brand") or "", row.get("model") or "", row.get("fuel_type") or "")
    groups = []
    for key, prices in buckets.items():
        brand, model, fuel = labels[key]
        mid = int(round(median(prices)))
        ready = len(prices) >= min_sample
        ceiling = int(round(mid * ltv / 100_000) * 100_000) if ready else ""
        groups.append({
            "brand": brand,
            "model": model,
            "manufacture_year": key[2],
            "mileage_band": key[3],
            "fuel_type": fuel,
            "sample_size": len(prices),
            "median_price_mnt": mid,
            "loan_ceiling_mnt": ceiling,
            "loan_ready": "тийм" if ready else "үгүй",
        })
    groups.sort(key=lambda item: (-item["sample_size"], item["brand"], item["model"]))
    return groups


def write_groups(path: Path, groups: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "brand",
        "model",
        "manufacture_year",
        "mileage_band",
        "fuel_type",
        "sample_size",
        "median_price_mnt",
        "loan_ceiling_mnt",
        "loan_ready",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(groups)


def _filter_name(rows: list[dict], field: str, query: str) -> tuple[list[dict], list[str]]:
    exact = [row for row in rows if _norm(row.get(field) or "") == _norm(query)]
    if exact:
        return exact, []
    partial = [row for row in rows if _norm(query) in _norm(row.get(field) or "")]
    names = sorted({row.get(field) or "" for row in partial})
    if len(names) == 1:
        return partial, []
    return [], names


def quote_loan(
    rows: list[dict],
    brand: str,
    model: str,
    year: int,
    km: int,
    fuel: str = "",
    listing_price: int | None = None,
    min_sample: int = MIN_SAMPLE,
    ltv: float = DEFAULT_LTV,
) -> dict:
    note = mileage_note(km, year, date.today().year)
    if note:
        return {"status": "rejected", "reason": note}
    brand_rows, brand_options = _filter_name(rows, "brand", brand)
    if not brand_rows:
        return {
            "status": "rejected",
            "reason": "марк олдсонгүй",
            "options": brand_options,
        }
    model_rows, model_options = _filter_name(brand_rows, "model", model)
    if not model_rows:
        return {
            "status": "rejected",
            "reason": "загвар олдсонгүй эсвэл хэд хэдэн загварт таарч байна",
            "options": model_options,
        }
    band = mileage_band(km)
    same_band = []
    for row in model_rows:
        parsed = _usable_number(row)
        if parsed is None:
            continue
        row_year, price, row_km = parsed
        if row_year == year and mileage_band(row_km) == band:
            if fuel and _norm(row.get("fuel_type") or "") != _norm(fuel):
                continue
            same_band.append((row, price))
    if fuel == "":
        fuels = sorted({(row.get("fuel_type") or "тодорхойгүй") for row, _ in same_band})
        if len(fuels) > 1:
            ready = []
            for name in fuels:
                subset = [(row, price) for row, price in same_band if (row.get("fuel_type") or "тодорхойгүй") == name]
                if len(subset) >= min_sample:
                    ready.append(name)
            if len(ready) != 1:
                return {
                    "status": "need_fuel",
                    "reason": "түлшээ заана уу",
                    "options": fuels,
                    "band": band,
                    "year": year,
                }
            fuel_name = ready[0]
            same_band = [
                (row, price)
                for row, price in same_band
                if (row.get("fuel_type") or "тодорхойгүй") == fuel_name
            ]
    prices = [price for _, price in same_band]
    if len(prices) < min_sample:
        nearby = defaultdict(int)
        for row in model_rows:
            parsed = _usable_number(row)
            if parsed is None:
                continue
            row_year, _, row_km = parsed
            if mileage_band(row_km) == band:
                nearby[row_year] += 1
        return {
            "status": "insufficient",
            "reason": f"энэ бүлэгт {len(prices)} зар байна, хамгийн багадаа {min_sample} хэрэгтэй",
            "band": band,
            "sample_size": len(prices),
            "nearby_years": dict(sorted(nearby.items())),
        }
    mid = int(round(median(prices)))
    base = mid if listing_price is None else min(listing_price, mid)
    ceiling = int(round(base * ltv / 100_000) * 100_000)
    used_fuel = ""
    if same_band:
        used_fuel = same_band[0][0].get("fuel_type") or ""
    return {
        "status": "ok",
        "brand": same_band[0][0].get("brand"),
        "model": same_band[0][0].get("model"),
        "year": year,
        "band": band,
        "fuel_type": used_fuel,
        "sample_size": len(prices),
        "median_price_mnt": mid,
        "base_price_mnt": base,
        "listing_price_mnt": listing_price,
        "ltv": ltv,
        "loan_ceiling_mnt": ceiling,
    }


def _money(value) -> str:
    if value == "" or value is None:
        return "-"
    return f"{int(value):,} төгрөг".replace(",", " ")


def cmd_collect(args: argparse.Namespace) -> int:
    try:
        found = resolve_search(args.brand, args.model)
        limit = args.pages if args.pages and args.pages > 0 else None
        rows = collect_search(found, limit)
    except SearchError as error:
        print(error)
        if error.options:
            print("Боломжит утга: " + ", ".join(error.options[:30]))
        return 1
    except urllib.error.HTTPError as error:
        print(f"Сайт хүсэлтийг буцаалаа: {error.code}. Дараагийн оролдлогоо түр хүлээгээд дахин ажиллуул.")
        return 1
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        print(f"Татаж чадсангүй: {error}")
        return 1
    if not rows:
        print("Хадгалах зар олдсонгүй.")
        return 1
    default_out, default_groups = search_output_paths(found["brand"], found["model"])
    out = Path(args.out) if args.out else default_out
    args.groups = args.groups or str(default_groups)
    write_listings(out, rows)
    usable = sum(1 for row in rows if row["usable_for_average"] == "тийм")
    print(f"Нийт {len(rows)} зар, дундажид орно {usable}. Файл: {out}")
    groups = build_groups(rows, args.min_sample, args.ltv)
    groups_path = Path(args.groups)
    write_groups(groups_path, groups)
    ready = sum(1 for group in groups if group["loan_ready"] == "тийм")
    print(f"Үнийн бүлэг {len(groups)}, зээл олгоход хүрэлцээтэй {ready}. Файл: {groups_path}")
    return 0


def cmd_summarize(args: argparse.Namespace) -> int:
    path = Path(args.csv)
    if not path.exists():
        print(f"Файл алга: {path}")
        return 1
    rows = read_listings(path)
    groups = build_groups(rows, args.min_sample, args.ltv)
    write_groups(Path(args.groups), groups)
    usable = sum(1 for row in rows if row.get("usable_for_average") == "тийм")
    print(f"Мөр {len(rows)}, дундажид орно {usable}, бүлэг {len(groups)}")
    for group in groups[:15]:
        mark = "зээл нээлттэй" if group["loan_ready"] == "тийм" else "хүрэлцэхгүй"
        print(
            f"{group['brand']} {group['model']} {group['manufacture_year']} "
            f"{group['mileage_band']} км {group['fuel_type'] or '-'} | "
            f"{group['sample_size']} зар | медиан {_money(group['median_price_mnt'])} | "
            f"зээл {_money(group['loan_ceiling_mnt'])} | {mark}"
        )
    print(f"Бүтэн жагсаалт: {args.groups}")
    return 0


def cmd_quote(args: argparse.Namespace) -> int:
    path = Path(args.csv)
    if not path.exists():
        print(f"Файл алга: {path}. Эхлээд collect ажиллуулна уу.")
        return 1
    result = quote_loan(
        read_listings(path),
        brand=args.brand,
        model=args.model,
        year=args.year,
        km=args.km,
        fuel=args.fuel,
        listing_price=args.price,
        min_sample=args.min_sample,
        ltv=args.ltv,
    )
    status = result["status"]
    if status == "ok":
        print(
            f"{result['brand']} {result['model']}, {result['year']}, "
            f"{result['band']} км, {result['fuel_type'] or 'түлш тодорхойгүй'}"
        )
        print(f"Жишээ зар: {result['sample_size']}")
        print(f"Медиан үнэ: {_money(result['median_price_mnt'])}")
        if result["listing_price_mnt"] is not None:
            print(f"Зарын үнэ: {_money(result['listing_price_mnt'])}")
            print(f"Суурь үнэ (медиан ба зарын үнийн бага нь): {_money(result['base_price_mnt'])}")
        print(f"Зээлийн хувь: {result['ltv']:.0%}")
        print(f"Олгох дээд дүн: {_money(result['loan_ceiling_mnt'])}")
        return 0
    print(result["reason"])
    options = result.get("options") or []
    if options:
        print("Боломжит утга: " + ", ".join(options[:12]))
    nearby = result.get("nearby_years") or {}
    if nearby:
        text = ", ".join(f"{year}: {count}" for year, count in nearby.items())
        print(f"Энэ гүйлтийн завсарт бусад он: {text}")
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="unegui.mn автомашины үнэ ба зээлийн дээд дүн")
    sub = parser.add_subparsers(dest="command", required=True)

    collect = sub.add_parser("collect", help="нэг марк, загварын бүх зарыг татах")
    collect.add_argument("--brand", required=True, help="марк, жишээ нь Toyota")
    collect.add_argument("--model", required=True, help="загвар, жишээ нь Harrier")
    collect.add_argument("--pages", type=int, default=0, help="0 бол тухайн загварын бүх хуудас")
    collect.add_argument("--out", default="")
    collect.add_argument("--groups", default="")
    collect.add_argument("--min-sample", type=int, default=MIN_SAMPLE)
    collect.add_argument("--ltv", type=float, default=DEFAULT_LTV)
    collect.set_defaults(func=cmd_collect)

    summary = sub.add_parser("summarize", help="CSV-ээс медиан ба зээлийн дээд дүн гаргах")
    summary.add_argument("--csv", default=str(DEFAULT_OUT))
    summary.add_argument("--groups", default=str(DEFAULT_GROUPS))
    summary.add_argument("--min-sample", type=int, default=MIN_SAMPLE)
    summary.add_argument("--ltv", type=float, default=DEFAULT_LTV)
    summary.set_defaults(func=cmd_summarize)

    quote = sub.add_parser("quote", help="нэг машины зээлийн дээд дүн")
    quote.add_argument("--csv", default=str(DEFAULT_OUT))
    quote.add_argument("--brand", required=True)
    quote.add_argument("--model", required=True)
    quote.add_argument("--year", type=int, required=True)
    quote.add_argument("--km", type=int, required=True)
    quote.add_argument("--fuel", default="")
    quote.add_argument("--price", type=int, default=None, help="зарын үнэ, төгрөг")
    quote.add_argument("--min-sample", type=int, default=MIN_SAMPLE)
    quote.add_argument("--ltv", type=float, default=DEFAULT_LTV)
    quote.set_defaults(func=cmd_quote)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
