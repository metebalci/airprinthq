#!/usr/bin/env python3
"""Tiny stdlib-only mDNS query tool. Sends a multicast query for the given
service type (default _ipp._tcp.local PTR) and prints every response.

Usage:
    ./mdns-query.py                          # default: _ipp._tcp.local PTR
    ./mdns-query.py _http._tcp.local         # any service type
    ./mdns-query.py nimbusdev.local A        # any name + type
    ./mdns-query.py -t 5 _ipp._tcp.local PTR # custom timeout

Types: A=1, AAAA=28, PTR=12, TXT=16, SRV=33, ANY=255
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys
import time

TYPES = {
    1: "A", 2: "NS", 5: "CNAME", 6: "SOA", 12: "PTR", 15: "MX",
    16: "TXT", 28: "AAAA", 33: "SRV", 47: "NSEC", 255: "ANY",
}
TYPES_BY_NAME = {v: k for k, v in TYPES.items()}


def encode_name(name: str) -> bytes:
    out = b""
    for label in name.rstrip(".").split("."):
        if not label:
            continue
        b = label.encode("utf-8")
        if len(b) > 63:
            raise ValueError(f"label too long: {label!r}")
        out += bytes([len(b)]) + b
    return out + b"\x00"


def decode_name(data: bytes, offset: int) -> tuple[str, int]:
    """Decode a DNS name (with compression). Returns (name, new_offset)."""
    labels = []
    jumped = False
    next_offset = offset
    safety = 0
    while True:
        safety += 1
        if safety > 256:
            raise ValueError("name decode loop")
        if offset >= len(data):
            break
        length = data[offset]
        if length == 0:
            offset += 1
            if not jumped:
                next_offset = offset
            break
        if length & 0xC0 == 0xC0:
            # pointer
            if offset + 1 >= len(data):
                raise ValueError("truncated pointer")
            ptr = ((length & 0x3F) << 8) | data[offset + 1]
            offset += 2
            if not jumped:
                next_offset = offset
                jumped = True
            offset = ptr
            continue
        offset += 1
        labels.append(data[offset:offset + length].decode("utf-8", errors="replace"))
        offset += length
    return ".".join(labels), next_offset


def decode_rdata(rtype: int, rdata: bytes, full: bytes, rdata_offset: int) -> str:
    if rtype == 1 and len(rdata) == 4:
        return socket.inet_ntop(socket.AF_INET, rdata)
    if rtype == 28 and len(rdata) == 16:
        return socket.inet_ntop(socket.AF_INET6, rdata)
    if rtype == 12:
        try:
            name, _ = decode_name(full, rdata_offset)
            return name
        except Exception as e:
            return f"<bad PTR: {e}>"
    if rtype == 33 and len(rdata) >= 7:
        prio, weight, port = struct.unpack(">HHH", rdata[:6])
        try:
            target, _ = decode_name(full, rdata_offset + 6)
        except Exception:
            target = rdata[6:].hex()
        return f"prio={prio} weight={weight} port={port} target={target}"
    if rtype == 16:
        out = []
        i = 0
        while i < len(rdata):
            n = rdata[i]
            i += 1
            if i + n > len(rdata):
                break
            out.append(rdata[i:i + n].decode("utf-8", errors="replace"))
            i += n
        return " | ".join(out)
    return rdata.hex() if len(rdata) <= 64 else rdata[:64].hex() + f"... ({len(rdata)} bytes)"


def parse_response(data: bytes, src: tuple[str, int]) -> None:
    if len(data) < 12:
        print(f"  from {src[0]}:{src[1]}: short ({len(data)} bytes)")
        return
    qid, flags, qd, an, ns, ar = struct.unpack(">HHHHHH", data[:12])
    print(f"  from {src[0]}:{src[1]} qid=0x{qid:04x} flags=0x{flags:04x} "
          f"qd={qd} an={an} ns={ns} ar={ar} bytes={len(data)}")
    offset = 12
    # skip questions
    for _ in range(qd):
        try:
            _, offset = decode_name(data, offset)
            offset += 4  # qtype + qclass
        except Exception as e:
            print(f"    <error in question section: {e}>")
            return
    # answers + authority + additional
    for section, count in [("AN", an), ("NS", ns), ("AR", ar)]:
        for _ in range(count):
            try:
                name, offset = decode_name(data, offset)
                if offset + 10 > len(data):
                    print(f"    <truncated record header>")
                    return
                rtype, rclass, ttl, rdlength = struct.unpack(">HHIH",
                                                              data[offset:offset + 10])
                offset += 10
                rdata = data[offset:offset + rdlength]
                rstr = decode_rdata(rtype, rdata, data, offset)
                offset += rdlength
                cls = "IN" if (rclass & 0x7FFF) == 1 else f"cls{rclass & 0x7FFF}"
                flush = " (flush)" if rclass & 0x8000 else ""
                tname = TYPES.get(rtype, f"type{rtype}")
                print(f"    [{section}] {name} {cls}{flush} {tname} ttl={ttl}: {rstr}")
            except Exception as e:
                print(f"    <error in {section} section: {e}>")
                return


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("name", nargs="?", default="_ipp._tcp.local",
                   help="name to query (default: _ipp._tcp.local)")
    p.add_argument("type", nargs="?", default="PTR",
                   help="record type, name or number (default: PTR)")
    p.add_argument("-t", "--timeout", type=float, default=3.0,
                   help="how long to listen for responses (default 3s)")
    p.add_argument("-u", "--unicast", action="store_true",
                   help="set the QU (Unicast Response) bit on the question")
    p.add_argument("-m", "--mdns-port", action="store_true",
                   help="bind source port to 5353 + join multicast group "
                        "(makes the query look like a real mDNS query so "
                        "other responders multicast their answers; needs "
                        "SO_REUSEPORT to coexist with avahi/zeroconf)")
    p.add_argument("--addr", default="224.0.0.251",
                   help="destination address (default 224.0.0.251)")
    p.add_argument("--port", type=int, default=5353,
                   help="destination port (default 5353)")
    args = p.parse_args()

    if args.type.isdigit():
        qtype = int(args.type)
    else:
        qtype = TYPES_BY_NAME.get(args.type.upper())
        if qtype is None:
            print(f"unknown type {args.type!r}; known: {sorted(TYPES_BY_NAME)}",
                  file=sys.stderr)
            return 2

    qid = 0
    qclass = 1 | (0x8000 if args.unicast else 0)
    pkt = struct.pack(">HHHHHH", qid, 0, 1, 0, 0, 0) + \
        encode_name(args.name) + struct.pack(">HH", qtype, qclass)

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
    if args.mdns_port:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        s.bind(("", 5353))
        mreq = struct.pack("4sl", socket.inet_aton("224.0.0.251"),
                           socket.INADDR_ANY)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    else:
        s.bind(("", 0))
    s.sendto(pkt, (args.addr, args.port))
    src_port = s.getsockname()[1]
    print(f"query: name={args.name!r} type={TYPES.get(qtype, qtype)} -> "
          f"{args.addr}:{args.port} from local port {src_port}"
          f"{' (QU)' if args.unicast else ''}"
          f"{' [mdns-port + multicast group]' if args.mdns_port else ''}")
    print(f"listening for {args.timeout}s...")

    s.settimeout(0.5)
    deadline = time.time() + args.timeout
    count = 0
    while time.time() < deadline:
        try:
            data, addr = s.recvfrom(9000)
        except socket.timeout:
            continue
        count += 1
        parse_response(data, addr)
    print(f"-- {count} response packet(s) --")
    return 0


if __name__ == "__main__":
    sys.exit(main())
