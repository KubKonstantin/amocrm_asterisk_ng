#!/usr/bin/env python3
"""Config-driven VoIP autotest runner with GitLab-compatible JUnit report.

Checks:
1. WebRTC signaling (HTTP or WebSocket endpoint health/check exchange)
2. SIP signaling (UDP OPTIONS -> response status code)
3. RTP media path (RTP packets sent/received)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import re
import socket
import struct
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

import aiohttp
import yaml

RTP_HEADER_SIZE = 12
RTP_CLOCK_RATE = 8000


@dataclass
class CheckResult:
    name: str
    passed: bool
    duration_sec: float
    details: str = ""


@dataclass
class SessionContext:
    call_id: str
    started_at: float
    metadata: dict[str, Any]


@dataclass(frozen=True)
class RtpPacket:
    payload_type: int
    sequence: int
    timestamp: int
    ssrc: int
    marker: int
    payload: bytes

    def to_bytes(self) -> bytes:
        first_byte = 0x80
        second_byte = ((self.marker & 0x01) << 7) | (self.payload_type & 0x7F)
        header = struct.pack("!BBHII", first_byte, second_byte, self.sequence, self.timestamp, self.ssrc)
        return header + self.payload

    @classmethod
    def from_bytes(cls, raw: bytes) -> "RtpPacket":
        if len(raw) < RTP_HEADER_SIZE:
            raise ValueError("RTP packet too short")

        first_byte, second_byte, sequence, timestamp, ssrc = struct.unpack("!BBHII", raw[:RTP_HEADER_SIZE])
        version = first_byte >> 6
        if version != 2:
            raise ValueError(f"Unsupported RTP version {version}")

        return cls(
            payload_type=second_byte & 0x7F,
            marker=(second_byte >> 7) & 0x01,
            sequence=sequence,
            timestamp=timestamp,
            ssrc=ssrc,
            payload=raw[RTP_HEADER_SIZE:],
        )


def load_config(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fp:
        data = yaml.safe_load(fp) or {}
    if not isinstance(data, dict):
        raise ValueError("Config root must be a mapping")
    return data


def generate_payload(frame_samples: int, frame_idx: int, freq: float = 440.0) -> bytes:
    buf = bytearray(frame_samples)
    for i in range(frame_samples):
        sample_idx = frame_idx * frame_samples + i
        value = math.sin(2.0 * math.pi * freq * sample_idx / RTP_CLOCK_RATE)
        buf[i] = int((value + 1.0) * 127.5) & 0xFF
    return bytes(buf)


async def run_webrtc_signaling_check(config: dict[str, Any], ctx: SessionContext) -> CheckResult:
    name = "WebRTC signaling"
    started = time.monotonic()
    if not config.get("enabled", True):
        return CheckResult(name=name, passed=True, duration_sec=0.0, details="Skipped (disabled)")

    mode = config.get("mode", "websocket")
    timeout_sec = float(config.get("timeout_sec", 5.0))

    try:
        timeout = aiohttp.ClientTimeout(total=timeout_sec)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            if mode == "http":
                method = str(config.get("method", "GET")).upper()
                url = str(config["url"])
                expected_status = int(config.get("expected_status", 200))
                body = config.get("body")
                if isinstance(body, dict):
                    body = json.loads(json.dumps(body).replace("{{call_id}}", ctx.call_id))

                async with session.request(method=method, url=url, json=body) as response:
                    response_text = await response.text()
                    if response.status != expected_status:
                        return CheckResult(
                            name=name,
                            passed=False,
                            duration_sec=time.monotonic() - started,
                            details=f"HTTP status {response.status}, expected {expected_status}, body={response_text[:400]}",
                        )
                    return CheckResult(
                        name=name,
                        passed=True,
                        duration_sec=time.monotonic() - started,
                        details=f"HTTP {method} {url} -> {response.status}",
                    )

            if mode == "websocket":
                url = str(config["url"])
                offer = str(config.get("offer", "ping")).replace("{{call_id}}", ctx.call_id)
                expected_substring = str(config.get("expected_answer_contains", ""))
                async with session.ws_connect(url) as ws:
                    await ws.send_str(offer)
                    message = await ws.receive(timeout=timeout_sec)
                    if message.type not in {aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY}:
                        return CheckResult(
                            name=name,
                            passed=False,
                            duration_sec=time.monotonic() - started,
                            details=f"Unexpected WS message type: {message.type}",
                        )
                    received_text = message.data.decode() if isinstance(message.data, bytes) else str(message.data)
                    if expected_substring:
                        expected_substring = expected_substring.replace("{{call_id}}", ctx.call_id)
                    if expected_substring and expected_substring not in received_text:
                        return CheckResult(
                            name=name,
                            passed=False,
                            duration_sec=time.monotonic() - started,
                            details=f"WS response mismatch, expected substring '{expected_substring}', got '{received_text[:200]}'",
                        )
                    match_pattern = str(config.get("response_call_id_pattern", ""))
                    if match_pattern and not re.search(match_pattern, received_text):
                        return CheckResult(
                            name=name,
                            passed=False,
                            duration_sec=time.monotonic() - started,
                            details=f"WS response does not match response_call_id_pattern={match_pattern}",
                        )

                    if config.get("response_json", False):
                        parsed = json.loads(received_text)
                        if isinstance(parsed, dict):
                            ctx.metadata.update(parsed)

                    return CheckResult(
                        name=name,
                        passed=True,
                        duration_sec=time.monotonic() - started,
                        details=f"WS exchange success ({len(received_text)} bytes), call_id={ctx.call_id}",
                    )

            return CheckResult(
                name=name,
                passed=False,
                duration_sec=time.monotonic() - started,
                details=f"Unsupported webrtc.mode={mode}",
            )
    except Exception as exc:
        return CheckResult(
            name=name,
            passed=False,
            duration_sec=time.monotonic() - started,
            details=f"Exception: {exc}",
        )


async def run_sip_check(config: dict[str, Any], session: SessionContext) -> CheckResult:
    name = "SIP"
    started = time.monotonic()
    if not config.get("enabled", True):
        return CheckResult(name=name, passed=True, duration_sec=0.0, details="Skipped (disabled)")

    host = str(config["host"])
    port = int(config.get("port", 5060))
    timeout_sec = float(config.get("timeout_sec", 5.0))
    expected_codes = {int(v) for v in config.get("expected_status_codes", [200])}

    from_user = str(config.get("from_user", "autotest"))
    to_user = str(config.get("to_user", "autotest"))
    domain = str(config.get("domain", host))

    branch = f"z9hG4bK-{random.randint(100000, 999999)}"
    tag = f"{random.randint(1000, 9999)}"
    call_id = session.call_id
    cseq = random.randint(1, 9999)

    request = (
        f"OPTIONS sip:{to_user}@{domain} SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP 0.0.0.0:0;branch={branch};rport\r\n"
        f"From: <sip:{from_user}@{domain}>;tag={tag}\r\n"
        f"To: <sip:{to_user}@{domain}>\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: {cseq} OPTIONS\r\n"
        f"Max-Forwards: 70\r\n"
        f"User-Agent: voip-autotest\r\n"
        f"Content-Length: 0\r\n\r\n"
    ).encode()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout_sec)
    try:
        def _exchange() -> tuple[bytes, tuple[str, int]]:
            sock.sendto(request, (host, port))
            return sock.recvfrom(4096)

        data, addr = await asyncio.to_thread(_exchange)
        text = data.decode(errors="ignore")
        first_line = text.splitlines()[0] if text.splitlines() else ""

        code = None
        parts = first_line.split(" ")
        if len(parts) >= 2 and parts[0].startswith("SIP/2.0"):
            try:
                code = int(parts[1])
            except ValueError:
                code = None

        if code is None:
            return CheckResult(
                name=name,
                passed=False,
                duration_sec=time.monotonic() - started,
                details=f"Invalid SIP response line: {first_line}",
            )

        if code not in expected_codes:
            return CheckResult(
                name=name,
                passed=False,
                duration_sec=time.monotonic() - started,
                details=f"SIP status {code} not in expected {sorted(expected_codes)}; from={addr}",
            )

        if call_id not in text:
            return CheckResult(
                name=name,
                passed=False,
                duration_sec=time.monotonic() - started,
                details=f"SIP response does not contain expected Call-ID={call_id}",
            )

        return CheckResult(
            name=name,
            passed=True,
            duration_sec=time.monotonic() - started,
            details=f"SIP status {code}; from={addr}; call_id={call_id}",
        )
    except Exception as exc:
        return CheckResult(
            name=name,
            passed=False,
            duration_sec=time.monotonic() - started,
            details=f"Exception: {exc}",
        )
    finally:
        sock.close()


class RtpProtocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.transport: asyncio.DatagramTransport | None = None
        self.sent_packets = 0
        self.recv_packets = 0
        self.parse_errors = 0

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        del addr
        try:
            RtpPacket.from_bytes(data)
        except ValueError:
            self.parse_errors += 1
            return
        self.recv_packets += 1

    def send_packet(self, raw_packet: bytes, addr: tuple[str, int]) -> None:
        if self.transport is None:
            raise RuntimeError("RTP transport is not initialized")
        self.transport.sendto(raw_packet, addr)
        self.sent_packets += 1


async def run_rtp_check(config: dict[str, Any], session: SessionContext) -> CheckResult:
    name = "RTP"
    started = time.monotonic()
    if not config.get("enabled", True):
        return CheckResult(name=name, passed=True, duration_sec=0.0, details="Skipped (disabled)")

    local_host = str(config.get("local_host", "0.0.0.0"))
    local_port = int(config["local_port"])
    remote_host = str(config.get("remote_host", session.metadata.get("rtp_remote_host", "")))
    remote_port = int(config.get("remote_port", session.metadata.get("rtp_remote_port", 0)))
    duration_sec = float(config.get("duration_sec", 5.0))
    frame_ms = int(config.get("frame_ms", 20))
    payload_type = int(config.get("payload_type", 0))
    min_received = int(config.get("min_received_packets", 1))

    protocol = RtpProtocol()
    loop = asyncio.get_running_loop()

    transport, _ = await loop.create_datagram_endpoint(
        lambda: protocol,
        local_addr=(local_host, local_port),
    )

    frame_samples = int(RTP_CLOCK_RATE * frame_ms / 1000.0)
    remote_addr = (remote_host, remote_port)
    sequence = random.randint(0, 65535)
    timestamp = random.randint(0, 2**32 - 1)
    if not remote_host or remote_port == 0:
        return CheckResult(
            name=name,
            passed=False,
            duration_sec=time.monotonic() - started,
            details="RTP remote endpoint is not configured and was not provided by WebRTC metadata",
        )

    ssrc = (abs(hash(session.call_id)) % (2**32 - 1)) + 1

    try:
        deadline = time.monotonic() + duration_sec
        frame_idx = 0
        while time.monotonic() < deadline:
            payload = generate_payload(frame_samples=frame_samples, frame_idx=frame_idx)
            packet = RtpPacket(
                payload_type=payload_type,
                sequence=sequence,
                timestamp=timestamp,
                ssrc=ssrc,
                marker=1 if frame_idx == 0 else 0,
                payload=payload,
            )
            protocol.send_packet(packet.to_bytes(), remote_addr)

            frame_idx += 1
            sequence = (sequence + 1) & 0xFFFF
            timestamp = (timestamp + frame_samples) & 0xFFFFFFFF
            await asyncio.sleep(frame_ms / 1000.0)

        if protocol.recv_packets < min_received:
            return CheckResult(
                name=name,
                passed=False,
                duration_sec=time.monotonic() - started,
                details=(
                    f"Received RTP packets {protocol.recv_packets}, required >= {min_received}; "
                    f"sent={protocol.sent_packets}; parse_errors={protocol.parse_errors}"
                ),
            )

        return CheckResult(
            name=name,
            passed=True,
            duration_sec=time.monotonic() - started,
                details=(
                    f"RTP ok: sent={protocol.sent_packets}, recv={protocol.recv_packets}, "
                    f"parse_errors={protocol.parse_errors}; call_id={session.call_id}; ssrc={ssrc}"
                ),
            )
    except Exception as exc:
        return CheckResult(
            name=name,
            passed=False,
            duration_sec=time.monotonic() - started,
            details=f"Exception: {exc}",
        )
    finally:
        transport.close()


def build_junit_report(results: list[CheckResult], output_path: str, session: SessionContext) -> None:
    tests = len(results)
    failures = sum(1 for item in results if not item.passed)
    skipped = sum(1 for item in results if item.details.startswith("Skipped"))
    total_time = sum(item.duration_sec for item in results)

    suite = ET.Element(
        "testsuite",
        {
            "name": "voip-autotest",
            "tests": str(tests),
            "failures": str(failures),
            "errors": "0",
            "skipped": str(skipped),
            "time": f"{total_time:.3f}",
        },
    )
    properties = ET.SubElement(suite, "properties")
    ET.SubElement(properties, "property", {"name": "call_id", "value": session.call_id})
    ET.SubElement(properties, "property", {"name": "single_session", "value": "true"})

    for item in results:
        case = ET.SubElement(
            suite,
            "testcase",
            {
                "classname": "voip",
                "name": item.name,
                "time": f"{item.duration_sec:.3f}",
            },
        )
        if item.details:
            out = ET.SubElement(case, "system-out")
            out.text = item.details
        if not item.passed and not item.details.startswith("Skipped"):
            failure = ET.SubElement(case, "failure", {"message": item.details or "failed"})
            failure.text = item.details or "failed"
        if item.details.startswith("Skipped"):
            ET.SubElement(case, "skipped")

    tree = ET.ElementTree(suite)
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    tree.write(output_path, encoding="utf-8", xml_declaration=True)


async def run_checks(config: dict[str, Any], session: SessionContext) -> list[CheckResult]:
    results: list[CheckResult] = []
    results.append(await run_webrtc_signaling_check(config.get("webrtc", {}), session))
    results.append(await run_sip_check(config.get("sip", {}), session))
    results.append(await run_rtp_check(config.get("rtp", {}), session))
    return results


def print_summary(results: list[CheckResult], junit_path: str) -> int:
    failed = [r for r in results if not r.passed and not r.details.startswith("Skipped")]
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        print(f"[{status}] {result.name}: {result.details}")
    print(f"JUnit report: {junit_path}")
    return 1 if failed else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="VoIP autotest runner")
    parser.add_argument("--config", required=True, help="Path to YAML config")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    call_id = str(
        config.get("session", {}).get(
            "call_id",
            f"voip-autotest-{uuid.uuid4()}@{socket.gethostname()}",
        )
    )
    session = SessionContext(call_id=call_id, started_at=time.monotonic(), metadata={})
    results = asyncio.run(run_checks(config, session))

    report_path = str(config.get("report", {}).get("junit_xml", "./artifacts/voip-autotest.junit.xml"))
    build_junit_report(results=results, output_path=report_path, session=session)
    return print_summary(results=results, junit_path=report_path)


if __name__ == "__main__":
    raise SystemExit(main())
