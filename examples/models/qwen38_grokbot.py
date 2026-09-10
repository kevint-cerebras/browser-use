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


class MarketplaceActionConfig(BaseModel):
	"""Validated local-only authorization for bounded Marketplace outreach."""

	target: str = Field(min_length=1, max_length=200)
	destination: str = Field(min_length=1, max_length=200)
	top_count: int = Field(ge=1, le=10)
	offer_discount: float = Field(gt=0, le=1_000)
	make_offers: bool = False
	send_messages: bool = False

	@classmethod
	def from_environment(cls) -> MarketplaceActionConfig | None:
		"""Load an ignored Marketplace override when all required values exist."""
		values = {
			'target': os.getenv('QWEN38_MARKETPLACE_TARGET', '').strip(),
			'destination': os.getenv('QWEN38_MARKETPLACE_DESTINATION', '').strip(),
			'top_count': os.getenv('QWEN38_MARKETPLACE_TOP_COUNT', '').strip(),
			'offer_discount': os.getenv('QWEN38_MARKETPLACE_OFFER_DISCOUNT', '').strip(),
			'make_offers': os.getenv('QWEN38_MARKETPLACE_MAKE_OFFERS', '').strip(),
			'send_messages': os.getenv('QWEN38_MARKETPLACE_SEND_MESSAGES', '').strip(),
		}
		if not all(values[key] for key in ('target', 'destination', 'top_count', 'offer_discount')):
			return None
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
	"""Turn a Marketplace request into a systematic US listing search and report."""
	config = MarketplaceActionConfig.from_environment()
	if config and (config.make_offers or config.send_messages):
		offer_action = f"""BOUNDED OUTREACH AUTHORIZATION
This ignored local demo configuration explicitly authorizes outreach for the top {config.top_count} qualifying listings for {config.target} that ship to {config.destination}.
- Rank qualifying listings by literal product match, confirmed shipping eligibility, condition, seller rating, and value.
- For each of the top {config.top_count}, calculate an offer exactly ${config.offer_discount:.2f} below the currently displayed listing price. Show the arithmetic before acting. Never offer zero or a negative amount.
- {'Use Facebook’s formal Make Offer control once per selected listing when it is available.' if config.make_offers else 'Do not use a formal Make Offer control.'}
- {f'Send exactly one concise message per selected seller: “Hi, would you accept $OFFER for this item? I’m in {config.destination} and would need shipping. Thank you.” Replace $OFFER with the calculated amount.' if config.send_messages else 'Do not message sellers.'}
- If no formal Offer control exists, the message may carry the proposal, but report that no formal offer was submitted.
- Before each submission, verify the listing URL, displayed price, calculated offer, shipping availability, and that this seller has not already been contacted. Never contact more than {config.top_count} sellers.
- Stop and report instead of acting if the price changed, shipping is unavailable or unclear, the item is not a literal match, the seller was already contacted, or any submission state is ambiguous."""
	else:
		offer_action = """READ-ONLY MODE
Do not message sellers, make offers, save listings, reveal contact information, change the account, add anything to a cart, begin checkout, or make a purchase."""
	return f"""USER REQUEST
{prompt.strip()}

LOCAL DEMO TARGET
{f'Search specifically for {config.target} offered in the United States that can ship to {config.destination}.' if config else 'Interpret the requested product and destination literally; do not invent either.'}

MARKETPLACE SEARCH WORKFLOW
1. Use only Facebook Marketplace. If login, CAPTCHA, passkey, OTP, or another authentication checkpoint appears, stop and ask the user to complete it manually.
2. Search Marketplace listings available in the United States. Use the widest US radius and shipping/delivery coverage the interface permits. If Marketplace remains location-limited, sample multiple major US regions and state exactly which regions were covered.
3. Search useful singular, plural, and common-title variants for the requested product. Scroll or paginate until no new qualifying results appear, the site imposes a limit, or the agent step budget is near exhaustion.
4. Open plausible candidates and verify that each is an actual matching item currently offered for sale and can ship to the configured destination. Exclude unrelated products, parts, wanted ads, rentals, stock photos without an actual item, and pickup-only listings.
5. Record title, price, condition, location, seller name, seller rating and rating count, delivery/shipping availability, listing URL, and relevant included details. Never infer a missing value; use “Not shown” when Facebook does not expose seller information.
6. Deduplicate primarily by listing URL, then by matching title, price, location, and seller-visible details. Keep distinct listings from the same seller when they are clearly separate units.
7. Apply the bounded outreach instructions below, then return the selected options, excluded near-matches, search variants, regions/filters covered, and explicit limitations. Never claim nationwide exhaustiveness when Facebook limits visible results.

{offer_action}

SAFETY BOUNDARY
Never disclose a street address, email, phone number, credentials, or payment information to a seller. Never purchase, pay, create more than the authorized offers/messages, or navigate away from Facebook Marketplace except for an unavoidable Facebook login checkpoint.

FINAL RESPONSE FORMAT
Return one tab-separated line per selected listing using the OPTION marker followed by exactly these ten fields:
OPTION<TAB>title<TAB>price<TAB>location<TAB>condition<TAB>seller name<TAB>seller rating and rating count<TAB>delivery or shipping<TAB>listing URL<TAB>offer amount and submission status<TAB>message status
Do not put tab characters inside a field. After the options, return COVERAGE<TAB>followed by regions, filters, and title variants searched; LIMITATIONS<TAB>followed by any Facebook visibility limits; and EXCLUDED<TAB>followed by a concise summary of near-matches rejected. If no qualifying listings are visible, return no OPTION lines and explain why in LIMITATIONS. The frontend will supply the “Here are your options” heading."""


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
