#!/usr/bin/env python3
"""
Cruise Control — GB10 Inbound Bandwidth Throttle
Limits inbound (download) traffic via Linux tc + IFB.
Serves a live web GUI on port 8090.
Runs as root. Config persisted to config.json alongside this file.
"""

import subprocess, os, json, re, socket, threading, time, math, datetime
import urllib.request as _urllib_req
from flask import Flask, request, redirect, url_for, Response, render_template_string, send_from_directory

IFACE        = os.environ.get("THROTTLE_IFACE", "enP7s7")
IFB_DEV      = "ifb0"
SPEEDTEST_URL = os.environ.get("SPEEDTEST_URL", "http://speed.hetzner.de/100MB.bin")

# Geo-tagged server catalog — sorted by distance at runtime via /api/speedtest/servers
SPEEDTEST_SERVER_CATALOG = [
    # North America — East
    {"label": "Linode — 100 MB (Newark, US)",       "url": "http://speedtest.newark.linode.com/100MB-newark.bin",        "lat": 40.74, "lon": -74.17},
    {"label": "Linode — 100 MB (Toronto, CA)",      "url": "http://speedtest.toronto1.linode.com/100MB-toronto.bin",     "lat": 43.65, "lon": -79.38},
    # North America — Central
    {"label": "Linode — 100 MB (Dallas, US)",       "url": "http://speedtest.dallas.linode.com/100MB-dallas.bin",        "lat": 32.78, "lon": -96.80},
    # North America — West
    {"label": "Linode — 100 MB (Fremont, US)",      "url": "http://speedtest.fremont.linode.com/100MB-fremont.bin",      "lat": 37.55, "lon": -121.99},
    # Europe
    {"label": "Hetzner — 100 MB (Nuremberg, DE)",   "url": "http://speed.hetzner.de/100MB.bin",                         "lat": 49.45, "lon": 11.08},
    {"label": "Hetzner — 1 GB (Nuremberg, DE)",     "url": "http://speed.hetzner.de/1GB.bin",                           "lat": 49.45, "lon": 11.08},
    {"label": "Hetzner — 10 GB (Nuremberg, DE)",    "url": "http://speed.hetzner.de/10GB.bin",                          "lat": 49.45, "lon": 11.08},
    {"label": "Tele2 — 100 MB (Stockholm, SE)",     "url": "http://speedtest.tele2.net/100MB.zip",                      "lat": 59.33, "lon": 18.07},
    {"label": "Tele2 — 1 GB (Stockholm, SE)",       "url": "http://speedtest.tele2.net/1GB.zip",                        "lat": 59.33, "lon": 18.07},
    {"label": "Linode — 100 MB (London, UK)",       "url": "http://speedtest.london.linode.com/100MB-london.bin",       "lat": 51.51, "lon": -0.12},
    {"label": "Linode — 100 MB (Frankfurt, DE)",    "url": "http://speedtest.frankfurt.linode.com/100MB-frankfurt.bin", "lat": 50.11, "lon":  8.68},
    # Asia Pacific
    {"label": "Linode — 100 MB (Singapore)",        "url": "http://speedtest.singapore.linode.com/100MB-singapore.bin", "lat":  1.35, "lon": 103.82},
    {"label": "Linode — 100 MB (Tokyo, JP)",        "url": "http://speedtest.tokyo2.linode.com/100MB-tokyo.bin",        "lat": 35.69, "lon": 139.69},
]
CONFIG_PATH  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
HOSTNAME     = socket.gethostname()
BUILTINS     = ["business", "heavy"]
DEFAULTS     = {
    "business": {"rate": "1gbit",   "label": "Business Hours", "builtin": True},
    "heavy":    {"rate": "200mbit", "label": "Heavy Throttle", "builtin": True},
}
RATE_RE = re.compile(r"^\d+(\.\d+)?\s*(kbit|mbit|gbit|kbps|mbps|gbps)$", re.IGNORECASE)
SLUG_RE = re.compile(r"[^a-z0-9]+")

app = Flask(__name__)


def _haversine(lat1, lon1, lat2, lon2):
    """Great-circle distance in km between two lat/lon points."""
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(min(1.0, a)))


# ── Config ────────────────────────────────────────────────────────────────────

def load_config():
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    for key, cfg in DEFAULTS.items():
        if key not in data:
            data[key] = dict(cfg)
        data[key]["builtin"] = True
    return data

def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)

def slugify(label):
    return SLUG_RE.sub("_", label.lower()).strip("_")[:32]

def rate_to_bps(rate_str):
    """Convert tc rate string to bits per second."""
    m = re.match(r"^(\d+(?:\.\d+)?)\s*(kbit|mbit|gbit|kbps|mbps|gbps)$", rate_str.strip(), re.I)
    if not m:
        return 0
    val, unit = float(m.group(1)), m.group(2).lower()
    mult = {"kbit": 1e3, "mbit": 1e6, "gbit": 1e9, "kbps": 8e3, "mbps": 8e6, "gbps": 8e9}
    return int(val * mult.get(unit, 1))


# ── Live stats ────────────────────────────────────────────────────────────────

_stats      = {"rx_bps": 0, "tx_bps": 0}
_stats_lock = threading.Lock()

def _read_iface_bytes():
    try:
        with open("/proc/net/dev") as f:
            for line in f:
                if IFACE + ":" in line:
                    parts = line.split()
                    return int(parts[1]), int(parts[9])  # rx_bytes, tx_bytes
    except Exception:
        pass
    return 0, 0

def _stats_collector():
    prev_rx, prev_tx = _read_iface_bytes()
    while True:
        time.sleep(1)
        rx, tx = _read_iface_bytes()
        with _stats_lock:
            _stats["rx_bps"] = max(0, (rx - prev_rx) * 8)
            _stats["tx_bps"] = max(0, (tx - prev_tx) * 8)
        prev_rx, prev_tx = rx, tx

threading.Thread(target=_stats_collector, daemon=True).start()


# ── Speed test ────────────────────────────────────────────────────────────────

_stest      = {"status": "idle", "speed_bps": 0, "progress": 0, "error": ""}
_stest_lock = threading.Lock()

