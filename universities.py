import json
import os
import time
from functools import lru_cache
from typing import Optional

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

HIPOLAB_COUNTRY = "http://universities.hipolabs.com/search"
DOWNLOAD_DIR = "data"
UNI_CSV = os.path.join(DOWNLOAD_DIR, "universities.csv")
ORG_CSV = os.path.join(DOWNLOAD_DIR, "organisations.csv")
CACHE_DIR = os.path.join(DOWNLOAD_DIR, "cache")
COUNTRY_CACHE = os.path.join(CACHE_DIR, "country_by_name.json")
REGION_CACHE = os.path.join(CACHE_DIR, "region_by_country.json")
REST_COUNTRIES_REGION = "https://restcountries.com/v3.1/name/{}?fields=region,subregion"
DEFAULT_TIMEOUT = 20
HEADERS = {"User-Agent": "ExchangePlannerApp/1.0 (contact: your_email@example.com)", "Accept": "application/json"}


def _requests_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(HEADERS)
    return session


SESSION = _requests_session()


def fetch_json(url: str, params: Optional[dict] = None, timeout: int = DEFAULT_TIMEOUT) -> Optional[dict | list]:
    try:
        resp = SESSION.get(url, params=params, timeout=timeout)
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except Exception:
        return None


def _load_cache(path: str) -> dict:
    try:
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


@lru_cache(maxsize=2048)
def get_country_for_name(name: str) -> Optional[str]:
    name = (name or "").strip()
    if len(name) < 3:
        return None
    print(f"➡️ Getting country for: {name}")
    # Check persistent cache first
    c = _load_cache(COUNTRY_CACHE)
    if name in c:
        return c.get(name)
    data = fetch_json(HIPOLAB_COUNTRY, params={"name": name})
    result: Optional[str] = None
    if data and isinstance(data, list) and data:
        try:
            result = data[0].get("country")
        except Exception:
            result = None
    # Persist even negative result to avoid repeated lookups
    c[name] = result
    _save_cache(COUNTRY_CACHE, c)
    return result


@lru_cache(maxsize=1024)
def get_region_for_country(country: str) -> Optional[str]:
    country = (country or "").strip()
    if not country:
        return None
    print(f"➡️ Getting region for: {country}")
    # Check persistent cache first
    c = _load_cache(REGION_CACHE)
    if country in c:
        return c.get(country)
    data = fetch_json(REST_COUNTRIES_REGION.format(country))
    result: Optional[str] = None
    if data and isinstance(data, list) and data:
        first = data[0]
        result = first.get("region") or first.get("subregion")
    c[country] = result
    _save_cache(REGION_CACHE, c)
    return result


def refine_universities_csv():
    # Step 1: get all universities
    df = pd.read_csv(ORG_CSV)
    df_unis = df[df["org"].astype(str).str.upper() == "SCHL"].copy()
    df_unis.to_csv(UNI_CSV, index=False)

    # Step 2: enrich with country from Hipolabs API (cached + retries)
    df_unis["country"] = df_unis["name"].astype(str).apply(get_country_for_name)
    df_unis.to_csv(UNI_CSV, index=False)

    # Step 3: enrich with region from RestCountries API (cached + retries)
    df_unis["region"] = df_unis["country"].astype(str).apply(get_region_for_country)
    df_unis.to_csv(UNI_CSV, index=False)

    # Step 4: filter rows with valid country values
    df = pd.read_csv(UNI_CSV)
    df_unis = df[df["country"].notna() & (df["country"].astype(str).str.strip() != "")].copy()
    df_unis.to_csv(UNI_CSV, index=False)

    print(f"Wrote {len(df_unis)} university rows to {UNI_CSV}")

if __name__ == "__main__":
    refine_universities_csv()