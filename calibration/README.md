# Model calibration

This tool sends real requests to a real model running on your machine and
measures its real CPU/RAM/network cost — a one-time measurement per model.
The point of doing that: it turns the model's footprint into the emulator's
linear load function, so that *afterward*, the emulator can spin up many
lightweight fake copies of that footprint at cluster scale (via `stress-ng`
/ `iperf3` worker pods — see [worker/loads.py](../worker/loads.py)) without
needing a real model running on every pod.

```
load = max(0, a * x + b)      cpu → millicores, ram → MB, net → Mbps
```

You choose the sweep of **x** values (x = concurrent in-flight requests — the
same load signal a template injects at a source app); the calibrator drives
the model at each level, measures the server's CPU/RAM/network, and
least-squares fits **a** and **b** per axis. The resulting block pastes
straight into a template app (see [TEMPLATE.md](../TEMPLATE.md), "The formula",
and [templates/llm-load.json](../templates/llm-load.json) for a real example).

One script, no extra binaries. Needs Python 3.10+ and the `requests` +
`prometheus_client` libraries — a virtual environment is the cleanest way to
get both without touching your system Python:

```bash
python3 -m venv calibration/.venv
calibration/.venv/bin/pip install -r calibration/requirements.txt
chmod +x calibration/calibrate.py
```

That's a **one-time** setup. `calibrate.py`'s shebang line points straight at
`calibration/.venv/bin/python3`, so every example below just runs
`calibration/calibrate.py ...` directly — no `python3` prefix, no
activating, no venv to remember, in any terminal tab. (This does mean the
shebang hardcodes an absolute path tied to *this* checkout — if you ever move
or re-clone the repo, re-run the `chmod +x` line above and update the first
line of `calibrate.py` to the new path.)

## Quick start

Any OpenAI-compatible server works (Ollama, vLLM, llama.cpp, LM Studio,
LocalAI, sd.cpp, …). Pick the mode that matches your model:

```bash
# text -> text  (e.g. Ollama)
ollama pull llama3.2:3b
OLLAMA_NUM_PARALLEL=8 ollama serve &
calibration/calibrate.py --mode text2text --model llama3.2:3b

# image -> text  (vision model; a synthetic PNG is built in, or --image calibration/images/photo.jpg)
ollama pull llava:7b
calibration/calibrate.py --mode image2text --model llava:7b

# text -> image  (any server with /v1/images/generations, e.g. LocalAI)
calibration/calibrate.py --mode text2image --model sd-1.5 \
    --host http://localhost:8080 --x 0 1 2 4

# image -> image
calibration/calibrate.py --mode image2image --model sd-1.5 \
    --host http://localhost:8080 --image calibration/images/in.png --x 0 1 2 4
```

A typical run still spans multiple terminal tabs (the model server in one,
`python3 -m http.server` for Grafana in another, `calibrate.py` in a third) —
but since there's no activation step anymore, a fresh tab just works, no
extra setup needed in it.

### Customizing the input

The defaults (a canned prompt, a synthetic gradient PNG) get you running
immediately, but a model's CPU/RAM/network cost can depend on what you feed
it — calibrating with input that resembles your real traffic gives a more
representative fit:

```bash
# your own prompt
calibration/calibrate.py --mode text2text --model llama3.2:3b \
    --prompt "Summarize this support ticket in three sentences."

# your own image, with your own prompt about it
calibration/calibrate.py --mode image2text --model llava:7b \
    --image calibration/images/photo.png --prompt "List every object visible in this photo."

# your own generation prompt (image-output modes)
calibration/calibrate.py --mode text2image --model sd-1.5 \
    --host http://localhost:8080 \
    --prompt "a photorealistic mountain landscape at sunset"

# your own source image to edit, with your own prompt
calibration/calibrate.py --mode image2image --model sd-1.5 \
    --host http://localhost:8080 --image calibration/images/in.png \
    --prompt "add a red bicycle leaning against the wall"
```

`--image` is resolved relative to whatever directory you *run* the command
from, not relative to `calibrate.py` itself — if you're running from the repo
root (as in every example here), that means `calibration/images/...`, not
just the bare filename.

