"""Run the Qwen Browser Use demo behind a lightweight chat harness."""

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
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

HERE = Path(__file__).resolve().parent
AMAZON_UI_PATH = HERE / 'qwen38_grokbot.html'
MARKETPLACE_UI_PATH = HERE / 'qwen38_marketplace.html'
AGENT_PATH = HERE / 'qwen38_modal.py'
REPO_ROOT = HERE.parents[1]
ANSI_RE = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
NUMBER_WORDS = {
	'one': 1,
	'two': 2,
	'three': 3,
	'four': 4,
	'five': 5,
	'six': 6,
	'seven': 7,
	'eight': 8,
	'nine': 9,
	'ten': 10,
}

load_dotenv(REPO_ROOT / '.env')
DEMO_MODE = os.getenv('QWEN38_DEMO_MODE', 'amazon').strip().lower()
if DEMO_MODE == 'amazon':
	load_dotenv(REPO_ROOT / '.checkout.env')
elif DEMO_MODE == 'marketplace':
	load_dotenv(REPO_ROOT / '.marketplace.env')


class RunRequest(BaseModel):
	"""Validated task submitted by the local chat composer."""

	prompt: str = Field(min_length=3, max_length=8_000)


class RunAction(BaseModel):
	"""Validated control action for an active task."""

	action: Literal['stop', 'close_browser']


class MarketplaceVisionConfig(BaseModel):
	"""Validated local-only target for a visual Marketplace search."""

	target: str = Field(min_length=1, max_length=200)
	visual_criterion: str = Field(min_length=1, max_length=500)
	destination: str = Field(min_length=1, max_length=200)
	max_results: int = Field(default=2, ge=1, le=50)

	@classmethod
	def from_environment(cls) -> MarketplaceVisionConfig:
		"""Load optional local overrides for the committed Marketplace demo brief."""
		values = {
			'target': os.getenv('QWEN38_MARKETPLACE_TARGET', 'geese statues').strip(),
			'visual_criterion': os.getenv(
				'QWEN38_MARKETPLACE_VISUAL_CRITERION',
				'the goose statue has a clearly visible open beak with a gap between the upper and lower beak',
			).strip(),
			'destination': os.getenv('QWEN38_MARKETPLACE_DESTINATION', 'Sunnyvale, CA 94085').strip(),
			'max_results': os.getenv('QWEN38_MARKETPLACE_MAX_RESULTS', '2').strip(),
		}
		return cls.model_validate(values)


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


def shopping_brief(prompt: str) -> dict[str, list[dict[str, str]]]:
	"""Extract only constraints actually present in the submitted request."""
	lower = prompt.lower()
	recipients_match = re.search(r'(\d+)\s+(?:girls|boys|kids|children|guests)', lower)
	favor_match = re.search(r'(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+(?:party\s+)?favors?\s+each', lower)
	price_match = re.search(r'(?:less than|under|max(?:imum)?(?: of)?)\s*\$\s*(\d+(?:\.\d+)?)', lower)
	recipients = int(recipients_match.group(1)) if recipients_match else None
	favors_each = (
		int(favor_match.group(1))
		if favor_match and favor_match.group(1).isdigit()
		else NUMBER_WORDS.get(favor_match.group(1))
		if favor_match
		else None
	)
	unit_cap = float(price_match.group(1)) if price_match else None
	metrics: list[dict[str, str]] = []
	if recipients is not None:
		metrics.append({'value': str(recipients), 'label': 'recipients'})
	if favors_each is not None:
		metrics.append({'value': str(favors_each), 'label': 'items per recipient'})
	if unit_cap is not None:
		metrics.append({'value': f'<${unit_cap:.2f}', 'label': 'per-item cap'})
	if recipients is not None and favors_each is not None:
		total_units = recipients * favors_each
		metrics.append({'value': str(total_units), 'label': 'items required'})
		if unit_cap is not None:
			metrics.append({'value': f'≤${total_units * unit_cap:.2f}', 'label': 'merchandise ceiling'})
	return {'metrics': metrics}


