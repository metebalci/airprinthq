"""airprinthq — standalone IPP/AirPrint proxy.

Env vars (all four IPP_* names match the IPP attribute they set):
    PRINTER_HOST                   real printer for raw-9100 forward (required)
    PRINTER_PORT                   default 9100
    BONJOUR_NAME                   Bonjour service instance name (required)
    IPP_DOCUMENT_FORMAT_SUPPORTED  comma-sep MIME types
                                   (default: application/pdf,image/jpeg,image/tiff,image/urf)
    IPP_DOCUMENT_FORMAT_DEFAULT    default: application/pdf
    IPP_MEDIA_SUPPORTED            comma-sep PWG media keywords
                                   (default: iso_a4_210x297mm)
    IPP_MEDIA_DEFAULT              default: iso_a4_210x297mm
    IPP_PORT                       plain IPP port; unset/empty disables IPP
    IPPS_PORT                      IPPS (TLS) port; unset/empty disables IPPS
    CERT_DIR                       persistent dir for the self-signed cert
                                   (default /var/lib/airprinthq) — used only
                                   when IPPS_PORT is set

At least one of IPP_PORT / IPPS_PORT must be set.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import sys
from pathlib import Path
from typing import Optional

from aiohttp import web

from .bonjour import BonjourRegistrar
from .caps import build_standalone
from .forwarder import Forwarder
from .ipp_server import build_app, ServerState
from .tls import detect_host_ip, make_ssl_context

log = logging.getLogger("airprinthq")


def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    if not v:
        return default
    try:
        return int(v)
    except ValueError:
        log.warning("invalid %s=%r, using default %d", name, v, default)
        return default


def _env_opt_int(name: str) -> Optional[int]:
    """Return the parsed int, or None if the env var is unset/empty."""
    v = os.environ.get(name, "").strip()
    if not v:
        return None
    try:
        return int(v)
    except ValueError:
        log.warning("invalid %s=%r, treating as disabled", name, v)
        return None


def _env_list(name: str, default: list[str]) -> list[str]:
    v = os.environ.get(name)
    if v is None:
        return default
    return [s.strip() for s in v.split(",") if s.strip()]


def _env_str(name: str, default: str) -> str:
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else default


async def amain() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    printer_host = os.environ.get("PRINTER_HOST", "").strip() or None
    printer_port = _env_int("PRINTER_PORT", 9100)

    bonjour_name = os.environ.get("BONJOUR_NAME", "").strip()
    if not bonjour_name:
        log.error("BONJOUR_NAME is required")
        return 2

    formats = _env_list("IPP_DOCUMENT_FORMAT_SUPPORTED",
                        ["application/pdf", "image/urf"])
    format_default = _env_str("IPP_DOCUMENT_FORMAT_DEFAULT", "application/pdf")
    media = _env_list("IPP_MEDIA_SUPPORTED", ["iso_a4_210x297mm"])
    media_default = _env_str("IPP_MEDIA_DEFAULT", "iso_a4_210x297mm")
    ipp_port = _env_opt_int("IPP_PORT")
    ipps_port = _env_opt_int("IPPS_PORT")
    if ipp_port is None and ipps_port is None:
        log.error("at least one of IPP_PORT or IPPS_PORT must be set")
        return 2
    cert_dir = Path(_env_str("CERT_DIR", "/var/lib/airprinthq"))

    hostname = socket.gethostname()
    fqdn = f"{hostname}.local"

    forward_target = (f"{printer_host}:{printer_port}" if printer_host
                      else "OBSERVE-ONLY (no PRINTER_HOST)")
    log.info("airprinthq starting: forward=%s, IPP=%s, IPPS=%s, "
             "host=%s, formats=%s, media=%s",
             forward_target,
             ipp_port if ipp_port else "disabled",
             ipps_port if ipps_port else "disabled",
             fqdn, formats, media)

    ssl_ctx = None
    if ipps_port:
        host_ip = detect_host_ip(printer_host or "1.1.1.1", printer_port)
        cert_dir.mkdir(parents=True, exist_ok=True)
        ssl_ctx, cert_generated = make_ssl_context(fqdn, host_ip, cert_dir)
        if cert_generated:
            log.info("generated self-signed cert (CN=%s, SAN IP=%s) in %s",
                     fqdn, host_ip, cert_dir)
        else:
            log.info("reusing existing cert in %s", cert_dir)

    # The adminurl points at whichever endpoint is enabled (prefer IPP).
    if ipp_port:
        adminurl = f"http://{fqdn}:{ipp_port}/"
    else:
        adminurl = f"https://{fqdn}:{ipps_port}/"
    ipp_uri = f"ipp://{fqdn}:{ipp_port}/ipp/print" if ipp_port else None
    ipps_uri = f"ipps://{fqdn}:{ipps_port}/ipp/print" if ipps_port else None
    caps = build_standalone(bonjour_name=bonjour_name,
                            formats=formats, format_default=format_default,
                            media=media, media_default=media_default,
                            adminurl=adminurl,
                            ipp_uri=ipp_uri, ipps_uri=ipps_uri)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    forwarder = Forwarder(printer_host, printer_port)
    forwarder.start()

    state = ServerState(caps=caps, forwarder=forwarder,
                        self_uri=ipp_uri or ipps_uri)
    app = build_app(state)

    runner = web.AppRunner(app)
    await runner.setup()
    if ipp_port:
        ipp_site = web.TCPSite(runner, host=None, port=ipp_port)
        await ipp_site.start()
        log.info("IPP listening on port %d (dual-stack)", ipp_port)
    if ipps_port:
        ipps_site = web.TCPSite(runner, host=None, port=ipps_port,
                                ssl_context=ssl_ctx)
        await ipps_site.start()
        log.info("IPPS listening on port %d (dual-stack)", ipps_port)

    bonjour = BonjourRegistrar(
        service_name=caps.txt["ty"],
        ipp_port=ipp_port,
        ipps_port=ipps_port,
        txt=caps.txt,
    )
    await bonjour.start()

    await stop_event.wait()

    log.info("shutting down...")
    await bonjour.stop()
    await runner.cleanup()
    await forwarder.stop()
    return 0


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
