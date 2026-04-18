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

---

## Requirements

| Requirement | Notes |
|-------------|-------|
| Linux, arm64 | Tested on Ubuntu 22.04 / 24.04 on NVIDIA GB10 |
| Python 3.8+ | Standard on all target systems |
| `python3-flask` | `apt install python3-flask` |
| `iproute2` | Ships with Ubuntu — provides `tc` and `ip` |
| `kmod` | For `modprobe ifb` — ships with Ubuntu |
| Root access | Required for `tc` and `ip link` commands |

---

## Install

### Option 1 — Debian package (recommended)

Download the latest `.deb` from the [Releases](https://github.com/mcglothi/cruise-control/releases) page and install:

```bash
sudo dpkg -i cruise-control_1.1.0_arm64.deb
```

The installer will:
1. Copy the app to `/opt/cruise-control/`
2. Install the systemd service
3. Auto-detect your primary network interface
4. Enable and start the service

The web UI is then available at **`http://<host-ip>:8090`**.

---

### Option 2 — deploy script

For deploying to multiple hosts from a management machine:

```bash
git clone https://github.com/mcglothi/cruise-control.git
cd cruise-control
./deploy.sh <hostname>              # auto-detects interface
./deploy.sh gb10-unit-02 enp3s0    # specify interface explicitly
```

The script uses SSH, so your key must be in the target host's `authorized_keys`.

---

### Option 3 — manual

```bash
sudo mkdir -p /opt/cruise-control
sudo cp app.py /opt/cruise-control/
sudo cp config.example.json /opt/cruise-control/config.json
sudo cp cruise-control.service /etc/systemd/system/

# Set the correct interface name
sudo nano /etc/systemd/system/cruise-control.service
# → edit THROTTLE_IFACE=<your-interface>

sudo systemctl daemon-reload
sudo systemctl enable --now cruise-control
```

---

## Finding the right interface name

The service needs to know which interface to shape. To find it:

```bash
ip route | grep default
# example: default via 10.0.0.1 dev enp3s0 proto dhcp
#                                    ^^^^^^
```

Or look for the interface that is `state UP` and has a 10G link:

```bash
ip link show
```

Update `THROTTLE_IFACE` in `/etc/systemd/system/cruise-control.service`, then:

```bash
sudo systemctl daemon-reload && sudo systemctl restart cruise-control
```

---

## Using the web UI

Open **`http://<host-ip>:8090`** in any browser on the same network.

### Live stats

The top panel shows live inbound and outbound speeds, updated every second directly from the kernel's interface counters. The bar below the numbers fills relative to the active limit — so at 1 Gbps with a 1 Gbps preset active, the bar is full when the link is saturated.

### Built-in presets

| Preset | Default rate | Intended use |
|--------|-------------|--------------|
| Business Hours | 1 Gbps | Throttled but usable — model pulls don't saturate shared links |
| Heavy Throttle | 200 Mbps | High-demand windows, all-hands meetings |
| Unrestricted | no limit | Full 10G, removes all tc rules |

Click the preset name to apply it immediately. Edit the rate field and click **Apply** to change the rate and apply in one step. Click **Save** to update the stored default without changing what's currently active.

### Custom presets

Use the **Add Custom Preset** form to create named presets for specific events:

- `All Hands Meeting` → `100mbit`
- `Training Day` → `500mbit`
- `Overnight Batch` → `2gbit`

Custom presets can be deleted; built-in presets cannot.

Valid rate formats: `100mbit`, `500mbit`, `1gbit`, `2.5gbit`, `500kbit`, etc.

### Speed test

The built-in speed test downloads a 100 MB file and reports the average download speed. Use it to confirm a preset is working:

1. Note the current unrestricted speed
2. Apply a preset
3. Run the speed test — the result should be at or below the preset rate

The default test server is `speedtest.tele2.net`. To use an internal file server instead (useful when GB10 units don't have internet access), set `SPEEDTEST_URL` in the service unit:

```ini
[Service]
Environment=SPEEDTEST_URL=http://your-fileserver.internal/testfile.bin
```

Then `sudo systemctl daemon-reload && sudo systemctl restart cruise-control`.

---

## Configuration

Config is stored at `/opt/cruise-control/config.json`. It is written by the web UI and survives service restarts. You can also edit it directly — changes take effect the next time a preset is applied.

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

`builtin: true` presets cannot be deleted from the UI. The `rate` field uses standard `tc` rate syntax.

### Environment variables

Set these in `/etc/systemd/system/cruise-control.service` under `[Service]`:

| Variable | Default | Description |
|----------|---------|-------------|
| `THROTTLE_IFACE` | `enP7s7` | Network interface to shape |
| `SPEEDTEST_URL` | `http://speedtest.tele2.net/100MB.zip` | File to download for speed test |

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

## Building the deb

Build on an arm64 machine (any GB10 works):

```bash
git clone https://github.com/mcglothi/cruise-control.git
cd cruise-control
./build-deb.sh
# → cruise-control_1.1.0_arm64.deb
```

To bump the version, edit `packaging/DEBIAN/control` before building.

---

## Deploying to multiple hosts

The `deploy.sh` script handles the full install on any reachable host:

```bash
./deploy.sh gb10-unit-01            # auto-detects interface
./deploy.sh gb10-unit-02 enp3s0     # explicit interface
./deploy.sh gb10-unit-03 enp1s0f0   # 10G NIC name on that host
```

For a fleet rollout, loop over your hosts:

```bash
for host in gb10-01 gb10-02 gb10-03; do
    ./deploy.sh $host
done
```

Each host gets its own `config.json` — presets are per-host and not shared.

---

## Service management

```bash
# Status
sudo systemctl status cruise-control

# Restart (e.g. after editing the service file)
sudo systemctl restart cruise-control

# View logs
sudo journalctl -u cruise-control -f

# Stop (also clears any active tc rules)
sudo systemctl stop cruise-control

# Uninstall
sudo dpkg -r cruise-control         # if installed via deb
# or
sudo systemctl disable --now cruise-control
sudo rm -rf /opt/cruise-control /etc/systemd/system/cruise-control.service
```

---

## Tested on

- NVIDIA GB10 (Grace Blackwell Superchip), Ubuntu 24.04, arm64
- Kernel 6.17 with `ifb` module available

Should work on any arm64 or x86\_64 Debian/Ubuntu system with a 5.x+ kernel. For x86\_64, rebuild the deb with `Architecture: amd64` in `packaging/DEBIAN/control`.

---

## License

MIT
