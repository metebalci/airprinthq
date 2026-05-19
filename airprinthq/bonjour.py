"""Bonjour / mDNS publication via avahi-publish-service.

We previously used python-zeroconf, but that library publishes DNS-SD
subtype PTR records pointing at a synthetic subtype-embedded name
(see RFC 6763 §7.1 for the spec, and the library's own source TODO
"need to make subtypes a first class citizen"). iOS's AirPrint
validator rejects the non-canonical PTR target, so it never probes
our IPP endpoint.

avahi handles subtypes RFC-correctly: the subtype PTR points at the
parent service's canonical instance name. We delegate mDNS to it via
the avahi-publish-service CLI; one subprocess per service type. The
subprocess holds the registration as long as it's alive — killing it
on shutdown is enough to withdraw the service.

Requires the avahi-daemon system service to be running.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)


class BonjourRegistrar:
    def __init__(self, service_name: str,
                 ipp_port: int | None, ipps_port: int | None,
                 txt: dict[str, str]):
        self.service_name = service_name
        self.ipp_port = ipp_port
        self.ipps_port = ipps_port
        self.txt = txt
        self._procs: list[asyncio.subprocess.Process] = []

    async def start(self) -> None:
        registrations = []
        if self.ipp_port:
            registrations.append(
                ("_ipp._tcp", "_universal._sub._ipp._tcp", self.ipp_port))
        if self.ipps_port:
            registrations.append(
                ("_ipps._tcp", "_universal._sub._ipps._tcp", self.ipps_port))
        for service_type, subtype, port in registrations:
            args = [
                "avahi-publish-service",
                f"--subtype={subtype}",
                self.service_name,
                service_type,
                str(port),
            ]
            args.extend(f"{k}={v}" for k, v in self.txt.items())
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            self._procs.append(proc)
            log.info("avahi-publish-service: %s (%s) port %d pid %d",
                     service_type, subtype, port, proc.pid)

    async def stop(self) -> None:
        for proc in self._procs:
            if proc.returncode is None:
                proc.terminate()
        for proc in self._procs:
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                log.warning("avahi-publish-service pid %d didn't exit, killing",
                            proc.pid)
                proc.kill()
                await proc.wait()
        self._procs = []
        log.info("avahi services unpublished")
