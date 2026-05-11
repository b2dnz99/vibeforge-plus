#!/usr/bin/env python3
"""
Seed health_metrics table from SAR (sysstat) data.
Extracts CPU%, memory%, swap% at ~10min intervals from SA binary files,
interpolates to 1-second resolution with Gaussian noise, and backports
disk% and db_conns as flat values.

Run once on the VM host (where /var/log/sysstat/sa* files live).

Usage:
    python seed_sar_history.py [--sar-dir /var/log/sysstat] [--force]

Reads DB credentials from environment: POSTGRES_HOST, POSTGRES_PORT,
POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD.
"""
import os
import sys
import random
import subprocess
from datetime import datetime, timezone, timedelta

import psycopg2
import psycopg2.extras


# ── Config ──
SAR_DIR = os.getenv("SAR_DIR", "/var/log/sysstat")
DB_HOST = os.getenv("POSTGRES_HOST", "localhost")
DB_PORT = os.getenv("POSTGRES_PORT", "5432")
DB_NAME = os.getenv("POSTGRES_DB", "vibeforge")
DB_USER = os.getenv("POSTGRES_USER", "vibeforge")
DB_PASS = os.getenv("POSTGRES_PASSWORD", "")
FORCE = "--force" in sys.argv


def parse_sadf(sa_file, flag, value_index):
    """Run sadf -d and extract (timestamp, value) pairs."""
    cmd = ["sadf", "-d", sa_file, "--", flag]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            print(f"  sadf failed for {sa_file} {flag}: {result.stderr.strip()}")
            return []
    except Exception as e:
        print(f"  sadf error for {sa_file}: {e}")
        return []

    points = []
    for line in result.stdout.strip().split("\n"):
        if line.startswith("#") or not line.strip():
            continue
        fields = line.split(";")
        if len(fields) <= value_index:
            continue
        try:
            ts_str = fields[2].strip()  # "2026-04-01 00:10:02 UTC"
            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S %Z").replace(tzinfo=timezone.utc)
            val = float(fields[value_index])
            points.append((ts, val))
        except (ValueError, IndexError):
            continue
    return points


def interpolate(anchors, noise_std, clamp_min=0, clamp_max=100):
    """Linearly interpolate between anchor points to 1s resolution with Gaussian noise."""
    if len(anchors) < 2:
        return []
    rows = []
    for i in range(len(anchors) - 1):
        t0, v0 = anchors[i]
        t1, v1 = anchors[i + 1]
        gap_seconds = int((t1 - t0).total_seconds())
        if gap_seconds <= 0:
            continue
        for s in range(gap_seconds):
            frac = s / gap_seconds
            val = v0 + (v1 - v0) * frac + random.gauss(0, noise_std)
            val = max(clamp_min, min(clamp_max, val))
            ts = t0 + timedelta(seconds=s)
            rows.append((ts, round(val, 1)))
    # Add the last anchor
    rows.append((anchors[-1][0], round(anchors[-1][1], 1)))
    return rows


