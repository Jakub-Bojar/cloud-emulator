#!/Users/jakubbojarski/Documents/Developer/GitHub/Personal/cloud-emulator/calibration/.venv/bin/python3
"""
Universal model calibrator — derive the emulator's (a, b) coefficients from a
real model running on this machine.

Works with any locally served model in any of four modalities:

    text2text     chat completion            POST /v1/chat/completions
    image2text    vision chat (image input)  POST /v1/chat/completions
    text2image    image generation           POST /v1/images/generations
    image2image   image edit / variation     POST /v1/images/edits

All four speak the OpenAI-compatible API, which every common local server
exposes: Ollama, vLLM, llama.cpp server, LM Studio, LocalAI, sd.cpp, TGI, …
Override `--host` / `--endpoint` for anything non-standard.

What it does
------------
Drives the model at a sweep of x values (x = concurrent in-flight requests —
the same load signal the emulator injects at a source app), measures the
server's real CPU, RAM and network footprint at each level, then least-squares
fits the worker's load function

    load = max(0, a * x + b)        (cpu→millicores, ram→MB, net→Mbps)

The resulting {a, b} block pastes straight into a template's app spec
(TEMPLATE.md, "The formula") so the emulator reproduces the model's footprint
without the model.

Division of labour
------------------
- Worker threads own the load: each of the x threads keeps one request in
  flight for the level's duration, and payload byte counts give net Mbps.
- `ps` sampling owns the server-side resources: CPU-time delta (→ millicores)
  and RSS (→ MB) of every process matching `--proc-match`, so it works for
  whatever server you run.
- A background `/metrics` endpoint (--metrics-port, default 9877) publishes
  the same CPU/RAM/net readings live, once a second, while the sweep runs —
  point a Prometheus ScrapeConfig at it (see manifests/host-calibration-scrape.yaml)
  to watch a real calibration run in Grafana alongside the emulator's own
  target-vs-actual panels.

Needs Python 3.10+ (for statistics.linear_regression/correlation) and the
`requests` + `prometheus_client` libraries
(`pip install -r calibration/requirements.txt`); no other third-party deps,
no extra binaries.

Examples
--------
    # text -> text (Ollama)
    ollama pull llama3.2:3b
    OLLAMA_NUM_PARALLEL=8 ollama serve &
    python3 calibration/calibrate.py --mode text2text --model llama3.2:3b

    # image -> text (any vision model; uses a built-in synthetic PNG, or pass
    # --image calibration/images/photo.jpg — that folder is gitignored, so
    # drop your own test images there freely)
    python3 calibration/calibrate.py --mode image2text --model llava:7b

    # text -> image (any server with /v1/images/generations, e.g. LocalAI)
    python3 calibration/calibrate.py --mode text2image --model sd-1.5 \
        --host http://localhost:8080 --x 0 1 2 4

    # image -> image
    python3 calibration/calibrate.py --mode image2image --model sd-1.5 \
        --host http://localhost:8080 --image calibration/images/in.png --x 0 1 2 4
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import statistics
import struct
import subprocess
import sys
import threading
import time
import zlib
from datetime import datetime

import requests
from prometheus_client import Gauge, start_http_server

CHAT_MODES = ("text2text", "image2text")
IMAGE_OUT_MODES = ("text2image", "image2image")
DEFAULT_ENDPOINTS = {
    "text2text": "/v1/chat/completions",
    "image2text": "/v1/chat/completions",
    "text2image": "/v1/images/generations",
    "image2image": "/v1/images/edits",
}
REQUEST_TIMEOUT_S = 600

# Live metrics — a Prometheus /metrics endpoint updated ~once/second while a
# level is running (see _collect's on_sample), so a Grafana dashboard can show
# a real calibration run alongside the emulator's own target-vs-actual panels
# for direct comparison. Labeled by model/mode so multiple runs don't collide.
LIVE_X = Gauge("calibration_x", "Concurrency level (x) currently being measured",
              ["model", "mode"])
LIVE_CPU = Gauge("calibration_cpu_millicores",
                 "Live measured CPU load (millicores), ~1s resolution",
                 ["model", "mode"])
LIVE_RAM = Gauge("calibration_ram_mb",
                 "Live measured RAM usage (MB), ~1s resolution",
                 ["model", "mode"])
LIVE_NET = Gauge("calibration_net_mbps",
                 "Live measured network egress (Mbps), ~1s resolution; 0 "
                 "during the idle x=0 baseline (no requests in flight)",
                 ["model", "mode"])


# Generates a Gradient RGB image of size 512 x 512 if no image is passed in as parameter
def synth_png(w: int = 512, h: int = 512) -> bytes:
    """A deterministic RGB gradient PNG, generated with stdlib only, so the
    image modes work out of the box with no input file."""
    rows = []
    for y in range(h):
        row = bytearray([0])  # PNG filter type 0 (None)
        for x in range(w):
            row += bytes((x * 255 // max(1, w - 1),
                          y * 255 // max(1, h - 1),
                          (x + y) * 255 // max(1, w + h - 2)))
        rows.append(bytes(row))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # 8-bit RGB
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 6))
            + chunk(b"IEND", b""))

# Opens the actual image you provided in the flag or uses the synthetic image instead
def load_image(path: str | None) -> tuple[bytes, str]:
    """(bytes, mime) of the input image for image2text / image2image."""
    if not path:
        return synth_png(), "image/png"
    with open(path, "rb") as f:
        data = f.read()
    mime = mimetypes.guess_type(path)[0] or "image/png"
    return data, mime

# Figures out based on which --mode, exactly what HTTP request needs to be sent - and builds it once before any measuring starts
def build_request(args, image: tuple[bytes, str]) -> tuple[str, dict]:
    """The one inference request this calibration run repeats: returns
    (url, kwargs) where kwargs is passed straight to requests.post(url, **kwargs)
    by every caller. Built once — every worker thread reuses it."""
    endpoint = args.endpoint or DEFAULT_ENDPOINTS[args.mode]
    url = args.host.rstrip("/") + endpoint

    if args.mode == "text2text":
        payload = {"model": args.model,
                   "messages": [{"role": "user", "content": args.prompt}],
                   "max_tokens": args.max_tokens, "stream": False}
        return url, {"json": payload}

    if args.mode == "image2text":
        data, mime = image
        uri = f"data:{mime};base64,{base64.b64encode(data).decode()}"
        content = [{"type": "text", "text": args.prompt},
                   {"type": "image_url", "image_url": {"url": uri}}]
        payload = {"model": args.model,
                   "messages": [{"role": "user", "content": content}],
                   "max_tokens": args.max_tokens, "stream": False}
        return url, {"json": payload}

    if args.mode == "text2image":
        payload = {"model": args.model, "prompt": args.prompt, "n": 1,
                   "size": args.size, "response_format": "b64_json"}
        return url, {"json": payload}

    # image2image — a real file upload, so it needs multipart/form-data.
    # requests builds and encodes that for us from files=/data=.
    data, mime = image
    ext = (mime.split("/")[-1] or "png").replace("jpeg", "jpg")
    return url, {
        "data": {"model": args.model, "prompt": args.prompt, "n": "1",
                 "size": args.size, "response_format": "b64_json"},
        "files": {"image": (f"input.{ext}", data, mime)},
    }

# Figures out how much actual work the did the model do from the response body - either tokens generated or images returned
def parse_response(mode: str, data: bytes) -> tuple[int, int]:
    """(completion_tokens, images) from one successful response body — the
    throughput sanity signal (not part of the fit)."""
    try:
        obj = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return 0, 0
    if mode in CHAT_MODES:
        usage = obj.get("usage") or {}
        return int(usage.get("completion_tokens") or 0), 0
    return 0, len(obj.get("data") or [])


# Sends exactly one real request to the model, before any measurements start, purely to force it into memory
def preload(args, url: str, kwargs: dict) -> None:
    print(f"preloading {args.model} ({args.mode}) ...", flush=True)
    try:
        resp = requests.post(url, timeout=REQUEST_TIMEOUT_S, **kwargs)
        resp.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        detail = exc.response.text[:300]
        sys.exit(f"\nThe server rejected the request ({exc.response.status_code}): {detail}\n"
                 f"Check --model / --mode / --endpoint against your server.")
    except requests.exceptions.RequestException as exc:
        sys.exit(f"\nCannot reach the model at {url} ({exc}).\n"
                 f"Start a server that speaks the OpenAI API, e.g.:\n"
                 f"  chat/vision:  OLLAMA_NUM_PARALLEL=8 ollama serve  &&  "
                 f"ollama pull {args.model}\n"
                 f"  images:       any server exposing /v1/images/generations "
                 f"(LocalAI, sd.cpp, ...)")


# ── Process sampling: the model server's CPU time + RSS ──────────────────────
def _parse_ps_time(s: str) -> float:
    """`ps -o time` -> seconds. Format is [[dd-]hh:]mm:ss[.ss]."""
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        days = int(d)
    parts = [float(p) for p in s.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    h, m, sec = parts[-3], parts[-2], parts[-1]
    return days * 86400 + h * 3600 + m * 60 + sec

#Takes a snapshot of how much RAM and total CPU time the model server is using right now
def sample_server(proc_re: "re.Pattern") -> tuple[float, float]:
    """(total RSS in MB, total CPU-seconds) summed across every process whose
    command matches `proc_re` — captures the server plus any inference child.
    Our own process is excluded."""
    out = subprocess.run(
        ["ps", "-axo", "rss=,time=,command="],
        capture_output=True, text=True).stdout
    rss_kb = 0
    cpu_s = 0.0
    for line in out.splitlines():
        if "calibrate.py" in line:
            continue
        if not proc_re.search(line):
            continue
        parts = line.split(None, 2)  # rss, time, command
        if len(parts) < 3:
            continue
        try:
            rss_kb += int(parts[0])
            cpu_s += _parse_ps_time(parts[1])
        except ValueError:
            continue
    return rss_kb / 1024.0, cpu_s


#Samples the CPU and RAM over a period of time by using the sample_server function
def _collect(alive, proc_re: "re.Pattern", on_sample=None,
             net_stats: dict | None = None,
             net_lock: threading.Lock | None = None) -> tuple[float, float, float]:
    """Sample the server once per second while `alive()` is true. Returns
    (cpu millicores over the window, mean RSS MB, wall seconds) — the exact
    whole-window aggregate, same as always.

    If `on_sample` is given, it's called every second with a live
    (cpu_millicores, ram_mb, net_mbps) reading — a plain second-over-second
    delta, not the whole-window aggregate — so a caller can publish it (e.g.
    to Prometheus) while the level is still running. Net requires `net_stats`
    (the shared byte counter `_worker()` writes into) + its `net_lock`; without
    them it's reported as 0 (correct for the x=0 idle baseline, which has no
    requests in flight at all)."""
    _, cpu0 = sample_server(proc_re)
    t0 = time.monotonic()
    rss: list[float] = []
    prev_t, prev_cpu = t0, cpu0
    prev_net_t = t0
    prev_net_bytes = net_stats["bytes"] if net_stats is not None else 0
    while alive():
        time.sleep(1.0)
        mb, cpu_now = sample_server(proc_re)
        rss.append(mb)
        now = time.monotonic()
        if on_sample is not None:
            live_cpu_mc = max(0.0, (cpu_now - prev_cpu)
                              / max(1e-6, now - prev_t) * 1000.0)
            live_net_mbps = 0.0
            if net_stats is not None:
                with net_lock:
                    cur_bytes = net_stats["bytes"]
                live_net_mbps = max(0.0, (cur_bytes - prev_net_bytes) * 8 / 1e6
                                    / max(1e-6, now - prev_net_t))
                prev_net_t, prev_net_bytes = now, cur_bytes
            on_sample(live_cpu_mc, mb, live_net_mbps)
        prev_t, prev_cpu = now, cpu_now
    _, cpu1 = sample_server(proc_re)
    wall = max(1e-6, time.monotonic() - t0)
    # max(0, ...): CPU/RAM can never be negative. A negative delta here means
    # a matched process (e.g. a restarted worker) disappeared mid-window, so
    # its accumulated CPU time dropped out of the "after" sum — a measurement
    # artifact, never real signal.
    cpu_millicores = max(0.0, (cpu1 - cpu0) / wall * 1000.0)
    ram_mb = max(0.0, statistics.fmean(rss) if rss else 0.0)
    return cpu_millicores, ram_mb, wall

# Simulates one user sending multiple requests back-to-back as fast as the server can answer them
def _worker(url: str, kwargs: dict, mode: str, deadline: float,
            stats: dict, lock: threading.Lock) -> None:
    """One virtual user: keep a single request in flight until the deadline.
    A request already in flight at the deadline is allowed to finish (and is
    counted) — the level's rates are computed over the actual wall window."""
    while time.monotonic() < deadline:
        t0 = time.monotonic()
        try:
            resp = requests.post(url, timeout=REQUEST_TIMEOUT_S, **kwargs)
            resp.raise_for_status()
            data = resp.content
        except Exception:
            with lock:
                stats["failed"] += 1
            time.sleep(1.0)  # don't hammer a down/erroring server
            continue
        latency = time.monotonic() - t0
        tokens, images = parse_response(mode, data)
        with lock:
            stats["requests"] += 1
            # resp.request.body is the exact bytes requests put on the wire —
            # the JSON payload, or the encoded multipart body for image2image.
            stats["bytes"] += len(resp.request.body or b"") + len(data)
            stats["tokens"] += tokens
            stats["images"] += images
            stats["latencies"].append(latency)

