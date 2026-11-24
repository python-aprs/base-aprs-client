# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "ax253 @ git+https://github.com/python-aprs/ax253@main",
#     "kiss3 @ git+https://github.com/python-aprs/kiss3@main",
#     "aprs3 @ git+https://github.com/python-aprs/aprs3@main",
#     "base-aprs-client[db] @ git+https://github.com/python-aprs/base-aprs-client@786676b80dbbc7544c3aef14bdb0b33bb0efa140",
#     "psutil",
# ]
# ///
"""
Stress test base-aprs-client and aprs3 against the APRS-IS full feed.

Receive only: one connection, passcode -1, nothing is transmitted.

    uv run firehose.py --call PYAPRS-1 --minutes 30          # live full feed
    uv run firehose.py --replay out/corpus.txt.gz            # max-speed replay

Every line is decoded exactly as the client decodes it (APRSTNC2Decode), and
the client logs every frame to a PacketStore (SQLite via aiosqlite). Writes
``out/stats.jsonl`` (one line per interval), ``out/summary.json`` and, for a
live run, ``out/corpus.txt.gz`` (every line heard, for replay).
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import gzip
import json
import logging
import os
import re
import sys
import time
import traceback
from pathlib import Path

import attr
import psutil
from aprs import APRSFrame, InformationField
from aprs.mic_e import MicEReport
from aprs.aprsis import APRSISProtocol, APRSTNC2Decode, create_aprsis_connection
from base_aprs_client import AsyncClient
from base_aprs_client.store import PacketStore
from sqlalchemy import func, select

from base_aprs_client.store import PacketRecord

MAX_SAMPLES = 5
PROC = psutil.Process()


def _norm(msg: str) -> str:
    """An exception message with its variable parts blanked, to group by."""
    msg = re.sub(r"b'.*?'|b\".*?\"|'.*?'", "'…'", msg)
    msg = re.sub(r"\d+(\.\d+)?", "#", msg)
    return msg[:120]


def _info_part(line: bytes) -> bytes:
    return line.partition(b":")[2]


class Stats:
    def __init__(self) -> None:
        self.lines = 0
        self.comments = 0
        self.decoded = 0
        self.failed = 0
        self.non_ascii = 0
        self.decode_seconds = 0.0
        self.slowest: list[tuple[float, str]] = []
        self.classes: collections.Counter[str] = collections.Counter()
        self.raw_fallback: collections.Counter[str] = collections.Counter()
        self.raw_samples: dict[str, list[str]] = collections.defaultdict(list)
        self.failures: collections.Counter[tuple] = collections.Counter()
        self.failure_samples: dict[tuple, list[dict]] = collections.defaultdict(list)
        self.reencode_mismatch: collections.Counter[str] = collections.Counter()
        self.reencode_lossy: collections.Counter[str] = collections.Counter()
        self.reencode_errors: collections.Counter[str] = collections.Counter()
        self.reencode_samples: dict[str, list[dict]] = collections.defaultdict(list)
        self.tnc2_mismatch = 0
        self.tnc2_samples: list[dict] = []
        self.log_records: collections.Counter[str] = collections.Counter()
        self.log_samples: dict[str, list[str]] = collections.defaultdict(list)
        self.delivered = 0
        self.max_loop_lag = 0.0
        self.max_backlog = 0

    @staticmethod
    def _sample(store: dict, key, value) -> None:
        if len(store[key]) < MAX_SAMPLES:
            store[key].append(value)


class FirehoseDecode(APRSTNC2Decode):
    """APRSTNC2Decode, instrumented: same decode, plus counting and checks."""

    stats: Stats
    corpus = None
    check = True

    def _decode_line(self, packet: bytes):
        s = self.stats
        s.lines += 1
        if self.corpus is not None:
            self.corpus.write(packet + b"\n")
        if packet.strip().startswith(b"#"):
            s.comments += 1
        yield from super()._decode_line(packet)

    def decode_frames(self, frame: bytes):  # type: ignore[override]
        s = self.stats
        if any(b > 0x7F for b in frame):
            s.non_ascii += 1
        t0 = time.perf_counter()
        try:
            # exactly what APRSTNC2Decode.decode_frames does
            decoded = APRSFrame.from_str(frame.decode("latin1"))
        except Exception as exc:  # noqa: BLE001
            elapsed = time.perf_counter() - t0
            s.decode_seconds += elapsed
            s.failed += 1
            info = _info_part(frame)
            dt = info[:1].decode("latin1") or "<empty>"
            where = traceback.extract_tb(exc.__traceback__)[-1]
            key = (
                dt,
                type(exc).__name__,
                _norm(str(exc)),
                f"{Path(where.filename).name}:{where.name}",
            )
            s.failures[key] += 1
            Stats._sample(
                s.failure_samples, key, frame.decode("latin1", "replace")
            )
            return
        elapsed = time.perf_counter() - t0
        s.decode_seconds += elapsed
        if len(s.slowest) < 10 or elapsed > s.slowest[0][0]:
            s.slowest.append((elapsed, frame.decode("latin1", "replace")))
            s.slowest.sort()
            del s.slowest[:-10]
        s.decoded += 1
        info = decoded.info
        name = type(info).__name__
        s.classes[name] += 1
        if type(info) is InformationField:
            dt = info.data_type
            if dt is None:
                first = info.raw[:1].decode("latin1") or "<empty>"
                key = f"unknown data type {first!r}"
            else:
                key = f"{dt.name} ({dt.value.decode()!r})"
            s.raw_fallback[key] += 1
            Stats._sample(s.raw_samples, key, frame.decode("latin1", "replace"))
        if self.check:
            self._check(decoded, frame, name)
        yield decoded

    def _check(self, decoded: APRSFrame, line: bytes, name: str) -> None:
        """Encode the decoded fields again (not the kept source bytes)."""
        s = self.stats
        info = decoded.info
        try:
            if type(info).__bytes__ is InformationField.__bytes__:
                encoded = info._encode()
            else:  # newer report types: evolve drops the kept source bytes
                encoded = bytes(attr.evolve(info))
        except Exception as exc:  # noqa: BLE001
            key = f"{name}: {type(exc).__name__}: {_norm(str(exc))}"
            s.reencode_errors[key] += 1
            Stats._sample(s.reencode_samples, key, {"line": line.decode("latin1")})
            return
        if info.raw and encoded != info.raw:
            s.reencode_mismatch[name] += 1
            # a different spelling is fine; losing or changing a value isn't
            lossy = False
            if not isinstance(info, MicEReport):
                try:
                    again = InformationField.from_bytes(encoded)
                    lossy = type(again) is not type(info) or attr.evolve(
                        again, raw=info.raw
                    ) != attr.evolve(info, raw=info.raw)
                except Exception:  # noqa: BLE001
                    lossy = True
            if lossy:
                s.reencode_lossy[name] += 1
                Stats._sample(
                    s.reencode_samples,
                    name,
                    {
                        "heard": info.raw.decode("latin1"),
                        "encoded": encoded.decode("latin1"),
                    },
                )
        tnc2 = str(decoded).encode("utf-8", "surrogateescape")
        if tnc2 != line:
            s.tnc2_mismatch += 1
            if len(s.tnc2_samples) < MAX_SAMPLES * 4:
                s.tnc2_samples.append(
                    {"heard": line.decode("latin1"), "str(frame)": tnc2.decode("latin1")}
                )


class CountingHandler(logging.Handler):
    def __init__(self, stats: Stats) -> None:
        super().__init__(logging.INFO)
        self.stats = stats

    def emit(self, record: logging.LogRecord) -> None:
        key = f"{record.name} {record.levelname}: {_norm(str(record.msg))[:80]}"
        self.stats.log_records[key] += 1
        if len(self.stats.log_samples[key]) < 3:
            self.stats.log_samples[key].append(record.getMessage()[:400])


async def replay_server(path: Path, port: int, rate: float = 0) -> asyncio.base_events.Server:
    lines = gzip.open(path, "rb").read() if path.suffix == ".gz" else path.read_bytes()
    lines = lines.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")

    async def serve(reader, writer):
        await reader.readline()  # login
        writer.write(b"# aprsc replay\r\n# logresp PYAPRS unverified, server REPLAY\r\n")
        await asyncio.sleep(2)  # let the client start before the flood
        if rate:
            # paced: rate lines per second, in 20 ms slices
            split = lines.split(b"\r\n")
            step = max(1, int(rate / 50))
            t0 = time.monotonic()
            for n, i in enumerate(range(0, len(split), step)):
                writer.write(b"".join(l + b"\r\n" for l in split[i : i + step]))
                await writer.drain()
                await asyncio.sleep(max(0, t0 + (n + 1) / 50 - time.monotonic()))
        else:
            for i in range(0, len(lines), 64 * 1024):
                writer.write(lines[i : i + 64 * 1024])
                await writer.drain()
        await writer.drain()
        await asyncio.sleep(1)
        writer.close()

    return await asyncio.start_server(serve, "127.0.0.1", port)


async def loop_lag(stats: Stats) -> None:
    loop = asyncio.get_running_loop()
    while True:
        t0 = loop.time()
        await asyncio.sleep(0.1)
        stats.max_loop_lag = max(stats.max_loop_lag, loop.time() - t0 - 0.1)


async def db_rows(store: PacketStore) -> int:
    async with store.session() as session:
        return (await session.execute(select(func.max(PacketRecord.id)))).scalar() or 0


async def main(args: argparse.Namespace) -> None:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stats = Stats()
    handler = CountingHandler(stats)
    for name in ("aprs", "ax253", "kiss", "base_aprs_client"):
        lg = logging.getLogger(name)
        lg.addHandler(handler)
        lg.setLevel(logging.INFO)
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(message)s")

    FirehoseDecode.stats = stats
    FirehoseDecode.check = not args.no_check
    corpus = None
    if args.replay:
        port = 14999
        server = await replay_server(Path(args.replay), port, args.rate)
        host = "127.0.0.1"
    else:
        server = None
        host, port = args.host, args.port
        corpus = gzip.open(out / "corpus.txt.gz", "wb", compresslevel=3)
        FirehoseDecode.corpus = corpus

    db = out / "firehose.db"
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    store = PacketStore(f"sqlite+aiosqlite:///{db}")

    t_start = time.monotonic()
    _, protocol = await create_aprsis_connection(
        host=host,
        port=port,
        user=args.call,
        passcode="-1",
        protocol_kwargs={"decoder": FirehoseDecode()},
        login_timeout=15,
    )
    print(f"login: {protocol.login_response}", flush=True)
    client = AsyncClient(mycall=args.call, store=store, reconnect=None)
    await client.start(protocol)
    lag_task = asyncio.create_task(loop_lag(stats))

    async def consume():
        async for _ in client:
            stats.delivered += 1

    consumer = asyncio.create_task(consume())
    deadline = t_start + args.minutes * 60
    stats_file = (out / "stats.jsonl").open("w")
    last_lines, last_t, last_cpu = 0, t_start, PROC.cpu_times()
    try:
        while time.monotonic() < deadline and not consumer.done():
            await asyncio.wait({consumer}, timeout=args.interval)
            now = time.monotonic()
            cpu = PROC.cpu_times()
            backlog = len(client._unrecorded) if client._unrecorded is not None else 0
            stats.max_backlog = max(stats.max_backlog, backlog)
            row = {
                "t": round(now - t_start, 1),
                "lines": stats.lines,
                "pps": round((stats.lines - last_lines) / (now - last_t), 1),
                "cpu_pct": round(
                    100
                    * ((cpu.user + cpu.system) - (last_cpu.user + last_cpu.system))
                    / (now - last_t),
                    1,
                ),
                "failed": stats.failed,
                "raw": sum(stats.raw_fallback.values()),
                "rows": await db_rows(store),
                "backlog": backlog,
                "unlogged": client.unlogged_frames,
                "dropped": client.dropped_frames,
                "proto_dropped": protocol.dropped_frames,
                "rss_mb": round(PROC.memory_info().rss / 2**20, 1),
                "loop_lag_ms": round(stats.max_loop_lag * 1000, 1),
            }
            stats.max_loop_lag_interval = stats.max_loop_lag
            stats.max_loop_lag = 0.0
            stats_file.write(json.dumps(row) + "\n")
            stats_file.flush()
            print(json.dumps(row), flush=True)
            last_lines, last_t, last_cpu = stats.lines, now, cpu
    finally:
        elapsed = time.monotonic() - t_start
        t_close = time.monotonic()
        await client.close()
        drain_s = time.monotonic() - t_close
        lag_task.cancel()
        consumer.cancel()
        rows = await db_rows(store)
        await store.close()
        if corpus is not None:
            corpus.close()
        if server is not None:
            server.close()
        stats_file.close()

    s = stats
    summary = {
        "mode": f"replay rate={args.rate or 'max'}" if args.replay else f"live {host}:{port}",
        "elapsed_s": round(elapsed, 1),
        "lines": s.lines,
        "comments": s.comments,
        "packets": s.lines - s.comments,
        "avg_pps": round(s.lines / elapsed, 1),
        "decoded": s.decoded,
        "failed": s.failed,
        "failed_pct": round(100 * s.failed / max(1, s.lines - s.comments), 3),
        "raw_fallback": sum(s.raw_fallback.values()),
        "non_ascii_lines": s.non_ascii,
        "decode_us_avg": round(1e6 * s.decode_seconds / max(1, s.decoded + s.failed), 1),
        "decode_cpu_s": round(s.decode_seconds, 2),
        "delivered": s.delivered,
        "db_rows": rows,
        "db_mb": round(db.stat().st_size / 2**20, 1),
        "store_max_backlog": s.max_backlog,
        "unlogged_frames": client.unlogged_frames,
        "protocol_dropped_frames": protocol.dropped_frames,
        "close_drain_s": round(drain_s, 2),
        "rss_mb_end": round(PROC.memory_info().rss / 2**20, 1),
        "classes": s.classes.most_common(),
        "raw_fallback_by_type": s.raw_fallback.most_common(),
        "raw_samples": dict(s.raw_samples),
        "failures": [
            {
                "data_type": k[0],
                "exception": k[1],
                "message": k[2],
                "raised_in": k[3],
                "count": n,
                "samples": s.failure_samples[k],
            }
            for k, n in s.failures.most_common()
        ],
        "reencode_mismatch": s.reencode_mismatch.most_common(),
        "reencode_lossy": s.reencode_lossy.most_common(),
        "reencode_errors": s.reencode_errors.most_common(),
        "reencode_samples": dict(s.reencode_samples),
        "tnc2_mismatch": s.tnc2_mismatch,
        "tnc2_samples": s.tnc2_samples,
        "log_records": s.log_records.most_common(),
        "log_samples": dict(s.log_samples),
        "slowest_decodes_us": [(round(t * 1e6), line) for t, line in reversed(s.slowest)],
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False))
    brief = {k: v for k, v in summary.items() if not isinstance(v, (list, dict))}
    print(json.dumps(brief, indent=1))
    print(f"full summary: {out / 'summary.json'}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("--call", default="PYAPRS-1", help="login callsign (receive only)")
    p.add_argument("--host", default="rotate.aprs2.net")
    p.add_argument("--port", type=int, default=10152, help="10152: full feed")
    p.add_argument("--minutes", type=float, default=30)
    p.add_argument("--interval", type=float, default=10, help="seconds between stats")
    p.add_argument("--replay", help="replay a corpus at full speed instead")
    p.add_argument("--rate", type=float, default=0, help="replay lines/s (0: max)")
    p.add_argument("--no-check", action="store_true", help="skip re-encode checks")
    p.add_argument("--out", default="out")
    try:
        asyncio.run(main(p.parse_args()))
    except KeyboardInterrupt:
        sys.exit(130)
