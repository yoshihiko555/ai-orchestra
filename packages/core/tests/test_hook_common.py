import io
import sys

from tests.module_loader import load_module

hook_common = load_module("hook_common", "packages/core/hooks/hook_common.py")


def test_read_hook_input_valid_json(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"tool_name":"Edit"}'))
    assert hook_common.read_hook_input() == {"tool_name": "Edit"}


def test_read_hook_input_invalid_json(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("{invalid-json"))
    assert hook_common.read_hook_input() == {}


def test_get_field_returns_value_or_empty_string() -> None:
    data = {"name": "alice", "empty": "", "none": None, "zero": 0}
    assert hook_common.get_field(data, "name") == "alice"
    assert hook_common.get_field(data, "missing") == ""
    assert hook_common.get_field(data, "empty") == ""
    assert hook_common.get_field(data, "none") == ""
    assert hook_common.get_field(data, "zero") == ""
