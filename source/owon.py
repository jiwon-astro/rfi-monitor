"""OWON HSA1000 SCPI socket transport; no VISA or platform-specific DLLs."""
from __future__ import annotations

import socket
import time


class ProtocolError(RuntimeError):
    pass


class Owon:
    def __init__(self, host="192.168.40.230", port=1015, timeout=8.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.buffer = bytearray()

    def close(self):
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def write(self, command):
        if "\n" in command or "\r" in command:
            raise ValueError("Only one SCPI command per transaction")
        self.sock.sendall((command + "\n").encode("ascii"))

    def _take(self, n):
        while len(self.buffer) < n:
            data = self.sock.recv(max(4096, n - len(self.buffer)))
            if not data:
                raise ProtocolError("Instrument closed the socket")
            self.buffer.extend(data)
        result = bytes(self.buffer[:n])
        del self.buffer[:n]
        return result

    def query_bytes(self, command, limit=16_000_000):
        self.write(command)
        first = self._take(1)
        # A block's optional CR/LF can arrive in a separate TCP packet.
        while first in (b"\r", b"\n"):
            first = self._take(1)
        if first == b"#":
            digits = self._take(1)
            if digits not in b"123456789":
                raise ProtocolError(f"Invalid SCPI block header: {digits!r}")
            field = self._take(int(digits))
            if not field.isdigit():
                raise ProtocolError(f"Invalid SCPI block length: {field!r}")
            size = int(field)
            if size > limit:
                raise ProtocolError(f"SCPI payload too large: {size}")
            return self._take(size)
        result = bytearray(first)
        while not result.endswith(b"\n"):
            if len(result) > limit:
                raise ProtocolError("Text response too large")
            result.extend(self._take(1))
        return bytes(result).strip()

    def query(self, command):
        return self.query_bytes(command).decode("ascii").strip()

    def trace(self):
        """V3.0.2.0: binary header is correct; ASCII length is off by one."""
        import numpy as np
        payload = self.query_bytes(":TRAC:SOCK? TRACE1")
        if len(payload) < 8 or len(payload) % 4:
            raise ProtocolError(f"Invalid float32 trace length: {len(payload)}")
        result = np.frombuffer(payload, dtype=">f4").astype("float32")
        if not np.isfinite(result).all() or (result < -300).any() or (result > 100).any():
            raise ProtocolError("Non-finite/out-of-range binary trace")
        return result


if __name__ == "__main__":
    import argparse
    import json
    parser = argparse.ArgumentParser(description="Read-only OWON probe")
    parser.add_argument("--host", default="192.168.40.230")
    parser.add_argument("--port", default=1015, type=int)
    parser.add_argument("--query", action="append")
    args = parser.parse_args()
    for command in args.query or ["*IDN?", ":FREQ:STAR?", ":FREQ:STOP?",
            ":BAND?", ":BAND:VID?", ":SWE:TIME?", ":INIT:CONT?",
            ":TRAC1:MODE?", ":TRAC1:DET?", ":POW:ATT?",
            ":POW:ATT:AUTO?", ":POW:GAIN:AUTO?", ":DISP:WIN:Y:RLEV?",
            ":BAND:EMC:STAT?", ":OUTP:TRAC?", ":TRAC1:READy?", ":TRAC1:DATA?"]:
        started = time.monotonic()
        try:
            # Isolate malformed/unsupported replies while discovering firmware.
            with Owon(args.host, args.port) as device:
                response = device.query_bytes(command)
            print(json.dumps({"query": command, "seconds": time.monotonic()-started,
                              "bytes": len(response), "reply": repr(response[:250])}), flush=True)
        except Exception as exc:
            print(json.dumps({"query": command, "error": str(exc)}), flush=True)
