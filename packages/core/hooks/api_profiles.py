"""API プロファイルと利用可否の共有解決。SDK は import しない。"""

from __future__ import annotations

import importlib.util
import math
import re
from dataclasses import dataclass, field

from api_credentials import PROVIDERS, resolve_api_key

SDK_MODULES = {"anthropic": "anthropic", "openai": "openai", "gemini": "google.genai"}
_SAFE_NAME = re.compile(r"[A-Za-z0-9_.:/@+=\-]+")


class ProfileError(ValueError):
    """設定の生値を表示しないプロファイルエラー。"""


@dataclass(frozen=True)
class ResolvedProfile:
    profile: str
    provider: str
    provider_enabled: bool
    model: str
    web_search: bool
    attachments: bool
    timeout_sec: float
    max_output_tokens: int
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class APIAvailability:
    reason: str | None
    api_key: str | None = field(repr=False)


@dataclass(frozen=True)
class AgentLimits:
    max_turns: int = 30
    max_total_tokens: int = 2_000_000
    max_duration_sec: float = 900
    rate_limit_attempts: int = 3
    rate_limit_initial_delay_sec: float = 2.0


@dataclass(frozen=True)
class AgentSettings:
    profile: ResolvedProfile
    repo_read: bool
    deny: tuple[str, ...]
    limits: AgentLimits


def provider_dependency_available(provider: str) -> bool:
    try:
        return importlib.util.find_spec(SDK_MODULES[provider]) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def _mapping(value: object, name: str) -> dict:
    if not isinstance(value, dict):
        raise ProfileError(f"API プロファイルの {name} は mapping が必要です。")
    return value


def _safe_name(value: object, name: str) -> str:
    if not isinstance(value, str) or not _SAFE_NAME.fullmatch(value):
        raise ProfileError(f"API プロファイルの {name} が不正です。")
    return value


def resolve_profile(config: dict, name: str) -> ResolvedProfile:
    name = _safe_name(name, "name")
    api = _mapping(config.get("api"), "api")
    profiles = _mapping(api.get("profiles"), "profiles")
    profile = _mapping(profiles.get(name), "profile")
    provider = profile.get("provider")
    if not isinstance(provider, str) or provider not in PROVIDERS:
        raise ProfileError("API プロファイルの provider が未対応です。")
    providers = _mapping(api.get("providers"), "providers")
    settings = _mapping(providers.get(provider), "provider")
    provider_enabled = settings.get("enabled", True)
    if not isinstance(provider_enabled, bool):
        raise ProfileError("API プロバイダの enabled は真偽値が必要です。")
    model = _safe_name(profile.get("model", settings.get("model")), "model")
    features = _mapping(profile.get("features", {}), "features")
    limits = _mapping(profile.get("limits", {}), "limits")
    web_search = features.get("web_search", False)
    attachments = features.get("attachments", False)
    if not isinstance(web_search, bool) or not isinstance(attachments, bool):
        raise ProfileError("API プロファイルの features は真偽値が必要です。")
    timeout = limits.get("timeout_sec", 120)
    tokens = limits.get("max_output_tokens", 4096)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ProfileError("API プロファイルの timeout_sec は正の有限数が必要です。")
    try:
        valid_timeout = timeout > 0 and math.isfinite(timeout)
    except OverflowError:
        valid_timeout = False
    if not valid_timeout:
        raise ProfileError("API プロファイルの timeout_sec は正の有限数が必要です。")
    if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0:
        raise ProfileError("API プロファイルの max_output_tokens は正の整数が必要です。")
    allowlist = settings.get("model_allowlist", [])
    if not isinstance(allowlist, list) or not all(isinstance(item, str) for item in allowlist):
        raise ProfileError("API プロバイダの model_allowlist は文字列の配列が必要です。")
    warnings = (
        ("API model は model_allowlist にありません。",)
        if allowlist and model not in allowlist
        else ()
    )
    return ResolvedProfile(
        name,
        provider,
        provider_enabled,
        model,
        web_search,
        attachments,
        float(timeout),
        tokens,
        warnings,
    )


def check_availability(profile: ResolvedProfile) -> APIAvailability:
    if not profile.provider_enabled:
        return APIAvailability("provider_disabled", None)
    resolved_key = resolve_api_key(profile.provider)
    if resolved_key is None:
        return APIAvailability("key_missing", None)
    if not provider_dependency_available(profile.provider):
        return APIAvailability("dependency_missing", None)
    return APIAvailability(None, resolved_key[0])


def resolve_agent_settings(config: dict, name: str) -> AgentSettings:
    resolved = resolve_profile(config, name)
    profile = config["api"]["profiles"][name]
    tools = _mapping(profile.get("tools", {}), "tools")
    repo_read = tools.get("repo_read", False)
    if set(tools) - {"repo_read"} or not isinstance(repo_read, bool):
        raise ProfileError("API プロファイルの tools は repo_read の真偽値だけを指定してください。")
    deny = profile.get("deny", [])
    if not isinstance(deny, list) or not all(isinstance(item, str) for item in deny):
        raise ProfileError("API プロファイルの deny は文字列の配列が必要です。")
    limits = profile.get("limits", {})
    defaults = AgentLimits()
    values = {}
    for field_name in ("max_turns", "max_total_tokens", "rate_limit_attempts"):
        value = limits.get(field_name, getattr(defaults, field_name))
        if type(value) is not int or value < 1:
            raise ProfileError(f"API プロファイルの {field_name} は正の整数が必要です。")
        values[field_name] = value
    for field_name in ("max_duration_sec", "rate_limit_initial_delay_sec"):
        value = limits.get(field_name, getattr(defaults, field_name))
        try:
            valid = (
                type(value) in (int, float)
                and math.isfinite(value)
                and (value >= 0 if field_name == "rate_limit_initial_delay_sec" else value > 0)
            )
        except OverflowError:
            valid = False
        if not valid:
            raise ProfileError(f"API プロファイルの {field_name} の数値が不正です。")
        values[field_name] = float(value)
    return AgentSettings(resolved, repo_read, tuple(deny), AgentLimits(**values))


def availability_message(reason: str, provider: str) -> str:
    messages = {
        "provider_disabled": f"API プロバイダ {provider} は無効です。",
        "key_missing": f"API キーが未設定です。orchex auth set {provider} を実行してください。",
        "dependency_missing": "API SDK が未導入です。orchex[llm] をインストールしてください。",
    }
    return messages[reason]
