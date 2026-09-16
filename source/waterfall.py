"""Plot single-file CSV sweeps and legacy hourly CSV journals.

Run on a laptop with numpy and matplotlib. No instrument connection required.
"""
import argparse
import csv
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import warnings

import numpy as np


def load_csv(directory):
    freq = None
    records = []
    settings = None
    for path in sorted(Path(directory).glob("raw_*.csv")):
        part = path.stem.rsplit("_", 1)[-1]
        metadata = json.loads((path.parent / f"metadata_{part}.json").read_text())
        compare = {k: metadata["settings"][k] for k in
                   ("start_hz", "stop_hz", "rbw_hz", "vbw_hz", "reference_dbm",
                    "attenuation_db", "preamp", "detector", "trace_mode", "unit")}
        compare['frequency_axis_schema'] = metadata['settings'].get('frequency_axis_schema', 'legacy_unvalidated')
        if settings is not None and settings != compare:
            raise ValueError("Files have different instrument settings; plot separately")
        settings = compare
        with path.open(newline="") as f:
            reader = csv.reader(f)
            header = next(reader)
            axis = np.array([float(v.removesuffix("_Hz")) for v in header[3:]])
            if compare['frequency_axis_schema'] == 'legacy_unvalidated' and len(axis) != 801:
                warnings.warn(f'{path.name}: legacy {len(axis)}-point frequency axis is unvalidated; '
                              'HSA1036-TG V3.0.2.0 forced-point traces can be frequency-shifted. '
                              'Do not automatically correct or combine with native801_segments_v1 data.')
            if freq is not None and not np.array_equal(axis, freq):
                raise ValueError("Frequency axis changed; plot the segments separately")
            freq = axis
            for line, row in enumerate(reader, 2):
                try:
                    if len(row) != 3 + len(freq):
                        raise ValueError("incomplete row")
                    start, end = int(row[0]), int(row[1])
                    int(row[2])
                    power = np.array(row[3:], dtype="float32")
                    if end < start or not np.isfinite(power).all():
                        raise ValueError("invalid time/amplitude")
                    records.append((start, end, power))
                except ValueError as exc:
                    warnings.warn(f"Skipping {path.name}:{line}: {exc}")
    if not records:
        raise ValueError("No valid recorded CSV sweeps")
    records.sort(key=lambda r: r[0])
    times = np.array([r[0] for r in records], dtype="int64")
    if np.any(np.diff(times) <= 0):
        raise ValueError("Duplicate or non-increasing acquisition timestamps")
    return freq, times, np.array([r[1] for r in records], dtype="int64"), np.stack([r[2] for r in records]), settings


def aggregate(times, power, seconds=60, threshold=None):
    if seconds <= 0:
        raise ValueError("Bin length must be positive")
    step = int(seconds * 1e9)
    first, last = times[0] // step, times[-1] // step
    n = int(last - first + 1)
    if n > 100000:
        raise ValueError("Too many time bins; increase bin length")
    maximum = np.full((n, power.shape[1]), np.nan, dtype="float32")
    median = maximum.copy()
    mean = maximum.copy()
    occupancy = maximum.copy()
    count = np.zeros(n, dtype="int64")
    ids = times // step - first
    # Sweep timestamps are sorted, so each group's indices are contiguous.
    for index in np.unique(ids):
        lo, hi = np.searchsorted(ids, [index, index+1])
        values = power[lo:hi]
        count[index] = len(values)
        maximum[index] = values.max(axis=0)
        median[index] = np.median(values, axis=0)
        mean[index] = 10*np.log10(np.mean(10**(values.astype("float64")/10), axis=0))
        if threshold is not None:
            occupancy[index] = (values > threshold).mean(axis=0)
    return {"time_edges_utc_ns": np.arange(first, last+2, dtype="int64") * step,
            "max_dbm": maximum, "median_peak_dbm": median,
            "mean_peak_power_dbm": mean, "sampled_sweep_occupancy": occupancy,
            "sweep_count": count}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("directory", type=Path)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--bin-seconds", type=float, default=60)
    p.add_argument("--min-mhz", type=float, default=1)
    p.add_argument("--max-mhz", type=float, default=1000)
    p.add_argument("--metric", choices=["max_dbm", "median_peak_dbm", "mean_peak_power_dbm", "sampled_sweep_occupancy"], default="max_dbm")
    p.add_argument("--threshold-dbm", type=float)
    p.add_argument("--timezone", choices=["UTC", "KST"], default="KST")
    p.add_argument("--vmin", type=float)
    p.add_argument("--vmax", type=float)
    a = p.parse_args()
    if a.metric == "sampled_sweep_occupancy" and a.threshold_dbm is None:
        p.error("Occupancy requires --threshold-dbm (fraction of sampled sweeps, not time duty cycle)")
    freq, times, ends, power, settings = load_csv(a.directory)
    result = aggregate(times, power, a.bin_seconds, a.threshold_dbm)
    mask = (freq / 1e6 >= a.min_mhz) & (freq / 1e6 <= a.max_mhz)
    if mask.sum() < 2:
        p.error("Selected frequency range contains fewer than two samples")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    zone = timezone.utc if a.timezone == "UTC" else timezone(timedelta(hours=9), "KST")
    edges = np.r_[freq[0]-(freq[1]-freq[0])/2,
                  (freq[:-1]+freq[1:])/2, freq[-1]+(freq[-1]-freq[-2])/2] / 1e6
    date_edges = [datetime.fromtimestamp(t/1e9, timezone.utc) for t in result["time_edges_utc_ns"]]
    fig, ax = plt.subplots(figsize=(12, 5.5), layout="constrained")
    cmap = plt.get_cmap("viridis").copy()
    cmap.set_bad("#dddddd")
    im = ax.pcolormesh(edges, mdates.date2num(date_edges), np.ma.masked_invalid(result[a.metric]),
                       shading="flat", cmap=cmap, vmin=a.vmin, vmax=a.vmax)
    ax.set_xlim(a.min_mhz, a.max_mhz)
    ax.yaxis_date()
    ax.yaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S", tz=zone))
    ax.set_xlabel("Frequency (MHz)")
    ax.set_ylabel(f"Time ({a.timezone})")
    start = datetime.fromtimestamp(times[0]/1e9, zone)
    ax.set_title(f"OWON HSA1036-TG | {start:%Y-%m-%d} | {a.metric}\n"
                 f"RBW {float(settings['rbw_hz'])/1000:g} kHz | {a.bin_seconds:g} s bins | "
                 f"{len(times)} sweeps | {len(freq)} frequency points")
    label = "Fraction of sampled sweeps above threshold" if "occupancy" in a.metric else "Reported power (dBm), positive-peak detector"
    fig.colorbar(im, ax=ax, label=label)
    a.output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(a.output, dpi=160)
    plt.close(fig)
    np.savez_compressed(a.output.with_suffix(".npz"), frequency_hz=freq,
                        **result, settings_json=np.array(json.dumps(settings)),
                        threshold_dbm=np.float64(np.nan if a.threshold_dbm is None else a.threshold_dbm))
    print(json.dumps({"plot": str(a.output), "sweeps": len(times), "time_bins": len(result['sweep_count']),
                      "first_request_utc": datetime.fromtimestamp(times[0]/1e9, timezone.utc).isoformat(),
                      "last_read_utc": datetime.fromtimestamp(ends[-1]/1e9, timezone.utc).isoformat()}))


if __name__ == "__main__":
    main()