def _run_speedtest(url=None):
    """
    Download via curl (handles auth/TLS/redirects cleanly) and report speed.
    While curl runs, sample live RX from the stats thread every 500 ms so the
    UI shows a real-time speed number during the test.
    """
    target = url or SPEEDTEST_URL
    with _stest_lock:
        _stest.update({"status": "running", "speed_bps": 0, "progress": 0, "error": ""})

    MAX_SECS = 12
    try:
        proc = subprocess.Popen(
            ["curl", "-o", "/dev/null", "-s", "-L",
             "--max-time", str(MAX_SECS),
             "-w", "%{speed_download}",   # bytes/sec printed to stdout on exit
             target],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        start = time.monotonic()
        while proc.poll() is None:
            time.sleep(0.5)
            elapsed = time.monotonic() - start
            # Borrow live RX from the stats thread as the "current" speed
            with _stats_lock:
                live_rx = _stats["rx_bps"]
            progress = min(99, int(elapsed / MAX_SECS * 100))
            with _stest_lock:
                _stest.update({"speed_bps": live_rx, "progress": progress})

        stdout, _ = proc.communicate()
        # rc=0 → completed; rc=28 → max-time hit on a partial download.
        # curl still writes %{speed_download} to stdout in both cases.
        if proc.returncode in (0, 28):
            bytes_per_sec = float(stdout.decode().strip() or "0")
            with _stest_lock:
                _stest.update({
                    "status": "done",
                    "speed_bps": int(bytes_per_sec * 8),
                    "progress": 100,
                })
        else:
            with _stest_lock:
                _stest.update({"status": "error", "error": f"curl exit {proc.returncode}"})
    except Exception as exc:
        with _stest_lock:
            _stest.update({"status": "error", "error": str(exc)})


# ── tc / IFB ──────────────────────────────────────────────────────────────────

def run(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)

def clear_all():
    run(f"tc qdisc del dev {IFACE} ingress 2>/dev/null || true")
    run(f"tc qdisc del dev {IFACE} root    2>/dev/null || true")
    run(f"tc qdisc del dev {IFB_DEV} root  2>/dev/null || true")
    run(f"ip link set {IFB_DEV} down       2>/dev/null || true")
    run(f"ip link del {IFB_DEV}            2>/dev/null || true")

def apply_ingress_limit(rate):
    clear_all()
    run("modprobe ifb numifbs=1")
    run(f"ip link add {IFB_DEV} type ifb 2>/dev/null || true")
    r = run(f"ip link set {IFB_DEV} up")
    if r.returncode != 0:
        return False, f"Could not bring up {IFB_DEV}: {r.stderr.strip()}"
    run(f"tc qdisc add dev {IFACE} handle ffff: ingress")
    r = run(f"tc filter add dev {IFACE} parent ffff: protocol ip "
            f"u32 match u32 0 0 action mirred egress redirect dev {IFB_DEV}")
    if r.returncode != 0:
        clear_all()
        return False, f"tc filter error: {r.stderr.strip()}"
    r1 = run(f"tc qdisc add dev {IFB_DEV} root handle 1: htb default 10")
    r2 = run(f"tc class add dev {IFB_DEV} parent 1: classid 1:10 "
             f"htb rate {rate} ceil {rate} burst 256k")
    if r1.returncode != 0 or r2.returncode != 0:
        clear_all()
        return False, f"tc HTB error: {(r1.stderr or r2.stderr).strip()}"
    return True, f"Limiting inbound to {rate}"

def get_link_speed_bps():
    """Read the negotiated link speed from sysfs. Returns bits per second, 0 if unknown."""
    try:
        with open(f"/sys/class/net/{IFACE}/speed") as f:
            mbps = int(f.read().strip())
        return mbps * 1_000_000 if mbps > 0 else 0
    except Exception:
        return 0


def get_status(cfg):
    result  = run(f"tc class show dev {IFB_DEV}")
    active  = "clear"
    limit_bps = 0
    tc_raw  = "no limit — full line rate"
    for line in result.stdout.splitlines():
        if "rate" not in line:
            continue
        parts = line.split()
        if "rate" not in parts:
            continue
        idx = parts.index("rate")
        live = (parts[idx + 1] if idx + 1 < len(parts) else "").lower()
        tc_raw = f"inbound capped at {live}"
        for key, mcfg in cfg.items():
            if mcfg.get("rate", "").lower() == live:
                active = key
                break
        else:
            active = "custom"
        limit_bps = rate_to_bps(live)
        break
    return active, tc_raw, limit_bps


# ── Scheduler ─────────────────────────────────────────────────────────────────

_sched_lock      = threading.Lock()
_sched_active_id = None   # id of the schedule currently being enforced, or None

def _get_schedules(cfg):
    return cfg.get("schedules", [])

def _schedule_matches_now(sched):
    now     = datetime.datetime.now()
    weekday = now.weekday()          # 0=Mon … 6=Sun
    if weekday not in sched.get("days", []):
        return False
    current = now.strftime("%H:%M")
    return sched["start"] <= current < sched["end"]

def _scheduler_tick():
    global _sched_active_id
    cfg       = load_config()
    schedules = [s for s in _get_schedules(cfg) if s.get("enabled", True)]

    matched = next((s for s in schedules if _schedule_matches_now(s)), None)

    with _sched_lock:
        prev_id = _sched_active_id
        new_id  = matched["id"] if matched else None

    if new_id == prev_id:
        return  # nothing changed

    if matched:
        preset_key = matched.get("preset", "")
        if preset_key in cfg and "rate" in cfg[preset_key]:
            apply_ingress_limit(cfg[preset_key]["rate"])
    else:
        # A scheduler-managed window just ended — revert to unrestricted
        clear_all()

    with _sched_lock:
        _sched_active_id = new_id

def _scheduler_loop():
    while True:
        try:
            _scheduler_tick()
        except Exception:
            pass
        time.sleep(30)

threading.Thread(target=_scheduler_loop, daemon=True).start()


# ── API routes ────────────────────────────────────────────────────────────────

@app.route("/api/stats")
def api_stats():
    with _stats_lock:
        rx, tx = _stats["rx_bps"], _stats["tx_bps"]
    cfg = load_config()
    active, tc_raw, limit_bps = get_status(cfg)
    label = cfg[active]["label"] if active in cfg else ("Unrestricted" if active == "clear" else "Custom")
    return Response(json.dumps({
        "rx_bps": rx, "tx_bps": tx,
        "mode": active, "mode_label": label,
        "limit_bps": limit_bps, "tc_raw": tc_raw,
        "link_speed_bps": get_link_speed_bps(),
    }), mimetype="application/json")

@app.route("/api/speedtest/start", methods=["POST"])
def api_speedtest_start():
    with _stest_lock:
        if _stest["status"] == "running":
            return Response(json.dumps({"ok": False, "msg": "Already running"}), mimetype="application/json")
    url = request.form.get("url", "").strip() or SPEEDTEST_URL
    # Reject obviously bad URLs
    if not url.startswith(("http://", "https://")):
        return Response(json.dumps({"ok": False, "msg": "Invalid URL"}), mimetype="application/json")
    threading.Thread(target=_run_speedtest, args=(url,), daemon=True).start()
    return Response(json.dumps({"ok": True}), mimetype="application/json")

@app.route("/api/speedtest/status")
def api_speedtest_status():
    with _stest_lock:
        data = dict(_stest)
    return Response(json.dumps(data), mimetype="application/json")

@app.route("/api/speedtest/servers")
def api_speedtest_servers():
    """Return the server catalog sorted by distance from this machine's public IP location."""
    lat = lon = None
    location = None
    try:
        with _urllib_req.urlopen(
            "http://ip-api.com/json/?fields=lat,lon,city,country", timeout=5
        ) as resp:
            geo = json.loads(resp.read().decode())
        lat, lon = float(geo["lat"]), float(geo["lon"])
        location = ", ".join(filter(None, [geo.get("city"), geo.get("country")]))
    except Exception:
        pass

    servers = []
    for s in SPEEDTEST_SERVER_CATALOG:
        if lat is not None:
            dist = round(_haversine(lat, lon, s["lat"], s["lon"]))
        else:
            dist = None
        servers.append({"label": s["label"], "url": s["url"], "distance_km": dist})

    if lat is not None:
        servers.sort(key=lambda x: x["distance_km"])

    return Response(
        json.dumps({"servers": servers, "location": location}),
        mimetype="application/json",
    )


@app.route("/api/schedules")
def api_schedules():
    cfg = load_config()
    with _sched_lock:
        active_id = _sched_active_id
    return Response(
        json.dumps({"schedules": _get_schedules(cfg), "active_id": active_id}),
        mimetype="application/json",
    )

@app.route("/api/schedules/add", methods=["POST"])
def api_schedule_add():
    data = request.get_json(force=True) or {}
    preset = data.get("preset", "")
    days   = data.get("days", [])
    start  = data.get("start", "")
    end    = data.get("end", "")
    if not preset or not days or not start or not end:
        return Response(json.dumps({"ok": False, "msg": "Missing fields"}), mimetype="application/json")
    if start >= end:
        return Response(json.dumps({"ok": False, "msg": "Start must be before end"}), mimetype="application/json")
    cfg = load_config()
    if preset not in cfg or "rate" not in cfg.get(preset, {}):
        return Response(json.dumps({"ok": False, "msg": "Unknown preset"}), mimetype="application/json")
    sched = {
        "id":      f"s{int(time.time() * 1000)}",
        "preset":  preset,
        "days":    sorted(int(d) for d in days),
        "start":   start,
        "end":     end,
        "enabled": True,
    }
    schedules = _get_schedules(cfg)
    schedules.append(sched)
    cfg["schedules"] = schedules
    save_config(cfg)
    return Response(json.dumps({"ok": True, "schedule": sched}), mimetype="application/json")

@app.route("/api/schedules/<sid>/delete", methods=["POST"])
def api_schedule_delete(sid):
    cfg       = load_config()
    schedules = [s for s in _get_schedules(cfg) if s["id"] != sid]
    cfg["schedules"] = schedules
    save_config(cfg)
    return Response(json.dumps({"ok": True}), mimetype="application/json")

@app.route("/api/schedules/<sid>/toggle", methods=["POST"])
def api_schedule_toggle(sid):
    cfg       = load_config()
    schedules = _get_schedules(cfg)
    for s in schedules:
        if s["id"] == sid:
            s["enabled"] = not s.get("enabled", True)
            break
    cfg["schedules"] = schedules
    save_config(cfg)
    return Response(json.dumps({"ok": True}), mimetype="application/json")


# ── Page ──────────────────────────────────────────────────────────────────────

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cruise Control</title>
<style>
:root {
  --bg:      #08090d;
  --surface: #0f1117;
  --card:    #141720;
  --border:  #1e2130;
  --border2: #252a3a;
  --text:    #d4d8e8;
  --muted:   #5a607a;
  --dim:     #3a4060;
  --blue:    #4d9fff;
  --blue-d:  #1a3a6a;
  --green:   #3dd68c;
  --green-d: #0f2e20;
  --amber:   #f0b429;
  --amber-d: #2a1f00;
  --red:     #f25c54;
  --red-d:   #2a0d0b;
  --purple:  #a78bfa;
}
* { box-sizing:border-box; margin:0; padding:0; }
body {
  background:var(--bg); color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  font-size:14px; line-height:1.5;
  min-height:100vh; display:flex; flex-direction:column;
  align-items:center; padding:2rem 1rem;
}
a { color:var(--blue); }

/* ── Header ── */
.header {
  width:100%; max-width:640px;
  display:flex; align-items:center; justify-content:space-between;
  margin-bottom:1.5rem;
}
.title-text {
  font-family:"Arial Black","Franklin Gothic Heavy","Impact",sans-serif;
  font-size:1.65rem; font-weight:900; font-style:italic;
  letter-spacing:0.06em; text-transform:uppercase;
  background:linear-gradient(180deg, #e8f8ff 0%, #a8e8ff 25%, #38c8f8 60%, #0890c8 100%);
  -webkit-background-clip:text; -webkit-text-fill-color:transparent;
  background-clip:text;
  filter:drop-shadow(0 0 8px rgba(56,200,248,0.65));
}
.header h1 span { color:var(--blue); }
.live-dot {
  display:inline-flex; align-items:center; gap:6px;
  font-size:0.72rem; font-weight:600; letter-spacing:0.08em;
  text-transform:uppercase; color:var(--green);
}
.live-dot::before {
  content:''; width:7px; height:7px; border-radius:50%;
  background:var(--green); flex-shrink:0;
  animation:pulse 2s ease-in-out infinite;
}
@keyframes pulse {
  0%,100% { opacity:1; } 50% { opacity:.35; }
}
.subhead {
  font-size:0.78rem; color:var(--muted); margin-bottom:1.5rem;
  font-family:monospace;
}

/* ── Card ── */
.card {
  background:var(--card); border:1px solid var(--border);
  border-radius:10px; padding:1.25rem 1.5rem;
  width:100%; max-width:640px; margin-bottom:1rem;
}
.card-title {
  font-size:0.68rem; font-weight:700; letter-spacing:0.12em;
  text-transform:uppercase; color:var(--muted); margin-bottom:1rem;
}

/* ── Live stats ── */
.stats-grid {
  display:grid; grid-template-columns:1fr 1fr; gap:1rem;
}
.stat-box { }
.stat-label {
  font-size:0.68rem; font-weight:600; letter-spacing:0.1em;
  text-transform:uppercase; color:var(--muted); margin-bottom:0.2rem;
}
.stat-val {
  font-size:2rem; font-weight:700; font-family:monospace;
  letter-spacing:-0.02em; color:#fff; line-height:1;
  transition:color 0.3s;
}
.stat-val.rx { color:var(--blue); }
.stat-val.tx { color:var(--muted); font-size:1.2rem; }
.stat-unit { font-size:0.8rem; font-weight:500; margin-left:2px; color:var(--muted); }
.speed-context { font-size:0.68rem; font-weight:600; letter-spacing:0.06em; color:var(--muted); margin-top:0.35rem; font-family:monospace; }

.mode-badge {
  margin-top:1rem; padding:0.5rem 0.75rem;
  background:var(--surface); border:1px solid var(--border);
  border-radius:6px; font-size:0.82rem;
  display:flex; align-items:center; justify-content:space-between;
}
.mode-badge .label { color:var(--muted); }
.mode-badge .value { font-weight:600; }
.mode-badge .value.clear  { color:var(--green); }
.mode-badge .value.active { color:var(--blue);  }
.mode-badge .value.custom { color:var(--purple);}
.mode-badge .tc-raw {
  font-family:monospace; font-size:0.72rem; color:var(--dim);
}

/* Speed bar */
.speed-bar-wrap {
  margin-top:0.9rem; height:3px;
  background:var(--border); border-radius:2px; overflow:hidden;
}
.speed-bar {
  height:100%; width:0%;
  background:linear-gradient(90deg, var(--blue-d), var(--blue));
  border-radius:2px; transition:width 0.8s ease;
}

/* ── Flash ── */
.flash {
  width:100%; max-width:640px;
  border-radius:6px; padding:0.6rem 1rem; margin-bottom:1rem;
  font-size:0.875rem;
}
.flash.ok    { background:var(--green-d); border:1px solid var(--green); color:var(--green); }
.flash.error { background:var(--red-d);   border:1px solid var(--red);   color:var(--red);   }

/* ── Section label ── */
.section-label {
  font-size:0.68rem; font-weight:700; letter-spacing:0.12em;
  text-transform:uppercase; color:var(--muted);
  margin-bottom:0.6rem; margin-top:0.25rem;
}

/* ── Mode rows ── */
.mode-row {
  display:flex; flex-direction:column; gap:0.3rem; margin-bottom:0.75rem;
}
.apply-btn {
  flex:1; padding:0.7rem 1rem;
  border:1px solid var(--border2); border-radius:8px;
  border-left:3px solid var(--dim);
  background:var(--surface); color:var(--text);
  font-size:0.88rem; font-weight:600;
  cursor:pointer; text-align:left;
  display:flex; align-items:center; justify-content:space-between;
  transition:border-color .15s, background .15s, border-left-color .15s;
}
.apply-btn:hover {
  border-color:var(--blue); border-left-color:var(--blue);
  background:var(--blue-d);
}
.apply-btn.is-active {
  border-color:var(--blue); border-left-color:var(--blue);
  background:var(--blue-d); color:var(--blue);
}
.apply-btn-text { flex:1; }
.apply-btn .sub {
  display:block; font-size:0.72rem; font-weight:400;
  font-family:monospace; color:var(--muted); margin-top:2px;
}
.apply-btn.is-active .sub { color:var(--blue); opacity:.7; }
.apply-btn .apply-arrow {
  font-size:1rem; color:var(--dim); margin-left:0.5rem; flex-shrink:0;
  transition:color .15s, transform .15s;
}
.apply-btn:hover .apply-arrow { color:var(--blue); transform:translateX(2px); }
.apply-btn.is-active .apply-arrow { color:var(--blue); }

/* ── Rate chip control ── */
.rate-row { display:flex; align-items:center; gap:0.5rem; }
.rate-ctl { display:flex; align-items:center; gap:0.25rem; flex:1; flex-wrap:wrap; }
.rate-chip {
  padding:0.28rem 0.52rem;
  background:var(--surface); border:1px solid var(--border2);
  border-radius:5px; color:var(--muted);
  font-size:0.74rem; font-weight:600; font-family:monospace;
  cursor:pointer; white-space:nowrap; line-height:1;
  transition:border-color .12s, color .12s, background .12s;
}
.rate-chip:hover    { border-color:var(--blue); color:var(--blue); }
.rate-chip.is-active{ border-color:var(--blue); color:var(--blue); background:var(--blue-d); }
.rate-custom-row { display:none; align-items:center; gap:0.2rem; }
.rate-custom-row.open { display:flex; }
.rate-num {
  width:50px; padding:0.28rem 0.3rem; text-align:right;
  background:var(--surface); border:1px solid var(--border2);
  border-radius:5px; color:var(--text);
  font-size:0.76rem; font-family:monospace;
}
.rate-num:focus { outline:none; border-color:var(--blue); }
.rate-unit {
  padding:0.28rem 0.3rem;
  background:var(--surface); border:1px solid var(--border2);
  border-radius:5px; color:var(--text);
  font-size:0.74rem; cursor:pointer;
  appearance:none; -webkit-appearance:none;
}
.rate-unit:focus { outline:none; border-color:var(--blue); }

.icon-btn {
  padding:0.5rem 0.65rem;
  background:none; border:1px solid var(--border2);
  border-radius:6px; color:var(--muted);
  font-size:0.78rem; cursor:pointer; white-space:nowrap;
}
.icon-btn:hover         { border-color:var(--blue);  color:var(--blue);  }
.icon-btn.del:hover     { border-color:var(--red);   color:var(--red);   }

.clear-btn {
  width:100%; padding:0.7rem 1rem;
  background:var(--green-d); border:1px solid var(--green);
  border-radius:8px; color:var(--green);
  font-size:0.88rem; font-weight:600; cursor:pointer; text-align:center;
  transition:opacity .15s;
}
.clear-btn:hover { opacity:.8; }
.clear-btn.is-active { filter:brightness(1.2); }

/* ── Add preset ── */
.add-row { display:flex; gap:0.5rem; align-items:center; flex-wrap:wrap; }
.add-row input {
  background:var(--surface); border:1px solid var(--border2);
  border-radius:6px; color:var(--text);
  font-size:0.85rem; padding:0.5rem 0.75rem;
}
.add-row input:focus { outline:none; border-color:var(--blue); }
.name-input { flex:1; min-width:140px; }
.add-btn {
  padding:0.5rem 1rem;
  background:var(--blue-d); border:1px solid var(--blue);
  border-radius:6px; color:var(--blue);
  font-size:0.85rem; font-weight:600; cursor:pointer; white-space:nowrap;
}
.add-btn:hover { opacity:.85; }
.hint { font-size:0.68rem; color:var(--dim); margin-top:0.4rem; }

/* ── Speed test ── */
.st-controls { display:flex; gap:0.5rem; align-items:center; }
.st-select {
  flex:1; padding:0.6rem 0.75rem;
  background:var(--surface); border:1px solid var(--border2);
  border-radius:8px; color:var(--text);
  font-size:0.85rem; cursor:pointer;
  appearance:none; -webkit-appearance:none;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath d='M1 1l5 5 5-5' stroke='%235a607a' stroke-width='1.5' fill='none' stroke-linecap='round'/%3E%3C/svg%3E");
  background-repeat:no-repeat; background-position:right 0.75rem center;
  padding-right:2rem;
}
.st-select:focus { outline:none; border-color:var(--blue); }
.st-select option { background:var(--card); }
.st-custom {
  margin-top:0.5rem; width:100%; padding:0.5rem 0.75rem;
  background:var(--surface); border:1px solid var(--blue);
  border-radius:6px; color:var(--text); font-size:0.82rem;
  font-family:monospace;
}
.st-custom:focus { outline:none; }
.st-btn {
  padding:0.6rem 1.1rem; white-space:nowrap;
  background:var(--surface); border:1px solid var(--border2);
  border-radius:8px; color:var(--text);
  font-size:0.88rem; font-weight:600; cursor:pointer;
  transition:border-color .15s;
}
.st-btn:hover:not(:disabled) { border-color:var(--blue); }
.st-btn:disabled { opacity:.4; cursor:not-allowed; }
.st-result {
  margin-top:1rem; display:none;
}
.st-speed {
  font-size:2rem; font-weight:700; font-family:monospace;
  color:var(--amber); letter-spacing:-0.02em;
}
.st-speed .unit { font-size:0.85rem; color:var(--muted); margin-left:3px; }
.st-bar-wrap {
  margin-top:0.6rem; height:4px;
  background:var(--border); border-radius:2px; overflow:hidden;
}
.st-bar {
  height:100%; width:0%;
  background:linear-gradient(90deg, var(--amber-d), var(--amber));
  border-radius:2px; transition:width 0.4s ease;
}
.st-label {
  margin-top:0.4rem; font-size:0.72rem;
  font-family:monospace; color:var(--muted);
}
.st-error { color:var(--red); font-size:0.82rem; margin-top:0.5rem; }
.st-geo-btn {
  padding:0.6rem 0.8rem; white-space:nowrap; flex-shrink:0;
  background:var(--surface); border:1px solid var(--border2);
  border-radius:8px; color:var(--muted);
  font-size:0.82rem; font-weight:600; cursor:pointer;
  transition:border-color .15s, color .15s;
}
.st-geo-btn:hover:not(:disabled) { border-color:var(--blue); color:var(--blue); }
.st-geo-btn:disabled { opacity:.4; cursor:not-allowed; }
.st-geo-status {
  margin-top:0.4rem; font-size:0.72rem;
  font-family:monospace; color:var(--muted);
}

/* ── Scheduler ── */
.sched-day-picker {
  display:flex; gap:0.3rem; flex-wrap:wrap; margin-bottom:0.6rem;
}
.sched-day-chip {
  padding:0.28rem 0.45rem;
  background:var(--surface); border:1px solid var(--border2);
  border-radius:5px; color:var(--muted);
  font-size:0.74rem; font-weight:600; font-family:monospace;
  cursor:pointer; line-height:1; user-select:none;
  transition:border-color .12s, color .12s, background .12s;
}
.sched-day-chip:hover    { border-color:var(--blue); color:var(--blue); }
.sched-day-chip.on       { border-color:var(--blue); color:var(--blue); background:var(--blue-d); }
.sched-time-row {
  display:flex; gap:0.5rem; align-items:center; flex-wrap:wrap; margin-bottom:0.6rem;
}
.sched-time-input {
  padding:0.5rem 0.5rem;
  background:var(--surface); border:1px solid var(--border2);
  border-radius:6px; color:var(--text);
  font-size:0.85rem; font-family:monospace;
  color-scheme:dark;
}
.sched-time-input:focus { outline:none; border-color:var(--blue); }
.sched-preset-select {
  flex:1; padding:0.5rem 0.75rem;
  background:var(--surface); border:1px solid var(--border2);
  border-radius:6px; color:var(--text); font-size:0.85rem; cursor:pointer;
  appearance:none; -webkit-appearance:none;
}
.sched-preset-select:focus { outline:none; border-color:var(--blue); }
.sched-row {
  display:flex; align-items:center; gap:0.6rem;
  padding:0.6rem 0.75rem; margin-bottom:0.4rem;
  background:var(--surface); border:1px solid var(--border2);
  border-radius:8px; flex-wrap:wrap;
  transition:border-color .15s;
}
.sched-row.active-now { border-color:var(--blue); }
.sched-row.disabled   { opacity:.5; }
.sched-days-badges    { display:flex; gap:0.2rem; }
.sched-day-badge {
  padding:0.15rem 0.3rem;
  border-radius:3px; font-size:0.68rem; font-weight:700;
  font-family:monospace; line-height:1;
  background:var(--border); color:var(--dim);
}
.sched-day-badge.on { background:var(--blue-d); color:var(--blue); }
.sched-meta {
  flex:1; display:flex; align-items:center; gap:0.6rem; flex-wrap:wrap;
}
.sched-time-label {
  font-size:0.82rem; font-family:monospace; color:var(--text);
}
.sched-preset-label {
  font-size:0.72rem; font-weight:600; padding:0.15rem 0.45rem;
  border-radius:4px; background:var(--blue-d); color:var(--blue);
  white-space:nowrap;
}
.sched-active-dot {
  font-size:0.68rem; font-weight:700; letter-spacing:0.06em;
  text-transform:uppercase; color:var(--green);
  display:flex; align-items:center; gap:4px;
}
.sched-active-dot::before {
  content:''; width:6px; height:6px; border-radius:50%;
  background:var(--green); animation:pulse 2s ease-in-out infinite;
}
.sched-actions { display:flex; gap:0.3rem; margin-left:auto; flex-shrink:0; }
.sched-empty {
  font-size:0.8rem; color:var(--muted); padding:0.5rem 0;
}

.divider { border:none; border-top:1px solid var(--border); margin:0.25rem 0; }
footer {
  margin-top:2rem; font-size:0.68rem; color:var(--dim); text-align:center;
}
</style>
</head>
<body>

<div class="header">
  <span class="title-text">Cruise Control</span>
  <span class="live-dot">Live</span>
</div>
<p class="subhead">{{ iface }} &nbsp;·&nbsp; {{ hostname }}</p>

{{ flash_html | safe }}

<!-- Live stats -->
<div class="card">
  <div class="card-title">Network Activity</div>
  <div class="stats-grid">
    <div class="stat-box">
      <div class="stat-label">↓ Inbound</div>
      <div class="stat-val rx" id="rx-val">—<span class="stat-unit" id="rx-unit"></span></div>
      <div class="speed-context" id="speed-context"></div>
    </div>
    <div class="stat-box">
      <div class="stat-label">↑ Outbound</div>
      <div class="stat-val tx" id="tx-val">—<span class="stat-unit" id="tx-unit"></span></div>
    </div>
  </div>
  <div class="speed-bar-wrap"><div class="speed-bar" id="speed-bar"></div></div>
  <div class="mode-badge">
    <span class="label">Mode</span>
    <span class="value" id="mode-live">—</span>
    <span class="tc-raw" id="tc-raw-live"></span>
  </div>
</div>

<!-- Presets -->
<div class="card">
  <div class="card-title">Built-in Presets</div>
  {{ builtin_rows | safe }}
  <hr class="divider" style="margin:0.75rem 0">
  <form method="post" action="/apply">
    <input type="hidden" name="mode" value="clear">
    <button type="submit" class="clear-btn{{ clear_active }}">Unrestricted — remove all limits</button>
  </form>

  {{ custom_section | safe }}

  <hr class="divider" style="margin:1rem 0 0.75rem">
  <div class="section-label">Add Custom Preset</div>
  <form method="post" action="/add">
    <div class="add-row">
      <input class="name-input" type="text" name="label"
             placeholder="Preset name" required maxlength="40">
      <button type="submit" class="add-btn">+ Add</button>
    </div>
    <div class="rate-ctl" style="margin-top:0.4rem">
      <button type="button" class="rate-chip" data-val="100mbit">100M</button>
      <button type="button" class="rate-chip" data-val="200mbit">200M</button>
      <button type="button" class="rate-chip is-active" data-val="500mbit">500M</button>
      <button type="button" class="rate-chip" data-val="1gbit">1G</button>
      <button type="button" class="rate-chip" data-val="2gbit">2G</button>
      <button type="button" class="rate-chip" data-val="5gbit">5G</button>
      <button type="button" class="rate-chip" data-val="custom">···</button>
      <div class="rate-custom-row">
        <input type="number" class="rate-num" min="1" max="9999">
        <select class="rate-unit">
          <option value="kbit">Kbps</option>
          <option value="mbit">Mbps</option>
          <option value="gbit">Gbps</option>
        </select>
      </div>
      <input type="hidden" name="rate" class="rate-val" value="500mbit" required>
    </div>
  </form>
</div>

<!-- Speed test -->
<div class="card">
  <div class="card-title">Speed Test</div>
  <p style="font-size:0.8rem;color:var(--muted);margin-bottom:0.75rem">
    Measures this machine's inbound download speed — useful for validating
    that a preset is working as expected.
  </p>
  <div class="st-controls">
    <select class="st-select" id="st-select" onchange="onEndpointChange(this)">
      {{ endpoint_opts | safe }}
    </select>
    <button class="st-geo-btn" id="st-geo-btn" onclick="findNearestServers()" title="Auto-detect nearest servers">⊕ Nearest</button>
    <button class="st-btn" id="st-btn" onclick="startSpeedTest()">▶ Run</button>
  </div>
  <div class="st-geo-status" id="st-geo-status" style="display:none"></div>
  <input class="st-custom" id="st-custom" type="text" placeholder="https://your-server.internal/testfile.bin" style="display:none">
  <div class="st-result" id="st-result">
    <div class="st-speed"><span id="st-num">0</span><span class="unit" id="st-unit">Mbps</span></div>
    <div class="st-bar-wrap"><div class="st-bar" id="st-bar"></div></div>
    <div class="st-label" id="st-label">starting…</div>
  </div>
  <div class="st-error" id="st-error"></div>
</div>

<!-- Scheduler -->
<div class="card">
  <div class="card-title">Scheduler</div>
  <p style="font-size:0.8rem;color:var(--muted);margin-bottom:0.75rem">
    Automatically apply a preset on a schedule. When a window ends with no
    follow-on schedule, bandwidth reverts to unrestricted.
  </p>
  <div id="sched-list"><span class="sched-empty">Loading…</span></div>

  <hr class="divider" style="margin:0.75rem 0">
  <div class="section-label">Add Schedule</div>

  <div class="sched-day-picker" id="sched-day-picker">
    <button type="button" class="sched-day-chip" data-day="0">Mo</button>
    <button type="button" class="sched-day-chip" data-day="1">Tu</button>
    <button type="button" class="sched-day-chip" data-day="2">We</button>
    <button type="button" class="sched-day-chip" data-day="3">Th</button>
    <button type="button" class="sched-day-chip" data-day="4">Fr</button>
    <button type="button" class="sched-day-chip" data-day="5">Sa</button>
    <button type="button" class="sched-day-chip" data-day="6">Su</button>
    <button type="button" class="sched-day-chip" id="sched-wkday-btn" data-group="weekdays" style="border-style:dashed">M–F</button>
    <button type="button" class="sched-day-chip" id="sched-wkend-btn" data-group="weekend" style="border-style:dashed">Sa–Su</button>
  </div>

  <div class="sched-time-row">
    <input type="time" id="sched-start" class="sched-time-input" value="09:00">
    <span style="color:var(--muted);font-size:0.9rem">–</span>
    <input type="time" id="sched-end" class="sched-time-input" value="17:00">
    <select id="sched-preset" class="sched-preset-select">
      <!-- populated by JS from PRESETS -->
    </select>
    <button class="add-btn" onclick="addSchedule()">+ Add</button>
  </div>
  <div id="sched-add-err" style="font-size:0.78rem;color:var(--red);margin-top:0.2rem;display:none"></div>
</div>

<footer>cruise-control &nbsp;·&nbsp; {{ hostname }} &nbsp;·&nbsp; github.com/mcglothi/cruise-control</footer>

<script>
// ── Formatting ────────────────────────────────────────────────────────────────
function fmtBps(bps) {
  if (bps >= 1e9) return { val: (bps/1e9).toFixed(2), unit: 'Gbps' };
  if (bps >= 1e6) return { val: (bps/1e6).toFixed(1), unit: 'Mbps' };
  if (bps >= 1e3) return { val: (bps/1e3).toFixed(0), unit: 'Kbps' };
  return { val: bps, unit: 'bps' };
}

// ── Live stats ────────────────────────────────────────────────────────────────
let limitBps = 0;

async function pollStats() {
  try {
    const r = await fetch('/api/stats');
    const d = await r.json();

    const rx = fmtBps(d.rx_bps);
    document.getElementById('rx-val').childNodes[0].nodeValue = rx.val;
    document.getElementById('rx-unit').textContent = ' ' + rx.unit;

    const tx = fmtBps(d.tx_bps);
    document.getElementById('tx-val').childNodes[0].nodeValue = tx.val;
    document.getElementById('tx-unit').textContent = ' ' + tx.unit;

    // Speed bar: fill relative to active limit (or link speed if unrestricted)
    limitBps = d.limit_bps || d.link_speed_bps || 1e9;
    const pct = Math.min(100, (d.rx_bps / limitBps) * 100).toFixed(1);
    document.getElementById('speed-bar').style.width = pct + '%';

    // Limit / link speed context line
    const ctx = document.getElementById('speed-context');
    if (d.link_speed_bps > 0) {
      const link = fmtBps(d.link_speed_bps);
      if (d.limit_bps > 0) {
        const lim = fmtBps(d.limit_bps);
        ctx.textContent = lim.val + ' ' + lim.unit + ' limit / ' + link.val + ' ' + link.unit + ' link';
      } else {
        ctx.textContent = 'no limit / ' + link.val + ' ' + link.unit + ' link';
      }
    } else {
      ctx.textContent = d.limit_bps > 0 ? fmtBps(d.limit_bps).val + ' ' + fmtBps(d.limit_bps).unit + ' limit' : '';
    }

    const mv = document.getElementById('mode-live');
    mv.textContent = d.mode_label;
    mv.className = 'value ' + (d.mode === 'clear' ? 'clear' : d.mode === 'custom' ? 'custom' : 'active');
    document.getElementById('tc-raw-live').textContent = d.tc_raw;
  } catch(e) {}
}

setInterval(pollStats, 1000);
pollStats();

// ── Speed test ────────────────────────────────────────────────────────────────
let stPoll = null;

function onEndpointChange(sel) {
  const custom = document.getElementById('st-custom');
  custom.style.display = sel.value === '__custom__' ? 'block' : 'none';
}

async function startSpeedTest() {
  let url = document.getElementById('st-select').value;
  if (url === '__custom__') {
    url = document.getElementById('st-custom').value.trim();
    if (!url) { alert('Enter a custom URL first.'); return; }
  }
  document.getElementById('st-btn').disabled = true;
  document.getElementById('st-error').textContent = '';
  document.getElementById('st-result').style.display = 'block';
  document.getElementById('st-bar').style.width = '0%';
  document.getElementById('st-label').textContent = 'connecting…';

  const body = new URLSearchParams({ url });
  await fetch('/api/speedtest/start', { method: 'POST', body });
  stPoll = setInterval(pollSpeedtest, 500);
}

// ── Scheduler ─────────────────────────────────────────────────────────────────
const PRESETS = {{ presets_json | safe }};
const DAY_NAMES = ['Mo','Tu','We','Th','Fr','Sa','Su'];

(function initSchedulerForm() {
  // Populate preset select
  const sel = document.getElementById('sched-preset');
  Object.entries(PRESETS).forEach(([k, label]) => {
    const opt = document.createElement('option');
    opt.value = k; opt.textContent = label;
    sel.appendChild(opt);
  });

  // Day chip toggles
  document.querySelectorAll('#sched-day-picker .sched-day-chip[data-day]').forEach(chip => {
    chip.addEventListener('click', () => chip.classList.toggle('on'));
  });
  document.getElementById('sched-wkday-btn').addEventListener('click', () => {
    document.querySelectorAll('#sched-day-picker [data-day]').forEach(c => {
      c.classList.toggle('on', [0,1,2,3,4].includes(+c.dataset.day));
    });
  });
  document.getElementById('sched-wkend-btn').addEventListener('click', () => {
    document.querySelectorAll('#sched-day-picker [data-day]').forEach(c => {
      c.classList.toggle('on', [5,6].includes(+c.dataset.day));
    });
  });
})();

function schedErr(msg) {
  const el = document.getElementById('sched-add-err');
  el.textContent = msg;
  el.style.display = msg ? 'block' : 'none';
}

async function addSchedule() {
  const days = Array.from(document.querySelectorAll('#sched-day-picker [data-day].on'))
                    .map(c => +c.dataset.day);
  const start  = document.getElementById('sched-start').value;
  const end    = document.getElementById('sched-end').value;
  const preset = document.getElementById('sched-preset').value;

  if (!days.length)   { schedErr('Select at least one day.'); return; }
  if (!start || !end) { schedErr('Enter start and end times.'); return; }
  if (start >= end)   { schedErr('Start must be before end.'); return; }
  if (!preset)        { schedErr('Select a preset.'); return; }
  schedErr('');

  const r = await fetch('/api/schedules/add', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ days, start, end, preset }),
  });
  const d = await r.json();
  if (d.ok) {
    // Reset day chips
    document.querySelectorAll('#sched-day-picker [data-day]').forEach(c => c.classList.remove('on'));
    loadSchedules();
  } else {
    schedErr(d.msg || 'Failed to add schedule.');
  }
}