# The orchestrator that ties worker and collect together
# What it does: given one single x value from your sweep,
# runs the full measurement for that level and hands back one row of results — CPU, RAM, net, throughput, everything.
# This gets called once per level in your --x 0 1 2 4 8 sweep.
def measure_level(x: int, args, url: str, kwargs: dict,
                  proc_re: "re.Pattern") -> dict:
    """Drive the model at concurrency `x` and return measured CPU/RAM/net.
    x=0 measures the idle baseline with no load."""
    labels = LIVE_X.labels(model=args.model, mode=args.mode)
    labels.set(x)
    def publish(cpu_mc: float, ram_mb: float, net_mbps: float) -> None:
        LIVE_CPU.labels(model=args.model, mode=args.mode).set(cpu_mc)
        LIVE_RAM.labels(model=args.model, mode=args.mode).set(ram_mb)
        LIVE_NET.labels(model=args.model, mode=args.mode).set(net_mbps)

    if x == 0:
        t0 = time.monotonic()
        cpu, ram, _ = _collect(
            lambda: time.monotonic() - t0 < args.duration, proc_re,
            on_sample=publish)
        return {"x": 0, "cpu_millicores": round(cpu, 1),
                "ram_mb": round(ram, 1), "net_mbps": 0.0, "throughput": 0.0,
                "requests": 0, "failed_rate": 0.0, "latency_ms": 0.0}

    stats = {"requests": 0, "failed": 0, "bytes": 0,
             "tokens": 0, "images": 0, "latencies": []}
    lock = threading.Lock()
    deadline = time.monotonic() + args.duration
    threads = [threading.Thread(target=_worker,
                                args=(url, kwargs, args.mode,
                                      deadline, stats, lock),
                                daemon=True)
               for _ in range(x)]
    for t in threads:
        t.start()
    cpu, ram, wall = _collect(
        lambda: any(t.is_alive() for t in threads), proc_re,
        on_sample=publish, net_stats=stats, net_lock=lock)
    for t in threads:
        t.join()

    net_mbps = stats["bytes"] * 8 / 1e6 / wall
    if args.mode in CHAT_MODES:
        throughput = stats["tokens"] / wall            # tokens/s
    else:
        throughput = stats["images"] / wall * 60.0     # images/min
    attempts = stats["requests"] + stats["failed"]
    return {
        "x": x,
        "cpu_millicores": round(cpu, 1),
        "ram_mb": round(ram, 1),
        "net_mbps": round(net_mbps, 4),
        "throughput": round(throughput, 2),
        "requests": stats["requests"],
        "failed_rate": round(stats["failed"] / attempts, 3) if attempts else 0.0,
        "latency_ms": round(statistics.fmean(stats["latencies"]) * 1000, 1)
                      if stats["latencies"] else 0.0,
    }


