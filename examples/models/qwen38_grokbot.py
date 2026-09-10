"""Run the Qwen Browser Use demo behind a lightweight split-pane web harness."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pty
import re
import signal
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from uuid import uuid4

import uvicorn
from pydantic import BaseModel, Field
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

HERE = Path(__file__).resolve().parent
UI_PATH = HERE / 'qwen38_grokbot.html'
AGENT_PATH = HERE / 'qwen38_modal.py'
ANSI_RE = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')


class RunRequest(BaseModel):
	"""Validated task submitted by the local chat composer."""

	prompt: str = Field(min_length=3, max_length=8_000)


class RunAction(BaseModel):
	"""Validated control action for an active task."""

	action: Literal['stop', 'close_browser']


@dataclass
class RunState:
	"""Mutable state for one locally running Browser Use task."""

	run_id: str
	prompt: str
	status: str = 'starting'
	started_at: float = field(default_factory=time.time)
	finished_at: float | None = None
	process: asyncio.subprocess.Process | None = None
	pty_master: int | None = None
	log: str = ''
	step: int = 0
	total_seconds: float | None = None
	final_result: str = ''
	event_path: Path | None = None


RUNS: dict[str, RunState] = {}
ACTIVE_RUN_ID: str | None = None


def shopping_brief(prompt: str) -> dict[str, object]:
	"""Extract the demo's core constraints for an answer-first story card."""
	lower = prompt.lower()
	recipients_match = re.search(r'(\d+)\s+(?:girls|boys|kids|children|guests)', lower)
	favor_match = re.search(r'(\d+)\s+(?:party\s+)?favors?\s+each', lower)
	price_match = re.search(r'(?:less than|under|max(?:imum)?(?: of)?)\s*\$\s*(\d+(?:\.\d+)?)', lower)
	recipients = int(recipients_match.group(1)) if recipients_match else 12
	favors_each = int(favor_match.group(1)) if favor_match else 3
	unit_cap = float(price_match.group(1)) if price_match else 5.0
	total_units = recipients * favors_each
	return {
		'recipients': recipients,
		'favors_each': favors_each,
		'unit_cap': unit_cap,
		'total_units': total_units,
		'max_merchandise': total_units * unit_cap,
	}


def optimized_task(prompt: str) -> str:
	"""Turn a casual party-shopping request into a verifiable agent brief."""
	brief = shopping_brief(prompt)
	return f"""USER STORY
{prompt.strip()}

INTERPRETATION FOR THIS DEMO
- Shop for {brief['recipients']} children.
- Choose {brief['favors_each']} distinct, age-appropriate party-favor types.
- Obtain at least {brief['recipients']} individual favors of each type ({brief['total_units']} favors total).
- Every individual favor must cost less than ${brief['unit_cap']:.2f}. A multipack may cost more only when its per-item price stays below that cap.
- Prefer non-food, non-toxic, non-choking-hazard options appropriate for children under five. Avoid sharp, magnetic, projectile, makeup, and small detachable items.

SHOPPING WORKFLOW
1. Open Amazon and search for clearly matching, available products.
2. Compare pack count, age guidance, price, delivery availability, and per-item cost before choosing.
3. Add enough one-time-purchase quantity for each favor type. Do not use subscriptions, pickup, or Buy Now.
4. After every add-to-cart action, verify the resulting page confirms the addition.
5. Open the cart and verify all favor types, quantities, pack math, and per-item price. Preserve unrelated cart items.
6. Proceed toward checkout only after verification, then stop immediately at sign-in, CAPTCHA, address, payment, or final order review.

SAFETY BOUNDARY
Never enter credentials, delivery addresses, payment details, or solve a CAPTCHA. Never click Place your order or complete a purchase. Report selected products, pack math, prices, cart verification, and checkout state."""


def snapshot_payload(state: RunState) -> dict[str, object]:
	"""Serialize run state without exposing environment secrets."""
	snapshot: dict[str, object] | None = None
	if state.event_path and state.event_path.is_file():
		try:
			snapshot = json.loads(state.event_path.read_text(encoding='utf-8'))
		except (OSError, json.JSONDecodeError):
			snapshot = None
	return {
		'id': state.run_id,
		'status': state.status,
		'step': state.step,
		'elapsed': state.total_seconds or time.time() - state.started_at,
		'log': state.log[-80_000:],
		'final_result': state.final_result,
		'brief': shopping_brief(state.prompt),
		'snapshot': snapshot,
	}