async function deleteSchedule(id) {
  await fetch('/api/schedules/' + id + '/delete', { method: 'POST' });
  loadSchedules();
}

async function toggleSchedule(id) {
  await fetch('/api/schedules/' + id + '/toggle', { method: 'POST' });
  loadSchedules();
}

function renderScheduleRow(s, activeId) {
  const isActive = s.id === activeId;
  const dayBadges = DAY_NAMES.map((n, i) =>
    `<span class="sched-day-badge${s.days.includes(i) ? ' on' : ''}">${n}</span>`
  ).join('');
  const presetLabel = PRESETS[s.preset] || s.preset;
  const activeTag = isActive ? '<span class="sched-active-dot">active now</span>' : '';
  const enabledLabel = s.enabled ? 'Pause' : 'Resume';
  return `
    <div class="sched-row${isActive ? ' active-now' : ''}${!s.enabled ? ' disabled' : ''}">
      <div class="sched-days-badges">${dayBadges}</div>
      <div class="sched-meta">
        <span class="sched-time-label">${s.start} – ${s.end}</span>
        <span class="sched-preset-label">${presetLabel}</span>
        ${activeTag}
      </div>
      <div class="sched-actions">
        <button class="icon-btn" onclick="toggleSchedule('${s.id}')">${enabledLabel}</button>
        <button class="icon-btn del" onclick="deleteSchedule('${s.id}')">&#10005;</button>
      </div>
    </div>`;
}

