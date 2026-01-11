from io import StringIO
from pathlib import Path
from typing import Optional

import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse, StreamingResponse

app = FastAPI(title="SEP EduRec CSV API", version="0.1.0")


# Base paths
ROOT = Path(__file__).parent
data = ROOT / "data"
MAPPINGS_DIR = data / "mappings"


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
    return df.to_dict(orient="records")


@app.get("/health", response_class=PlainTextResponse)
def health() -> str:
    return "ok"


@app.get("/faculties.csv")
def get_faculties_csv():
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
    return df_to_csv_response(df, "faculties.csv")


@app.get("/faculties")
def get_faculties_json(
    limit: Optional[int] = Query(None, ge=0, description="Max rows to return"),
    offset: Optional[int] = Query(None, ge=0, description="Rows to skip"),
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
    return df_to_json_records(df, limit=limit, offset=offset)


@app.get("/organisations.csv")
def get_organisations_csv(
    org: Optional[str] = Query(None, description="Filter by organisation type, e.g., SCHL"),
    search: Optional[str] = Query(None, description="Filter by search name contains (case-insensitive)"),
    contains: Optional[str] = Query(None, description="Filter rows where any text column contains this value"),
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

    return df_to_csv_response(df, "organisations.csv")


@app.get("/organisations")
def get_organisations_json(
    org: Optional[str] = Query(None, description="Filter by organisation type, e.g., SCHL"),
    search: Optional[str] = Query(None, description="Filter by search name contains (case-insensitive)"),
    contains: Optional[str] = Query(None, description="Filter rows where any text column contains this value"),
    limit: Optional[int] = Query(None, ge=0, description="Max rows to return"),
    offset: Optional[int] = Query(None, ge=0, description="Rows to skip"),
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

    return df_to_json_records(df, limit=limit, offset=offset)


@app.get("/universities.csv")
def get_universities_csv(
    contains: Optional[str] = Query(None, description="Filter rows where any text column contains this value"),
    country: Optional[str] = Query(None, description="Filter by country (exact, case-insensitive)"),
    region: Optional[str] = Query(None, description="Filter by region (exact, case-insensitive)"),
    name: Optional[str] = Query(None, description="Filter by university name contains (case-insensitive)"),
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
    if name and "name" in df.columns:
        df = df[df["name"].astype(str).str.contains(name, case=False, na=False)]
    if contains:
        mask = pd.Series(False, index=df.index)
        for col in df.columns:
            mask = mask | df[col].astype(str).str.contains(contains, case=False, na=False)
        df = df[mask]

    return df_to_csv_response(df, "universities.csv")


@app.get("/universities")
def get_universities_json(
    contains: Optional[str] = Query(None, description="Filter rows where any text column contains this value"),
    country: Optional[str] = Query(None, description="Filter by country (exact, case-insensitive)"),
    region: Optional[str] = Query(None, description="Filter by region (exact, case-insensitive)"),
    name: Optional[str] = Query(None, description="Filter by university name contains (case-insensitive)"),
    limit: Optional[int] = Query(None, ge=0, description="Max rows to return"),
    offset: Optional[int] = Query(None, ge=0, description="Rows to skip"),
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
    if name and "name" in df.columns:
        df = df[df["name"].astype(str).str.contains(name, case=False, na=False)]
    if contains:
        mask = pd.Series(False, index=df.index)
        for col in df.columns:
            mask = mask | df[col].astype(str).str.contains(contains, case=False, na=False)
        df = df[mask]

    return df_to_json_records(df, limit=limit, offset=offset)


@app.get("/mappings.csv")
def get_mappings_csv(
    faculty_id: Optional[str] = Query(None, description="Filter by Faculty ID"),
    university_id: Optional[str] = Query(None, description="Filter by University ID"),
    faculty: Optional[str] = Query(None, description="Filter by Faculty name contains"),
    university: Optional[str] = Query(None, description="Filter by University name contains"),
):
    """Return the master_partner_mappings.csv if available, else merge all mapping CSVs in data/mappings."""
    master = MAPPINGS_DIR / "master_partner_mappings.csv"
    dfs: list[pd.DataFrame] = []
    if master.exists():
        try:
            df = pd.read_csv(master)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed reading master_partner_mappings.csv: {e}")
        dfs.append(df)
    else:
        # Merge individual CSVs if present
        if not MAPPINGS_DIR.exists():
            raise HTTPException(status_code=404, detail="No mappings directory found")
        for p in sorted(MAPPINGS_DIR.glob("*.csv")):
            try:
                dfs.append(pd.read_csv(p))
            except Exception:
                continue
        if not dfs:
            raise HTTPException(status_code=404, detail="No mapping CSVs found to merge")
    df = pd.concat(dfs, ignore_index=True)

    # Apply filters if provided
    if faculty_id and "Faculty ID" in df.columns:
        df = df[df["Faculty ID"].astype(str) == str(faculty_id)]
    if university_id and "University ID" in df.columns:
        df = df[df["University ID"].astype(str) == str(university_id)]
    if faculty and "Faculty" in df.columns:
        df = df[df["Faculty"].astype(str).str.contains(faculty, case=False, na=False)]
    if university and "University" in df.columns:
        df = df[df["University"].astype(str).str.contains(university, case=False, na=False)]

    return df_to_csv_response(df, "mappings.csv")


@app.get("/mappings")
def get_mappings_json(
    faculty_id: Optional[str] = Query(None, description="Filter by Faculty ID"),
    university_id: Optional[str] = Query(None, description="Filter by University ID"),
    faculty: Optional[str] = Query(None, description="Filter by Faculty name contains"),
    university: Optional[str] = Query(None, description="Filter by University name contains"),
    limit: Optional[int] = Query(None, ge=0, description="Max rows to return"),
    offset: Optional[int] = Query(None, ge=0, description="Rows to skip"),
):
    master = MAPPINGS_DIR / "master_partner_mappings.csv"
    dfs: list[pd.DataFrame] = []
    if master.exists():
        try:
            df = pd.read_csv(master)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed reading master_partner_mappings.csv: {e}")
        dfs.append(df)
    else:
        if not MAPPINGS_DIR.exists():
            raise HTTPException(status_code=404, detail="No mappings directory found")
        for p in sorted(MAPPINGS_DIR.glob("*.csv")):
            try:
                dfs.append(pd.read_csv(p))
            except Exception:
                continue
        if not dfs:
            raise HTTPException(status_code=404, detail="No mapping CSVs found to merge")
    df = pd.concat(dfs, ignore_index=True)

    if faculty_id and "Faculty ID" in df.columns:
        df = df[df["Faculty ID"].astype(str) == str(faculty_id)]
    if university_id and "University ID" in df.columns:
        df = df[df["University ID"].astype(str) == str(university_id)]
    if faculty and "Faculty" in df.columns:
        df = df[df["Faculty"].astype(str).str.contains(faculty, case=False, na=False)]
    if university and "University" in df.columns:
        df = df[df["University"].astype(str).str.contains(university, case=False, na=False)]

    return df_to_json_records(df, limit=limit, offset=offset)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
