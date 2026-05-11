"""
VibeForge+ Health Dashboard
Standalone service — auto-discovers VM, Docker, Postgres, Caddy stats.
Time-series stored in Postgres. Synthetic fill between polls. Outage backfill on startup.
Organic discovery: TLS certs, scheduled jobs (cron + systemd timers), network I/O.
"""
import os
import re
import json
import subprocess
import time
import platform
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

import psutil
import psycopg2
import psycopg2.pool
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

app = FastAPI(title="VibeForge+ Health Dashboard")
templates = Jinja2Templates(directory="templates")

# ── Config from environment ──
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "db")
POSTGRES_PORT = os.getenv("POSTGRES_PORT", "5432")
POSTGRES_DB = os.getenv("POSTGRES_DB", "vibeforge")
POSTGRES_USER = os.getenv("POSTGRES_USER", "vibeforge")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "")
DOCKER_SOCKET = os.getenv("DOCKER_SOCKET", "unix:///var/run/docker.sock")

SYNTHETIC_INTERVAL = 5          # seconds between synthetic fill points
OUTAGE_THRESHOLD = 30           # seconds of silence before declaring outage
MAX_BACKFILL_DAYS = 7           # cap outage backfill to retention window

# ── Cached state (for instant /full and /summary responses) ──
_cache = {
    "vm": {}, "docker": {}, "postgres": {}, "caddy": {}, "tls": {},
    "network": {}, "scheduled_jobs": {},
    "last_poll": None, "errors": [],
}

# Last real collection values + timestamp (for synthetic fill)
_last_real = {
    "ts": None, "cpu": 0, "memory": 0, "disk": 0, "swap": 0, "db_conns": 0,
    "net_in": 0, "net_out": 0, "req_per_sec": 0,
}
_last_real_lock = threading.Lock()

# For req/s rate calculation (Caddy counters)
_prev_caddy_requests = None
_prev_caddy_time = None

# ── DB connection pool for metrics storage ──
_db_pool = None


def _get_pool():
    global _db_pool
    if _db_pool is None:
        _db_pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1, maxconn=5,
            host=POSTGRES_HOST, port=POSTGRES_PORT,
            dbname=POSTGRES_DB, user=POSTGRES_USER,
            password=POSTGRES_PASSWORD, connect_timeout=5,
        )
    return _db_pool


def _ensure_table():
    """Create health_metrics table if it doesn't exist."""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS health_metrics (
                ts           TIMESTAMPTZ PRIMARY KEY,
                cpu          REAL,
                memory       REAL,
                disk         REAL,
                swap         REAL,
                db_conns     SMALLINT,
                synthetic    BOOLEAN NOT NULL DEFAULT FALSE,
                outage       BOOLEAN NOT NULL DEFAULT FALSE,
                net_bytes_in  BIGINT,
                net_bytes_out BIGINT,
                req_per_sec  REAL,
                event        TEXT
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_health_metrics_ts
            ON health_metrics (ts DESC)
        """)
        # Forward-compat: existing installs may predate the event column. Add it if missing.
        cur.execute("""
            ALTER TABLE health_metrics ADD COLUMN IF NOT EXISTS event TEXT
        """)
        conn.commit()
        cur.close()
        print("health_metrics table ready")
    except Exception as e:
        conn.rollback()
        print(f"Failed to create health_metrics table: {e}")
    finally:
        pool.putconn(conn)


_last_insert_ts = 0


def _insert_metric(ts_iso, cpu, memory, disk, swap, db_conns,
                    net_in=None, net_out=None, req_s=None,
                    synthetic=False, outage=False, event=None):
    """Insert one metric row. ON CONFLICT DO NOTHING for idempotency."""
    global _last_insert_ts
    now = time.time()
    if now - _last_insert_ts < 1 and not event:
        return
    _last_insert_ts = now
    try:
        pool = _get_pool()
        conn = pool.getconn()
        try:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO health_metrics
                   (ts, cpu, memory, disk, swap, db_conns, net_bytes_in, net_bytes_out, req_per_sec, synthetic, outage, event)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (ts) DO NOTHING""",
                (ts_iso, cpu, memory, disk, swap, db_conns, net_in, net_out, req_s, synthetic, outage, event)
            )
            conn.commit()
            cur.close()
        finally:
            pool.putconn(conn)
    except Exception as e:
        print(f"Metric insert failed: {e}")


def _bulk_insert_outage(start_ts, end_ts):
    """Backfill outage rows at 5s intervals between start and end timestamps."""
    rows = []
    current = start_ts + timedelta(seconds=SYNTHETIC_INTERVAL)
    while current < end_ts:
        rows.append((current, None, None, None, None, None, None, None, None, True, True))
        current += timedelta(seconds=SYNTHETIC_INTERVAL)
    if not rows:
        return 0
    try:
        pool = _get_pool()
        conn = pool.getconn()
        try:
            cur = conn.cursor()
            args = ",".join(
                cur.mogrify("(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", r).decode()
                for r in rows
            )
            cur.execute(
                f"INSERT INTO health_metrics (ts, cpu, memory, disk, swap, db_conns, net_bytes_in, net_bytes_out, req_per_sec, synthetic, outage) "
                f"VALUES {args} ON CONFLICT (ts) DO NOTHING"
            )
            conn.commit()
            inserted = cur.rowcount
            cur.close()
            return inserted
        finally:
            pool.putconn(conn)
    except Exception as e:
        print(f"Outage backfill failed: {e}")
        return 0


