# airprinthq

Licensed under the Apache License 2.0 — see [LICENSE](LICENSE).

**This project is specifically created and tested for a Canon LBP673Cdw II
printer behind a host running Ubuntu 26.04.** It will probably work for
other printers whose port-9100 dispatcher auto-detects PDF/JPEG/TIFF, but
the hardcoded capabilities in `airprinthq/caps.py` (URF string, media
list, color/duplex defaults) are tailored to that Canon model and have
not been validated against anything else.

Standalone IPP/AirPrint proxy. Announces itself on Bonjour as an AirPrint
printer with hardcoded capabilities and forwards every print job as raw
bytes to a real printer's port 9100. Useful when:

- You want iOS/macOS AirPrint to print PDFs to a printer whose own IPP
  capabilities don't advertise `application/pdf` (but whose port 9100
  auto-detects PDFs by magic bytes).
- You want to print photo-sized jobs (3.5×5, 4×6, 5×7) from iOS centered
  on A4 paper at their original size — not scaled up to fill the sheet.
  The printer only has A4 in the tray; the transcoder rewrites every
  page to A4 with smaller pages centered (and any larger pages scaled
  down to fit).
- You want full control over what capabilities get announced.

## Requirements

- Linux host (Ubuntu 26.04 is the tested target) with mDNS reachable to
  AirPrint clients (same L2 segment, or via a Bonjour reflector across
  VLANs).
- Python 3.12+.
- System packages: `python3-aiohttp`, `avahi-daemon`, `avahi-utils`,
  `openssl` (usually preinstalled). Also `cups-ipp-utils` if you want
  `ipptool` for debugging.

## Install (bare-metal, systemd)

### 1. Install dependencies

```bash
sudo apt-get update
sudo apt-get install -y \
    python3-aiohttp python3-pypdf python3-pil \
    avahi-daemon avahi-utils
sudo systemctl enable --now avahi-daemon
# optional: ipptool for IPP debugging
sudo apt-get install -y cups-ipp-utils
```

`python3-aiohttp` pulls in its transitive deps. `python3-pypdf` and
`python3-pil` are used by the A4-normalization transcoder.
`avahi-daemon` pulls in `libavahi-*` and is required because
python-zeroconf doesn't emit RFC 6763 §7.1-correct subtype PTRs (iOS
rejects services without the canonical form).

### 2. Drop the code in place

```bash
INSTALL_DIR=/opt/airprinthq                            # adjust to taste
SERVICE_USER=printproxy                                # adjust to taste
sudo useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER" 2>/dev/null || true
sudo install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$INSTALL_DIR"
# rsync or git the repo into $INSTALL_DIR
```

### 3. Install the systemd unit

Write `/etc/systemd/system/airprinthq.service`:

```ini
[Unit]
Description=airprinthq — standalone IPP/AirPrint proxy
After=network-online.target avahi-daemon.service
Wants=network-online.target
Requires=avahi-daemon.service

[Service]
Type=exec
User=printproxy
Group=printproxy
WorkingDirectory=/opt/airprinthq
ExecStart=/usr/bin/python3 -m airprinthq
Restart=on-failure
RestartSec=5

# Bind 631 (IPP) and 443 (IPPS) without running as root.
# mDNS (5353) is handled by avahi-daemon.
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE

# Persistent cert dir used only if IPPS is enabled below.
StateDirectory=airprinthq
StateDirectoryMode=0700

Environment=PRINTER_HOST=192.0.2.10               # IP or hostname of your real printer
Environment=PRINTER_PORT=9100
Environment=BONJOUR_NAME=AirprintHQ
Environment=IPP_DOCUMENT_FORMAT_SUPPORTED=application/pdf,image/jpeg,image/tiff,image/urf
Environment=IPP_DOCUMENT_FORMAT_DEFAULT=application/pdf
Environment=IPP_MEDIA_SUPPORTED=iso_a4_210x297mm,na_index-3x5_3x5in,na_index-4x6_4x6in,na_5x7_5x7in
Environment=IPP_MEDIA_DEFAULT=iso_a4_210x297mm
Environment=IPP_PORT=631
# IPPS is optional. iOS does NOT require it for AirPrint visibility or
# printing (the actual visibility gate is the RFC-correct subtype PTR,
# which avahi handles). Leave empty unless a future iOS makes it
# necessary. With IPPS enabled, set CERT_DIR=/var/lib/airprinthq.
Environment=IPPS_PORT=

NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

### 4. Enable and start

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now airprinthq.service
sudo systemctl status airprinthq.service
journalctl -u airprinthq.service -f
```

