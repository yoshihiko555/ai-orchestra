"""候補ごとの隔離環境で実行する、実APIに接続しない比較実験。"""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import os
import socket
import sys
import traceback
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

from tests.unit.llm_sdk_support import error_response, tool_response


def reject_network(*args, **kwargs):
    raise AssertionError("比較実験で実ネットワークに接続してはいけません。")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("library", choices=["any", "litellm"])
    args = parser.parse_args()
    os.environ["LITELLM_MODE"] = "PRODUCTION"
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    os.environ["LITELLM_LOG"] = "ERROR"
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        os.environ.pop(name, None)
    socket.socket.connect = reject_network
    records = []
    state = {"provider": "", "category": None, "requests": []}

    with ExitStack() as stack:
        os.environ["HOME"] = stack.enter_context(TemporaryDirectory(prefix="orchex-compare-"))
        for module_name in ("httpx", "httpx2"):
            try:
                http = importlib.import_module(module_name)
            except ImportError:
                continue

            def send(client, request, *, _http=http, **kwargs):
                assert request.method == "POST"
                state["requests"].append(json.loads(request.content))
                if state["category"] is not None:
                    status, payload = error_response(state["provider"], state["category"])
                else:
                    status, payload = (
                        200,
                        tool_response(
                            state["provider"], request.url.path, final=len(state["requests"]) == 2
                        ),
                    )
                return _http.Response(status, json=payload, request=request)

            async def asend(client, request, *, _send=send, **kwargs):
                return _send(client, request, **kwargs)

            stack.enter_context(patch.object(http.Client, "send", send))
            stack.enter_context(patch.object(http.AsyncClient, "send", asend))

        if args.library == "any":
            from any_llm import AnyLLM
            from any_llm.utils.aio import run_async_in_sync

            def make_call(provider):
                instance = AnyLLM.create(
                    provider, api_key="dummy-key", unified_exceptions=False, max_retries=0
                )

                def call(messages, tools):
                    return instance.completion(
                        model="test-model", messages=messages, tools=tools, max_tokens=123
                    )

                def close():
                    if provider == "gemini":
                        run_async_in_sync(instance.client.aio.aclose())
                        instance.client.close()
                    else:
                        run_async_in_sync(instance.client.close())

                return call, close

        else:
            import litellm

            litellm.telemetry = False
            litellm.disable_cost_tracking = True
            litellm.suppress_debug_info = True

            def make_call(provider):
                def call(messages, tools):
                    return litellm.completion(
                        model=f"{provider}/"
                        + {
                            "anthropic": "claude-sonnet-4-5",
                            "openai": "gpt-4o-mini",
                            "gemini": "gemini-2.5-flash",
                        }[provider],
                        messages=messages,
                        tools=tools,
                        api_key="dummy-key",
                        num_retries=0,
                        max_tokens=123,
                    )

                return call, lambda: None

        tool = {
            "type": "function",
            "function": {
                "name": "lookup_value",
                "description": "値を照会する",
                "parameters": {
                    "type": "object",
                    "properties": {"value": {"type": "integer"}},
                    "required": ["value"],
                },
            },
        }
        for provider in ("anthropic", "openai", "gemini"):
            state.update(provider=provider, category=None, requests=[])
            first = None
            call, close = make_call(provider)
            try:
                messages = [{"role": "user", "content": "質問"}]
                first = call(messages, [tool]).choices[0].message
                assert len(state["requests"]) == 1
                assert len(first.tool_calls) == 2
                assert len({c.id for c in first.tool_calls}) == 2
                assert [json.loads(c.function.arguments) for c in first.tool_calls] == [
                    {"value": 1},
                    {"value": 2},
                ]
                messages.append(first.model_dump(exclude_none=True))
                messages.extend(
                    {
                        "role": "tool",
                        "tool_call_id": c.id,
                        "name": c.function.name,
                        "content": c.id + "-result",
                    }
                    for c in reversed(first.tool_calls)
                )
                second = call(messages, [tool]).choices[0].message
                assert second.content == "確認完了"
                assert len(state["requests"]) == 2
                body = state["requests"][-1]
                if provider == "gemini":
                    parts = body["contents"][-2]["parts"]
                    assert [p["functionCall"]["id"] for p in parts] == ["call_first", "call_second"]
                    expected = tool_response(provider, "", final=False)["candidates"][0]["content"]
                    assert [p["thoughtSignature"] for p in parts] == [
                        p["thoughtSignature"] for p in expected["parts"]
                    ]
                    results = [p["functionResponse"] for p in body["contents"][-1]["parts"]]
                    assert {r["id"] for r in results} == {"call_first", "call_second"}
                elif provider == "anthropic":
                    assert {r["tool_use_id"] for r in body["messages"][-1]["content"]} == {
                        "call_first",
                        "call_second",
                    }
                else:
                    assert {m["tool_call_id"] for m in body["messages"] if m["role"] == "tool"} == {
                        "call_first",
                        "call_second",
                    }
                records.append({"provider": provider, "roundtrip": "passed"})
            except Exception as exc:
                records.append(
                    {
                        "provider": provider,
                        "roundtrip": type(exc).__name__,
                        "missing_module": getattr(exc, "name", None),
                        "requests": len(state["requests"]),
                        "request_fields": [sorted(request) for request in state["requests"]],
                        "failure_sites": [
                            [Path(frame.filename).name, frame.lineno, frame.name]
                            for frame in traceback.extract_tb(exc.__traceback__)[-4:]
                        ],
                        "native_ids_preserved": first is not None
                        and [c.id for c in first.tool_calls] == ["call_first", "call_second"],
                    }
                )
            finally:
                close()

            for category in ("quota_exhausted", "rate_limited", "auth_error"):
                state.update(category=category, requests=[])
                call, close = make_call(provider)
                try:
                    call([{"role": "user", "content": "質問"}], [tool])
                    records.append({"provider": provider, "category": category, "raised": False})
                except Exception as exc:
                    response = getattr(exc, "response", None)
                    try:
                        payload = response.json() if response is not None else {}
                        response_json_available = response is not None
                    except json.JSONDecodeError:
                        payload = {}
                        response_json_available = False
                    error = payload.get("error", {})
                    causes = []
                    cause = exc.__cause__ or exc.__context__
                    seen = {id(exc)}
                    while cause is not None and id(cause) not in seen:
                        seen.add(id(cause))
                        cause_response = getattr(cause, "response", None)
                        try:
                            cause_payload = (
                                cause_response.json() if cause_response is not None else {}
                            )
                        except json.JSONDecodeError:
                            cause_payload = {}
                        cause_error = cause_payload.get("error", {})
                        detail_codes = [
                            {
                                "reason": detail.get("reason"),
                                "quota_ids": [
                                    v.get("quotaId") for v in detail.get("violations", [])
                                ],
                            }
                            for detail in cause_error.get("details", [])
                        ]
                        causes.append(
                            {
                                "exception": type(cause).__name__,
                                "code": getattr(cause, "code", None),
                                "status_code": getattr(cause, "status_code", None),
                                "response_error_code": cause_error.get("code"),
                                "response_error_status": cause_error.get("status"),
                                "detail_codes": detail_codes,
                            }
                        )
                        cause = cause.__cause__ or cause.__context__
                    records.append(
                        {
                            "provider": provider,
                            "category": category,
                            "exception": type(exc).__name__,
                            "status_code": getattr(exc, "status_code", None),
                            "code": getattr(exc, "code", None),
                            "type": getattr(exc, "type", None),
                            "status": getattr(exc, "status", None),
                            "response_available": getattr(exc, "response", None) is not None,
                            "response_error_code": error.get("code"),
                            "response_error_status": error.get("status"),
                            "response_json_available": response_json_available,
                            "causes": causes,
                            "requests": len(state["requests"]),
                        }
                    )
                finally:
                    close()
    print(
        json.dumps(
            {
                "library": args.library,
                "version": importlib.metadata.version(
                    "any-llm-sdk" if args.library == "any" else "litellm"
                ),
                "python": sys.version.split()[0],
                "results": records,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
