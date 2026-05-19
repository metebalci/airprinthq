# AirPrintHQ

> Not affiliated with Apple Inc. AirPrint is a trademark of Apple Inc.

I started this project because I wanted to print photos from my iPhone
to my A4-only printer without iOS scaling them up to fill the sheet —
a 4×6 photo should print at 4×6 size, centered on the A4 paper, with
white margins around it. And while I was at it, US Letter documents
should land on A4 without the default ~3% downscale every print pipeline
applies. That was the original itch; everything else here (URF decoding,
multi-copy, observe-only mode, the AirPrint protocol detective work)
accumulated as the project grew to handle whatever iOS sent.

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
- You want to print US Letter documents on A4 paper **without** the
  ~3% downscale every print pipeline applies by default. Letter is
  17pt wider than A4 (no height issue — Letter is shorter), so we just
  center the Letter content on A4 at 100% size and accept ~8.5pt clip
  per horizontal side. That always lands inside the document's own
  margin whitespace for typical Letter docs, so no real content is lost
  and the pixels you'd see at native size on Letter paper appear at the
  exact same size on A4.
- You want B&W and multi-copy jobs from iOS to work on a printer whose
  port-9100 dispatcher can't decode Apple URF. iOS rasterizes B&W and
  multi-copy content into URF; we include a from-scratch Python URF
  decoder (`airprinthq/urf.py`) that converts incoming URF into a
  multi-page PDF before forwarding. The PDF then flows through the
  same A4 normalizer used for native PDF jobs.
- You want full control over what capabilities get announced.

**Scope:** specifically created and tested for a Canon LBP673Cdw II
printer behind a host running Ubuntu 26.04. It will probably work for
other printers whose port-9100 dispatcher auto-detects PDF, but
the hardcoded capabilities in `airprinthq/caps.py` (URF string, media
list, color/duplex defaults) are tailored to that Canon model and have
not been validated against anything else.

**URF & B&W handling.** Two facts about iOS + the Canon LBP673C II are
relevant:

1. **iOS requires `image/urf` in `pdl=` to submit any AirPrint job** at
   all — color, B&W, single, multi-copy. Without URF in our advertised
   pdl, iOS shows the printer but silently refuses to print. So we
   keep `image/urf` in the default `IPP_DOCUMENT_FORMAT_SUPPORTED`.
2. **The Canon's port-9100 dispatcher cannot decode URF.** Sending
   raw URF to the printer produces megabytes of garbage characters as
   the dispatcher falls back to text mode. So we never forward URF
   directly.

To bridge these, `airprinthq/urf.py` contains a from-scratch Python
decoder for Apple's URF format (no public spec; built by reverse-
engineering the captured byte layout and the standard PackBits-style
RLE the format uses). When iOS sends a job as URF (typically for
explicit B&W content or multi-copy jobs — iOS expands copies as extra
URF pages), the transcoder pipes the bytes through `urf.to_pdf()` first,
yielding a multi-page PDF at the URF's native DPI, which then flows
through the same A4 normalizer used for native PDF input. The Canon
only ever sees PDF. URF decoding failures abort the job rather than
forward potentially-garbage bytes.

**Copies semantics.** We honor the IPP `copies` operation attribute via
a TCP loop in the forwarder: send the (transcoded) PDF to the printer's
port 9100 `N` times for `copies=N`. For URF jobs, iOS typically expands
copies into multi-page URF documents itself, so each page already
represents one copy and the forwarder just sends the multi-page PDF
once — same end result. Cancellation is honored both between copies
and mid-stream within a copy.

## What works, what doesn't

### Works end-to-end

- iPhone discovers `AirPrintHQ` in the AirPrint picker via Bonjour
  (avahi with RFC 6763 §7.1-correct subtype PTRs).
- iOS sends jobs as either PDF or URF; both paths land at the Canon as
  A4-normalized PDF.
- A4 normalizer:
  - Already-A4 pages pass through unchanged.
  - Smaller pages (photos, 4×6, A6, …) centered on A4 at 100% size.
  - US Letter at 100% size with ~8.5pt margin-area clip per horizontal
    side (no 3% downscale).
  - Legal / A3 / oversized pages scaled to fit, then centered.
- URF decoded by our from-scratch Python decoder
  (`airprinthq/urf.py`) → multi-page PDF → A4 normalizer → printer.
- Multi-copy via TCP-loop when iOS sets `copies=N` in IPP; when iOS
  inlines copies as extra URF pages, those go through naturally.
- Dual-stack TCP listener (IPv4 + IPv6) — iOS can use whichever it
  prefers; in practice it picks IPv6 when our AAAA record is reachable.
- Optional IPPS (TLS) endpoint on a separate port — disabled by default
  (`IPPS_PORT=` empty) because iOS doesn't require it for AirPrint.
  When enabled, a self-signed cert is generated and persisted in
  `CERT_DIR` so iOS doesn't show the "different printer" warning on
  every restart.
- Observe-only mode: leave `PRINTER_HOST` empty to accept jobs without
  forwarding (useful for capturing what iOS sends).
- Save-incoming / save-outgoing debug capture via `SAVE_INCOMING_DIR` /
  `SAVE_OUTGOING_DIR` env vars (filenames include ISO timestamp + job
  id + magic-byte-derived extension like `.pdf` / `.urf` / `.jpg`).

