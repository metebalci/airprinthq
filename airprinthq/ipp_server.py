"""IPP server — handles the AirPrint client's requests.

Implements (RFC 8011):
  Print-Job, Validate-Job, Get-Printer-Attributes, Get-Job-Attributes,
  Get-Jobs, Cancel-Job, Create-Job, Send-Document

Anything else returns server-error-operation-not-supported.

The Get-Printer-Attributes response is built from the cached merged caps
(real printer + injected formats), so the proxy advertises a faithful
capability set with PDF/TIFF added.

Print-Job / Send-Document hand the document body to the Forwarder.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from aiohttp import web

from . import ipp_codec as ipp
from .caps import MergedCaps
from .forwarder import Forwarder, Job

log = logging.getLogger(__name__)

JOB_STATE_PENDING = 3
JOB_STATE_PROCESSING = 5
JOB_STATE_COMPLETED = 9
JOB_STATE_CANCELED = 7
JOB_STATE_ABORTED = 8

_FORWARDER_STATE_TO_IPP = {
    "pending":    JOB_STATE_PENDING,
    "processing": JOB_STATE_PROCESSING,
    "completed":  JOB_STATE_COMPLETED,
    "canceled":   JOB_STATE_CANCELED,
    "aborted":    JOB_STATE_ABORTED,
}


@dataclass
class ServerState:
    caps: MergedCaps
    forwarder: Forwarder
    self_uri: str                           # ipp://host[:port]/ipp/print as advertised
    jobs: dict[int, Job] = field(default_factory=dict)
    next_job_id: int = 1
    # for Create-Job / Send-Document
    pending_buffers: dict[int, bytearray] = field(default_factory=dict)

    def new_job_id(self) -> int:
        jid = self.next_job_id
        self.next_job_id += 1
        return jid


# --- helpers -----------------------------------------------------------

def _op_group(request: ipp.IppMessage) -> dict[str, ipp.Attribute]:
    g = request.group(ipp.TAG_OPERATION)
    if g is None:
        return {}
    return {a.name: a for a in g.attributes}


def _string_value(attr: Optional[ipp.Attribute], default: str = "") -> str:
    if attr is None or not attr.values:
        return default
    return ipp.decode_string(attr.values[0][1])


def _int_value(attr: Optional[ipp.Attribute], default: int = 0) -> int:
    if attr is None or not attr.values:
        return default
    return ipp.decode_integer(attr.values[0][1])


def _base_response(request: ipp.IppMessage, status: int) -> ipp.IppMessage:
    """Build a response with the standard operation-attributes group."""
    op = _op_group(request)
    charset = _string_value(op.get("attributes-charset"), "utf-8")
    natlang = _string_value(op.get("attributes-natural-language"), "en")
    msg = ipp.IppMessage(
        version=request.version,
        operation_or_status=status,
        request_id=request.request_id,
        groups=[
            ipp.Group(tag=ipp.TAG_OPERATION, attributes=[
                ipp.str_attr("attributes-charset", ipp.TAG_CHARSET, charset),
                ipp.str_attr("attributes-natural-language",
                             ipp.TAG_NATURAL_LANGUAGE, natlang),
            ]),
        ],
    )
    return msg


def _job_attributes_group(state: ServerState, job: Job) -> ipp.Group:
    job_uri = f"{state.self_uri}/{job.job_id}"
    ipp_state = _FORWARDER_STATE_TO_IPP.get(job.state, JOB_STATE_PENDING)
    reason = "none"
    if job.state == "aborted":
        reason = "job-aborted-by-system"
    elif job.state == "canceled":
        reason = "job-canceled-by-user"
    return ipp.Group(tag=ipp.TAG_JOB, attributes=[
        ipp.str_attr("job-uri", ipp.TAG_URI, job_uri),
        ipp.int_attr("job-id", job.job_id),
        ipp.int_attr("job-state", ipp_state, tag=ipp.TAG_ENUM),
        ipp.str_attr("job-state-reasons", ipp.TAG_KEYWORD, reason),
        ipp.str_attr("job-name", ipp.TAG_NAME_WITHOUT_LANG, job.name),
    ])


# --- operation handlers -----------------------------------------------

def op_get_printer_attributes(state: ServerState,
                              request: ipp.IppMessage) -> ipp.IppMessage:
    resp = _base_response(request, ipp.STATUS_OK)
    # Clone the cached printer-attributes group into the response. We
    # encode/decode to ensure the cached message is untouched if the
    # caller later mutates the response.
    src = state.caps.message.group(ipp.TAG_PRINTER)
    cloned = ipp.Group(tag=ipp.TAG_PRINTER,
                       attributes=[ipp.Attribute(name=a.name,
                                                 values=list(a.values))
                                   for a in src.attributes])
    # Make sure printer-uri-supported reflects *our* URI
    for a in cloned.attributes:
        if a.name == "printer-uri-supported":
            a.values = [(ipp.TAG_URI, state.self_uri.encode("utf-8"))]
            break
    resp.groups.append(cloned)
    return resp


def op_validate_job(state: ServerState, request: ipp.IppMessage) -> ipp.IppMessage:
    # Minimal: accept everything.
    return _base_response(request, ipp.STATUS_OK)


def op_print_job(state: ServerState, request: ipp.IppMessage) -> ipp.IppMessage:
    op = _op_group(request)
    job_name = _string_value(op.get("job-name"), "AirPrint job")
    document = request.data
    if not document:
        log.warning("Print-Job with no document data")
    job = Job(job_id=state.new_job_id(), name=job_name, document=document)
    state.jobs[job.job_id] = job
    state.forwarder.submit(job)
    resp = _base_response(request, ipp.STATUS_OK)
    resp.groups.append(_job_attributes_group(state, job))
    return resp


def op_create_job(state: ServerState, request: ipp.IppMessage) -> ipp.IppMessage:
    op = _op_group(request)
    job_name = _string_value(op.get("job-name"), "AirPrint job")
    job = Job(job_id=state.new_job_id(), name=job_name, document=b"")
    state.jobs[job.job_id] = job
    state.pending_buffers[job.job_id] = bytearray()
    resp = _base_response(request, ipp.STATUS_OK)
    resp.groups.append(_job_attributes_group(state, job))
    return resp


def op_send_document(state: ServerState, request: ipp.IppMessage) -> ipp.IppMessage:
    op = _op_group(request)
    job_id = _int_value(op.get("job-id"))
    last_doc = False
    a = op.get("last-document")
    if a is not None and a.values and a.values[0][0] == ipp.TAG_BOOLEAN:
        last_doc = ipp.decode_boolean(a.values[0][1])
    job = state.jobs.get(job_id)
    if job is None:
        return _base_response(request, ipp.STATUS_CLIENT_ERROR_NOT_FOUND)
    buf = state.pending_buffers.setdefault(job_id, bytearray())
    buf.extend(request.data)
    if last_doc:
        job.document = bytes(buf)
        state.pending_buffers.pop(job_id, None)
        state.forwarder.submit(job)
    resp = _base_response(request, ipp.STATUS_OK)
    resp.groups.append(_job_attributes_group(state, job))
    return resp


def op_get_job_attributes(state: ServerState,
                          request: ipp.IppMessage) -> ipp.IppMessage:
    op = _op_group(request)
    job_id = _int_value(op.get("job-id"))
    job = state.jobs.get(job_id)
    if job is None:
        return _base_response(request, ipp.STATUS_CLIENT_ERROR_NOT_FOUND)
    resp = _base_response(request, ipp.STATUS_OK)
    resp.groups.append(_job_attributes_group(state, job))
    return resp


def op_get_jobs(state: ServerState, request: ipp.IppMessage) -> ipp.IppMessage:
    resp = _base_response(request, ipp.STATUS_OK)
    # Return active jobs only (pending/processing) by default.
    op = _op_group(request)
    which = _string_value(op.get("which-jobs"), "not-completed")
    for job in state.jobs.values():
        if which == "not-completed" and job.state in ("completed", "canceled", "aborted"):
            continue
        resp.groups.append(_job_attributes_group(state, job))
    return resp


def op_cancel_job(state: ServerState, request: ipp.IppMessage) -> ipp.IppMessage:
    op = _op_group(request)
    job_id = _int_value(op.get("job-id"))
    job = state.jobs.get(job_id)
    if job is None:
        return _base_response(request, ipp.STATUS_CLIENT_ERROR_NOT_FOUND)
    job.cancelled = True
    log.info("Cancel-Job: marked job %s as cancelled", job_id)
    return _base_response(request, ipp.STATUS_OK)


_DISPATCH = {
    ipp.OP_GET_PRINTER_ATTRIBUTES: op_get_printer_attributes,
    ipp.OP_VALIDATE_JOB: op_validate_job,
    ipp.OP_PRINT_JOB: op_print_job,
    ipp.OP_CREATE_JOB: op_create_job,
    ipp.OP_SEND_DOCUMENT: op_send_document,
    ipp.OP_GET_JOB_ATTRIBUTES: op_get_job_attributes,
    ipp.OP_GET_JOBS: op_get_jobs,
    ipp.OP_CANCEL_JOB: op_cancel_job,
}


async def _handle_ipp(request: web.Request) -> web.Response:
    state: ServerState = request.app["state"]
    raw = await request.read()
    try:
        ipp_req = ipp.decode(raw)
    except ipp.IppParseError as exc:
        log.warning("malformed IPP request: %s", exc)
        return web.Response(status=400, text=f"bad IPP: {exc}")
    op = ipp_req.operation_or_status
    handler = _DISPATCH.get(op)
    if handler is None:
        log.info("unsupported IPP op 0x%04x", op)
        resp = _base_response(ipp_req, ipp.STATUS_SERVER_ERROR_OP_NOT_SUPPORTED)
    else:
        log.info("IPP op 0x%04x from %s", op, request.remote)
        try:
            resp = handler(state, ipp_req)
        except Exception:
            log.exception("handler for op 0x%04x crashed", op)
            resp = _base_response(ipp_req, ipp.STATUS_SERVER_ERROR_INTERNAL)
    body = ipp.encode(resp)
    return web.Response(body=body, content_type="application/ipp")


def build_app(state: ServerState) -> web.Application:
    app = web.Application(client_max_size=512 * 1024 * 1024)  # 512MB job cap
    app["state"] = state
    # AirPrint clients typically POST to /ipp/print, but some send to /
    app.router.add_post("/ipp/print", _handle_ipp)
    app.router.add_post("/", _handle_ipp)
    return app
