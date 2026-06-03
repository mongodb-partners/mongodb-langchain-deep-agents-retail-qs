#!/usr/bin/env python3
"""Generate the retail-assistant seed corpus from Open Food Facts (OFF).

Why
---
The demo shipped a hand-authored catalog of 25 products. With so few products,
most recipes the LLM proposes reference ingredients that are not in the catalog,
so it cannot price them or add them to the cart. This script rebuilds the catalog
with ~1000+ realistic US grocery products pulled from the Open Food Facts Search
API, then regenerates the *product-referencing* parts of the rest of the seed
corpus (promotions, orders, knowledge graph, knowledge base) so the whole demo
stays internally consistent and the existing tests keep passing.

Data source
-----------
OFF Search API (https://world.openfoodfacts.org/api/v2/search), filtered
server-side to ``countries_tags_en=United States`` + a category, sorted by
popularity (``unique_scans_n``), requesting only the handful of fields we need.
We deliberately do NOT use the full ``openfoodfacts-mongodbdump.gz`` (~10 GB
compressed / 50+ GB restored / 3.7M mostly-European products) — it would require
the same filtering afterwards anyway.

Determinism
-----------
A single seeded ``random.Random`` drives every synthetic value (price, sale,
stock, order composition). Raw API responses are cached under
``scripts/.cache/off/`` so reruns are reproducible and work offline. No
wall-clock is read (dates are hardcoded) so the seeders stay idempotent.

Run
---
    python scripts/generate_retail_catalog.py
"""
from __future__ import annotations

import json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# Paths & constants
# --------------------------------------------------------------------------- #
REPO = Path(__file__).resolve().parent.parent
SEEDS = REPO / "examples" / "retail_assistant" / "seeds"
OPS = SEEDS / "operational"
CACHE = Path(__file__).resolve().parent / ".cache" / "off"

SEED = 530530
TARGET_TOTAL = 1000
MAX_PAGES = 3            # OFF pages (page_size=100) to pull per category filter
PAGE_SIZE = 100
REQUEST_DELAY_S = 0.3    # politeness between live API calls

UA = (
    "mongodb-langchain-deep-agents-retail-qs/1.0 "
    "(retail catalog seed generator; +https://github.com/mongodb-partners)"
)
OFF_FIELDS = (
    "code,product_name,product_name_en,brands,categories_tags,"
    "quantity,nutriments,unique_scans_n"
)

# Domain-neutrality guard (mirrors tests/unit/test_ingestion_seed.py).
BAN = re.compile(r"\b(telco|gep|finance|healthcare|phi)\b", re.IGNORECASE)

SMALL_WORDS = {"of", "and", "the", "with", "in", "a", "an", "for", "to", "on", "no"}
TAG_STOP = SMALL_WORDS | {"made", "from", "each", "pack", "value", "size", "free"}

# Distinctive non-English tokens — OFF's US filter still returns imports with
# foreign-language names. Reject those to keep the catalog US-grocery-realistic.
# Conservative list (low collision with English food names), matched whole-word.
FOREIGN = {
    "de", "du", "des", "avec", "sans", "vierge", "huile", "riz", "porc", "poulet",
    "boeuf", "lait", "fromage", "pain", "sucre", "oeuf", "oeufs", "pates", "beurre",
    "farine", "poivre", "biologique", "fraise", "pomme", "jus",
    "con", "sin", "leche", "queso", "aceite", "arroz", "pollo", "cerdo", "azucar",
    "huevo", "huevos", "fresa", "zumo",
    "olio", "riso", "formaggio", "pane", "zucchero", "uova", "latte",
    "mit", "milch", "brot", "zucker", "mehl", "salz",
}

rng = random.Random(SEED)