## Configuration

All knobs are env vars. Capabilities not in the table are hardcoded in
`airprinthq/caps.py::build_standalone()` — edit there to change color, duplex,
DPI, URF, sides, copies, etc.

| Env var | Required | Default | Purpose |
|---|---|---|---|
| `PRINTER_HOST` | yes | — | IP or hostname of the real printer (raw 9100 target). |
| `PRINTER_PORT` | no | `9100` | Port for raw forwarding. |
| `BONJOUR_NAME` | yes | — | DNS-SD service instance name. Must differ from any real printer's Bonjour name on the LAN. |
| `IPP_DOCUMENT_FORMAT_SUPPORTED` | no | `application/pdf,image/jpeg,image/tiff,image/urf` | Comma-separated MIME types announced. Each must work via raw 9100 magic-byte auto-detection on your printer. |
| `IPP_DOCUMENT_FORMAT_DEFAULT` | no | `application/pdf` | Default format if the client doesn't pick one. |
| `IPP_MEDIA_SUPPORTED` | no | `iso_a4_210x297mm,na_index-3x5_3x5in,na_index-4x6_4x6in,na_5x7_5x7in` | Comma-separated PWG media keywords. iOS does NOT strictly obey this list — its print dialog mixes our declared sizes with hardcoded per-locale defaults (it may show A6, Letter etc. we never declared, and may hide ones we do). Functionally moot since the transcoder normalizes every page to A4 regardless. |
| `IPP_MEDIA_DEFAULT` | no | `iso_a4_210x297mm` | Default media. |
| `IPP_PORT` | no | `631` | Plain IPP port. Unset/empty disables plain IPP. |
| `IPPS_PORT` | no | (empty) | IPPS (TLS) port. Unset/empty disables IPPS — that's the default; not needed for AirPrint. |
| `CERT_DIR` | no | `/var/lib/airprinthq` | Persistent dir for the self-signed cert when `IPPS_PORT` is set. systemd's `StateDirectory=` makes one for us. |

At least one of `IPP_PORT` / `IPPS_PORT` must be set.

## Debug

- `avahi-browse -rt _ipp._tcp` — see what mDNS advertises locally
  (full TXT, port, target hostname).
- `mdns-query.py` — a stdlib-only mDNS browser shipped with the repo,
  useful when debugging discovery from a host that isn't running the
  proxy (and so doesn't have `avahi-browse` looking at its own services).
  Examples:
  ```bash
  python3 mdns-query.py -m _ipp._tcp.local PTR                 # browse IPP printers
  python3 mdns-query.py -m _universal._sub._ipp._tcp.local PTR # AirPrint subtype
  python3 mdns-query.py airprinthq.local A                     # resolve our hostname
  ```
  The `-m` flag (bind UDP/5353 + join the multicast group) is required to
  see PTR responses from devices other than your own host; without it,
  only legacy-unicast responders answer, which excludes most printers and
  AppleTVs.
- `journalctl -u airprinthq.service -f` — live logs.
- `ipptool -tv ipp://airprinthq.local/ipp/print` — IPP probe (from
  `cups-ipp-utils`).
- `cd /opt/airprinthq && python3 -m airprinthq` — run in foreground
  for interactive debugging.

## How it works (one paragraph)

We don't query the real printer. Capabilities are hardcoded in
`build_standalone()` and announced verbatim. mDNS publication is
delegated to `avahi-daemon` via `avahi-publish-service` subprocesses
(this is required because iOS's AirPrint validator needs RFC 6763
§7.1-compliant subtype PTRs, which python-zeroconf can't emit). We
listen on plain IPP port 631 by default (IPPS is optional). When a
client POSTs `/ipp/print`, `ipp_server` parses the IPP message (RFC
8010 codec in `ipp_codec`), accepts the job and returns immediately
with `job-state=pending`. A background `forwarder` worker pulls the
job, runs `transcode_to_a4` to normalize every page to A4 (smaller
pages get centered with white margins; larger pages get scaled-to-fit),
then streams the resulting PDF to `PRINTER_HOST:PRINTER_PORT`. The
printer's port-9100 dispatcher detects the format from magic bytes
and prints on whatever paper is loaded (always A4 in our setup).
