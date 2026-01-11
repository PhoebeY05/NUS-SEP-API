import json
import os
import re
import threading
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
from difflib import get_close_matches

import pandas as pd
import requests

# =========================
# CONFIG
# =========================

HEADERS = {
    "User-Agent": "ExchangePlannerApp/1.0 (contact: your_email@example.com)",
    "Accept": "application/json"
}

HIPOLABS_URL = "http://universities.hipolabs.com/search"
RESTCOUNTRIES_ALL = "https://restcountries.com/v3.1/all?fields=name,region,subregion"
WIKIDATA_SEARCH = "https://www.wikidata.org/w/api.php"
WIKI_SEARCH = "https://en.wikipedia.org/w/api.php"
WIKI_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/{}"
NOMINATIM = "https://nominatim.openstreetmap.org/search"

CACHE_DIR = "data/cache"
COUNTRY_CACHE = os.path.join(CACHE_DIR, "country_by_name.json")
REGION_CACHE = os.path.join(CACHE_DIR, "region_by_country.json")

DOWNLOAD_DIR = "data"
ORG_CSV = os.path.join(DOWNLOAD_DIR, "organisations.csv")
UNI_CSV = os.path.join(DOWNLOAD_DIR, "universities.csv")

PARALLEL_WORKERS = 8
CACHE_RW_LOCK = threading.RLock()

os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# =========================
# MANUAL OVERRIDES & ALIASES
# =========================

UNIVERSITY_ALIASES = {
    "Aalto Uni Sch of Economics": "Aalto University School of Business",
    "École de Cuisine de Paris": "École de Cuisine de Paris",
    "Bologna Cooking School": "Bologna Cooking School",
    "Bryn Mawr College, PA": "Bryn Mawr College"
}

MANUAL_COUNTRY_OVERRIDES = {
    "Bologna Cooking School": "Italy",
    "École de Cuisine de Paris": "France"
}

# =========================
# NAME CANDIDATES LOGIC
# =========================

ABBREV_EXPANSIONS = [
    ("&", " and "), ("Uni ", "University "), (" Uni", " University"), ("Univ", "University"),
    ("Inst", "Institute"), ("Tech", "Technology"), ("Mgmt", "Management"), ("Mngt", "Management"),
    ("Sci", "Science"), ("Comm", "Communication"), ("Info", "Information"), ("Engrg", "Engineering"),
    ("Engg", "Engineering"), ("Eng", "Engineering"), ("Comp", "Computer"), ("Med", "Medical"),
]

STOP_PREFIXES = ["The ", "the "]
STRIP_TOKENS = ["university", "college", "institute", "school", "national", "state", "of", "and", "the", "for", "at", "of the"]

def clean_basic(s: str) -> str:
    s2 = re.sub(r"[\[\]\(\)/\\\-\_\.,\u00a0]", " ", s)
    s2 = " ".join(s2.split())
    return s2.strip()

def expand_abbrev(s: str) -> str:
    for a, b in ABBREV_EXPANSIONS:
        s = s.replace(a, b)
    return " ".join(s.split())

def remove_city_qualifier(s: str) -> str:
    return s.split(",")[0].strip()

def ensure_the_prefix_variants(s: str) -> list[str]:
    variants = [s]
    if s.lower().startswith("the "):
        variants.append(s[4:].strip())
    else:
        variants.append(f"The {s}")
    return variants

def generate_name_candidates(name: str) -> list[str]:
    base = name.strip()
    cands = [base, base.title()]
    cands.extend(ensure_the_prefix_variants(base))

    cleaned = clean_basic(base)
    cleaned = remove_city_qualifier(cleaned)
    cands.append(cleaned)
    cands.extend(ensure_the_prefix_variants(cleaned))

    expanded = expand_abbrev(cleaned)
    cands.append(expanded)
    cands.extend(ensure_the_prefix_variants(expanded))

    for p in STOP_PREFIXES:
        if base.startswith(p):
            cands.append(base[len(p):].strip())

    tokens = [t for t in clean_basic(expanded).split() if t.lower() not in STRIP_TOKENS]
    if tokens:
        slim = " ".join(tokens)
        cands.append(slim)

    # Deduplicate, preserve order
    seen = set()
    ordered = []
    for x in cands:
        key = x.lower()
        if len(x) >= 3 and key not in seen:
            seen.add(key)
            ordered.append(x)
    return ordered[:10]

# =========================
# UTILITIES
# =========================

def load_cache(path):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_cache(path, data):
    with CACHE_RW_LOCK:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

def normalize_name(name: str) -> str:
    name = name.strip()
    name = UNIVERSITY_ALIASES.get(name, name)
    name = re.sub(r",\s*[A-Z]{2}$", "", name)
    name = re.sub(r",\s*(USA|United States|UK|United Kingdom|Italy|France|Germany|Japan|Singapore)$", "", name, flags=re.I)
    return name.strip()

def fetch_json(url, params=None, timeout=20):
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None

def fuzzy_match(query: str, choices: list[str], cutoff=0.7) -> Optional[str]:
    query = query.lower()
    choices_lower = [c.lower() for c in choices]
    matches = get_close_matches(query, choices_lower, n=1, cutoff=cutoff)
    if matches:
        index = choices_lower.index(matches[0])
        return choices[index]
    return None

# =========================
# COUNTRY LOOKUPS
# =========================

def get_country_from_hipolabs(name):
    for cand in generate_name_candidates(name):
        data = fetch_json(HIPOLABS_URL, {"name": cand})
        if data:
            names = [d.get("name", "") for d in data if d.get("country")]
            countries = [d.get("country") for d in data if d.get("country")]
            match = fuzzy_match(cand, names)
            if match:
                idx = names.index(match)
                return countries[idx]
    return None