def main():
    # Discover SA files
    sa_files = []
    for f in sorted(os.listdir(SAR_DIR)):
        if f.startswith("sa") and not f.startswith("sar") and f[2:].isdigit():
            sa_files.append(os.path.join(SAR_DIR, f))

    if not sa_files:
        print(f"No SA files found in {SAR_DIR}")
        sys.exit(1)

    print(f"Found {len(sa_files)} SA files: {[os.path.basename(f) for f in sa_files]}")

    # Extract anchors from all SA files
    cpu_anchors = []
    mem_anchors = []
    swap_anchors = []

    for sa_file in sa_files:
        name = os.path.basename(sa_file)
        print(f"Extracting {name}...")
        # CPU: %idle is field index 9, CPU% = 100 - %idle
        cpu_raw = parse_sadf(sa_file, "-u", 9)
        cpu_anchors.extend([(ts, 100.0 - val) for ts, val in cpu_raw])
        # Memory: %memused is field index 6
        mem_anchors.extend(parse_sadf(sa_file, "-r", 6))
        # Swap: %swpused is field index 5
        swap_anchors.extend(parse_sadf(sa_file, "-S", 5))

    # Sort by timestamp
    cpu_anchors.sort(key=lambda x: x[0])
    mem_anchors.sort(key=lambda x: x[0])
    swap_anchors.sort(key=lambda x: x[0])

    print(f"Anchor points: CPU={len(cpu_anchors)}, Memory={len(mem_anchors)}, Swap={len(swap_anchors)}")

    if not cpu_anchors:
        print("No CPU data extracted. Aborting.")
        sys.exit(1)

    # Time range
    t_start = cpu_anchors[0][0]
    t_end = cpu_anchors[-1][0]
    total_seconds = int((t_end - t_start).total_seconds())
    print(f"Time range: {t_start.isoformat()} to {t_end.isoformat()} ({total_seconds}s = {total_seconds/3600:.1f}h)")

    # Interpolate to 1s
    print("Interpolating CPU (sigma=2.0)...")
    cpu_rows = interpolate(cpu_anchors, noise_std=2.0)
    print(f"  {len(cpu_rows)} points")

    print("Interpolating Memory (sigma=0.5)...")
    mem_rows = interpolate(mem_anchors, noise_std=0.5)
    print(f"  {len(mem_rows)} points")

    print("Interpolating Swap (sigma=0.2)...")
    swap_rows = interpolate(swap_anchors, noise_std=0.2, clamp_min=0, clamp_max=100)
    print(f"  {len(swap_rows)} points")

    # Build timestamp-keyed lookup for mem and swap
    mem_lookup = {ts.isoformat()[:19]: val for ts, val in mem_rows}
    swap_lookup = {ts.isoformat()[:19]: val for ts, val in swap_rows}

    # Build final rows using CPU timestamps as the master
    print("Building final rows with backported disk + db_conns...")
    batch = []
    for ts, cpu_val in cpu_rows:
        key = ts.isoformat()[:19]
        mem_val = mem_lookup.get(key, 12.5)
        swap_val = swap_lookup.get(key, 0.01)
        disk_val = round(max(0, min(100, 42.0 + random.gauss(0, 0.1))), 1)
        db_conns = max(1, min(8, int(4 + random.gauss(0, 0.8))))
        batch.append((ts, cpu_val, mem_val, disk_val, swap_val, db_conns))

    print(f"Total rows to insert: {len(batch)}")

    # Connect to DB
    print(f"Connecting to {DB_HOST}:{DB_PORT}/{DB_NAME}...")
    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT,
        dbname=DB_NAME, user=DB_USER, password=DB_PASS,
        connect_timeout=10,
    )
    cur = conn.cursor()

    # Ensure table exists
    cur.execute("""
        CREATE TABLE IF NOT EXISTS health_metrics (
            ts        TIMESTAMPTZ PRIMARY KEY,
            cpu       REAL NOT NULL,
            memory    REAL NOT NULL,
            disk      REAL NOT NULL,
            swap      REAL NOT NULL,
            db_conns  SMALLINT NOT NULL
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_health_metrics_ts ON health_metrics (ts DESC)")
    conn.commit()

    # Check existing data
    cur.execute("SELECT count(*) FROM health_metrics WHERE ts BETWEEN %s AND %s", (t_start, t_end))
    existing = cur.fetchone()[0]
    if existing > 0 and not FORCE:
        print(f"ERROR: {existing} rows already exist in the seed time range.")
        print("Use --force to overwrite (ON CONFLICT DO NOTHING).")
        sys.exit(1)

    # Batch insert
    PAGE_SIZE = 10000
    inserted = 0
    for i in range(0, len(batch), PAGE_SIZE):
        page = batch[i:i + PAGE_SIZE]
        psycopg2.extras.execute_values(
            cur,
            """INSERT INTO health_metrics (ts, cpu, memory, disk, swap, db_conns)
               VALUES %s ON CONFLICT (ts) DO NOTHING""",
            page,
            page_size=PAGE_SIZE,
        )
        conn.commit()
        inserted += len(page)
        pct = min(100, int(inserted / len(batch) * 100))
        print(f"  Inserted {inserted}/{len(batch)} ({pct}%)")

    cur.close()
    conn.close()
    print(f"Done. Seeded {len(batch)} rows from {t_start.date()} to {t_end.date()}.")


if __name__ == "__main__":
    main()
