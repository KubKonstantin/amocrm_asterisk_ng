#!/usr/bin/env python3
"""Minimal RTP exchange utility for end-to-end media checks.

The script can run in two modes:
* peer mode (default): sends RTP packets and receives remote RTP packets.
* echo mode: sends RTP packets and echoes every received RTP packet back.

It is intended for smoke tests and lab diagnostics where a lightweight RTP
traffic generator is needed without external dependencies.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import math
import random
import signal
import struct
import time
from dataclasses import dataclass

RTP_HEADER_SIZE = 12
PCMU_PAYLOAD_TYPE = 0
DEFAULT_CLOCK_RATE = 8000


@dataclass(frozen=True)
class RtpPacket:
    payload_type: int
    sequence: int
    timestamp: int
    ssrc: int
    marker: int
    payload: bytes

    def to_bytes(self) -> bytes:
        first_byte = 0x80  # Version 2, no padding, no extension, CC=0
        second_byte = ((self.marker & 0x01) << 7) | (self.payload_type & 0x7F)
        header = struct.pack(
            "!BBHII",
            first_byte,
            second_byte,
            self.sequence & 0xFFFF,
            self.timestamp & 0xFFFFFFFF,
            self.ssrc & 0xFFFFFFFF,
        )
        return header + self.payload

    @classmethod
    def from_bytes(cls, packet_data: bytes) -> "RtpPacket":
        if len(packet_data) < RTP_HEADER_SIZE:
            raise ValueError("RTP packet is too short")

        first_byte, second_byte, sequence, timestamp, ssrc = struct.unpack(
            "!BBHII", packet_data[:RTP_HEADER_SIZE]
        )

        version = first_byte >> 6
        if version != 2:
            raise ValueError(f"Unsupported RTP version: {version}")

        marker = (second_byte >> 7) & 0x01
        payload_type = second_byte & 0x7F
        payload = packet_data[RTP_HEADER_SIZE:]

        return cls(
            payload_type=payload_type,
            sequence=sequence,
            timestamp=timestamp,
            ssrc=ssrc,
            marker=marker,
            payload=payload,
        )


def generate_pcmu_tone(frame_samples: int, frame_index: int, frequency: float = 440.0) -> bytes:
    """Generate deterministic pseudo PCMU-like payload for diagnostics.

    Real G.711 μ-law encoding is not required for transport checks; we generate
    bounded bytes with stable phase progression to make payload non-trivial.
    """

    frame = bytearray(frame_samples)
    for i in range(frame_samples):
        sample_no = frame_index * frame_samples + i
        value = math.sin(2.0 * math.pi * frequency * sample_no / DEFAULT_CLOCK_RATE)
        frame[i] = int((value + 1.0) * 127.5) & 0xFF
    return bytes(frame)


class RtpExchangeProtocol(asyncio.DatagramProtocol):
    def __init__(self, echo: bool = False) -> None:
        self.echo = echo
        self.transport: asyncio.DatagramTransport | None = None
        self.sent_packets = 0
        self.recv_packets = 0
        self.parse_errors = 0
        self.last_sequence: int | None = None
        self.remote_addr: tuple[str, int] | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.remote_addr = addr
        try:
            packet = RtpPacket.from_bytes(data)
        except ValueError:
            self.parse_errors += 1
            return

        self.recv_packets += 1
        self.last_sequence = packet.sequence

        if self.echo and self.transport:
            self.transport.sendto(data, addr)
            self.sent_packets += 1

    def send_packet(self, packet: RtpPacket, addr: tuple[str, int]) -> None:
        if not self.transport:
            raise RuntimeError("Transport is not initialized")
        self.transport.sendto(packet.to_bytes(), addr)
        self.sent_packets += 1


async def run_exchange(args: argparse.Namespace) -> None:
    loop = asyncio.get_running_loop()
    protocol = RtpExchangeProtocol(echo=args.echo)

    transport, _ = await loop.create_datagram_endpoint(
        lambda: protocol,
        local_addr=(args.local_host, args.local_port),
    )

    remote_addr = (args.remote_host, args.remote_port)
    stop_event = asyncio.Event()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    async def sender() -> None:
        if args.echo:
            return

        sequence = random.randint(0, 65535)
        timestamp = random.randint(0, 2**32 - 1)
        ssrc = random.randint(1, 2**32 - 1)
        frame_interval = args.frame_ms / 1000.0
        frame_samples = int(DEFAULT_CLOCK_RATE * frame_interval)

        started_at = time.monotonic()
        frame_index = 0
        while not stop_event.is_set():
            if args.duration > 0 and (time.monotonic() - started_at) >= args.duration:
                stop_event.set()
                break

            payload = generate_pcmu_tone(frame_samples=frame_samples, frame_index=frame_index)
            packet = RtpPacket(
                payload_type=args.payload_type,
                sequence=sequence,
                timestamp=timestamp,
                ssrc=ssrc,
                marker=1 if frame_index == 0 else 0,
                payload=payload,
            )
            protocol.send_packet(packet, remote_addr)

            frame_index += 1
            sequence = (sequence + 1) & 0xFFFF
            timestamp = (timestamp + frame_samples) & 0xFFFFFFFF

            await asyncio.sleep(frame_interval)

    sender_task = asyncio.create_task(sender())
    await stop_event.wait()

    sender_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await sender_task

    transport.close()

    print(
        "RTP summary: "
        f"sent={protocol.sent_packets}, "
        f"received={protocol.recv_packets}, "
        f"parse_errors={protocol.parse_errors}, "
        f"last_sequence={protocol.last_sequence}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="RTP media exchange utility")
    parser.add_argument("--local-host", default="0.0.0.0", help="Local bind host")
    parser.add_argument("--local-port", type=int, required=True, help="Local bind UDP port")
    parser.add_argument("--remote-host", default="127.0.0.1", help="Remote RTP host")
    parser.add_argument("--remote-port", type=int, required=True, help="Remote RTP UDP port")
    parser.add_argument("--duration", type=float, default=15.0, help="Exchange duration in seconds")
    parser.add_argument("--frame-ms", type=int, default=20, help="Frame interval in milliseconds")
    parser.add_argument("--payload-type", type=int, default=PCMU_PAYLOAD_TYPE, help="RTP payload type")
    parser.add_argument("--echo", action="store_true", help="Echo received RTP packets back")
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(run_exchange(parse_args()))
