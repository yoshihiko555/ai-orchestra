"""認証情報と一回の LLM API 呼び出しの CLI 境界。"""

from __future__ import annotations

import argparse
import getpass
import sys
import warnings
from pathlib import Path

from api_credentials import PROVIDERS, CredentialsError, key_status, save_api_key
from api_profiles import ProfileError, availability_message, check_availability, resolve_profile
from hook_common import load_cli_tools_config

from .llm.attachments import read_attachment
from .llm.providers import call_provider
from .llm.retry import call_with_rate_limit_retry
from .llm.types import LLMError, LLMRequest

_AVAILABILITY_CODES = {"key_missing": 3, "provider_disabled": 4, "dependency_missing": 5}
_ERROR_CODES = {
    "timeout": 6,
    "api_error": 7,
    "unsupported_attachment": 8,
    "quota_exhausted": 9,
    "rate_limited": 10,
    "auth_error": 11,
}
_RATE_LIMIT_ATTEMPTS = 3
_RATE_LIMIT_INITIAL_DELAY_SEC = 2.0


def run_auth_set(provider: str | None) -> int:
    if provider not in PROVIDERS:
        print("auth set には anthropic / openai / gemini を指定してください。", file=sys.stderr)
        return 2
    try:
        # TTY が使えない場合の getpass のエコー付き fallback を禁止する。
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            key = getpass.getpass("API キー: ")
        save_api_key(provider, key)
    except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        print("キーを非表示で入力できませんでした。対話端末で実行してください。", file=sys.stderr)
        return 2
    except (CredentialsError, OSError):
        print(
            "API キーを保存できませんでした。空でない一行のキーと保存先の権限を確認してください。",
            file=sys.stderr,
        )
        return 8
    print(f"{provider}: キーを保存しました。")
    return 0


def run_auth_status() -> int:
    try:
        statuses = key_status()
    except CredentialsError:
        print("認証情報ファイルを読み込めません。", file=sys.stderr)
        return 8
    for provider, source in statuses.items():
        print(f"{provider}: {'設定済み' if source else '未設定'}")
    return 0


def run_llm_ask(args: argparse.Namespace, project_dir: Path) -> int:
    key = ""
    try:
        config = load_cli_tools_config(str(project_dir))
        profile = resolve_profile(config, args.profile)
        availability = check_availability(profile)
        if availability.reason is not None:
            print(availability_message(availability.reason, profile.provider), file=sys.stderr)
            return _AVAILABILITY_CODES[availability.reason]
        assert availability.api_key is not None
        key = availability.api_key
        for warning in profile.warnings:
            print(warning, file=sys.stderr)
        if args.attach and not profile.attachments:
            raise LLMError(
                "unsupported_attachment", "このプロファイルでは添付が許可されていません。"
            )
        attachments = tuple(read_attachment(path) for path in args.attach)
        request = LLMRequest(
            model=profile.model,
            prompt=args.prompt,
            attachments=attachments,
            web_search=profile.web_search,
            timeout_sec=profile.timeout_sec,
            max_output_tokens=profile.max_output_tokens,
        )
        response = call_with_rate_limit_retry(
            lambda: call_provider(profile.provider, request, key),
            max_attempts=_RATE_LIMIT_ATTEMPTS,
            initial_delay_sec=_RATE_LIMIT_INITIAL_DELAY_SEC,
        )
        text = response.text.replace(key, "[REDACTED]")
        if args.output:
            args.output.write_text(text + ("" if text.endswith("\n") else "\n"), encoding="utf-8")
        else:
            print(text)
        return 0
    except (ProfileError, CredentialsError) as exc:
        print(str(exc), file=sys.stderr)
        return 8
    except LLMError as exc:
        message = str(exc).replace(key, "[REDACTED]") if key else str(exc)
        print(message, file=sys.stderr)
        return _ERROR_CODES[exc.category]
    except (OSError, UnicodeError):
        print(
            "LLM 入出力を完了できませんでした。ファイルの権限と文字コードを確認してください。",
            file=sys.stderr,
        )
        return 8
