from io import StringIO
from pathlib import Path
from typing import Optional

import pandas as pd
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, StreamingResponse

app = FastAPI(title="SEP EduRec CSV API", version="0.1.0")


# Base paths
ROOT = Path(__file__).parent
data = ROOT / "data"

def df_to_csv_response(df: pd.DataFrame, filename: str) -> StreamingResponse:
    """Return DataFrame as a CSV StreamingResponse with sensible headers."""
    # Ensure deterministic column order by preserving current order
    buf = StringIO()
    df.to_csv(buf, index=False)
    buf.seek(0)
    headers = {
        "Content-Disposition": f"attachment; filename={filename}",
    }
    return StreamingResponse(buf, media_type="text/csv", headers=headers)


def df_to_json_records(
    df: pd.DataFrame,
    *,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
) -> list[dict]:
    """Return a list of dict records with optional pagination."""
    if offset is not None and offset > 0:
        df = df.iloc[offset:]
    if limit is not None and limit >= 0:
        df = df.iloc[:limit]
    # Replace NaN/NaT and +/-Inf with None to ensure JSON compliance
    try:
        df = df.replace([float('inf'), float('-inf')], None)
        # Ensure columns can hold Python None (not coerced back to NaN)
        df = df.astype(object)
        df = df.where(pd.notna(df), None)
    except Exception:
        # Fallback: best-effort nulling
        df = df.where(pd.notna(df), None)
    return df.to_dict(orient="records")


# Add reserved params and a dynamic filter helper applied across all endpoints
RESERVED_PARAMS = {
    "limit",
    "offset",
    "contains",
    # existing explicit filters across endpoints
    "country",
    "region",
    "name",
    "org",
    "search",
    "faculty_id",
    "university_id",
    "faculty",
    "university",
}


def _resolve_column(
    df: pd.DataFrame,
    col: str,
    aliases: dict[str, list[str] | str] | None = None,
) -> Optional[str]:
    """Resolve a requested column name to an actual DataFrame column.
    Resolution order:
      1) Exact match
      2) Case-insensitive match
      3) Aliases mapping (first present candidate, case-insensitive)
    Returns the concrete column name or None if not found.
    """
    if col in df.columns:
        return col
    norm = {c.strip().lower(): c for c in df.columns}
    if col.strip().lower() in norm:
        return norm[col.strip().lower()]
    if aliases and col in aliases:
        candidates = aliases[col]
        if isinstance(candidates, str):
            candidates = [candidates]
        for cand in candidates:
            if cand in df.columns:
                return cand
            if cand.strip().lower() in norm:
                return norm[cand.strip().lower()]
    return None


def apply_dynamic_filters(
    df: pd.DataFrame, query_params, exclude: set[str] | None = None, aliases: dict[str, list[str] | str] | None = None
) -> pd.DataFrame:
    """
    Apply filters for any column found in query params.
    Defaults:
      - string columns: case-insensitive contains
      - non-string columns: equality
    Supported operators via suffix:
      - __eq, __contains, __in (comma-separated), __gt, __ge, __lt, __le, __ne
    """
    exclude = exclude or set()
    # iterate over keys except reserved ones
    for key in query_params.keys():
        if key in exclude:
            continue
        values = query_params.getlist(key)
        if not values:
            continue

        col, op = key, None
        if "__" in key:
            col, op = key.split("__", 1)

        target_col = _resolve_column(df, col, aliases)
        if not target_col:
            continue

        s = df[target_col]
        # Build mask per operator
        if op in (None, "contains"):
            if pd.api.types.is_string_dtype(s):
                mask = pd.Series(False, index=df.index)
                for v in values:
                    mask |= s.astype(str).str.contains(str(v), case=False, na=False)
            else:
                # non-string default to equality; fallback to contains if coercion fails
                mask = pd.Series(False, index=df.index)
                for v in values:
                    num = pd.to_numeric(v, errors="coerce")
                    try:
                        mask |= (s == num)
                    except Exception:
                        mask |= s.astype(str).str.contains(str(v), case=False, na=False)
        elif op == "eq":
            if pd.api.types.is_string_dtype(s):
                mask = pd.Series(False, index=df.index)
                for v in values:
                    mask |= (s.astype(str).str.lower() == str(v).lower())
            else:
                mask = pd.Series(False, index=df.index)
                for v in values:
                    mask |= (s == pd.to_numeric(v, errors="coerce"))
        elif op == "in":
            # membership in comma-separated list
            if pd.api.types.is_string_dtype(s):
                targets = {t.strip().lower() for v in values for t in str(v).split(",")}
                mask = s.astype(str).str.lower().isin(targets)
            else:
                targets = [pd.to_numeric(t.strip(), errors="coerce") for v in values for t in str(v).split(",")]
                mask = s.isin(targets)
        elif op in {"gt", "ge", "lt", "le", "ne"}:
            val = pd.to_numeric(values[0], errors="coerce")
            if pd.isna(val):
                continue
            if op == "gt":
                mask = s > val
            elif op == "ge":
                mask = s >= val
            elif op == "lt":
                mask = s < val
            elif op == "le":
                mask = s <= val
            else:  # ne
                mask = s != val
        else:
            # unknown operator, skip
            continue

        df = df[mask]

    return df


