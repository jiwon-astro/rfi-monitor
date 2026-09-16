"""Offline scientific plots for OWON CSV journals. Requires numpy/matplotlib."""
from datetime import datetime, timezone, timedelta
from pathlib import Path
import json

import numpy as np
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from waterfall import load_csv

KST = timezone(timedelta(hours=9), "KST")
STYLE = {
    "font.family": "DejaVu Sans", "font.size": 16,
    "axes.labelsize": 21, "xtick.labelsize": 16, "ytick.labelsize": 16,
    "legend.fontsize": 15, "axes.linewidth": 1.1,
    "xtick.direction": "out", "ytick.direction": "out",
    "xtick.major.size": 6, "ytick.major.size": 6,
    "figure.facecolor": "white", "axes.facecolor": "white",
    "savefig.facecolor": "white", "pdf.fonttype": 42,
}


def resolve_run_inputs(inputs, data_root):
    """Accept a raw_sweeps.csv, a flat Pi run, or a legacy run/raw folder."""
    if isinstance(inputs, (str, Path)):
        inputs = [inputs]
    roots = []
    for value in inputs:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = Path(data_root) / path
        path = path.resolve()
        if path.is_file():
            if path.name != 'raw_sweeps.csv':
                raise ValueError('Select raw_sweeps.csv, or the entire legacy run folder')
            path = path.parent
        elif path.is_dir() and not (path / 'session.json').is_file():
            if (path / 'raw').is_dir():
                path = path / 'raw'
        if not path.is_dir():
            raise FileNotFoundError(f'Input does not exist: {path}')
        csvs = sorted(path.glob('raw_*.csv'))
        if not csvs:
            raise ValueError(f'No raw CSV files: {path}')
        if (path / 'raw_sweeps.csv') in csvs and len(csvs) != 1:
            raise ValueError(f'Mixed single-CSV and legacy files: {path}')
        required = [path / 'session.json'] + [
            path / f'metadata_{csv.stem.rsplit("_", 1)[-1]}.json' for csv in csvs]
        missing = [p.name for p in required if not p.is_file()]
        if missing:
            raise ValueError(f'Missing companion files in {path}: {missing}. '
                             'Use fetch-data.ps1 -Run to download the complete run.')
        if path not in roots:
            roots.append(path)
    if not roots:
        raise ValueError('Choose at least one input')
    return roots


def load_runs(directories):
    """Rebuild from unique source directories; never append old derived NPZs.

    Sorting is by UTC, not filename. Identical timestamp duplicates are removed;
    conflicting duplicates and incompatible settings/sites raise an error.
    """
    chunks, reference, frequency, site = [], None, None, None
    recording_starts = []
    paths = sorted({Path(p).resolve() for p in directories})
    if not paths:
        raise ValueError("Select at least one run directory")
    for root in paths:
        if (root / "session.json").exists():
            recording_starts.append(int(json.loads((root / "session.json").read_text())["start_utc_ns"]))
        f, starts, ends, values, setting = load_csv(root)
        meta_paths = sorted(root.glob("metadata_*.json"))
        identities = []
        for meta_path in meta_paths:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            cfg = meta["config"]
            identities.append({k: cfg.get(k) for k in ("site", "polarization", "rf_chain")})
        if any(v != identities[0] for v in identities):
            raise ValueError("Mixed site/polarization/RF chain in one run")
        signature = {"settings": setting, "identity": identities[0]}
        if reference is not None and signature != reference:
            raise ValueError("Different settings/site/polarization/RF chain: analyze separately")
        if frequency is not None and not np.array_equal(f, frequency):
            raise ValueError("Different frequency axes: analyze separately")
        reference, frequency, site = signature, f, identities[0]["site"]
        chunks.append((starts, ends, values))
    starts = np.concatenate([v[0] for v in chunks])
    ends = np.concatenate([v[1] for v in chunks])
    power = np.concatenate([v[2] for v in chunks])
    order = np.argsort(starts, kind="stable")
    starts, ends, power = starts[order], ends[order], power[order]
    keep = np.ones(len(starts), dtype=bool)
    for i in np.flatnonzero(np.diff(starts) == 0) + 1:
        if ends[i] != ends[i-1] or not np.array_equal(power[i], power[i-1]):
            raise ValueError("Conflicting data at the same acquisition timestamp")
        keep[i] = False
    starts, ends, power = starts[keep], ends[keep], power[keep]
    if np.any(ends <= starts) or np.any(starts[1:] < ends[:-1]):
        raise ValueError("Invalid or overlapping acquisition intervals")
    return {"frequency_hz": frequency, "request_utc_ns": starts,
            "read_complete_utc_ns": ends, "power_dbm": power,
            "site": site, "settings": reference["settings"],
            "recording_start_utc_ns": min(recording_starts) if recording_starts else int(starts[0]),
            "identity": reference["identity"], "source_directories": [str(p) for p in paths]}


