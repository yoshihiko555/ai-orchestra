from tests.module_loader import load_module

check_codex_before_write = load_module(
    "check_codex_before_write",
    "packages/codex-suggestions/hooks/check-codex-before-write.py",
)


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
