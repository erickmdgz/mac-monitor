#!/usr/bin/env python3
"""Recolecta métricas de macOS, las guarda en SQLite y regenera dashboard.html."""

import json
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


def fetch_top_apps_all(conn):
    rows = conn.execute(
        "SELECT ts, app, rss_mb, num_procs FROM top_apps ORDER BY ts, rank"
    ).fetchall()
    return [{"ts": ts, "app": app, "rss_mb": rss, "num_procs": n} for ts, app, rss, n in rows]


# TODO: si el HTML crece >5 MB (≈30 días @30s), cap'ear el embed a últimos N días.
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

  .filters {
    display:flex; flex-wrap:wrap; gap:10px; align-items:center;
    margin:14px 0 0; padding:14px;
    background:var(--card); border:1px solid var(--border); border-radius:10px;
  }
  .filters label { font-size:12px; color:var(--muted); display:flex; align-items:center; gap:6px; }
  .filters input[type="datetime-local"] {
    background:var(--card-2); color:var(--text);
    border:1px solid var(--border); border-radius:6px;
    padding:6px 8px; font-size:13px; font-family:inherit;
  }
  .presets { display:flex; gap:6px; flex-wrap:wrap; }
  .preset, .toggle {
    background:var(--card-2); color:var(--text);
    border:1px solid var(--border); border-radius:6px;
    padding:6px 10px; font-size:12px; font-family:inherit; cursor:pointer;
  }
  .preset:hover, .toggle:hover { border-color:var(--accent); }
  .preset.active { background:var(--accent); color:white; border-color:var(--accent); }
  .toggle.on::before { content:"●"; color:#9ece6a; margin-right:6px; }
  .toggle.off::before { content:"●"; color:var(--muted); margin-right:6px; }
  .spacer { flex:1; }

  #range-info { color:var(--muted); font-size:13px; margin:14px 0 18px; }

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
  <div class="subtitle">Última muestra: __NOW__ · Histórico: __NTOTAL__ muestras desde __TMIN__</div>

  <div class="filters">
    <label>Desde <input type="datetime-local" id="from"></label>
    <label>Hasta <input type="datetime-local" id="to"></label>
    <div class="presets">
      <button class="preset" data-preset="1h">1h</button>
      <button class="preset" data-preset="6h">6h</button>
      <button class="preset" data-preset="24h">24h</button>
      <button class="preset" data-preset="7d">7d</button>
      <button class="preset" data-preset="all">Todo</button>
    </div>
    <div class="spacer"></div>
    <button class="toggle on" id="auto-refresh">Auto-refresh: ON</button>
  </div>

  <div id="range-info"></div>

  <div class="stat-grid" id="stats"></div>

  <h2>Tendencias</h2>
  <div class="charts" id="charts"></div>

  <h2 id="top-title">Top apps por RAM (promedio en ventana)</h2>
  <table>
    <thead><tr><th>App</th><th class="num">RAM (prom.)</th><th class="num">Procesos (prom.)</th></tr></thead>
    <tbody id="top-apps"></tbody>
  </table>

  <footer>Control: <code>./bin/mac-monitor on|off|now|open</code>.</footer>
</div>

<script id="data" type="application/json">__DATA_JSON__</script>
<script>
(function () {
  const DATA = JSON.parse(document.getElementById('data').textContent);
  const samplesAll = DATA.samples;
  const topAppsAll = DATA.topApps;

  const container = document.querySelector('.container');
  if (!samplesAll.length) {
    container.innerHTML = '<h1>mac-monitor</h1><p>No hay datos todavía. Corré <code>./bin/mac-monitor now</code>.</p>';
    return;
  }

  const fromInput = document.getElementById('from');
  const toInput = document.getElementById('to');
  const presetButtons = document.querySelectorAll('.preset');
  const autoBtn = document.getElementById('auto-refresh');

  const FIRST_TS = samplesAll[0].ts;
  const LAST_TS = samplesAll[samplesAll.length - 1].ts;

  const STORAGE_RANGE = 'mac-monitor:range';
  const STORAGE_AUTO = 'mac-monitor:autorefresh';

  function pad(n) { return String(n).padStart(2, '0'); }
  function unixToLocalInput(unix) {
    const d = new Date(unix * 1000);
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate())
         + 'T' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  }
  function inputToUnix(value) {
    if (!value) return null;
    const t = new Date(value).getTime();
    if (isNaN(t)) return null;
    return Math.floor(t / 1000);
  }
  function fmtTs(unix, span) {
    const d = new Date(unix * 1000);
    if (span < 24 * 3600) {
      return pad(d.getHours()) + ':' + pad(d.getMinutes());
    }
    if (span < 7 * 24 * 3600) {
      const months = ['ene','feb','mar','abr','may','jun','jul','ago','sep','oct','nov','dic'];
      return pad(d.getDate()) + ' ' + months[d.getMonth()] + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
    }
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  }

  function filterSamples(from, to) {
    return samplesAll.filter(s => s.ts >= from && s.ts <= to);
  }
  function filterTopApps(from, to) {
    return topAppsAll.filter(t => t.ts >= from && t.ts <= to);
  }

  function aggregate(arr, key) {
    let min = Infinity, max = -Infinity, sum = 0, n = 0, last = null;
    for (const x of arr) {
      const v = x[key];
      if (v == null) continue;
      if (v < min) min = v;
      if (v > max) max = v;
      sum += v; n++; last = v;
    }
    if (!n) return null;
    return { min, max, avg: sum / n, last };
  }

  function downsample(arr, maxPoints) {
    const n = arr.length;
    if (n <= maxPoints) return arr;
    const bucket = n / maxPoints;
    const numKeys = Object.keys(arr[0]).filter(k => k !== 'ts');
    const out = [];
    for (let i = 0; i < maxPoints; i++) {
      const a = Math.floor(i * bucket);
      let b = Math.floor((i + 1) * bucket);
      if (b <= a) b = a + 1;
      const slice = arr.slice(a, b);
      if (!slice.length) continue;
      const agg = { ts: Math.floor(slice.reduce((s, x) => s + x.ts, 0) / slice.length) };
      for (const k of numKeys) {
        let s = 0, c = 0;
        for (const x of slice) { if (x[k] != null) { s += x[k]; c++; } }
        agg[k] = c ? s / c : null;
      }
      out.push(agg);
    }
    return out;
  }

  function svgLine(samples, key, label, color) {
    const W = 720, H = 180, padL = 50, padR = 14, padT = 22, padB = 22;
    const chartW = W - padL - padR, chartH = H - padT - padB;
    if (samples.length < 2) {
      return '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" style="max-width:' + W + 'px;display:block">'
           + '<text x="50%" y="50%" fill="#888" text-anchor="middle" font-family="system-ui" font-size="13">'
           + label + ': datos insuficientes</text></svg>';
    }
    const values = samples.map(s => s[key] == null ? 0 : s[key]);
    const ts = samples.map(s => s.ts);
    let vmin = Math.min.apply(null, values), vmax = Math.max.apply(null, values);
    if (vmax === vmin) vmax = vmin + 1;
    let tmin = ts[0], tmax = ts[ts.length - 1];
    if (tmax === tmin) tmax = tmin + 1;
    const xp = t => padL + (t - tmin) / (tmax - tmin) * chartW;
    const yp = v => padT + (1 - (v - vmin) / (vmax - vmin)) * chartH;
    const pts = ts.map((t, i) => xp(t).toFixed(1) + ',' + yp(values[i]).toFixed(1)).join(' ');

    let ticks = '';
    for (let i = 0; i < 4; i++) {
      const v = vmin + (vmax - vmin) * (i / 3);
      const y = yp(v);
      ticks += '<line x1="' + padL + '" x2="' + (W - padR) + '" y1="' + y.toFixed(1) + '" y2="' + y.toFixed(1)
            + '" stroke="currentColor" stroke-opacity="0.08" stroke-width="1"/>'
            + '<text x="' + (padL - 6) + '" y="' + (y + 4).toFixed(1)
            + '" fill="currentColor" fill-opacity="0.55" text-anchor="end" font-size="10" font-family="system-ui">'
            + v.toFixed(0) + '</text>';
    }
    const span = tmax - tmin;
    const t0 = fmtTs(tmin, span), t1 = fmtTs(tmax, span);

    return '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" style="max-width:' + W + 'px;height:auto;display:block">'
         + '<text x="' + padL + '" y="14" fill="currentColor" font-size="12" font-family="system-ui" font-weight="600">' + label + '</text>'
         + '<text x="' + (W - padR) + '" y="14" fill="' + color + '" font-size="12" font-family="system-ui" text-anchor="end" font-variant-numeric="tabular-nums">'
         + values[values.length - 1].toFixed(1) + '</text>'
         + ticks
         + '<polyline fill="none" stroke="' + color + '" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round" points="' + pts + '"/>'
         + '<text x="' + padL + '" y="' + (H - 6) + '" fill="currentColor" fill-opacity="0.55" font-size="10" font-family="system-ui">' + t0 + '</text>'
         + '<text x="' + (W - padR) + '" y="' + (H - 6) + '" fill="currentColor" fill-opacity="0.55" font-size="10" font-family="system-ui" text-anchor="end">' + t1 + '</text>'
         + '</svg>';
  }

  function fmt(n, digits) {
    if (n == null || isNaN(n)) return '—';
    return digits === undefined ? n.toFixed(0) : n.toFixed(digits);
  }

  function renderStats(samples) {
    const latest = samples[samples.length - 1];
    const ramUsed = (latest.ram_total_mb || 0) - (latest.ram_free_mb || 0);
    const ramPct = latest.ram_total_mb ? (ramUsed / latest.ram_total_mb * 100) : 0;
    const cpuSamples = samples.map(s => ({ cpu_used: s.cpu_idle == null ? null : 100 - s.cpu_idle }));
    const cpuAgg = aggregate(cpuSamples, 'cpu_used');
    const ramFreeAgg = aggregate(samples, 'ram_free_mb');
    const loadAgg = aggregate(samples, 'load_1');
    const cpuLast = latest.cpu_idle == null ? null : 100 - latest.cpu_idle;

    const cards = [];
    cards.push('<div class="stat"><div class="label">RAM total</div><div class="value">' + fmt(latest.ram_total_mb / 1024) + ' GB</div></div>');
    cards.push('<div class="stat"><div class="label">RAM en uso (final)</div><div class="value">' + fmt(ramUsed / 1024, 1) + ' GB</div><div class="muted">' + fmt(ramPct) + '%</div></div>');
    cards.push('<div class="stat"><div class="label">RAM libre</div><div class="value">' + fmt(latest.ram_free_mb) + ' MB</div><div class="muted">min ' + (ramFreeAgg ? fmt(ramFreeAgg.min) : '—') + ' · prom ' + (ramFreeAgg ? fmt(ramFreeAgg.avg) : '—') + '</div></div>');
    cards.push('<div class="stat"><div class="label">Comprimido</div><div class="value">' + fmt(latest.ram_compressed_mb) + ' MB</div></div>');
    cards.push('<div class="stat"><div class="label">CPU usado</div><div class="value">' + fmt(cpuLast) + '%</div><div class="muted">prom ' + (cpuAgg ? fmt(cpuAgg.avg) : '—') + ' · máx ' + (cpuAgg ? fmt(cpuAgg.max) : '—') + '</div></div>');
    cards.push('<div class="stat"><div class="label">Load 1m</div><div class="value">' + fmt(latest.load_1, 2) + '</div><div class="muted">prom ' + (loadAgg ? fmt(loadAgg.avg, 2) : '—') + ' · máx ' + (loadAgg ? fmt(loadAgg.max, 2) : '—') + '</div></div>');
    cards.push('<div class="stat"><div class="label">Disco /</div><div class="value">' + fmt(latest.disk_pct) + '%</div><div class="muted">' + fmt(latest.disk_used_gb) + ' / ' + fmt(latest.disk_total_gb) + ' GB</div></div>');
    if (latest.battery_pct != null) {
      const ch = latest.battery_charging ? 'cargando' : 'batería';
      cards.push('<div class="stat"><div class="label">Batería</div><div class="value">' + latest.battery_pct + '%</div><div class="muted">' + ch + '</div></div>');
    }
    document.getElementById('stats').innerHTML = cards.join('');
  }

  function renderCharts(samples) {
    const enriched = samples.map(s => Object.assign({}, s, { cpu_used: s.cpu_idle == null ? null : 100 - s.cpu_idle }));
    const ds = downsample(enriched, 800);
    document.getElementById('charts').innerHTML = [
      '<div class="chart-card">' + svgLine(ds, 'ram_free_mb', 'RAM libre (MB)', '#7aa2f7') + '</div>',
      '<div class="chart-card">' + svgLine(ds, 'ram_compressed_mb', 'Compresor (MB)', '#e0af68') + '</div>',
      '<div class="chart-card">' + svgLine(ds, 'cpu_used', 'CPU usado (%)', '#f7768e') + '</div>',
      '<div class="chart-card">' + svgLine(ds, 'load_1', 'Load avg (1 min)', '#9ece6a') + '</div>',
    ].join('');
  }

  function renderTopApps(rows) {
    const groups = {};
    for (const r of rows) {
      if (!groups[r.app]) groups[r.app] = { rss: 0, np: 0, n: 0 };
      groups[r.app].rss += r.rss_mb;
      groups[r.app].np += r.num_procs;
      groups[r.app].n++;
    }
    const list = Object.keys(groups).map(app => ({
      app,
      rss: groups[app].rss / groups[app].n,
      np: groups[app].np / groups[app].n,
    })).sort((a, b) => b.rss - a.rss).slice(0, 10);

    const tbody = document.getElementById('top-apps');
    if (!list.length) {
      tbody.innerHTML = '<tr><td colspan="3" style="text-align:center;color:var(--muted)">sin datos en la ventana</td></tr>';
      return;
    }
    tbody.innerHTML = list.map(r =>
      '<tr><td>' + r.app + '</td><td class="num">' + fmt(r.rss) + ' MB</td><td class="num">' + fmt(r.np, 1) + '</td></tr>'
    ).join('');
  }

  function renderRangeInfo(samples) {
    const el = document.getElementById('range-info');
    if (!samples.length) {
      el.textContent = 'Sin muestras en la ventana seleccionada.';
      return;
    }
    const span = samples[samples.length - 1].ts - samples[0].ts;
    const word = samples.length === 1 ? 'muestra' : 'muestras';
    el.textContent = 'Mostrando ' + samples.length + ' ' + word
      + ' · ' + fmtTs(samples[0].ts, span) + ' → ' + fmtTs(samples[samples.length - 1].ts, span);
  }

  function apply(saveToStorage) {
    const from = inputToUnix(fromInput.value);
    const to = inputToUnix(toInput.value);
    if (from == null || to == null || to < from) return;

    const samples = filterSamples(from, to);
    const tops = filterTopApps(from, to);
    renderRangeInfo(samples);

    if (!samples.length) {
      document.getElementById('stats').innerHTML = '';
      document.getElementById('charts').innerHTML = '';
      renderTopApps([]);
    } else {
      renderStats(samples);
      renderCharts(samples);
      renderTopApps(tops);
    }

    if (saveToStorage !== false) {
      try {
        const activePreset = document.querySelector('.preset.active');
        localStorage.setItem(STORAGE_RANGE, JSON.stringify({
          from: fromInput.value,
          to: toInput.value,
          preset: activePreset ? activePreset.dataset.preset : null,
        }));
      } catch (e) { /* ignore */ }
    }
  }

  const PRESET_SPANS = { '1h': 3600, '6h': 21600, '24h': 86400, '7d': 604800 };

  function applyPreset(preset, persist) {
    presetButtons.forEach(b => b.classList.remove('active'));
    const btn = document.querySelector('.preset[data-preset="' + preset + '"]');
    if (btn) btn.classList.add('active');
    let from;
    if (preset === 'all') {
      from = FIRST_TS;
    } else {
      const span = PRESET_SPANS[preset] || 86400;
      from = Math.max(FIRST_TS, LAST_TS - span);
    }
    fromInput.value = unixToLocalInput(from);
    toInput.value = unixToLocalInput(LAST_TS);
    apply(persist !== false);
  }

  function clearPresetActive() {
    presetButtons.forEach(b => b.classList.remove('active'));
  }

  fromInput.addEventListener('change', () => { clearPresetActive(); apply(true); });
  toInput.addEventListener('change', () => { clearPresetActive(); apply(true); });
  presetButtons.forEach(btn => btn.addEventListener('click', () => applyPreset(btn.dataset.preset, true)));

  // Auto-refresh
  let refreshTimer = null;
  function setAutoRefresh(on) {
    if (on) {
      autoBtn.classList.add('on'); autoBtn.classList.remove('off');
      autoBtn.textContent = 'Auto-refresh: ON';
      if (!refreshTimer) {
        refreshTimer = setInterval(() => {
          if (document.visibilityState === 'visible') location.reload();
        }, 30000);
      }
    } else {
      autoBtn.classList.add('off'); autoBtn.classList.remove('on');
      autoBtn.textContent = 'Auto-refresh: OFF';
      if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; }
    }
    try { localStorage.setItem(STORAGE_AUTO, on ? 'on' : 'off'); } catch (e) {}
  }
  autoBtn.addEventListener('click', () => setAutoRefresh(!autoBtn.classList.contains('on')));

  // Restaurar estado desde localStorage
  let restored = false;
  try {
    const saved = localStorage.getItem(STORAGE_RANGE);
    if (saved) {
      const r = JSON.parse(saved);
      if (r.preset && PRESET_SPANS[r.preset] !== undefined || r.preset === 'all') {
        applyPreset(r.preset, false);
        restored = true;
      } else if (r.from && r.to) {
        fromInput.value = r.from;
        toInput.value = r.to;
        apply(false);
        restored = true;
      }
    }
  } catch (e) { /* ignore */ }

  if (!restored) {
    const span = LAST_TS - FIRST_TS;
    applyPreset(span <= 86400 ? 'all' : '24h', false);
  }

  try {
    const auto = localStorage.getItem(STORAGE_AUTO);
    setAutoRefresh(auto !== 'off');
  } catch (e) { setAutoRefresh(true); }
})();
</script>
</body>
</html>"""


def render_html(conn) -> str:
    all_samples = fetch_all(conn)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if not all_samples:
        body = '<div class="container"><h1>mac-monitor</h1><p>No hay datos todavía. Corré <code>./bin/mac-monitor now</code>.</p></div>'
        return f"<!DOCTYPE html><html><head><meta charset='UTF-8'><title>mac-monitor</title></head><body>{body}</body></html>"

    top_apps = fetch_top_apps_all(conn)
    t_min = all_samples[0]["ts"]
    t_min_str = datetime.fromtimestamp(t_min).strftime("%Y-%m-%d %H:%M")

    payload = {"samples": all_samples, "topApps": top_apps}
    data_json = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")

    return (
        HTML_TEMPLATE
        .replace("__NOW__", now)
        .replace("__NTOTAL__", str(len(all_samples)))
        .replace("__TMIN__", t_min_str)
        .replace("__DATA_JSON__", data_json)
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
