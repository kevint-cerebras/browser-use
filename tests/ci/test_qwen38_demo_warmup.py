"""Cold inference must not consume browser-agent steps or hide auth failures."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from examples.models.qwen38_modal import completion_extra_body, wait_for_inference


@pytest.mark.parametrize('base_url', ['https://api.cerebras.ai/v1', 'HTTPS://API.CEREBRAS.AI:443/v1/'])
def test_cerebras_cloud_omits_sglang_sampling_parameters(base_url: str) -> None:
	body = completion_extra_body(base_url=base_url, effort='none', repetition_penalty=1.08)
	assert body == {
		'reasoning_effort': 'none',
		'response_format': {'type': 'json_object'},
	}


def test_modal_endpoint_keeps_sglang_sampling_parameters() -> None:
	body = completion_extra_body(base_url='https://demo.modal.run/v1', effort='none', repetition_penalty=1.08)
	assert body['top_k'] == 20
	assert body['repetition_penalty'] == 1.08

	lookalike_body = completion_extra_body(base_url='https://api.cerebras.ai.example/v1', effort='none', repetition_penalty=1.08)
	assert lookalike_body['top_k'] == 20
	assert lookalike_body['repetition_penalty'] == 1.08


@pytest.mark.asyncio
async def test_warmup_recovers_after_cold_503() -> None:
	calls = 0

	def respond(request: httpx.Request) -> httpx.Response:
		nonlocal calls
		calls += 1
		assert request.headers['Authorization'] == 'Bearer test-key'
		if calls == 1:
			return httpx.Response(503)
		return httpx.Response(200, json={'choices': [{'message': {'content': 'OK'}}]})

	async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
		with patch('examples.models.qwen38_modal.asyncio.sleep', new=AsyncMock()):
			await wait_for_inference(client, 'https://inference.test/v1/', 'test-key', 'test-model')
	assert calls == 2


@pytest.mark.asyncio
async def test_warmup_rejects_bad_credentials_immediately() -> None:
	async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(401))) as client:
		with pytest.raises(httpx.HTTPStatusError):
			await wait_for_inference(client, 'https://inference.test/v1', 'test-key', 'test-model')


@pytest.mark.asyncio
async def test_warmup_rejects_empty_success_response() -> None:
	async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={'choices': []}))) as client:
		with pytest.raises(ValueError, match='no completion'):
			await wait_for_inference(client, 'https://inference.test/v1', 'test-key', 'test-model')


@pytest.mark.asyncio
async def test_warmup_has_a_deadline() -> None:
	clock = 0.0

	async def advance_clock(seconds: float) -> None:
		nonlocal clock
		clock += seconds

	async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(503))) as client:
		with (
			patch('examples.models.qwen38_modal.time', new=SimpleNamespace(monotonic=lambda: clock)),
			patch('examples.models.qwen38_modal.asyncio.sleep', side_effect=advance_clock),
		):
			with pytest.raises(TimeoutError, match='five minutes'):
				await wait_for_inference(client, 'https://inference.test/v1', 'test-key', 'test-model')
	assert clock == 300
