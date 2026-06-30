"""Hardcoded AirPrint capabilities + Bonjour TXT derivation.

build_standalone() produces a MergedCaps with:
  - the printer-attributes IPP group we'll serve verbatim
  - the document-format-supported list
  - the Bonjour TXT record dict

To change capabilities, edit build_standalone() directly. The four IPP
attributes most likely to need tweaking (formats, media) are accepted as
arguments and wired through from env vars in __main__.py. Everything
else (DPI, color/duplex, URF string, sides, copies, etc.) is hardcoded.
"""

from __future__ import annotations

import time
import uuid as _uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from . import ipp_codec as ipp

# Stable namespace for deriving the proxy's UUID. The UUID is the same
# across restarts as long as the AirPrint name doesn't change.
_PROXY_UUID_NS = _uuid.UUID("e1a3f9c2-1f6e-4d4e-bd8a-1c2d3e4f5a6b")

# URF is required for iOS to surface the service in the AirPrint picker.
# image/urf must be in document-format-supported AND the URF= TXT key
# must match urf-supported — iOS rejects an inconsistent pair.
# Value lifted verbatim from a Canon LBP673C II with AirPrint on.
DEFAULT_URF = "CP255,DEVCMYK32,PQ5,RS600,SRGB24,W8-16,DM3,FN3,IS1-4,OB10-40,V1.5"


@dataclass
class MergedCaps:
    message: ipp.IppMessage
    formats: list[str]
    txt: dict[str, str]


def _airprint_txt(bonjour_name: str, formats: list[str], urf: str,
                  uuid_str: str, color: bool, duplex: bool,
                  adminurl: str,
                  tls_supported: bool) -> dict[str, str]:
    """Minimal AirPrint TXT — modeled on CUPS's ippeveprinter reference.

    Optional keys:
      URF=   emitted only when image/urf is in `formats` (must stay
             consistent with IPP `urf-supported` — iOS rejects mismatch).
      TLS=   emitted only when an IPPS endpoint is available.
    """
    txt = {
        "txtvers": "1",
        "qtotal": "1",
        "rp": "ipp/print",
        "note": "",
        "ty": bonjour_name,
        "pdl": ",".join(formats),
        "adminurl": adminurl,
        "Color": "T" if color else "F",
        "Duplex": "T" if duplex else "F",
        # Apple BPS enum: <legal-A4 (smaller), legal-A4 (office tier
        # covering both Legal and A4), tabloid-A3, isoC-A2, >isoC-A2.
        # No A4-only value exists; legal-A4 is the closest match.
        "PaperMax": "legal-A4",
        # kind= controls which paper-size categories iOS surfaces in the
        # print dialog dropdown. "photo" is required for 5×7 to appear
        # (3.5×5 and 4×6 also exist as index-card sizes under "document",
        # so they appear regardless).
        "kind": "document,photo,postcard",
        "UUID": uuid_str.removeprefix("urn:uuid:"),
    }
    if "image/urf" in formats:
        txt["URF"] = urf
    if tls_supported:
        txt["TLS"] = "1.3"
    return txt