def optimized_task(prompt: str) -> str:
	"""Turn a casual party-shopping request into a verifiable agent brief."""
	if DEMO_MODE == 'marketplace':
		return optimized_marketplace_task(prompt)
	brief = shopping_brief(prompt)
	metrics = brief['metrics']
	constraint_lines = (
		'\n'.join(f'- {metric["label"]}: {metric["value"]}' for metric in metrics)
		or '- No numeric constraints detected; infer nothing and verify ambiguous requirements with the user.'
	)
	age_safety = (
		'- The recipients are under five: prefer non-toxic, age-appropriate products without magnets, sharp parts, projectiles, or small detachable choking hazards.\n'
		if re.search(r'under\s+(?:five|5)', prompt, re.I)
		else ''
	)
	checkout_address = ' '.join(os.getenv('QWEN38_CHECKOUT_ADDRESS', '').splitlines()).strip()
	checkout_item_count = os.getenv('QWEN38_CHECKOUT_ITEM_COUNT', '').strip()
	use_saved_card = os.getenv('QWEN38_CHECKOUT_USE_SAVED_CARD', '').strip().lower() in {'1', 'true', 'yes', 'on'}
	place_order = os.getenv('QWEN38_CHECKOUT_PLACE_ORDER', '').strip().lower() in {'1', 'true', 'yes', 'on'}
	checkout_override = bool(checkout_address or checkout_item_count or use_saved_card or place_order)
	if checkout_override:
		final_checkout_step = (
			f'10. On final review, re-verify exactly {checkout_item_count or "the requested number of"} requested '
			'line items, quantities, shipping address, saved payment selection, delivery details, and displayed order '
			'total. If they all match, click the final Place your order button exactly once. Verify and report the '
			'resulting Amazon order confirmation; do not buy anything else.'
			if place_order
			else '10. Stop on the final order-review page before any button that submits or places the order.'
		)
		checkout_steps = f"""6. Once exactly {checkout_item_count or 'the requested number of'} requested cart line items are verified, proceed directly to checkout.
7. Stop for the user at any login, CAPTCHA, passkey, OTP, or other authentication challenge.
8. Enter or select this user-authorized shipping address exactly: {checkout_address or '[no address supplied]'}.
9. {'Select the existing card on file without revealing, copying, or changing its details.' if use_saved_card else 'Stop before selecting or entering payment.'}
{final_checkout_step}"""
		safety_boundary = (
			'The shipping address, saved-card selection, and one final Amazon order submission above are explicitly '
			'authorized for this local demo. Never enter credentials, solve a CAPTCHA, add a new payment method, reveal '
			'card details, submit with a cart mismatch, or place more than one order.'
			if place_order
			else 'The shipping address and saved-card selection above are explicitly authorized for this local demo. '
			'Never enter credentials, solve a CAPTCHA, add a new payment method, reveal card details, click Place your '
			'order, or complete a purchase.'
		)
	else:
		checkout_steps = (
			'6. Proceed toward checkout only after verification, then stop immediately at sign-in, CAPTCHA, address, '
			'payment, or final order review.'
		)
		safety_boundary = (
			'Never enter credentials, delivery addresses, payment details, or solve a CAPTCHA. Never click Place your '
			'order or complete a purchase.'
		)
	return f"""USER STORY
{prompt.strip()}

EXPLICITLY PARSED CONSTRAINTS
{constraint_lines}
{age_safety}
Interpret the request literally. Do not invent quantities, budgets, recipients, product categories, or preferences that the user did not state. For multipacks, distinguish listing price from per-item price and show the arithmetic.

SHOPPING WORKFLOW
1. Open Amazon and search for clearly matching, available products.
2. Compare pack count, age guidance, price, delivery availability, and per-item cost before choosing.
3. Add the correct one-time-purchase quantity for every requested item or category. Do not use subscriptions, pickup, or Buy Now.
4. After every add-to-cart action, verify the resulting page confirms the addition.
5. Open the cart and verify all requested items, quantities, pack math, and applicable per-item prices. Preserve unrelated cart items.
{checkout_steps}

SAFETY BOUNDARY
{safety_boundary} Report selected products, pack math, prices, cart verification, and checkout state."""


