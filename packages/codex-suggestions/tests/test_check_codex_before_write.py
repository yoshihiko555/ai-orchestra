import sys

from tests.module_loader import REPO_ROOT, load_module

check_codex_before_write = load_module(
    "check_codex_before_write",
    "packages/codex-suggestions/hooks/check-codex-before-write.py",
)


def test_imports_without_agent_routing_hooks_on_path() -> None:
    """agent-routing の hooks ディレクトリが sys.path に無くても import できる。

    manifest.json の depends は ["core"] のみであり、is_cli_enabled は
    hook_common（core）から直接 import するため agent-routing への暗黙依存が
    無いことを保証する回帰テスト。
    """
    routing_hooks_dir = str(REPO_ROOT / "packages" / "agent-routing" / "hooks")
    removed_paths = [p for p in sys.path if p == routing_hooks_dir]
    for path in removed_paths:
        sys.path.remove(path)
    sys.modules.pop("route_config", None)
    try:
        assert routing_hooks_dir not in sys.path
        module = load_module(
            "check_codex_before_write_no_routing_dep",
            "packages/codex-suggestions/hooks/check-codex-before-write.py",
        )
        assert hasattr(module, "is_cli_enabled")
    finally:
        sys.path.extend(removed_paths)


def test_validate_input_accepts_reasonable_values() -> None:
    assert check_codex_before_write.validate_input("src/app.py", "print('ok')")


def test_validate_input_rejects_invalid_values() -> None:
    assert not check_codex_before_write.validate_input("", "x")
    assert not check_codex_before_write.validate_input("a" * 4097, "x")
    assert not check_codex_before_write.validate_input("src/app.py", "x" * 1_000_001)
    assert not check_codex_before_write.validate_input("../secret.py", "x")


def test_should_suggest_codex_skips_simple_edit_files() -> None:
    should_suggest, reason = check_codex_before_write.should_suggest_codex(
        "README.md", "class A: pass"
    )
    assert not should_suggest
    assert reason == ""


def test_should_suggest_codex_for_design_path_indicator() -> None:
    should_suggest, reason = check_codex_before_write.should_suggest_codex(
        "docs/ARCHITECTURE.md", "small"
    )
    assert should_suggest
    assert "File path contains" in reason


def test_should_suggest_codex_for_large_content() -> None:
    should_suggest, reason = check_codex_before_write.should_suggest_codex(
        "src/new_feature.py", "x" * 600
    )
    assert should_suggest
    assert reason == "Creating new file with significant content"


def test_should_suggest_codex_for_content_indicator() -> None:
    should_suggest, reason = check_codex_before_write.should_suggest_codex(
        "src/service_logic.py", "class Service:\n    pass"
    )
    assert should_suggest
    assert "Content contains" in reason


def test_should_suggest_codex_for_large_src_file() -> None:
    should_suggest, reason = check_codex_before_write.should_suggest_codex(
        "src/feature.py", "y" * 250
    )
    assert should_suggest
    assert reason == "New source file"


def test_should_suggest_codex_false_for_small_regular_file() -> None:
    should_suggest, reason = check_codex_before_write.should_suggest_codex(
        "tools/script.py", "print('ok')"
    )
    assert not should_suggest
    assert reason == ""
