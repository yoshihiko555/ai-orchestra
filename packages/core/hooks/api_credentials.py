"""API キーの保存と解決。SDK とプロジェクト設定には依存しない。"""

from __future__ import annotations

import configparser
import os
import stat
import tempfile
from pathlib import Path

PROVIDERS = ("anthropic", "openai", "gemini")
ENV_VARS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
}


class CredentialsError(ValueError):
    """キーの値を含まない認証情報のエラー。"""


def credentials_path() -> Path:
    return Path.home() / ".config" / "orchex" / "credentials"


def _read_credentials() -> configparser.RawConfigParser:
    config = configparser.RawConfigParser()
    path = credentials_path()
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            return config
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, encoding="utf-8") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return config
            config.read_file(stream)
    except FileNotFoundError:
        return config
    except (OSError, UnicodeError, configparser.Error):
        raise CredentialsError("認証情報ファイルを読み込めません。") from None
    return config


def resolve_api_key(provider: str) -> tuple[str, str] | None:
    if provider not in ENV_VARS:
        raise CredentialsError("未対応の API プロバイダです。")
    key = os.environ.get(ENV_VARS[provider], "").strip()
    if key:
        return key, "environment"
    key = _read_credentials().get(provider, "api_key", fallback="").strip()
    return (key, "credentials") if key else None


def save_api_key(provider: str, key: str) -> None:
    if provider not in PROVIDERS or not key.strip() or any(char in key for char in "\r\n\x00"):
        raise CredentialsError("プロバイダと空でない一行の API キーを指定してください。")
    path = credentials_path()
    if path.parent.is_symlink():
        raise CredentialsError("認証情報ディレクトリにシンボリックリンクは使えません。")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    config = _read_credentials()
    if not config.has_section(provider):
        config.add_section(provider)
    config.set(provider, "api_key", key.strip())
    fd, temporary = tempfile.mkstemp(prefix=".credentials-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            config.write(stream)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def key_status() -> dict[str, str | None]:
    statuses = {}
    for provider in PROVIDERS:
        resolved = resolve_api_key(provider)
        statuses[provider] = resolved[1] if resolved else None
    return statuses