def bin_max(data, seconds=60):
    """Max over sampled sweeps; windows anchored to first request, gaps = NaN.

    Membership uses request time. The final window may be partial. This is not
    a continuous-time measurement or a firmware Max Hold trace.
    """
    if not np.isfinite(seconds) or seconds < 0.001:
        raise ValueError("seconds must be finite and >= 0.001")
    starts, ends, power = (data[k] for k in ("request_utc_ns", "read_complete_utc_ns", "power_dbm"))
    step = int(seconds * 1e9)
    ids = (starts - starts[0]) // step
    n = int(ids[-1]) + 1
    if n * power.shape[1] > 50_000_000:
        raise ValueError("Too many bins; increase bin_seconds or select fewer runs")
    values = np.full((n, power.shape[1]), np.nan, dtype=np.float32)
    count = np.zeros(n, dtype=np.int64)
    for index in np.unique(ids):
        a, b = np.searchsorted(ids, [index, index+1])
        values[index] = power[a:b].max(axis=0)
        count[index] = b-a
    edges = starts[0] + np.arange(n+1, dtype=np.int64) * step
    # End at the actual last read, even when the final request crossed a boundary.
    edges[-1] = min(edges[-1], ends[-1])
    return {"time_edges_utc_ns": edges, "max_dbm": values, "sweep_count": count}


def frequency_edges(frequency_hz):
    f = np.asarray(frequency_hz) / 1e6
    return np.r_[f[0]-(f[1]-f[0])/2, (f[1:]+f[:-1])/2, f[-1]+(f[-1]-f[-2])/2]


def figure_title(data, user_text="", time_zone="KST", start_utc_ns=None):
    """Recording-start Date_HMS_Site_(user text), in the selected timezone."""
    if time_zone not in ("UTC", "KST"):
        raise ValueError("time_zone must be UTC or KST")
    zone = timezone.utc if time_zone == "UTC" else KST
    ns = data.get("recording_start_utc_ns", data["request_utc_ns"][0]) if start_utc_ns is None else start_utc_ns
    stamp = datetime.fromtimestamp(int(ns)/1e9, zone).strftime("%Y-%m-%d_%H%M%S")
    result = f"{stamp}_{data.get('site', 'unknown')}"
    return result + (f"_({user_text})" if user_text else "")


def absolute_time_ticks(ax, edges, time_zone):
    zone = timezone.utc if time_zone == "UTC" else KST
    a = datetime.fromtimestamp(int(edges[0])/1e9, zone)
    b = datetime.fromtimestamp(int(edges[-1])/1e9, zone)
    fmt = "%H:%M:%S" if a.date() == b.date() else "%m-%d\n%H:%M"
    locator = mdates.AutoDateLocator(minticks=4, maxticks=7, tz=zone)
    locator.intervald[mdates.MINUTELY] = [1, 2, 5, 10, 15, 20, 30]
    ax.yaxis.set_major_locator(locator)
    ax.yaxis.set_major_formatter(mdates.DateFormatter(fmt, tz=zone))


