#!/usr/bin/env python3
"""Recolecta métricas de macOS, las guarda en SQLite y regenera dashboard.html."""

import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

RUNTIME_DIR = Path.home() / "Library" / "Application Support" / "mac-monitor"
DATA_DB = RUNTIME_DIR / "metrics.db"
DASHBOARD_HTML = RUNTIME_DIR / "dashboard.html"


def sh(cmd: str) -> str:
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout


def parse_vm_stat() -> dict:
    out = sh("vm_stat")
    page_size = 16384
    m = {}
    for line in out.splitlines():
        if "page size of" in line:
            try:
                page_size = int(line.split("page size of")[1].split()[0])
            except (IndexError, ValueError):
                pass
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        value = value.strip().rstrip(".")
        try:
            m[key.strip()] = int(value)
        except ValueError:
            pass

    def mb(pages_key):
        return (m.get(pages_key, 0) * page_size) / (1024 * 1024)

    return {
        "ram_free_mb": mb("Pages free"),
        "ram_active_mb": mb("Pages active"),
        "ram_inactive_mb": mb("Pages inactive"),
        "ram_wired_mb": mb("Pages wired down"),
        "ram_compressed_mb": mb("Pages occupied by compressor"),
    }


def get_ram_total_mb() -> float:
    out = sh("sysctl -n hw.memsize").strip()
    try:
        return int(out) / (1024 * 1024)
    except ValueError:
        return 0.0


def get_load_avg():
    out = sh("sysctl -n vm.loadavg").strip()
    parts = out.replace("{", "").replace("}", "").split()
    try:
        return float(parts[0]), float(parts[1]), float(parts[2])
    except (ValueError, IndexError):
        return 0.0, 0.0, 0.0


def get_cpu_pct():
    """(user, sys, idle) % — usa la 2ª muestra de top porque la 1ª es desde boot."""
    out = sh("top -l 2 -n 0 -s 1")
    lines = [l for l in out.splitlines() if "CPU usage" in l]
    if not lines:
        return 0.0, 0.0, 100.0
    nums = re.findall(r"(\d+\.\d+)%", lines[-1])
    if len(nums) >= 3:
        return float(nums[0]), float(nums[1]), float(nums[2])
    return 0.0, 0.0, 100.0


def get_disk() -> dict:
    out = sh("df -k / | tail -1")
    parts = out.split()
    if len(parts) >= 5:
        try:
            total_kb = int(parts[1])
            used_kb = int(parts[2])
            return {
                "disk_total_gb": total_kb / (1024 * 1024),
                "disk_used_gb": used_kb / (1024 * 1024),
                "disk_pct": (used_kb / total_kb) * 100 if total_kb else 0.0,
            }
        except ValueError:
            pass
    return {"disk_total_gb": 0.0, "disk_used_gb": 0.0, "disk_pct": 0.0}


def get_battery() -> dict:
    out = sh("pmset -g batt")
    pct_match = re.search(r"(\d+)%", out)
    pct = int(pct_match.group(1)) if pct_match else None
    charging = 1 if ("AC Power" in out or "charging" in out.lower()) else 0
    return {"battery_pct": pct, "battery_charging": charging}


def classify(comm: str) -> str:
    c = comm
    if "Cursor" in c:
        return "Cursor"
    if "WebKit.WebContent" in c:
        return "Safari (pestañas)"
    if "WebKit" in c or "/Safari.app" in c:
        return "Safari (otros)"
    if "WhatsApp" in c:
        return "WhatsApp"
    if "Spotify" in c:
        return "Spotify"
    if "Keynote" in c:
        return "Keynote"
    if "ChatGPT" in c:
        return "ChatGPT"
    if "/Arc.app" in c or c.endswith("/Arc"):
        return "Arc"
    if c.endswith("/claude") or c == "claude":
        return "claude (CLI)"
    if "Google Chrome" in c:
        return "Chrome"
    if "Visual Studio Code" in c or "Code Helper" in c:
        return "VSCode"
    if "Docker" in c or "com.docker" in c:
        return "Docker"
    if "Slack" in c:
        return "Slack"
    if "zoom" in c.lower():
        return "Zoom"
    return "otros (sistema)"