@app.get("/health", response_class=PlainTextResponse)
def health() -> str:
    return "ok"


@app.get("/faculties.csv")
def get_faculties_csv(request: Request):
    path = data / "faculties.csv"
    if not path.exists():
        raise HTTPException(status_code=404, detail="faculties.csv not found")
    try:
        df = pd.read_csv(path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed reading faculties.csv: {e}")
    # Normalize expected columns if present
    cols = [c for c in ["id", "name"] if c in df.columns]
    if cols:
        df = df[cols]
    # Apply dynamic per-column filters
    df = apply_dynamic_filters(df, request.query_params, exclude=RESERVED_PARAMS)
    return df_to_csv_response(df, "faculties.csv")


@app.get("/faculties")
def get_faculties_json(
    limit: Optional[int] = Query(None, ge=0, description="Max rows to return"),
    offset: Optional[int] = Query(None, ge=0, description="Rows to skip"),
    request: Request = None,
):
    path = data / "faculties.csv"
    if not path.exists():
        raise HTTPException(status_code=404, detail="faculties.csv not found")
    try:
        df = pd.read_csv(path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed reading faculties.csv: {e}")
    cols = [c for c in ["id", "name"] if c in df.columns]
    if cols:
        df = df[cols]
    # Apply dynamic per-column filters
    df = apply_dynamic_filters(df, request.query_params, exclude=RESERVED_PARAMS)
    return df_to_json_records(df, limit=limit, offset=offset)


@app.get("/organisations.csv")
def get_organisations_csv(
    org: Optional[str] = Query(None, description="Filter by organisation type, e.g., SCHL"),
    search: Optional[str] = Query(None, description="Filter by search name contains (case-insensitive)"),
    contains: Optional[str] = Query(None, description="Filter rows where any text column contains this value"),
    request: Request = None,
):
    path = data / "organisations.csv"
    if not path.exists():
        raise HTTPException(status_code=404, detail="organisations.csv not found")
    try:
        df = pd.read_csv(path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed reading organisations.csv: {e}")

    # Optional filters
    if org and "org" in df.columns:
        df = df[df["org"].astype(str).str.upper() == org.upper()]
    if search and "search" in df.columns:
        df = df[df["search"].astype(str).str.contains(search, case=False, na=False)]
    if contains:
        # Apply a broad contains across all string-like columns
        mask = pd.Series(False, index=df.index)
        for col in df.columns:
            mask = mask | df[col].astype(str).str.contains(contains, case=False, na=False)
        df = df[mask]
    # Apply dynamic per-column filters
    df = apply_dynamic_filters(df, request.query_params, exclude=RESERVED_PARAMS)
    return df_to_csv_response(df, "organisations.csv")


@app.get("/organisations")
def get_organisations_json(
    org: Optional[str] = Query(None, description="Filter by organisation type, e.g., SCHL"),
    search: Optional[str] = Query(None, description="Filter by search name contains (case-insensitive)"),
    contains: Optional[str] = Query(None, description="Filter rows where any text column contains this value"),
    limit: Optional[int] = Query(None, ge=0, description="Max rows to return"),
    offset: Optional[int] = Query(None, ge=0, description="Rows to skip"),
    request: Request = None,
):
    path = data / "organisations.csv"
    if not path.exists():
        raise HTTPException(status_code=404, detail="organisations.csv not found")
    try:
        df = pd.read_csv(path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed reading organisations.csv: {e}")

    if org and "org" in df.columns:
        df = df[df["org"].astype(str).str.upper() == org.upper()]
    if search and "search" in df.columns:
        df = df[df["search"].astype(str).str.contains(search, case=False, na=False)]
    if contains:
        mask = pd.Series(False, index=df.index)
        for col in df.columns:
            mask = mask | df[col].astype(str).str.contains(contains, case=False, na=False)
        df = df[mask]
    # Apply dynamic per-column filters
    df = apply_dynamic_filters(df, request.query_params, exclude=RESERVED_PARAMS)
    return df_to_json_records(df, limit=limit, offset=offset)


@app.get("/universities.csv")
def get_universities_csv(
    contains: Optional[str] = Query(None, description="Filter rows where any text column contains this value"),
    country: Optional[str] = Query(None, description="Filter by country (exact, case-insensitive)"),
    region: Optional[str] = Query(None, description="Filter by region (exact, case-insensitive)"),
    name: Optional[str] = Query(None, description="Filter by university name contains (case-insensitive)"),
    request: Request = None,
):
    """Return partner universities with optional filters for country, region, and name."""
    path = data / "universities.csv"
    if not path.exists():
        raise HTTPException(status_code=404, detail="universities.csv not found")
    try:
        df = pd.read_csv(path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed reading universities.csv: {e}")

    # Apply explicit filters
    if country and "country" in df.columns:
        df = df[df["country"].astype(str).str.lower() == country.strip().lower()]
    if region and "region" in df.columns:
        df = df[df["region"].astype(str).str.lower() == region.strip().lower()]
    if name:
        name_col = _resolve_column(df, "name", aliases={"name": ["name", "university"]})
        if name_col:
            df = df[df[name_col].astype(str).str.contains(name, case=False, na=False)]
    if contains:
        mask = pd.Series(False, index=df.index)
        for col in df.columns:
            mask = mask | df[col].astype(str).str.contains(contains, case=False, na=False)
        df = df[mask]
    # Apply dynamic per-column filters
    df = apply_dynamic_filters(
        df,
        request.query_params,
        exclude=RESERVED_PARAMS,
        aliases={
            "name": ["name", "university"],
            "id": ["id", "university id", "university_id"],
        },
    )
    return df_to_csv_response(df, "universities.csv")


@app.get("/universities")
def get_universities_json(
    contains: Optional[str] = Query(None, description="Filter rows where any text column contains this value"),
    country: Optional[str] = Query(None, description="Filter by country (exact, case-insensitive)"),
    region: Optional[str] = Query(None, description="Filter by region (exact, case-insensitive)"),
    name: Optional[str] = Query(None, description="Filter by university name contains (case-insensitive)"),
    limit: Optional[int] = Query(None, ge=0, description="Max rows to return"),
    offset: Optional[int] = Query(None, ge=0, description="Rows to skip"),
    request: Request = None,
):
    path = data / "universities.csv"
    if not path.exists():
        raise HTTPException(status_code=404, detail="universities.csv not found")
    try:
        df = pd.read_csv(path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed reading universities.csv: {e}")

    if country and "country" in df.columns:
        df = df[df["country"].astype(str).str.lower() == country.strip().lower()]
    if region and "region" in df.columns:
        df = df[df["region"].astype(str).str.lower() == region.strip().lower()]
    if name:
        name_col = _resolve_column(df, "name", aliases={"name": ["name", "university"]})
        if name_col:
            df = df[df[name_col].astype(str).str.contains(name, case=False, na=False)]
    if contains:
        mask = pd.Series(False, index=df.index)
        for col in df.columns:
            mask = mask | df[col].astype(str).str.contains(contains, case=False, na=False)
        df = df[mask]
    # Apply dynamic per-column filters
    df = apply_dynamic_filters(
        df,
        request.query_params,
        exclude=RESERVED_PARAMS,
        aliases={
            "name": ["name", "university"],
            "id": ["id", "university id", "university_id"],
        },
    )
    return df_to_json_records(df, limit=limit, offset=offset)


@app.get("/universities/ids")
def get_university_ids_json():
    """Return a compact JSON list of all universities with their IDs: [{id, name}]."""
    path = data / "universities.csv"
    if not path.exists():
        raise HTTPException(status_code=404, detail="universities.csv not found")
    try:
        df = pd.read_csv(path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed reading universities.csv: {e}")

    # Resolve id/name columns flexibly (case-insensitive, common aliases)
    norm = {c.strip().lower(): c for c in df.columns}
    id_candidates = ["id", "university id", "university_id"]
    name_candidates = ["name", "university", "university name", "university_name"]

    id_col = next((norm[n] for n in id_candidates if n in norm), None)
    name_col = next((norm[n] for n in name_candidates if n in norm), None)

    if not id_col or not name_col:
        raise HTTPException(status_code=500, detail="Could not locate university id/name columns")

    out = (
        df[[id_col, name_col]]
        .dropna(subset=[id_col, name_col])
        .drop_duplicates(subset=[id_col])
        .rename(columns={id_col: "id", name_col: "name"})
        .sort_values("name", key=lambda s: s.astype(str).str.lower())
        .reset_index(drop=True)
    )

    return df_to_json_records(out)


@app.get("/mappings.csv")
def get_mappings_csv(
    faculty_id: Optional[str] = Query(None, description="Filter by Faculty ID"),
    university_id: Optional[str] = Query(None, description="Filter by University ID"),
    faculty: Optional[str] = Query(None, description="Filter by Faculty name contains"),
    university: Optional[str] = Query(None, description="Filter by University name contains"),
    request: Request = None,
):
    """Return the master_partner_mappings.csv if available, else merge all mapping CSVs in data/mappings."""
    master = data / "master_partner_mappings.csv"
    dfs: list[pd.DataFrame] = []
    if master.exists():
        try:
            df = pd.read_csv(master)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed reading master_partner_mappings.csv: {e}")
        dfs.append(df)
    else:
        # Merge individual CSVs if present
        if not data.exists():
            raise HTTPException(status_code=404, detail="No mappings directory found")
        for p in sorted(data.glob("*.csv")):
            try:
                dfs.append(pd.read_csv(p))
            except Exception:
                continue
        if not dfs:
            raise HTTPException(status_code=404, detail="No mapping CSVs found to merge")
    df = pd.concat(dfs, ignore_index=True)

    # Apply explicit filters
    if faculty_id and "Faculty ID" in df.columns:
        df = df[df["Faculty ID"].astype(str) == str(faculty_id)]
    if university_id and "University ID" in df.columns:
        df = df[df["University ID"].astype(str) == str(university_id)]
    if faculty and "Faculty" in df.columns:
        df = df[df["Faculty"].astype(str).str.contains(faculty, case=False, na=False)]
    if university and "University" in df.columns:
        df = df[df["University"].astype(str).str.contains(university, case=False, na=False)]
    # Apply dynamic per-column filters
    df = apply_dynamic_filters(df, request.query_params, exclude=RESERVED_PARAMS)
    return df_to_csv_response(df, "mappings.csv")


@app.get("/mappings")
def get_mappings_json(
    faculty_id: Optional[str] = Query(None, description="Filter by Faculty ID"),
    university_id: Optional[str] = Query(None, description="Filter by University ID"),
    faculty: Optional[str] = Query(None, description="Filter by Faculty name contains"),
    university: Optional[str] = Query(None, description="Filter by University name contains"),
    limit: Optional[int] = Query(None, ge=0, description="Max rows to return"),
    offset: Optional[int] = Query(None, ge=0, description="Rows to skip"),
    request: Request = None,
):
    master = data / "master_partner_mappings.csv"
    dfs: list[pd.DataFrame] = []
    if master.exists():
        try:
            df = pd.read_csv(master)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed reading master_partner_mappings.csv: {e}")
        dfs.append(df)
    else:
        if not data.exists():
            raise HTTPException(status_code=404, detail="No mappings directory found")
        for p in sorted(data.glob("*.csv")):
            try:
                dfs.append(pd.read_csv(p))
            except Exception:
                continue
        if not dfs:
            raise HTTPException(status_code=404, detail="No mapping CSVs found to merge")
    df = pd.concat(dfs, ignore_index=True)

    # Apply explicit filters
    if faculty_id and "Faculty ID" in df.columns:
        df = df[df["Faculty ID"].astype(str) == str(faculty_id)]
    if university_id and "University ID" in df.columns:
        df = df[df["University ID"].astype(str) == str(university_id)]
    if faculty and "Faculty" in df.columns:
        df = df[df["Faculty"].astype(str).str.contains(faculty, case=False, na=False)]
    if university and "University" in df.columns:
        df = df[df["University"].astype(str).str.contains(university, case=False, na=False)]
    # Apply dynamic per-column filters
    df = apply_dynamic_filters(df, request.query_params, exclude=RESERVED_PARAMS)
    return df_to_json_records(df, limit=limit, offset=offset)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
