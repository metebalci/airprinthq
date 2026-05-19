"""Self-signed TLS cert for IPPS.

iOS will not probe an AirPrint printer that doesn't have a reachable
IPPS endpoint — TXT-level TLS=1.3 alone isn't enough. iOS accepts
self-signed certs from AirPrint printers without CA validation, so no
trust setup needed on the client.

The cert is generated ONCE into a persistent dir (CERT_DIR, e.g.
/var/lib/airprinthq) and reused on subsequent starts. Regenerating
every start changes the cert's public-key fingerprint, which makes
iOS flag the printer as "different from the previously used printer
with the same name" — a warning iOS users have to dismiss. The cert's
DNS SAN (airprinthq.local) is stable across DHCP renewals, so we
don't need to rotate on IP change either.

To force regeneration, delete CERT_DIR/cert.pem and CERT_DIR/key.pem.
"""

from __future__ import annotations

import socket
import ssl
import subprocess
from pathlib import Path


def detect_host_ip(reachable_host: str, reachable_port: int = 9100) -> str:
    """Return the source IPv4 the kernel would use to reach reachable_host.

    Uses a UDP socket's `connect` (no packet sent) so we don't rely on
    interface enumeration, default-route parsing, or DNS round-trips.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((reachable_host, reachable_port))
        return s.getsockname()[0]
    finally:
        s.close()


def make_ssl_context(hostname_fqdn: str, host_ip: str,
                     cert_dir: Path) -> tuple[ssl.SSLContext, bool]:
    """Return an SSLContext for IPPS. Generate the cert if missing.

    Returns (ctx, generated) where `generated` is True iff a new cert
    was created on this call. Otherwise an existing cert+key from
    cert_dir is loaded as-is.
    """
    cert_path = cert_dir / "cert.pem"
    key_path = cert_dir / "key.pem"
    generated = False
    if not (cert_path.is_file() and key_path.is_file()):
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", str(key_path), "-out", str(cert_path),
            "-days", "3650", "-nodes",
            "-subj", f"/CN={hostname_fqdn}",
            "-addext", f"subjectAltName=DNS:{hostname_fqdn},IP:{host_ip}",
        ], check=True, stderr=subprocess.DEVNULL)
        generated = True
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    return ctx, generated
