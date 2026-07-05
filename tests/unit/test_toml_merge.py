"""toml_merge.py のユニットテスト。"""

from __future__ import annotations

import tomllib

import pytest

from tests.module_loader import load_module

toml_merge = load_module("toml_merge", "scripts/lib/toml_merge.py")


class TestFindTomlSection:
    def test_finds_section_to_eof(self) -> None:
        content = "[a]\nx = 1\n"
        assert toml_merge.find_toml_section(content, "a") == (0, 2)

    def test_finds_section_to_next_header(self) -> None:
        content = "[a]\nx = 1\n[b]\ny = 2\n"
        assert toml_merge.find_toml_section(content, "a") == (0, 2)

    def test_returns_none_when_missing(self) -> None:
        content = "[a]\nx = 1\n"
        assert toml_merge.find_toml_section(content, "b") is None

    def test_finds_dotted_section_name(self) -> None:
        content = '[mcp_servers.foo]\ncommand = "x"\n'
        assert toml_merge.find_toml_section(content, "mcp_servers.foo") == (0, 2)

    def test_array_of_tables_header_not_mistaken_for_section(self) -> None:
        # "[[items]]" must not be misdetected as a "[items]" section header
        # (its own body is opaque to this merge helper, but the *real*
        # section below it must still be found correctly).
        content = "[[items]]\nname = 1\n[[items]]\nname = 2\n[a]\nx = 1\n"
        assert toml_merge.find_toml_section(content, "a") == (4, 6)
        assert toml_merge.find_toml_section(content, "items") is None

    def test_multiline_array_continuation_lines_not_mistaken_for_header(self) -> None:
        # A bracketed continuation line inside a multi-line array (e.g. the
        # last element of an array-of-arrays, "[3, 4]") must not be treated
        # as a section header just because it also ends with "]".
        content = "[a]\nmatrix = [\n  [1, 2],\n  [3, 4]\n]\n[b]\ny = 2\n"
        assert toml_merge.find_toml_section(content, "a") == (0, 5)
        assert toml_merge.find_toml_section(content, "b") == (5, 7)

    def test_triple_double_quoted_string_body_not_mistaken_for_header(self) -> None:
        # R12: a bracketed-looking line quoted *inside* a multi-line """
        # string literal must not be treated as a section header.
        content = '[a]\nx = """\n[b]\nnot a real section\n"""\n[c]\ny = 2\n'
        assert toml_merge.find_toml_section(content, "a") == (0, 5)
        assert toml_merge.find_toml_section(content, "b") is None
        assert toml_merge.find_toml_section(content, "c") == (5, 7)

    def test_triple_single_quoted_string_body_not_mistaken_for_header(self) -> None:
        # Same as above but for the ''' literal string variant.
        content = "[a]\nx = '''\n[b]\nnot a real section\n'''\n[c]\ny = 2\n"
        assert toml_merge.find_toml_section(content, "a") == (0, 5)
        assert toml_merge.find_toml_section(content, "b") is None
        assert toml_merge.find_toml_section(content, "c") == (5, 7)

    def test_single_line_triple_quoted_string_does_not_start_continuation(self) -> None:
        # A """...""" string fully opened and closed on one line must not
        # put subsequent lines into "inside a string" mode.
        content = '[a]\nx = """inline"""\n[b]\ny = 2\n'
        assert toml_merge.find_toml_section(content, "a") == (0, 2)
        assert toml_merge.find_toml_section(content, "b") == (2, 4)

    def test_multiline_string_body_ending_with_bracket_does_not_skip_next_header(self) -> None:
        # N4 (review repro): a line *inside* a multi-line """ string that
        # happens to end with "[" must not leak continuation state into the
        # real header that follows the string's closing delimiter.
        content = '[a]\ns = """\ntext ending with [\n"""\n[b]\ny = 2\n'
        assert toml_merge.find_toml_section(content, "a") == (0, 4)
        assert toml_merge.find_toml_section(content, "b") == (4, 6)

    def test_triple_quote_opening_line_ending_with_bracket_does_not_leak_continuation(
        self,
    ) -> None:
        # N4: when the *opening* delimiter line itself ends with one of the
        # continuation suffixes ("[", ",", "\\"), in_continuation must be
        # reset by the triple-quote-open detection so it is not carried
        # across the whole string body and past its closing line, which
        # would otherwise cause the real header right after the string to be
        # misdetected as still "inside a continuation" and skipped.
        content = '[a]\ns = """[\ntext\n"""\n[b]\ny = 2\n'
        assert toml_merge.find_toml_section(content, "a") == (0, 4)
        assert toml_merge.find_toml_section(content, "b") == (4, 6)


