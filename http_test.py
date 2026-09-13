#!/usr/bin/env python3
"""HTTP-layer end-to-end test against the live WSGI app (in-process)."""
import io
import json
import math
import os
import struct
import sys
import wave

import server

FR = 48000


def wav_bytes(samples):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(FR)
        w.writeframes(b"".join(struct.pack("<h", int(max(-1, min(1, v)) * 32767))
                               for v in samples))
    return buf.getvalue()


def make_samples():
    n = FR * 18
    out = [0.0] * n
    # tone 1025 Hz at 4-6s; tone 1025 at 13-15s; splice at 9s
    for i in range(n):
        t = i / FR
        if 4 < t < 6 or 13 < t < 15:
            out[i] = 0.4 * math.sin(2 * math.pi * 1025 * t)
        elif 7 < t < 8.5:
            out[i] = 0.2 * math.sin(2 * math.pi * 180 * t)
    return out


# ---- minimal WSGI test client -------------------------------------------
import wsgiref.util


class Client:
    def __init__(self, app):
        self.app = app

    def request(self, method, path, body=None, headers=None, raw=False):
        environ = {}
        wsgiref.util.setup_testing_defaults(environ)
        environ["REQUEST_METHOD"] = method
        environ["PATH_INFO"] = path
        environ["QUERY_STRING"] = ""
        data = b""
        if body is not None:
            data = body if raw else json.dumps(body).encode()
            environ["CONTENT_TYPE"] = "application/json"
        environ["CONTENT_LENGTH"] = str(len(data))
        environ["wsgi.input"] = io.BytesIO(data)
        for k, v in (headers or {}).items():
            if k.lower() == "content-type":
                environ["CONTENT_TYPE"] = v
        captured = {}

        def start(status, hdrs, exc=None):
            captured["status"] = int(status.split()[0])
            captured["headers"] = hdrs

        chunks = self.app(environ, start)
        payload = b"".join(chunks)
        hd = {k.lower(): v for k, v in captured["headers"]}
        return captured["status"], hd, payload

    def multipart(self, path, fields, files):
        boundary = "----tb" + os.urandom(6).hex()
        body = bytearray()
        for k, v in fields.items():
            body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
        for k, (fn, content, ct) in files.items():
            body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fn}\"\r\n".encode()
            body += f"Content-Type: {ct}\r\n\r\n".encode()
            body += content + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        return self.request("POST", path, bytes(body),
                            {"content-type": f"multipart/form-data; boundary={boundary}"},
                            raw=True)


