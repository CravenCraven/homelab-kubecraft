#!/usr/bin/env python3
"""Load the Application Tracker sheet into the applications database.

The sheet is the source of truth, so every run replaces the whole table.
That happens in one transaction, so Grafana never catches it half empty.
The table creates itself on first run.
"""

import os
from datetime import date, datetime, timedelta

import psycopg
from google.oauth2 import service_account
from googleapiclient.discovery import build

KEY_FILE = os.environ.get("GOOGLE_KEY_FILE", os.path.expanduser("~/secrets/scorecard-sa.json"))
SHEET_ID = "1bgNF8Sg2YgFlAhgaWkCz062B4grPh2wdq9zOmZqwgn0"
CELL_RANGE = "Applications!A2:I"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
  id          bigserial PRIMARY KEY,
  applied_on  date NOT NULL,
  role        text NOT NULL,
  company     text NOT NULL,
  link        text,
  work_mode   text,
  salary_min  integer,
  salary_max  integer,
  status      text NOT NULL DEFAULT 'Applied',
  notes       text
);
CREATE INDEX IF NOT EXISTS applications_applied_on_idx ON applications (applied_on);
"""


def read_sheet():
    creds = service_account.Credentials.from_service_account_file(KEY_FILE, scopes=SCOPES)
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False).spreadsheets()
    response = (
        sheets.values()
        .get(
            spreadsheetId=SHEET_ID,
            range=CELL_RANGE,
            # Without this, a date column comes back as 46342 instead of 2026-11-16.
            valueRenderOption="FORMATTED_VALUE",
        )
        .execute()
    )
    return response.get("values", [])


def cell(row, i):
    """Trailing empty cells are missing from the API response, not blank."""
    return row[i].strip() if i < len(row) and row[i] else ""


def to_int(value):
    digits = "".join(c for c in value if c.isdigit())
    return int(digits) if digits else None


def to_date(value):
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    # Sheets hands back a date serial when the column has no date format:
    # days since 1899-12-30.
    if value.isdigit():
        serial = int(value)
        if 20000 < serial < 80000:
            return date(1899, 12, 30) + timedelta(days=serial)
    return None


def to_rows(values):
    rows, skipped = [], 0
    for raw in values:
        applied_on = to_date(cell(raw, 0))
        role = cell(raw, 1)
        company = cell(raw, 2)
        if not applied_on or not role or not company:
            skipped += 1
            continue
        rows.append(
            (
                applied_on,
                role,
                company,
                cell(raw, 3) or None,
                cell(raw, 4) or None,
                to_int(cell(raw, 5)),
                to_int(cell(raw, 6)),
                cell(raw, 7) or "Applied",
                cell(raw, 8) or None,
            )
        )
    return rows, skipped


def main():
    dsn = os.environ.get("APPLICATIONS_DSN")
    if not dsn:
        raise SystemExit("Set APPLICATIONS_DSN first.")

    rows, skipped = to_rows(read_sheet())

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
            cur.execute("TRUNCATE applications RESTART IDENTITY")
            if rows:
                cur.executemany(
                    """
                    INSERT INTO applications
                        (applied_on, role, company, link, work_mode,
                         salary_min, salary_max, status, notes)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    rows,
                )

    print(f"loaded {len(rows)} applications")
    if skipped:
        print(f"skipped {skipped} incomplete row(s)")


if __name__ == "__main__":
    main()
