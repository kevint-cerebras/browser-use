"""Launch the Marketplace research demo as a 25/75 two-window workspace on macOS."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from pydantic import BaseModel, Field

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
HARNESS_PATH = HERE / 'qwen38_grokbot.py'
DEFAULT_CHROME_PATH = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
MARKETPLACE_URL = 'https://www.facebook.com/marketplace/'


class ScreenBounds(BaseModel):
	"""Validated bounds for the display containing the demo windows."""

	left: int
	top: int
	right: int
	bottom: int

	@property
	def width(self) -> int:
		return self.right - self.left

	@property
	def height(self) -> int:
		return self.bottom - self.top


class WindowLayout(BaseModel):
	"""Validated position and size for one Chrome window."""

	x: int
	y: int
	width: int = Field(ge=320)
	height: int = Field(ge=480)


def read_main_screen_bounds() -> ScreenBounds:
	"""Read the macOS desktop bounds without hardcoding a resolution."""
	result = subprocess.run(
		['osascript', '-e', 'tell application "Finder" to get bounds of window of desktop'],
		check=True,
		capture_output=True,
		text=True,
	)
	values = [int(value) for value in re.findall(r'-?\d+', result.stdout)]
	if len(values) != 4:
		raise RuntimeError(f'Could not parse display bounds from macOS: {result.stdout.strip()!r}')
	return ScreenBounds(left=values[0], top=values[1], right=values[2], bottom=values[3])


def split_screen(bounds: ScreenBounds) -> tuple[WindowLayout, WindowLayout]:
	"""Allocate 25 percent to the prompt and 75 percent to Marketplace."""
	prompt_width = max(320, round(bounds.width * 0.25))
	browser_width = bounds.width - prompt_width
	prompt_layout = WindowLayout(x=bounds.left, y=bounds.top, width=prompt_width, height=bounds.height)
	browser_layout = WindowLayout(
		x=bounds.left + prompt_width,
		y=bounds.top,
		width=browser_width,
		height=bounds.height,
	)
	return prompt_layout, browser_layout


def wait_for_url(url: str, *, timeout: float) -> bool:
	"""Wait until a local HTTP endpoint responds or the deadline expires."""
	deadline = time.monotonic() + timeout
	while time.monotonic() < deadline:
		try:
			with urllib.request.urlopen(url, timeout=0.5) as response:
				if response.status < 500:
					return True
		except (OSError, urllib.error.URLError):
			pass
		time.sleep(0.1)
	return False


def chrome_window_command(
	chrome_path: Path,
	*,
	profile_path: Path,
	layout: WindowLayout,
	url: str,
	remote_debugging_port: int | None = None,
	app_window: bool = False,
) -> list[str]:
	"""Build a Chrome command for one isolated persistent profile."""
	command = [
		str(chrome_path),
		'--no-first-run',
		'--no-default-browser-check',
		'--disable-session-crashed-bubble',
		f'--user-data-dir={profile_path}',
		f'--window-position={layout.x},{layout.y}',
		f'--window-size={layout.width},{layout.height}',
	]
	if remote_debugging_port is not None:
		command.extend(
			[
				'--remote-debugging-address=127.0.0.1',
				f'--remote-debugging-port={remote_debugging_port}',
			]
		)
	command.append(f'--app={url}' if app_window else url)
	return command


def launch_demo(*, chrome_path: Path, harness_port: int, cdp_port: int) -> int:
	"""Open Marketplace first, then the prompt UI, and supervise the local server."""
	if not chrome_path.is_file():
		raise FileNotFoundError(f'Chrome executable not found: {chrome_path}')
	bounds = read_main_screen_bounds()
	prompt_layout, browser_layout = split_screen(bounds)
	marketplace_profile = REPO_ROOT / '.browser-use-marketplace-profile'
	ui_profile = REPO_ROOT / '.scout-marketplace-ui-profile'
	marketplace_profile.mkdir(mode=0o700, exist_ok=True)
	ui_profile.mkdir(mode=0o700, exist_ok=True)
	cdp_url = f'http://127.0.0.1:{cdp_port}'
	if not wait_for_url(f'{cdp_url}/json/version', timeout=0.2):
		subprocess.Popen(
			chrome_window_command(
				chrome_path,
				profile_path=marketplace_profile,
				layout=browser_layout,
				url=MARKETPLACE_URL,
				remote_debugging_port=cdp_port,
			),
			stdout=subprocess.DEVNULL,
			stderr=subprocess.DEVNULL,
			start_new_session=True,
		)
	if not wait_for_url(f'{cdp_url}/json/version', timeout=15):
		raise RuntimeError('Marketplace Chrome did not expose its local debugging endpoint within 15 seconds.')
	try:
		with urllib.request.urlopen(f'{cdp_url}/json/list', timeout=1) as response:
			targets = response.read().decode('utf-8')
		if 'facebook.com/marketplace' not in targets:
			encoded_url = urllib.parse.quote(MARKETPLACE_URL, safe='')
			request = urllib.request.Request(f'{cdp_url}/json/new?{encoded_url}', method='PUT')
			with urllib.request.urlopen(request, timeout=2):
				pass
	except (OSError, urllib.error.URLError):
		pass

	environment = os.environ.copy()
	environment.pop('BROWSER_USE_HEADLESS', None)
	environment.pop('QWEN38_UI_BROWSER', None)
	for name in (
		'QWEN38_CHECKOUT_ADDRESS',
		'QWEN38_CHECKOUT_ITEM_COUNT',
		'QWEN38_CHECKOUT_USE_SAVED_CARD',
		'QWEN38_CHECKOUT_PLACE_ORDER',
	):
		environment.pop(name, None)
	environment['BROWSER_USE_CDP_URL'] = cdp_url
	environment['QWEN38_DEMO_MODE'] = 'marketplace'
	server = subprocess.Popen(
		[sys.executable, str(HARNESS_PATH), '--host', '127.0.0.1', '--port', str(harness_port)],
		cwd=REPO_ROOT,
		env=environment,
	)
	harness_url = f'http://127.0.0.1:{harness_port}'
	try:
		if not wait_for_url(harness_url, timeout=15):
			raise RuntimeError('Marketplace prompt server did not become ready within 15 seconds.')
		subprocess.Popen(
			chrome_window_command(
				chrome_path,
				profile_path=ui_profile,
				layout=prompt_layout,
				url=harness_url,
				app_window=True,
			),
			stdout=subprocess.DEVNULL,
			stderr=subprocess.DEVNULL,
			start_new_session=True,
		)
		print('\nMarketplace is open on the right. Sign in there before submitting a prompt.', flush=True)
		print(f'Scout is open on the left at {harness_url}. Press Ctrl+C here to stop the server.\n', flush=True)
		return server.wait()
	except KeyboardInterrupt:
		return 0
	finally:
		if server.poll() is None:
			server.terminate()
			try:
				server.wait(timeout=5)
			except subprocess.TimeoutExpired:
				server.kill()


if __name__ == '__main__':
	parser = argparse.ArgumentParser(description='Launch the 25/75 Facebook Marketplace research demo.')
	parser.add_argument('--chrome-path', type=Path, default=DEFAULT_CHROME_PATH)
	parser.add_argument('--harness-port', type=int, default=8766)
	parser.add_argument('--cdp-port', type=int, default=9223)
	arguments = parser.parse_args()
	raise SystemExit(
		launch_demo(
			chrome_path=arguments.chrome_path.expanduser().resolve(),
			harness_port=arguments.harness_port,
			cdp_port=arguments.cdp_port,
		)
	)