def get_top_apps(top_n: int = 10):
    out = sh("ps -axm -o pid,rss,comm")
    groups = {}
    for line in out.splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            rss_kb = int(parts[1])
        except ValueError:
            continue
        app = classify(parts[2])
        if app not in groups:
            groups[app] = [0, 0]
        groups[app][0] += rss_kb
        groups[app][1] += 1
    items = [(app, kb / 1024, n) for app, (kb, n) in groups.items()]
    items.sort(key=lambda x: -x[1])
    return items[:top_n]


SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    ts INTEGER PRIMARY KEY,
    ram_total_mb REAL,
    ram_free_mb REAL,
    ram_active_mb REAL,
    ram_inactive_mb REAL,
    ram_wired_mb REAL,
    ram_compressed_mb REAL,
    cpu_user REAL,
    cpu_sys REAL,
    cpu_idle REAL,
    load_1 REAL,
    load_5 REAL,
    load_15 REAL,
    disk_used_gb REAL,
    disk_total_gb REAL,
    disk_pct REAL,
    battery_pct INTEGER,
    battery_charging INTEGER
);
CREATE TABLE IF NOT EXISTS top_apps (
    ts INTEGER,
    rank INTEGER,
    app TEXT,
    rss_mb REAL,
    num_procs INTEGER
);
CREATE INDEX IF NOT EXISTS idx_top_apps_ts ON top_apps(ts);
"""


def get_conn() -> sqlite3.Connection:
    DATA_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DATA_DB))
    conn.executescript(SCHEMA)
    return conn


SAMPLE_COLS = [
    "ts", "ram_total_mb", "ram_free_mb", "ram_active_mb", "ram_inactive_mb",
    "ram_wired_mb", "ram_compressed_mb", "cpu_user", "cpu_sys", "cpu_idle",
    "load_1", "load_5", "load_15", "disk_used_gb", "disk_total_gb", "disk_pct",
    "battery_pct", "battery_charging",
]


def write_sample(conn, ts, sample, top_apps):
    values = [ts] + [sample.get(k) for k in SAMPLE_COLS[1:]]
    placeholders = ",".join(["?"] * len(SAMPLE_COLS))
    conn.execute(
        f"INSERT OR REPLACE INTO samples ({','.join(SAMPLE_COLS)}) VALUES ({placeholders})",
        values,
    )
    conn.execute("DELETE FROM top_apps WHERE ts = ?", (ts,))
    for i, (app, rss, n) in enumerate(top_apps):
        conn.execute(
            "INSERT INTO top_apps (ts,rank,app,rss_mb,num_procs) VALUES (?,?,?,?,?)",
            (ts, i, app, rss, n),
        )
    conn.commit()


def fetch_all(conn):
    rows = conn.execute("SELECT * FROM samples ORDER BY ts").fetchall()
    cols = [d[0] for d in conn.execute("SELECT * FROM samples LIMIT 0").description]
    return [dict(zip(cols, r)) for r in rows]


NUMERIC_KEYS = [k for k in SAMPLE_COLS if k != "ts"]


def downsample(samples, max_points=800):
    n = len(samples)
    if n <= max_points:
        return samples
    bucket_size = n / max_points
    out = []
    for i in range(max_points):
        start = int(i * bucket_size)
        end = int((i + 1) * bucket_size)
        if end <= start:
            end = start + 1
        bucket = samples[start:end]
        if not bucket:
            continue
        agg = {"ts": int(sum(s["ts"] for s in bucket) / len(bucket))}
        for k in NUMERIC_KEYS:
            vals = [s[k] for s in bucket if s.get(k) is not None]
            agg[k] = (sum(vals) / len(vals)) if vals else None
        out.append(agg)
    return out


def fetch_top_apps_now(conn):
    latest = conn.execute("SELECT MAX(ts) FROM samples").fetchone()[0]
    if latest is None:
        return []
    return conn.execute(
        "SELECT app, rss_mb, num_procs FROM top_apps WHERE ts = ? ORDER BY rank",
        (latest,),
    ).fetchall()


def svg_line(samples, key, label, color, height=180, width=720):
    if len(samples) < 2:
        return (
            f'<svg viewBox="0 0 {width} {height}" width="100%" '
            f'style="max-width:{width}px;display:block">'
            f'<text x="50%" y="50%" fill="#888" text-anchor="middle" '
            f'font-family="system-ui" font-size="13">{label}: datos insuficientes</text></svg>'
        )

    pad_l, pad_r, pad_t, pad_b = 50, 14, 22, 22
    chart_w = width - pad_l - pad_r
    chart_h = height - pad_t - pad_b

    values = [(s[key] if s.get(key) is not None else 0.0) for s in samples]
    ts_list = [s["ts"] for s in samples]

    v_min, v_max = min(values), max(values)
    if v_max == v_min:
        v_max = v_min + 1
    t_min, t_max = ts_list[0], ts_list[-1]
    if t_max == t_min:
        t_max = t_min + 1

    def xp(ts):
        return pad_l + (ts - t_min) / (t_max - t_min) * chart_w

    def yp(v):
        return pad_t + (1 - (v - v_min) / (v_max - v_min)) * chart_h

    pts = " ".join(f"{xp(t):.1f},{yp(v):.1f}" for t, v in zip(ts_list, values))

    ticks_svg = []
    for i in range(4):
        v = v_min + (v_max - v_min) * (i / 3)
        y = yp(v)
        ticks_svg.append(
            f'<line x1="{pad_l}" x2="{width - pad_r}" y1="{y:.1f}" y2="{y:.1f}" '
            f'stroke="currentColor" stroke-opacity="0.08" stroke-width="1"/>'
            f'<text x="{pad_l - 6}" y="{y + 4:.1f}" fill="currentColor" fill-opacity="0.55" '
            f'text-anchor="end" font-size="10" font-family="system-ui">{v:.0f}</text>'
        )

    span = t_max - t_min
    if span < 24 * 3600:
        fmt = "%H:%M"
    elif span < 7 * 24 * 3600:
        fmt = "%d %b %H:%M"
    else:
        fmt = "%Y-%m-%d"
    t0 = datetime.fromtimestamp(t_min).strftime(fmt)
    t1 = datetime.fromtimestamp(t_max).strftime(fmt)

    return f"""<svg viewBox="0 0 {width} {height}" width="100%" style="max-width:{width}px;height:auto;display:block">
  <text x="{pad_l}" y="14" fill="currentColor" font-size="12" font-family="system-ui" font-weight="600">{label}</text>
  <text x="{width - pad_r}" y="14" fill="{color}" font-size="12" font-family="system-ui" text-anchor="end" font-variant-numeric="tabular-nums">{values[-1]:.1f}</text>
  {"".join(ticks_svg)}
  <polyline fill="none" stroke="{color}" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round" points="{pts}"/>
  <text x="{pad_l}" y="{height - 6}" fill="currentColor" fill-opacity="0.55" font-size="10" font-family="system-ui">{t0}</text>
  <text x="{width - pad_r}" y="{height - 6}" fill="currentColor" fill-opacity="0.55" font-size="10" font-family="system-ui" text-anchor="end">{t1}</text>
