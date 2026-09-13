#!/usr/bin/env python3
"""End-to-end smoke test exercised without a browser.

Synthetic reels cover: uniform speed bias (pitch restoration), abrupt speed
jump + adoption with reason, ambiguous calibration tone, anchor order
contradiction, splice timecode overlap, coverage gaps, log parsing and
incremental revisions.
"""
import json
import math
import os
import sqlite3
import struct
import wave

import server

FR = 48000
DATA = "/tmp/audit_test"
os.makedirs(DATA, exist_ok=True)
server.init_db()


def tone(freq, dur, amp=0.4):
    n = int(FR * dur)
    return [amp * math.sin(2 * math.pi * freq * i / FR) for i in range(n)]


def silence(dur):
    return [0.0] * int(FR * dur)


def voice_like(dur, base=180.0):
    n = int(FR * dur)
    out = []
    for i in range(n):
        f = base + 6 * math.sin(2 * math.pi * 3 * i / FR)
        out.append(0.3 * math.sin(2 * math.pi * f * i / FR)
                   + 0.1 * math.sin(2 * math.pi * 2 * f * i / FR))
    return out


def write_wav(path, samples, sw=2):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(sw)
        w.setframerate(FR)
        frames = bytearray()
        for v in samples:
            frames += struct.pack("<h", int(max(-1.0, min(1.0, v)) * 32767))
        w.writeframes(bytes(frames))


def measured_anchor(ratio, pos, side, aid, window=1.0):
    return {"id": aid, "pos_s": pos, "side": side, "tone_hz": 1000,
            "label": "", "window_s": window,
            "analysis": {"candidates": [{"hz": 1000 * ratio, "ratio": ratio,
                                         "deviation": ratio - 1,
                                         "ambiguous": False, "level_db": 0.0,
                                         "rms_ratio": 1.0}],
                         "flags": [],
                         "window_start_s": pos - window / 2,
                         "window_end_s": pos + window / 2},
            "chosen_index": 0}


def make_uniform_wav(path, ratio=1.03):
    # whole reel captured slow uniformly -> every 1 kHz tone reads 1030 Hz
    a = silence(4) + tone(1000 * ratio, 2) + voice_like(3)
    b = voice_like(3) + silence(1) + tone(1000 * ratio, 2) + voice_like(2)
    src = a + b
    if len(src) < FR * 20:
        src += silence((FR * 20 - len(src)) / FR)
    write_wav(path, src[:FR * 20])
    return 20.0