def _detect_and_backfill_outage():
    """On startup: check last row in health_metrics, backfill outage if gap detected."""
    try:
        pool = _get_pool()
        conn = pool.getconn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT ts, cpu, memory, disk, swap, db_conns FROM health_metrics ORDER BY ts DESC LIMIT 1")
            row = cur.fetchone()
            cur.close()
        finally:
            pool.putconn(conn)

        if not row:
            print("No prior metrics — fresh start, no outage to backfill")
            return

        last_ts = row[0]
        now = datetime.now(timezone.utc)
        gap_seconds = (now - last_ts).total_seconds()

        if gap_seconds < OUTAGE_THRESHOLD:
            print(f"Last metric {gap_seconds:.0f}s ago — no outage detected")
            with _last_real_lock:
                _last_real["ts"] = last_ts
                _last_real["cpu"] = row[1] or 0
                _last_real["memory"] = row[2] or 0
                _last_real["disk"] = row[3] or 0
                _last_real["swap"] = row[4] or 0
                _last_real["db_conns"] = row[5] or 0
            return

        max_backfill = timedelta(days=MAX_BACKFILL_DAYS)
        if now - last_ts > max_backfill:
            backfill_start = now - max_backfill
            print(f"Outage exceeds {MAX_BACKFILL_DAYS}d — backfilling from {backfill_start.isoformat()}")
        else:
            backfill_start = last_ts
            print(f"Outage detected: {gap_seconds:.0f}s ({gap_seconds/60:.1f}min). Backfilling from {last_ts.isoformat()}")

        inserted = _bulk_insert_outage(backfill_start, now)
        print(f"Outage backfill: {inserted} rows inserted")

        # Tag outage start and end with activity events
        duration = gap_seconds
        if duration >= 3600:
            dur_str = f"{int(duration/3600)}h {int((duration%3600)/60)}m"
        elif duration >= 60:
            dur_str = f"{int(duration/60)}m {int(duration%60)}s"
        else:
            dur_str = f"{int(duration)}s"

        try:
            pool2 = _get_pool()
            conn2 = pool2.getconn()
            try:
                cur2 = conn2.cursor()
                # Tag the first outage row with "offline" event
                cur2.execute(
                    """UPDATE health_metrics SET event = %s
                       WHERE ts = (SELECT ts FROM health_metrics WHERE outage = true AND ts > %s ORDER BY ts LIMIT 1)
                       AND event IS NULL""",
                    (f"error:Health monitoring offline", backfill_start.isoformat())
                )
                # Tag the last outage row with "restored" event
                cur2.execute(
                    """UPDATE health_metrics SET event = %s
                       WHERE ts = (SELECT ts FROM health_metrics WHERE outage = true AND ts > %s ORDER BY ts DESC LIMIT 1)
                       AND event IS NULL""",
                    (f"ok:Health monitoring restored ({dur_str} offline)", backfill_start.isoformat())
                )
                conn2.commit()
                cur2.close()
            finally:
                pool2.putconn(conn2)
            print(f"Outage events tagged: offline + restored ({dur_str})")
        except Exception as e2:
            print(f"Outage event tagging failed: {e2}")

    except Exception as e:
        print(f"Outage detection failed: {e}")


# ── Range config ──

RANGE_SECONDS = {
    # Live window: 10 minutes. Tested 5m/10m/15m/30m — 10m is the sweet spot.
    # Shorter (5m) compresses spikes into too few pixels, losing shape.
    # Longer (15m+) stretches data too thin, graphs look flat and inorganic.
    # At 10m with 5s synthetic fill, ~120 points fill the graph width naturally
    # and CPU/memory spikes display with recognisable peaks and valleys.
    "live": 600,        # 10 minutes
    "1h": 3600,         # 1 hour
    "2h": 7200,         # 2 hours
    "6h": 21600,        # 6 hours
    "12h": 43200,       # 12 hours
    "24h": 86400,       # 24 hours
    "3d": 259200,       # 3 days
    "7d": 604800,       # 7 days
}
MAX_DISPLAY = 1500


def _downsample(data, target_points):
    """Average numeric data down to target_points for graph rendering."""
    if not data or len(data) <= target_points:
        return data
    n = len(data)
    step = n / target_points
    result = [None] * target_points
    for i in range(target_points):
        start = int(i * step)
        end = min(int((i + 1) * step), n)
        total = 0
        count = 0
        for j in range(start, end):
            v = data[j]
            if v is not None:
                total += v
                count += 1
        result[i] = round(total / count, 2) if count > 0 else None
    return result


def _downsample_timestamps(timestamps, target_points):
    if not timestamps or len(timestamps) <= target_points:
        return timestamps
    n = len(timestamps)
    step = n / target_points
    return [timestamps[int(i * step)] for i in range(target_points)]


def _downsample_flags(flags, target_points):
    if not flags or len(flags) <= target_points:
        return flags
    n = len(flags)
    step = n / target_points
    result = [False] * target_points
    for i in range(target_points):
        start = int(i * step)
        end = min(int((i + 1) * step), n)
        for j in range(start, end):
            if flags[j]:
                result[i] = True
                break
    return result


