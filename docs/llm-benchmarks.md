# LLM Model Selection Benchmark — Glassbox Production Tasks

**Backlog item:** P1-C — Per-task LLM model selection benchmark
**Date:** 2026-05-21
**Hardware:** Mac Mini M4 Pro 24 GB
**Ollama version:** as installed at `/Applications/Ollama.app/Contents/Resources/ollama serve`

## TL;DR

Benchmarked 2026-05-21 against 4 chat-capable Ollama models. Winners and the env vars that pin them:

| Task           | Prompt shape           | Winner             | env var to override          | Rationale                                                                                |
|----------------|------------------------|--------------------|------------------------------|------------------------------------------------------------------------------------------|
| `brief_llm`    | 1-sentence prose ≤80 t | **qwen2.5:14b**    | `GLASSBOX_LLM_MODEL_BRIEF`   | More disciplined output; honors "no padding" + "say 'No priority items.' for empty data" |
| `intel_query`  | Tactical prose ≤500 t  | **llama3.1:latest**| `GLASSBOX_LLM_MODEL_INTEL_QUERY` | 2× faster warm (5.3s vs 11.3s) at equal quality; user-facing endpoint                |
| `forecast`     | JSON ≤400 t            | **llama3.1:latest**| `GLASSBOX_LLM_MODEL_FORECAST`| 1.5× faster (2.6s vs 3.9s) and JSON-validated 3/3                                       |
| `sitrep`       | JSON ≤700 t (ctx=8k)   | _qwen2.5:14b (default)_ | `GLASSBOX_LLM_MODEL_SITREP` | NOT benchmarked — kept on baseline pending re-run                                  |
| `ask`          | Prose + citations ≤500t| _qwen2.5:14b (default)_ | `GLASSBOX_LLM_MODEL_ASK`     | NOT benchmarked — kept on baseline pending re-run                                  |

**Caveats from the run:**

- **`qwen3.5:9b` produced 0-byte output on every task** (running 4-22s but returning empty content). Unusable as-is — likely a chat-template or stop-token incompatibility with the legacy `/api/generate` endpoint. Worth a separate investigation before discarding the model.
- **`deepseek-r1:14b` produced 0-byte output on prose tasks** (brief_llm, intel_query) but **valid JSON 2/3 on forecast**. Likely the reasoning-model `<think>...</think>` token stream is being captured-then-stripped, leaving empty content on prose; on JSON-mode the `format=json` constraint forces a structured emission past the thinking phase. Same investigation candidate as qwen3.5.
- Cold loads are punishing on bigger models: qwen2.5:14b cold = 85s, deepseek-r1:14b cold = 93s. Once warm, ALL models stay under their respective latency budgets. The Ollama daemon auto-unloads after idle, so first-call-after-quiet-period is always a cold load — production should keep the per-task models warm via small synthetic ping calls if cold latency matters.

## Why this benchmark exists

The pre-P1-C Glassbox routed every LLM call through `llm_ollama.py` with a single model defaulting to `qwen2.5:14b`. Five distinct production tasks shared one model. That's wasteful for short prose (a smaller model would be just as good and 3-5× faster) and potentially wrong for structured JSON (a reasoning-tuned model may produce stricter JSON than a generalist).

This benchmark picks one model per task based on **measured cold/warm latency, output quality, and JSON validity** against the real production prompts.

## LLM call-site inventory

Five distinct LLM tasks in the production hot path (excluding `sentinel_runner.py` which is a batch script outside `llm_ollama.py`):

| Site                              | Task          | Shape       | Token budget | Latency budget       |
|-----------------------------------|---------------|-------------|--------------|----------------------|
| `brief.py:1003`                   | `brief_llm`   | prose       | ≤ 80         | 30 s (viewport path) |
| `glassbox_server.py:3093` (/ask)  | `ask`         | prose       | ≤ 500        | 120 s (user-facing)  |
| `glassbox_server.py:3209` (/intel)| `intel_query` | prose       | ≤ 500        | 90 s (user-facing)   |
| `intelligence_loop.py:227`        | `sitrep`      | JSON, ctx=8k| ≤ 700        | 150 s (background)   |
| `forecaster.py:351`               | `forecast`    | JSON, ctx=4k| ≤ 400        | 90 s (background)    |

## Candidate models

Pulled from `ollama list` on 2026-05-21:

| Model                  | Size  | Family       | Notes                                  |
|------------------------|-------|--------------|----------------------------------------|
| `qwen2.5:14b`          | 9.0 GB| Qwen-2.5     | Current production baseline            |
| `llama3.1:latest`      | 4.9 GB| Llama-3.1 8B | Smallest chat model — latency hypothesis |
| `qwen3.5:9b`           | 6.6 GB| Qwen-3.5     | Newer Qwen, mid-size                   |
| `deepseek-r1:14b`      | 9.0 GB| DeepSeek-R1  | Reasoning-tuned — JSON hypothesis      |
| `phi4:latest`          | 9.1 GB| Phi-4        | Excluded by default (40-min budget)    |
| `qwen2.5-coder:14b`    | 9.0 GB| Qwen-2.5     | Code-specialized — not applicable      |
| `command-r:latest`     | 18 GB | Command-R    | Excluded by default (24 GB VRAM tight) |