def test_good_reel():
    print("- uniform 3% slow reel")
    path = os.path.join(DATA, "good.wav")
    dur = make_uniform_wav(path)
    info = server.read_wav_info(path)
    assert info["frame_rate"] == FR and abs(info["duration"] - 20) < 0.01

    state = {"anchors": [], "splices": [], "suspect_zones": [],
             "params": {"tone_hz": 1000, "window_s": 1.0, "band_hz": 60,
                        "speed_jump_limit": 0.04, "max_gap_s": 120},
             "adoptions": {}}
    for pos, side in [(5.0, "A"), (14.0, "B")]:
        res = server.analyze_tone(path, info, pos, 1.0, 1000, 60)
        assert res["candidates"], "no candidate at %s" % pos
        state["anchors"].append(
            {"id": "a%d" % pos, "pos_s": pos, "side": side, "tone_hz": 1000,
             "label": "", "window_s": 1.0,
             "analysis": res, "chosen_index": 0})
        print("  tone at %4.1fs (%s): %7.2f Hz  ratio %.5f  flags=%s fft %.0fms"
              % (pos, side, res["candidates"][0]["hz"],
                 res["candidates"][0]["ratio"], res["flags"],
                 res.get("fft_ms", 0)))

    state["splices"].append({"id": "s1", "at_s": 9.5, "side": "A",
                             "tc_in": "00:09:00", "tc_out": "00:09:02",
                             "tc_in_s": 540.0, "tc_out_s": 542.0,
                             "label": ""})

    issues, _ = server.compute_validation(state, dur)
    segs, _ = server.build_segments(state, dur, issues)
    print("  issues:", {i["code"]: i["severity"] for i in issues})
    assert not any(i["severity"] == "block" for i in issues)
    assert all(s["confirmed"] for s in segs if s["mode"] == "resample")
    mid = [s for s in segs if s["start_s"] == 5.0][0]
    assert abs(mid["ratio"] - 1.03) < 2e-4
    for s in segs:
        assert s["corrected_end_s"] >= s["corrected_start_s"]

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(server.SCHEMA)
    proj = {"id": "ptest", "state": json.dumps(state), "wav_path": path,
            "wav_name": "good.wav", "wav_sha": "x"}
    rev_id, man = server.build_revision(proj, conn, "initial", None)
    print("  revision:", rev_id, "confirmed_segments:",
          man["confirmed_segments"], "/", man["total_segments"],
          "rendered:", man["incremental"]["segments_rendered"])
    internal = [s for s in man["segments"] if s["mode"] == "resample"]
    assert all(s["confirmed"] for s in internal)
    edges = [s for s in man["segments"] if s["mode"] == "passthrough_uncovered"]
    assert edges and all(not s["confirmed"] for s in edges)
    out_info = server.read_wav_info(man["output"]["wav_path"])
    print("  output duration:", round(out_info["duration"], 3),
          "input:", round(dur, 3))
    assert out_info["sample_width"] == 2
    assert out_info["duration"] > dur
    # source 5.5s (inside the 1030 Hz tone) lands at 5 + .5*1.03 = 5.65s
    r = server.analyze_tone(man["output"]["wav_path"], out_info,
                            5.65, 1.0, 1000, 25)
    assert r["candidates"], "no corrected tone: %s" % r["flags"]
    print("  corrected side-A tone:", round(r["candidates"][0]["hz"], 2), "Hz")
    assert abs(r["candidates"][0]["hz"] - 1000) < 2.0

    # incremental revision: nudge anchor B -> only segments touching it render
    state2 = json.loads(proj["state"])
    c0 = state2["anchors"][1]["analysis"]["candidates"][0]
    c0["hz"], c0["ratio"], c0["deviation"] = 990.0, 0.99, -0.01
    proj2 = dict(proj, state=json.dumps(state2))
    _, man2 = server.build_revision(proj2, conn, "nudge B", rev_id)
    print("  incremental rendered/reused:",
          man2["incremental"]["segments_rendered"], "/",
          man2["incremental"]["parent_segments_reused"])
    assert man2["incremental"]["parent_segments_reused"] >= 1
    return state, dur, path


def test_jump_and_adoption(dur, path):
    print("- speed jump + reason adoption")
    state = {"anchors": [measured_anchor(1.03, 5.0, "A", "j_a1"),
                         measured_anchor(0.98, 14.0, "B", "j_a2")],
             "splices": [{"id": "j_s1", "at_s": 9.5, "side": "A",
                          "tc_in_s": 540.0, "tc_out_s": 542.0}],
             "suspect_zones": [], "adoptions": {},
             "params": {"speed_jump_limit": 0.04, "max_gap_s": 120}}
    issues, _ = server.compute_validation(state, dur)
    assert any(i["code"] == "speed_jump" and not i["adopted"] for i in issues)
    segs, _ = server.build_segments(state, dur, issues)
    assert all(not s["confirmed"] for s in segs if s["mode"] == "resample")
    for i in issues:
        if i["code"] == "speed_jump":
            state["adoptions"][i["key"]] = "接带两侧卷盘不同，转速差属预期"
    issues, _ = server.compute_validation(state, dur)
    assert all(i["adopted"] for i in issues if i["code"] == "speed_jump")
    segs, _ = server.build_segments(state, dur, issues)
    assert all(s["confirmed"] for s in segs if s["mode"] == "resample")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(server.SCHEMA)
    proj = {"id": "pjump", "state": json.dumps(state), "wav_path": path,
            "wav_name": "good.wav", "wav_sha": "x"}
    _, man = server.build_revision(proj, conn, "jump adopted", None)
    dec = man["splice_decisions"][0]
    assert dec["decision"] == "confirmed" and dec["at_s"] == 9.5
    print("  adopted; splice decision:", dec["decision"])