def _query_history(range_key):
    """Query health_metrics for a time range. Returns frontend-compatible dict with outage flags."""
    seconds = RANGE_SECONDS.get(range_key, RANGE_SECONDS["24h"])
    try:
        pool = _get_pool()
        conn = pool.getconn()
        try:
            cur = conn.cursor()
            cur.execute(
                """SELECT ts, cpu, memory, disk, swap, db_conns, outage, net_bytes_in, net_bytes_out, req_per_sec
                   FROM health_metrics
                   WHERE ts > now() - interval '%s seconds'
                   ORDER BY ts""",
                (seconds,)
            )
            rows = cur.fetchall()
            cur.close()
        finally:
            pool.putconn(conn)
    except Exception as e:
        print(f"History query failed: {e}")
        return {"timestamps": [], "cpu": [], "memory": [], "disk": [], "swap": [],
                "db_connections": [], "outage": [], "net_in": [], "net_out": [], "req_per_sec": []}

    if not rows:
        return {"timestamps": [], "cpu": [], "memory": [], "disk": [], "swap": [],
                "db_connections": [], "outage": [], "net_in": [], "net_out": [], "req_per_sec": []}

    timestamps = [r[0].isoformat() for r in rows]
    cpu = [r[1] for r in rows]
    memory = [r[2] for r in rows]
    disk = [r[3] for r in rows]
    swap = [r[4] for r in rows]
    db_connections = [r[5] for r in rows]
    outage = [r[6] for r in rows]
    net_in = [r[7] for r in rows]
    net_out = [r[8] for r in rows]
    req_per_sec = [r[9] for r in rows]

    if len(rows) > MAX_DISPLAY:
        timestamps = _downsample_timestamps(timestamps, MAX_DISPLAY)
        cpu = _downsample(cpu, MAX_DISPLAY)
        memory = _downsample(memory, MAX_DISPLAY)
        disk = _downsample(disk, MAX_DISPLAY)
        swap = _downsample(swap, MAX_DISPLAY)
        db_connections = _downsample(db_connections, MAX_DISPLAY)
        outage = _downsample_flags(outage, MAX_DISPLAY)
        net_in = _downsample(net_in, MAX_DISPLAY)
        net_out = _downsample(net_out, MAX_DISPLAY)
        req_per_sec = _downsample(req_per_sec, MAX_DISPLAY)

    return {
        "timestamps": timestamps, "cpu": cpu, "memory": memory, "disk": disk,
        "swap": swap, "db_connections": db_connections, "outage": outage,
        "net_in": net_in, "net_out": net_out, "req_per_sec": req_per_sec,
    }


def _prune_old_metrics():
    """Delete metrics older than 7 days. Runs once daily."""
    try:
        pool = _get_pool()
        conn = pool.getconn()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM health_metrics WHERE ts < now() - interval '7 days'")
            deleted = cur.rowcount
            conn.commit()
            cur.close()
            if deleted > 0:
                print(f"Pruned {deleted} metrics older than 7 days")
        finally:
            pool.putconn(conn)
    except Exception as e:
        print(f"Prune failed: {e}")


