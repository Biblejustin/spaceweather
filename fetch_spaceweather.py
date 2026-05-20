#!/usr/bin/env python3
"""Fetch space weather indices into a local SQLite database.

Two sources, both freely distributed in plain-text form:

  SILSO daily sunspot numbers (Royal Observatory of Belgium, v2.0 series):
    https://www.sidc.be/SILSO/datafiles
    Daily total sunspot number, 1818-01-01 → today. Pre-1849 is sparse
    (many days have no observation; -1 in the catalog).

  GFZ Potsdam Kp/ap/Ap + SN + F10.7 (Helmholtz Centre / Niemegk Observatory):
    https://kp.gfz.de/en/data
    Daily geomagnetic indices and solar indicators, 1932-01-01 → today.

Re-running refreshes the most recent year (which is preliminary) and
backfills any gaps. Idempotent on the date key.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

SILSO_URL = "https://www.sidc.be/SILSO/INFO/sndtotcsv.php"
GFZ_URL = "https://kp.gfz.de/fileadmin/files_for_gfz_cms/Kp_ap_Ap_SN_F107_since_1932.txt"

SCHEMA = """
CREATE TABLE IF NOT EXISTS silso_daily (
    date_iso        TEXT PRIMARY KEY,
    year            INTEGER NOT NULL,
    month           INTEGER NOT NULL,
    day             INTEGER NOT NULL,
    decimal_year    REAL,
    sunspot_number  INTEGER,
    std_dev         REAL,
    n_obs           INTEGER,
    definitive      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_silso_year ON silso_daily(year);

CREATE TABLE IF NOT EXISTS gfz_daily (
    date_iso        TEXT PRIMARY KEY,
    year            INTEGER NOT NULL,
    month           INTEGER NOT NULL,
    day             INTEGER NOT NULL,
    bsr             INTEGER,
    db              INTEGER,
    kp1 REAL, kp2 REAL, kp3 REAL, kp4 REAL,
    kp5 REAL, kp6 REAL, kp7 REAL, kp8 REAL,
    ap1 INTEGER, ap2 INTEGER, ap3 INTEGER, ap4 INTEGER,
    ap5 INTEGER, ap6 INTEGER, ap7 INTEGER, ap8 INTEGER,
    ap_daily        INTEGER,
    sunspot_number  REAL,
    f107_obs        REAL,
    f107_adj        REAL,
    definitiveness  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_gfz_year ON gfz_daily(year);

CREATE TABLE IF NOT EXISTS fetch_log (
    source      TEXT PRIMARY KEY,
    fetched_at  INTEGER NOT NULL,
    rows        INTEGER NOT NULL,
    note        TEXT
);
"""


def download(url: str, timeout: int = 180) -> str:
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.text


def parse_silso(text: str) -> list[tuple]:
    """SILSO daily total sunspot CSV is semicolon-separated.

    Columns: year; month; day; decimal_year; daily_total; std_dev;
             num_observations; definitive_flag

    A daily_total of -1 marks days with no observation.
    """
    rows: list[tuple] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(";")]
        if len(parts) < 8:
            continue
        try:
            y = int(parts[0]); m = int(parts[1]); d = int(parts[2])
            dy = float(parts[3])
            sn = int(parts[4])
            sd = float(parts[5])
            nobs = int(parts[6])
            defn = int(parts[7])
        except ValueError:
            continue
        date_iso = f"{y:04d}-{m:02d}-{d:02d}"
        rows.append((date_iso, y, m, d, dy, sn, sd, nobs, defn))
    return rows


def parse_gfz(text: str) -> list[tuple]:
    """GFZ file: comment lines start with '#'. Data lines are whitespace-
    separated, 28 columns:

      YYYY MM DD days days_m Bsr dB
      Kp1..Kp8 (8 cols)  ap1..ap8 (8 cols)
      Ap SN F10.7obs F10.7adj D

    Missing data: -1.000 (Kp), -1 (ap, SN), -1.0 (F10.7).
    """
    rows: list[tuple] = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 28:
            continue
        try:
            y = int(parts[0]); m = int(parts[1]); d = int(parts[2])
            # parts[3] = days, parts[4] = days_m  (we don't store these)
            bsr = int(parts[5])
            db = int(parts[6])
            kp = [float(parts[7 + i]) for i in range(8)]
            ap = [int(parts[15 + i]) for i in range(8)]
            ap_daily = int(parts[23])
            sn = float(parts[24])
            f107_obs = float(parts[25])
            f107_adj = float(parts[26])
            defin = int(parts[27])
        except (ValueError, IndexError):
            continue
        date_iso = f"{y:04d}-{m:02d}-{d:02d}"
        rows.append((
            date_iso, y, m, d, bsr, db,
            *kp, *ap,
            ap_daily, sn, f107_obs, f107_adj, defin,
        ))
    return rows


def upsert_silso(conn: sqlite3.Connection, rows: list[tuple]) -> int:
    conn.executemany(
        "INSERT OR REPLACE INTO silso_daily "
        "(date_iso, year, month, day, decimal_year, sunspot_number, "
        " std_dev, n_obs, definitive) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def upsert_gfz(conn: sqlite3.Connection, rows: list[tuple]) -> int:
    conn.executemany(
        "INSERT OR REPLACE INTO gfz_daily "
        "(date_iso, year, month, day, bsr, db, "
        " kp1, kp2, kp3, kp4, kp5, kp6, kp7, kp8, "
        " ap1, ap2, ap3, ap4, ap5, ap6, ap7, ap8, "
        " ap_daily, sunspot_number, f107_obs, f107_adj, definitiveness) "
        "VALUES (?, ?, ?, ?, ?, ?,"
        " ?, ?, ?, ?, ?, ?, ?, ?,"
        " ?, ?, ?, ?, ?, ?, ?, ?,"
        " ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def record_fetch(conn, source: str, rows: int, note: str = "") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO fetch_log (source, fetched_at, rows, note) "
        "VALUES (?, ?, ?, ?)",
        (source, int(time.time()), rows, note),
    )


def fetch_silso(conn) -> int:
    print("SILSO daily sunspot numbers... ", end="", flush=True)
    text = download(SILSO_URL)
    rows = parse_silso(text)
    n = upsert_silso(conn, rows)
    record_fetch(conn, "silso_daily", n, SILSO_URL)
    conn.commit()
    print(f"{n:,} rows")
    return n


def fetch_gfz(conn) -> int:
    print("GFZ Kp/ap/Ap + SN + F10.7... ", end="", flush=True)
    text = download(GFZ_URL)
    rows = parse_gfz(text)
    n = upsert_gfz(conn, rows)
    record_fetch(conn, "gfz_daily", n, GFZ_URL)
    conn.commit()
    print(f"{n:,} rows")
    return n


def summarize(conn) -> None:
    n_silso = conn.execute("SELECT COUNT(*) FROM silso_daily").fetchone()[0]
    n_silso_obs = conn.execute(
        "SELECT COUNT(*) FROM silso_daily WHERE sunspot_number >= 0"
    ).fetchone()[0]
    if n_silso:
        e_silso, l_silso = conn.execute(
            "SELECT MIN(date_iso), MAX(date_iso) FROM silso_daily"
        ).fetchone()
        print(f"\nSILSO span:    {e_silso} → {l_silso}")
        print(f"SILSO rows:    {n_silso:,} ({n_silso_obs:,} with observation)")

    n_gfz = conn.execute("SELECT COUNT(*) FROM gfz_daily").fetchone()[0]
    if n_gfz:
        e_gfz, l_gfz = conn.execute(
            "SELECT MIN(date_iso), MAX(date_iso) FROM gfz_daily"
        ).fetchone()
        print(f"GFZ span:      {e_gfz} → {l_gfz}")
        print(f"GFZ rows:      {n_gfz:,}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--db",
        default=str(Path(__file__).parent / "spaceweather.sqlite"),
        help="SQLite database path",
    )
    ap.add_argument(
        "--skip-silso", action="store_true", help="Skip SILSO download"
    )
    ap.add_argument(
        "--skip-gfz", action="store_true", help="Skip GFZ download"
    )
    args = ap.parse_args()

    print(f"Database: {args.db}")
    conn = sqlite3.connect(args.db)
    conn.executescript(SCHEMA)
    conn.commit()

    if not args.skip_silso:
        fetch_silso(conn)
    if not args.skip_gfz:
        fetch_gfz(conn)

    summarize(conn)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