async function loadSchedules() {
  try {
    const r = await fetch('/api/schedules');
    const d = await r.json();
    const list = document.getElementById('sched-list');
    if (!d.schedules.length) {
      list.innerHTML = '<p class="sched-empty">No schedules configured.</p>';
    } else {
      list.innerHTML = d.schedules.map(s => renderScheduleRow(s, d.active_id)).join('');
    }
  } catch(e) {}
}

loadSchedules();
setInterval(loadSchedules, 10000);

// ── Rate chip controls ────────────────────────────────────────────────────────
function parseRate(s) {
  const m = (s || '').match(/^(\d+(?:\.\d+)?)\s*(kbit|mbit|gbit)/i);
  if (!m) return { val: 500, unit: 'mbit' };
  return { val: parseFloat(m[1]), unit: m[2].toLowerCase() };
}

function initRateControl(ctl) {
  const chips     = Array.from(ctl.querySelectorAll('.rate-chip[data-val]'));
  const hidden    = ctl.querySelector('.rate-val');
  const customRow = ctl.querySelector('.rate-custom-row');
  const num       = customRow && customRow.querySelector('.rate-num');
  const unit      = customRow && customRow.querySelector('.rate-unit');

  function getSub() { const f = ctl.closest('form'); return f && f.querySelector('.sub'); }

  function activateChip(chip) {
    chips.forEach(c => c.classList.remove('is-active'));
    if (chip) chip.classList.add('is-active');
  }

  function setRate(val) {
    hidden.value = val;
    const sub = getSub();
    if (sub) sub.textContent = val;
  }

  function openCustom(open) {
    if (!customRow) return;
    customRow.classList.toggle('open', open);
  }

  // ── Init from stored value ──
  const stored = hidden.value;
  const exactChip = chips.find(c => c.dataset.val === stored);
  if (exactChip) {
    activateChip(exactChip);
  } else {
    // Value doesn't match a chip — show custom row pre-filled
    const { val, unit: u } = parseRate(stored);
    if (num)  num.value  = val;
    if (unit) unit.value = u;
    openCustom(true);
    activateChip(chips.find(c => c.dataset.val === 'custom') || null);
  }

  // ── Chip clicks ──
  chips.forEach(chip => {
    chip.addEventListener('click', e => {
      e.preventDefault();
      activateChip(chip);
      if (chip.dataset.val === 'custom') {
        openCustom(true);
        // Pre-fill custom inputs from current stored value
        if (num && unit) {
          const { val, unit: u } = parseRate(hidden.value);
          num.value  = val;
          unit.value = u;
        }
        if (num) num.focus();
      } else {
        openCustom(false);
        setRate(chip.dataset.val);
      }
    });
  });

  // ── Custom inputs ──
  function syncCustom() {
    const v = parseFloat(num.value);
    const u = unit ? unit.value : 'mbit';
    if (v > 0) setRate(v + u);
  }
  if (num)  num.addEventListener('input',   syncCustom);
  if (unit) unit.addEventListener('change', syncCustom);
}

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.rate-ctl').forEach(initRateControl);
});

