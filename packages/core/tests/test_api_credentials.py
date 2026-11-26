"""API キーの保存と解決を、一時 HOME とダミー値だけで検証する。"""

from __future__ import annotations

import configparser
import importlib
import stat
from pathlib import Path

import pytest

from tests.module_loader import REPO_ROOT


@pytest.fixture
def credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.syspath_prepend(str(REPO_ROOT / "packages/core/hooks"))
    return importlib.import_module("api_credentials")


def test_save_creates_private_credentials(credentials, tmp_path: Path) -> None:
    credentials.save_api_key("gemini", "dummy-gemini-percent%value")

    path = tmp_path / ".config/orchex/credentials"
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    saved = configparser.RawConfigParser()
    saved.read(path, encoding="utf-8")
    assert saved["gemini"]["api_key"] == "dummy-gemini-percent%value"


def test_update_preserves_other_provider_and_repairs_permissions(
    credentials, tmp_path: Path
) -> None:
    credentials.save_api_key("anthropic", "dummy-anthropic-retained")
    path = tmp_path / ".config/orchex/credentials"
    path.parent.chmod(0o755)
    path.chmod(0o644)

    credentials.save_api_key("gemini", "dummy-gemini-updated")

    assert credentials.resolve_api_key("anthropic")[0] == "dummy-anthropic-retained"
    assert credentials.resolve_api_key("gemini")[0] == "dummy-gemini-updated"
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
def test_environment_key_wins_over_saved_key(credentials, monkeypatch, provider: str) -> None:
    credentials.save_api_key(provider, "dummy-file-value")
    monkeypatch.setenv(f"{provider.upper()}_API_KEY", "dummy-environment-value")

    assert credentials.resolve_api_key(provider)[0] == "dummy-environment-value"


@pytest.mark.parametrize("environment_value", [None, ""])
def test_missing_or_empty_environment_uses_file(
    credentials, monkeypatch, environment_value
) -> None:
    credentials.save_api_key("gemini", "dummy-file-fallback")
    if environment_value is not None:
        monkeypatch.setenv("GEMINI_API_KEY", environment_value)

    assert credentials.resolve_api_key("gemini")[0] == "dummy-file-fallback"


def test_missing_key_is_not_resolved(credentials) -> None:
    assert credentials.resolve_api_key("openai") is None


def test_key_status_contains_sources_without_values(credentials, monkeypatch) -> None:
    credentials.save_api_key("anthropic", "dummy-file-anthropic")
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-env-gemini")

    statuses = credentials.key_status()

    assert set(statuses) == {"anthropic", "openai", "gemini"}
    assert statuses["anthropic"]
    assert statuses["gemini"]
    assert statuses["openai"] is None
    assert all("dummy-" not in str(source) for source in statuses.values())


def test_failed_atomic_replace_preserves_previous_key(credentials, monkeypatch, tmp_path) -> None:
    credentials.save_api_key("gemini", "dummy-previous-value")
    path = tmp_path / ".config/orchex/credentials"
    previous = path.read_bytes()

    def fail_replace(source, destination):
        assert stat.S_IMODE(Path(source).stat().st_mode) == 0o600
        raise OSError("ダミーの保存失敗")

    monkeypatch.setattr(credentials.os, "replace", fail_replace)

    with pytest.raises(OSError, match="ダミーの保存失敗"):
        credentials.save_api_key("gemini", "dummy-new-value")

    assert path.read_bytes() == previous
    assert credentials.resolve_api_key("gemini")[0] == "dummy-previous-value"
    assert list(path.parent.iterdir()) == [path]


def test_save_replaces_symlink_without_changing_target(credentials, tmp_path) -> None:
    target = tmp_path / "other-file"
    target.write_text("保持するデータ", encoding="utf-8")
    path = tmp_path / ".config/orchex/credentials"
    path.parent.mkdir(parents=True)
    path.symlink_to(target)

    credentials.save_api_key("gemini", "dummy-new-key")

    assert not path.is_symlink()
    assert target.read_text(encoding="utf-8") == "保持するデータ"
    assert credentials.resolve_api_key("gemini")[0] == "dummy-new-key"


def test_malformed_file_error_does_not_include_contents(credentials, tmp_path) -> None:
    path = tmp_path / ".config/orchex/credentials"
    path.parent.mkdir(parents=True)
    path.write_text("dummy-private-credentialmarker\n[broken", encoding="utf-8")

    with pytest.raises(credentials.CredentialsError) as caught:
        credentials.resolve_api_key("gemini")

    assert "dummy-private" not in str(caught.value)
    assert "credentialmarker" not in str(caught.value)
