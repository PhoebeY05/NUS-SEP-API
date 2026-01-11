from openpyxl import load_workbook
import pandas as pd
import os

# ========= CONFIG =========
EXCEL_PATH = "SEP-Credit-Transfer-Calculator.xlsx"
OUTPUT_DIR = "data"
OUTPUT_PATH = os.path.join(OUTPUT_DIR, "universities.csv")

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ========= STEP 1: LOAD WORKBOOK =========
wb = load_workbook(EXCEL_PATH, data_only=True)

print("Scanning workbook...")

# ========= STEP 2: EXTRACT HIDDEN SHEETS =========
hidden_csv_paths = {}

for ws in wb.worksheets:
    if ws.sheet_state == "hidden":
        data = []
        for row in ws.iter_rows(values_only=True):
            data.append(row)

        df = pd.DataFrame(data)

        csv_name = ws.title.replace(" ", "_") + ".csv"
        csv_path = os.path.join(OUTPUT_DIR, csv_name)

        df.to_csv(csv_path, index=False, header=False)
        hidden_csv_paths[ws.title] = csv_path

        print(f"✔ Extracted hidden sheet: {ws.title} → {csv_path}")

# ========= STEP 3: LOAD CREDIT SCORES =========
if "Credit Scores" not in hidden_csv_paths:
    raise RuntimeError("Credit Scores sheet not found!")

credit_scores_path = hidden_csv_paths["Credit Scores"]

df = pd.read_csv(credit_scores_path, header=None)

# ========= STEP 4: ASSIGN COLUMN NAMES =========
df.columns = [
    "region",
    "faculty",
    "country",
    "university",
    "partner_credit_value",
    "unit_type",
    "conversion_factor",
    "notes"
]

print("✔ Parsed Credit Scores table")

# ========= STEP 5: EXPORT CLEAN UNIVERSITY LIST =========
universities = (
    df[["region", "country", "university"]]
    .dropna()
    .drop_duplicates()
    .sort_values(["region", "country", "university"])
)

universities_path = os.path.join(OUTPUT_DIR, "universities_by_country.csv")
universities.to_csv(universities_path, index=False)

print(f"✔ Exported university list → {universities_path}")

# ========= STEP 6: OPTIONAL DERIVED DATASETS =========

# Universities by faculty
universities_by_faculty = (
    df[["faculty", "country", "university"]]
    .dropna()
    .drop_duplicates()
    .sort_values(["faculty", "country", "university"])
)

universities_by_faculty_path = os.path.join(
    OUTPUT_DIR, "universities_by_faculty.csv"
)
universities_by_faculty.to_csv(universities_by_faculty_path, index=False)

print(f"✔ Exported universities by faculty → {universities_by_faculty_path}")

# ========= STEP 7: OPTIONAL HELPER FUNCTION =========
def get_universities(faculty: str):
    return (
        df[df["faculty"] == faculty]["university"]
        .dropna()
        .unique()
        .tolist()
    )


# ========= STEP 8: COMBINE DATA =========
# Load both CSVs
universities = pd.read_csv(os.path.join(OUTPUT_DIR, "universities_by_country.csv"))
universities_by_faculty = pd.read_csv(
    os.path.join(OUTPUT_DIR, "universities_by_faculty.csv")
)

# Merge on country + university
combined = universities_by_faculty.merge(
    universities,
    on=["country", "university"],
    how="left"
)

# Reorder columns nicely
combined = combined[["region", "country", "faculty", "university"]]

# Sort for readability
combined = combined.sort_values(
    ["region", "country", "faculty", "university"]
)

# Save combined CSV
combined.to_csv(OUTPUT_PATH, index=False)

print(f"✔ Combined dataset saved → {OUTPUT_PATH}")

# ======== STEP 9: CLEANUP =========
# Files to DELETE
DELETE_FILES = {
    "universities_by_country.csv",
    "universities_by_faculty.csv",
    "Credit_Scores.csv",
    "List.csv"
}

print("Starting cleanup...")

for filename in os.listdir(OUTPUT_DIR):
    file_path = os.path.join(OUTPUT_DIR, filename)

    # Only touch files (ignore folders)
    if os.path.isfile(file_path) and filename in DELETE_FILES:
        os.remove(file_path)
        print(f"🗑 Deleted: {filename}")

print("\n✅ Cleanup complete.")
print("Remaining files:", os.listdir(OUTPUT_DIR))