async function findNearestServers() {
  const btn    = document.getElementById('st-geo-btn');
  const status = document.getElementById('st-geo-status');
  btn.disabled = true;
  btn.textContent = '⊕ Locating…';
  status.style.display = 'none';

  try {
    const r = await fetch('/api/speedtest/servers');
    const d = await r.json();

    const sel = document.getElementById('st-select');
    sel.innerHTML = '';
    d.servers.forEach(s => {
      const opt = document.createElement('option');
      opt.value = s.url;
      opt.textContent = s.label + (s.distance_km != null ? ' — ' + s.distance_km.toLocaleString() + ' km' : '');
      sel.appendChild(opt);
    });
    const custom = document.createElement('option');
    custom.value = '__custom__';
    custom.textContent = 'Custom URL';
    sel.appendChild(custom);

    onEndpointChange(sel);

    if (d.location) {
      status.textContent = 'Sorted by distance from ' + d.location;
      status.style.display = 'block';
    }
    btn.textContent = '✓ Nearest';
  } catch(e) {
    status.textContent = 'Could not detect location — showing full list';
    status.style.display = 'block';
    btn.textContent = '⊕ Nearest';
  }
  btn.disabled = false;
}

async function pollSpeedtest() {
  try {
    const r = await fetch('/api/speedtest/status');
    const d = await r.json();

    if (d.status === 'idle') return;

    const sp = fmtBps(d.speed_bps);
    document.getElementById('st-num').textContent = sp.val;
    document.getElementById('st-unit').textContent = sp.unit;
    document.getElementById('st-bar').style.width = d.progress + '%';

    if (d.status === 'running') {
      document.getElementById('st-label').textContent =
        'downloading… ' + d.progress + '% complete';
    } else if (d.status === 'done') {
      document.getElementById('st-label').textContent =
        'complete — ' + sp.val + ' ' + sp.unit + ' average';
      clearInterval(stPoll);
      document.getElementById('st-btn').disabled = false;
    } else if (d.status === 'error') {
      document.getElementById('st-error').textContent = 'Error: ' + d.error;
      document.getElementById('st-result').style.display = 'none';
      clearInterval(stPoll);
      document.getElementById('st-btn').disabled = false;
    }
  } catch(e) {}
}
</script>
</body>
</html>"""

BUILTIN_ROW = """\
<form method="post" action="/apply" class="mode-row">
  <input type="hidden" name="mode" value="{key}">
  <button type="submit" class="apply-btn{active_cls}">
    <span class="apply-btn-text">{label}<span class="sub">{rate}</span></span>
    <span class="apply-arrow">›</span>
  </button>
  <div class="rate-row">
    <div class="rate-ctl">
      <button type="button" class="rate-chip" data-val="100mbit">100M</button>
      <button type="button" class="rate-chip" data-val="200mbit">200M</button>
      <button type="button" class="rate-chip" data-val="500mbit">500M</button>
      <button type="button" class="rate-chip" data-val="1gbit">1G</button>
      <button type="button" class="rate-chip" data-val="2gbit">2G</button>
      <button type="button" class="rate-chip" data-val="5gbit">5G</button>
      <button type="button" class="rate-chip" data-val="custom">···</button>
      <div class="rate-custom-row">
        <input type="number" class="rate-num" min="1" max="9999">
        <select class="rate-unit">
          <option value="kbit">Kbps</option>
          <option value="mbit">Mbps</option>
          <option value="gbit">Gbps</option>
        </select>
      </div>
      <input type="hidden" name="rate" class="rate-val" value="{rate}">
    </div>
    <button type="submit" formaction="/save" class="icon-btn">Save</button>
  </div>
