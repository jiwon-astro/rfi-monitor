"""Offline OWON recorder: one durable spectrum CSV per recording session."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import shutil
import signal
import sys
import time
import uuid

import numpy as np
from clock_anchor import Clock, atomic_json
from owon import Owon, ProtocolError
from frequency_plan import PLAN_SCHEMA, make_plan, frequency_axis, segment_config
NATIVE_POINTS = 801  # Verified trace interface, NOT a claim about ADC samples.
AXIS_SCHEMA = 'native801_single_v1'

HERE = Path(__file__).resolve().parent
LOG = logging.getLogger("owon-rfi")
STORAGE_FORMAT = "single_csv_v1"
FIELDS = {
    "idn": "*IDN?", "start_hz": ":FREQ:STAR?", "stop_hz": ":FREQ:STOP?",
    "rbw_hz": ":BAND?", "rbw_auto": ":BAND:AUTO?", "vbw_hz": ":BAND:VID?",
    "vbw_auto": ":BAND:VID:AUTO?", "sweep_ms": ":SWE:TIME?", "sweep_auto": ":SWE:TIME:AUTO?",
    "continuous": ":INIT:CONT?", "trace_mode": ":TRAC1:MODE?",
    "detector": ":TRAC1:DET?", "attenuation_db": ":POW:ATT?",
    "attenuation_auto": ":POW:ATT:AUTO?", "preamp": ":POW:GAIN:AUTO?",
    "reference_dbm": ":DISP:WIN:Y:RLEV?", "emi": ":BAND:EMC:STAT?",
    "npoints": ":SWE:POIN?",
    "tg": ":OUTP:TRAC?", "unit": ":UNIT:POW?",
    "x_scale": ":DISP:WIN:X:SPAC?", "x_offset_hz": ":DISP:WIN:X:OFFS?",
    "y_offset_db": ":DISP:WIN:Y:RLEV:OFFS?",
}


def iso(ns):
    return datetime.fromtimestamp(ns / 1e9, timezone.utc).isoformat(timespec="milliseconds")


def create_run_directory(root, start_utc_ns):
    """UTC seconds only; refuse collisions instead of merging or overwriting."""
    name = datetime.fromtimestamp(start_utc_ns // 1_000_000_000, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = Path(root) / name
    try:
        path.mkdir()
    except FileExistsError as exc:
        raise ValueError(f"Run directory already exists: {path}. Retry with a new start second; existing data were not modified.") from exc
    return path


def settings(d):
    return {k: d.query(q) for k, q in FIELDS.items()}


def preamp_option(c):
    """Config uses integer 0/1; absent means OFF for older configs."""
    value = c.get('preamp', 0)
    if type(value) is not int or value not in (0, 1):
        raise ValueError('preamp must be integer 0 (OFF) or 1 (ON)')
    return value


def validate_config(c):
    preamp_option(c)
    if not 9000 <= c["start_hz"] < c["stop_hz"] <= 3.6e9:
        raise ValueError("Invalid frequency range for HSA1036-TG")
    if not 1 <= c["vbw_hz"] <= c["rbw_hz"] <= 1e6:
        raise ValueError("Invalid RBW/VBW")
    if not 0 <= c["attenuation_db"] <= 50 or not -80 <= c["reference_dbm"] <= 30:
        raise ValueError("Invalid attenuation/reference")
    if not 1 <= c["window_seconds"] <= 3600:
        raise ValueError("window_seconds must be 1..3600")
    if c["sweep_wait_factor"] < 2 or c["sweep_wait_margin_seconds"] < .3:
        raise ValueError("Conservative sweep wait must be >= 2 * sweep time + 0.3 s")
    if c["stable_check_seconds"] < .05:
        raise ValueError("Stable check must be >= 0.05 s")
    if c.get("npoints") is not None and (isinstance(c['npoints'], bool) or not isinstance(c["npoints"], int) or c["npoints"] < 2):
        raise ValueError("npoints must be an integer >= 2")


def require_single_sweep_config(c):
    """Validate explicit planned mode, or retain the legacy native-only guard."""
    if 'bin_size_hz' in c:
        make_plan(c)
        return
    n = c.get('npoints', NATIVE_POINTS)
    if not isinstance(n, int) or isinstance(n, bool) or n != NATIVE_POINTS:
        raise ValueError(
            f'Requested npoints={n}: arbitrary-point single sweeps are not verified '
            'on HSA1036-TG V3.0.2.0. Segmented acquisition is disabled. '
            'No data were substituted; no SWE:POIN command will be sent. '
            '801 is the verified native trace interface, not an ADC limit.')


def requested_duration(args):
    """Baseline defaults to 60 s; ordinary records still default to 24 h."""
    if args.seconds is not None:
        duration = args.seconds
    elif args.hours is not None:
        duration = args.hours * 3600
    else:
        duration = 60 if args.command == 'baseline' else 86400
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Duration must be finite and positive")
    return duration


def config_summary(c, args):
    """Requested settings, including enforced settings not exposed in JSON."""
    duration = requested_duration(args)
    baseline_mode = getattr(args, 'command', 'record') == 'baseline'
    output = Path(c["output_dir"])
    if not output.is_absolute():
        output = HERE / output
    plan = make_plan(c) if 'bin_size_hz' in c else None
    points = plan['output_npoints'] if plan else c.get("npoints", NATIVE_POINTS)
    if plan:
        df = np.diff(frequency_axis(plan))
        grid = f"{df.min()/1e6:g}..{df.max()/1e6:g} MHz"
    else:
        grid = f"{(c['stop_hz']-c['start_hz'])/(points-1)/1e6:g} MHz" if points else "read from instrument"
    pairs = [
        ("Config file", str(Path(args.config).resolve())),
        ("Site / polarization", f"{c.get('site', 'UNSET')} / {c.get('polarization', 'UNSET')}"),
        ("Instrument", f"{c['host']}:{c['port']} | expected serial {c['expected_serial']}"),
        ("Frequency", f"{c['start_hz']/1e6:g} - {c['stop_hz']/1e6:g} MHz"),
        ("Points / grid", f"{points if points else 'instrument current value'} / {grid} (not RBW)"),
        ("Native acquisition", f"{len(plan['segments'])} sequential sweeps x 801 native points; no interpolation" if plan else "One full-band native sweep; NO stitching/interpolation"),
        ("Points validation", plan['mode']+'; NO SWE:POIN writes' if plan else ("native 801 verified" if points == NATIVE_POINTS else "UNSUPPORTED: recording will stop; no automatic substitution")),
        ("RBW / VBW", f"{c['rbw_hz']/1000:g} / {c['vbw_hz']/1000:g} kHz, manual"),
        ("Reference / attenuation", f"{c['reference_dbm']:g} dBm / {c['attenuation_db']:g} dB, manual"),
        ("Internal preamp", 'ON (1)' if preamp_option(c) else 'OFF (0; default if omitted)'),
        ("Enforced acquisition", "Positive Peak | WRITE | single sweep | dBm | EMI/TG OFF"),
        ("Sweep wait", f"{c['sweep_wait_factor']:g} x reported sweep + {c['sweep_wait_margin_seconds']:g} s; stability {c['stable_check_seconds']:g} s"),
        ("New-run duration", f"{duration:g} s ({duration/3600:g} h)"),
        ("Resume", "YES: unfinished run keeps its original deadline" if args.resume else "NO: create a new run"),
        ("Storage", str(output.resolve() / '<UTC-start>' / ('baseline.csv' if baseline_mode else 'raw_sweeps.csv'))),
        ("Write policy", "ONE mean spectrum at successful completion; no raw/NPZ; mean in mW then dBm" if baseline_mode else "one CSV row per complete scan cycle, flush + fsync; NO recorder NPZ or window buffer"),
        ("Analysis window", "--seconds/--hours controls baseline; window_seconds unused" if baseline_mode else f"{c['window_seconds']:g} s preference ONLY; averaging/Max Hold computed offline"),
        ("Free-space minimum", f"{c['min_free_mb']:g} MiB"),
        ("RF chain", "50 ohm termination directly at SA input (operator confirmation required)" if baseline_mode else str(c.get('rf_chain', 'UNSET'))),
        ("Notes (verbatim)", str(c.get('notes', ''))),
        ("Calibration", "none; uncalibrated SA-input positive-peak data"),
    ]
    if baseline_mode:
        pairs += [('Baseline statistics', 'Equal-weight mean of complete Positive Peak cycles; NOT continuous RMS integration'),
                  ('Baseline timing', 'Timer starts AFTER initial configuration; final complete cycle may overrun'),
                  ('Subtraction', 'NONE; standalone reference only')]
    if plan:
        pairs += [('Requested max bin', f"{plan['requested_bin_size_hz']/1e6:g} MHz (not RBW)"),
                  ('Protection region', str(plan['protection_region_hz'])+' Hz; one sweep'),
                  ('Timing', 'Sequential, NOT simultaneous; segment UTC intervals in events.log')]
        for i, part in enumerate(plan['segments']):
            pairs.append((f'Sweep {i+1}', f"{part['start_hz']/1e6:g} - {part['stop_hz']/1e6:g} MHz | "
                          f"801 points | {part['bin_spacing_hz']/1e6:g} MHz/bin | "
                          f"keep [{part['keep_from']}:{part['keep_to']}]"))
    heading = '=== OWON 50 ohm baseline configuration ===' if baseline_mode else '=== OWON RFI recording configuration ==='
    return "\n".join([heading] +
                     [f"{key:25s}: {value}" for key, value in pairs] +
                     ["Requested settings above; instrument readback is verified on connection."])


def verify(s, c):
    if c["expected_serial"] not in [v.strip() for v in s["idn"].split(",")]:
        raise ValueError(f"Wrong instrument: {s['idn']}")
    for key in ("start_hz", "stop_hz", "rbw_hz", "vbw_hz", "reference_dbm", "attenuation_db"):
        if not math.isclose(float(s[key]), float(c[key]), rel_tol=1e-7, abs_tol=1e-3):
            raise ValueError(f"Readback mismatch {key}: {s[key]} != {c[key]}")
    for key in ("rbw_auto", "vbw_auto", "attenuation_auto", "emi", "tg", "continuous"):
        if s[key] != "0":
            raise ValueError(f"Expected {key}=0, got {s[key]}")
    if s.get('preamp') != str(preamp_option(c)):
        raise ValueError(f"Expected preamp={preamp_option(c)}, got {s.get('preamp')}")
    for key, value in (("trace_mode", "write"), ("detector", "positive"),
                       ("unit", "dbm"), ("x_scale", "linear")):
        if s[key].lower() != value:
            raise ValueError(f"Unexpected {key}: {s[key]}")
    if float(s["x_offset_hz"]) != 0 or float(s["y_offset_db"]) != 0:
        raise ValueError("Non-zero instrument display offset")
    if "npoints" in c and int(s["npoints"]) != c["npoints"]:
        raise ValueError(f"Requested {c['npoints']} points, instrument returned {s['npoints']}")
    if not 0 < float(s["sweep_ms"]) <= 3e6:
        raise ValueError("Invalid instrument sweep time")
    if s.get('sweep_auto') != '1':
        raise ValueError('Expected automatic sweep time; manual timing is not validated')


def configure_native(d, c):
    preamp = preamp_option(c)  # Reject malformed values before any device access.
    if c.get('npoints', NATIVE_POINTS) != NATIVE_POINTS:
        raise ValueError('Unsafe direct SCPI points: only native 801 is verified')
    before = settings(d)
    if c["expected_serial"] not in [v.strip() for v in before["idn"].split(",")]:
        raise ValueError(f"Wrong instrument: {before['idn']}")
    if 'HSA1036-TG' not in before['idn'] or 'V3.0.2.0' not in before['idn']:
        raise ValueError('Native trace interface needs validation for this model/firmware')
    # Temporarily disable preamp while changing reference/attenuation/range.
    # Apply requested ON only after those settings, before the measured sweep.
    # Do not reset the entire instrument. TG remains off throughout.
    commands = [":OUTP:TRAC 0", ":UNIT:POW DBM", ":POW:GAIN:AUTO 0",
        f":DISP:WIN:Y:RLEV {c['reference_dbm']}", ":DISP:WIN:Y:RLEV:OFFS 0",
        ":POW:ATT:AUTO 0", f":POW:ATT {c['attenuation_db']}",
        ":DISP:WIN:X:OFFS 0", ":DISP:WIN:X:SPAC LIN", ":FREQ:STAR 9000",
        f":FREQ:STOP {c['stop_hz']}", f":FREQ:STAR {c['start_hz']}",
        ":BAND:AUTO 0", f":BAND {c['rbw_hz']}", ":BAND:VID:AUTO 0",
        f":BAND:VID {c['vbw_hz']}", ":BAND:EMC:STAT 0", ":TRAC1:DET POS",
        ":TRAC1:MODE WRIT", ":SWE:TIME:AUTO 1"]
    # Frequency/bandwidth changes rebuild a native 801-point trace. Do NOT send
    # SWE:POIN here: non-native lengths were observed to mislabel the tone.
    # The device-internal marker reproduces this; ADC/buffer internals unknown.
    if preamp:
        commands.append(':POW:GAIN:AUTO 1')
    commands.append(":INIT:CONT 0")
    for command in commands:
        d.write(command)
        time.sleep(.03)
    after = settings(d)
    verify(after, dict(c, npoints=NATIVE_POINTS))
    return before, after


def configure(d, c):
    require_single_sweep_config(c)
    if 'bin_size_hz' in c:
        plan = make_plan(c)
        before, native = configure_native(d, segment_config(c, plan['segments'][0]))
        verify_planned_axis(native, plan['segments'][0])
        return before, dict(native, start_hz=str(c['start_hz']), stop_hz=str(c['stop_hz']),
                            npoints=str(plan['output_npoints']), frequency_axis_schema=PLAN_SCHEMA,
                            plan=plan, native_first_settings=dict(native))
    before, native = configure_native(d, dict(c, npoints=NATIVE_POINTS))
    return before, dict(native, frequency_axis_schema=AXIS_SCHEMA)


def verify_record_settings(d, c, s):
    require_single_sweep_config(c)
    if s.get('frequency_axis_schema') == PLAN_SCHEMA:
        part = s['plan']['segments'][-1]
        actual = settings(d)
        verify(actual, segment_config(c, part))
        verify_planned_axis(actual, part)
        return
    if 'segments' in s:
        raise ValueError('Segmented acquisition is disabled; start a new single-sweep run')
    verify(settings(d), dict(c, npoints=NATIVE_POINTS))


def acquire(d, c, s, clock):
    require_single_sweep_config(c)
    if s.get('frequency_axis_schema') == PLAN_SCHEMA:
        parts = s['plan']['segments']
        rows, timings = [], []
        for index, part in enumerate(parts):
            native_c = segment_config(c, part)
            if len(parts) == 1:
                actual = settings(d)
                verify(actual, native_c)
            else:
                _, actual = configure_native(d, native_c)
            verify_planned_axis(actual, part)
            row = acquire_single(d, native_c, actual, clock)
            if len(row[3]) != NATIVE_POINTS:
                raise ProtocolError(f'Expected native 801 values, got {len(row[3])}')
            after = settings(d)
            verify(after, native_c)
            verify_planned_axis(after, part)
            rows.append(row)
            timings.append(dict(segment=index, request_utc_ns=row[0], read_complete_utc_ns=row[1],
                                pi_system_request_ns=row[2], output_start_index=part['output_start_index'],
                                output_stop_index=part['output_stop_index'], sweep_ms=actual['sweep_ms']))
        if any(rows[i][0] < rows[i-1][1] for i in range(1, len(rows))):
            raise ValueError('Segment timestamps overlap')
        values = np.concatenate([r[3][p['keep_from']:p['keep_to']] for r, p in zip(rows, parts)])
        LOG.info('SEGMENTS %s', json.dumps(dict(cycle_request_utc_ns=rows[0][0], segments=timings), separators=(',', ':')))
        return rows[0][0], rows[-1][1], rows[0][2], values
    if 'segments' in s:
        raise ValueError('Segmented acquisition is disabled; no stitched rows are accepted')
    row = acquire_single(d, c, s, clock)
    if len(row[3]) != NATIVE_POINTS:
        raise ProtocolError(f'Expected native 801 values, got {len(row[3])}')
    return row


def verify_planned_axis(actual, part):
    # Reject rounded endpoints rather than relabeling native measurements.
    for key in ('start_hz', 'stop_hz'):
        if not math.isclose(float(actual[key]), part[key], rel_tol=0, abs_tol=.001):
            raise ValueError(f'Native frequency endpoint mismatch {key}: {actual[key]} != {part[key]}')


def acquire_single(d, c, s, clock):
    start = clock.now_ns()
    system_start = time.time_ns()
    d.write(":INIT:CONT 0")  # On V3.0.2.0, repeating OFF initiates a new single sweep.
    wait = float(s["sweep_ms"]) / 1000 * c["sweep_wait_factor"] + c["sweep_wait_margin_seconds"]
    time.sleep(wait)
    first = d.trace()
    for retry in range(3):
        time.sleep(c["stable_check_seconds"])
        second = d.trace()
        if np.array_equal(first, second):
            return start, clock.now_ns(), system_start, second
        LOG.warning("Trace still changing after wait; extending wait (attempt %s)", retry + 1)
        time.sleep(wait)
        first = d.trace()
    raise ProtocolError("Single-sweep trace did not become stable")


class Journal:
    def __init__(self, run_dir, c, metadata, clock):
        self.root = run_dir
        self.c, self.meta, self.clock = c, metadata, clock
        self.freq = None
        self.file = None
        self.part = uuid.uuid4().hex[:8]
        self.count = 0
        self.last = None

    def open_csv(self):
        """Append only after validating the schema and durable last row.

        A damaged tail is never truncated automatically. Existing legacy runs
        must be analyzed separately or resumed with the previous recorder.
        """
        path = self.root / "raw_sweeps.csv"
        if any(p != path for p in self.root.glob("raw_*.csv")):
            raise ValueError("Legacy multi-file run: use the previous recorder to resume, or start a new run without --resume")
        header = ["request_utc_ns", "read_complete_utc_ns", "pi_system_request_ns"] + [f"{v:.3f}_Hz" for v in self.freq]
        exists = path.exists()
        if exists:
            metadata_path = self.root / "metadata_sweeps.json"
            original = json.loads(metadata_path.read_text())
            if original.get('settings', {}).get('frequency_axis_schema') != self.meta.get('settings', {}).get('frequency_axis_schema'):
                raise ValueError('Existing CSV frequency-axis method differs; start a new run, never resume legacy shifted data')
            if original.get("config") != self.meta.get("config"):
                raise ValueError("Existing CSV config differs; refusing append")
            with path.open("rb") as f:
                encoded_header = f.readline()
                if not encoded_header.endswith(b"\n") or next(csv.reader([encoded_header.decode('ascii')])) != header:
                    raise ValueError("Existing CSV header differs or is incomplete; refusing append")
                header_end = f.tell()
                f.seek(0, os.SEEK_END)
                size = f.tell()
                if size > header_end:
                    # Bound the tail read by a generous maximum numeric row size.
                    f.seek(max(header_end, size - (len(header)*64 + 256)))
                    tail = f.read()
                    if not tail.endswith(b"\n"):
                        raise ValueError("Incomplete CSV tail; preserve the file and recover it before resuming")
                    values = next(csv.reader([tail.splitlines()[-1].decode('ascii')]))
                    try:
                        a, b, system = map(int, values[:3])
                        power = np.asarray(values[3:], dtype='float64')
                        valid = len(values) == len(header) and b > a and np.isfinite(power).all()
                    except (ValueError, TypeError):
                        valid = False
                    if not valid:
                        raise ValueError("Invalid CSV tail; refusing append")
                    self.last = b
            # Preserve a new clock/readback snapshot for each connection.
            atomic_json(self.root / f"metadata_{self.part}.json", self.meta)
        else:
            atomic_json(self.root / "metadata_sweeps.json", self.meta)
        self.file = path.open("a" if exists else "x", newline="", encoding="ascii")
        self.writer = csv.writer(self.file)
        if not exists:
            self.writer.writerow(header)

    def add(self, row):
        start, end, system, y = row
        if len(y) < 2 or not np.isfinite(y).all() or end <= start:
            raise ValueError("Invalid trace values or acquisition interval")
        if self.c.get("npoints") is not None and len(y) != self.c["npoints"]:
            raise ValueError(f"Binary trace length {len(y)} != requested {self.c['npoints']}")
        if self.freq is None:
            planned = self.meta['settings'].get('frequency_axis_schema') == PLAN_SCHEMA
            self.freq = frequency_axis(self.meta['settings']['plan']) if planned else np.linspace(
                float(self.meta['settings']['start_hz']), float(self.meta['settings']['stop_hz']), len(y))
            if len(y) != len(self.freq):
                raise ValueError('Trace length does not match planned frequency axis')
            df = np.diff(self.freq)
            uniform = bool(np.allclose(df, df[0], rtol=0, atol=1e-5))
            self.meta.update(point_count=len(y), bin_spacing_hz=float(df[0]) if uniform else None,
                             bin_spacing_min_hz=float(df.min()), bin_spacing_max_hz=float(df.max()),
                             frequency_axis='Concatenated native segment coordinates; cropped duplicates only; no interpolation' if planned else 'Native/legacy linear trace axis; consult frequency_axis_schema')
            self.meta.update(storage_format=STORAGE_FORMAT, raw_file="raw_sweeps.csv")
            self.open_csv()
        if len(y) != len(self.freq):
            raise ValueError("Trace length changed during acquisition")
        if self.last is not None and start < self.last:
            raise ValueError("Acquisition overlaps existing CSV data; refusing append")
        self.writer.writerow([start, end, system] + [format(float(v), ".8g") for v in y])
        self.file.flush()
        os.fsync(self.file.fileno())  # Reduces buffered loss; does not guarantee power-loss safety.
        self.count += 1
        self.last = end

    def close(self):
        if self.file:
            self.file.close()
            self.file = None


def record(c, args, clock):
    require_single_sweep_config(c)
    root = Path(c["output_dir"])
    if not root.is_absolute():
        root = HERE / root
    root.mkdir(parents=True, exist_ok=True)
    pointer = HERE / "current_session.json"
    duration = args.seconds if args.seconds is not None else args.hours * 3600
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Duration must be finite and positive")
    session = None
    if args.resume and pointer.exists():
        previous = json.loads(pointer.read_text())
        if not previous.get("complete", False):
            session = previous
            if session["config"] != c:
                raise ValueError("Unfinished session uses another config; finish it or archive current_session.json")
    if session is None:
        start = clock.now_ns()
        run = create_run_directory(root, start)
        session = {"directory": str(run), "start_utc_ns": start,
                   "deadline_utc_ns": start + int(duration*1e9), "config": c, "complete": False,
                   "storage_format": STORAGE_FORMAT}
        if args.resume:
            atomic_json(pointer, session)
    run = Path(session["directory"])
    if clock.now_ns() < session["deadline_utc_ns"] and any(p.name != "raw_sweeps.csv" for p in run.glob("raw_*.csv")):
        raise ValueError("Legacy multi-file run cannot be appended by CSV-only recorder; start a new run without --resume")
    run.mkdir(parents=True, exist_ok=True)
    atomic_json(run / "session.json", session)
    filelog = logging.FileHandler(run / "events.log", encoding="utf-8")
    filelog.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    LOG.addHandler(filelog)
    stop = False

    def request_stop(signum, frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    device = None
    journal = None
    previous_cont = "1"
    measured = None
    errors = 0
    total = 0
    last_status = {}
    try:
        LOG.info("UTC %s run=%s deadline=%s", iso(clock.now_ns()), run, iso(session["deadline_utc_ns"]))
        LOG.info("PID %s | CSV %s | remaining %.1f s", os.getpid(), run / "raw_sweeps.csv",
                 max(0, (session["deadline_utc_ns"]-clock.now_ns())/1e9))
        while not stop and clock.now_ns() < session["deadline_utc_ns"]:
            if shutil.disk_usage(root).free < c["min_free_mb"] * 1024**2:
                raise ValueError("Low disk space; recording stopped")
            try:
                if device is None:
                    device = Owon(c["host"], c["port"])
                    before, measured = configure(device, c)
                    previous_cont = before["continuous"]
                    metadata = {"config": c, "settings": measured, "before_settings": before,
                        "window_origin_utc_ns": session["start_utc_ns"],
                        "window_membership": "Acquisition request time, non-overlapping windows from recording start",
                        "clock_anchor": clock.anchor,
                        "timestamp_semantics": "UTC acquisition-request and read-complete; not per-frequency hardware timestamps",
                        "acquisition": "Sequential native 801-point sweeps per recorded plan (or one legacy native sweep); >=2x reported sweep time + margin and equal binary reads. No interpolation or forced points. Complete cycles only.",
                        "statistics": "Raw positive-peak sweeps only. Window statistics computed offline; NOT continuous RMS integration.",
                        "software_calibration_applied": False}
                    journal = Journal(run, c, metadata, clock)
                    LOG.info("UTC %s connected: %s | verified points=%s | reported sweep=%s ms",
                             iso(clock.now_ns()), measured["idn"], measured.get("npoints", "read from first trace"), measured["sweep_ms"])
                row = acquire(device, c, measured, clock)
                # Check all measurement settings before accepting the trace.
                verify_record_settings(device, c, measured)
                if stop:
                    break
            except (OSError, ProtocolError) as exc:
                # Only acquisition failures are retried; disk errors must stop the process.
                errors += 1
                LOG.error("UTC %s acquisition error %s: %s", iso(clock.now_ns()), errors, exc)
                last_status.update(state="reconnecting", utc=iso(clock.now_ns()), last_error=str(exc), run=str(run))
                atomic_json(HERE / "status.json", last_status)
                if journal:
                    journal.close()
                    journal = None
                if device:
                    device.close()
                    device = None
                until = time.monotonic() + min(30, 2**min(errors, 5))
                while not stop and time.monotonic() < until and clock.now_ns() < session["deadline_utc_ns"]:
                    time.sleep(.2)
                continue
            journal.add(row)
            total += 1
            errors = 0
            last_status = {"state": "recording", "utc": iso(clock.now_ns()), "run": str(run),
                "pid": os.getpid(), "storage_format": STORAGE_FORMAT, "csv_file": str(run / "raw_sweeps.csv"),
                "process_sweep_count": total, "points": len(row[3]),
                "bin_spacing_hz": journal.meta.get('bin_spacing_hz'),
                "bin_spacing_max_hz": journal.meta.get('bin_spacing_max_hz'),
                "segments_per_cycle": len(measured.get('plan', {}).get('segments', [None])),
                "request_to_read_seconds": (row[1]-row[0])/1e9,
                "deadline_utc": iso(session["deadline_utc_ns"]), "last_error": None}
            atomic_json(HERE / "status.json", last_status)
        session["complete"] = True
        session["reason"] = "user_stop" if stop else "duration_elapsed"
    finally:
        try:
            if journal:
                journal.close()
        finally:
            if device:
                try:
                    device.write(f":INIT:CONT {previous_cont}")
                except (OSError, ValueError, ProtocolError) as exc:
                    LOG.warning('Could not restore continuous mode: %s', exc)
                device.close()
        if args.resume:
            atomic_json(pointer, session)
        atomic_json(run / "session.json", session)
        last_status.update(state="stopped" if session["complete"] else "failed", utc=iso(clock.now_ns()), run=str(run))
        atomic_json(HERE / "status.json", last_status)
        LOG.info("UTC %s finished: %s sweeps in this process", iso(clock.now_ns()), total)
        LOG.removeHandler(filelog)
        filelog.close()


class LinearPowerMean:
    """Streaming equal-weight mean; memory is O(points), not O(sweeps)."""
    def __init__(self, points):
        self.mean_mw = np.zeros(points, dtype=np.float64)
        self.count = 0

    def add(self, dbm):
        y = np.asarray(dbm, dtype=np.float64)
        if y.shape != self.mean_mw.shape or not np.all(np.isfinite(y)):
            raise ValueError('Invalid baseline trace length or non-finite power')
        with np.errstate(over='ignore', under='ignore'):
            mw = np.power(10.0, y / 10.0)
        if not np.all(np.isfinite(mw)) or np.any(mw <= 0):
            raise ValueError('Baseline power is outside representable linear range')
        self.count += 1
        self.mean_mw += (mw - self.mean_mw) / self.count

    def dbm(self):
        if not self.count:
            raise ValueError('No complete baseline cycles acquired')
        return 10.0 * np.log10(self.mean_mw)


def baseline(c, args, clock):
    """Standalone SA-input termination reference. Never used for subtraction."""
    duration = requested_duration(args)
    root = Path(c['output_dir'])
    if not root.is_absolute():
        root = HERE / root
    root.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(root).free < c['min_free_mb'] * 1024**2:
        raise ValueError('Low disk space; baseline not started')
    start = clock.now_ns()
    run = create_run_directory(root, start)
    session = dict(directory=str(run), start_utc_ns=start, config=c,
                   measurement_type='sa_input_50ohm_baseline', complete=False,
                   storage_format='mean_baseline_csv_v1', requested_seconds=duration)
    atomic_json(run / 'session.json', session)
    filelog = logging.FileHandler(run / 'events.log', encoding='utf-8')
    filelog.setFormatter(logging.Formatter('%(levelname)s %(message)s'))
    LOG.addHandler(filelog)
    stop = False
    def request_stop(signum, frame):
        nonlocal stop
        stop = True
    handlers = {sig: signal.signal(sig, request_stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    device = None
    previous_cont = None
    count = 0
    temporary = run / 'baseline.csv.tmp'
    status = dict(state='configuring', operation='baseline', pid=os.getpid(), run=str(run))
    try:
        atomic_json(HERE / 'baseline_status.json', status)
        LOG.info('BASELINE %s | connect 50 ohm termination directly to SA input | %.1f s', run, duration)
        device = Owon(c['host'], c['port'])
        before, measured = configure(device, c)
        previous_cont = before['continuous']
        frequencies = (frequency_axis(measured['plan']) if measured.get('frequency_axis_schema') == PLAN_SCHEMA
                       else np.linspace(float(measured['start_hz']), float(measured['stop_hz']), int(measured['npoints'])))
        mean = LinearPowerMean(len(frequencies))
        acquisition_start = clock.now_ns()
        session['deadline_utc_ns'] = acquisition_start + int(duration * 1e9)
        atomic_json(run / 'session.json', session)
        deadline = time.monotonic() + duration
        first_request = last_read = None
        while not stop and time.monotonic() < deadline:
            if shutil.disk_usage(root).free < c['min_free_mb'] * 1024**2:
                raise ValueError('Low disk space; baseline stopped')
            row = acquire(device, c, measured, clock)
            verify_record_settings(device, c, measured)
            if stop:
                break
            if row[1] < row[0] or (last_read is not None and row[0] < last_read):
                raise ValueError('Invalid baseline acquisition timestamps')
            mean.add(row[3])
            count = mean.count
            first_request = row[0] if first_request is None else first_request
            last_read = row[1]
            status.update(state='measuring', cycle_count=count, utc=iso(clock.now_ns()),
                          deadline_utc=iso(session['deadline_utc_ns']))
            atomic_json(HERE / 'baseline_status.json', status)
            LOG.info('BASELINE cycle=%d | elapsed=%.1f s / %.1f s', count,
                     (clock.now_ns()-acquisition_start)/1e9, duration)
        if stop:
            raise InterruptedError('Baseline interrupted; no completed mean spectrum published')
        averaged_dbm = mean.dbm()
        metadata = dict(measurement_type=session['measurement_type'], config=c,
                        settings=measured, before_settings=before, clock_anchor=clock.anchor,
                        reference_plane='SA input; 50 ohm termination supplied/connected by operator; not electronically verified',
                        termination_temperature_kelvin=None, software_calibration_applied=False,
                        subtraction_applied=False, spectrum_file='baseline.csv', points=len(frequencies),
                        cycle_count=count, requested_seconds=duration,
                        acquisition_start_utc_ns=acquisition_start,
                        first_request_utc_ns=first_request, last_read_complete_utc_ns=last_read,
                        acquisition_elapsed_seconds=(last_read-acquisition_start)/1e9,
                        statistics='Equal-weight complete-cycle mean: 10*log10(mean(10**(dBm/10))). Positive Peak detector; NOT continuous RMS integration or calibrated noise figure.',
                        timing='Sequential segments with setup/readout dead time; not 60 seconds dwell at every frequency. Final complete cycle may overrun. Segment times in events.log.',
                        config_context='rf_chain/polarization/notes describe the source config only; actual baseline input is the 50 ohm termination.')
        atomic_json(run / 'metadata_baseline.json', metadata)
        with temporary.open('x', newline='', encoding='utf-8') as stream:
            writer = csv.writer(stream)
            writer.writerow(['frequency_hz', 'mean_power_dbm'])
            writer.writerows((f'{f:.3f}', f'{y:.9f}') for f, y in zip(frequencies, averaged_dbm))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, run / 'baseline.csv')
        session.update(complete=True, reason='duration_elapsed', cycle_count=count)
        LOG.info('BASELINE saved %s | %d cycles | %d points | NO subtraction', run / 'baseline.csv', count, len(frequencies))
        return run
    except BaseException as exc:
        session.update(reason='interrupted' if isinstance(exc, (InterruptedError, KeyboardInterrupt)) else 'error',
                       error=str(exc), cycle_count=count)
        raise
    finally:
        if device:
            try:
                if previous_cont is not None:
                    device.write(f':INIT:CONT {previous_cont}')
            except (OSError, ValueError, ProtocolError) as exc:
                LOG.warning('Could not restore continuous mode: %s', exc)
            finally:
                device.close()
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
        temporary.unlink(missing_ok=True)
        session['end_utc_ns'] = clock.now_ns()
        try:
            atomic_json(run / 'session.json', session)
            status.update(state='complete' if session['complete'] else 'failed',
                          cycle_count=count, utc=iso(session['end_utc_ns']), reason=session.get('reason'))
            atomic_json(HERE / 'baseline_status.json', status)
        finally:
            LOG.removeHandler(filelog)
            filelog.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["probe", "configure", "record", "baseline", "status"])
    p.add_argument("--config", type=Path, default=HERE / "config.json")
    p.add_argument("--hours", type=float, help="Duration in hours (record default: 24 h; baseline default: 60 s)")
    p.add_argument("--seconds", type=float)
    p.add_argument("--resume", action="store_true", help="Preserve the original deadline across process restarts")
    p.add_argument("--summary-only", action="store_true", help="Print record/baseline config without clock access, files, or instrument connection")
    a = p.parse_args()
    if a.command == 'baseline' and a.resume:
        p.error('baseline cannot resume; start a new independent reference')
    if a.command == 'record' and a.hours is None:
        a.hours = 24
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if a.command == "status":
        print((HERE / "status.json").read_text() if (HERE / "status.json").exists() else "No recording yet")
        return
    c = json.loads(a.config.read_text())
    validate_config(c)
    if a.command in ('record', 'baseline'):
        print(config_summary(c, a), flush=True)
    if a.summary_only:
        if a.command not in ('record', 'baseline'):
            p.error("--summary-only requires record or baseline")
        return
    if a.command in ('record', 'baseline', 'configure'):
        # Reject BEFORE opening a device/lock/clock or creating a run directory.
        require_single_sweep_config(c)
    with (HERE / ".control.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another recorder/configuration process owns the instrument")
        if a.command == "record":
            record(c, a, Clock())
        elif a.command == 'baseline':
            baseline(c, a, Clock())
        else:
            with Owon(c["host"], c["port"]) as d:
                if a.command == "configure":
                    print(json.dumps(configure(d, c), indent=2))
                else:
                    s = settings(d)
                    y = d.trace()
                    print(json.dumps({"settings": s, "trace_points": len(y),
                                      "min_dbm": float(y.min()), "max_dbm": float(y.max())}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, FileNotFoundError) as exc:
        print(f"Configuration/clock error: {exc}", file=sys.stderr)
        sys.exit(2)