Test images of your own go in **`calibration/images/`** — that folder is
gitignored, so drop in and delete whatever you're testing with freely,
nothing there ever gets committed.

`--prompt` also accepts longer strings — quote it and it can be a whole
paragraph. `--max-tokens` and `--size` (see [All flags](#all-flags)) shape the
output side the same way.

Each run writes, next to this README:

- `<model>-<mode>-<timestamp>.csv` — the measured levels
- `<model>-<mode>-<timestamp>.fit.json` — everything, including the paste-ready `load` block

`<timestamp>` is the run's start time (`YYYYMMDD-HHMMSS`), so re-running the
same model/mode never overwrites a previous result — every run gets its own
pair of files, and they sort chronologically. The `.fit.json` also carries
the same timestamp as a `run_started` field, in case the file ever gets
renamed. Pass `--out` yourself to skip this and use an exact name of your
choosing (that name is used as-is, no timestamp added).

and prints the block to paste into a template app:

```json
{"cpu":{"a":13.69,"b":68.45},"ram":{"a":4.34,"b":5537.55},"net":{"a":0.0004,"b":0.0}}
```

## Using other servers / models

Ollama is just the default — `calibrate.py` works with **any** server that
speaks the OpenAI-compatible API, via `--host` (and `--endpoint` only if a
server puts its routes somewhere non-standard):

| Server | Start it | Point calibrate.py at it |
|--------|----------|---------------------------|
| **Ollama** | `OLLAMA_NUM_PARALLEL=8 ollama serve` | `--host http://localhost:11434` (the default) |
| **llama.cpp** (`llama-server`) | `llama-server -m model.gguf --port 8080` | `--host http://localhost:8080` |
| **vLLM** | `vllm serve <model> --port 8000` | `--host http://localhost:8000 --model <model>` |
| **LM Studio** | Start its local server (GUI "Local Server" tab, or `lms server start`) | `--host http://localhost:1234` |
| **LocalAI** | `docker run -p 8080:8080 localai/localai` | `--host http://localhost:8080` |
| **TGI** (text-generation-inference) | `docker run -p 8080:80 ... ghcr.io/huggingface/text-generation-inference` | `--host http://localhost:8080` |

`--model` must exactly match whatever name the server expects — what you
`ollama pull`ed, the GGUF filename, the vLLM `--model` argument, etc. — the
calibrator just forwards it in every request payload; it never inspects or
validates it locally.

`text2image`/`image2image` need a server that exposes
`/v1/images/generations`/`/v1/images/edits` — Ollama doesn't have these
routes, so use LocalAI or an sd.cpp/ComfyUI wrapper exposing that API instead.

If your server's process name doesn't match the default `--proc-match` regex
(see [All flags](#all-flags)), CPU/RAM sampling won't find it — point
`--proc-match` at a regex matching your server's binary name.

## Visualise in Grafana

The **Model calibration** dashboard ([grafana/grafana-calibration.json](../grafana/grafana-calibration.json),
auto-provisioned like the other dashboards — see RUNBOOK.md) plots measured
points vs. the fitted line per axis, the a/b/R² table, and the paste-ready
block. It reads the `.fit.json` through the provisioned **Infinity
Calibration** datasource (`manifests/monitoring-values.yaml`), whose base URL
points at a plain file server on the host. So all you do is serve this
directory over HTTP:

```bash
python3 -m http.server 8000 --directory calibration
```

then open the dashboard and set its **fit.json file** variable, e.g.
`/llama3.2-3b-text2text.fit.json`. The datasource's base URL is
`http://192.168.2.1:8000` — the host's address as seen from the Grafana pod
(the VM's default gateway on the multipass bridge). If yours differs, change
it in `monitoring-values.yaml` and re-apply the values (RUNBOOK.md step 5).

That dashboard shows a **completed** run. To watch CPU/RAM/network live while
a run is actually happening — e.g. to eyeball it side-by-side with the
emulator's own panels — see the next section instead.

## Watching a run live, alongside the emulator

Every run also exposes a live Prometheus `/metrics` endpoint
(`--metrics-port`, default `9877`) with `calibration_cpu_millicores` /
`calibration_ram_mb` / `calibration_net_mbps` / `calibration_x`, updated
about once a second — this is a separate mechanism from the `.fit.json`
dashboard above, meant for watching a run *as it happens* rather than
reviewing one after the fact.

One-time cluster setup — point the in-cluster Prometheus at your Mac, the
same way `manifests/host-calibration-scrape.yaml` documents:

```bash
multipass exec microk8s-vm -- microk8s kubectl apply -f - \
    < <(sed 's/HOST_IP/192.168.2.1/' manifests/host-calibration-scrape.yaml)
```

(`192.168.2.1` is the host's address as seen from the cluster — the same
value used elsewhere in this README; adjust if yours differs.)

Then just run `calibrate.py` as normal — no extra flag needed, live metrics
are on by default. Open the **Worker Emulator — Target vs Actual** dashboard
([grafana/grafana-dashboard.json](../grafana/grafana-dashboard.json)) and
scroll to the **Live model calibration** row at the bottom: three panels
(CPU/RAM/network) update in real time while the sweep runs. Since they're on
the same dashboard as the emulator's own target-vs-actual panels, you can run
a template through the emulator and a real calibration side by side and
compare directly.

The scrape target goes quiet between runs (`calibrate.py` only serves
`/metrics` while it's actually alive) — that's expected, not a fault; the
panels just flatline until the next run starts. Pass `--metrics-port 0` to
disable this endpoint entirely for a given run.

## All flags

| Flag | Default | Meaning |
|------|---------|---------|
| `--mode` | `text2text` | Which modality to calibrate: `text2text`, `image2text`, `text2image`, `image2image`. Determines the endpoint and request shape (see [build_request()](calibrate.py)). |
| `--model` | `llama3.2:3b` | Model name exactly as your server knows it. |
| `--host` | `http://localhost:11434` | Server base URL (Ollama default `:11434`; LocalAI/vLLM commonly `:8080`/`:8000`). |
| `--endpoint` | mode-dependent | Override the OpenAI-compatible endpoint the mode implies — for a non-standard server route. |
| `--x` | `0 1 2 4 8` | The concurrency sweep, space-separated integers. Keep `0` in — it anchors `b`, the idle baseline. |
| `--duration` | `40` (`120` for `text2image`/`image2image`) | Seconds measured per level. Must comfortably exceed one request's latency, or a level measures less than a full request. |
| `--prompt` | *"Explain how a CPU pipeline works, step by step, in detail."* | Text input (chat modes) or generation prompt (image modes) — shape it like your real traffic. |
| `--image` | built-in synthetic PNG | Input image file for `image2text` / `image2image`, e.g. `calibration/images/photo.jpg` — resolved relative to your current directory, not this script's location. Omit to use the generated 512×512 gradient PNG. |
| `--max-tokens` | `200` | Completion token cap for the chat modes (`text2text`, `image2text`). |
| `--size` | `512x512` | Output image size for the image-generation modes (`text2image`, `image2image`). |
| `--proc-match` | regex covering ollama/llama.cpp/vllm/mlx/lm-studio/text-generation/local-ai/stable-diffusion/comfyui/invokeai/a1111/fooocus | Case-insensitive regex matching your model server's process, so `ps`-based CPU/RAM sampling finds it. Override if your server binary is something else. |
| `--out` | `calibration/<model>-<mode>-<timestamp>.csv` | Where to write the CSV. The `.fit.json` lands right next to it, same base name. The default's `<timestamp>` (run start, `YYYYMMDD-HHMMSS`) keeps repeat runs from overwriting each other; an explicit `--out` is used exactly as given, no timestamp added. |
| `--metrics-port` | `9877` | Port for the live Prometheus `/metrics` endpoint (see [Watching a run live](#watching-a-run-live-alongside-the-emulator)). `0` disables it. |

Run `calibration/calibrate.py --help` any time for this same list straight from the source.

## How to read the result

- **`b`** is the model's standing cost at x=0 (for LLMs, mostly the weights
  held in RAM).
- **`a`** is the marginal cost per concurrent request.
- **R²** close to 1 means the linear function describes the model well over
  your sweep. A low R² usually means the server saturated (e.g. it processes
  requests sequentially, so CPU flatlines after x=1) — calibrate over the x
  range you'll actually emulate, and check the Grafana dashboard.
