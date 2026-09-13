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
    gap_issues = [i for i in issues if i["code"] == "coverage_gap"]
    assert gap_issues
    gap = gap_issues[0]
    assert gap["severity"] == "block", "coverage_gap must block confirmation"
    assert abs(gap["start_s"] - 1.0) < 1e-9 and abs(gap["end_s"] - 19.0) < 1e-9
    # the whole 1-19s interval between the two anchors stays unconfirmed
    segs, _ = server.build_segments(far, dur, issues)
    inner = [s for s in segs if s["mode"] == "resample"]
    assert inner and all(not s["confirmed"] for s in inner), \
        "interval beyond max_gap_s must not confirm"
    assert any(s["blocking_issues"]
               and s["blocking_issues"][0].startswith("coverage_gap")
               for s in inner)
    # suspect droop zone with no anchor inside -> cannot confirm
    far["suspect_zones"] = [{"id": "z1", "start_s": 8.0, "end_s": 10.0,
                             "label": "疑似掉速"}]
    issues, _ = server.compute_validation(far, dur)
    ziss = [i for i in issues if "z1" in i["refs"]]
    assert ziss and ziss[0]["code"] == "coverage_gap"
    # adopting the zone gap with a reason marks it adopted
    far["adoptions"][ziss[0]["key"]] = "掉速区经人工复核可接受"
    issues2, _ = server.compute_validation(far, dur)
    assert all(i["adopted"] for i in issues2 if "z1" in i["refs"])


def test_ambiguous_blocks_until_reason(path, dur):
    print("- regression: ambiguous_candidates blocks until reason")
    info = server.read_wav_info(path)
    # real two-tone window inside the uploaded WAV region? build a dedicated
    # short file so the result is independent of the main reel.
    p2 = os.path.join(DATA, "reg_amb.wav")
    two = [0.35 * math.sin(2 * math.pi * 1000 * i / FR)
           + 0.34 * math.sin(2 * math.pi * 960 * i / FR)
           for i in range(FR * 3)]
    write_wav(p2, silence(2) + two + silence(2))
    info2 = server.read_wav_info(p2)
    res = server.analyze_tone(p2, info2, 3.5, 1.0, 1000, 60)
    assert "ambiguous_candidates" in res["flags"], res["flags"]
    assert len(res["candidates"]) >= 2
    state = {"anchors": [{
                "id": "amb1", "pos_s": 3.5, "side": "A", "tone_hz": 1000,
                "window_s": 1.0, "analysis": res, "chosen_index": 0},
               measured_anchor(1.0, 6.5, "A", "ref1")],
             "splices": [], "suspect_zones": [], "adoptions": {},
             "params": {"speed_jump_limit": 0.04, "max_gap_s": 120}}
    issues, _ = server.compute_validation(state, info2["duration"])
    blocks = [i for i in issues if i["code"] == "ambiguous_candidates"]
    assert blocks and blocks[0]["severity"] == "block"
    assert not blocks[0]["adopted"]
    # choosing a candidate does NOT clear the block; the ambiguous anchor is
    # excluded from the mapping so its interval stays unconfirmed
    state["anchors"][0]["chosen_index"] = 1
    segs, usable = server.build_segments(state, info2["duration"], issues)
    assert not usable or all(a[0]["id"] != "amb1" for a in usable)
    interval = [s for s in segs if s["end_s"] > 3.5 and s["start_s"] < 6.5]
    assert interval and all(not s["confirmed"] for s in interval)
    # reason recorded -> anchor joins the mapping and interval confirms
    state["adoptions"][blocks[0]["key"]] = "结合卷盘记录确认 960Hz 为邻频串扰，取 1000Hz"
    issues2, _ = server.compute_validation(state, info2["duration"])
    assert all(i["adopted"] for i in issues2 if i["code"] == "ambiguous_candidates")
    segs2, usable2 = server.build_segments(state, info2["duration"], issues2)
    assert any(a[0]["id"] == "amb1" for a in usable2)
    resampled = [s for s in segs2 if s["mode"] == "resample"]
    assert resampled and all(s["confirmed"] for s in resampled)
    print("  blocked while no reason; confirmed after reason ✓")