# --------------------------------------------------------------------------- #
# Category plan — one entry per (category, aisle). Drives OFF queries, aisle &
# price assignment. Caps sum well above TARGET_TOTAL to absorb filter/dedupe loss.
# --------------------------------------------------------------------------- #
CATEGORY_PLAN: list[dict[str, Any]] = [
    {"category": "Produce", "aisle": "Produce",
     "filters": ["Fruits", "Vegetables", "Fresh-vegetables"], "price": (0.50, 6.0), "cap": 150},
    {"category": "Produce", "aisle": "Fresh Herbs",
     "filters": ["Herbs", "Fresh-herbs"], "price": (1.0, 4.0), "cap": 20},
    {"category": "Meat & Seafood", "aisle": "Meat Counter",
     "filters": ["Meats", "Poultry", "Prepared-meats"], "price": (3.0, 15.0), "cap": 90},
    {"category": "Meat & Seafood", "aisle": "Seafood Counter",
     "filters": ["Seafood", "Fishes", "Canned-fishes"], "price": (4.0, 15.0), "cap": 50},
    {"category": "Dairy", "aisle": "Dairy",
     "filters": ["Milks", "Yogurts", "Creams", "Plant-based-milk-alternatives"], "price": (1.5, 9.0), "cap": 90},
    {"category": "Dairy", "aisle": "Specialty Cheese",
     "filters": ["Cheeses"], "price": (2.0, 12.0), "cap": 70},
    {"category": "Dairy", "aisle": "Eggs",
     "filters": ["Eggs"], "price": (2.0, 7.0), "cap": 15},
    {"category": "Bakery", "aisle": "Bread",
     "filters": ["Breads"], "price": (1.5, 8.0), "cap": 60},
    {"category": "Bakery", "aisle": "Bakery",
     "filters": ["Bakery-products", "Viennoiseries", "Pastries"], "price": (1.5, 8.0), "cap": 50},
    {"category": "Pantry", "aisle": "Pasta & Sauces",
     "filters": ["Pastas", "Pasta-sauces", "Sauces"], "price": (0.99, 9.0), "cap": 110},
    {"category": "Pantry", "aisle": "Canned Goods",
     "filters": ["Canned-foods", "Canned-vegetables", "Legumes", "Canned-beans"], "price": (0.99, 6.0), "cap": 80},
    {"category": "Pantry", "aisle": "Cooking Oils",
     "filters": ["Vegetable-oils", "Olive-oils"], "price": (3.0, 15.0), "cap": 30},
    {"category": "Pantry", "aisle": "Rice & Grains",
     "filters": ["Rice", "Cereals-and-their-products", "Grains"], "price": (0.99, 9.0), "cap": 60},
    {"category": "Beverages", "aisle": "Water & Sparkling",
     "filters": ["Waters", "Sparkling-waters"], "price": (0.99, 6.0), "cap": 40},
    {"category": "Beverages", "aisle": "Refrigerated Beverages",
     "filters": ["Fruit-juices", "Juices"], "price": (0.99, 8.0), "cap": 60},
    {"category": "Beverages", "aisle": "Sodas",
     "filters": ["Sodas", "Carbonated-drinks"], "price": (0.99, 7.0), "cap": 50},
    {"category": "Beverages", "aisle": "Coffee & Tea",
     "filters": ["Coffees", "Teas"], "price": (2.0, 12.0), "cap": 50},
    {"category": "Frozen", "aisle": "Frozen Foods",
     "filters": ["Frozen-foods", "Frozen-desserts", "Ice-creams"], "price": (2.0, 11.0), "cap": 70},
    {"category": "Snacks & Candy", "aisle": "Snacks",
     "filters": ["Snacks", "Salty-snacks", "Chips-and-fries", "Crackers"], "price": (1.5, 7.0), "cap": 80},
    {"category": "Snacks & Candy", "aisle": "Candy",
     "filters": ["Chocolates", "Confectioneries", "Candies"], "price": (1.5, 8.0), "cap": 60},
    {"category": "Condiments & Sauces", "aisle": "Condiments",
     "filters": ["Condiments", "Mustards", "Ketchup", "Salad-dressings", "Spreads"], "price": (1.5, 8.0), "cap": 70},
    {"category": "Baking & Spices", "aisle": "Baking",
     "filters": ["Flours", "Sugars", "Baking-ingredients"], "price": (0.99, 9.0), "cap": 50},
    {"category": "Baking & Spices", "aisle": "Spices",
     "filters": ["Spices", "Salts", "Condiments-and-sauces"], "price": (1.0, 9.0), "cap": 40},
    {"category": "Breakfast & Cereal", "aisle": "Breakfast",
     "filters": ["Breakfast-cereals", "Cereals"], "price": (2.0, 8.0), "cap": 70},
    {"category": "Breakfast & Cereal", "aisle": "Cereal",
     "filters": ["Granolas", "Mueslis", "Jams"], "price": (2.0, 8.0), "cap": 40},
]


# --------------------------------------------------------------------------- #
# Curated anchor staples — guaranteed clean recipe ingredients with STABLE ids.
# Promotions / orders / KB / KG reference these by ``key``. ``nut`` is
# (calories, protein_g, carbs_g, fat_g). ``sale`` is the sale price or None.
# --------------------------------------------------------------------------- #
def _anchor(key, name, category, aisle, brand, unit, price, sale, nut, tags):
    return {"key": key, "name": name, "category": category, "aisle": aisle,
            "brand": brand, "unit": unit, "price": price, "sale": sale,
            "nut": nut, "tags": tags}