# ── VM Stats (psutil) ──
def collect_vm():
    try:
        cpu_pct = psutil.cpu_percent(interval=0.5)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        swap = psutil.swap_memory()
        boot = datetime.fromtimestamp(psutil.boot_time(), tz=timezone.utc)
        uptime = datetime.now(timezone.utc) - boot
        uptime_str = f"{uptime.days}d {uptime.seconds // 3600}h"

        uname = platform.uname()
        cpu_count = psutil.cpu_count()
        try:
            with open("/etc/os-release") as f:
                lines = {k: v.strip('"') for line in f for k, _, v in [line.strip().partition("=")]}
                os_pretty = lines.get("PRETTY_NAME", f"{uname.system} {uname.release}")
        except Exception:
            os_pretty = f"{uname.system} {uname.release}"

        return {
            "status": "ok",
            "cpu_percent": cpu_pct,
            "cpu_count": cpu_count,
            "memory_total_gb": round(mem.total / (1024**3), 1),
            "memory_used_gb": round(mem.used / (1024**3), 1),
            "memory_percent": mem.percent,
            "disk_total_gb": round(disk.total / (1024**3), 1),
            "disk_used_gb": round(disk.used / (1024**3), 1),
            "disk_percent": round(disk.percent, 1),
            "swap_percent": swap.percent,
            "uptime": uptime_str,
            "boot_time": boot.isoformat(),
            "os": os_pretty,
            "hostname": uname.node,
            "cpu_model": uname.processor or uname.machine,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ── Network I/O (psutil — host level) ──
def collect_network():
    """Host-level network I/O from psutil. Shows total bytes in/out across all interfaces."""
    try:
        counters = psutil.net_io_counters()
        # Discover host VM IP — use Docker one-shot container with host networking
        # Cached since it rarely changes
        vm_ip = _discover_vm_ip()
        return {
            "status": "ok",
            "bytes_sent": counters.bytes_sent,
            "bytes_recv": counters.bytes_recv,
            "packets_sent": counters.packets_sent,
            "packets_recv": counters.packets_recv,
            "errin": counters.errin,
            "errout": counters.errout,
            "sent_mb": round(counters.bytes_sent / (1024**2), 1),
            "recv_mb": round(counters.bytes_recv / (1024**2), 1),
            "vm_ip": vm_ip,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


_vm_ip_cache = None
_vm_ip_last = 0

def _discover_vm_ip():
    """Discover the host VM's real IP via Docker one-shot container with host networking. Cached 1hr."""
    global _vm_ip_cache, _vm_ip_last
    now = time.time()
    if _vm_ip_cache and (now - _vm_ip_last) < 3600:
        return _vm_ip_cache
    try:
        import docker
        client = docker.from_env()
        result = client.containers.run(
            "alpine", ["sh", "-c", "ip route get 1.1.1.1 2>/dev/null | head -1"],
            network_mode="host", remove=True, stderr=False,
        )
        # Output like: "1.1.1.1 via 172.16.20.1 dev ens18 src 172.16.20.49 uid 0"
        line = result.decode().strip()
        parts = line.split()
        if "src" in parts:
            idx = parts.index("src")
            if idx + 1 < len(parts):
                _vm_ip_cache = parts[idx + 1]
                _vm_ip_last = now
                return _vm_ip_cache
    except Exception as e:
        print(f"VM IP discovery: {e}")
    # Fallback
    _vm_ip_cache = "?"
    _vm_ip_last = now
    return _vm_ip_cache


# ── Docker Stats ──
def collect_docker():
    try:
        import docker
        client = docker.from_env()
        client.ping()
        containers = []
        for c in client.containers.list(all=True):
            stats = {}
            net_info = {}
            if c.status == "running":
                try:
                    raw = c.stats(stream=False)
                    cpu_delta = raw["cpu_stats"]["cpu_usage"]["total_usage"] - raw["precpu_stats"]["cpu_usage"]["total_usage"]
                    sys_delta = raw["cpu_stats"]["system_cpu_usage"] - raw["precpu_stats"]["system_cpu_usage"]
                    n_cpus = raw["cpu_stats"].get("online_cpus", 1)
                    cpu_pct = round((cpu_delta / sys_delta) * n_cpus * 100, 1) if sys_delta > 0 else 0
                    mem_usage = raw["memory_stats"].get("usage", 0)
                    mem_mb = round(mem_usage / (1024**2), 1)
                    stats = {"cpu_percent": cpu_pct, "memory_mb": mem_mb}
                    # Container network I/O
                    networks = raw.get("networks", {})
                    rx = sum(n.get("rx_bytes", 0) for n in networks.values())
                    tx = sum(n.get("tx_bytes", 0) for n in networks.values())
                    net_info = {"rx_bytes": rx, "tx_bytes": tx,
                                "rx_mb": round(rx / (1024**2), 1), "tx_mb": round(tx / (1024**2), 1)}
                except Exception:
                    stats = {"cpu_percent": 0, "memory_mb": 0}
                    net_info = {}

            started = c.attrs.get("State", {}).get("StartedAt", "")
            uptime_str = ""
            if started and c.status == "running":
                try:
                    start_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
                    delta = datetime.now(timezone.utc) - start_dt
                    uptime_str = f"{delta.days}d {delta.seconds // 3600}h"
                except Exception:
                    pass

            ports = []
            port_bindings = c.attrs.get("NetworkSettings", {}).get("Ports", {})
            for container_port, bindings in (port_bindings or {}).items():
                if bindings:
                    for b in bindings:
                        ports.append(f"{b.get('HostPort', '?')}:{container_port}")
                else:
                    ports.append(container_port)

            # Network info from container inspection
            networks = c.attrs.get("NetworkSettings", {}).get("Networks", {})
            container_ips = {name: net.get("IPAddress", "") for name, net in networks.items()}

            containers.append({
                "name": c.name,
                "image": c.image.tags[0] if c.image.tags else str(c.image.short_id),
                "status": c.status,
                "uptime": uptime_str,
                "ports": ", ".join(ports) if ports else "none",
                "id_short": c.short_id,
                "networks": container_ips,
                **stats,
                **net_info,
            })

        # Docker network info
        docker_networks = []
        for n in client.networks.list():
            if n.name in ("bridge", "host", "none"):
                continue
            ipam = n.attrs.get("IPAM", {}).get("Config", [{}])
            subnet = ipam[0].get("Subnet", "") if ipam else ""
            gateway = ipam[0].get("Gateway", "") if ipam else ""
            docker_networks.append({
                "name": n.name, "subnet": subnet, "gateway": gateway,
                "driver": n.attrs.get("Driver", ""),
            })

        return {
            "status": "ok", "containers": containers,
            "total": len(containers),
            "running": sum(1 for c in containers if c["status"] == "running"),
            "networks": docker_networks,
        }
    except Exception as e:
        return {"status": "error", "error": str(e), "containers": [], "networks": []}


# ── Postgres Stats ──
def collect_postgres():
    try:
        conn = psycopg2.connect(
            host=POSTGRES_HOST, port=POSTGRES_PORT,
            dbname=POSTGRES_DB, user=POSTGRES_USER, password=POSTGRES_PASSWORD,
            connect_timeout=3,
        )
        cur = conn.cursor()
        cur.execute(f"SELECT pg_size_pretty(pg_database_size('{POSTGRES_DB}'))")
        db_size = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM pg_stat_activity")
        active_conns = cur.fetchone()[0]
        cur.execute("SHOW max_connections")
        max_conns = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")
        table_count = cur.fetchone()[0]
        cur.execute("SELECT SUM(n_live_tup) FROM pg_stat_user_tables")
        total_rows = cur.fetchone()[0] or 0
        cur.execute("SELECT version()")
        version_full = cur.fetchone()[0]
        version_short = version_full.split(",")[0] if version_full else "Unknown"

        backup_info = "Unknown"
        try:
            backup_dir = Path("/opt/vibeforge/backups")
            if backup_dir.exists():
                backups = sorted(backup_dir.glob("vibeforge_*.sql.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
                if backups:
                    age = time.time() - backups[0].stat().st_mtime
                    if age < 3600:
                        backup_info = f"{int(age/60)}m ago ({backups[0].stat().st_size // 1024} KB)"
                    else:
                        backup_info = f"{int(age/3600)}h ago ({backups[0].stat().st_size // 1024} KB)"
        except Exception:
            pass

        cur.close()
        conn.close()

        return {
            "status": "ok",
            "db_size": db_size,
            "active_connections": active_conns,
            "max_connections": max_conns,
            "table_count": table_count,
            "total_rows": int(total_rows),
            "version": version_short,
            "last_backup": backup_info,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ── Caddy Stats ──
# VF-246: replaced nginx stub_status with Caddy's Prometheus metrics endpoint
# at http://caddy:2019/metrics. Enabled via `servers { metrics }` in the
# Caddyfile global options. Counters are summed across per-handler labels.

_CADDY_METRIC_REQ_TOTAL = "caddy_http_requests_total"
_CADDY_METRIC_REQ_IN_FLIGHT = "caddy_http_requests_in_flight"
_CADDY_METRIC_UPSTREAMS_HEALTHY = "caddy_reverse_proxy_upstreams_healthy"


def _parse_prom_counter(body: str, name: str) -> float:
    """Sum all samples of a Prometheus counter/gauge across label sets.

    Kept tiny — the metrics endpoint is small enough to line-scan; no
    prometheus-client dep required. Lines like:
        caddy_http_requests_total{handler="reverse_proxy",...} 42
    """
    total = 0.0
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith(name):
            continue
        # Split on the last space — the value is always last.
        parts = line.rsplit(" ", 1)
        if len(parts) != 2:
            continue
        try:
            total += float(parts[1])
        except ValueError:
            continue
    return total


def _parse_prom_counter_by_label(body: str, name: str, label_key: str,
                                  label_value_prefix: str) -> float:
    """Sum counter samples whose label matches a prefix. Used for status-class
    buckets ("code=\"4xx\"" would need regrouping; instead we sum all codes
    starting with a digit, e.g. all "4..")."""
    total = 0.0
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith(name) or line.startswith("#"):
            continue
        # Extract labels between { and }
        lb, rb = line.find("{"), line.find("}")
        if lb < 0 or rb < 0:
            continue
        labels_s = line[lb + 1:rb]
        # crude parse: look for label_key="..."
        tok = f'{label_key}="'
        i = labels_s.find(tok)
        if i < 0:
            continue
        start = i + len(tok)
        end = labels_s.find('"', start)
        if end < 0:
            continue
        val = labels_s[start:end]
        if not val.startswith(label_value_prefix):
            continue
        parts = line.rsplit(" ", 1)
        if len(parts) != 2:
            continue
        try:
            total += float(parts[1])
        except ValueError:
            continue
    return total


def collect_caddy():
    try:
        import urllib.request
        resp = urllib.request.urlopen("http://caddy:2019/metrics", timeout=2)
        body = resp.read().decode()
        total_requests = int(_parse_prom_counter(body, _CADDY_METRIC_REQ_TOTAL))
        in_flight = int(_parse_prom_counter(body, _CADDY_METRIC_REQ_IN_FLIGHT))
        upstreams_healthy = int(_parse_prom_counter(body, _CADDY_METRIC_UPSTREAMS_HEALTHY))
        # VF-324: response-class buckets. Caddy labels responses with `code="200"`
        # etc; sum each 1xx/4xx/5xx class across all label combinations.
        err_4xx = int(_parse_prom_counter_by_label(body, _CADDY_METRIC_REQ_TOTAL, "code", "4"))
        err_5xx = int(_parse_prom_counter_by_label(body, _CADDY_METRIC_REQ_TOTAL, "code", "5"))
        return {
            "status": "ok",
            "active_connections": in_flight,
            "total_requests": total_requests,
            "upstreams_healthy": upstreams_healthy,
            "err_4xx": err_4xx,
            "err_5xx": err_5xx,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


def collect_caddy_upstreams():
    """VF-324 / T3: ask Caddy about each upstream's live health. Returns a list
    of {address, num_requests, fails, healthy}. No caching — cheap call.
    """
    try:
        import urllib.request
        r = urllib.request.urlopen("http://caddy:2019/reverse_proxy/upstreams", timeout=2)
        raw = json.loads(r.read())
        # Caddy returns a list of {address, num_requests, fails}. A non-zero
        # `fails` field means Caddy's seen recent failures; we surface that.
        items = []
        for u in raw:
            items.append({
                "address": u.get("address"),
                "num_requests": u.get("num_requests", 0),
                "fails": u.get("fails", 0),
                "healthy": (u.get("fails", 0) == 0),
            })
        return {"status": "ok", "upstreams": items}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _calc_req_per_sec(proxy_data):
    """Calculate requests/sec from Caddy total_requests delta."""
    global _prev_caddy_requests, _prev_caddy_time
    if proxy_data.get("status") != "ok":
        return 0
    total = proxy_data.get("total_requests", 0)
    now = time.time()
    if _prev_caddy_requests is not None and _prev_caddy_time is not None:
        dt = now - _prev_caddy_time
        if dt > 0:
            rate = (total - _prev_caddy_requests) / dt
            _prev_caddy_requests = total
            _prev_caddy_time = now
            return round(max(0, rate), 2)
    _prev_caddy_requests = total
    _prev_caddy_time = now
    return 0


# ── TLS Certificate Discovery ──
# VF-246: reads the PEM file directly via a read-only bind mount at /certs/.
# Replaces the old pattern (docker-exec into nginx, run openssl there) because
# Caddy's alpine image has no openssl binary. `cryptography` in this container
# parses the PEM in-process — no subprocess, no cross-container dance.
_tls_cache = None
_tls_last_check = 0
TLS_CHECK_INTERVAL = 3600

CERT_PATH = "/certs/fullchain.pem"


def collect_tls():
    """Parse the mounted TLS cert at /certs/fullchain.pem."""
    global _tls_cache, _tls_last_check
    now = time.time()
    if _tls_cache is not None and (now - _tls_last_check) < TLS_CHECK_INTERVAL:
        return _tls_cache
    try:
        from cryptography import x509
        from cryptography.hazmat.backends import default_backend

        cert_file = Path(CERT_PATH)
        if not cert_file.exists():
            _tls_cache = {"status": "no_tls", "cert_path": CERT_PATH,
                          "note": f"Certificate file not found at {CERT_PATH}"}
            _tls_last_check = now
            return _tls_cache

        cert_bytes = cert_file.read_bytes()
        cert = x509.load_pem_x509_certificate(cert_bytes, default_backend())

        info = {"status": "ok", "cert_path": CERT_PATH, "container": "vibeforge-caddy-1"}
        info["subject"] = cert.subject.rfc4514_string()
        info["issuer"] = cert.issuer.rfc4514_string()

        try:
            cn_attr = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
            if cn_attr:
                info["cn"] = cn_attr[0].value
        except Exception:
            pass
        try:
            issuer_cn = cert.issuer.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
            if issuer_cn:
                info["issuer_cn"] = issuer_cn[0].value
        except Exception:
            pass

        info["not_before"] = cert.not_valid_before_utc.strftime("%b %d %H:%M:%S %Y GMT")
        info["not_after"]  = cert.not_valid_after_utc.strftime("%b %d %H:%M:%S %Y GMT")

        # SAN extraction
        try:
            san_ext = cert.extensions.get_extension_for_oid(x509.OID_SUBJECT_ALTERNATIVE_NAME)
            san_value = san_ext.value
            info["san"] = [n.value for n in san_value if hasattr(n, "value")]
        except Exception:
            pass

        # SHA-256 fingerprint in uppercase colon-separated bytes (matches openssl -fingerprint -sha256 format)
        try:
            import hashlib
            fp = hashlib.sha256(cert.public_bytes(__import__("cryptography.hazmat.primitives.serialization", fromlist=["Encoding"]).Encoding.DER)).hexdigest().upper()
            info["fingerprint_sha256"] = ":".join(fp[i:i+2] for i in range(0, len(fp), 2))
        except Exception:
            pass

        # Expiry countdown + status bucket
        days_left = (cert.not_valid_after_utc - datetime.now(timezone.utc)).days
        info["days_remaining"] = days_left
        if days_left < 0:
            info["cert_status"] = "expired"
        elif days_left < 7:
            info["cert_status"] = "critical"
        elif days_left < 30:
            info["cert_status"] = "warning"
        else:
            info["cert_status"] = "ok"

        info["self_signed"] = (info.get("subject", "").strip() == info.get("issuer", "").strip())
        info["wildcard"] = info.get("cn", "").startswith("*.")

        _tls_cache = info
        _tls_last_check = now
        return _tls_cache

    except Exception as e:
        _tls_cache = {"status": "error", "error": str(e)}
        _tls_last_check = now
        return _tls_cache


# ── Scheduled Jobs Discovery ──
# Organic: reads crontab + systemd timers, parses frequency, returns top 5 by frequency.
# Works on any VM — no hardcoded job names.

_jobs_cache = None
_jobs_last_check = 0
JOBS_CHECK_INTERVAL = 300  # re-check every 5 min


def _parse_cron_frequency(cron_expr):
    """Estimate runs per day from a cron expression (rough heuristic)."""
    parts = cron_expr.split()
    if len(parts) < 5:
        return 0
    minute, hour, dom, month, dow = parts[:5]
    if minute == "*" and hour == "*":
        return 1440  # every minute
    if minute.startswith("*/"):
        interval = int(minute[2:])
        if hour == "*":
            return 1440 // interval
        return 1  # every N min but only specific hours
    if hour == "*":
        return 24  # every hour (specific minute)
    if hour.startswith("*/"):
        interval = int(hour[2:])
        return 24 // interval
    if "," in hour:
        return len(hour.split(","))
    return 1  # specific time, once a day


def collect_scheduled_jobs():
    """Discover cron jobs + systemd timers. Sort by frequency, return top 5."""
    global _jobs_cache, _jobs_last_check
    now = time.time()
    if _jobs_cache is not None and (now - _jobs_last_check) < JOBS_CHECK_INTERVAL:
        return _jobs_cache

    jobs = []

    # Internal jobs (always present)
    jobs.append({"name": "Metrics prune", "schedule": "Daily", "frequency": 1, "source": "internal",
                 "last_run": "auto", "status": "ok"})
    jobs.append({"name": "TLS cert check", "schedule": "Hourly", "frequency": 24, "source": "internal",
                 "last_run": "auto", "status": "ok"})

    # Crontab discovery — read from host-mounted paths (volume-mounted read-only)
    # /host/crontab = /etc/crontab, /host/crontabs/ = /var/spool/cron/crontabs/
    try:
        for cron_path in ["/host/crontab"] + list(Path("/host/crontabs").glob("*")):
            try:
                content = Path(cron_path).read_text()
                for line in content.split("\n"):
                    line = line.strip()
                    if not line or line.startswith("#") or line.startswith("SHELL") or \
                       line.startswith("PATH") or line.startswith("MAILTO") or line.startswith("HOME"):
                        continue
                    parts = line.split()
                    if len(parts) >= 6:
                        cron_expr = " ".join(parts[:5])
                        # /etc/crontab has a user field before command
                        command = " ".join(parts[6:]) if str(cron_path) == "/host/crontab" else " ".join(parts[5:])
                        # Extract meaningful name from command
                        # Strip redirects (>>), semicolons, 2>&1 etc
                        clean_cmd = re.split(r'[>;|&]', command)[0].strip()
                        # Get the script/binary name from the path
                        cmd_short = clean_cmd.split("/")[-1].split(" ")[0] if "/" in clean_cmd else clean_cmd[:40]
                        cmd_short = cmd_short.strip(";").strip()
                        if not cmd_short or cmd_short in ("run-parts", "test", "-"):
                            continue
                        freq = _parse_cron_frequency(cron_expr)
                        jobs.append({
                            "name": cmd_short,
                            "schedule": cron_expr,
                            "frequency": freq,
                            "source": "crontab",
                            "last_run": "?",
                            "status": "ok",
                        })
            except Exception:
                continue
    except Exception as e:
        print(f"Crontab discovery: {e}")

    # Systemd timers — exec into a container that has systemctl (unlikely in Docker,
    # but works if host PID namespace is shared or nsenter is available)
    try:
        import docker
        client = docker.from_env()
        for c in client.containers.list():
            try:
                result = c.exec_run("systemctl list-timers --no-pager --no-legend 2>/dev/null", demux=True)
                stdout = (result.output[0] or b"").decode().strip()
                if stdout and result.exit_code == 0:
                    for line in stdout.split("\n"):
                        line = line.strip()
                        if not line:
                            continue
                        parts = line.split()
                        if len(parts) >= 6:
                            unit = parts[-2]
                            noise = ["man-db", "fwupd", "motd", "mdmonitor", "mdcheck",
                                     "update-notifier", "dpkg-db-backup"]
                            if any(n in unit for n in noise):
                                continue
                            left = parts[2] if len(parts) > 5 else ""
                            freq = 1
                            if "min" in left or "s" in left:
                                freq = 144
                            elif "h" in left and "day" not in left:
                                freq = 24
                            clean_name = unit.replace(".timer", "").replace("-", " ").title()
                            jobs.append({
                                "name": clean_name,
                                "schedule": f"Timer ({left} left)",
                                "frequency": freq,
                                "source": "systemd",
                                "last_run": "?",
                                "status": "ok",
                            })
                    break
            except Exception:
                continue
    except Exception as e:
        print(f"Timer discovery: {e}")

    # Deduplicate by name
    seen = set()
    unique = []
    for j in jobs:
        if j["name"] not in seen:
            seen.add(j["name"])
            unique.append(j)

    # Sort by frequency descending, take top 5
    unique.sort(key=lambda j: j["frequency"], reverse=True)
    _jobs_cache = {"status": "ok", "jobs": unique[:5], "total_discovered": len(unique)}
    _jobs_last_check = now
    return _jobs_cache


# ── State tracking for event detection ──
_prev_container_states = {}
_prev_thresholds = {"cpu": False, "mem": False, "disk": False}  # True = currently breached
_collect_count = 0
_last_health_summary_hour = None  # tracks (date, hour) of last health check event
_last_tls_warning_hour = None  # tracks (date, hour) of last TLS warning

# ── Combined collection (real data) ──
def collect_all():
    _cache["vm"] = collect_vm()
    _cache["docker"] = collect_docker()
    _cache["postgres"] = collect_postgres()
    _cache["caddy"] = collect_caddy()
    _cache["caddy_upstreams"] = collect_caddy_upstreams()
    _cache["tls"] = collect_tls()
    _cache["network"] = collect_network()
    _cache["scheduled_jobs"] = collect_scheduled_jobs()

    # Calculate req/s from Caddy total_requests delta
    req_s = _calc_req_per_sec(_cache["caddy"])
    _cache["caddy"]["req_per_sec"] = req_s

    now = datetime.now(timezone.utc)
    _cache["last_poll"] = now.isoformat()
    _cache["errors"] = [
        k for k in ("vm", "docker", "postgres", "caddy") if _cache[k].get("status") == "error"
    ]
    tls = _cache.get("tls", {})
    if tls.get("cert_status") in ("expired", "critical"):
        _cache["errors"].append("tls")

    global _prev_container_states, _prev_thresholds, _collect_count
    _collect_count += 1

    vm = _cache["vm"]
    pg = _cache["postgres"]
    dk = _cache["docker"]
    net = _cache["network"]
    cpu = vm.get("cpu_percent", 0)
    memory = vm.get("memory_percent", 0)
    disk = vm.get("disk_percent", 0)
    swap = vm.get("swap_percent", 0)
    db_conns = pg.get("active_connections", 0) if pg.get("status") == "ok" else 0
    net_in = net.get("bytes_recv", 0) if net.get("status") == "ok" else 0
    net_out = net.get("bytes_sent", 0) if net.get("status") == "ok" else 0

    # ── Event detection ──
    events = []

    # Container state changes (skip first poll — baseline capture)
    if _prev_container_states:
        for c in dk.get("containers", []):
            prev = _prev_container_states.get(c["name"])
            if prev and prev != c["status"]:
                if c["status"] == "running":
                    events.append(f"ok:{c['name']} is now running")
                else:
                    events.append(f"error:{c['name']} is {c['status']}")
    for c in dk.get("containers", []):
        _prev_container_states[c["name"]] = c["status"]

    # Threshold crossings
    cpu_breach = cpu > 80
    mem_breach = memory > 85
    disk_breach = disk > 90
    if cpu_breach and not _prev_thresholds["cpu"]:
        events.append(f"warn:CPU crossed 80% ({cpu}%)")
    if not cpu_breach and _prev_thresholds["cpu"]:
        events.append(f"ok:CPU returned below 80% ({cpu}%)")
    if mem_breach and not _prev_thresholds["mem"]:
        events.append(f"warn:Memory crossed 85% ({memory}%)")
    if not mem_breach and _prev_thresholds["mem"]:
        events.append(f"ok:Memory returned below 85% ({memory}%)")
    if disk_breach and not _prev_thresholds["disk"]:
        events.append(f"error:Disk critical >90% ({disk}%)")
    if not disk_breach and _prev_thresholds["disk"]:
        events.append(f"ok:Disk returned below 90% ({disk}%)")
    _prev_thresholds = {"cpu": cpu_breach, "mem": mem_breach, "disk": disk_breach}

    # Periodic health summary — once per hour, tracked by (date, hour) to avoid missed windows
    global _last_health_summary_hour
    current_hour = (now.date(), now.hour)
    if _collect_count == 1 or current_hour != _last_health_summary_hour:
        _last_health_summary_hour = current_hour
        date_str = now.strftime("%Y-%m-%d")
        events.append(f"ok:Health check {date_str} — CPU {cpu}% MEM {memory}% Disk {disk}%")

    # TLS cert warning
    tls_data = _cache.get("tls", {})
    global _last_tls_warning_hour
    if tls_data.get("cert_status") in ("warning", "critical", "expired"):
        if current_hour != _last_tls_warning_hour:
            _last_tls_warning_hour = current_hour
            events.append(f"warn:TLS cert {tls_data.get('cert_status')} — {tls_data.get('days_remaining', '?')}d remaining")

    # Build event string (pipe-separated if multiple, usually just one or none)
    event_str = "|".join(events) if events else None

    # Insert real metric with event tag
    _insert_metric(now.isoformat(), cpu, memory, disk, swap, db_conns,
                    net_in=net_in, net_out=net_out, req_s=req_s,
                    synthetic=False, outage=False, event=event_str)

    # Update last_real for synthetic ticker
    with _last_real_lock:
        _last_real["ts"] = now
        _last_real["cpu"] = cpu
        _last_real["memory"] = memory
        _last_real["disk"] = disk
        _last_real["swap"] = swap
        _last_real["db_conns"] = db_conns
        _last_real["net_in"] = net_in
        _last_real["net_out"] = net_out
        _last_real["req_per_sec"] = req_s

    return _cache


# ── Background threads ──

_prune_counter = 0


def _bg_collect():
    global _prune_counter
    while True:
        try:
            collect_all()
            _prune_counter += 1
            if _prune_counter >= 8640:
                _prune_counter = 0
                _prune_old_metrics()
        except Exception as e:
            print(f"Collection error: {e}")
        time.sleep(1)


def _bg_synthetic():
    while True:
        time.sleep(SYNTHETIC_INTERVAL)
        try:
            with _last_real_lock:
                last_ts = _last_real["ts"]
                cpu = _last_real["cpu"]
                memory = _last_real["memory"]
                disk = _last_real["disk"]
                swap = _last_real["swap"]
                db_conns = _last_real["db_conns"]
                net_in = _last_real["net_in"]
                net_out = _last_real["net_out"]
                req_s = _last_real["req_per_sec"]

            if last_ts is None:
                continue

            now = datetime.now(timezone.utc)
            age = (now - last_ts).total_seconds()
            if age > OUTAGE_THRESHOLD:
                continue

            _insert_metric(now.isoformat(), cpu, memory, disk, swap, db_conns,
                            net_in=net_in, net_out=net_out, req_s=req_s,
                            synthetic=True, outage=False)

        except Exception as e:
            print(f"Synthetic fill error: {e}")


# ── Startup ──
_ensure_table()
_detect_and_backfill_outage()

_collect_thread = threading.Thread(target=_bg_collect, daemon=True)
_collect_thread.start()

_synthetic_thread = threading.Thread(target=_bg_synthetic, daemon=True)
_synthetic_thread.start()

print(f"Health polling started. Synthetic fill every {SYNTHETIC_INTERVAL}s. Outage threshold {OUTAGE_THRESHOLD}s.")


# ── API Endpoints ──

@app.get("/api/health/summary")
def health_summary():
    vm = _cache.get("vm", {})
    docker = _cache.get("docker", {})
    overall = "ok"
    if _cache.get("errors"):
        overall = "error"
    elif vm.get("disk_percent", 0) > 90 or vm.get("cpu_percent", 0) > 90 or vm.get("memory_percent", 0) > 90:
        overall = "warn"
    return {
        "overall": overall,
        "containers": f"{docker.get('running', 0)}/{docker.get('total', 0)}",
        "cpu_percent": vm.get("cpu_percent", 0),
        "memory_percent": vm.get("memory_percent", 0),
        "disk_percent": vm.get("disk_percent", 0),
        "uptime": vm.get("uptime", "?"),
        "errors": _cache.get("errors", []),
        "polled_at": _cache.get("last_poll"),
    }


@app.get("/api/health/full")
def health_full():
    return _cache


@app.get("/api/health/vm")
def health_vm():
    return _cache.get("vm", {"status": "loading"})


@app.get("/api/health/docker")
def health_docker():
    return _cache.get("docker", {"status": "loading"})


@app.get("/api/health/postgres")
def health_postgres():
    return _cache.get("postgres", {"status": "loading"})


@app.get("/api/health/caddy")
def health_caddy():
    return _cache.get("caddy", {"status": "loading"})


@app.get("/api/health/caddy-upstreams")
def health_caddy_upstreams():
    """VF-324 / T3: live upstream reachability, one record per proxied service."""
    return _cache.get("caddy_upstreams", {"status": "loading"})


# VF-246: /api/health/nginx kept as alias for any legacy poller during cutover.
# Remove in a later cleanup once no callers reference it.
@app.get("/api/health/nginx")
def health_nginx_legacy():
    return _cache.get("caddy", {"status": "loading"})


@app.get("/api/health/tls")
def health_tls():
    return _cache.get("tls", {"status": "loading"})


@app.get("/api/health/network")
def health_network():
    return _cache.get("network", {"status": "loading"})


@app.get("/api/health/jobs")
def health_jobs():
    """Scheduled jobs — organically discovered from cron + systemd timers. Top 5 by frequency."""
    return _cache.get("scheduled_jobs", {"status": "loading"})


@app.get("/api/health/events")
def health_events(limit: int = 150):
    """Recent health events — from event column in health_metrics. Survives reboots."""
    try:
        pool = _get_pool()
        conn = pool.getconn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT ts, event FROM health_metrics WHERE event IS NOT NULL ORDER BY ts DESC LIMIT %s",
                (limit,)
            )
            rows = cur.fetchall()
            cur.close()
            # Parse "level:message" format, support pipe-separated multiple events per row
            events = []
            for r in rows:
                ts_iso = r[0].isoformat()
                for part in r[1].split("|"):
                    if ":" in part:
                        level, msg = part.split(":", 1)
                        events.append({"time": ts_iso, "level": level.strip(), "msg": msg.strip()})
            return events[:limit]
        finally:
            pool.putconn(conn)
    except Exception as e:
        return []


@app.get("/api/health/history")
def health_history(range: str = "24h"):
    return _query_history(range)


# ── Dashboard UI ──

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})