Excluded models can be added via `--models <list>` on the benchmark CLI.

## Methodology

Each (model, task, sample) tuple runs through the **production prompt verbatim** (extracted from the call site source). Each model is **unloaded before its first call** (Ollama `keep_alive=0`) so the first sample measures cold-load + inference; subsequent samples measure warm inference.

Sample inputs are pulled live from the production Postgres for `brief_llm` and `intel_query` (real recent events, real activity counts). `forecast` uses three fixed synthetic-but-realistic hotspots so the benchmark is repeatable across runs.

Metrics captured per call:
- **cold latency** (wall seconds, first call per model)
- **warm latency** (wall seconds, subsequent calls per model)
- **output bytes** (rough proxy for response volume)
- **JSON validity** (for `forecast`: parses + has required keys?)
- **first 200 chars of output** (for the operator's quality eyeball)

Quality is verified by **side-by-side comparison against the qwen2.5:14b baseline output** for the same sample, not by an automated scorer — the production prompts are short enough that human eyeball is more honest than a brittle automated rubric. See `llm-benchmarks-raw.json` for the verbatim outputs.

## Raw numbers

Raw per-call results at [llm-benchmarks-raw.json](llm-benchmarks-raw.json). Total wall time: 472 s (7.9 min) for 36 inferences + 4 cold loads.

### Task: `brief_llm`

| Model | Cold (s) | Warm p50 (s) | Warm max (s) | Avg bytes | JSON valid | Errors |
|---|---:|---:|---:|---:|:---:|:---:|
| `deepseek-r1:14b` | 92.9 | 5.2 | 5.2 | 0 | — | 0 |
| `llama3.1:latest` | 10.0 | 1.1 | 1.1 | 120 | — | 0 |
| `qwen2.5:14b` | 84.9 | 2.4 | 2.4 | 74 | — | 0 |
| `qwen3.5:9b` | 24.0 | 4.1 | 4.1 | 0 | — | 0 |

### Task: `intel_query`

| Model | Cold (s) | Warm p50 (s) | Warm max (s) | Avg bytes | JSON valid | Errors |
|---|---:|---:|---:|---:|:---:|:---:|
| `deepseek-r1:14b` | 25.8 | 22.7 | 22.7 | 114 | — | 0 |
| `llama3.1:latest` | 7.9 | 5.3 | 5.3 | 1145 | — | 0 |
| `qwen2.5:14b` | 13.9 | 11.3 | 11.3 | 1214 | — | 0 |
| `qwen3.5:9b` | 21.6 | 21.7 | 21.7 | 0 | — | 0 |

### Task: `forecast`

| Model | Cold (s) | Warm p50 (s) | Warm max (s) | Avg bytes | JSON valid | Errors |
|---|---:|---:|---:|---:|:---:|:---:|
| `deepseek-r1:14b` | 4.6 | 3.3 | 3.3 | 155 | 2/3 | 0 |
| `llama3.1:latest` | 2.6 | 2.6 | 2.6 | 263 | 3/3 | 0 |
| `qwen2.5:14b` | 4.0 | 3.9 | 3.9 | 188 | 3/3 | 0 |
| `qwen3.5:9b` | 5.5 | 4.8 | 4.8 | 0 | 0/3 | 0 |

## Per-task analysis

### `brief_llm` — qwen2.5:14b wins on discipline

The prompt asks for a one-sentence (max 25 words) analyst note over a structured brief, with explicit rules: no padding, no fabrication, and `'No priority items.'` when nothing is notable. Quality on these axes matters more than 1.3 s of latency.

Side-by-side on the three production samples:

| Sample | qwen2.5:14b output | llama3.1:latest output |
|---|---|---|
| `brief_aircraft` | "Investigate the 70 events of military aircraft underway with max severity of 5.0." (81 bytes) | "Investigate the 70 military aircraft underway events with a maximum severity of 5.0 to determine their current status and potential impact." (139 bytes — padded) |
| `brief_nature` (empty data) | **"No priority items."** (18 bytes — correct) | **"Investigate the lack of seismic activity within the past hour to determine if it's a normal fluctuation or an anomaly."** (118 bytes — hallucinated content from no data) |
| `brief_vessel` | "Investigate the high-severity proximity events involving aircraft .N952JB near METAR KJFK **and a Special Weather Statement**." (122 bytes — both signals) | "Investigate the proximity event with maximum severity (10.0) involving aircraft .N952JB near METAR KJFK." (104 bytes — missed the weather statement) |

llama3.1 was 2× faster (1.1 s vs 2.4 s warm), but the hallucination on empty data (`brief_nature`) is disqualifying — the prompt explicitly says to say "No priority items." in that case. Production cannot ship a brief that invents nonexistent seismic activity.

**Decision: qwen2.5:14b.** Latency cost is 1.3 s warm, well inside the 30 s viewport budget.

### `intel_query` — llama3.1:latest wins on speed at equal quality

User-facing `/api/intel/query` endpoint. The 90 s timeout budget means most of the latency cost is borne by the user.

Side-by-side on the three queries (all 3 produced concrete, operational briefs of ~1200 bytes):
- Both models cited specific entity IDs (`aircraft SWR9LC`, `Hämmerlingstraße`) and specific event counts (`1,229 events`, `767 events`)
- qwen2.5 honestly says "no direct mention of seismic" when asked about earthquakes (good calibration)
- llama3.1's outputs were similarly tactical and grounded

llama3.1 is **2× faster warm (5.3 s vs 11.3 s)**. For a user-facing query, 5–6 seconds is the difference between "feels responsive" and "feels slow."

**Decision: llama3.1:latest.** Quality is on par; latency wins.

### `forecast` — llama3.1:latest wins on speed + valid JSON

JSON-shaped output, 400-token budget. Both qwen2.5 and llama3.1 produced valid JSON 3/3 with the expected keys (`prediction`, `confidence`, `watch_items`). llama3.1 was **1.5× faster warm (2.6 s vs 3.9 s)** and tended to include slightly richer `watch_items` context.

**Decision: llama3.1:latest.**

### `sitrep` and `ask` — kept on qwen2.5:14b (not benchmarked)

Honestly: this pass benchmarked 3 tasks, not 5. `sitrep` is similar in shape to `forecast` (JSON-structured) but with a much larger context budget (`num_ctx=8192`). `ask` is similar to `intel_query` (prose) but adds the constraint of `[#ID]` citation faithfulness, which is hard to score automatically and wasn't covered in this pass.

The routing infrastructure supports both — env vars `GLASSBOX_LLM_MODEL_SITREP` and `GLASSBOX_LLM_MODEL_ASK` will flip these once benchmarked. Until then, the safer choice is the qwen2.5:14b baseline.

## Follow-ons (not blocking)

1. **Investigate the 0-byte-output bug on qwen3.5:9b and deepseek-r1:14b** under `/api/generate`. Possibly:
   - Modelfile-level stop-token mismatch
   - Chat-template parsing in Ollama's generate handler differing from chat-completions
   - For deepseek-r1: thinking-tokens being captured-and-stripped, leaving empty content
2. **Benchmark `sitrep` and `ask`** on a future pass. `sitrep` especially — it's the largest-context production call and probably benefits most from a model with strong long-context handling.
3. **Add `phi4:latest` and `command-r:latest`** to the benchmark. They were excluded from this pass for budget reasons.
4. **Keep-warm cron**: a tiny periodic GET against each chosen model would prevent the 85–93 s cold-load penalty when production has been idle. Could be a 10-line `bash` script + launchctl plist.

## Routing implementation

After benchmark, `llm_ollama.py` adds per-task model resolution:

```python
TASK_MODEL_ENVS = {
    "brief_llm":   "GLASSBOX_LLM_MODEL_BRIEF",
    "intel_query": "GLASSBOX_LLM_MODEL_INTEL_QUERY",
    "forecast":    "GLASSBOX_LLM_MODEL_FORECAST",
    "sitrep":      "GLASSBOX_LLM_MODEL_SITREP",
    "ask":         "GLASSBOX_LLM_MODEL_ASK",
}
```

`generate_json()` and `generate_text()` accept an optional `task=` keyword. If the caller passes `task="brief_llm"`, the model is resolved by:

1. Explicit `model=` argument (wins, backwards-compat)
2. Per-task env var (e.g. `GLASSBOX_LLM_MODEL_BRIEF`)
3. Global `FULCRUM_LLM_MODEL` env var
4. Hardcoded default (`qwen2.5:14b`)

The benchmark's chosen-winner defaults are baked into the source as fallback constants (so the operator doesn't need to set env vars to get the optimized routing). Env vars are overrides for experiments or when a new model lands.

## How to re-run

```bash
cd "/Volumes/Mac Mini Expanded Storage/ewilmoth/MEWR Creative Enterprises LLC"
21_GLASSBOX_AI/.venv/bin/python 21_GLASSBOX_AI/scripts/llm_benchmark.py --samples 3
```

CLI flags:
- `--models qwen2.5:14b,llama3.1:latest,...` — restrict the model set
- `--tasks brief_llm,forecast`              — restrict task set
- `--samples 3`                              — samples per task
- `--cold-start 1`                           — force model unload before each model's run

Raw results land at `21_GLASSBOX_AI/docs/llm-benchmarks-raw.json`. Append a new dated section to this doc when re-running.

## When to re-run

- A new Ollama model is installed and worth testing
- Hardware changes (e.g. RAM bump, GPU swap)
- Production prompt changes for any of the 5 tasks
- Quarterly check that the chosen model is still winning