</form>"""

CUSTOM_ROW = """\
<form method="post" action="/apply" class="mode-row">
  <input type="hidden" name="mode" value="{key}">
  <button type="submit" class="apply-btn{active_cls}">
    <span class="apply-btn-text">{label}<span class="sub">{rate}</span></span>
    <span class="apply-arrow">›</span>
  </button>
  <div class="rate-row">
    <div class="rate-ctl">
      <button type="button" class="rate-chip" data-val="100mbit">100M</button>
      <button type="button" class="rate-chip" data-val="200mbit">200M</button>
      <button type="button" class="rate-chip" data-val="500mbit">500M</button>
      <button type="button" class="rate-chip" data-val="1gbit">1G</button>
      <button type="button" class="rate-chip" data-val="2gbit">2G</button>
      <button type="button" class="rate-chip" data-val="5gbit">5G</button>
      <button type="button" class="rate-chip" data-val="custom">···</button>
      <div class="rate-custom-row">
        <input type="number" class="rate-num" min="1" max="9999">
        <select class="rate-unit">
          <option value="kbit">Kbps</option>
          <option value="mbit">Mbps</option>
          <option value="gbit">Gbps</option>
        </select>
      </div>
      <input type="hidden" name="rate" class="rate-val" value="{rate}">
    </div>
    <button type="submit" formaction="/save" class="icon-btn">Save</button>
    <button type="submit" formaction="/delete"
            onclick="return confirm('Delete &quot;{label}&quot;?')"
            class="icon-btn del">&#10005;</button>
  </div>