ANCHORS: list[dict[str, Any]] = [
    _anchor("spaghetti", "Barilla Spaghetti", "Pantry", "Pasta & Sauces", "Barilla",
            "16 oz box", 1.49, None, (357, 12, 72, 1.5),
            ["pasta", "italian", "recipe-bolognese", "dinner"]),
    _anchor("marinara", "Rao's Homemade Marinara Sauce", "Pantry", "Pasta & Sauces", "Rao's",
            "24 oz jar", 5.99, 3.99, (70, 2, 9, 4),
            ["sauce", "marinara", "tomato", "italian", "recipe-bolognese"]),
    _anchor("ground_beef", "Signature Farms Ground Beef 80/20", "Meat & Seafood", "Meat Counter",
            "Signature Farms", "1 lb", 5.99, 4.99, (254, 17, 0, 20),
            ["beef", "ground beef", "meat", "protein", "recipe-bolognese"]),
    _anchor("italian_sausage", "Johnsonville Italian Sausage", "Meat & Seafood", "Meat Counter",
            "Johnsonville", "19 oz", 6.49, None, (290, 14, 3, 25),
            ["sausage", "pork", "italian", "recipe-bolognese"]),
    _anchor("parmesan", "BelGioioso Grated Parmesan Cheese", "Dairy", "Specialty Cheese",
            "BelGioioso", "5 oz", 4.99, None, (420, 38, 4, 28),
            ["cheese", "parmesan", "italian", "recipe-bolognese"]),
    _anchor("olive_oil", "Bertolli Extra Virgin Olive Oil", "Pantry", "Cooking Oils", "Bertolli",
            "16.9 fl oz", 8.99, 6.99, (884, 0, 0, 100),
            ["oil", "olive oil", "cooking", "recipe-bolognese"]),
    _anchor("canned_tomatoes", "Hunt's Whole Peeled Tomatoes", "Pantry", "Canned Goods", "Hunt's",
            "28 oz can", 1.99, None, (21, 1, 4, 0),
            ["tomato", "canned", "recipe-bolognese"]),
    _anchor("garlic", "Fresh Garlic Bulb", "Produce", "Produce", "Signature Farms", "each",
            0.69, None, (149, 6, 33, 0.5),
            ["garlic", "aromatics", "fresh", "produce", "recipe-bolognese"]),
    _anchor("yellow_onion", "Yellow Onion", "Produce", "Produce", "Signature Farms", "each",
            0.89, None, (40, 1, 9, 0),
            ["onion", "aromatics", "fresh", "produce", "recipe-bolognese"]),
    _anchor("italian_bread", "Signature Select Italian Bread", "Bakery", "Bread",
            "Signature Select", "16 oz loaf", 2.49, None, (260, 9, 50, 2),
            ["bread", "bakery", "italian", "recipe-bolognese"]),
    _anchor("baby_spinach", "O Organics Baby Spinach", "Produce", "Produce", "O Organics",
            "5 oz", 2.99, 1.99, (23, 3, 4, 0),
            ["spinach", "greens", "salad", "organic", "produce"]),
    _anchor("chicken_breast", "Open Nature Boneless Chicken Breast", "Meat & Seafood", "Meat Counter",
            "Open Nature", "1 lb", 6.49, 4.99, (165, 31, 0, 4),
            ["chicken", "poultry", "protein", "recipe-chicken"]),
    _anchor("russet_potato", "Signature Farms Russet Potatoes", "Produce", "Produce",
            "Signature Farms", "5 lb bag", 4.49, None, (79, 2, 18, 0),
            ["potato", "produce", "recipe-chicken"]),
    _anchor("roma_tomatoes", "O Organics Roma Tomatoes", "Produce", "Produce", "O Organics",
            "1 lb", 2.29, None, (18, 1, 4, 0),
            ["tomato", "produce", "fresh"]),
    _anchor("salmon", "Waterfront Bistro Atlantic Salmon Fillet", "Meat & Seafood", "Seafood Counter",
            "Waterfront Bistro", "1 lb", 9.99, 7.99, (208, 20, 0, 13),
            ["salmon", "fish", "seafood", "protein"]),
    _anchor("eggs", "Lucerne Grade A Large Eggs", "Dairy", "Eggs", "Lucerne", "dozen",
            3.29, None, (143, 13, 1, 10),
            ["eggs", "dairy", "breakfast", "protein"]),
    _anchor("milk", "Lucerne 2% Reduced Fat Milk", "Dairy", "Dairy", "Lucerne", "1 gal",
            3.79, None, (50, 3, 5, 2),
            ["milk", "dairy", "breakfast"]),
    _anchor("butter", "Lucerne Salted Butter", "Dairy", "Dairy", "Lucerne", "16 oz",
            4.49, None, (717, 1, 0, 81),
            ["butter", "dairy", "baking"]),
    _anchor("cheddar", "Tillamook Medium Cheddar Cheese", "Dairy", "Specialty Cheese", "Tillamook",
            "8 oz", 4.99, 3.99, (403, 25, 1, 33),
            ["cheese", "cheddar", "dairy"]),
    _anchor("honey_wheat_bread", "Dave's Killer Bread Honey Wheat", "Bakery", "Bread",
            "Dave's Killer Bread", "27 oz", 5.49, None, (260, 12, 46, 3),
            ["bread", "wheat", "bakery", "sandwich"]),
    _anchor("white_rice", "Signature Select Long Grain White Rice", "Pantry", "Rice & Grains",
            "Signature Select", "32 oz", 2.99, None, (360, 7, 80, 0.5),
            ["rice", "grain", "pantry", "recipe-chicken"]),
    _anchor("broccoli", "O Organics Broccoli Florets", "Produce", "Produce", "O Organics",
            "12 oz", 2.79, None, (34, 3, 7, 0),
            ["broccoli", "vegetable", "produce", "recipe-chicken"]),
    _anchor("lemon", "Fresh Lemon", "Produce", "Produce", "Signature Farms", "each",
            0.59, None, (29, 1, 9, 0),
            ["lemon", "citrus", "produce", "fresh"]),
    _anchor("bananas", "Fresh Bananas", "Produce", "Produce", "Signature Farms", "per lb",
            0.59, None, (89, 1, 23, 0),
            ["banana", "fruit", "produce", "breakfast"]),
    _anchor("strawberries", "Driscoll's Strawberries", "Produce", "Produce", "Driscoll's",
            "1 lb", 3.99, 2.99, (32, 1, 8, 0),
            ["strawberries", "berries", "fruit", "produce"]),
    _anchor("orange_juice", "Signature Select Orange Juice", "Beverages", "Refrigerated Beverages",
            "Signature Select", "52 fl oz", 3.49, None, (45, 1, 10, 0),
            ["juice", "orange", "beverage", "breakfast"]),
    _anchor("sparkling_water", "Signature Select Sparkling Water", "Beverages", "Water & Sparkling",
            "Signature Select", "12 pk", 3.99, None, (0, 0, 0, 0),
            ["sparkling water", "beverage", "zero-calorie"]),
    _anchor("coffee", "Peet's Major Dickason's Blend Ground Coffee", "Beverages", "Coffee & Tea",
            "Peet's", "12 oz", 8.99, 6.99, (2, 0, 0, 0),
            ["coffee", "beverage", "breakfast"]),
    _anchor("black_pepper", "Morton Ground Black Pepper", "Baking & Spices", "Spices", "Morton",
            "4 oz", 3.49, None, (251, 10, 64, 3),
            ["pepper", "spice", "seasoning"]),
    _anchor("salt", "Morton Iodized Salt", "Baking & Spices", "Spices", "Morton", "26 oz",
            1.29, None, (0, 0, 0, 0),
            ["salt", "seasoning", "baking"]),
    _anchor("all_purpose_flour", "Gold Medal All-Purpose Flour", "Baking & Spices", "Baking",
            "Gold Medal", "5 lb", 3.29, None, (364, 10, 76, 1),
            ["flour", "baking", "pantry"]),
    _anchor("sugar", "C&H Pure Cane Sugar", "Baking & Spices", "Baking", "C&H", "4 lb",
            3.99, None, (387, 0, 100, 0),
            ["sugar", "baking", "pantry"]),
]