def waterfall_plot(data, *, frequency_range=(1, 1000), bin_seconds=None,
                   cumulative=False, time_axis="KST", vmin=None, vmax=None,
                   title_text="", title_start_utc_ns=None):
    """Left-aligned recording title; large fonts; gray = missing intervals.

    Raw rows occupy request-to-read intervals, NOT hardware dwell time.
    Cumulative mode carries past maxima across rows, but leaves gaps gray.
    """
    if cumulative and bin_seconds is not None:
        raise ValueError("Use raw sweeps for cumulative mode, or bin_seconds for window maxima")
    lo, hi = frequency_range
    f = data["frequency_hz"] / 1e6
    if lo >= hi or np.sum((f >= lo) & (f <= hi)) < 2:
        raise ValueError("Frequency limits must contain at least two samples")
    start, end = data["request_utc_ns"], data["read_complete_utc_ns"]
    if bin_seconds is None:
        power = data["power_dbm"]
        if cumulative:
            power = np.maximum.accumulate(power, axis=0)
        values = np.full((2*len(start)-1, len(f)), np.nan, dtype=np.float32)
        values[::2] = power
        edges = np.column_stack([start, end]).ravel()
    else:
        bins = bin_max(data, bin_seconds)
        edges, values = bins["time_edges_utc_ns"], bins["max_dbm"]
    if time_axis == "elapsed":
        y = (edges-start[0])/1e9
        ylabel = "Elapsed time (s)"
    elif time_axis in ("UTC", "KST"):
        y = mdates.date2num(edges.astype("datetime64[ns]"))
        ylabel = f"Time ({time_axis})"
    else:
        raise ValueError("time_axis must be elapsed, UTC, or KST")
    with plt.rc_context(STYLE):
        fig, ax = plt.subplots(figsize=(13, 6.5), layout="constrained")
        cmap = plt.get_cmap("viridis").copy()
        cmap.set_bad("#ededed")
        mesh = ax.pcolormesh(frequency_edges(data["frequency_hz"]), y,
                            np.ma.masked_invalid(values), cmap=cmap, shading="flat",
                            vmin=vmin, vmax=vmax, rasterized=True)
        ax.set(xlim=(lo, hi), ylim=(y[-1], y[0]), xlabel="Frequency (MHz)", ylabel=ylabel)
        zone = time_axis if time_axis in ("UTC", "KST") else "KST"
        if time_axis in ("UTC", "KST"):
            absolute_time_ticks(ax, edges, time_axis)
        ax.set_title(figure_title(data, title_text, zone, title_start_utc_ns), loc="left", fontsize=16, pad=14)
        cb = fig.colorbar(mesh, ax=ax, pad=0.025, fraction=0.04)
        cb.set_label("Power (dBm)", fontsize=20, labelpad=12)
        cb.ax.tick_params(labelsize=16)
        return fig, ax


def spectrum_plot(data, *, frequency_range=(1, 1000), time_zone="KST", title_text="", title_start_utc_ns=None):
    power = data["power_dbm"].astype(np.float64)
    maximum = power.max(axis=0)
    mean_peak = 10*np.log10(np.mean(10**(power/10), axis=0))
    with plt.rc_context(STYLE):
        fig, ax = plt.subplots(figsize=(13, 5), layout="constrained")
        ax.plot(data["frequency_hz"]/1e6, maximum, color="#b34b24", lw=1.5, label="Max Hold (sampled sweeps)")
        ax.plot(data["frequency_hz"]/1e6, mean_peak, color="#245b82", lw=1.3, label="Mean peak power (linear average)")
        ax.set(xlim=frequency_range, xlabel="Frequency (MHz)", ylabel="Power (dBm)")
        ax.set_title(figure_title(data, title_text, time_zone, title_start_utc_ns), loc="left", fontsize=16, pad=14)
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(axis="y", color="0.88", linewidth=0.7)
        ax.legend(frameon=False, loc="best")
        return fig, ax