def test_pathologies(dur):
    print("- ambiguous / order / overlap / coverage")
    two = [0.35 * math.sin(2 * math.pi * 1000 * i / FR)
           + 0.33 * math.sin(2 * math.pi * 958 * i / FR)
           for i in range(FR * 2)]
    p2 = os.path.join(DATA, "amb2.wav")
    write_wav(p2, silence(1) + two)
    info2 = server.read_wav_info(p2)
    r = server.analyze_tone(p2, info2, 1.5, 1.0, 1000, 60)
    print("  ambiguous candidates:",
          [(round(c["hz"], 1), round(c["level_db"], 1)) for c in r["candidates"]],
          r["flags"])
    assert "ambiguous_candidates" in r["flags"]

    bad = {"anchors": [
        {"id": "x1", "pos_s": 12.0, "side": "B", "tone_hz": 1000,
         "window_s": 1.0, "analysis": None},
        {"id": "x2", "pos_s": 5.0, "side": "A", "tone_hz": 1000,
         "window_s": 1.0, "analysis": None}],
        "splices": [], "suspect_zones": [], "adoptions": {},
        "params": {"speed_jump_limit": 0.04, "max_gap_s": 120}}
    issues, _ = server.compute_validation(bad, dur)
    codes = {i["code"] for i in issues}
    print("  order issue codes:", sorted(codes))
    assert "anchor_order" in codes
    ev = [i for i in issues if i["code"] == "anchor_order"][0]
    assert ev["start_s"] <= 5.0 and ev["end_s"] >= 12.0  # circles both

    bad2 = {"anchors": [], "splices": [
        {"id": "j1", "at_s": 4.0, "side": "A",
         "tc_in_s": 60.0, "tc_out_s": 63.0},
        {"id": "j2", "at_s": 8.0, "side": "A",
         "tc_in_s": 62.0, "tc_out_s": 65.0}],
        "suspect_zones": [], "adoptions": {},
        "params": {"speed_jump_limit": 0.04, "max_gap_s": 120}}
    issues, _ = server.compute_validation(bad2, dur)
    ov = [i for i in issues if i["code"] == "splice_overlap"]
    assert ov, issues
    print("  overlap evidence span:", round(ov[0]["start_s"], 1),
          "-", round(ov[0]["end_s"], 1), ov[0]["refs"])
    # adopting overlap with reason
    bad2["adoptions"][ov[0]["key"]] = "同一卷盘时码复用，已与卷盘记录核对"
    issues, _ = server.compute_validation(bad2, dur)
    assert all(i["adopted"] for i in issues if i["code"] == "splice_overlap")

    far = {"anchors": [measured_anchor(1.0, 1.0, "A", "f1"),
                       measured_anchor(1.0, 19.0, "A", "f2")],
           "splices": [], "suspect_zones": [], "adoptions": {},
           "params": {"speed_jump_limit": 0.04, "max_gap_s": 5}}
    issues, _ = server.compute_validation(far, dur)
    print("  sparse-anchor codes:", sorted(i["code"] for i in issues))
    assert any(i["code"] == "coverage_gap" for i in issues)
    gap = [i for i in issues if i["code"] == "coverage_gap"][0]
    assert abs(gap["start_s"] - 1.0) < 1e-9 and abs(gap["end_s"] - 19.0) < 1e-9
    # suspect droop zone with no anchor inside -> cannot confirm
    far["suspect_zones"] = [{"id": "z1", "start_s": 8.0, "end_s": 10.0,
                             "label": "疑似掉速"}]
    issues, _ = server.compute_validation(far, dur)
    assert any(i["code"] == "coverage_gap" and "z1" in i["refs"] for i in issues)


def test_log_parse():
    print("- reel log parsing")
    txt = ('{"fps": 25, "anchors": [{"at": "00:05", "hz": 1000, "side": "A"}],'
           ' "splices": [{"at": 9.5, "side": "A", "tc_in": "00:09:00",'
           ' "tc_out": "00:09:02"}]}')
    p = server.parse_reel_log(txt)
    assert abs(p["anchors"][0]["pos_s"] - 5.0) < 1e-9
    assert abs(p["splices"][0]["tc_in_s"] - 540.0) < 1e-9
    p2 = server.parse_reel_log(
        "tone 5 hz=1000 side=A label=head\n"
        "splice 9.5 side=A tcin=00:10 tcout=00:12\n")
    assert abs(p2["anchors"][0]["pos_s"] - 5.0) < 1e-9
    assert abs(p2["splices"][0]["tc_out_s"] - 12.0) < 1e-9
    assert server.format_smpte(3661.5, 25) == "01:01:01:12"


if __name__ == "__main__":
    state, dur, path = test_good_reel()
    test_jump_and_adoption(dur, path)
    test_pathologies(dur)
    test_log_parse()
    print("ALL SMOKE TESTS PASSED")