def get_country_from_wikidata(name):
    for cand in generate_name_candidates(name):
        params = {"action": "wbsearchentities", "search": cand, "language": "en", "format": "json"}
        data = fetch_json(WIKIDATA_SEARCH, params)
        if not data or not data.get("search"):
            continue

        for item in data["search"][:5]:
            qid = item["id"]
            entity_params = {"action": "wbgetentities", "ids": qid, "props": "claims|labels|aliases", "languages": "en", "format": "json"}
            entity_data = fetch_json(WIKIDATA_SEARCH, entity_params)
            if not entity_data:
                continue
            entity = entity_data.get("entities", {}).get(qid, {})
            names_list = [entity.get("labels", {}).get("en", {}).get("value", "")]
            names_list += [a.get("value") for a in entity.get("aliases", {}).get("en", [])]
            match = fuzzy_match(cand.lower(), names_list)
            if match:
                claims = entity.get("claims", {})
                if "P17" in claims:
                    for claim in claims["P17"]:
                        mainsnak = claim.get("mainsnak", {})
                        datavalue = mainsnak.get("datavalue", {})
                        value = datavalue.get("value", {})
                        if isinstance(value, dict) and "id" in value:
                            country_qid = value["id"]
                            label_params = {"action": "wbgetentities", "ids": country_qid, "props": "labels", "languages": "en", "format": "json"}
                            label_data = fetch_json(WIKIDATA_SEARCH, label_params)
                            if label_data:
                                country_label = label_data.get("entities", {}).get(country_qid, {}).get("labels", {}).get("en", {}).get("value")
                                if country_label:
                                    return country_label
    return None


def get_country_from_nominatim(name):
    for cand in generate_name_candidates(name):
        params = {"q": cand, "format": "json", "addressdetails": 1, "limit": 1}
        data = fetch_json(NOMINATIM, params)
        if data:
            return data[0].get("address", {}).get("country")
    return None

def get_all_countries():
    cache_path = os.path.join(CACHE_DIR, "countries_list.json")
    cached = load_cache(cache_path)
    if cached:
        return cached

    data = fetch_json(RESTCOUNTRIES_ALL)
    countries = []
    if data:
        for c in data:
            name = c.get("name", {}).get("common") or c.get("name", {}).get("official")
            if name:
                countries.append(name)
    countries = sorted(countries, key=len, reverse=True)
    save_cache(cache_path, countries)
    return countries

def get_country_from_wikipedia_text(name):
    countries = get_all_countries()
    for cand in generate_name_candidates(name):
        search = fetch_json(WIKI_SEARCH, {"action": "query", "list": "search", "srsearch": cand, "format": "json"})
        if not search or not search.get("query", {}).get("search"):
            continue
        title = search["query"]["search"][0]["title"]
        summary = fetch_json(WIKI_SUMMARY.format(title.replace(" ", "_")))
        text = summary.get("extract", "") if summary else ""
        match = fuzzy_match(cand.lower(), countries, cutoff=0.6)
        if match:
            return match
        for country in countries:
            if re.search(rf"\b{re.escape(country)}\b", text):
                return country
    return None

@lru_cache(maxsize=4096)
def get_country_for_name(name: str) -> Optional[str]:
    name = normalize_name(name)
    if name in MANUAL_COUNTRY_OVERRIDES:
        return MANUAL_COUNTRY_OVERRIDES[name]

    cache = load_cache(COUNTRY_CACHE)
    if name in cache:
        return cache[name]

    result = (
        get_country_from_hipolabs(name)
        or get_country_from_wikidata(name)
        or get_country_from_nominatim(name)
        or get_country_from_wikipedia_text(name)
    )

    cache[name] = result
    save_cache(COUNTRY_CACHE, cache)
    return result

# =========================
# REGION LOOKUP
# =========================

@lru_cache(maxsize=2048)
def get_region_for_country(country: str) -> Optional[str]:
    country = country.strip() if country else ""
    if not country:
        return None

    cache = load_cache(REGION_CACHE)
    if country in cache:
        return cache[country]

    all_data = fetch_json(RESTCOUNTRIES_ALL)
    region = None
    if all_data:
        for c in all_data:
            name = c.get("name", {}).get("common") or c.get("name", {}).get("official")
            if name and name.lower() == country.lower():
                region = c.get("region") or c.get("subregion")
                break

    cache[country] = region
    save_cache(REGION_CACHE, cache)
    return region

# =========================
# CSV PIPELINE
# =========================

def parallel_map_unique(series: pd.Series, func) -> pd.Series:
    values = series.astype(str).map(lambda v: v.strip() or None)
    uniques = sorted(set(v for v in values if v))
    results = {}
    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as ex:
        for k, r in zip(uniques, ex.map(func, uniques)):
            results[k] = r
    return values.map(lambda v: results.get(v))

def refine_universities_csv():
    df = pd.read_csv(ORG_CSV)
    df_unis = df[df["org"].astype(str).str.upper() == "SCHL"].copy()

    df_unis["country"] = parallel_map_unique(df_unis["name"], get_country_for_name)
    df_unis["region"] = parallel_map_unique(df_unis["country"], get_region_for_country)

    df_unis.to_csv(UNI_CSV, index=False)
    print(f"✅ Wrote {len(df_unis)} rows to {UNI_CSV}")

# =========================
# EXAMPLES
# =========================

if __name__ == "__main__":
    refine_universities_csv()