# --------------------------------------------------------------------------- #
# OFF fetch + cache
# --------------------------------------------------------------------------- #
def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def off_search(filter_en: str, page: int) -> list[dict[str, Any]]:
    """Fetch one popularity-sorted page of US products for an OFF category.

    Responses are cached to disk; cache is consulted first so reruns are
    reproducible and work offline. A live-fetch failure falls back to cache,
    else returns an empty page (the run continues; final validation is the gate).
    """
    CACHE.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE / f"{_slug(filter_en)}-p{page}.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text()).get("products", [])

    params = urllib.parse.urlencode({
        "countries_tags_en": "United States",
        "categories_tags_en": filter_en,
        "fields": OFF_FIELDS,
        "sort_by": "unique_scans_n",
        "page_size": PAGE_SIZE,
        "page": page,
    })
    url = f"https://world.openfoodfacts.org/api/v2/search?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        time.sleep(REQUEST_DELAY_S)
        with urllib.request.urlopen(req, timeout=45) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        print(f"  ! fetch failed for {filter_en} p{page}: {exc}")
        return []
    cache_file.write_text(json.dumps(payload))
    return payload.get("products", [])


# --------------------------------------------------------------------------- #
# Cleaning / transform helpers
# --------------------------------------------------------------------------- #
ASCII_PRINTABLE = re.compile(r"^[\x20-\x7E]+$")


def smart_title(s: str) -> str:
    words = s.split()
    out: list[str] = []
    for i, w in enumerate(words):
        if any(c.isdigit() for c in w) or any(c.isupper() for c in w[1:]):
            out.append(w)  # keep sizes, acronyms, internal caps (e.g. "BelGioioso")
        elif w.lower() in SMALL_WORDS and i > 0:
            out.append(w.lower())
        else:
            out.append(w[:1].upper() + w[1:].lower())
    return " ".join(out)


def clean_name(raw: str) -> str:
    name = re.sub(r"\s+", " ", raw).strip(" -,.;:’'\"")
    return smart_title(name)


def is_good_name(name: str) -> bool:
    if not (3 <= len(name) <= 60):
        return False
    if not ASCII_PRINTABLE.match(name):
        return False
    letters = sum(c.isalpha() for c in name)
    if letters < 3 or letters / len(name) < 0.5:
        return False
    if "http" in name.lower() or "www." in name.lower():
        return False
    tokens = re.split(r"[^a-z]+", name.lower())
    if any(t in FOREIGN for t in tokens):
        return False
    return True


def norm_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def first_brand(brands: str | None) -> str:
    if not brands:
        return ""
    b = brands.split(",")[0].strip()
    return smart_title(b) if b else ""


def nutrition_of(n: dict[str, Any]) -> dict[str, int] | None:
    kcal = n.get("energy-kcal_100g")
    if kcal is None:
        kj = n.get("energy_100g")
        kcal = (kj / 4.184) if isinstance(kj, (int, float)) else None
    if not isinstance(kcal, (int, float)) or not (5 <= kcal <= 900):
        return None  # quality gate

    def g(key: str) -> int:
        v = n.get(key)
        return round(v) if isinstance(v, (int, float)) and v >= 0 else 0

    return {
        "calories": round(kcal),
        "protein_g": g("proteins_100g"),
        "carbs_g": g("carbohydrates_100g"),
        "fat_g": g("fat_100g"),
    }


def unit_of(quantity: str | None) -> str:
    if quantity:
        q = re.sub(r"\s+", " ", str(quantity)).strip()
        if q and is_good_name(q) and len(q) <= 24:
            return q
    return "1 ct"


def derive_tags(name: str, category: str, aisle: str, off_cats: list[str]) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()

    def add(tok: str) -> None:
        tok = tok.strip().lower()
        if len(tok) >= 3 and tok not in seen and tok not in TAG_STOP:
            seen.add(tok)
            tags.append(tok)

    for chunk in (category, aisle):
        for w in re.split(r"[^a-z]+", chunk.lower()):
            add(w)
    if off_cats:
        leaf = off_cats[-1].split(":")[-1].replace("-", " ")
        for w in leaf.split():
            add(w)
    for w in re.split(r"[^a-z]+", name.lower()):
        if len(w) >= 4:
            add(w)
    return tags[:8]


def synth_price(lo: float, hi: float) -> float:
    return round(rng.uniform(lo, hi), 2)


def synth_sale(price: float) -> float | None:
    if rng.random() < 0.25:
        return round(price * (1 - rng.uniform(0.10, 0.30)), 2)
    return None


# --------------------------------------------------------------------------- #
# Build the catalog
# --------------------------------------------------------------------------- #
def collect_raw() -> list[dict[str, Any]]:
    """Pull + clean OFF products per category plan. Returns intermediate dicts
    carrying everything needed to assemble the final records (ids assigned later)."""
    seen: set[str] = {norm_key(a["name"]) for a in ANCHORS}
    rows: list[dict[str, Any]] = []

    for entry in CATEGORY_PLAN:
        got = 0
        print(f"- {entry['category']} / {entry['aisle']}")
        for filt in entry["filters"]:
            if got >= entry["cap"]:
                break
            for page in range(1, MAX_PAGES + 1):
                if got >= entry["cap"]:
                    break
                products = off_search(filt, page)
                if not products:
                    break
                for p in products:
                    raw_name = p.get("product_name_en") or p.get("product_name") or ""
                    name = clean_name(raw_name)
                    if not is_good_name(name):
                        continue
                    brand = first_brand(p.get("brands"))
                    if brand and brand.lower() not in name.lower():
                        name = f"{brand} {name}"
                        if not is_good_name(name):
                            continue
                    key = norm_key(name)
                    if key in seen:
                        continue
                    nutr = nutrition_of(p.get("nutriments") or {})
                    if nutr is None:
                        continue
                    blob = f"{name} {brand} {p.get('categories_tags')}"
                    if BAN.search(blob):
                        continue
                    seen.add(key)
                    rows.append({
                        "name": name,
                        "category": entry["category"],
                        "aisle": entry["aisle"],
                        "brand": brand,
                        "unit": unit_of(p.get("quantity")),
                        "price_range": entry["price"],
                        "tags": derive_tags(name, entry["category"], entry["aisle"],
                                            p.get("categories_tags") or []),
                        "nutrition": nutr,
                        "popularity": p.get("unique_scans_n") or 0,
                    })
                    got += 1
                    if got >= entry["cap"]:
                        break
        print(f"    collected {got}")
    return rows


