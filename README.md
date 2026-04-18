# Cruise Control

![Cruise Control](./hero_graphic.png)

> Inbound bandwidth throttle for GB10-class machines — web GUI, live speed display, named presets, one-command install.

---

## What it does

GB10 units on a 10 Gigabit Ethernet network can saturate shared links when pulling large AI models. Cruise Control gives you a browser-based control panel to rate-limit inbound download traffic on demand — no SSH, no command line, no restart required.

You set a rate, hit a button, and the machine stays under that ceiling until you lift it. Presets are named and editable, so different teams can configure their own thresholds for their own events (business hours, all-hands meetings, training days, etc.).

**Key behaviors:**

- Limits **inbound** traffic (model downloads) using Linux `tc` + IFB, not just egress
- Changes take effect instantly — no service restart, no network interruption
- Settings persist across reboots via `config.json`
- Built-in speed test validates that a preset is actually working
- Live RX/TX display updates every second in the browser

---

## Requirements

| Requirement | Notes |
|-------------|-------|
| Linux, arm64 or x86_64 | Tested on Ubuntu 22.04 / 24.04 on NVIDIA GB10 |
| Python 3.8+ | Standard on all target systems |
| `python3-flask` | Installed automatically by the online installer |
| `iproute2` | Ships with Ubuntu — provides `tc` and `ip` |
| `kmod` | For `modprobe ifb` — ships with Ubuntu |
| Root / sudo | Required for `tc` and `ip link` commands |

---

## Install

### Option 1 — Online installer (recommended)

On any machine with internet access, run:

```bash
curl -fsSL https://raw.githubusercontent.com/mcglothi/cruise-control/main/install.sh | sudo bash
```

The installer will:
1. Detect your CPU architecture (arm64 or amd64)
2. Install dependencies via `apt` (`python3-flask`, `iproute2`, `kmod`)
3. Download the latest `.deb` from GitHub Releases
4. Auto-detect your primary network interface
5. Install and start the systemd service
6. Print the web UI address

The web UI is available at **`http://<host-ip>:8090`** immediately after install.

#### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--iface <name>` | auto-detected | Force a specific network interface |
| `--port <port>` | `8090` | Use a different port for the web UI |

```bash
# Specify the interface explicitly
curl -fsSL .../install.sh | sudo bash -s -- --iface enp3s0

# Use a custom port
curl -fsSL .../install.sh | sudo bash -s -- --port 9000
```

> **No internet?** Use the offline deb method below.

---

### Option 2 — Offline .deb package

