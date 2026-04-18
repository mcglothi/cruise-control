#!/usr/bin/env python3
"""
Cruise Control — GB10 Bandwidth Throttle
Web GUI to rate-limit inbound traffic (model downloads) on GB10-class machines.

Uses Linux tc + IFB to limit ingress (inbound) throughput.
Runs as root (needed for tc/ip). Listens on 0.0.0.0:8090.
Config persisted to config.json alongside this file.
"""

import subprocess
import os
import json
import re
import socket
from flask import Flask, request, redirect, url_for

IFACE      = os.environ.get("THROTTLE_IFACE", "enP7s7")
IFB_DEV    = "ifb0"
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
HOSTNAME   = socket.gethostname()

BUILTINS = ["business", "heavy"]

DEFAULTS = {
    "business": {"rate": "1gbit",   "label": "Business Hours",  "builtin": True},
    "heavy":    {"rate": "200mbit", "label": "Heavy Throttle",  "builtin": True},
}

RATE_RE = re.compile(r"^\d+(\.\d+)?\s*(kbit|mbit|gbit|kbps|mbps|gbps)$", re.IGNORECASE)
SLUG_RE = re.compile(r"[^a-z0-9]+")

app = Flask(__name__)


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


# ── tc / IFB helpers ──────────────────────────────────────────────────────────

def run(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)


def clear_all():
    """Remove all cruise-control tc rules and tear down IFB."""
    run(f"tc qdisc del dev {IFACE} ingress 2>/dev/null || true")
    run(f"tc qdisc del dev {IFACE} root    2>/dev/null || true")
    run(f"tc qdisc del dev {IFB_DEV} root  2>/dev/null || true")
    run(f"ip link set {IFB_DEV} down       2>/dev/null || true")
    run(f"ip link del {IFB_DEV}            2>/dev/null || true")


def apply_ingress_limit(rate):
    """
    Rate-limit inbound traffic (model downloads) using IFB redirect.
    IFB mirrors ingress to a virtual egress queue where HTB can shape it.
    Returns (ok, message).
    """
    clear_all()

    # Load IFB module and bring up virtual interface
    run("modprobe ifb numifbs=1")
    run(f"ip link add {IFB_DEV} type ifb 2>/dev/null || true")
    r_up = run(f"ip link set {IFB_DEV} up")
    if r_up.returncode != 0:
        return False, f"Could not bring up {IFB_DEV}: {r_up.stderr.strip()}"

    # Redirect ingress of IFACE into IFB egress
    run(f"tc qdisc add dev {IFACE} handle ffff: ingress")
    r_filt = run(
        f"tc filter add dev {IFACE} parent ffff: protocol ip "
        f"u32 match u32 0 0 action mirred egress redirect dev {IFB_DEV}"
    )
    if r_filt.returncode != 0:
        clear_all()
        return False, f"tc filter error: {r_filt.stderr.strip()}"

    # HTB on IFB device
    r1 = run(f"tc qdisc add dev {IFB_DEV} root handle 1: htb default 10")
    r2 = run(
        f"tc class add dev {IFB_DEV} parent 1: classid 1:10 "
        f"htb rate {rate} ceil {rate} burst 256k"
    )
    if r1.returncode != 0 or r2.returncode != 0:
        clear_all()
        err = (r1.stderr or r2.stderr).strip()
        return False, f"tc HTB error: {err}"

    return True, f"Limiting inbound to {rate}"


def get_status(cfg):
    """
    Returns (active_mode_key, human_summary).
    Checks IFB device for active HTB class.
    """
    result = run(f"tc class show dev {IFB_DEV}")
    tc_summary = "no limit — full line rate"
    active = "clear"

    for line in result.stdout.splitlines():
        if "rate" not in line:
            continue
        parts = line.split()
        if "rate" not in parts:
            continue
        idx = parts.index("rate")
        live_rate = (parts[idx + 1] if idx + 1 < len(parts) else "").lower()
        tc_summary = f"inbound capped at {live_rate} via IFB"

        for key, mcfg in cfg.items():
            if mcfg.get("rate", "").lower() == live_rate:
                active = key
                break
        else:
            active = "custom"
        break

    return active, tc_summary