### Known limitations

- The iOS print dialog shows paper sizes and toggles we can't control
  (iOS uses hardcoded per-locale UI lists and ignores most of our IPP
  declarations for UI purposes). Functionally moot — the transcoder
  normalizes whatever iOS sends to A4 anyway.
- The Color / B&W toggle is iOS-side UI; we can't hide it.
- "Print PDF Annotations" toggle is iOS-side; we can't change the
  default or hide it.

### Deferred (not implemented)

- True B&W output for the path where iOS sends a *color* PDF with an
  IPP `print-color-mode=monochrome` flag (rare — iOS prefers URF for
  B&W when our `print-color-mode-supported` advertises `monochrome`,
  and the URF path now works correctly). Implementing would require
  reading the IPP attribute and rewriting the PDF in grayscale via
  pypdf — substantial work for marginal benefit.
- A real web admin page at the `adminurl` (currently the URL points at
  our IPP port, which returns 405 Method Not Allowed for browser GETs;
  iOS doesn't seem to care).
- Content-aware alignment on the A4 sheet. The current transcoder
  centers pages based on the PDF `MediaBox` (i.e., the declared paper
  size) — that includes the source document's own margin whitespace.
  A smarter alignment would examine the actual content's bounding box
  (text and image extents) and center the *content* on A4 with even
  margins, rather than centering the source paper rectangle. Would
  help for documents authored with asymmetric margins, edge-to-edge
  photos within a larger page, or content that doesn't fill its
  declared page size. Requires parsing PDF content streams — non-trivial.

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

`python3-aiohttp` pulls in its transitive deps. `python3-pypdf` is used
by the A4-normalization transcoder; `python3-pil` by the URF decoder
to wrap decoded raster pages into a PDF. `avahi-daemon` pulls in
`libavahi-*` and is required because python-zeroconf doesn't emit
RFC 6763 §7.1-correct subtype PTRs (iOS rejects services without the
canonical form).

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
Environment=BONJOUR_NAME=AirPrintHQ
Environment=IPP_DOCUMENT_FORMAT_SUPPORTED=application/pdf,image/urf
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
| `PRINTER_HOST` | no | — | IP or hostname of the real printer (raw 9100 target). **Leave empty for observe-only mode**: the proxy accepts jobs and returns success to the client, but never forwards anything to a printer. Pairs naturally with `SAVE_INCOMING_DIR` for capturing what iOS sends without paper risk. |
| `PRINTER_PORT` | no | `9100` | Port for raw forwarding. |
| `BONJOUR_NAME` | yes | — | DNS-SD service instance name. Must differ from any real printer's Bonjour name on the LAN. |
| `IPP_DOCUMENT_FORMAT_SUPPORTED` | no | `application/pdf,image/urf` | Comma-separated MIME types announced. iOS practically always wraps content in PDF (it carries page-size metadata that raw images don't), so PDF is the only one actually used in flight. URF must remain in the list because iOS gates job submission on URF being claimed (even though our forwarder refuses any URF document the printer can't decode). |
| `IPP_DOCUMENT_FORMAT_DEFAULT` | no | `application/pdf` | Default format if the client doesn't pick one. |
| `IPP_MEDIA_SUPPORTED` | no | `iso_a4_210x297mm,na_index-3x5_3x5in,na_index-4x6_4x6in,na_5x7_5x7in` | Comma-separated PWG media keywords. iOS does NOT strictly obey this list — its print dialog mixes our declared sizes with hardcoded per-locale defaults (it may show A6, Letter etc. we never declared, and may hide ones we do). Functionally moot since the transcoder normalizes every page to A4 regardless. |
| `IPP_MEDIA_DEFAULT` | no | `iso_a4_210x297mm` | Default media. |
| `IPP_PORT` | no | `631` | Plain IPP port. Unset/empty disables plain IPP. |
| `IPPS_PORT` | no | (empty) | IPPS (TLS) port. Unset/empty disables IPPS — that's the default; not needed for AirPrint. |
| `CERT_DIR` | no | `/var/lib/airprinthq` | Persistent dir for the self-signed cert when `IPPS_PORT` is set. systemd's `StateDirectory=` makes one for us. |
| `SAVE_INCOMING_DIR` | no | (empty) | If set, save each incoming job's raw bytes (as received from the client, before transcoding) to this directory. Files named `<ISO timestamp>_job<id>.<ext>` where ext is derived from magic bytes (`.pdf`, `.urf`, `.jpg`, `.tif`, or `.bin`). Useful for inspecting what iOS actually sends. |
| `SAVE_OUTGOING_DIR` | no | (empty) | If set, save the transcoded document (the bytes that would be sent to the printer) to this directory, with the same naming scheme. Useful for verifying the transcoder's output. |

At least one of `IPP_PORT` / `IPPS_PORT` must be set.

Combine `PRINTER_HOST=` (empty) with the save dirs to run in
**observe-only** mode: the proxy accepts jobs, captures them to disk,
and returns success to the client without ever forwarding to a real
printer.

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

## License

Apache License 2.0 — see [LICENSE](LICENSE).