# Finds the single straight line value (y = ax + b)
def fit(points: list[tuple[float, float]]) -> tuple[float, float, float]:
    """Ordinary least squares y = a*x + b over (x, y) points; returns (a, b, R²)."""
    if len(points) < 2:
        return 0.0, (points[0][1] if points else 0.0), 0.0
    xs, ys = zip(*points)
    try:
        a, b = statistics.linear_regression(xs, ys)
    except statistics.StatisticsError:  # every x identical — no slope to fit
        return 0.0, statistics.fmean(ys), 0.0
    try:
        r2 = statistics.correlation(xs, ys) ** 2
    except statistics.StatisticsError:  # every y identical — a flat line fits perfectly
        r2 = 1.0
    return a, b, r2


# ── Orchestration ────────────────────────────────────────────────────────────

AXES = (("cpu", "cpu_millicores", "millicores"),
        ("ram", "ram_mb", "MB"),
        ("net", "net_mbps", "Mbps"))

def main() -> None:
    run_started = datetime.now()  # stamps the default output filename, so
                                  # repeat runs never overwrite each other
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=list(DEFAULT_ENDPOINTS), default="text2text",
                    help="what goes in and what comes out (default text2text)")
    ap.add_argument("--model", default="llama3.2:3b",
                    help="model name as the server knows it")
    ap.add_argument("--host", default="http://localhost:11434",
                    help="server base URL (Ollama 11434, LocalAI/vLLM 8080/8000, …)")
    ap.add_argument("--endpoint", default=None,
                    help="override the OpenAI-compatible endpoint the mode implies")
    ap.add_argument("--x", type=int, nargs="+", default=[0, 1, 2, 4, 8],
                    help="concurrency sweep; include 0 for the idle baseline")
    ap.add_argument("--duration", type=float, default=None,
                    help="measured window per level, seconds (default 40; "
                         "120 for image output — use more if one request is slower)")
    ap.add_argument("--prompt",
                    default="Explain how a CPU pipeline works, step by step, in detail.",
                    help="text input (chat modes) or generation prompt (image modes)")
    ap.add_argument("--image", default=None,
                    help="input image for image2text/image2image, e.g. "
                         "calibration/images/photo.jpg (default: built-in "
                         "synthetic PNG). Path is resolved relative to your "
                         "current directory, not this script's location.")
    ap.add_argument("--max-tokens", type=int, default=200,
                    help="completion cap for the chat modes")
    ap.add_argument("--size", default="512x512",
                    help="output size for the image modes")
    ap.add_argument("--proc-match",
                    default=r"ollama|llama[-_.]?(server|cpp)|vllm|mlx|lm[-_ ]?studio"
                            r"|text-generation|local[-_]?ai|stable[-_ ]?diffusion"
                            r"|sd[-_]?server|comfyui|invokeai|a1111|fooocus",
                    help="case-insensitive regex matching the model server's process")
    ap.add_argument("--out", default=None,
                    help="CSV path (default calibration/<model>-<mode>.csv); "
                         "the .fit.json lands next to it")
    ap.add_argument("--metrics-port", type=int, default=9877,
                    help="port for a live Prometheus /metrics endpoint, "
                         "updated ~1/s while the sweep runs (for watching in "
                         "Grafana alongside the emulator); 0 disables it")
    args = ap.parse_args()

    if args.duration is None:
        args.duration = 120.0 if args.mode in IMAGE_OUT_MODES else 40.0
    proc_re = re.compile(args.proc_match, re.IGNORECASE)
    image = load_image(args.image) if args.mode in ("image2text", "image2image") \
        else (b"", "")
    url, kwargs = build_request(args, image)

    if args.metrics_port:
        try:
            start_http_server(args.metrics_port)
            print(f"live metrics: http://0.0.0.0:{args.metrics_port}/metrics",
                  flush=True)
        except OSError as exc:
            print(f"WARNING: could not start the live metrics server on port "
                  f"{args.metrics_port} ({exc}) — continuing without it. Pass "
                  f"--metrics-port to use a different one, or 0 to disable.",
                  flush=True)

    preload(args, url, kwargs)

    rows: list[dict] = []
    for x in args.x:
        print(f"\n── level x={x} ({int(args.duration)}s) ──", flush=True)
        row = measure_level(x, args, url, kwargs, proc_re)
        rows.append(row)
        thr_unit = "tok/s" if args.mode in CHAT_MODES else "img/min"
        print(f"  cpu={row['cpu_millicores']}mc  ram={row['ram_mb']}MB  "
              f"net={row['net_mbps']}Mbps  {row['throughput']}{thr_unit}  "
              f"reqs={row['requests']}  fail={row['failed_rate']}  "
              f"lat={row['latency_ms']}ms", flush=True)
        if x > 0 and row["requests"] == 0:
            print("  WARNING: no successful requests at this level — "
                  "the fit will be unreliable", flush=True)

    fits = {}
    for axis, key, _unit in AXES:
        a, b, r2 = fit([(r["x"], r[key]) for r in rows])
        fits[axis] = {"a": round(a, 4), "b": round(b, 2), "r2": round(r2, 4)}

    # The fitted line evaluated at each measured x, as extra row columns —
    # so the Grafana dashboard can plot measured points and the fit line
    # straight from the `rows` table, no computation on the Grafana side.
    for r in rows:
        for axis, key, _unit in AXES:
            r[f"{axis}_fit"] = round(
                max(0.0, fits[axis]["a"] * r["x"] + fits[axis]["b"]), 4)

    safe_model = re.sub(r"[^A-Za-z0-9._-]+", "-", args.model)
    timestamp = run_started.strftime("%Y%m%d-%H%M%S")
    out_path = args.out or os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        f"{safe_model}-{args.mode}-{timestamp}.csv")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    cols = ["x", "cpu_millicores", "ram_mb", "net_mbps",
            "throughput", "requests", "failed_rate", "latency_ms"]
    with open(out_path, "w") as f:
        f.write(",".join(cols) + "\n")
        for r in rows:
            f.write(",".join(str(r[c]) for c in cols) + "\n")

    thr_unit = "tok/s" if args.mode in CHAT_MODES else "img/min"
    load_block = {ax: {"a": fits[ax]["a"], "b": fits[ax]["b"]}
                  for ax in ("cpu", "ram", "net")}
    json_path = os.path.splitext(out_path)[0] + ".fit.json"
    with open(json_path, "w") as f:
        json.dump({"model": args.model, "mode": args.mode, "x": args.x,
                   "run_started": run_started.isoformat(timespec="seconds"),
                   "throughput_unit": thr_unit, "rows": rows, "fits": fits,
                   # fits as an array + load as a string: the shapes the
                   # Grafana calibration dashboard's table/stat panels want.
                   "fit_table": [{"axis": ax, "unit": unit, **fits[ax]}
                                 for ax, _key, unit in AXES],
                   "load": load_block,
                   "load_json": json.dumps(load_block, separators=(",", ":")),
                   }, f, indent=2)

    print("\n" + "=" * 64)
    print(f"wrote {out_path}\nwrote {json_path}")
    print(f"visualise:  python3 -m http.server 8000 --directory "
          f"{os.path.dirname(json_path) or '.'}\n"
          f"  then open Grafana → 'Model calibration' and set its fit.json "
          f"file variable to /{os.path.basename(json_path)}")
    print("\nfit  load = a*x + b   (R² in parens)")
    for ax in ("cpu", "ram", "net"):
        unit = {"cpu": "mc", "ram": "MB", "net": "Mbps"}[ax]
        print(f"  {ax:<3} a={fits[ax]['a']:<10} b={fits[ax]['b']:<10} "
              f"({unit})  R²={fits[ax]['r2']}")
    print("\npaste into a template app:")
    print("  " + json.dumps(load_block, separators=(",", ":")))


if __name__ == "__main__":
    main()
