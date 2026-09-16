"""Full-range native-801 acquisition plans; never interpolate amplitudes.

bin_size_hz is a maximum trace-coordinate spacing, not the RBW.
Optional npoints means exact OUTPUT points, never a SCPI point count.
"""
import math

import numpy as np

NATIVE_POINTS = 801
PLAN_SCHEMA = 'native801_planned_v2'
DEFAULT_PROTECTION = (50e6, 400e6)
MAX_SEGMENTS = 128


def _number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{label} must be a finite number')
    return float(value)


def _protected(c, start, stop):
    region = c.get('protection_region_hz', DEFAULT_PROTECTION)
    if region is None:
        return None
    if not isinstance(region, (list, tuple)) or len(region) != 2:
        raise ValueError('protection_region_hz must be [low_hz, high_hz] or null')
    lo, hi = (_number(v, 'protection endpoint') for v in region)
    if lo >= hi:
        raise ValueError('Invalid protection region')
    lo, hi = max(start, lo), min(stop, hi)
    return (lo, hi) if lo < hi else None


def _feasible(start, stop, protection, count, width, slot):
    lo, hi = protection
    right_count = count-slot-1
    x_max = min(lo, start+slot*width)
    y_min = max(hi, stop-right_count*width)
    return y_min-x_max <= width + 1e-6


def _auto_edges(start, stop, limit, protection):
    # Minimize sweep count first, then the largest span. Without protection
    # conflicts this is exactly the equal-width partition of the full range.
    count0 = max(1, math.ceil((stop-start)/limit-1e-12))
    if count0 > MAX_SEGMENTS:
        raise ValueError(f'Plan exceeds {MAX_SEGMENTS} sweeps per row')
    if protection is None:
        return np.linspace(start, stop, count0+1)
    for count in range(count0, min(MAX_SEGMENTS, count0+2)+1):
        candidates = []
        for slot in range(count):
            if not _feasible(start, stop, protection, count, limit, slot):
                continue
            low = max((stop-start)/count, protection[1]-protection[0])
            high = limit
            for _ in range(55):
                mid = (low+high)/2
                if _feasible(start, stop, protection, count, mid, slot):
                    high = mid
                else:
                    low = mid
            candidates.append((high, slot))
        if not candidates:
            continue
        width, slot = min(candidates)
        width = min(limit, width+2e-6)
        lo, hi = protection
        right_count = count-slot-1
        y_min = max(hi, stop-right_count*width)
        x_low, x_high = max(start, y_min-width), min(lo, start+slot*width)
        x = min(max(start+slot*(stop-start)/count, x_low), x_high)
        if slot == 0:
            x = start
        y = min(max(start+(slot+1)*(stop-start)/count, y_min), x+width, stop)
        if right_count == 0:
            y = stop
        return np.r_[np.linspace(start, x, slot+1),
                     np.linspace(y, stop, right_count+1)]
    raise ValueError('Cannot partition range while preserving protection region')


def make_plan(c):
    start, stop = (_number(c[k], k) for k in ('start_hz', 'stop_hz'))
    requested = _number(c['bin_size_hz'], 'bin_size_hz')
    if not 9000 <= start < stop <= 3.6e9 or requested <= 0:
        raise ValueError('Invalid frequency range or bin_size_hz')
    protection = _protected(c, start, stop)
    if protection and protection[1]-protection[0] > 800*requested+1e-6:
        minimum = (protection[1]-protection[0])/800
        raise ValueError(f'Protection region requires bin_size_hz >= {minimum:g}; '
                         'cannot silently split protection or relax bin size (RBW is independent)')
    n = c.get('npoints')
    segments = []
    if n is None:
        edges = _auto_edges(start, stop, 800*requested, protection)
        for i, (a, b) in enumerate(zip(edges[:-1], edges[1:])):
            # Keep a boundary belonging to the protected interval in its own
            # protected sweep, even if that sweep is not the first one.
            owns_protection = protection is not None and a <= protection[0]+1e-6 and b >= protection[1]-1e-6
            keep_from = 0 if i == 0 or owns_protection else 1
            if i and keep_from == 0:
                segments[-1]['keep_to'] = 800
            segments.append(dict(start_hz=float(a), stop_hz=float(b),
                                 npoints=801, keep_from=keep_from, keep_to=801))
        mode = 'auto_max_bin'
    else:
        if isinstance(n, bool) or not isinstance(n, int) or n < 801:
            raise ValueError('Exact output npoints must be an integer >= 801; omit it for automatic planning')
        if n > MAX_SEGMENTS*800+1:
            raise ValueError('Exact output npoints exceeds plan size limit')
        step = (stop-start)/(n-1)
        if step > requested+1e-6:
            raise ValueError(f'npoints={n} needs {step:g} Hz bins, larger than bin_size_hz={requested:g}; '
                             'omit npoints or increase it; frequency range is never truncated')
        if protection and protection[1]-protection[0] > 800*step+1e-6:
            raise ValueError('Exact npoints grid cannot contain the protection region in one native sweep')
        first = 0
        p0 = math.floor((protection[0]-start)/step+1e-10) if protection else None
        p1 = math.ceil((protection[1]-start)/step-1e-10) if protection else None
        if protection and p1-p0 > 800:
            raise ValueError('Exact grid alignment cannot preserve protection; use automatic mode')
        while first < n:
            left = min(first, n-801)
            if protection and first >= p0 and first <= p1:
                left = min(p0, n-801)
            last = min(left+800, n-1)
            if protection and left <= p0 and p0 < last < p1:
                last = p0-1
            if last < first:
                raise ValueError('Exact grid cannot preserve protection without losing samples')
            segments.append(dict(start_hz=start+left*step, stop_hz=start+(left+800)*step,
                                 npoints=801, keep_from=first-left, keep_to=last-left+1))
            first = last+1
        mode = 'exact_output_points'
    output = 0
    for part in segments:
        part['output_start_index'] = output
        output += part['keep_to']-part['keep_from']
        part['output_stop_index'] = output
        part['bin_spacing_hz'] = (part['stop_hz']-part['start_hz'])/800
    plan = dict(mode=mode, segments=segments, requested_bin_size_hz=requested,
                protection_region_hz=list(protection) if protection else None,
                output_npoints=output, frequency_axis_schema=PLAN_SCHEMA)
    f = frequency_axis(plan)
    if not np.all(np.diff(f) > 0) or np.max(np.diff(f)) > requested+1e-5:
        raise ValueError('Invalid planned frequency spacing')
    if not math.isclose(f[0], start, abs_tol=1e-5, rel_tol=0) or not math.isclose(f[-1], stop, abs_tol=1e-5, rel_tol=0):
        raise ValueError('Plan must preserve both frequency endpoints')
    if n is not None and len(f) != n:
        raise ValueError('Plan did not produce the exact requested output count')
    if protection and not any(p['start_hz'] <= protection[0]+1e-6 and p['stop_hz'] >= protection[1]-1e-6 for p in segments):
        raise ValueError('Protection region not contained in a single sweep')
    return plan


def frequency_axis(plan):
    return np.concatenate([np.linspace(p['start_hz'], p['stop_hz'], 801)
                           [p['keep_from']:p['keep_to']] for p in plan['segments']])


def segment_config(c, part):
    return dict(c, start_hz=part['start_hz'], stop_hz=part['stop_hz'], npoints=801)