Download the appropriate `.deb` from the [Releases](https://github.com/mcglothi/cruise-control/releases) page:

| File | Platform |
|------|----------|
| `cruise-control_<version>_arm64.deb` | NVIDIA GB10, Grace-class (arm64) |
| `cruise-control_<version>_amd64.deb` | Standard Intel/AMD servers (x86_64) |

Transfer to the target machine and install:

```bash
# From a management machine
scp cruise-control_1.1.0_arm64.deb gb10-unit-01:/tmp/

# On the target machine
sudo dpkg -i /tmp/cruise-control_1.1.0_arm64.deb
```

If `dpkg` reports missing dependencies, install them first:

```bash
sudo apt-get install -y python3-flask iproute2 kmod
sudo dpkg -i /tmp/cruise-control_1.1.0_arm64.deb
```

---

### Option 3 — Fleet deploy script

For rolling out to multiple hosts from a management machine:

```bash
git clone https://github.com/mcglothi/cruise-control.git
cd cruise-control

./deploy.sh gb10-unit-01              # auto-detects interface
./deploy.sh gb10-unit-02 enp3s0       # specify interface explicitly
```

Requires SSH key access to each target host. For a full fleet:

```bash
for host in gb10-01 gb10-02 gb10-03; do
    ./deploy.sh "$host"
done
```

Each host maintains its own `config.json` — presets are per-host and not shared.

---

### Option 4 — Manual install

```bash
sudo mkdir -p /opt/cruise-control
sudo cp app.py /opt/cruise-control/
sudo cp config.example.json /opt/cruise-control/config.json
sudo cp cruise-control.service /etc/systemd/system/
```

Edit the service file to set your interface:

```bash
sudo nano /etc/systemd/system/cruise-control.service
# → set THROTTLE_IFACE=<your-interface>
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now cruise-control
```

---

## Finding your interface name

The service needs to know which interface to shape. To find it:

```bash
ip route | grep default
# example: default via 10.0.0.1 dev enP7s7 proto dhcp
#                                    ^^^^^^
```

Or look for the interface that is `state UP` with a 10G link:

```bash
ip link show
```

The online installer and deb `postinst` script auto-detect this. To change it after install:

```bash
sudo nano /etc/systemd/system/cruise-control.service
# → edit: Environment=THROTTLE_IFACE=<your-interface>
sudo systemctl daemon-reload && sudo systemctl restart cruise-control
```

---

## Upgrading

### From the online installer

Re-run the installer — it will download and install the latest release over the existing one. Your `config.json` is preserved (it is marked as a `dpkg` conffile and will not be overwritten).

```bash
curl -fsSL https://raw.githubusercontent.com/mcglothi/cruise-control/main/install.sh | sudo bash
```

### From a .deb

Download the new `.deb` and install it with `dpkg -i`. The `--force-confold` flag keeps your existing config:

```bash
sudo dpkg -i cruise-control_<new-version>_arm64.deb
```

---

## Uninstall

```bash
# Via dpkg (removes the package, preserves config.json in /opt/cruise-control/)
sudo dpkg -r cruise-control

# Full removal including config
sudo dpkg --purge cruise-control
sudo rm -rf /opt/cruise-control

# If installed manually (not via deb)
sudo systemctl disable --now cruise-control
sudo rm -rf /opt/cruise-control /etc/systemd/system/cruise-control.service
sudo systemctl daemon-reload
```

Uninstalling stops the service and runs `tc` cleanup — no stale rate limits are left behind.

---

## Using the web UI

Open **`http://<host-ip>:8090`** in any browser on the same network.

### Live stats

The top panel shows live inbound and outbound speeds, updated every second directly from the kernel's interface counters. A context line shows the current limit and link speed (e.g., `500 Mbps limit / 10 Gbps link`). The bar below fills relative to the active limit, so at 1 Gbps with a 1 Gbps preset active, the bar is full when the link is saturated.

### Presets

Each preset row has a **slider + number + unit selector** (Kbps / Mbps / Gbps). Adjust the rate, then:

- Click the **preset name** to apply it immediately at the current slider value
- Click **Save** to update the stored default without changing what is currently active

| Built-in preset | Default rate | Intended use |
|----------------|-------------|--------------|
| Business Hours | 1 Gbps | Throttled but usable |
| Heavy Throttle | 200 Mbps | High-demand windows, all-hands |
| Unrestricted | no limit | Full 10G, removes all tc rules |

### Custom presets

Use the **Add Custom Preset** form to create named presets:

- `All Hands Meeting` → 100 Mbps
- `Training Day` → 500 Mbps
- `Overnight Batch` → 2 Gbps

Custom presets can be deleted. Built-in presets cannot.

### Speed test

The built-in speed test downloads a test file and reports the average download speed. Use it to confirm a preset is working:

1. Note the current unrestricted speed
2. Apply a preset
3. Run the speed test — the result should be at or below the preset rate

Choose from several pre-configured endpoints (Tele2, Hetzner) or supply a custom internal URL. To set a default internal test server:

```ini
# /etc/systemd/system/cruise-control.service
[Service]
Environment=SPEEDTEST_URL=http://your-fileserver.internal/testfile.bin
```

```bash
sudo systemctl daemon-reload && sudo systemctl restart cruise-control
```

---

## Configuration

Config is stored at `/opt/cruise-control/config.json`. It is written by the web UI and survives service restarts and upgrades. You can also edit it directly — changes take effect the next time a preset is applied.

```json
{
  "business": {
    "rate": "1gbit",
    "label": "Business Hours",
    "builtin": true
  },
  "heavy": {
    "rate": "200mbit",
    "label": "Heavy Throttle",
    "builtin": true
  },
  "all_hands": {
    "rate": "100mbit",
    "label": "All Hands Meeting",
    "builtin": false
  }
}
```

`builtin: true` presets cannot be deleted from the UI. The `rate` field uses standard `tc` rate syntax (`kbit`, `mbit`, `gbit`).

### Environment variables

Set these in `/etc/systemd/system/cruise-control.service` under `[Service]`:

| Variable | Default | Description |
|----------|---------|-------------|
| `THROTTLE_IFACE` | `enP7s7` | Network interface to shape |
| `SPEEDTEST_URL` | `http://speedtest.tele2.net/100MB.zip` | Default speed test file |

---

## Service management

```bash
# Status
sudo systemctl status cruise-control

# Restart (e.g. after editing the service file)
sudo systemctl restart cruise-control

# View live logs
sudo journalctl -u cruise-control -f

# Stop (also clears any active tc rules)
sudo systemctl stop cruise-control
```

---

## How it works

Linux `tc` (traffic control) can rate-limit a network interface's **egress** (outbound) queue natively using HTB (Hierarchical Token Bucket). However, model downloads are **ingress** (inbound) traffic — `tc` cannot directly queue that.

Cruise Control solves this with **IFB (Intermediate Functional Block)**, a kernel module that creates a virtual interface:

```
incoming traffic
       ↓
  enP7s7 ingress  →  tc mirred redirect  →  ifb0 egress  →  HTB rate limit
                                                                    ↓
                                                           packet delivered
```

When you apply a preset:
1. `modprobe ifb` loads the IFB kernel module
2. A virtual `ifb0` interface is created and brought up
3. A `tc ingress` qdisc is added to your real interface
4. A `tc filter` redirects all inbound packets to `ifb0`'s egress queue
5. An HTB qdisc on `ifb0` enforces the rate limit

When you clear:
1. The `ingress` qdisc and filter are removed from the real interface
2. The HTB qdisc is removed from `ifb0`
3. `ifb0` is taken down and deleted
4. Traffic flows at full line rate immediately

The service's `ExecStop` clause ensures tc rules are always cleaned up when the service stops, so a reboot or crash never leaves a stale rate limit in place.

---

## Building the .deb

Build on an arm64 machine (any GB10 works):

```bash
git clone https://github.com/mcglothi/cruise-control.git
cd cruise-control
./build-deb.sh
# → cruise-control_1.1.0_arm64.deb
```

To bump the version, edit `packaging/DEBIAN/control` before building. To build an amd64 package, change `Architecture: amd64` in that same file.

---

## Tested on

- NVIDIA GB10 (Grace Blackwell Superchip), Ubuntu 24.04, arm64
- Kernel 6.17 with `ifb` module available

Should work on any arm64 or x86\_64 Debian/Ubuntu system with a 5.x+ kernel.

---

## License

MIT
