#!/usr/bin/env python3
"""
Tape speed & splice audit console - backend.

Pure standard-library WSGI server.  PCM/IEEE-float WAV is handled with the
``wave`` module (fmt-chunk parsed by hand to recognise 24-bit PCM and 32-bit
float), persistence with ``sqlite3``.  Uploaded WAVs are never modified:
every correction is written as a new revision.
"""

import csv
import io
import json
import math
import os
import re
import sqlite3
import struct
import threading
import time
import wave
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
STATIC_DIR = os.path.join(BASE_DIR, "static")
DB_PATH = os.path.join(DATA_DIR, "audit.db")

DEFAULT_TONE_HZ = 1000.0
DEFAULT_SPEED_JUMP = 0.04          # 4 % ratio change between adjacent anchors
DEFAULT_MAX_GAP_S = 120.0          # gap longer than this lacks coverage
FFT_BINS_CAP = 65536               # keeps the pure-python FFT responsive

_tls = threading.local()


# --------------------------------------------------------------------------
# tiny utilities
# --------------------------------------------------------------------------

def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def short_id(prefix):
    return prefix + os.urandom(5).hex()


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


# --------------------------------------------------------------------------
# sqlite
# --------------------------------------------------------------------------

def db():
    conn = getattr(_tls, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _tls.conn = conn
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    state       TEXT NOT NULL,
    wav_path    TEXT,
    wav_name    TEXT,
    wav_sha     TEXT,
    log_path    TEXT,
    log_name    TEXT,
    log_text    TEXT
);
CREATE TABLE IF NOT EXISTS revisions (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL,
    parent_id   TEXT,
    note        TEXT,
    wav_path    TEXT,
    csv_path    TEXT,
    json_path   TEXT,
    manifest    TEXT NOT NULL
);
"""


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = db()
    conn.executescript(SCHEMA)
    conn.commit()


# --------------------------------------------------------------------------
# WAV reading (PCM 8/16/24/32 and IEEE float32)
# --------------------------------------------------------------------------

def read_wav_info(path):
    with wave.open(path, "rb") as wf:
        nch = wf.getnchannels()
        sw = wf.getsampwidth()
        fr = wf.getframerate()
        nframes = wf.getnframes()
    # wave accepts only uncompressed PCM; float files need manual inspection
    fmt_code = 1
    with open(path, "rb") as f:
        f.seek(12)
        while True:
            header = f.read(8)
            if len(header) < 8:
                break
            cid, size = struct.unpack("<4sI", header)
            if cid == b"fmt ":
                fmt = f.read(size)
                fmt_code = struct.unpack("<H", fmt[0:2])[0]
                break
            f.seek(size + (size & 1), 1)
    if fmt_code not in (1, 3):
        raise ValueError("unsupported WAV format code %d (only PCM / IEEE float)" % fmt_code)
    return {"channels": nch, "sample_width": sw, "frame_rate": fr,
            "n_frames": nframes, "format_code": fmt_code,
            "duration": nframes / float(fr)}


def _decode_bytes(raw, sw, fmt_code):
    """raw interleaved byte string -> list of per-channel float lists."""
    if sw == 1:                                  # 8-bit PCM is unsigned
        data = [(b - 128) / 128.0 for b in raw]
    elif sw == 2:
        import array
        a = array.array("h")
        a.frombytes(raw)
        if sys_byteorder != "little":
            a.byteswap()
        data = [x / 32768.0 for x in a]
    elif sw == 3:
        n = len(raw) // 3
        data = [0.0] * n
        for i in range(n):
            b0, b1, b2 = raw[3 * i], raw[3 * i + 1], raw[3 * i + 2]
            v = b0 | (b1 << 8) | (b2 << 16)
            if v & 0x800000:
                v -= 1 << 24
            data[i] = v / 8388608.0
    elif sw == 4 and fmt_code == 3:
        import array
        a = array.array("f")
        a.frombytes(raw)
        if sys_byteorder != "little":
            a.byteswap()
        data = list(a)
    elif sw == 4:
        import array
        a = array.array("i")
        a.frombytes(raw)
        if sys_byteorder != "little":
            a.byteswap()
        data = [x / 2147483648.0 for x in a]
    else:
        raise ValueError("unsupported sample width %d" % sw)
    return data


import sys
sys_byteorder = sys.byteorder


def read_wav_frames(path, info, start_frame, count):
    """Return list[channels] of float lists for [start_frame, start+count)."""
    with wave.open(path, "rb") as wf:
        wf.setpos(max(0, start_frame))
        raw = wf.readframes(count)
    nch, sw = info["channels"], info["sample_width"]
    data = _decode_bytes(raw, sw, info["format_code"])
    frames = len(data) // nch
    out = [[] for _ in range(nch)]
    for c in range(nch):
        ch = out[c]
        ch.extend([data[i * nch + c] for i in range(frames)])
    return out


def downmix(channels):
    if len(channels) == 1:
        return channels[0]
    n = min(len(c) for c in channels)
    return [sum(c[i] for c in channels) / len(channels) for i in range(n)]


# --------------------------------------------------------------------------
# FFT + calibration-tone analysis (pure python, radix-2)
# --------------------------------------------------------------------------

def fft(re, im):
    n = len(re)
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j ^= bit
        if i < j:
            re[i], re[j] = re[j], re[i]
            im[i], im[j] = im[j], im[i]
    length = 2
    while length <= n:
        half = length >> 1
        ang = -2.0 * math.pi / length
        wlr, wli = math.cos(ang), math.sin(ang)
        for i in range(0, n, length):
            wr, wi = 1.0, 0.0
            for k in range(half):
                a = i + k
                b = a + half
                tr = wr * re[b] - wi * im[b]
                ti = wr * im[b] + wi * re[b]
                re[b] = re[a] - tr
                im[b] = im[a] - ti
                re[a] += tr
                im[a] += ti
                nwr = wr * wlr - wi * wli
                wi = wr * wli + wi * wlr
                wr = nwr
        length <<= 1


def analyze_tone(path, info, center_s, win_s, nominal_hz,
                 band_hz, max_candidates=5):
    """Estimate tone frequency around ``center_s``.

    Hann-windowed FFT, magnitude peaks within nominal +/- band_hz, parabolic
    bin refinement.  Returns candidates [{hz, level_db, rms, deviation,
    ambiguous}] plus diagnostic flags.
    """
    fr = info["frame_rate"]
    n = int(win_s * fr)
    if n < 256:
        raise ValueError("analysis window too short")
    start = clamp(int(center_s * fr - n / 2), 0, max(0, info["n_frames"] - n))
    n = min(n, info["n_frames"] - start)
    chans = read_wav_frames(path, info, start, n)
    x = downmix(chans)

    rms = math.sqrt(sum(v * v for v in x) / max(1, len(x)))
    if rms < 1e-6:
        return {"candidates": [], "flags": ["silence"], "rms": 0.0,
                "window_start_s": start / fr, "window_end_s": (start + n) / fr}

    # Hann window
    xw = [x[i] * (0.5 - 0.5 * math.cos(2 * math.pi * i / (n - 1)))
          for i in range(n)]
    nfft = 1
    while nfft < n:
        nfft <<= 1
    capped = nfft > FFT_BINS_CAP
    nfft = min(nfft, FFT_BINS_CAP)
    re = (xw[:nfft] if capped else xw) + [0.0] * max(0, nfft - min(n, nfft))
    im = [0.0] * nfft
    t0 = time.time()
    fft(re, im)
    fft_ms = (time.time() - t0) * 1000.0

    bin_hz = fr / nfft
    lo = max(1, int((nominal_hz - band_hz) / bin_hz))
    hi = min(nfft // 2, int((nominal_hz + band_hz) / bin_hz))
    mag = [math.hypot(re[k], im[k]) for k in range(lo, hi + 1)]
    if not mag:
        return {"candidates": [], "flags": ["out_of_band"], "rms": rms,
                "window_start_s": start / fr, "window_end_s": (start + n) / fr,
                "fft_ms": fft_ms}
    peak = max(mag)
    floor = peak * 0.30
    peaks = []
    m = len(mag)
    for i in range(1, m - 1):
        if mag[i] > mag[i - 1] and mag[i] >= mag[i + 1] and mag[i] >= floor:
            idx = lo + i
            # parabolic interpolation on log magnitude
            y0, y1, y2 = mag[i - 1], mag[i], mag[i + 1]
            denom = (y0 - 2 * y1 + y2)
            delta = 0.5 * (y0 - y2) / denom if denom else 0.0
            hz = (idx + delta) * bin_hz
            level = 20 * math.log10(mag[i] / peak + 1e-12)
            peaks.append({"hz": hz, "level_db": level,
                          "rms_ratio": mag[i] / peak})
    peaks.sort(key=lambda p: -p["rms_ratio"])
    cands = peaks[:max_candidates]
    for p in cands:
        p["ratio"] = p["hz"] / nominal_hz
        p["deviation"] = p["ratio"] - 1.0
    flags = []
    if not cands:
        flags.append("no_tone_found")
    else:
        within = [p for p in cands if abs(p["deviation"]) <= 0.12]
        if not within:
            flags.append("out_of_range")
        # ambiguity: a second peak within 6 dB and separated by > 2 Hz
        if len(cands) > 1 and cands[1]["level_db"] > -6.0 \
                and abs(cands[1]["hz"] - cands[0]["hz"]) > 2.0:
            flags.append("ambiguous_candidates")
            for p in cands:
                p["ambiguous"] = True
        else:
            cands[0]["ambiguous"] = False
    return {"candidates": cands, "flags": flags, "rms": rms,
            "window_start_s": start / fr, "window_end_s": (start + n) / fr,
            "fft_ms": fft_ms, "fft_size": nfft, "bin_hz": bin_hz}


# --------------------------------------------------------------------------
# reel log parsing (JSON, or forgiving line format)
# --------------------------------------------------------------------------

TC_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2}(?:\.\d+)?)$")


def parse_timecode(tc, fps=25):
    """'MM:SS.s' / 'HH:MM:SS.s' / frames ('FFF@25') -> seconds."""
    if tc is None:
        return None
    if isinstance(tc, (int, float)):
        return float(tc)
    s = str(tc).strip()
    m = TC_RE.match(s)
    if m:
        h, mm, ss = m.groups()
        return (int(h or 0)) * 3600 + int(mm) * 60 + float(ss)
    m = re.match(r"^(\d+(?:\.\d+)?)\s*@\s*(\d+(?:\.\d+)?)?$", s)
    if m:
        frames = float(m.group(1))
        rate = float(m.group(2) or fps)
        return frames / rate
    try:
        return float(s)
    except ValueError:
        return None


def format_smpte(seconds, fps=25):
    if seconds is None:
        return None
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    sec = int(seconds % 60)
    fr = int(round((seconds - int(seconds)) * fps))
    if fr >= fps:
        fr = 0
        sec += 1
    return "%02d:%02d:%02d:%02d" % (h, m, sec, fr)


def parse_reel_log(text):
    """Accept JSON or a forgiving text format. Returns dict with
    anchors[] (pos_s, tone_hz, side, label) and splices[] (at_s, side,
    tc_in, tc_out, label)."""
    text = text.strip()
    anchors, splices = [], []
    meta = {}
    if not text:
        return {"meta": meta, "anchors": anchors, "splices": splices}
    try:
        doc = json.loads(text)
    except (ValueError, TypeError):
        doc = None
    if isinstance(doc, dict):
        meta = {k: v for k, v in doc.items()
                if k not in ("anchors", "splices")}
        fps = float(doc.get("fps", 25))
        for a in doc.get("anchors", []) or []:
            pos = parse_timecode(a.get("pos_s", a.get("at", a.get("time"))), fps)
            if pos is None:
                continue
            anchors.append({"pos_s": pos,
                            "tone_hz": float(a.get("tone_hz", a.get("hz", DEFAULT_TONE_HZ))),
                            "side": str(a.get("side", "A")),
                            "label": a.get("label", "")})
        for s in doc.get("splices", []) or []:
            at = parse_timecode(s.get("at_s", s.get("at", s.get("time"))), fps)
            if at is None:
                continue
            splices.append({"at_s": at, "side": str(s.get("side", "")),
                            "tc_in": s.get("tc_in"), "tc_out": s.get("tc_out"),
                            "tc_in_s": parse_timecode(s.get("tc_in"), fps),
                            "tc_out_s": parse_timecode(s.get("tc_out"), fps),
                            "label": s.get("label", "")})
        return {"meta": meta, "anchors": anchors, "splices": splices}

    # forgiving text: `tone 12.4 hz=1000 side=A label=...`
    # `splice 300 side=A tcin=00:10 tcout=00:12 label=...`
    fps = 25.0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        kind = parts[0].lower()
        fields = {}
        posword = None
        for p in parts[1:]:
            if "=" in p:
                k, v = p.split("=", 1)
                fields[k.lower()] = v
            elif posword is None:
                posword = p
        pos = parse_timecode(posword, fps)
        if pos is None:
            continue
        if kind in ("tone", "anchor", "cal", "calibration"):
            anchors.append({"pos_s": pos,
                            "tone_hz": float(fields.get("hz", fields.get("tone_hz", DEFAULT_TONE_HZ))),
                            "side": fields.get("side", "A"),
                            "label": fields.get("label", "")})
        elif kind in ("splice", "join", "cut"):
            splices.append({"at_s": pos, "side": fields.get("side", ""),
                            "tc_in": fields.get("tcin", fields.get("tc_in")),
                            "tc_out": fields.get("tcout", fields.get("tc_out")),
                            "tc_in_s": parse_timecode(fields.get("tcin", fields.get("tc_in")), fps),
                            "tc_out_s": parse_timecode(fields.get("tcout", fields.get("tc_out")), fps),
                            "label": fields.get("label", "")})
    return {"meta": meta, "anchors": anchors, "splices": splices}


# --------------------------------------------------------------------------
# validation + monotone timecode mapping
# --------------------------------------------------------------------------

# issue severities: "block" => unconfirmed unless adopted with a reason
ISSUE_META = {
    "ambiguous_tone": ("block", "校准音候选不唯一 / ambiguous calibration tone"),
    "out_of_range": ("block", "测得频率远超校准范围 / frequency outside ±12%"),
    "no_tone_found": ("block", "未找到校准音 / no calibration tone found"),
    "silence": ("block", "分析窗为静音 / silent analysis window"),
    "anchor_order": ("block", "锚点次序相悖（面别/时码不单调）"),
    "speed_jump": ("block", "相邻锚点走速骤变 / abrupt speed change"),
    "splice_overlap": ("block", "接带区时码重叠 / spliced timecodes overlap"),
    "coverage_gap": ("warn", "该段超出校准覆盖/间隔过长 / beyond calibration coverage"),
    "uncovered_edge": ("warn", "卷首或卷尾缺少校准音 / uncalibrated edge"),
    "unanalyzed": ("warn", "锚点尚未分析 / anchor not analyzed"),
    "splice_near_jump": ("info", "接带点与走速骤变重合"),
}


def _anchor_ratio(a):
    """authoritative playback ratio adopted for an anchor (or None)."""
    if not a.get("analysis"):
        return None
    chosen = a.get("chosen_index")
    cands = a["analysis"].get("candidates") or []
    if not cands:
        return None
    if chosen is None or chosen < 0 or chosen >= len(cands):
        chosen = 0
    return cands[chosen].get("ratio")


def compute_validation(state, duration):
    """Return (issues, anchors sorted by pos).  Issues carry evidence ranges
    (start_s/end_s in source time) and a per-issue adoption reason."""
    params = state.get("params", {})
    jump_limit = float(params.get("speed_jump_limit", DEFAULT_SPEED_JUMP))
    max_gap = float(params.get("max_gap_s", DEFAULT_MAX_GAP_S))
    reasons = dict(state.get("adoptions", {}))  # issue_key -> reason

    anchors = list(state.get("anchors", []))
    anchors.sort(key=lambda a: a["pos_s"])
    splices = sorted(state.get("splices", []), key=lambda s: s["at_s"])

    issues = []

    def add(code, start, end, refs=None, detail=None):
        key = "%s:%s" % (code, ",".join(refs or []))
        sev, msg = ISSUE_META[code]
        iss = {"code": code, "severity": sev, "message": msg,
               "start_s": max(0.0, start), "end_s": min(duration, end),
               "refs": refs or [], "key": key, "detail": detail}
        reason = (reasons.get(key) or "").strip()
        iss["adopted"] = bool(reason)
        iss["reason"] = reason
        issues.append(iss)
        return iss

    # ---- per-anchor analysis flags
    for a in anchors:
        pos = a["pos_s"]
        win = float(a.get("window_s", 1.0))
        if not a.get("analysis"):
            add("unanalyzed", pos - win / 2, pos + win / 2, refs=[a["id"]])
            continue
        flags = a["analysis"].get("flags") or []
        for f in flags:
            if f in ISSUE_META:
                add(f, a["analysis"].get("window_start_s", pos - win / 2),
                    a["analysis"].get("window_end_s", pos + win / 2),
                    refs=[a["id"]],
                    detail="candidates=%s" %
                    [round(c["hz"], 2) for c in a["analysis"].get("candidates", [])])
        # chosen candidate marked ambiguous explicitly
        ci = a.get("chosen_index", 0) or 0
        cands = a["analysis"].get("candidates") or []
        if cands and ci < len(cands) and cands[ci].get("ambiguous"):
            # already covered by ambiguous_tone flag
            pass

    # ---- side / order monotonicity on the REEL order (the list order in
    # which the archivist / reel log supplied the anchors): sides may only
    # advance A->B once, and reel-time positions must be non-decreasing.
    reel_order = list(state.get("anchors", []))
    seen_b = False
    prev = None
    for a in reel_order:
        side = (a.get("side") or "A").upper()
        if seen_b and not side.startswith("B"):
            add("anchor_order",
                min(prev["pos_s"], a["pos_s"]),
                max(prev["pos_s"], a["pos_s"]),
                refs=[prev["id"], a["id"]],
                detail="side %s appears after side B" % side)
        if side.startswith("B"):
            seen_b = True
        if prev is not None and a["pos_s"] < prev["pos_s"]:
            add("anchor_order", a["pos_s"] - 0.5, a["pos_s"] + 0.5,
                refs=[prev["id"], a["id"]],
                detail="reel-order position %.3f after %.3f"
                       % (a["pos_s"], prev["pos_s"]))
        prev = a

    # ---- speed jumps
    ratios = [(a, _anchor_ratio(a)) for a in anchors]
    for i in range(1, len(ratios)):
        a0, r0 = ratios[i - 1]
        a1, r1 = ratios[i]
        if r0 and r1 and abs(r1 - r0) > jump_limit:
            iss = add("speed_jump", a0["pos_s"], a1["pos_s"],
                      refs=[a0["id"], a1["id"]],
                      detail="ratio %.4f -> %.4f (Δ %.2f%%)" %
                             (r0, r1, (r1 - r0) * 100))
            # annotate a splice coinciding with the jump (within 1 s)
            for s in splices:
                if a0["pos_s"] <= s["at_s"] <= a1["pos_s"]:
                    add("splice_near_jump", s["at_s"] - 0.5, s["at_s"] + 0.5,
                        refs=[s.get("id", "")],
                        detail="splice inside jump interval")
                    break

    # ---- calibration coverage
    if anchors:
        if anchors[0]["pos_s"] > max_gap:
            add("uncovered_edge", 0.0, anchors[0]["pos_s"],
                detail="leader without calibration")
        if duration - anchors[-1]["pos_s"] > max_gap:
            add("uncovered_edge", anchors[-1]["pos_s"], duration,
                detail="tail without calibration")
        for i in range(1, len(anchors)):
            if anchors[i]["pos_s"] - anchors[i - 1]["pos_s"] > max_gap:
                add("coverage_gap", anchors[i - 1]["pos_s"],
                    anchors[i]["pos_s"],
                    refs=[anchors[i - 1]["id"], anchors[i]["id"]])

    # ---- splice timecode overlap:
    # each splice states the reel-time interval it joins; two splice
    # intervals on the same side claiming overlapping reel time mean two
    # physical sections occupy the same timecode.
    claimed = []
    for s in splices:
        tin, tout = s.get("tc_in_s"), s.get("tc_out_s")
        if tin is not None and tout is not None:
            lo, hi = min(tin, tout), max(tin, tout)
            for other in claimed:
                # same side (or unknown side on either) overlaps
                sside = (s.get("side") or "").upper()
                oside = (other.get("side") or "").upper()
                if (not sside or not oside or sside == oside) \
                        and lo < other["hi"] and other["lo"] < hi:
                    add("splice_overlap",
                        min(s["at_s"], other["at_s"]) - 0.5,
                        max(s["at_s"], other["at_s"]) + 0.5,
                        refs=[s.get("id", ""), other.get("id", "")],
                        detail="%s [%s,%s] vs [%s,%s]" %
                               (sside or "?", format_smpte(lo), format_smpte(hi),
                                format_smpte(other["lo"]), format_smpte(other["hi"])))
            claimed.append({"lo": lo, "hi": hi, "at_s": s["at_s"],
                            "side": s.get("side", ""), "id": s.get("id", "")})

    # ---- suspected droop ("wow") regions declared by the archivist:
    # if a marked region has no anchor inside, it cannot be confirmed.
    for z in state.get("suspect_zones", []):
        inside = [a for a in anchors if z["start_s"] <= a["pos_s"] <= z["end_s"]]
        if not inside:
            add("coverage_gap", z["start_s"], z["end_s"],
                refs=[z.get("id", "")],
                detail="suspect zone without calibration anchor")

    # dedupe identical keys, merge refs
    merged = {}
    for iss in issues:
        if iss["key"] in merged:
            m = merged[iss["key"]]
            m["start_s"] = min(m["start_s"], iss["start_s"])
            m["end_s"] = max(m["end_s"], iss["end_s"])
            m["refs"] = sorted(set(m["refs"] + iss["refs"]))
        else:
            merged[iss["key"]] = iss
    out = list(merged.values())
    out.sort(key=lambda i: i["start_s"])
    return out, anchors


def build_segments(state, duration, issues):
    """Piecewise-linear source->corrected mapping.

    Calibration measured at captured pitch: ratio r = measured/nominal.
    Because the captured file plays slow by 1/r (capture t = real t * r),
    the corrected timeline grows by r: corrected interval = source interval
    * k, k = sqrt(r_{i-1}*r_i).  Resampling with ratio k both restores the
    original duration and pitch.
    Returns segments with resample ratio, confirmed flag and reasons.
    """
    anchors = list(state.get("anchors", []))
    anchors.sort(key=lambda a: a["pos_s"])
    usable = [(a, _anchor_ratio(a)) for a in anchors if _anchor_ratio(a) is not None]

    active_blocks = set()
    for iss in issues:
        if iss["severity"] == "block" and not iss["adopted"]:
            for r in iss["refs"]:
                active_blocks.add(r)
            # speed_jump ref anchors bound the interval -> both block it
            if iss["code"] == "speed_jump":
                active_blocks.update(iss["refs"])

    segs = []

    def seg_status(start, end, ref_ids):
        hit = [i for i in issues
               if i["severity"] == "block" and not i["adopted"]
               and i["end_s"] > start and i["start_s"] < end]
        unconfirmed = bool(hit)
        return unconfirmed, hit

    boundaries = [0.0] + [a["pos_s"] for a, _ in usable] + [duration]
    boundaries = sorted(set(b for b in boundaries if 0.0 <= b <= duration))

    # cumulative corrected time at each usable anchor
    corr_t = {}
    if usable:
        t = usable[0][0]["pos_s"]
        corr_t[usable[0][0]["id"]] = t
        for i in range(1, len(usable)):
            (a0, r0), (a1, r1) = usable[i - 1], usable[i]
            k = math.sqrt(max(1e-6, r0) * max(1e-6, r1))
            t += (a1["pos_s"] - a0["pos_s"]) * k
            corr_t[a1["id"]] = t

    for bi in range(len(boundaries) - 1):
        s, e = boundaries[bi], boundaries[bi + 1]
        if e - s < 1e-9:
            continue
        # find usable anchors around this interval
        before = [u for u in usable if u[0]["pos_s"] <= s + 1e-9]
        after = [u for u in usable if u[0]["pos_s"] >= e - 1e-9]
        if before and after:
            a0, r0 = before[-1]
            a1, r1 = after[0]
            if a1["pos_s"] - a0["pos_s"] > 1e-9:
                k = math.sqrt(r0 * r1)
            else:
                k = r0
            ratio = k
            confirmed = not (a0["id"] in active_blocks or a1["id"] in active_blocks)
            refs = [a0["id"], a1["id"]]
            mode = "resample"
        else:
            ratio = 1.0
            confirmed = False
            refs = []
            mode = "passthrough_uncovered"
        unconf, hits = seg_status(s, e, refs)
        confirmed = confirmed and not unconf
        segs.append({
            "start_s": s, "end_s": e,
            "ratio": ratio, "mode": mode,
            "confirmed": confirmed,
            "refs": refs,
            "blocking_issues": [h["key"] for h in hits],
            "corrected_start_s": None, "corrected_end_s": None,
        })

    # assign corrected times to segments
    if segs:
        t = 0.0
        if usable:
            first_a_pos = usable[0][0]["pos_s"]
            # leader length shrunk: no ratio known, keep raw length
            t = first_a_pos
        for sg in segs:
            sg["corrected_start_s"] = t
            t += (sg["end_s"] - sg["start_s"]) * sg["ratio"]
            sg["corrected_end_s"] = t
    return segs, usable


# --------------------------------------------------------------------------
# revision building: resample only affected intervals
# --------------------------------------------------------------------------

def resample_channel(ch, ratio, n_out):
    """Linear interpolation; ch sampled at source frame positions,
    output frame i reads source position i/ratio."""
    n = len(ch)
    out = [0.0] * n_out
    if n == 0:
        return out
    last = n - 1
    for i in range(n_out):
        pos = i / ratio
        i0 = int(pos)
        if i0 >= last:
            out[i] = ch[last]
        else:
            frac = pos - i0
            out[i] = ch[i0] * (1 - frac) + ch[i0 + 1] * frac
    return out


def encode_samples(channels, sw):
    """interleave float channels -> little-endian PCM bytes (16-bit out)."""
    import array
    n = min(len(c) for c in channels)
    if sw == 2:
        buf = array.array("h", [0]) * (n * len(channels))
        k = 0
        for i in range(n):
            for c in channels:
                v = int(clamp(c[i], -1.0, 1.0) * 32767.0)
                buf[k] = v
                k += 1
        if sys_byteorder != "little":
            buf.byteswap()
        return buf.tobytes()
    raise ValueError("output writer supports 16-bit PCM")


def build_revision(project, conn, note, parent_id):
    state = json.loads(project["state"])
    wav_path = project["wav_path"]
    info = read_wav_info(wav_path)
    duration = info["n_frames"] / info["frame_rate"]
    issues, _ = compute_validation(state, duration)
    segments, usable = build_segments(state, duration, issues)

    rev_id = short_id("r")
    rev_dir = os.path.join(DATA_DIR, project["id"], rev_id)
    os.makedirs(rev_dir, exist_ok=True)
    out_wav = os.path.join(rev_dir, "corrected.wav")
    out_csv = os.path.join(rev_dir, "timecode_map.csv")
    out_json = os.path.join(rev_dir, "revision.json")

    # incremental resampling: only intervals touched by this revision are
    # rendered.  Because corrected positions are cumulative, segments may be
    # byte-copied from the parent WAV only while the *prefix* of unchanged
    # segments continues from time 0; at the first changed interval every
    # subsequent segment must be re-rendered.
    parent_segs = []
    parent_manifest = None
    if parent_id:
        row = conn.execute("SELECT manifest FROM revisions WHERE id=?",
                           (parent_id,)).fetchone()
        if row:
            parent_manifest = json.loads(row["manifest"])
            parent_segs = parent_manifest["segments"]

    reused, rendered = [], []
    fr = info["frame_rate"]
    prefix_broken = parent_manifest is None
    with wave.open(out_wav, "wb") as wf:
        wf.setnchannels(info["channels"])
        wf.setsampwidth(2)
        wf.setframerate(fr)
        for idx, sg in enumerate(segments):
            key = (round(sg["start_s"], 6), round(sg["end_s"], 6))
            old = None if prefix_broken or idx >= len(parent_segs) \
                else parent_segs[idx]
            same = (old is not None
                    and (round(old["start_s"], 6), round(old["end_s"], 6)) == key
                    and abs(old["ratio"] - sg["ratio"]) < 1e-9
                    and old["mode"] == sg["mode"])
            if same:
                # identical prefix: corrected offsets coincide with parent's
                ppath = parent_manifest["output"]["wav_path"]
                f0 = int(round(old["corrected_start_s"] * fr))
                f1 = int(round(old["corrected_end_s"] * fr))
                _copy_wav_span(ppath, wf, f0, min(f1, _wav_nframes(ppath)),
                               info["channels"])
                sg["source"] = "reused"
                reused.append(key)
                continue
            prefix_broken = True
            f0 = int(round(sg["start_s"] * fr))
            f1 = min(info["n_frames"], int(round(sg["end_s"] * fr)))
            count = f1 - f0
            if count <= 0:
                sg["source"] = "empty"
                continue
            data = read_wav_frames(wav_path, info, f0, count)
            n_out = int(round(count * sg["ratio"]))
            if sg["mode"] == "resample" and abs(sg["ratio"] - 1.0) > 1e-9:
                data = [resample_channel(c, sg["ratio"], n_out) for c in data]
            wf.writeframes(encode_samples(data, 2))
            sg["source"] = "rendered"
            rendered.append(key)

    # CSV: timecode mapping rows
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["src_start_s", "src_end_s", "corr_start_s", "corr_end_s",
                    "resample_ratio", "mode", "confirmed", "anchor_refs",
                    "blocking_issues"])
        for sg in segments:
            w.writerow(["%.6f" % sg["start_s"], "%.6f" % sg["end_s"],
                        "%.6f" % (sg["corrected_start_s"] or 0.0),
                        "%.6f" % (sg["corrected_end_s"] or 0.0),
                        "%.8f" % sg["ratio"], sg["mode"],
                        "yes" if sg["confirmed"] else "no",
                        ";".join(sg["refs"]),
                        ";".join(sg["blocking_issues"])])
        # anchor rows for direct lookup
        anchor_corr_t = {}
        for sg in segments:
            for a, _ in usable:
                if abs(sg["end_s"] - a["pos_s"]) < 1e-6:
                    anchor_corr_t[a["id"]] = sg["corrected_end_s"]
        w.writerow([])
        w.writerow(["# anchor_id", "src_s", "side", "nominal_hz",
                    "measured_hz", "ratio", "corrected_s", "label"])
        for a, r in usable:
            w.writerow([a["id"], "%.6f" % a["pos_s"], a.get("side", ""),
                        a.get("tone_hz", DEFAULT_TONE_HZ),
                        "%.4f" % (a["tone_hz"] * r) if r else "",
                        "%.8f" % r if r else "",
                        "%.6f" % anchor_corr_t.get(a["id"], 0.0),
                        a.get("label", "")])

    n_conf = sum(1 for s in segments if s["confirmed"])
    fully_confirmed = bool(segments) and n_conf == len(segments)
    manifest = {
        "revision_id": rev_id,
        "project_id": project["id"],
        "created_at": now_iso(),
        "parent_id": parent_id,
        "note": note,
        "source_wav": {"name": project["wav_name"], "sha256": project["wav_sha"],
                       "channels": info["channels"],
                       "sample_width_in": info["sample_width"],
                       "frame_rate": fr, "n_frames": info["n_frames"],
                       "duration_s": duration},
        "output": {"wav_path": out_wav, "csv_path": out_csv,
                   "json_path": out_json,
                   "sample_width": 16, "frame_rate": fr},
        "algorithm": {
            "method": "hann-windowed radix-2 FFT, parabolic bin refinement; "
                      "piecewise-linear monotone timecode mapping, linear "
                      "resampling per interval",
            "params": state.get("params", {}),
            "interval_ratio": "sqrt(r_i-1 * r_i)",
        },
        "anchors": state.get("anchors", []),
        "splices": state.get("splices", []),
        "suspect_zones": state.get("suspect_zones", []),
        "segments": segments,
        "issues": issues,
        "splice_decisions": _splice_decisions(state, issues, segments),
        "incremental": {"parent_segments_reused": len(reused),
                        "segments_rendered": len(rendered),
                        "reused_keys": [list(k) for k in reused]},
        "confirmed": fully_confirmed,
        "confirmed_segments": n_conf,
        "total_segments": len(segments),
    }
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    conn.execute(
        "INSERT INTO revisions (id,project_id,created_at,parent_id,note,"
        "wav_path,csv_path,json_path,manifest) VALUES (?,?,?,?,?,?,?,?,?)",
        (rev_id, project["id"], now_iso(), parent_id, note,
         out_wav, out_csv, out_json, json.dumps(manifest)))
    conn.commit()
    return rev_id, manifest


def _wav_nframes(path):
    with wave.open(path, "rb") as r:
        return r.getnframes()


def _copy_wav_span(src_path, out_wf, f0, f1, nch=None):
    if f1 <= f0:
        return
    with wave.open(src_path, "rb") as r:
        r.setpos(f0)
        remaining = f1 - f0
        while remaining > 0:
            chunk = min(remaining, 262144)
            frames = r.readframes(chunk)
            if not frames:
                break
            out_wf.writeframes(frames)
            remaining -= chunk


def _splice_decisions(state, issues, segments):
    dec = []
    overlap_keys = {i["key"] for i in issues if i["code"] == "splice_overlap"}
    for s in state.get("splices", []):
        overlapping = [k for k in overlap_keys if s.get("id", "") in k]
        adopted = any(i["adopted"] for i in issues
                      if i["code"] == "splice_overlap"
                      and s.get("id", "") in i["key"])
        dec.append({
            "splice_id": s.get("id"), "at_s": s["at_s"],
            "side": s.get("side"),
            "timecode_in": s.get("tc_in"), "timecode_out": s.get("tc_out"),
            "decision": ("confirmed" if not overlapping else
                         "adopted_with_reason" if adopted else
                         "rejected_overlap"),
            "reason": next((i["reason"] for i in issues
                            if i["code"] == "splice_overlap"
                            and s.get("id", "") in i["key"]), ""),
        })
    return dec


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------

class Response(Exception):
    def __init__(self, status, body=b"", headers=()):
        self.status = status
        self.body = body if isinstance(body, (bytes, bytearray)) else str(body).encode()
        self.headers = list(headers)


def json_ok(obj, status=200):
    raise Response(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   [("Content-Type", "application/json; charset=utf-8")])


def json_err(msg, status=400, extra=None):
    doc = {"error": msg}
    if extra:
        doc.update(extra)
    raise Response(status, json.dumps(doc, ensure_ascii=False).encode("utf-8"),
                   [("Content-Type", "application/json; charset=utf-8")])


def read_json(environ):
    try:
        n = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        n = 0
    raw = environ["wsgi.input"].read(n) if n else b""
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        json_err("invalid JSON body", 400)


def parse_multipart(environ):
    ctype = environ.get("CONTENT_TYPE", "")
    if "multipart/form-data" not in ctype:
        json_err("expected multipart/form-data", 400)
    m = re.search(r"boundary=([^;]+)", ctype)
    if not m:
        json_err("missing multipart boundary", 400)
    boundary = m.group(1).strip('"')
    n = int(environ.get("CONTENT_LENGTH") or 0)
    body = environ["wsgi.input"].read(n)
    delim = b"--" + boundary.encode()
    parts = body.split(delim)
    fields, files = {}, {}
    for part in parts:
        if not part or part in (b"--", b"--\r\n", b"\r\n"):
            continue
        part = part.lstrip(b"\r\n")
        if part.startswith(b"--"):
            continue
        head, _, content = part.partition(b"\r\n\r\n")
        headers = head.decode("latin1", "replace")
        if not content:
            continue
        if content.endswith(b"\r\n"):
            content = content[:-2]
        name_m = re.search(r'name="([^"]*)"', headers)
        fn_m = re.search(r'filename="([^"]*)"', headers)
        if not name_m:
            continue
        name = name_m.group(1)
        if fn_m and fn_m.group(1):
            files[name] = {"filename": fn_m.group(1), "bytes": content,
                           "content_type":
                               re.search(r"Content-Type:\s*([^\r\n]+)",
                                         headers, re.I).group(1).strip()
                               if re.search(r"Content-Type:\s*([^\r\n]+)",
                                            headers, re.I) else
                               "application/octet-stream"}
        else:
            fields[name] = content.decode("utf-8", "replace")
    return fields, files


def serve_file(path, download_name=None, ctype="application/octet-stream"):
    if not os.path.isfile(path):
        json_err("artifact missing on disk", 404)
    headers = [("Content-Type", ctype),
               ("Content-Length", str(os.path.getsize(path)))]
    if download_name:
        headers.append(("Content-Disposition",
                        'attachment; filename="%s"' % download_name))
    with open(path, "rb") as f:
        raise Response(200, f.read(), headers)


# --------------------------------------------------------------------------
# route handlers
# --------------------------------------------------------------------------

def get_project_or_404(pid):
    row = db().execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if not row:
        json_err("project not found", 404)
    return row


def state_summary(project):
    state = json.loads(project["state"])
    duration = 0.0
    info = None
    if project["wav_path"]:
        info = read_wav_info(project["wav_path"])
        duration = info["duration"]
    issues, anchors = compute_validation(state, duration)
    segments, usable = build_segments(state, duration, issues)
    return {"id": project["id"], "name": project["name"],
            "created_at": project["created_at"],
            "wav": ({"name": project["wav_name"], "info": info,
                     "sha256": project["wav_sha"]} if info else None),
            "log_name": project["log_name"],
            "state": state, "issues": issues,
            "segments": segments,
            "fully_confirmed": bool(segments)
                and all(s["confirmed"] for s in segments),
            "usable_anchors": len(usable)}


def api_create_project(environ):
    fields, files = parse_multipart(environ)
    name = fields.get("name") or files.get("wav", {}).get("filename") or "untitled"
    pid = short_id("p")
    pdir = os.path.join(DATA_DIR, pid)
    os.makedirs(pdir, exist_ok=True)
    wav_path = wav_name = wav_sha = None
    log_path = log_name = log_text = None
    state = {"anchors": [], "splices": [], "suspect_zones": [],
             "params": {"tone_hz": DEFAULT_TONE_HZ, "window_s": 1.0,
                        "band_hz": 60.0,
                        "speed_jump_limit": DEFAULT_SPEED_JUMP,
                        "max_gap_s": DEFAULT_MAX_GAP_S},
             "adoptions": {}}
    if "wav" in files:
        import hashlib
        wav = files["wav"]
        wav_name = os.path.basename(wav["filename"])
        wav_path = os.path.join(pdir, "source" + os.path.splitext(wav_name)[1])
        with open(wav_path, "wb") as f:
            f.write(wav["bytes"])
        try:
            read_wav_info(wav_path)
        except Exception as e:
            os.remove(wav_path)
            json_err("not a readable PCM/IEEE-float WAV: %s" % e, 400)
        wav_sha = hashlib.sha256(wav["bytes"]).hexdigest()
    if "log" in files:
        lf = files["log"]
        log_name = os.path.basename(lf["filename"])
        log_text = lf["bytes"].decode("utf-8", "replace")
        log_path = os.path.join(pdir, "reel_log" + os.path.splitext(log_name)[1])
        with open(log_path, "wb") as f:
            f.write(lf["bytes"])
        parsed = parse_reel_log(log_text)
        for a in parsed["anchors"]:
            state["anchors"].append({
                "id": short_id("a"), "pos_s": a["pos_s"],
                "tone_hz": a["tone_hz"], "side": a["side"],
                "label": a["label"], "window_s": 1.0,
                "analysis": None, "chosen_index": None})
        for s in parsed["splices"]:
            s["id"] = short_id("s")
            state["splices"].append(s)
    db().execute(
        "INSERT INTO projects (id,name,created_at,state,wav_path,wav_name,"
        "wav_sha,log_path,log_name,log_text) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (pid, name, now_iso(), json.dumps(state), wav_path, wav_name,
         wav_sha, log_path, log_name, log_text))
    db().commit()
    json_ok(state_summary(get_project_or_404(pid)), 201)


def api_list_projects(environ):
    rows = db().execute(
        "SELECT p.*, (SELECT COUNT(*) FROM revisions r WHERE r.project_id=p.id)"
        " AS nrev FROM projects p ORDER BY p.created_at DESC").fetchall()
    out = []
    for r in rows:
        info = None
        if r["wav_path"]:
            try:
                info = read_wav_info(r["wav_path"])
            except Exception:
                info = None
        out.append({"id": r["id"], "name": r["name"],
                    "created_at": r["created_at"],
                    "wav_name": r["wav_name"], "duration":
                        info["duration"] if info else None,
                    "revisions": r["nrev"]})
    json_ok({"projects": out})


def api_get_project(environ, pid):
    json_ok(state_summary(get_project_or_404(pid)))


def api_update_state(environ, pid):
    project = get_project_or_404(pid)
    body = read_json(environ)
    state = body.get("state")
    if not isinstance(state, dict):
        json_err("body needs state{anchors,splices,suspect_zones,params,...}", 400)
    # normalise: ids, numeric positions
    for a in state.get("anchors", []):
        a.setdefault("id", short_id("a"))
        a["pos_s"] = float(a["pos_s"])
        a.setdefault("tone_hz", DEFAULT_TONE_HZ)
        a.setdefault("side", "A")
        a.setdefault("window_s", 1.0)
    for s in state.get("splices", []):
        s.setdefault("id", short_id("s"))
        s["at_s"] = float(s["at_s"])
        if s.get("tc_in_s") is not None:
            s["tc_in_s"] = float(s["tc_in_s"])
        if s.get("tc_out_s") is not None:
            s["tc_out_s"] = float(s["tc_out_s"])
    for z in state.get("suspect_zones", []):
        z.setdefault("id", short_id("z"))
        z["start_s"] = float(z["start_s"])
        z["end_s"] = float(z["end_s"])
    state.setdefault("adoptions", {})
    db().execute("UPDATE projects SET state=? WHERE id=?",
                 (json.dumps(state), pid))
    db().commit()
    json_ok(state_summary(get_project_or_404(pid)))


def api_analyze(environ, pid):
    project = get_project_or_404(pid)
    if not project["wav_path"]:
        json_err("upload a WAV first", 409)
    body = read_json(environ)
    state = json.loads(project["state"])
    anchor_id = body.get("anchor_id")
    a = next((x for x in state["anchors"] if x["id"] == anchor_id), None)
    if not a:
        json_err("anchor not found: %s" % anchor_id, 404)
    params = state.get("params", {})
    win = float(body.get("window_s", a.get("window_s", params.get("window_s", 1.0))))
    band = float(body.get("band_hz", params.get("band_hz", 60.0)))
    nominal = float(a.get("tone_hz") or params.get("tone_hz", DEFAULT_TONE_HZ))
    info = read_wav_info(project["wav_path"])
    result = analyze_tone(project["wav_path"], info, a["pos_s"], win,
                          nominal, band)
    a["analysis"] = result
    a["window_s"] = win
    # auto-select first in-range candidate, else the strongest
    cands = result.get("candidates") or []
    inrange = [i for i, c in enumerate(cands) if abs(c["deviation"]) <= 0.12]
    a["chosen_index"] = inrange[0] if inrange else (0 if cands else None)
    db().execute("UPDATE projects SET state=? WHERE id=?",
                 (json.dumps(state), pid))
    db().commit()
    json_ok(state_summary(get_project_or_404(pid)))


def api_analyze_all(environ, pid):
    project = get_project_or_404(pid)
    if not project["wav_path"]:
        json_err("upload a WAV first", 409)
    state = json.loads(project["state"])
    info = read_wav_info(project["wav_path"])
    params = state.get("params", {})
    win = float(params.get("window_s", 1.0))
    band = float(params.get("band_hz", 60.0))
    for a in state["anchors"]:
        nominal = float(a.get("tone_hz") or params.get("tone_hz", DEFAULT_TONE_HZ))
        result = analyze_tone(project["wav_path"], info, a["pos_s"], win,
                              nominal, band)
        a["analysis"] = result
        a["window_s"] = win
        cands = result.get("candidates") or []
        inrange = [i for i, c in enumerate(cands) if abs(c["deviation"]) <= 0.12]
        a["chosen_index"] = inrange[0] if inrange else (0 if cands else None)
    db().execute("UPDATE projects SET state=? WHERE id=?",
                 (json.dumps(state), pid))
    db().commit()
    json_ok(state_summary(get_project_or_404(pid)))


def api_revisions(environ, pid):
    get_project_or_404(pid)
    rows = db().execute(
        "SELECT id,created_at,parent_id,note,manifest FROM revisions"
        " WHERE project_id=? ORDER BY created_at", (pid,)).fetchall()
    out = []
    for r in rows:
        m = json.loads(r["manifest"])
        out.append({"id": r["id"], "created_at": r["created_at"],
                    "parent_id": r["parent_id"], "note": r["note"],
                    "confirmed": m.get("confirmed"),
                    "segments": m.get("total_segments"),
                    "rendered": m.get("incremental", {}).get("segments_rendered"),
                    "reused": m.get("incremental", {}).get("parent_segments_reused")})
    json_ok({"revisions": out})


def api_create_revision(environ, pid):
    project = get_project_or_404(pid)
    if not project["wav_path"]:
        json_err("upload a WAV first", 409)
    body = read_json(environ)
    # persist latest state before rendering
    if isinstance(body.get("state"), dict):
        incoming = body["state"]
        cur = json.loads(project["state"])
        cur.update({k: incoming[k] for k in
                    ("anchors", "splices", "suspect_zones", "params", "adoptions")
                    if k in incoming})
        for a in cur.get("anchors", []):
            a.setdefault("id", short_id("a"))
            a["pos_s"] = float(a["pos_s"])
            a.setdefault("tone_hz", DEFAULT_TONE_HZ)
            a.setdefault("side", "A")
            a.setdefault("window_s", 1.0)
        for s in cur.get("splices", []):
            s.setdefault("id", short_id("s"))
            s["at_s"] = float(s["at_s"])
        for z in cur.get("suspect_zones", []):
            z.setdefault("id", short_id("z"))
        cur.setdefault("adoptions", {})
        db().execute("UPDATE projects SET state=? WHERE id=?",
                     (json.dumps(cur), pid))
        db().commit()
        project = get_project_or_404(pid)
    parent_id = body.get("parent_id")
    note = (body.get("note") or "").strip()
    rev_id, manifest = build_revision(project, db(), note, parent_id)
    json_ok({"revision_id": rev_id,
             "downloads": {
                 "wav": "/api/projects/%s/revisions/%s/corrected.wav" % (pid, rev_id),
                 "csv": "/api/projects/%s/revisions/%s/timecode_map.csv" % (pid, rev_id),
                 "json": "/api/projects/%s/revisions/%s/revision.json" % (pid, rev_id)},
             "confirmed": manifest["confirmed"],
             "confirmed_segments": manifest["confirmed_segments"],
             "total_segments": manifest["total_segments"],
             "incremental": manifest["incremental"]}, 210)


def api_source_wav(environ, pid):
    project = get_project_or_404(pid)
    if not project["wav_path"]:
        json_err("no WAV in this project", 404)
    serve_file(project["wav_path"], download_name=None, ctype="audio/wav")


def api_artifact(environ, pid, rev_id, kind):
    row = db().execute(
        "SELECT * FROM revisions WHERE id=? AND project_id=?",
        (rev_id, pid)).fetchone()
    if not row:
        json_err("revision not found", 404)
    if kind == "corrected.wav":
        serve_file(row["wav_path"], "corrected_%s.wav" % rev_id, "audio/wav")
    if kind == "timecode_map.csv":
        serve_file(row["csv_path"], "timecode_map_%s.csv" % rev_id, "text/csv")
    if kind == "revision.json":
        serve_file(row["json_path"], "revision_%s.json" % rev_id,
                   "application/json")
    json_err("unknown artifact", 404)


# --------------------------------------------------------------------------
# WSGI application
# --------------------------------------------------------------------------

ROUTES = [
    ("POST",   r"^/api/projects$"),
    ("GET",    r"^/api/projects$"),
    ("GET",    r"^/api/projects/(?P<pid>p[a-f0-9]+)$"),
    ("PUT",    r"^/api/projects/(?P<pid>p[a-f0-9]+)/state$"),
    ("POST",   r"^/api/projects/(?P<pid>p[a-f0-9]+)/analyze$"),
    ("POST",   r"^/api/projects/(?P<pid>p[a-f0-9]+)/analyze_all$"),
    ("GET",    r"^/api/projects/(?P<pid>p[a-f0-9]+)/source\.wav$"),
    ("GET",    r"^/api/projects/(?P<pid>p[a-f0-9]+)/revisions$"),
    ("POST",   r"^/api/projects/(?P<pid>p[a-f0-9]+)/revisions$"),
    ("GET",    r"^/api/projects/(?P<pid>p[a-f0-9]+)/revisions/(?P<rid>r[a-f0-9]+)/(?P<kind>corrected\.wav|timecode_map\.csv|revision\.json)$"),
]


def application(environ, start_response):
    try:
        path = urlparse(environ.get("PATH_INFO", "/")).path
        method = environ.get("REQUEST_METHOD", "GET")
        # static UI
        if method == "GET" and (path == "/" or path == "/index.html"):
            return _static(start_response, "index.html", "text/html; charset=utf-8")
        m = re.match(r"^/static/(?P<f>.+)$", path)
        if method == "GET" and m:
            return _static(start_response, m.group("f"))

        for rmeth, pattern in ROUTES:
            m = re.match(pattern + r"$", path)
            if m and rmeth == method:
                kw = m.groupdict()
                if path == "/api/projects" and method == "POST":
                    api_create_project(environ)
                elif path == "/api/projects":
                    api_list_projects(environ)
                else:
                    pid = kw["pid"]
                    if method == "GET" and path.endswith(pid):
                        api_get_project(environ, pid)
                    elif method == "PUT":
                        api_update_state(environ, pid)
                    elif path.endswith("/analyze"):
                        api_analyze(environ, pid)
                    elif path.endswith("/analyze_all"):
                        api_analyze_all(environ, pid)
                    elif path.endswith("/source.wav"):
                        api_source_wav(environ, pid)
                    elif method == "GET" and path.endswith("/revisions"):
                        api_revisions(environ, pid)
                    elif method == "POST" and path.endswith("/revisions"):
                        api_create_revision(environ, pid)
                    elif "rid" in kw:
                        api_artifact(environ, pid, kw["rid"], kw["kind"])
                break
        else:
            json_err("not found: %s %s" % (method, path), 404)
    except Response as r:
        status = {200: "200 OK", 201: "201 Created", 210: "210 Content",
                  400: "400 Bad Request", 404: "404 Not Found",
                  409: "409 Conflict", 500: "500 Internal Server Error"}[r.status]
        start_response(status, r.headers + [("Content-Length", str(len(r.body)))])
        return [r.body]
    except Exception as e:  # pragma: no cover
        import traceback
        traceback.print_exc()
        body = ("server error: %s" % e).encode()
        start_response("500 Internal Server Error",
                       [("Content-Type", "text/plain"),
                        ("Content-Length", str(len(body)))])
        return [body]


def _static(start_response, name, ctype=None):
    ctypes = {".html": "text/html; charset=utf-8",
              ".js": "application/javascript; charset=utf-8",
              ".css": "text/css; charset=utf-8",
              ".svg": "image/svg+xml"}
    # no traversal
    name = os.path.normpath(name).lstrip(os.sep)
    if name.startswith(".."):
        start_response("403 Forbidden", [])
        return [b"forbidden"]
    path = os.path.join(STATIC_DIR, name)
    if not os.path.isfile(path):
        start_response("404 Not Found", [])
        return [b"missing"]
    ext = os.path.splitext(name)[1]
    with open(path, "rb") as f:
        body = f.read()
    start_response("200 OK",
                   [("Content-Type", ctype or ctypes.get(ext, "application/octet-stream"),),
                    ("Content-Length", str(len(body)))])
    return [body]


if __name__ == "__main__":
    init_db()
    from wsgiref.simple_server import WSGIServer, WSGIRequestHandler
    import socketserver

    class ThreadingServer(socketserver.ThreadingMixIn, WSGIServer):
        daemon_threads = True
        allow_reuse_address = True

    class Quiet(WSGIRequestHandler):
        def log_message(self, *a):
            pass

    port = int(os.environ.get("PORT", "8000"))
    httpd = ThreadingServer(("0.0.0.0", port), Quiet)
    httpd.set_app(application)
    print("Tape audit console on http://localhost:%d" % port)
    httpd.serve_forever()