async def read_process(state: RunState) -> None:
	"""Read the agent pseudo-terminal and derive live UI status."""
	assert state.pty_master is not None
	while True:
		try:
			data = await asyncio.to_thread(os.read, state.pty_master, 4096)
		except OSError:
			break
		if not data:
			break
		text = ANSI_RE.sub('', data.decode('utf-8', errors='replace').replace('\r', ''))
		state.log += text
		step_matches = re.findall(r'STEP (\d+) TOTAL=', state.log)
		if step_matches:
			state.step = max(map(int, step_matches))
			state.status = 'running'
		total_match = re.search(r'RUN TOTAL=([\d.]+)s', state.log)
		if total_match:
			state.total_seconds = float(total_match.group(1))
		result_match = re.search(r'Final result:\s*(.*?)(?:\nBrowser left open|\Z)', state.log, re.S)
		if result_match:
			state.final_result = result_match.group(1).strip()
		if 'Browser left open. Press Enter to close it' in state.log:
			state.status = 'waiting'
	if state.process:
		return_code = await state.process.wait()
		if state.status not in {'stopped', 'closed'}:
			state.status = 'completed' if return_code == 0 else 'failed'
		state.finished_at = time.time()


async def launch_agent(state: RunState) -> None:
	"""Launch the visible agent in a PTY so its final browser stays open."""
	master, slave = pty.openpty()
	state.pty_master = master
	run_dir = Path(tempfile.gettempdir()) / 'qwen38-grokbot' / state.run_id
	state.event_path = run_dir / 'latest.json'
	environment = os.environ.copy()
	environment.pop('BROWSER_USE_HEADLESS', None)
	environment['QWEN38_UI_EVENT_PATH'] = str(state.event_path)
	state.process = await asyncio.create_subprocess_exec(
		sys.executable,
		str(AGENT_PATH),
		'--chromium',
		'--keep-open',
		'--task',
		optimized_task(state.prompt),
		stdin=slave,
		stdout=slave,
		stderr=slave,
		env=environment,
		start_new_session=True,
	)
	os.close(slave)
	await read_process(state)


async def home(_request: Request) -> FileResponse:
	return FileResponse(UI_PATH)


async def create_run(request: Request) -> JSONResponse:
	global ACTIVE_RUN_ID
	active = RUNS.get(ACTIVE_RUN_ID or '')
	if active and active.status in {'starting', 'running', 'waiting'}:
		return JSONResponse({'error': 'Finish or stop the active run first.'}, status_code=409)
	submitted = RunRequest.model_validate(await request.json())
	state = RunState(run_id=uuid4().hex, prompt=submitted.prompt)
	RUNS[state.run_id] = state
	ACTIVE_RUN_ID = state.run_id
	asyncio.create_task(launch_agent(state))
	return JSONResponse(snapshot_payload(state), status_code=201)


async def get_run(request: Request) -> JSONResponse:
	state = RUNS.get(request.path_params['run_id'])
	return JSONResponse(snapshot_payload(state)) if state else JSONResponse({'error': 'Run not found.'}, status_code=404)


async def control_run(request: Request) -> JSONResponse:
	state = RUNS.get(request.path_params['run_id'])
	if not state:
		return JSONResponse({'error': 'Run not found.'}, status_code=404)
	command = RunAction.model_validate(await request.json())
	if command.action == 'close_browser' and state.pty_master is not None:
		os.write(state.pty_master, b'\n')
		state.status = 'closed'
	elif state.process and state.process.returncode is None:
		os.killpg(state.process.pid, signal.SIGTERM)
		state.status = 'stopped'
	return JSONResponse(snapshot_payload(state))


APP = Starlette(
	debug=False,
	routes=[
		Route('/', home),
		Route('/api/runs', create_run, methods=['POST']),
		Route('/api/runs/{run_id:str}', get_run),
		Route('/api/runs/{run_id:str}/control', control_run, methods=['POST']),
	],
)


if __name__ == '__main__':
	parser = argparse.ArgumentParser(description='Run the split-pane Qwen shopping harness.')
	parser.add_argument('--host', default='127.0.0.1')
	parser.add_argument('--port', type=int, default=8765)
	arguments = parser.parse_args()
	uvicorn.run(APP, host=arguments.host, port=arguments.port, log_level='warning')
