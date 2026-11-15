from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from deskbot_server.client.llm.runtime import ResolvedLlmConfig
from deskbot_server.client.llm.streaming import stream_tool_completion


def _cfg(protocol="openai"):
    return ResolvedLlmConfig("test", "test-key", "http://model/v1", protocol, "test", "test")


def _event(event):
    return ("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode()


class _Stream(httpx.AsyncByteStream):
    def __init__(self, events, *, pause=None, closed=None):
        self.events, self.pause, self.closed = events, pause, closed

    async def __aiter__(self):
        for i, event in enumerate(self.events):
            yield _event(event)
            if i == 0 and self.pause is not None:
                await self.pause.wait()

    async def aclose(self):
        if self.closed is not None:
            self.closed.set()


def test_text_arrives_before_completion_and_native_tools_are_assembled():
    async def run():
        release, delta_seen = asyncio.Event(), asyncio.Event()
        events = [
            {"choices": [{"delta": {"content": "让我算一下。"}}]},
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "id": "c1", "function": {"name": "calculate", "arguments": '{"expr":'}}
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"2+2"}'}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]

        def handler(request):
            assert json.loads(request.content)["stream"] is True
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=_Stream(events, pause=release)
            )

        async def delta(text):
            assert text == "让我算一下。"
            delta_seen.set()

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            task = asyncio.create_task(
                stream_tool_completion(
                    client, [], _cfg(), tools=[], tool_choice="auto", temperature=0.7, on_delta=delta
                )
            )
            await asyncio.wait_for(delta_seen.wait(), 1)
            assert not task.done()
            release.set()
            text, calls, meta = await task
        assert text == "让我算一下。"
        assert calls == [{"id": "c1", "name": "calculate", "arguments": '{"expr":"2+2"}'}]
        assert meta["streamed"] is True

    asyncio.run(run())


def test_cancel_closes_upstream_response():
    async def run():
        seen, closed = asyncio.Event(), asyncio.Event()
        stream = _Stream([{"choices": [{"delta": {"content": "你好"}}]}], pause=asyncio.Event(), closed=closed)

        async def delta(_text):
            seen.set()

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))
        ) as client:
            task = asyncio.create_task(
                stream_tool_completion(
                    client, [], _cfg(), tools=None, tool_choice=None, temperature=0.7, on_delta=delta
                )
            )
            await asyncio.wait_for(seen.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert closed.is_set()

    asyncio.run(run())


@pytest.mark.parametrize("end", [None, "length"])
def test_truncated_stream_does_not_return_executable_tools(end):
    async def run():
        events = [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "id": "c", "function": {"name": "memory_add", "arguments": '{"text":'}}
                            ]
                        }
                    }
                ]
            }
        ]
        if end:
            events.append({"choices": [{"delta": {}, "finish_reason": end}]})

        async def delta(_):
            pass

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=_Stream(events)))
        ) as client:
            with pytest.raises(RuntimeError, match="未完整|提前结束"):
                await stream_tool_completion(
                    client, [], _cfg(), tools=[], tool_choice="auto", temperature=0.7, on_delta=delta
                )

    asyncio.run(run())


def test_ark_stream_uses_completed_response_for_function_arguments():
    async def run():
        response = {
            "output": [{"type": "function_call", "call_id": "c1", "name": "calculate", "arguments": '{"n":2}'}],
            "usage": {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13},
        }
        events = [
            {"type": "response.output_item.added", "item": {"type": "function_call"}},
            {"type": "response.function_call_arguments.delta", "delta": '{"n":'},
            {"type": "response.completed", "response": response},
        ]

        async def delta(_):
            raise AssertionError("tool arguments must never be spoken")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=_Stream(events)))
        ) as client:
            _, calls, meta = await stream_tool_completion(
                client,
                [{"role": "user", "content": "算一下"}],
                _cfg("ark_responses"),
                tools=[],
                tool_choice="auto",
                temperature=0.7,
                on_delta=delta,
            )
        assert calls == [{"id": "c1", "name": "calculate", "arguments": '{"n":2}'}]
        assert meta["usage"]["total_tokens"] == 13

    asyncio.run(run())


def test_local_json_wrapper_is_not_retried_or_falsely_reported_streaming():
    async def run():
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"choices": [{"message": {"content": "你好。"}}]})

        async def delta(_):
            raise AssertionError("whole JSON must follow the normal validated playback path")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            text, calls, meta = await stream_tool_completion(
                client, [], _cfg(), tools=[], tool_choice="auto", temperature=0.7, on_delta=delta
            )
        assert text == "你好。" and not calls
        assert not meta["streamed"] and len(requests) == 1

    asyncio.run(run())


def test_local_model_without_key_omits_empty_bearer_header():
    async def run():
        def handler(request):
            assert "authorization" not in request.headers
            return httpx.Response(200, json={"choices": [{"message": {"content": "你好。"}}]})

        async def delta(_):
            pass

        cfg = ResolvedLlmConfig("local", "", "http://127.0.0.1:9106/v1", "openai", "test", "local")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await stream_tool_completion(client, [], cfg, tools=[], tool_choice="auto", temperature=0.7, on_delta=delta)

    asyncio.run(run())
