"""auth / llm の CLI から SDK 境界までを、実 API に接続せず検証する。"""

from __future__ import annotations

import base64
import configparser
import getpass
import importlib
import importlib.machinery
import re
import stat
import sys
import tomllib
import warnings
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
import yaml

from ai_orchestra import cli
from tests.module_loader import REPO_ROOT

PROVIDERS = ("anthropic", "openai", "gemini")


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AI_ORCHESTRA_DIR", str(REPO_ROOT))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.syspath_prepend(str(REPO_ROOT / "packages/core/hooks"))


@pytest.fixture
def project(tmp_path):
    path = tmp_path / "project"
    config_dir = path / ".claude/config/agent-routing"
    config_dir.mkdir(parents=True)
    config = {
        "api": {
            "providers": {
                provider: {
                    "enabled": True,
                    "model": f"{provider}-test-model",
                    "model_allowlist": [f"{provider}-test-model"],
                }
                for provider in PROVIDERS
            },
            "profiles": {
                provider: {
                    "provider": provider,
                    "features": {"web_search": False, "attachments": True},
                    "limits": {"timeout_sec": 17, "max_output_tokens": 123},
                }
                for provider in PROVIDERS
            },
        }
    }
    (config_dir / "cli-tools.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    return path


@pytest.fixture
def sdk_clients(monkeypatch):
    # 戻り値の必要部分は各社の公式 SDK の text / content / output_text 契約に合わせる。
    anthropic_call = Mock(
        return_value=SimpleNamespace(content=[SimpleNamespace(type="text", text="回答 anthropic")])
    )
    openai_call = Mock(return_value=SimpleNamespace(output_text="回答 openai"))
    gemini_call = Mock(return_value=SimpleNamespace(text="回答 gemini"))
    factories = {}
    for module_name, constructor, resource, method, operation in (
        ("anthropic", "Anthropic", "messages", "create", anthropic_call),
        ("openai", "OpenAI", "responses", "create", openai_call),
        ("google.genai", "Client", "models", "generate_content", gemini_call),
    ):
        client = MagicMock(spec=[resource, "close", "__enter__", "__exit__"])
        setattr(client, resource, SimpleNamespace(**{method: operation}))
        client.__enter__.return_value = client
        module = ModuleType(module_name)
        module.__spec__ = importlib.machinery.ModuleSpec(module_name, loader=None)
        factory = Mock(return_value=client)
        setattr(module, constructor, factory)
        monkeypatch.setitem(sys.modules, module_name, module)
        factories[module_name] = factory
    google = ModuleType("google")
    google.__path__ = []
    google.__spec__ = importlib.machinery.ModuleSpec("google", loader=None, is_package=True)
    google.genai = sys.modules["google.genai"]
    monkeypatch.setitem(sys.modules, "google", google)
    return {
        "calls": {"anthropic": anthropic_call, "openai": openai_call, "gemini": gemini_call},
        "factories": factories,
    }


def _run(monkeypatch, *args) -> int:
    monkeypatch.setattr(sys, "argv", ["orchex", *map(str, args)])
    try:
        cli.main()
    except SystemExit as exc:
        return 0 if exc.code is None else exc.code
    return 0


def _ask(monkeypatch, project, profile, *extra) -> int:
    return _run(
        monkeypatch, "llm", "ask", "--project", project, "--profile", profile, *extra, "質問"
    )


def _local(project, data) -> None:
    (project / ".claude/config/agent-routing/cli-tools.local.yaml").write_text(
        yaml.safe_dump(data), encoding="utf-8"
    )


def _assert_no_key(text: str) -> None:
    for line in text.splitlines():
        assert "dummy-private" not in line.casefold()
        assert "credentialmarker" not in line.casefold()


def test_auth_set_uses_hidden_prompt_and_private_file(monkeypatch, tmp_path, capsys) -> None:
    hidden_prompt = Mock(return_value="dummy-private-credentialmarker")
    monkeypatch.setattr(getpass, "getpass", hidden_prompt)
    monkeypatch.setattr("builtins.input", Mock(side_effect=AssertionError("非秘匿入力は使用不可")))

    code = _run(monkeypatch, "auth", "set", "gemini")

    assert code == 0
    hidden_prompt.assert_called_once()
    path = tmp_path / "home/.config/orchex/credentials"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    saved = configparser.RawConfigParser()
    saved.read(path, encoding="utf-8")
    assert saved["gemini"]["api_key"] == "dummy-private-credentialmarker"
    captured = capsys.readouterr()
    _assert_no_key(captured.out + captured.err)


def test_auth_set_rejects_key_argument_without_saving(monkeypatch, tmp_path, capsys) -> None:
    hidden_prompt = Mock(side_effect=AssertionError("引数エラーなら入力しない"))
    monkeypatch.setattr(getpass, "getpass", hidden_prompt)

    code = _run(monkeypatch, "auth", "set", "gemini", "dummy-private-credentialmarker")

    assert code == 2
    hidden_prompt.assert_not_called()
    assert not (tmp_path / "home/.config/orchex/credentials").exists()
    captured = capsys.readouterr()
    _assert_no_key(captured.out + captured.err)


def test_auth_set_empty_input_does_not_create_credentials(monkeypatch, tmp_path) -> None:
    hidden_prompt = Mock(return_value="")
    monkeypatch.setattr(getpass, "getpass", hidden_prompt)

    assert _run(monkeypatch, "auth", "set", "gemini") != 0
    hidden_prompt.assert_called_once()
    assert not (tmp_path / "home/.config/orchex/credentials").exists()


def test_auth_set_refuses_getpass_echo_fallback(monkeypatch, tmp_path, capsys) -> None:
    def unavailable_hidden_input(prompt):
        warnings.warn("hidden input unavailable", getpass.GetPassWarning, stacklevel=2)
        raise AssertionError("エコー付き入力へ進んではいけません。")

    monkeypatch.setattr(getpass, "getpass", unavailable_hidden_input)

    assert _run(monkeypatch, "auth", "set", "gemini") == 2

    assert not (tmp_path / "home/.config/orchex/credentials").exists()
    captured = capsys.readouterr()
    assert "非表示" in captured.err
    assert not captured.out


def test_auth_status_reports_presence_without_key_values(monkeypatch, capsys, caplog) -> None:
    credentials = importlib.import_module("api_credentials")
    credentials.save_api_key("anthropic", "dummy-private-credentialmarker-file")
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-private-credentialmarker-env")

    assert _run(monkeypatch, "auth", "status") == 0

    captured = capsys.readouterr()
    lines = {
        provider: next(line for line in captured.out.splitlines() if provider in line.lower())
        for provider in PROVIDERS
    }
    assert re.search(r"not.set|未設定|なし", lines["openai"], re.I)
    for provider in ("anthropic", "gemini"):
        assert re.search(r"set|設定|あり", lines[provider], re.I)
        assert not re.search(r"not.set|未設定|なし", lines[provider], re.I)
    _assert_no_key(captured.out + captured.err + caplog.text)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_ask_calls_selected_provider_once_and_prints_answer(
    project, sdk_clients, monkeypatch, capsys, provider
) -> None:
    monkeypatch.setenv(f"{provider.upper()}_API_KEY", "dummy-private-credentialmarker")

    assert _ask(monkeypatch, project, provider) == 0

    assert capsys.readouterr().out == f"回答 {provider}\n"
    selected = sdk_clients["calls"][provider]
    selected.assert_called_once()
    assert selected.call_args.kwargs["model"] == f"{provider}-test-model"
    for other, call in sdk_clients["calls"].items():
        if other != provider:
            call.assert_not_called()
    module_name = "google.genai" if provider == "gemini" else provider
    assert (
        sdk_clients["factories"][module_name].call_args.kwargs["api_key"]
        == "dummy-private-credentialmarker"
    )
    kwargs = selected.call_args.kwargs
    if provider == "gemini":
        assert kwargs["config"]["max_output_tokens"] == 123
        assert "質問" in str(kwargs["contents"])
        factory_options = sdk_clients["factories"][module_name].call_args.kwargs
        http_options = kwargs["config"].get("http_options", factory_options.get("http_options", {}))
        assert http_options["timeout"] == 17000
    else:
        assert kwargs["max_tokens" if provider == "anthropic" else "max_output_tokens"] == 123
        assert "質問" in str(kwargs["messages" if provider == "anthropic" else "input"])
        factory_options = sdk_clients["factories"][module_name].call_args.kwargs
        assert kwargs.get("timeout", factory_options.get("timeout")) == 17


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("web_search", [False, True])
def test_web_search_is_sent_only_when_profile_allows(
    project, sdk_clients, monkeypatch, provider, web_search
) -> None:
    monkeypatch.setenv(f"{provider.upper()}_API_KEY", "dummy-private-credentialmarker")
    _local(project, {"api": {"profiles": {provider: {"features": {"web_search": web_search}}}}})

    assert _ask(monkeypatch, project, provider) == 0

    kwargs = sdk_clients["calls"][provider].call_args.kwargs
    tools = kwargs["config"].get("tools", []) if provider == "gemini" else kwargs.get("tools", [])
    if web_search:
        assert len(tools) == 1
        if provider == "gemini":
            assert "google_search" in tools[0]
        elif provider == "openai":
            assert tools[0]["type"] == "web_search"
        else:
            assert tools[0]["name"] == "web_search"
            assert tools[0]["type"].startswith("web_search_")
    else:
        assert not tools


def test_local_profile_addition_and_override_reaches_anthropic(
    project, sdk_clients, monkeypatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-private-credentialmarker")
    _local(
        project,
        {"api": {"profiles": {"review": {"provider": "anthropic", "model": "local-review-model"}}}},
    )

    assert _ask(monkeypatch, project, "review") == 0

    sdk_clients["calls"]["anthropic"].assert_called_once()
    assert sdk_clients["calls"]["anthropic"].call_args.kwargs["model"] == "local-review-model"
    sdk_clients["calls"]["gemini"].assert_not_called()
    sdk_clients["calls"]["openai"].assert_not_called()


def test_profile_name_starting_with_dash_can_be_passed_with_equals(
    project, sdk_clients, monkeypatch
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-private-credentialmarker")
    _local(project, {"api": {"profiles": {"-review": {"provider": "anthropic"}}}})

    assert _run(monkeypatch, "llm", "ask", "--project", project, "--profile=-review", "質問") == 0

    sdk_clients["calls"]["anthropic"].assert_called_once()


def test_ask_redacts_key_if_response_echoes_it(project, sdk_clients, monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-private-credentialmarker")
    sdk_clients["calls"]["openai"].return_value.output_text = "回答 dummy-private-credentialmarker"

    assert _ask(monkeypatch, project, "openai") == 0

    captured = capsys.readouterr()
    assert captured.out == "回答 [REDACTED]\n"
    _assert_no_key(captured.out + captured.err)


def test_gemini_missing_text_keeps_specific_api_error(
    project, sdk_clients, monkeypatch, capsys, caplog
):
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-private-credentialmarker")
    sdk_clients["calls"]["gemini"].return_value = SimpleNamespace(text=None)

    assert _ask(monkeypatch, project, "gemini") == 7

    captured = capsys.readouterr()
    assert captured.err == "API error: テキストの回答がありませんでした。\n"
    assert not captured.out
    _assert_no_key(captured.out + captured.err + caplog.text)
    sdk_clients["calls"]["gemini"].assert_called_once()
    sdk_clients["factories"]["google.genai"].return_value.__exit__.assert_called_once()


def test_missing_key_has_distinct_exit_and_auth_guidance(project, sdk_clients, monkeypatch, capsys):
    assert _ask(monkeypatch, project, "openai") == 3

    captured = capsys.readouterr()
    assert "orchex auth set openai" in captured.err
    assert re.search(r"key|キー", captured.err, re.I)
    for call in sdk_clients["calls"].values():
        call.assert_not_called()


def test_disabled_provider_precedes_missing_key(project, sdk_clients, monkeypatch, capsys):
    _local(project, {"api": {"providers": {"openai": {"enabled": False}}}})

    assert _ask(monkeypatch, project, "openai") == 4

    assert re.search(r"disabled|無効", capsys.readouterr().err, re.I)
    for call in sdk_clients["calls"].values():
        call.assert_not_called()


def test_missing_sdk_has_distinct_exit(project, monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-private-credentialmarker")
    profiles = importlib.import_module("api_profiles")
    monkeypatch.setattr(profiles, "provider_dependency_available", lambda provider: False)

    assert _ask(monkeypatch, project, "openai") == 5

    assert re.search(r"dependenc|SDK|依存|llm", capsys.readouterr().err, re.I)


@pytest.mark.parametrize(("category", "code"), [("timeout", 6), ("api_error", 7)])
def test_provider_error_is_classified_and_key_is_redacted(
    project, sdk_clients, monkeypatch, capsys, caplog, category, code
):
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-private-credentialmarker")
    types = importlib.import_module("lib.llm.types")
    provider = importlib.import_module("lib.llm.openai_provider")
    call = Mock(side_effect=types.LLMError(category, f"{category}: dummy-private-credentialmarker"))
    monkeypatch.setattr(provider, "call", call)

    assert _ask(monkeypatch, project, "openai") == code

    call.assert_called_once()
    captured = capsys.readouterr()
    assert captured.err
    if category == "timeout":
        assert re.search(r"time.?out|timed.out|タイムアウト", captured.err, re.I)
    else:
        assert re.search(r"api", captured.err, re.I)
    _assert_no_key(captured.out + captured.err + caplog.text)


def test_forbidden_attachment_is_rejected_before_api(project, sdk_clients, monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-private-credentialmarker")
    _local(project, {"api": {"profiles": {"openai": {"features": {"attachments": False}}}}})
    attachment = tmp_path / "image.png"
    attachment.write_bytes(b"dummy-image")

    assert _ask(monkeypatch, project, "openai", "--attach", attachment) == 8

    for call in sdk_clients["calls"].values():
        call.assert_not_called()


def test_unreadable_attachment_is_rejected_before_api(project, sdk_clients, monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-private-credentialmarker")

    assert _ask(monkeypatch, project, "openai", "--attach", tmp_path / "missing.pdf") == 8

    for call in sdk_clients["calls"].values():
        call.assert_not_called()


def test_unsupported_attachment_is_rejected_before_api(project, sdk_clients, monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-private-credentialmarker")
    attachment = tmp_path / "archive.zip"
    attachment.write_bytes(b"dummy-archive")

    assert _ask(monkeypatch, project, "anthropic", "--attach", attachment) == 8

    for call in sdk_clients["calls"].values():
        call.assert_not_called()


def test_undefined_profile_is_rejected_before_api(project, sdk_clients, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-private-credentialmarker")

    assert _ask(monkeypatch, project, "undefined") == 8

    for call in sdk_clients["calls"].values():
        call.assert_not_called()


@pytest.mark.parametrize("provider", PROVIDERS)
def test_image_and_pdf_are_transferred_as_attachment_content(
    project, sdk_clients, monkeypatch, tmp_path, provider
):
    monkeypatch.setenv(f"{provider.upper()}_API_KEY", "dummy-private-credentialmarker")
    image = tmp_path / "image.png"
    pdf = tmp_path / "document.pdf"
    image.write_bytes(b"dummy-image-content")
    pdf.write_bytes(b"%PDF-1.4\ndummy-document-content")

    assert _ask(monkeypatch, project, provider, "--attach", image, "--attach", pdf) == 0

    call = sdk_clients["calls"][provider]
    call.assert_called_once()
    kwargs = call.call_args.kwargs
    if provider == "anthropic":
        content = kwargs["messages"][0]["content"]
        attachments = {block["type"]: block for block in content if block["type"] != "text"}
        assert set(attachments) == {"image", "document"}
        for kind, path, mime in (
            ("image", image, "image/png"),
            ("document", pdf, "application/pdf"),
        ):
            source = attachments[kind]["source"]
            assert source["media_type"] == mime
            assert source["type"] == "base64"
            assert base64.b64decode(source["data"]) == path.read_bytes()
    elif provider == "openai":
        content = kwargs["input"][0]["content"]
        attachments = {block["type"]: block for block in content if block["type"] != "input_text"}
        assert set(attachments) == {"input_image", "input_file"}
        image_url = attachments["input_image"]["image_url"]
        assert image_url.startswith("data:image/png;base64,")
        assert base64.b64decode(image_url.split(",", 1)[1]) == image.read_bytes()
        file_data = attachments["input_file"]["file_data"]
        assert file_data.startswith("data:application/pdf;base64,")
        assert base64.b64decode(file_data.split(",", 1)[1]) == pdf.read_bytes()
    else:
        parts = kwargs["contents"][0]["parts"]
        attachments = {
            part["inline_data"]["mime_type"]: part["inline_data"]["data"]
            for part in parts
            if "inline_data" in part
        }
        assert set(attachments) == {"image/png", "application/pdf"}
        for mime, path in (("image/png", image), ("application/pdf", pdf)):
            data = attachments[mime]
            assert (
                data if isinstance(data, bytes) else base64.b64decode(data)
            ) == path.read_bytes()


def test_output_option_writes_answer(project, sdk_clients, monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-private-credentialmarker")
    output = tmp_path / "answer.txt"

    assert _ask(monkeypatch, project, "gemini", "--output", output) == 0

    assert output.read_text(encoding="utf-8").rstrip("\n") == "回答 gemini"
    sdk_clients["calls"]["gemini"].assert_called_once()


def test_llm_dependencies_are_optional():
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert config["project"]["dependencies"] == ["pyyaml>=6.0"]
    extras = config["project"]["optional-dependencies"]["llm"]
    names = {re.split(r"[<>=!~;\[ ]", dependency, maxsplit=1)[0].lower() for dependency in extras}
    assert {"anthropic", "openai", "google-genai"} <= names
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    for dependency in extras:
        name = re.split(r"[<>=!~;\[ ]", dependency, maxsplit=1)[0].lower()
        if name not in {"any-llm-sdk", "litellm"}:
            continue
        match = re.fullmatch(r"[\w-]+(?:\[[^]]+\])?==([\d]+\.[\d]+\.[\d]+)", dependency)
        assert match is not None, "採用したライブラリは厳密なバージョン固定が必要です。"
        package = next(package for package in lock["package"] if package["name"] == name)
        assert package["version"] == match.group(1)
        distributions = package.get("wheels", []) + (
            [package["sdist"]] if "sdist" in package else []
        )
        assert distributions
        assert all(re.fullmatch(r"sha256:[a-f0-9]{64}", item["hash"]) for item in distributions)


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
@pytest.mark.parametrize(
    ("category", "exit_code", "message_pattern"),
    [
        ("quota_exhausted", 9, r"quota|枠|残高"),
        ("rate_limited", 10, r"rate|レート"),
        ("auth_error", 11, r"auth|認証|キー|権限"),
    ],
)
def test_ask_real_sdk_errors_have_distinct_exit_codes_and_no_private_details(
    sdk_environment,
    sdk_client_factory,
    project,
    monkeypatch,
    capsys,
    caplog,
    provider,
    category,
    exit_code,
    message_pattern,
):
    import logging

    from lib import llm_cli

    from tests.unit.llm_sdk_support import assert_no_error_details, error_response

    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv(f"{provider.upper()}_API_KEY", "dummy-private-credentialmarker")
    monkeypatch.setattr(llm_cli, "_RATE_LIMIT_ATTEMPTS", 3, raising=False)
    monkeypatch.setattr(llm_cli, "_RATE_LIMIT_INITIAL_DELAY_SEC", 0, raising=False)
    requests = []
    status, payload = error_response(provider, category)

    def handle(request):
        requests.append(request)
        return sdk_environment.http.Response(
            status, json=payload, headers={"x-private": "header-private-headermarker"}
        )

    adapter = importlib.import_module(f"lib.llm.{provider}_provider")
    factory = sdk_client_factory(provider, handle)
    monkeypatch.setattr(adapter, "_client_factory", lambda: factory)

    code = _ask(monkeypatch, project, provider)

    captured = capsys.readouterr()
    assert_no_error_details(captured.out, captured.err, caplog.text)
    assert code == exit_code
    assert not captured.out
    assert re.search(message_pattern, captured.err, re.I)
    assert len(requests) == (3 if category == "rate_limited" else 1)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_ask_recovers_after_one_real_sdk_rate_limit(
    sdk_environment, sdk_client_factory, project, monkeypatch, capsys, provider
):
    from lib import llm_cli

    from tests.unit.llm_sdk_support import error_response, tool_response

    monkeypatch.setenv(f"{provider.upper()}_API_KEY", "dummy-key")
    monkeypatch.setattr(llm_cli, "_RATE_LIMIT_ATTEMPTS", 3, raising=False)
    monkeypatch.setattr(llm_cli, "_RATE_LIMIT_INITIAL_DELAY_SEC", 0, raising=False)
    requests = []

    def handle(request):
        requests.append(request)
        assert len(requests) <= 2
        if len(requests) == 1:
            status, payload = error_response(provider, "rate_limited")
            return sdk_environment.http.Response(status, json=payload)
        return sdk_environment.http.Response(
            200, json=tool_response(provider, request.url.path, final=True)
        )

    adapter = importlib.import_module(f"lib.llm.{provider}_provider")
    factory = sdk_client_factory(provider, handle)
    monkeypatch.setattr(adapter, "_client_factory", lambda: factory)

    assert _ask(monkeypatch, project, provider) == 0

    assert len(requests) == 2
    captured = capsys.readouterr()
    assert captured.out == "確認完了\n"
    assert not captured.err