# ── HTML ──────────────────────────────────────────────────────────────────────

PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cruise Control — {hostname}</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  background: #0d1117; color: #e6edf3;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  min-height: 100vh; display: flex; align-items: center;
  justify-content: center; padding: 2rem;
}}
.card {{
  background: #161b22; border: 1px solid #30363d;
  border-radius: 12px; padding: 2rem; width: 100%; max-width: 600px;
}}
h1 {{ font-size: 1.4rem; color: #58a6ff; margin-bottom: 0.2rem; }}
.subtitle {{ color: #8b949e; font-size: 0.85rem; margin-bottom: 1.75rem; }}

.status-box {{
  background: #0d1117; border: 1px solid #30363d;
  border-radius: 8px; padding: 0.9rem 1.2rem; margin-bottom: 1.75rem;
}}
.status-label {{
  font-size: 0.72rem; text-transform: uppercase;
  letter-spacing: 0.08em; color: #8b949e;
}}
.status-value {{ font-size: 1.05rem; font-weight: 600; margin: 0.2rem 0 0.35rem; }}
.status-value.clear    {{ color: #3fb950; }}
.status-value.active   {{ color: #58a6ff; }}
.status-value.custom   {{ color: #a371f7; }}
.tc-raw {{ font-family: monospace; font-size: 0.76rem; color: #6e7681; }}

.flash {{ border-radius: 6px; padding: 0.55rem 1rem; margin-bottom: 1.1rem; font-size: 0.875rem; }}
.flash.ok    {{ background: #1c2a1c; border: 1px solid #3fb950; color: #3fb950; }}
.flash.error {{ background: #2d1a1a; border: 1px solid #f85149; color: #f85149; }}

.section-label {{
  font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.08em;
  color: #484f58; margin-bottom: 0.5rem; margin-top: 1.25rem;
}}
.section-label:first-of-type {{ margin-top: 0; }}

.mode-row {{ display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.6rem; }}
.apply-btn {{
  flex: 1; padding: 0.75rem 1rem; border: 1px solid #30363d;
  border-radius: 8px; cursor: pointer; text-align: left;
  font-size: 0.9rem; font-weight: 600; background: #1c2128;
  color: #e6edf3; transition: border-color 0.15s, opacity 0.15s;
}}
.apply-btn:hover {{ border-color: #58a6ff; opacity: 0.9; }}
.apply-btn.is-active {{ border-color: #58a6ff; background: #1c2a3a; color: #58a6ff; }}
.apply-btn .sub {{
  display: block; font-size: 0.75rem; font-weight: 400;
  color: #8b949e; margin-top: 0.15rem;
}}
.apply-btn.is-active .sub {{ color: #58a6ff; opacity: 0.75; }}

.rate-input {{
  background: #0d1117; border: 1px solid #30363d; border-radius: 6px;
  color: #e6edf3; font-size: 0.85rem; font-family: monospace;
  padding: 0.45rem 0.6rem; width: 105px; text-align: right;
}}
.rate-input:focus {{ outline: none; border-color: #58a6ff; }}

.icon-btn {{
  background: none; border: 1px solid #30363d; border-radius: 6px;
  color: #8b949e; font-size: 0.8rem; padding: 0.45rem 0.6rem;
  cursor: pointer; white-space: nowrap;
}}
.icon-btn:hover {{ border-color: #58a6ff; color: #58a6ff; }}
.icon-btn.del:hover {{ border-color: #f85149; color: #f85149; }}

.clear-btn {{
  width: 100%; padding: 0.7rem 1rem; margin-bottom: 0.6rem;
  background: #1a2d1e; border: 1px solid #3fb950; border-radius: 8px;
  color: #3fb950; font-size: 0.9rem; font-weight: 600; cursor: pointer;
  text-align: center; transition: opacity 0.15s;
}}
.clear-btn:hover {{ opacity: 0.82; }}
.clear-btn.is-active {{ filter: brightness(1.2); }}

.add-section {{
  margin-top: 1.5rem; padding-top: 1.25rem; border-top: 1px solid #21262d;
}}
.add-row {{ display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap; }}
.add-row input[type=text] {{
  background: #0d1117; border: 1px solid #30363d; border-radius: 6px;
  color: #e6edf3; font-size: 0.88rem; padding: 0.5rem 0.75rem;
}}
.add-row input[type=text]:focus {{ outline: none; border-color: #58a6ff; }}
.name-input {{ flex: 1; min-width: 130px; }}
.add-btn {{
  background: #1c2a3a; border: 1px solid #58a6ff; border-radius: 6px;
  color: #58a6ff; font-size: 0.88rem; font-weight: 600;
  padding: 0.5rem 1rem; cursor: pointer; white-space: nowrap;
}}
.add-btn:hover {{ opacity: 0.85; }}
.hint {{ font-size: 0.72rem; color: #484f58; margin-top: 0.4rem; }}

footer {{ margin-top: 1.5rem; font-size: 0.72rem; color: #484f58; text-align: center; }}
</style>
</head>
<body>
<div class="card">
  <h1>Cruise Control</h1>
  <p class="subtitle">
    Inbound throttle &nbsp;&middot;&nbsp;
    Interface: <code>{iface}</code> &nbsp;&middot;&nbsp;
    Host: <code>{hostname}</code>
  </p>

  {flash_html}

  <div class="status-box">
    <div class="status-label">Current Mode</div>
    <div class="status-value {status_cls}">{active_label}</div>
    <div class="tc-raw">{tc_raw}</div>
  </div>

  <div class="section-label">Built-in Presets</div>
  {builtin_rows}

  <form method="post" action="/apply">
    <input type="hidden" name="mode" value="clear">
    <button type="submit" class="clear-btn{clear_active_cls}">
      Unrestricted — remove all limits
    </button>
  </form>

  {custom_section}

  <div class="add-section">
    <div class="section-label">Add Custom Preset</div>
    <form method="post" action="/add" class="add-row">
      <input class="name-input" type="text" name="label"
             placeholder="Preset name (e.g. All Hands)" required maxlength="40">
      <input class="rate-input" type="text" name="rate"
             placeholder="e.g. 500mbit" required>
      <button type="submit" class="add-btn">+ Add</button>
    </form>
    <p class="hint">Valid units: kbit &nbsp;mbit &nbsp;gbit &nbsp;&middot;&nbsp; e.g. 100mbit &nbsp;500mbit &nbsp;1gbit &nbsp;2.5gbit</p>
  </div>

  <footer>cruise-control &nbsp;&middot;&nbsp; {hostname}</footer>
</div>
</body>
</html>"""

BUILTIN_ROW = """\
<form method="post" action="/apply" class="mode-row">
  <input type="hidden" name="mode" value="{key}">
  <button type="submit" class="apply-btn{active_cls}">
    {label}<span class="sub">{rate}</span>
  </button>
  <input class="rate-input" type="text" name="rate" value="{rate}" title="tc rate">
  <button type="submit" formaction="/save" class="icon-btn">Save</button>
</form>"""

CUSTOM_ROW = """\
<form method="post" action="/apply" class="mode-row">
  <input type="hidden" name="mode" value="{key}">
  <button type="submit" class="apply-btn{active_cls}">
    {label}<span class="sub">{rate}</span>
  </button>
  <input class="rate-input" type="text" name="rate" value="{rate}" title="tc rate">
  <button type="submit" formaction="/save" class="icon-btn">Save</button>
  <button type="submit" formaction="/delete"
          onclick="return confirm('Delete preset &quot;{label}&quot;?')"
          class="icon-btn del">&#10005;</button>
</form>"""

CUSTOM_SECTION = """\
<div class="section-label">Custom Presets</div>
{rows}"""


def render_page(flash="", flash_type="ok"):
    cfg = load_config()
    active_mode, tc_raw = get_status(cfg)

    flash_html = ""
    if flash:
        flash_html = f'<div class="flash {flash_type}">{flash}</div>'

    builtin_rows = []
    for key in BUILTINS:
        mcfg = cfg[key]
        active_cls = " is-active" if active_mode == key else ""
        builtin_rows.append(BUILTIN_ROW.format(
            key=key, label=mcfg["label"], rate=mcfg["rate"], active_cls=active_cls,
        ))

    custom_rows = []
    for key, mcfg in cfg.items():
        if mcfg.get("builtin"):
            continue
        active_cls = " is-active" if active_mode == key else ""
        custom_rows.append(CUSTOM_ROW.format(
            key=key, label=mcfg["label"], rate=mcfg["rate"], active_cls=active_cls,
        ))

    custom_section = ""
    if custom_rows:
        custom_section = CUSTOM_SECTION.format(rows="\n".join(custom_rows))

    if active_mode == "clear":
        status_cls, active_label = "clear", "Unrestricted"
    elif active_mode == "custom":
        status_cls, active_label = "custom", "Custom"
    else:
        status_cls = "active"
        active_label = cfg[active_mode]["label"] + " (" + cfg[active_mode]["rate"] + ")"

    clear_active_cls = " is-active" if active_mode == "clear" else ""

    return PAGE.format(
        hostname=HOSTNAME, iface=IFACE,
        flash_html=flash_html,
        status_cls=status_cls, active_label=active_label, tc_raw=tc_raw,
        builtin_rows="\n".join(builtin_rows),
        clear_active_cls=clear_active_cls,
        custom_section=custom_section,
    )


# ── Routes ────────────────────────────────────────────────────────────────────

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
        return redirect(url_for("index",
            flash="Cleared — no bandwidth limit active.", flash_type="ok"))

    cfg = load_config()
    if mode not in cfg:
        return redirect(url_for("index", flash="Unknown preset.", flash_type="error"))

    if rate and RATE_RE.match(rate):
        cfg[mode]["rate"] = rate
        save_config(cfg)
        use_rate = rate
    elif rate:
        return redirect(url_for("index",
            flash=f"Invalid rate '{rate}'. Use e.g. 500mbit, 1gbit.",
            flash_type="error"))
    else:
        use_rate = cfg[mode]["rate"]

    ok, msg = apply_ingress_limit(use_rate)
    label = cfg[mode]["label"]
    return redirect(url_for("index",
        flash=f"{label}: {msg}" if ok else msg,
        flash_type="ok" if ok else "error"))


@app.route("/save", methods=["POST"])
def save():
    mode = request.form.get("mode", "")
    rate = request.form.get("rate", "").strip()

    cfg = load_config()
    if mode not in cfg:
        return redirect(url_for("index", flash="Unknown preset.", flash_type="error"))
    if not rate or not RATE_RE.match(rate):
        return redirect(url_for("index",
            flash=f"Invalid rate '{rate}'. Use e.g. 500mbit, 1gbit.",
            flash_type="error"))

    cfg[mode]["rate"] = rate
    save_config(cfg)
    return redirect(url_for("index",
        flash=f"Saved '{cfg[mode]['label']}' default \u2192 {rate}",
        flash_type="ok"))


@app.route("/add", methods=["POST"])
def add():
    label = request.form.get("label", "").strip()
    rate  = request.form.get("rate",  "").strip()

    if not label:
        return redirect(url_for("index", flash="Preset name is required.", flash_type="error"))
    if not rate or not RATE_RE.match(rate):
        return redirect(url_for("index",
            flash=f"Invalid rate '{rate}'. Use e.g. 500mbit, 1gbit.",
            flash_type="error"))

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
    cfg = load_config()

    if mode not in cfg:
        return redirect(url_for("index", flash="Unknown preset.", flash_type="error"))
    if cfg[mode].get("builtin"):
        return redirect(url_for("index",
            flash="Cannot delete built-in presets.", flash_type="error"))

    label = cfg[mode]["label"]
    del cfg[mode]
    save_config(cfg)
    return redirect(url_for("index", flash=f"Deleted preset '{label}'", flash_type="ok"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8090, debug=False)
