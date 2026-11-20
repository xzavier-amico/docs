#!/usr/bin/env python3
"""Bounded HTTP/3 loopback lab; requires aioquic==1.3.0 and openssl."""

import socket
import subprocess
import tempfile
import time
from pathlib import Path

from aioquic.buffer import Buffer
from aioquic.h3.connection import H3_ALPN, H3Connection
from aioquic.h3.events import DataReceived, HeadersReceived
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.connection import QuicConnection
from aioquic.quic.events import HandshakeCompleted
from aioquic.quic.logger import QuicLogger
from aioquic.quic.packet import pull_quic_header


def main():
    with tempfile.TemporaryDirectory() as directory, socket.socket(
        socket.AF_INET, socket.SOCK_DGRAM
    ) as client_socket, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server_socket:
        for sock in (client_socket, server_socket):
            sock.bind(("127.0.0.1", 0))
            sock.settimeout(0.25)
        cert, key = Path(directory) / "cert.pem", Path(directory) / "key.pem"
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt",
             "ec_paramgen_curve:P-256", "-nodes", "-days", "1", "-subj",
             "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
             "-out", str(cert), "-keyout", str(key)],
            check=True, timeout=3, capture_output=True,
        )
        lab(client_socket, server_socket, cert, key)


def lab(cs, ss, cert, key):
    cl, sl = QuicLogger(), QuicLogger()
    cc = QuicConfiguration(is_client=True, alpn_protocols=H3_ALPN,
                           server_name="localhost", quic_logger=cl)
    cc.load_verify_locations(cafile=str(cert))
    sc = QuicConfiguration(is_client=False, alpn_protocols=H3_ALPN, quic_logger=sl)
    sc.load_cert_chain(str(cert), str(key))
    ca, sa = cs.getsockname(), ss.getsockname()
    c = QuicConnection(configuration=cc)
    # Use virtual time to avoid introducing real-path jitter or triggering PTO.
    # The bytes nonetheless cross real loopback UDP sockets.
    now = time.monotonic()
    c.connect(sa, now=now)
    initial = c.datagrams_to_send(now=now)
    header = pull_quic_header(Buffer(data=initial[0][0]), host_cid_length=8)
    s = QuicConnection(configuration=sc, original_destination_connection_id=header.destination_cid)
    ch, sh = H3Connection(c), H3Connection(s)

    def deliver(datagrams, src, dst, expected_peer, receiver):
        for data, addr in datagrams:
            assert addr == dst.getsockname()
            src.sendto(data, addr)
            received, peer = dst.recvfrom(65536)
            assert (received, peer) == (data, expected_peer)
            receiver.receive_datagram(received, peer, now=now)

    requests, outputs, completed, negotiated = set(), {0: b"", 4: b""}, set(), {}
    statuses = {}

    def drain(side, connection, http):
        while (ev := connection.next_event()) is not None:
            if isinstance(ev, HandshakeCompleted):
                negotiated[side] = ev.alpn_protocol
            for item in http.handle_event(ev):
                if side == "server" and isinstance(item, HeadersReceived):
                    requests.add(item.stream_id)
                elif side == "client" and isinstance(item, HeadersReceived):
                    statuses[item.stream_id] = dict(item.headers).get(b":status")
                elif side == "client" and isinstance(item, DataReceived):
                    outputs[item.stream_id] += item.data
                    if item.stream_ended:
                        completed.add(item.stream_id)

    def step():
        nonlocal now
        now += 0.002
        deliver(c.datagrams_to_send(now=now), cs, ss, ca, s)
        drain("server", s, sh)
        deliver(s.datagrams_to_send(now=now), ss, cs, sa, c)
        drain("client", c, ch)

    def server_only():
        nonlocal now
        now += 0.002
        deliver(s.datagrams_to_send(now=now), ss, cs, sa, c)
        drain("client", c, ch)

    def sent(log):
        return [e["data"] for trace in log.to_dict()["traces"] for e in trace["events"]
                if e["name"] == "transport:packet_sent"]

    deliver(initial, cs, ss, ca, s)
    drain("server", s, sh)
    for _ in range(5):
        step()
    assert negotiated == {"client": "h3", "server": "h3"}
    print("ALPN:", negotiated["client"], negotiated["server"])

    for stream in (0, 4):
        ch.send_headers(stream, [(b":method", b"GET"), (b":scheme", b"https"),
                                 (b":authority", b"localhost"),
                                 (b":path", f"/stream-{stream}".encode())], end_stream=True)
    step()
    assert requests == {0, 4}
    for stream in sorted(requests):
        sh.send_headers(stream, [(b":status", b"200")])
    step()
    assert statuses == {0: b"200", 4: b"200"}

    before = len(sent(sl))
    sh.send_data(0, b"A1", end_stream=False)
    held = s.datagrams_to_send(now=now)
    new_packets = sent(sl)[before:]
    assert len(held) == len(new_packets) == 1
    frames = [f for f in new_packets[0]["frames"] if f["frame_type"] == "stream"]
    assert len(frames) == 1 and frames[0]["stream_id"] == 0
    # Hold back client ACK delivery during the short reordering window as well,
    # so loss recovery cannot fill the deliberate gap before the observation.
    sh.send_data(0, b"A2", end_stream=True)
    server_only()
    sh.send_data(4, b"B", end_stream=True)
    server_only()
    stream_frames = [(f["stream_id"], f["offset"], f["length"])
                     for p in sent(sl)[before:] for f in p["frames"] if f["frame_type"] == "stream"]
    assert [sid for sid, _, _ in stream_frames] == [0, 0, 4], stream_frames
    received_streams = {
        (f["stream_id"], f["offset"], f["length"])
        for trace in cl.to_dict()["traces"] for ev in trace["events"]
        if ev["name"] == "transport:packet_received"
        for f in ev["data"]["frames"] if f["frame_type"] == "stream"
    }
    assert stream_frames[0] not in received_streams and set(stream_frames[1:]) <= received_streams
    print("server STREAM (id, offset, length):", stream_frames)
    assert completed == {4} and outputs == {0: b"", 4: b"B"}, (completed, outputs, sent(sl)[before:])
    print("while held:", sorted(completed), outputs)

    deliver(held, ss, cs, sa, c)
    drain("client", c, ch)
    assert completed == {0, 4} and outputs == {0: b"A1A2", 4: b"B"}
    print("after delivery:", sorted(completed), outputs)

    for label, log in (("client", cl), ("server", sl)):
        pairs = {(p["header"]["packet_type"], f["frame_type"])
                 for p in sent(log) for f in p["frames"]
                 if f["frame_type"] in {"crypto", "stream"}}
        assert {("initial", "crypto"), ("handshake", "crypto"), ("1RTT", "stream")} <= pairs
        print(label, sorted(pairs))


if __name__ == "__main__":
    main()