</svg>"""


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>mac-monitor — __NOW__</title>
<style>
  :root { --bg:#0f1115; --card:#181b22; --card-2:#1f232c; --text:#e6e8ee; --muted:#9aa3b2; --border:#2a2f3a; --accent:#7aa2f7; }
  @media (prefers-color-scheme: light) {
    :root { --bg:#f7f8fb; --card:#fff; --card-2:#f0f2f7; --text:#1a1d24; --muted:#5b6472; --border:#e1e4ec; --accent:#3a6bd6; }
  }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--text); font-family:-apple-system,system-ui,Helvetica,Arial,sans-serif; line-height:1.5; -webkit-font-smoothing:antialiased; }
  .container { max-width:1100px; margin:0 auto; padding:32px 24px 96px; }
  h1 { font-size:26px; margin:0 0 4px; letter-spacing:-0.02em; }
  .subtitle { color:var(--muted); font-size:13px; margin-bottom:24px; }
  h2 { font-size:17px; margin:36px 0 12px; padding-bottom:6px; border-bottom:1px solid var(--border); letter-spacing:-0.01em; }
  .stat-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; }
  .stat { background:var(--card); border:1px solid var(--border); border-radius:10px; padding:14px 16px; }
  .stat .label { font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:0.06em; }
  .stat .value { font-size:22px; font-weight:600; margin-top:4px; font-variant-numeric:tabular-nums; }
  .stat .muted { font-size:12px; color:var(--muted); margin-top:2px; }
  .charts { display:grid; grid-template-columns:1fr 1fr; gap:14px; }
  .chart-card { background:var(--card); border:1px solid var(--border); border-radius:10px; padding:12px 14px; color:var(--text); }
  @media (max-width:780px) { .charts { grid-template-columns:1fr; } }
  table { width:100%; border-collapse:collapse; margin-top:4px; font-size:14px; }
  th, td { text-align:left; padding:8px 12px; border-bottom:1px solid var(--border); }
  th { color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:0.05em; font-weight:500; }
  td.num { text-align:right; font-variant-numeric:tabular-nums; }
  code { font-family:"SF Mono",Menlo,monospace; font-size:12px; background:var(--card-2); padding:1px 5px; border-radius:4px; }
  footer { margin-top:48px; padding-top:16px; border-top:1px solid var(--border); color:var(--muted); font-size:12px; }
</style>
</head>
<body>
<div class="container">
  <h1>mac-monitor</h1>
  <div class="subtitle">Última muestra: __NOW__ · Rango: __RANGE__ · Muestras: __NSAMPLES__ (mostradas: __NPLOT__)</div>

  <div class="stat-grid">__STATS__</div>

  <h2>Tendencias (histórico completo)</h2>
  <div class="charts">__CHARTS__</div>

  <h2>Top apps por RAM (snapshot actual)</h2>
  <table>
    <thead><tr><th>App</th><th class="num">RAM</th><th class="num">Procesos</th></tr></thead>
    <tbody>__ROWS__</tbody>
  </table>

  <footer>Control: <code>./bin/mac-monitor on|off|now|open</code>.</footer>
</div>
</body>
</html>"""