def test_sparse_1_19_blocked(dur):
    print("- regression: anchors at 1s and 19s, max_gap 5 -> blocked")
    state = {"anchors": [measured_anchor(1.02, 1.0, "A", "s1"),
                         measured_anchor(1.02, 19.0, "A", "s2")],
             "splices": [], "suspect_zones": [], "adoptions": {},
             "params": {"speed_jump_limit": 0.04, "max_gap_s": 5}}
    issues, _ = server.compute_validation(state, dur)
    gap = [i for i in issues if i["code"] == "coverage_gap"]
    assert gap and all(g["severity"] == "block" for g in gap)
    assert abs(gap[0]["start_s"] - 1.0) < 1e-9
    assert abs(gap[0]["end_s"] - 19.0) < 1e-9
    segs, _ = server.build_segments(state, dur, issues)
    mid = [s for s in segs if abs(s["start_s"] - 1.0) < 1e-9]
    assert mid and not mid[0]["confirmed"]
    # with a permissive max_gap the same anchors confirm
    state["params"]["max_gap_s"] = 100
    issues2, _ = server.compute_validation(state, dur)
    assert not any(i["code"] == "coverage_gap" for i in issues2)
    segs2, _ = server.build_segments(state, dur, issues2)
    assert all(s["confirmed"] for s in segs2 if s["mode"] == "resample")
    print("  blocked under gap>5s, confirmed when covered ✓")


def test_first_anchor_2s_origin(path, dur):
    print("- regression: first anchor at 2s, leader counted once")
    state = {"anchors": [measured_anchor(1.03, 2.0, "A", "g1"),
                         measured_anchor(1.03, 12.0, "A", "g2")],
             "splices": [], "suspect_zones": [], "adoptions": {},
             "params": {"speed_jump_limit": 0.04, "max_gap_s": 120}}
    issues, _ = server.compute_validation(state, dur)
    segs, _ = server.build_segments(state, dur, issues)
    # first segment is the 0-2s leader, corrected timeline starts at 0
    assert abs(segs[0]["start_s"] - 0.0) < 1e-9
    assert abs(segs[0]["corrected_start_s"] - 0.0) < 1e-12, segs[0]
    assert abs(segs[0]["corrected_end_s"] - 2.0) < 1e-9, \
        "leader must be counted exactly once"
    # expected total = 2 + (12-2)*1.03 + (dur-12)*1
    expected = 2.0 + 10.0 * 1.03 + (dur - 12.0)
    assert abs(segs[-1]["corrected_end_s"] - expected) < 1e-6

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(server.SCHEMA)
    proj = {"id": "porg", "state": json.dumps(state), "wav_path": path,
            "wav_name": "good.wav", "wav_sha": "x"}
    _, man = server.build_revision(proj, conn, "origin regression", None)
    wav_dur = server.read_wav_info(man["output"]["wav_path"])["duration"]
    csv_end = man["segments"][-1]["corrected_end_s"]
    json_end = man["output"]["duration_s"]
    print("  wav_dur=%.6f csv_end=%.6f json_end=%.6f expected=%.6f"
          % (wav_dur, csv_end, json_end, expected))
    assert abs(wav_dur - csv_end) < 1.0 / 48000 + 1e-9
    assert abs(wav_dur - json_end) < 1e-9
    assert abs(wav_dur - expected) < 1.0 / 48000 + 1e-6
    # leader PCM samples identical to source (raw passthrough, not duplicated)
    src_ch = server.read_wav_frames(path, server.read_wav_info(path), 0, 96)[0]
    out_info = server.read_wav_info(man["output"]["wav_path"])
    out_ch = server.read_wav_frames(man["output"]["wav_path"], out_info, 0, 96)[0]
    assert all(abs(a - b) < 1e-6 for a, b in zip(src_ch, out_ch)), \
        "leader audio must be the original samples, once"
    print("  JSON/CSV/WAV end agree; leader single-counted ✓")


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
    test_ambiguous_blocks_until_reason(path, dur)
    test_sparse_1_19_blocked(dur)
    test_first_anchor_2s_origin(path, dur)
    test_log_parse()
    print("ALL SMOKE TESTS PASSED")