def main():
    server.init_db()
    c = Client(server.application)

    st, _, b = c.request("GET", "/api/projects")
    assert st == 200
    print("list projects ->", st)

    wb = wav_bytes(make_samples())
    log = json.dumps({
        "fps": 25,
        "anchors": [{"at": 5, "hz": 1000, "side": "A", "label": "head"},
                    {"at": 14, "hz": 1000, "side": "B", "label": "tail"}],
        "splices": [{"at": 9, "side": "A", "tc_in": "00:10", "tc_out": "00:12"}]
    }).encode()
    st, _, b = c.multipart("/api/projects", {"name": "HTTP卷"},
                           {"wav": ("reel.wav", wb, "audio/wav"),
                            "log": ("reel.json", log, "application/json")})
    assert st == 201, b
    proj = json.loads(b)
    pid = proj["id"]
    print("create ->", st, pid, "anchors from log:",
          [(a["pos_s"], a["side"]) for a in proj["state"]["anchors"]],
          "splices:", [(s["at_s"], s["tc_in_s"]) for s in proj["state"]["splices"]])
    assert len(proj["state"]["anchors"]) == 2
    assert abs(proj["state"]["splices"][0]["tc_in_s"] - 10) < 1e-9

    # source WAV byte-identical to upload
    st, hd, b2 = c.request("GET", f"/api/projects/{pid}/source.wav")
    assert st == 200 and b2 == wb, (st, len(b2), len(wb))
    print("source.wav bytes identical:", len(b2))

    # analyze all
    st, _, b = c.request("POST", f"/api/projects/{pid}/analyze_all", {})
    assert st == 200, b
    proj = json.loads(b)
    for a in proj["state"]["anchors"]:
        c0 = a["analysis"]["candidates"][0]
        print(f"  anchor {a['pos_s']}s: {c0['hz']:.2f} Hz ratio {c0['ratio']:.5f} flags={a['analysis']['flags']}")
        assert abs(c0["hz"] - 1025) < 1.5

    # no blocks expected (uniform 2.5%, splice has no overlap partner)
    blocks = [i for i in proj["issues"] if i["severity"] == "block" and not i["adopted"]]
    print("issues:", [(i["code"], i["severity"]) for i in proj["issues"]])
    assert not blocks

    # create revision
    st, _, b = c.request("POST", f"/api/projects/{pid}/revisions",
                         {"note": "http e2e", "state": proj["state"]})
    assert st == 210, b
    rev = json.loads(b)
    rid = rev["revision_id"]
    print("revision ->", rid, "confirmed segs:", rev["confirmed_segments"],
          "/", rev["total_segments"], "incremental:", rev["incremental"])

    # download all three artifacts
    for kind, ctype, magic in [("corrected.wav", "audio/wav", b"RIFF"),
                               ("timecode_map.csv", "text/csv", b"src_start"),
                               ("revision.json", "application/json", b"{")]:
        st, hd, payload = c.request("GET", f"/api/projects/{pid}/revisions/{rid}/{kind}")
        assert st == 200 and payload[:4] if kind.endswith("wav") else st == 200
        assert magic in payload[:200], (kind, payload[:20])
        print(f"  {kind}: {st} {len(payload)} bytes {hd.get('content-type')}")

    rj = json.loads(c.request("GET", f"/api/projects/{pid}/revisions/{rid}/revision.json")[2])
    assert rj["algorithm"]["interval_ratio"] == "sqrt(r_i-1 * r_i)"
    assert rj["anchors"] and rj["splice_decisions"] and "params" in rj["algorithm"]
    csv_text = c.request("GET", f"/api/projects/{pid}/revisions/{rid}/timecode_map.csv")[2].decode()
    assert "passthrough_uncovered" in csv_text and "resample" in csv_text
    print("CSV head:\n" + "\n".join(csv_text.splitlines()[:5]))

    # second revision with tiny nudge -> parent reuse
    proj["state"]["anchors"][0]["label"] = "head (renamed)"
    st, _, b = c.request("POST", f"/api/projects/{pid}/revisions",
                         {"note": "rename only", "parent_id": rid, "state": proj["state"]})
    rev2 = json.loads(b)
    print("second revision rendered/reused:",
          rev2["incremental"]["segments_rendered"], "/",
          rev2["incremental"]["parent_segments_reused"])
    assert rev2["incremental"]["parent_segments_reused"] >= 1

    # splice overlap forces block, adoption with reason clears it
    code, _, st_body = c.request("GET", f"/api/projects/{pid}")
    assert code == 200
    cur = json.loads(st_body)
    cur["state"]["splices"].append({"id": "sX", "at_s": 11.0, "side": "A",
                                    "tc_in": "00:11", "tc_out": "00:13",
                                    "tc_in_s": 11.0, "tc_out_s": 13.0})
    r = json.loads(c.request("PUT", f"/api/projects/{pid}/state",
                             {"state": cur["state"]})[2])
    ov = [i for i in r["issues"] if i["code"] == "splice_overlap"]
    assert ov and not ov[0]["adopted"]
    print("overlap block:", ov[0]["key"], ov[0]["start_s"], ov[0]["end_s"])
    cur["state"]["adoptions"][ov[0]["key"]] = "已核对卷盘记录，复用合理"
    r2 = json.loads(c.request("PUT", f"/api/projects/{pid}/state",
                              {"state": cur["state"]})[2])
    assert all(i["adopted"] for i in r2["issues"] if i["code"] == "splice_overlap")
    print("overlap adopted with reason ✓")

    # bad inputs
    assert c.request("PUT", f"/api/projects/{pid}/state", {"nope": 1})[0] == 400
    assert c.request("GET", "/api/projects/pdeadbeef")[0] == 404
    assert c.request("GET", "/static/../server.py")[0] in (403, 404)
    st, _, b3 = c.multipart("/api/projects", {"name": "bad"},
                            {"wav": ("x.wav", b"NOTWAV", "audio/wav")})
    assert st == 400, b3[:80]
    print("error handling ✓")
    print("HTTP E2E PASSED")


if __name__ == "__main__":
    main()