def optimized_marketplace_task(prompt: str) -> str:
	"""Turn a Marketplace request into a screenshot-verified listing search."""
	config = MarketplaceVisionConfig.from_environment()
	target = config.target
	visual_criterion = config.visual_criterion
	destination = config.destination
	max_results = config.max_results
	return f"""USER REQUEST
{prompt.strip()}

VISUAL SEARCH TARGET
Search specifically for {target} offered in the United States and available for shipping to {destination}. A listing qualifies only when its photos visibly satisfy this criterion: {visual_criterion}.

MARKETPLACE SEARCH WORKFLOW
1. Use only Facebook Marketplace. If login, CAPTCHA, passkey, OTP, or another authentication checkpoint appears, stop and ask the user to complete it manually.
2. Search Marketplace listings available in the United States. Use the widest US radius and shipping coverage the interface permits. Open each plausible listing and confirm from visible listing details that shipping or delivery to {destination} is available. Exclude pickup-only listings and listings whose shipping eligibility remains unclear. Never enter a street address or change the account's saved location.
3. Search useful singular, plural, and common-title variants for the requested product. Keep a durable working record of every fully verified match as soon as it qualifies. Stop searching immediately when {max_results} unique listings have both clear visual proof and confirmed shipping to {destination}; do not keep scrolling, inspect additional candidates, or attempt an exhaustive search after reaching that target.
4. Use screenshot vision for every plausible candidate. Open the listing and click through every available product photo before accepting or rejecting it; never reject a listing from its main photo alone when more photos are available. After each gallery click, use the next screenshot, active thumbnail, or photo counter to verify that a different photo actually appeared before inspecting it. Prefer explicit thumbnails when present; otherwise use the visible Next-photo control.
5. If the Next-photo control does not change the image, do not repeat the same click. Try one alternate visible thumbnail or one ArrowRight keypress and verify the resulting image. If neither route advances the gallery, record the control as blocked, preserve all earlier verified matches, abandon this candidate, and continue from the results. Stop after returning to the first photo or after every distinct available photo has been inspected; do not cycle indefinitely.
6. Include a listing only when at least one clear product photo shows the requested visual feature. For an open beak, require a visible gap between the upper and lower beak. Reject closed beaks, unclear thumbnails, occluded or out-of-frame beaks, illustrations when a statue is requested, and ambiguous side angles only after completing the available-photo review.
7. Record the listing title, price, location, exact visual evidence including which photo showed it, visible shipping evidence for {destination}, and canonical Marketplace URL. Never infer a visual feature or shipping eligibility that is not clearly shown.
8. Deduplicate primarily by listing URL, then by matching photos, title, price, and location. Rank the verified set by open-beak visual confidence first, confirmed shipping confidence second, then listing completeness, seller rating when visible, condition, and value. Return exactly {max_results} matches when that many qualify. If Facebook blocks progress or an individual listing control fails before the target is reached, abandon that candidate and return every match already verified rather than discarding partial success. Never claim nationwide exhaustiveness when Facebook limits visible results.

STRICT READ-ONLY BOUNDARY
Do not message sellers, click Contact or Make Offer, save listings, reveal contact information, change the account, add anything to a cart, begin checkout, or make a purchase. A Facebook location-verification modal is an immediate blocker for outreach but does not prevent completing this read-only visual search. Close or leave that modal without retrying seller contact, then continue research.

FINAL RESPONSE FORMAT
Return one tab-separated line per visually verified and shipping-confirmed listing using the MATCH marker followed by exactly these six fields:
MATCH<TAB>title<TAB>price<TAB>location<TAB>specific visual evidence that the beak is open, including the photo position<TAB>shipping evidence for {destination}<TAB>listing URL
Do not put tab characters inside a field. After the matches, return COVERAGE<TAB>followed by regions, filters, and title variants searched; LIMITATIONS<TAB>followed by any Facebook visibility limits; and EXCLUDED<TAB>followed by a concise summary of closed-beak, unclear, or unrelated candidates rejected. If no qualifying listings are visible, return no MATCH lines and explain why in LIMITATIONS. The frontend will supply the results heading."""


def snapshot_payload(state: RunState) -> dict[str, object]:
	"""Serialize run state without exposing environment secrets."""
	snapshot: dict[str, object] | None = None
	if state.event_path and state.event_path.is_file():
		try:
			loaded_snapshot = json.loads(state.event_path.read_text(encoding='utf-8'))
			snapshot = loaded_snapshot if isinstance(loaded_snapshot, dict) else None
			# The real browser is visible in its own window; only send progress text
			# to the chat UI rather than transferring screenshot bytes on every poll.
			if snapshot is not None:
				snapshot.pop('screenshot', None)
		except (OSError, json.JSONDecodeError):
			snapshot = None
	return {
		'id': state.run_id,
		'status': state.status,
		'step': state.step,
		'elapsed': state.total_seconds or time.time() - state.started_at,
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
	browser_arguments = ['--chromium'] if environment.get('QWEN38_UI_BROWSER') == 'playwright' else []
	state.process = await asyncio.create_subprocess_exec(
		sys.executable,
		str(AGENT_PATH),
		*browser_arguments,
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
	ui_path = MARKETPLACE_UI_PATH if DEMO_MODE == 'marketplace' else AMAZON_UI_PATH
	return FileResponse(ui_path)


async def create_run(request: Request) -> JSONResponse:
	global ACTIVE_RUN_ID
	if os.getenv('CODEX_SANDBOX'):
		return JSONResponse(
			{
				'error': (
					'This server is running inside the managed Codex sandbox, which macOS prevents from launching Chrome. '
					'Start the same command in a normal Terminal, then reload this page.'
				)
			},
			status_code=409,
		)
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
	parser = argparse.ArgumentParser(description='Run the Qwen browser-agent chat harness.')
	parser.add_argument('--host', default='127.0.0.1')
	parser.add_argument('--port', type=int, default=8765)
	arguments = parser.parse_args()
	print(f'\nScout is ready at http://{arguments.host}:{arguments.port}', flush=True)
	print('Keep this terminal open while using the UI. Press Ctrl+C to stop.\n', flush=True)
	uvicorn.run(APP, host=arguments.host, port=arguments.port, log_level='warning')