def build_products() -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Assemble products.json records. Returns (products, anchor_key -> product_id)."""
    products: list[dict[str, Any]] = []
    anchor_id: dict[str, str] = {}
    pid = 3001

    # Anchors first, with stable ids.
    for a in ANCHORS:
        cal, prot, carb, fat = a["nut"]
        product_id = f"p-{pid}"
        anchor_id[a["key"]] = product_id
        products.append({
            "product_id": product_id,
            "name": a["name"],
            "category": a["category"],
            "sku": f"MDB-{100000 + pid}",
            "brand": a["brand"],
            "unit": a["unit"],
            "price_usd": a["price"],
            "sale_price_usd": a["sale"],
            "in_stock": True,
            "aisle": a["aisle"],
            "tags": a["tags"],
            "nutrition": {"calories": cal, "protein_g": prot, "carbs_g": carb, "fat_g": fat},
        })
        pid += 1

    # Bulk, sorted deterministically before id/price assignment.
    rows = collect_raw()
    rows.sort(key=lambda r: (r["category"], r["aisle"], -int(r["popularity"]), r["name"]))
    for r in rows:
        lo, hi = r["price_range"]
        price = synth_price(lo, hi)
        products.append({
            "product_id": f"p-{pid}",
            "name": r["name"],
            "category": r["category"],
            "sku": f"MDB-{100000 + pid}",
            "brand": r["brand"],
            "unit": r["unit"],
            "price_usd": price,
            "sale_price_usd": synth_sale(price),
            "in_stock": rng.random() < 0.92,
            "aisle": r["aisle"],
            "tags": r["tags"],
            "nutrition": r["nutrition"],
        })
        pid += 1

    return products, anchor_id


# --------------------------------------------------------------------------- #
# Downstream regeneration: promotions, orders, KG, KB
# --------------------------------------------------------------------------- #
def _by_id(products: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {p["product_id"]: p for p in products}


def sample_by_category(products: list[dict[str, Any]], category: str, n: int,
                       exclude: set[str]) -> list[str]:
    pool = [p["product_id"] for p in products
            if p["category"] == category and p["product_id"] not in exclude]
    rng.shuffle(pool)
    return pool[:n]


def build_promotions(products: list[dict[str, Any]], aid: dict[str, str]) -> list[dict[str, Any]]:
    frozen = sample_by_category(products, "Frozen", 1, set(aid.values()))
    snack = sample_by_category(products, "Snacks & Candy", 1, set(aid.values()))

    def promo(code, ptype, kind, scope, stackable, applies, desc):
        return {"code": code, "type": ptype, "kind": kind, "scope": scope,
                "stackable": stackable,
                "applies_to": [{"product_id": pid, "amount": amt} for pid, amt in applies],
                "description": desc}

    promos = [
        promo("JFU-PASTA-50", "manufacturer", "amount_off", "item", True,
              [(aid["spaghetti"], 0.50), (aid["marinara"], 2.00)],
              "Manufacturer coupon: 50¢ off Barilla Spaghetti and $2 off Rao's Homemade Marinara Sauce."),
        promo("JFU-SAUCE-1OFF", "store", "amount_off", "item", True,
              [(aid["marinara"], 1.00)],
              "Store coupon: $1 off Rao's Homemade Marinara Sauce."),
        promo("JFU-MEAT-2OFF", "store", "amount_off", "item", True,
              [(aid["ground_beef"], 1.00), (aid["italian_sausage"], 1.50),
               (aid["chicken_breast"], 1.50), (aid["salmon"], 2.00)],
              "Store coupon: savings on Meat & Seafood favorites."),
        promo("JFU-DAIRY-1OFF", "manufacturer", "amount_off", "item", True,
              [(aid["parmesan"], 1.00), (aid["cheddar"], 1.00)],
              "Manufacturer coupon: $1 off BelGioioso Grated Parmesan and Tillamook Cheddar."),
        promo("JFU-PRODUCE-1OFF", "store", "amount_off", "item", True,
              [(aid["baby_spinach"], 1.00), (aid["strawberries"], 1.00)],
              "Store coupon: $1 off O Organics Baby Spinach and Driscoll's Strawberries."),
        promo("JFU-OIL-2OFF", "manufacturer", "amount_off", "item", True,
              [(aid["olive_oil"], 2.00)],
              "Manufacturer coupon: $2 off Bertolli Extra Virgin Olive Oil."),
        promo("JFU-BREAKFAST-1OFF", "store", "amount_off", "item", True,
              [(aid["orange_juice"], 0.50), (aid["coffee"], 2.00)],
              "Store coupon: breakfast savings on orange juice and ground coffee."),
        promo("JFU-BAKERY-1OFF", "store", "amount_off", "item", True,
              [(aid["honey_wheat_bread"], 1.00), (aid["italian_bread"], 0.50)],
              "Store coupon: $1 off Dave's Killer Bread and 50¢ off Signature Select Italian Bread."),
        promo("JFU-BEV-50", "store", "amount_off", "item", True,
              [(aid["sparkling_water"], 0.50)],
              "Store coupon: 50¢ off Signature Select Sparkling Water."),
    ]
    if frozen:
        promos.append(promo("JFU-FROZEN-2OFF", "manufacturer", "amount_off", "item", True,
                            [(frozen[0], 2.00)],
                            "Manufacturer coupon: $2 off a frozen favorite."))
    if snack:
        promos.append(promo("JFU-SNACK-1OFF", "store", "amount_off", "item", True,
                            [(snack[0], 1.00)],
                            "Store coupon: $1 off a snack favorite."))
    return promos


def _line(products_by_id, aid, key_or_id, qty, discount=0.0):
    pid = aid.get(key_or_id, key_or_id)
    p = products_by_id[pid]
    unit_price = p["sale_price_usd"] if p["sale_price_usd"] is not None else p["price_usd"]
    return {"product_id": pid, "name": p["name"], "qty": qty,
            "unit_price_usd": round(unit_price, 2), "discount_usd": round(discount, 2)}


def build_orders(products: list[dict[str, Any]], aid: dict[str, str]) -> list[dict[str, Any]]:
    pbid = _by_id(products)
    orders: list[dict[str, Any]] = []
    statuses = ["delivered", "completed", "delivered", "completed", "cancelled"]
    channels = ["curbside", "in-store", "delivery", "pickup"]
    n = [0]

    def add_order(cust, date, lines, coupons):
        n[0] += 1
        total = sum(l["qty"] * l["unit_price_usd"] - l["discount_usd"] for l in lines)
        savings = sum(l["discount_usd"] for l in lines)
        orders.append({
            "order_id": f"o-{20000 + n[0]}",
            "customer_id": cust,
            "order_date": date,
            "total_usd": round(total, 2),
            "status": statuses[n[0] % len(statuses)] if n[0] % 7 else "cancelled",
            "channel": channels[n[0] % len(channels)],
            "items": lines,
            "coupons_used": coupons,
            "savings_usd": round(savings, 2),
        })

    L = lambda k, q, d=0.0: _line(pbid, aid, k, q, d)  # noqa: E731

    # cust_R001 (Maria) — demo protagonist: recurring bolognese + produce so the
    # reorder preset surfaces a meaningful regular basket.
    add_order("cust_R001", "2026-04-12",
              [L("spaghetti", 2), L("marinara", 2, 4.0), L("baby_spinach", 1, 1.0), L("bananas", 3)],
              ["JFU-PASTA-50", "JFU-PRODUCE-1OFF"])
    add_order("cust_R001", "2026-04-26",
              [L("spaghetti", 1, 0.5), L("marinara", 1, 1.0), L("ground_beef", 2, 2.0),
               L("italian_sausage", 1), L("parmesan", 1)],
              ["JFU-PASTA-50", "JFU-SAUCE-1OFF", "JFU-MEAT-2OFF"])
    add_order("cust_R001", "2026-05-09",
              [L("spaghetti", 1), L("marinara", 1), L("baby_spinach", 2, 1.0),
               L("bananas", 2), L("italian_bread", 1)],
              ["JFU-PRODUCE-1OFF"])
    add_order("cust_R001", "2026-05-18",
              [L("spaghetti", 2), L("marinara", 2, 4.0), L("parmesan", 1), L("baby_spinach", 1, 1.0)],
              ["JFU-PASTA-50", "JFU-PRODUCE-1OFF"])
    add_order("cust_R001", "2026-05-28",
              [L("bananas", 3), L("strawberries", 1, 1.0), L("milk", 1), L("honey_wheat_bread", 1, 1.0)],
              ["JFU-PRODUCE-1OFF", "JFU-BAKERY-1OFF"])

    # Other customers — baskets weighted toward their favorite categories.
    add_order("cust_R002", "2026-05-20",
              [L("ground_beef", 2, 2.0), L("italian_sausage", 1), L("salmon", 2, 4.0), L("sparkling_water", 2, 0.5)],
              ["JFU-MEAT-2OFF", "JFU-BEV-50"])
    add_order("cust_R002", "2026-05-30",
              [L("chicken_breast", 2, 3.0), L("orange_juice", 1, 0.5), L("coffee", 1, 2.0)],
              ["JFU-MEAT-2OFF", "JFU-BREAKFAST-1OFF"])
    add_order("cust_R003", "2026-05-15",
              [L("baby_spinach", 2, 2.0), L("strawberries", 1, 1.0), L("milk", 1), L("eggs", 1)],
              ["JFU-PRODUCE-1OFF"])
    add_order("cust_R003", "2026-05-27",
              [L("bananas", 4), L("cheddar", 1, 1.0), L("milk", 2)],
              ["JFU-DAIRY-1OFF"])
    add_order("cust_R004", "2026-05-22",
              [L("spaghetti", 2), L("white_rice", 1), L("canned_tomatoes", 3), L("orange_juice", 1, 0.5)],
              ["JFU-BREAKFAST-1OFF"])
    add_order("cust_R005", "2026-05-19",
              [L("ground_beef", 1, 1.0), L("chicken_breast", 2, 3.0), L("white_rice", 1), L("olive_oil", 1, 2.0)],
              ["JFU-MEAT-2OFF", "JFU-OIL-2OFF"])
    add_order("cust_R005", "2026-05-31",
              [L("salmon", 2, 4.0), L("broccoli", 2), L("lemon", 3)],
              ["JFU-MEAT-2OFF"])
    add_order("cust_R006", "2026-05-21",
              [L("salmon", 1, 2.0), L("cheddar", 2, 2.0), L("milk", 1), L("butter", 1)],
              ["JFU-MEAT-2OFF", "JFU-DAIRY-1OFF"])
    add_order("cust_R006", "2026-05-29",
              [L("ground_beef", 2, 2.0), L("eggs", 2), L("cheddar", 1, 1.0)],
              ["JFU-MEAT-2OFF", "JFU-DAIRY-1OFF"])
    add_order("cust_R007", "2026-05-16",
              [L("strawberries", 2, 2.0), L("bananas", 3), L("orange_juice", 2, 1.0)],
              ["JFU-PRODUCE-1OFF", "JFU-BREAKFAST-1OFF"])
    add_order("cust_R008", "2026-05-23",
              [L("honey_wheat_bread", 1, 1.0), L("italian_bread", 1, 0.5), L("all_purpose_flour", 1), L("sugar", 1)],
              ["JFU-BAKERY-1OFF"])
    add_order("cust_R009", "2026-05-24",
              [L("baby_spinach", 1, 1.0), L("chicken_breast", 2, 3.0), L("roma_tomatoes", 2), L("garlic", 2)],
              ["JFU-PRODUCE-1OFF", "JFU-MEAT-2OFF"])
    add_order("cust_R010", "2026-05-25",
              [L("sparkling_water", 3, 0.5), L("coffee", 1, 2.0), L("canned_tomatoes", 2)],
              ["JFU-BEV-50", "JFU-BREAKFAST-1OFF"])
    return orders


def _names(aid, products_by_id, *keys) -> list[str]:
    return [products_by_id[aid[k]]["name"] for k in keys]


def rewrite_knowledge_graph(products, aid) -> list[dict[str, Any]]:
    pbid = _by_id(products)
    nm = lambda k: pbid[aid[k]]["name"]  # noqa: E731
    promos = build_promotions(products, aid)  # for coupon listing (codes only)
    coupon_codes = ", ".join(p["code"] for p in promos)

    rewrites = {
        "kg-product-category":
            f"{nm('spaghetti')} is a product in the Pantry category and is made by the brand "
            f"Barilla. {nm('marinara')} is also a Pantry product, made by the brand Rao's. Both are "
            f"staple ingredients for Italian pasta dishes.",
        "kg-product-brand":
            f"{nm('ground_beef')} and {nm('italian_sausage')} are Meat & Seafood products. "
            f"{nm('parmesan')} is a Dairy product used to finish pasta dishes.",
        "kg-recipe-ingredients":
            "The Spaghetti Bolognese recipe uses " + ", ".join(_names(
                aid, pbid, "spaghetti", "marinara", "ground_beef", "italian_sausage",
                "parmesan", "olive_oil", "canned_tomatoes", "garlic", "yellow_onion",
                "italian_bread", "baby_spinach")) + ".",
        "kg-promotion-product":
            "Agent Cartsmith promotions map coupons to the products they discount. The active "
            f"coupons are: {coupon_codes}. Each coupon lists the products it applies to in the "
            "promotions catalog, and member price is always applied before any coupon.",
        "kg-brand-products":
            f"{nm('baby_spinach')}, {nm('roma_tomatoes')} and {nm('broccoli')} are O Organics "
            "store-brand Produce items.",
    }

    existing = json.loads((SEEDS / "knowledge_graph.json").read_text())
    out = []
    for e in existing:
        src = e["metadata"].get("source")
        if src in rewrites:
            out.append({"text": rewrites[src], "metadata": e["metadata"]})
        else:
            out.append(e)  # preserve brand-origin, customer, loyalty, store triples
    return out


def rewrite_knowledge_base(products, aid) -> list[dict[str, Any]]:
    pbid = _by_id(products)
    nm = lambda k: pbid[aid[k]]["name"]  # noqa: E731

    recipe = (
        f"Recipe: Spaghetti Bolognese (serves 4). Ingredients: {nm('spaghetti')} (1 box), "
        f"{nm('marinara')} (1 jar), {nm('ground_beef')} (1 lb), {nm('italian_sausage')} (1/2 lb), "
        f"{nm('parmesan')} (to taste), {nm('olive_oil')} (2 tbsp), {nm('canned_tomatoes')} (1 can), "
        f"{nm('garlic')} (3 cloves), {nm('yellow_onion')} (1 medium), {nm('italian_bread')} "
        f"(1 loaf, for serving). Steps: 1) Heat the olive oil in a large pan over medium heat. Dice "
        f"the yellow onion and mince the garlic, then saute until fragrant and translucent. 2) Add "
        f"the ground beef and Italian sausage (casing removed); brown thoroughly, breaking it up "
        f"with a spoon. 3) Stir in the canned tomatoes and marinara sauce; simmer 25-30 minutes "
        f"until thickened. 4) Meanwhile, cook the spaghetti in salted boiling water until al dente, "
        f"then drain. 5) Toss the pasta with the sauce, top with grated parmesan, and serve with "
        f"warm Italian bread. Pairs well with a side salad of {nm('baby_spinach')}."
    )
    chicken = (
        f"Recipe: Quick Weeknight Lemon Garlic Chicken (serves 4, about 30 minutes). Ingredients: "
        f"{nm('chicken_breast')} (1.5 lb), {nm('olive_oil')} (2 tbsp), {nm('garlic')} (4 cloves), "
        f"{nm('lemon')} (1), {nm('white_rice')} (1 cup), {nm('broccoli')} (12 oz), salt and pepper to "
        f"taste. Steps: 1) Cook the white rice per package directions. 2) Season the chicken breasts "
        f"with salt and pepper. Heat olive oil in a skillet over medium-high and sear the chicken 5-6 "
        f"minutes per side until cooked through. 3) Add minced garlic and the juice of one lemon to "
        f"the pan; spoon over the chicken. 4) Steam or roast the broccoli florets until tender-crisp. "
        f"5) Serve the chicken and broccoli over the rice. A fast, in-stock weeknight dinner."
    )
    meal_plan = (
        f"Weekly Meal Plan for a Family of 4 on a $150 Budget. Monday: Spaghetti Bolognese with "
        f"{nm('spaghetti')}, {nm('ground_beef')}, and {nm('marinara')}, served with {nm('italian_bread')} "
        f"and a baby spinach side salad (~$22). Tuesday: Lemon garlic chicken with {nm('chicken_breast')}, "
        f"{nm('white_rice')}, and {nm('broccoli')} (~$18). Wednesday: Pan-seared {nm('salmon')} with "
        f"sauteed {nm('baby_spinach')} and {nm('lemon')} (~$26). Thursday: Italian sausage and pepper "
        f"skillet over pasta with parmesan (~$16). Friday: Breakfast-for-dinner with {nm('eggs')}, "
        f"{nm('honey_wheat_bread')} toast, and {nm('strawberries')} and {nm('bananas')} (~$15). Saturday: "
        f"{nm('cheddar')} grilled cheese with tomato soup (~$12). Sunday: Roast chicken with "
        f"{nm('russet_potato')} and a mixed greens salad (~$20). Stock up on {nm('milk')}, "
        f"{nm('orange_juice')}, and {nm('sparkling_water')} for the week (~$15). The plan leans on "
        f"O Organics and Signature Farms store-brand staples and this week's sale items to stay under $150."
    )
    weekly_ad = (
        f"This Week's Ad Highlights (valid through 2026-06-07). On sale now: {nm('marinara')} for "
        f"$3.99, {nm('ground_beef')} for $4.99/lb, {nm('chicken_breast')} for $4.99/lb, {nm('salmon')} "
        f"for $7.99/lb, {nm('olive_oil')} for $6.99, {nm('baby_spinach')} for $1.99, {nm('strawberries')} "
        f"for $2.99, {nm('cheddar')} for $3.99, and {nm('coffee')} for $6.99. Clip the matching digital "
        f"coupons in the Agent Cartsmith app to stack additional savings on member prices."
    )

    rewrites = {"recipe": recipe, "meal-plan": meal_plan, "weekly-ad": weekly_ad}
    existing = json.loads((SEEDS / "knowledge_base.json").read_text())
    out = []
    have_chicken = any(e["metadata"].get("source") == "recipe-chicken" for e in existing)
    for e in existing:
        src = e["metadata"].get("source")
        if src in rewrites:
            out.append({"text": rewrites[src], "metadata": e["metadata"]})
        else:
            out.append(e)  # preserve coupon-policy, loyalty, return, store-info, produce-guide
    if not have_chicken:
        out.append({"text": chicken, "metadata": {"source": "recipe-chicken",
                                                  "category": "recipes", "updated": "2026-05-30"}})
    return out


# --------------------------------------------------------------------------- #
# Validation + write
# --------------------------------------------------------------------------- #
def validate(products, promotions, orders, kg, kb, aid) -> None:
    ids = {p["product_id"] for p in products}
    assert len(products) >= TARGET_TOTAL, f"only {len(products)} products (< {TARGET_TOTAL})"
    assert len(ids) == len(products), "duplicate product_id"

    promo_codes = {p["code"] for p in promotions}
    for pr in promotions:
        for app in pr["applies_to"]:
            assert app["product_id"] in ids, f"{pr['code']} targets missing {app['product_id']}"
    assert len(promo_codes) >= 7, "need >=7 promotions"

    for o in orders:
        for it in o["items"]:
            assert it["product_id"] in ids, f"{o['order_id']} item missing {it['product_id']}"
        for c in o["coupons_used"]:
            assert c in promo_codes, f"{o['order_id']} uses unknown coupon {c}"

    kg_promo = next(e for e in kg if e["metadata"].get("source") == "kg-promotion-product")
    for code in promo_codes:
        assert code in kg_promo["text"], f"coupon {code} absent from kg-promotion-product"

    # Bolognese ingredients must all resolve to real products.
    bolognese = ["spaghetti", "marinara", "ground_beef", "italian_sausage", "parmesan",
                 "olive_oil", "canned_tomatoes", "garlic", "yellow_onion", "italian_bread"]
    for k in bolognese:
        assert aid[k] in ids, f"bolognese ingredient {k} missing"

    blob = json.dumps([products, promotions, orders, kg, kb])
    assert not BAN.search(blob), "banned industry term leaked into seeds"


def write_array_compact(path: Path, items: list[dict[str, Any]]) -> None:
    """One JSON object per line within an array (matches products/orders style)."""
    body = ",\n".join("  " + json.dumps(it, ensure_ascii=False) for it in items)
    path.write_text(f"[\n{body}\n]\n", encoding="utf-8")


def write_pretty(path: Path, items: list[dict[str, Any]]) -> None:
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    print("Building catalog from Open Food Facts ...")
    products, aid = build_products()
    promotions = build_promotions(products, aid)
    orders = build_orders(products, aid)
    kg = rewrite_knowledge_graph(products, aid)
    kb = rewrite_knowledge_base(products, aid)

    validate(products, promotions, orders, kg, kb, aid)

    write_array_compact(OPS / "products.json", products)
    write_array_compact(OPS / "orders.json", orders)
    write_pretty(OPS / "promotions.json", promotions)
    write_pretty(SEEDS / "knowledge_graph.json", kg)
    write_pretty(SEEDS / "knowledge_base.json", kb)

    # Summary.
    by_cat: dict[str, int] = {}
    for p in products:
        by_cat[p["category"]] = by_cat.get(p["category"], 0) + 1
    print("\n=== summary ===")
    print(f"products: {len(products)}  promotions: {len(promotions)}  orders: {len(orders)}")
    print(f"knowledge_graph: {len(kg)}  knowledge_base: {len(kb)}")
    for cat in sorted(by_cat):
        print(f"  {cat:24s} {by_cat[cat]}")
    print("OK")


if __name__ == "__main__":
    main()