def save_combined(data, path):
    """Save one rebuildable analysis product. Original CSV/NPZ are untouched."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {k: data[k] for k in ("site", "settings", "identity", "source_directories")}
    np.savez_compressed(path, **{k: data[k] for k in
        ("frequency_hz", "request_utc_ns", "read_complete_utc_ns", "power_dbm")},
        cumulative_max_dbm=np.maximum.accumulate(data["power_dbm"], axis=0),
        metadata_json=np.array(json.dumps(meta)))


def integrate_windows(data, seconds=10, *, start_utc_ns=None, stop_utc_ns=None):
    """Equal-weight linear-power mean of sampled positive-peak sweeps.

    Windows are [start, stop), selected by acquisition REQUEST time. A sweep
    which finishes across a boundary belongs wholly to its request window.
    This is neither time-continuous integration nor an RMS-detector average.
    Supply recording start/deadline for an exact requested-duration grid.
    """
    if not np.isfinite(seconds) or seconds < .001:
        raise ValueError("seconds must be finite and >= .001")
    step = int(seconds*1e9)
    times = np.asarray(data["request_utc_ns"], dtype=np.int64)
    power = np.asarray(data["power_dbm"])
    if len(times) == 0 or power.ndim != 2 or power.shape[0] != len(times):
        raise ValueError("Empty or mismatched input arrays")
    if not np.isfinite(power).all() or np.any(np.diff(times) <= 0):
        raise ValueError("Invalid values or unordered/duplicate timestamps")
    start = int(times[0] if start_utc_ns is None else start_utc_ns)
    stop = int(data["read_complete_utc_ns"][-1] if stop_utc_ns is None else stop_utc_ns)
    if start > times[0] or stop <= times[-1] or stop <= start:
        raise ValueError("Requested bounds must contain all acquisition requests")
    n = (stop-start+step-1)//step
    if n*power.shape[1] > 50_000_000:
        raise ValueError("Too many bins; select less data or increase window size")
    mean = np.full((n, power.shape[1]), np.nan, dtype=np.float32)
    maximum = mean.copy()
    count = np.zeros(n, dtype=np.int64)
    first_request = np.full(n, -1, dtype=np.int64)
    last_read = first_request.copy()
    ids = (times-start)//step
    for index in np.unique(ids):
        a, b = np.searchsorted(ids, [index, index+1])
        values = power[a:b].astype(np.float64)
        mean[index] = 10*np.log10(np.mean(10**(values/10), axis=0))
        maximum[index] = values.max(axis=0)
        count[index] = b-a
        first_request[index] = times[a]
        last_read[index] = data["read_complete_utc_ns"][b-1]
    edges = start + np.arange(n+1, dtype=np.int64)*step
    edges[-1] = stop
    return {"time_edges_utc_ns": edges, "mean_peak_power_dbm": mean,
            "max_dbm": maximum, "sweep_count": count,
            "first_request_utc_ns": first_request, "last_read_utc_ns": last_read,
            "nominal_window_seconds": np.float64(seconds)}


def integrated_plot(data, windows, *, metric="mean_peak_power_dbm",
                    frequency_range=(10, 1000), time_axis="KST", vmin=None, vmax=None,
                    title_text="", title_start_utc_ns=None):
    """Windowed waterfall with a left-aligned title and explicit mean/max label."""
    labels = {"mean_peak_power_dbm": "Mean peak power (dBm)",
              "max_dbm": "Max Hold power (dBm)"}
    if metric not in labels:
        raise ValueError("metric must be mean_peak_power_dbm or max_dbm")
    f = data["frequency_hz"]/1e6
    lo, hi = frequency_range
    if lo >= hi or np.sum((f >= lo) & (f <= hi)) < 2:
        raise ValueError("Frequency limits must contain at least two samples")
    edges = windows["time_edges_utc_ns"]
    if time_axis == "minutes":
        y, ylabel = (edges-edges[0])/60e9, "Elapsed time (min)"
    elif time_axis in ("UTC", "KST"):
        y, ylabel = mdates.date2num(edges.astype("datetime64[ns]")), f"Time ({time_axis})"
    else:
        raise ValueError("time_axis must be minutes, UTC, or KST")
    with plt.rc_context(STYLE):
        fig, ax = plt.subplots(figsize=(13, 6.5), layout="constrained")
        cmap = plt.get_cmap("viridis").copy()
        cmap.set_bad("#ededed")
        mesh = ax.pcolormesh(frequency_edges(data["frequency_hz"]), y,
            np.ma.masked_invalid(windows[metric]), cmap=cmap, shading="flat",
            vmin=vmin, vmax=vmax, rasterized=True)
        ax.set(xlim=(lo, hi), ylim=(y[-1], y[0]), xlabel="Frequency (MHz)", ylabel=ylabel)
        zone = time_axis if time_axis in ("UTC", "KST") else "KST"
        if time_axis in ("UTC", "KST"):
            absolute_time_ticks(ax, edges, time_axis)
        ax.set_title(figure_title(data, title_text, zone, title_start_utc_ns), loc="left", fontsize=16, pad=14)
        cb = fig.colorbar(mesh, ax=ax, pad=.025, fraction=.04)
        cb.set_label(labels[metric], fontsize=20, labelpad=12)
        cb.ax.tick_params(labelsize=16)
        return fig, ax
