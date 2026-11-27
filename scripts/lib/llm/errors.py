"""SDKをimportせず、ステータスと機械向けコードだけで失敗を分類する。"""

from __future__ import annotations

from .types import LLMError


def classify_anthropic(exc: Exception) -> LLMError:
    status = getattr(exc, "status_code", None)
    if (
        status == 402
        or getattr(exc, "type", None) == "billing_error"
        or getattr(exc, "error_code", None)
        in (
            "enforced_spend_limit_reached",
            "contract_spend_limit_reached",
            "linked_organization_spend_limit_reached",
        )
    ):
        return LLMError.for_category("quota_exhausted")
    if status in (401, 403):
        return LLMError.for_category("auth_error")
    if status == 429:
        return LLMError.for_category("rate_limited")
    return LLMError.from_exception(exc)


def classify_openai(exc: Exception) -> LLMError:
    status = getattr(exc, "status_code", None)
    if status == 429 and getattr(exc, "code", None) == "insufficient_quota":
        return LLMError.for_category("quota_exhausted")
    if status in (401, 403):
        return LLMError.for_category("auth_error")
    if status == 429:
        return LLMError.for_category("rate_limited")
    return LLMError.from_exception(exc)


def classify_gemini(exc: Exception) -> LLMError:
    status = getattr(exc, "code", None)
    payload = getattr(exc, "details", None)
    error = payload.get("error", payload) if isinstance(payload, dict) else None
    details = error.get("details", []) if isinstance(error, dict) else []
    details = details if isinstance(details, list) else []
    if status == 429 and getattr(exc, "status", None) == "RESOURCE_EXHAUSTED":
        for detail in details:
            if (
                not isinstance(detail, dict)
                or detail.get("@type") != "type.googleapis.com/google.rpc.QuotaFailure"
            ):
                continue
            violations = detail.get("violations", [])
            if not isinstance(violations, list):
                continue
            for violation in violations:
                quota_id = violation.get("quotaId") if isinstance(violation, dict) else None
                if isinstance(quota_id, str) and "PerDay" in quota_id:
                    return LLMError.for_category("quota_exhausted")
    if status in (401, 403) or (
        status == 400
        and any(
            isinstance(detail, dict)
            and detail.get("@type") == "type.googleapis.com/google.rpc.ErrorInfo"
            and detail.get("reason") == "API_KEY_INVALID"
            for detail in details
        )
    ):
        return LLMError.for_category("auth_error")
    if status == 429:
        return LLMError.for_category("rate_limited")
    return LLMError.from_exception(exc)