</form>"""


def render_page(flash="", flash_type="ok"):
    cfg = load_config()
    active, tc_raw, _ = get_status(cfg)

    flash_html = f'<div class="flash {flash_type}">{flash}</div>' if flash else ""

    builtin_rows = []
    for key in BUILTINS:
        m = cfg[key]
        builtin_rows.append(BUILTIN_ROW.format(
            key=key, label=m["label"], rate=m["rate"],
            active_cls=" is-active" if active == key else "",
        ))

    custom_rows = [
        CUSTOM_ROW.format(
            key=k, label=v["label"], rate=v["rate"],
            active_cls=" is-active" if active == k else "",
        )
        for k, v in cfg.items() if isinstance(v, dict) and not v.get("builtin") and "rate" in v
    ]

    custom_section = ""
    if custom_rows:
        custom_section = (
            '<hr class="divider" style="margin:.75rem 0">'
            '<div class="section-label">Custom Presets</div>'
            + "\n".join(custom_rows)
        )

    endpoint_opts = "".join(
        f'<option value="{s["url"]}">{s["label"]}</option>'
        for s in SPEEDTEST_SERVER_CATALOG
    ) + '<option value="__custom__">Custom URL</option>'

    presets_json = json.dumps({
        k: v["label"]
        for k, v in cfg.items()
        if isinstance(v, dict) and "rate" in v
    })

    return render_template_string(PAGE,
        iface=IFACE, hostname=HOSTNAME,
        flash_html=flash_html,
        builtin_rows="\n".join(builtin_rows),
        clear_active=" is-active" if active == "clear" else "",
        custom_section=custom_section,
        endpoint_opts=endpoint_opts,
        presets_json=presets_json,
    )


# ── Page routes ───────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def index():
    return render_page(
        flash=request.args.get("flash", ""),
        flash_type=request.args.get("flash_type", "ok"),
    )

@app.route("/apply", methods=["POST"])
def apply():
    mode = request.form.get("mode", "clear")
    rate = request.form.get("rate", "").strip()
    if mode == "clear":
        clear_all()
        return redirect(url_for("index", flash="Cleared — no bandwidth limit active.", flash_type="ok"))
    cfg = load_config()
    if mode not in cfg:
        return redirect(url_for("index", flash="Unknown preset.", flash_type="error"))
    if rate and RATE_RE.match(rate):
        cfg[mode]["rate"] = rate
        save_config(cfg)
        use_rate = rate
    elif rate:
        return redirect(url_for("index",
            flash=f"Invalid rate '{rate}'. Use e.g. 500mbit, 1gbit.", flash_type="error"))
    else:
        use_rate = cfg[mode]["rate"]
    ok, msg = apply_ingress_limit(use_rate)
    return redirect(url_for("index",
        flash=f"{cfg[mode]['label']}: {msg}" if ok else msg,
        flash_type="ok" if ok else "error"))

@app.route("/save", methods=["POST"])
def save():
    mode = request.form.get("mode", "")
    rate = request.form.get("rate", "").strip()
    cfg  = load_config()
    if mode not in cfg:
        return redirect(url_for("index", flash="Unknown preset.", flash_type="error"))
    if not rate or not RATE_RE.match(rate):
        return redirect(url_for("index",
            flash=f"Invalid rate '{rate}'. Use e.g. 500mbit, 1gbit.", flash_type="error"))
    cfg[mode]["rate"] = rate
    save_config(cfg)
    return redirect(url_for("index",
        flash=f"Saved '{cfg[mode]['label']}' → {rate}", flash_type="ok"))

@app.route("/add", methods=["POST"])
def add():
    label = request.form.get("label", "").strip()
    rate  = request.form.get("rate",  "").strip()
    if not label:
        return redirect(url_for("index", flash="Preset name is required.", flash_type="error"))
    if not rate or not RATE_RE.match(rate):
        return redirect(url_for("index",
            flash=f"Invalid rate '{rate}'. Use e.g. 500mbit, 1gbit.", flash_type="error"))
    cfg = load_config()
    key = slugify(label)
    if not key:
        return redirect(url_for("index", flash="Invalid preset name.", flash_type="error"))
    if key in cfg:
        key = key + "_2"
    cfg[key] = {"label": label, "rate": rate, "builtin": False}
    save_config(cfg)
    return redirect(url_for("index", flash=f"Added preset '{label}' at {rate}", flash_type="ok"))

@app.route("/delete", methods=["POST"])
def delete():
    mode = request.form.get("mode", "")
    cfg  = load_config()
    if mode not in cfg:
        return redirect(url_for("index", flash="Unknown preset.", flash_type="error"))
    if cfg[mode].get("builtin"):
        return redirect(url_for("index", flash="Cannot delete built-in presets.", flash_type="error"))
    label = cfg[mode]["label"]
    del cfg[mode]
    save_config(cfg)
    return redirect(url_for("index", flash=f"Deleted preset '{label}'", flash_type="ok"))


@app.route("/assets/<path:filename>")
def assets(filename):
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), filename)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8090, debug=False)