def render_html(conn) -> str:
    all_samples = fetch_all(conn)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if not all_samples:
        body = '<div class="container"><h1>mac-monitor</h1><p>No hay datos todavía. Corré <code>./bin/mac-monitor now</code>.</p></div>'
        return f"<!DOCTYPE html><html><head><meta charset='UTF-8'><title>mac-monitor</title></head><body>{body}</body></html>"

    latest = all_samples[-1]
    samples = downsample(all_samples, max_points=800)
    for s in samples:
        s["cpu_used"] = 100.0 - (s.get("cpu_idle") or 0.0)

    t_min = all_samples[0]["ts"]
    t_max = all_samples[-1]["ts"]
    span = t_max - t_min
    if span < 24 * 3600:
        range_fmt = "%H:%M"
    elif span < 7 * 24 * 3600:
        range_fmt = "%d %b %H:%M"
    else:
        range_fmt = "%Y-%m-%d %H:%M"
    range_str = f"{datetime.fromtimestamp(t_min).strftime(range_fmt)} → {datetime.fromtimestamp(t_max).strftime(range_fmt)}"

    ram_used = (latest["ram_total_mb"] or 0) - (latest["ram_free_mb"] or 0)
    ram_pct = (ram_used / latest["ram_total_mb"] * 100) if latest["ram_total_mb"] else 0

    stats = []
    stats.append(f'<div class="stat"><div class="label">RAM total</div><div class="value">{latest["ram_total_mb"]/1024:.0f} GB</div></div>')
    stats.append(f'<div class="stat"><div class="label">RAM en uso</div><div class="value">{ram_used/1024:.1f} GB</div><div class="muted">{ram_pct:.0f}%</div></div>')
    stats.append(f'<div class="stat"><div class="label">RAM libre</div><div class="value">{latest["ram_free_mb"]:.0f} MB</div></div>')
    stats.append(f'<div class="stat"><div class="label">Comprimido</div><div class="value">{latest["ram_compressed_mb"]:.0f} MB</div></div>')
    stats.append(f'<div class="stat"><div class="label">CPU usado</div><div class="value">{100-latest["cpu_idle"]:.0f}%</div><div class="muted">u {latest["cpu_user"]:.0f} · s {latest["cpu_sys"]:.0f}</div></div>')
    stats.append(f'<div class="stat"><div class="label">Load 1m</div><div class="value">{latest["load_1"]:.2f}</div><div class="muted">5m {latest["load_5"]:.2f} · 15m {latest["load_15"]:.2f}</div></div>')
    stats.append(f'<div class="stat"><div class="label">Disco /</div><div class="value">{latest["disk_pct"]:.0f}%</div><div class="muted">{latest["disk_used_gb"]:.0f} / {latest["disk_total_gb"]:.0f} GB</div></div>')
    if latest["battery_pct"] is not None:
        ch = "cargando" if latest["battery_charging"] else "batería"
        stats.append(f'<div class="stat"><div class="label">Batería</div><div class="value">{latest["battery_pct"]}%</div><div class="muted">{ch}</div></div>')

    charts = "".join([
        '<div class="chart-card">' + svg_line(samples, "ram_free_mb", "RAM libre (MB)", "#7aa2f7") + '</div>',
        '<div class="chart-card">' + svg_line(samples, "ram_compressed_mb", "Compresor (MB)", "#e0af68") + '</div>',
        '<div class="chart-card">' + svg_line(samples, "cpu_used", "CPU usado (%)", "#f7768e") + '</div>',
        '<div class="chart-card">' + svg_line(samples, "load_1", "Load avg (1 min)", "#9ece6a") + '</div>',
    ])

    top_apps = fetch_top_apps_now(conn)
    rows = "".join(
        f'<tr><td>{app}</td><td class="num">{rss:.0f} MB</td><td class="num">{n}</td></tr>'
        for app, rss, n in top_apps
    )

    return (
        HTML_TEMPLATE
        .replace("__NOW__", now)
        .replace("__RANGE__", range_str)
        .replace("__NSAMPLES__", str(len(all_samples)))
        .replace("__NPLOT__", str(len(samples)))
        .replace("__STATS__", "".join(stats))
        .replace("__CHARTS__", charts)
        .replace("__ROWS__", rows or '<tr><td colspan="3" style="text-align:center;color:var(--muted)">sin datos</td></tr>')
    )


def main():
    ts = int(time.time())

    sample = {"ram_total_mb": get_ram_total_mb()}
    sample.update(parse_vm_stat())
    cu, cs, ci = get_cpu_pct()
    sample.update({"cpu_user": cu, "cpu_sys": cs, "cpu_idle": ci})
    l1, l5, l15 = get_load_avg()
    sample.update({"load_1": l1, "load_5": l5, "load_15": l15})
    sample.update(get_disk())
    sample.update(get_battery())

    top_apps = get_top_apps(top_n=10)

    conn = get_conn()
    try:
        write_sample(conn, ts, sample, top_apps)
        html = render_html(conn)
        DASHBOARD_HTML.write_text(html, encoding="utf-8")
    finally:
        conn.close()

    print(f"[mac-monitor] tick @ {datetime.fromtimestamp(ts).strftime('%H:%M:%S')} → {DASHBOARD_HTML}")


if __name__ == "__main__":
    sys.exit(main() or 0)