class TestUpsertTomlSection:
    def test_appends_when_missing(self) -> None:
        content = "[a]\nx = 1\n"
        merged = toml_merge.upsert_toml_section(content, "b", "[b]\ny = 2")
        assert "[a]\nx = 1" in merged
        assert "[b]\ny = 2" in merged
        tomllib.loads(merged)

    def test_appends_to_empty_content(self) -> None:
        merged = toml_merge.upsert_toml_section("", "a", "[a]\nx = 1")
        assert merged == "[a]\nx = 1\n"

    def test_replaces_existing_section(self) -> None:
        content = "[a]\nx = 1\n[b]\ny = 2\n"
        merged = toml_merge.upsert_toml_section(content, "a", "[a]\nx = 99")
        assert "x = 99" in merged
        assert "x = 1" not in merged
        assert "[b]\ny = 2" in merged

    def test_no_op_when_identical(self) -> None:
        content = "[a]\nx = 1\n"
        merged = toml_merge.upsert_toml_section(content, "a", "[a]\nx = 1")
        assert merged == content

    def test_preserves_comments_outside_section(self) -> None:
        content = "# top comment\n[a]\nx = 1\n"
        merged = toml_merge.upsert_toml_section(content, "a", "[a]\nx = 2")
        assert "# top comment" in merged

    def test_raises_on_invalid_new_section(self) -> None:
        content = "[a]\nx = 1\n"
        with pytest.raises(toml_merge.TomlMergeError):
            toml_merge.upsert_toml_section(content, "a", "[a]\nx = ")


class TestUpsertTomlKeyInSection:
    def test_adds_key_to_existing_section(self) -> None:
        content = "[features]\nfoo = true\n"
        merged = toml_merge.upsert_toml_key_in_section(content, "features", "bar", "false")
        assert "foo = true" in merged
        assert "bar = false" in merged
        tomllib.loads(merged)

    def test_updates_existing_key_when_overwrite_true(self) -> None:
        content = "[features]\nfoo = true\n"
        merged = toml_merge.upsert_toml_key_in_section(content, "features", "foo", "false")
        assert "foo = false" in merged
        assert "foo = true" not in merged

    def test_keeps_existing_key_when_overwrite_false(self) -> None:
        content = "[features]\nfoo = true\n"
        merged = toml_merge.upsert_toml_key_in_section(
            content, "features", "foo", "false", overwrite=False
        )
        assert merged == content

    def test_creates_section_when_missing(self) -> None:
        content = "[a]\nx = 1\n"
        merged = toml_merge.upsert_toml_key_in_section(content, "features", "foo", "true")
        assert "[features]" in merged
        assert "foo = true" in merged
        tomllib.loads(merged)

    def test_no_op_when_value_unchanged(self) -> None:
        content = "[features]\nfoo = true\n"
        merged = toml_merge.upsert_toml_key_in_section(content, "features", "foo", "true")
        assert merged == content

    def test_preserves_comment_lines_in_section(self) -> None:
        content = "[features]\n# a comment\nfoo = true\n"
        merged = toml_merge.upsert_toml_key_in_section(content, "features", "bar", "false")
        assert "# a comment" in merged
        assert "bar = false" in merged


class TestUpsertTomlTopLevelKey:
    def test_adds_key_when_no_sections(self) -> None:
        content = "x = 1\n"
        merged = toml_merge.upsert_toml_top_level_key(content, "default_permissions", '"read"')
        assert 'default_permissions = "read"' in merged
        tomllib.loads(merged)

    def test_adds_key_before_first_section(self) -> None:
        content = "x = 1\n[a]\ny = 2\n"
        merged = toml_merge.upsert_toml_top_level_key(content, "z", "3")
        lines = merged.splitlines()
        assert lines.index("z = 3") < lines.index("[a]")

    def test_updates_existing_top_level_key(self) -> None:
        content = 'default_permissions = "read"\n[a]\ny = 2\n'
        merged = toml_merge.upsert_toml_top_level_key(content, "default_permissions", '"write"')
        assert 'default_permissions = "write"' in merged
        assert 'default_permissions = "read"' not in merged

    def test_keeps_existing_key_when_overwrite_false(self) -> None:
        content = 'default_permissions = "read"\n'
        merged = toml_merge.upsert_toml_top_level_key(
            content, "default_permissions", '"write"', overwrite=False
        )
        assert merged == content

    def test_no_op_when_value_unchanged(self) -> None:
        content = 'default_permissions = "read"\n'
        merged = toml_merge.upsert_toml_top_level_key(content, "default_permissions", '"read"')
        assert merged == content


class TestFailClosedValidation:
    def test_key_in_section_invalid_result_raises(self) -> None:
        content = "[features]\nfoo = true\n"
        with pytest.raises(toml_merge.TomlMergeError):
            toml_merge.upsert_toml_key_in_section(content, "features", "bar", "not-a-value =")

    def test_top_level_invalid_result_raises(self) -> None:
        content = "x = 1\n"
        with pytest.raises(toml_merge.TomlMergeError):
            toml_merge.upsert_toml_top_level_key(content, "y", "not-a-value =")