def build_standalone(bonjour_name: str,
                     formats: list[str],
                     format_default: str,
                     media: list[str],
                     media_default: str,
                     adminurl: str,
                     ipp_uri: str | None,
                     ipps_uri: str | None,
                     urf: str = DEFAULT_URF,
                     color: bool = True,
                     duplex: bool = True,
                     ) -> MergedCaps:
    """Build the hardcoded AirPrint capability set.

    `formats`, `format_default`, `media`, `media_default` come from env vars
    (IPP_DOCUMENT_FORMAT_SUPPORTED / _DEFAULT, IPP_MEDIA_SUPPORTED / _DEFAULT).
    `adminurl`, `ipp_uri`, `ipps_uri` are composed in __main__. Either of
    `ipp_uri` or `ipps_uri` may be None (but not both — caller checks).
    Everything else is hardcoded here; edit this function to change them.
    """
    assert ipp_uri or ipps_uri, "at least one URI must be set"
    proxy_uuid = "urn:uuid:" + str(_uuid.uuid5(_PROXY_UUID_NS, bonjour_name))

    # printer-config-change-* / printer-state-change-* are how iOS knows
    # to invalidate its cached Get-Printer-Attributes response: when these
    # bump, iOS refetches. We anchor them at process-start so every
    # service restart invalidates iOS's cache.
    now = datetime.now(timezone.utc)
    change_ts_int = int(time.time())
    change_ts_dt = ipp.encode_date_time(
        now.year, now.month, now.day,
        now.hour, now.minute, now.second)

    printer = ipp.Group(tag=ipp.TAG_PRINTER, attributes=[
        ipp.str_attr("printer-name", ipp.TAG_NAME_WITHOUT_LANG, bonjour_name),
        ipp.str_attr("printer-info", ipp.TAG_TEXT_WITHOUT_LANG, bonjour_name),
        ipp.str_attr("printer-make-and-model", ipp.TAG_TEXT_WITHOUT_LANG, bonjour_name),
        ipp.str_attr("printer-dns-sd-name", ipp.TAG_NAME_WITHOUT_LANG, bonjour_name),
        ipp.str_attr("printer-uuid", ipp.TAG_URI, proxy_uuid),
        ipp.str_attr("printer-location", ipp.TAG_TEXT_WITHOUT_LANG, ""),
        ipp.str_attr("printer-more-info", ipp.TAG_URI, adminurl),
        ipp.str_attr("ipp-versions-supported", ipp.TAG_KEYWORD, "2.0", "1.1"),
        ipp.str_attr("ipp-features-supported", ipp.TAG_KEYWORD,
                     "airprint-2.1", "ipp-everywhere"),
        ipp.str_attr("charset-configured", ipp.TAG_CHARSET, "utf-8"),
        ipp.str_attr("charset-supported", ipp.TAG_CHARSET, "utf-8"),
        ipp.str_attr("natural-language-configured", ipp.TAG_NATURAL_LANGUAGE, "en"),
        ipp.str_attr("generated-natural-language-supported",
                     ipp.TAG_NATURAL_LANGUAGE, "en"),
        ipp.int_attr("printer-state", 3, tag=ipp.TAG_ENUM),
        ipp.str_attr("printer-state-reasons", ipp.TAG_KEYWORD, "none"),
        ipp.bool_attr("printer-is-accepting-jobs", True),
        ipp.int_attr("queued-job-count", 0),
        ipp.int_attr("printer-up-time", 1),
        ipp.int_attr("printer-config-change-time", change_ts_int),
        ipp.attr("printer-config-change-date-time",
                 ipp.TAG_DATE_TIME, change_ts_dt),
        ipp.int_attr("printer-state-change-time", change_ts_int),
        ipp.attr("printer-state-change-date-time",
                 ipp.TAG_DATE_TIME, change_ts_dt),
        ipp.int_attr("operations-supported",
                     ipp.OP_PRINT_JOB, ipp.OP_VALIDATE_JOB,
                     ipp.OP_CREATE_JOB, ipp.OP_SEND_DOCUMENT,
                     ipp.OP_CANCEL_JOB, ipp.OP_GET_JOB_ATTRIBUTES,
                     ipp.OP_GET_JOBS, ipp.OP_GET_PRINTER_ATTRIBUTES,
                     tag=ipp.TAG_ENUM),
        ipp.str_attr("document-format-default", ipp.TAG_MIME_MEDIA_TYPE,
                     format_default),
        ipp.str_attr("document-format-supported", ipp.TAG_MIME_MEDIA_TYPE,
                     *formats),
        ipp.str_attr("compression-supported", ipp.TAG_KEYWORD, "none"),
        ipp.str_attr("pdl-override-supported", ipp.TAG_KEYWORD, "attempted"),
        ipp.bool_attr("color-supported", color),
        ipp.str_attr("print-color-mode-default", ipp.TAG_KEYWORD, "auto"),
        ipp.str_attr("print-color-mode-supported", ipp.TAG_KEYWORD,
                     "auto", "color", "monochrome"),
        ipp.str_attr("sides-default", ipp.TAG_KEYWORD, "one-sided"),
        ipp.str_attr("sides-supported", ipp.TAG_KEYWORD,
                     *(("one-sided", "two-sided-long-edge", "two-sided-short-edge")
                       if duplex else ("one-sided",))),
        ipp.int_attr("copies-default", 1),
        ipp.int_attr("orientation-requested-default", 3, tag=ipp.TAG_ENUM),
        ipp.str_attr("media-default", ipp.TAG_KEYWORD, media_default),
        ipp.str_attr("media-supported", ipp.TAG_KEYWORD, *media),
        ipp.attr("printer-resolution-default", ipp.TAG_RESOLUTION,
                 ipp.encode_resolution(600, 600, 3)),
        ipp.attr("printer-resolution-supported", ipp.TAG_RESOLUTION,
                 ipp.encode_resolution(600, 600, 3),
                 ipp.encode_resolution(1200, 1200, 3)),
    ])
    uris = [u for u in (ipp_uri, ipps_uri) if u]
    security = [s for s, u in (("none", ipp_uri), ("tls", ipps_uri)) if u]
    auth = ["none"] * len(uris)
    printer.attributes.extend([
        ipp.str_attr("printer-uri-supported", ipp.TAG_URI, *uris),
        ipp.str_attr("uri-security-supported", ipp.TAG_KEYWORD, *security),
        ipp.str_attr("uri-authentication-supported", ipp.TAG_KEYWORD, *auth),
    ])
    if "image/urf" in formats:
        printer.attributes.append(
            ipp.str_attr("urf-supported", ipp.TAG_KEYWORD, *urf.split(",")))
    msg = ipp.IppMessage(version=(2, 0), operation_or_status=0,
                         request_id=1, groups=[printer])

    return MergedCaps(message=msg, formats=formats,
                      txt=_airprint_txt(bonjour_name, formats, urf,
                                        proxy_uuid, color, duplex,
                                        adminurl, tls_supported=bool(ipps_uri)))
