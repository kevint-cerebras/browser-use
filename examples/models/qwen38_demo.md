# Fast Qwen3.8 browser demo

Paste a task into a CLI, or run the included four-item Amazon shopping task. This branch contains the Python Browser Use runtime changes required by the demo. Use this checkout; installing the latest published `browser-use` package does not reproduce it.

## Run

Requires Python 3.12, [uv](https://docs.astral.sh/uv/), and access to a Qwen3.8-27B-FP8 OpenAI-compatible inference endpoint. Ask the endpoint owner for its URL and API key separately. This branch contains neither credentials nor a shared endpoint URL.

```bash
git clone --branch codex/qwen38-fast-demo --single-branch https://github.com/browser-use/browser-use.git browser-use-fast-demo
cd browser-use-fast-demo
uv sync --no-dev
uv run --no-dev --with playwright playwright install chromium
```

Set these environment variables or put them in a local `.env` file, which Git ignores:

```bash
export QWEN38_DFLASH2_BASE_URL='https://YOUR-ENDPOINT/v1'
export QWEN38_DFLASH2_API_KEY='YOUR-KEY'
```

For the Cerebras Cloud OpenAI-compatible endpoint, also override the model name:

```bash
export QWEN38_DFLASH2_BASE_URL='https://api.cerebras.ai/v1'
export QWEN38_DFLASH2_API_KEY='YOUR-CEREBRAS-KEY'
export QWEN38_DFLASH2_MODEL='qwen-3.8-27b'
```

The runner omits the SGLang-only `top_k` and `repetition_penalty` request fields for the exact
`api.cerebras.ai` hostname while preserving them for the original Modal/DFlash2 deployment.

Run the Amazon task:

```bash
uv run --no-dev --with playwright examples/models/qwen38_modal.py \
  --chromium --keep-open --task-file examples/models/qwen38_amazon_task.txt
```

It adds Hues and Cues, big claw hair clips, Starbursts, and birthday wrapping paper, then proceeds to checkout. The task instructs the agent to stop before payment/address entry or order submission. It reports login/CAPTCHA blockers. These are agent instructions, not a deterministic purchase-prevention mechanism; supervise shopping runs.

For your own task, omit `--task-file`, paste multiple lines, and enter `END` on its own line. `--task '...'` also works. `--keep-open` waits for Enter at the end of an interactive terminal run. To attach to an existing debugging-enabled browser, set `BROWSER_USE_CDP_URL` and omit `--chromium`; the demo disconnects without closing that browser. `BROWSER_USE_EXECUTABLE_PATH` selects a specific installed browser when `--chromium` is omitted.

For a quick connectivity smoke:

```bash
uv run --no-dev --with playwright examples/models/qwen38_modal.py --chromium \
  --task 'Open https://example.com and report its heading. Do not navigate elsewhere.'
```

## Split-pane shopping harness

Launch the Grok-style chat and live browser preview:

```bash
uv run --no-dev --with playwright examples/models/qwen38_grokbot.py
```

Open `http://127.0.0.1:8765`. The left pane converts the party-shopping story
into a concrete brief and progress narrative. The right pane mirrors each agent
step and shows live runtime status. The API key stays server-side, and the agent
keeps the same checkout safety boundary as the terminal demo.

## Inference recipe

The original fast deployment used Qwen/Qwen3.8-27B-FP8 on two B200 GPUs in US West, tensor parallelism 2, DFlash2 speculative decoding with 8 draft tokens, BF16 KV cache, and one concurrent inference request. Its SGLang source was pinned to `746418a1ec78ff1231e452706ce560bcad787c39` with `trtllm_mha` attention and `flashinfer_trtllm` FP8 GEMM. The context budget was 262,144 tokens. This branch connects to an existing endpoint; it does not provision or deploy GPUs.

The CLI requests `reasoning_effort=none` and a small visible JSON thinking field, shares one HTTP connection pool and sticky Modal session, keeps six history items, batches up to five actions, captures vision on demand, and records one final judge screenshot. It retries malformed/truncated output within bounded completion budgets. `--full-browser-capabilities` restores downloads, cross-origin iframes, waits, extraction, search, and file tools excluded by the default navigation profile.

The CLI first waits up to five minutes for a real inference completion, retrying transient startup errors before launching the browser. Authentication errors fail immediately. An unsuccessful agent task returns a nonzero exit status. The demo endpoint was configured for 60 minutes of inactivity before scale-down on September 9, 2026. The next request can incur a several-minute cold start. That setting belongs to the deployment and can change. Concurrent demos share its single-request capacity and can queue.

## Scope and recovery

This is an experimental sharing branch based on Browser Use commit `85ddbfedf`, not a release or a merge into current main. The branch restores bounded state polling, click-delivery checks, prompted-JSON handling, configurable screenshots/judging, and compact Flash thinking. These library changes affect callers within this checkout, including click-result descriptions and malformed-output classification. No persisted-data migration is required. Use a separate clone/environment; returning to your previous checkout restores its behavior.

Browser state and screenshots go to the configured inference provider. Keep credentials in your environment or ignored `.env`; do not commit browser profiles, screenshots, or task logs. The ordinary terminal output includes task/page details.

Validation on September 9: the shared CLI launched a fresh headless Chromium, used the real Qwen endpoint, read `example.com`, returned its exact heading `Example Domain`, and exited successfully. Agent run time was 3.55 seconds, excluding inference warm-up. Target was blocked by a CAPTCHA in the earlier local run. The Amazon checkout flow has not been independently verified for this branch. Successful startup or small smokes do not establish shopping success or benchmark speed.
