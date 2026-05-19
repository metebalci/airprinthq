"""Serial TCP forwarder to PRINTER_IP:PRINTER_PORT.

A single background worker pulls jobs off an asyncio.Queue and streams
each one to the printer's raw port (typically 9100). Serial dispatch
avoids races in the printer's port-9100 magic-byte dispatcher when
AirPrint submits multiple jobs in quick succession.

Cancel-Job is supported by setting Job.cancelled = True. If the job has
not yet started, the worker skips it. If it's mid-stream, the worker
closes the transport at its next chunk boundary.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import transcode

log = logging.getLogger(__name__)

_CHUNK = 64 * 1024
# If set, save each job's raw incoming document (as received from the
# client, before transcoding) to this directory. Useful to inspect
# what iOS / clients actually send. Combine with PRINTER_HOST empty
# for observe-only mode (jobs accepted, nothing forwarded).
_SAVE_INCOMING_DIR = os.environ.get("SAVE_INCOMING_DIR", "").strip()
# If set, save the transcoded document (the bytes that would be sent
# to the printer's port 9100) to this directory. Useful for verifying
# the transcoder's output.
_SAVE_OUTGOING_DIR = os.environ.get("SAVE_OUTGOING_DIR", "").strip()


def _ext_for_bytes(data: bytes) -> str:
    if data[:4] == b"%PDF":
        return "pdf"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "tif"
    return "bin"


def _save(dir_: str, job_id: int, data: bytes, label: str) -> None:
    try:
        d = Path(dir_)
        d.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        name = f"{stamp}_job{job_id}.{_ext_for_bytes(data)}"
        (d / name).write_bytes(data)
        log.info("saved %s job %d to %s/%s", label, job_id, d, name)
    except Exception:
        log.exception("save-%s failed", label)


@dataclass
class Job:
    job_id: int
    name: str
    document: bytes
    state: str = "pending"          # pending | processing | completed | canceled | aborted
    cancelled: bool = False
    done: asyncio.Event = field(default_factory=asyncio.Event)
    error: Optional[str] = None


class Forwarder:
    def __init__(self, host: Optional[str], port: int):
        self.host = host
        self.port = port
        self._queue: asyncio.Queue[Job] = asyncio.Queue()
        self._worker: Optional[asyncio.Task] = None
        self._stopping = False

    def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._run(), name="forwarder")

    async def stop(self) -> None:
        self._stopping = True
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass

    def submit(self, job: Job) -> None:
        self._queue.put_nowait(job)

    async def _run(self) -> None:
        while not self._stopping:
            try:
                job = await self._queue.get()
            except asyncio.CancelledError:
                return
            await self._send(job)

    async def _send(self, job: Job) -> None:
        if job.cancelled:
            job.state = "canceled"
            job.done.set()
            log.info("job %s canceled before send", job.job_id)
            return
        job.state = "processing"
        if _SAVE_INCOMING_DIR:
            _save(_SAVE_INCOMING_DIR, job.job_id, job.document, "incoming")
        document = transcode.transcode_to_a4(job.document)
        if _SAVE_OUTGOING_DIR:
            _save(_SAVE_OUTGOING_DIR, job.job_id, document, "outgoing")
        if self.host is None:
            job.state = "completed"
            job.done.set()
            log.info("job %s observe-only (%d -> %d bytes); not forwarded",
                     job.job_id, len(job.document), len(document))
            return
        log.info("forwarding job %s (%d bytes) to %s:%s",
                 job.job_id, len(document), self.host, self.port)
        writer = None
        try:
            _, writer = await asyncio.open_connection(self.host, self.port)
            for offset in range(0, len(document), _CHUNK):
                if job.cancelled:
                    log.info("job %s canceled mid-stream", job.job_id)
                    writer.close()
                    await writer.wait_closed()
                    job.state = "canceled"
                    return
                writer.write(document[offset:offset + _CHUNK])
                await writer.drain()
            writer.close()
            await writer.wait_closed()
            job.state = "completed"
            log.info("job %s completed", job.job_id)
        except Exception as exc:
            job.state = "aborted"
            job.error = str(exc)
            log.exception("job %s failed", job.job_id)
            if writer is not None:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass
        finally:
            job.done.set()
