"""EV-66〜68: 実行せずに下書きの境界を検査する。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from candidates_checks import command_allowed, fixture_allowlist, inspect_draft, setup_allowed
from candidates_safety import load_masker
from candidates_schema import schema_errors

REPO = Path(__file__).resolve().parents[3]


def inspect(context, texts, *, excerpts=None, masker=None):
    return inspect_draft(
        {f"draft-{index}.yaml": text.encode() for index, text in enumerate(texts)},
        context.orchestra,
        "C-00000001",
        "review",
        excerpts or [],
        masker or load_masker(),
    )


@pytest.mark.parametrize(
    "line",
    [
        "mkdir -p .meta-harness/artifacts/train/result",
        "touch .meta-harness/result.json",
        "printf 'synthetic' > .meta-harness/result.json",
        "cp packages/meta-harness/scenarios/fixtures/fake-gh.py .meta-harness/result.py",
    ],
)
def test_ev66_allowed_setup_shapes_pass(scenario_context, scenario_texts, line):
    assert inspect(scenario_context, scenario_texts(setup=[line]))["passed"]


@pytest.mark.parametrize(
    "line",
    [
        "mkdir -p /tmp",
        "touch -evil",
        "touch nested/a..b/result",
        "touch $(whoami)",
        "touch result;whoami",
        "touch result | whoami",
        "touch result && whoami",
        "touch result || whoami",
        "touch $HOME",
        "touch result > elsewhere",
        "printf '$HOME' > result",
        "printf 'a\\nb' > result",
        "printf '%s' > result",
        "printf '`whoami`' > result",
        "printf 'a'; whoami > result",
        "printf 'a;b' > result",
        "printf 'a|b' > result",
        "printf 'a&&b' > result",
        "printf 'a' >> result",
        "cp packages/meta-harness/scenarios/fixtures/absent.py result.py",
        "git status",
        "touch result\nwhoami",
        "printf 'safe\nunsafe' > result",
    ],
)
def test_ev66_unsafe_setup_is_reported_with_exact_line(scenario_context, scenario_texts, line):
    checks = inspect(scenario_context, scenario_texts(setup=[line]))
    assert any(
        finding["category"] == "shape"
        and finding["path"] == "$.setup[0]"
        and finding["line"] == line
        and finding["file"] == "draft-0.yaml"
        for finding in checks["findings"]
    )


@pytest.mark.parametrize(
    "tail",
    [
        "assert-cli-skill-route.py --tool codex --artifact result.json --added-flag value",
        "assert-cli-skill-route.py --project-root elsewhere",
        "assert-cli-skill-route.py --artifact nested/a..b/result",
        "assert-cli-skill-route.py --artifact /tmp/result",
        "assert-cli-skill-route.py --artifact=/tmp/result",
        "assert-cli-skill-route.py --artifact $HOME",
        "assert-cli-skill-route.py --artifact result;whoami",
        "assert-cli-skill-route.py --artifact result | whoami",
        "assert-cli-skill-route.py --artifact result && whoami",
        "assert-cli-skill-route.py --artifact result || whoami",
        "assert-cli-skill-route.py --artifact $(whoami)",
        "assert-cli-skill-route.py --artifact result > elsewhere",
        "assert-cli-skill-route.py -B",
        "not-allowed.py result",
        "assert-function-behavior.py --module result.py",
        "merge-local-cli-tools-override.py --project-root elsewhere",
    ],
)
def test_ev66_unsafe_commands_and_added_flags_are_rejected(scenario_context, scenario_texts, tail):
    line = "python3 packages/meta-harness/scenarios/fixtures/" + tail
    check = {"id": "command", "text": "人工検査", "oracle": "command_exit", "command": line}
    checks = inspect(scenario_context, scenario_texts(critical=[check]))
    assert any(
        finding["category"] == "shape" and finding["line"] == line for finding in checks["findings"]
    )


def test_ev66_flags_are_derived_per_script_and_only_safe_privileges_pass(
    scenario_context, scenario_texts
):
    allowlist = fixture_allowlist(scenario_context.orchestra)
    assert "--artifact" in allowlist["assert-cli-skill-route.py"]
    assert "--project-root" not in allowlist["merge-local-cli-tools-override.py"]
    assert "assert-function-behavior.py" not in allowlist
    assert command_allowed(
        "python3 packages/meta-harness/scenarios/fixtures/assert-cli-skill-route.py --tool codex --artifact result.json",
        allowlist,
    )
    assert not command_allowed(
        "python3 packages/meta-harness/scenarios/fixtures/assert-cli-skill-route.py --target-task result",
        allowlist,
    )
    assert inspect(
        scenario_context, scenario_texts(permission_mode="acceptEdits", allowed_tools=["Read"])
    )["passed"]
    for fields in (
        {"permission_mode": "bypassPermissions"},
        {"allowed_tools": ["Bash"]},
        {"path_prepend": ["bin"]},
    ):
        assert any(
            finding["category"] == "shape"
            for finding in inspect(scenario_context, scenario_texts(**fields))["findings"]
        )


@pytest.mark.parametrize(
    "value",
    [
        "password=sample-secret",
        "sk-syntheticsecret0123456789abcdef",
        "/Users/sample/file",
        "~/private",
        "nested/a..b/file",
        "person@example.invalid",
        "https://example.invalid/x",
        "192.0.2.1",
        "owner/repository#17",
        "資料\u202e",
        "資料\u200b",
        "資料\ue000",
        "資料\t",
        "資料\u0378",
        "資料\u2028",
        "資料\u2029",
        "資料\x1b",
        "資料\r",
    ],
)
def test_ev67_private_strings_are_rejected_in_every_field_and_comments(
    scenario_context, scenario_texts, value
):
    texts = scenario_texts(description=value)
    checks = inspect(scenario_context, texts)
    assert any(finding["category"] == "publish" for finding in checks["findings"])
    texts = scenario_texts()
    texts[0] += "# " + value + "\n"
    assert any(
        finding["category"] == "publish" for finding in inspect(scenario_context, texts)["findings"]
    )


def test_ev67_twenty_character_overlap_is_rejected_without_paraphrasing(
    scenario_context, scenario_texts
):
    evidence = "prefix 0123456789abcdefghij suffix"
    assert not inspect(
        scenario_context, scenario_texts(description="0123456789abcdefghij"), excerpts=[evidence]
    )["passed"]
    assert inspect(
        scenario_context, scenario_texts(description="0123456789a"), excerpts=[evidence]
    )["passed"]
    assert not inspect(scenario_context, scenario_texts(), masker=lambda _: "masked")["passed"]


@pytest.mark.parametrize(
    "variation",
    [
        "schema",
        "target",
        "id",
        "duplicate",
        "existing-id",
        "graded",
        "no-train",
        "no-holdout",
        "yaml",
        "unknown-property",
        "missing-oracle-field",
        "wrong-bool",
        "recursive",
    ],
)
def test_ev68_invalid_schema_target_ids_and_split_are_rejected(
    scenario_context, scenario_texts, variation
):
    documents = [yaml.safe_load(text) for text in scenario_texts()]
    if variation == "schema":
        documents[0]["schema_version"] = "2.0"
    elif variation == "target":
        documents[0]["target"] = "skill:other"
    elif variation == "id":
        documents[0]["id"] = "sevo-deadbeef-train"
    elif variation == "duplicate":
        documents[0]["id"] = documents[1]["id"]
    elif variation == "existing-id":
        (
            scenario_context.orchestra
            / "packages/meta-harness/scenarios/skill/review/collision.yaml"
        ).write_text(yaml.safe_dump(documents[0]))
    elif variation == "graded":
        documents[0]["graded"] = documents[0]["critical"]
    elif variation in {"no-train", "no-holdout"}:
        for document in documents:
            document["holdout"] = variation == "no-train"
    elif variation == "unknown-property":
        documents[0]["surprise"] = True
    elif variation == "missing-oracle-field":
        del documents[0]["critical"][0]["path"]
    elif variation == "wrong-bool":
        documents[0]["holdout"] = 1
    texts = [yaml.safe_dump(document) for document in documents]
    if variation == "yaml":
        texts[0] = "!!python/object:unsafe {}"
    if variation == "recursive":
        texts[0] = "&loop [*loop]"
    checks = inspect(scenario_context, texts)
    assert any(finding["category"] in {"schema", "structure"} for finding in checks["findings"])


def test_ev68_missing_train_requires_tools_to_be_absent(scenario_context, scenario_texts):
    suite = (
        scenario_context.orchestra / "packages/meta-harness/scenarios/skill/review/existing.yaml"
    )
    suite.unlink()
    assert inspect(scenario_context, scenario_texts(allowed_tools=[]))["passed"]
    assert inspect(scenario_context, scenario_texts(allowed_tools=["Edit", "Glob"]))["passed"]
    assert not inspect(scenario_context, scenario_texts(allowed_tools=["Bash"]))["passed"]


def test_ev68_schema_accepts_every_existing_meta_harness_skill_scenario(meta_evaluator):
    evaluator = meta_evaluator
    schema_dir = REPO / "packages/meta-harness/schemas"
    schema = json.loads((schema_dir / "scenario.schema.json").read_text())
    paths = sorted((REPO / "packages/meta-harness/scenarios/skill").glob("*/*.yaml"))
    assert paths
    for path in paths:
        evaluator.load_scenario(path, schema_dir)
        assert schema_errors(yaml.safe_load(path.read_text()), schema) == [], path


def test_ev68_schema_keyword_constraints_are_enforced():
    schema = json.loads((REPO / "packages/meta-harness/schemas/scenario.schema.json").read_text())
    assert schema_errors([], schema)
    budget = schema["properties"]["budget"]
    assert schema_errors({"max_output_tokens": 0, "unknown": 1}, budget)
    assert schema_errors(["bin", "bin"], schema["properties"]["path_prepend"])
    assert not setup_allowed("mkdir -p a..b", REPO)


@pytest.mark.parametrize(
    "line",
    [
        "touch .claude/settings.json",
        "mkdir -p .git/hooks/x",
        "touch packages/meta-harness/scenarios/fixtures/fake-gh.py",
        "cp packages/meta-harness/scenarios/fixtures/fake-gh.py packages/meta-harness/scenarios/fixtures/fake-gh.py",
        "touch .mcp.json",
        "touch CLAUDE.md",
        "touch AGENTS.md",
        "touch .meta-harness/.claude/settings.json",
        "mkdir -p .meta-harness/out/.git",
        "printf '\"value\"' > .meta-harness/result",
        "printf '{value}' > .meta-harness/result",
    ],
)
def test_ev66_setup_writes_are_confined_to_work_directory(scenario_context, scenario_texts, line):
    checks = inspect(scenario_context, scenario_texts(setup=[line]))
    assert any(f["category"] == "shape" and f["line"] == line for f in checks["findings"])


@pytest.mark.parametrize(
    "field,value",
    [
        ("repeat", 2),
        ("timeout_ms", 31),
        ("command_timeout_ms", 11),
        ("budget", {"max_budget_usd": 0.51}),
        ("budget", {"max_output_tokens": 101}),
    ],
)
def test_ev68_resource_limits_use_only_human_suite_maxima(
    scenario_context, scenario_texts, field, value
):
    suite = scenario_context.orchestra / "packages/meta-harness/scenarios/skill/review"
    baseline = yaml.safe_load((suite / "existing.yaml").read_text())
    baseline.update(
        repeat=1,
        timeout_ms=30,
        command_timeout_ms=10,
        budget={"max_budget_usd": 0.5, "max_output_tokens": 100},
    )
    (suite / "existing.yaml").write_text(yaml.safe_dump(baseline))
    injected = {
        **baseline,
        "id": "sevo-ffffffff-unsafe",
        "repeat": 100,
        "budget": {"max_budget_usd": 100, "max_output_tokens": 10000},
        "timeout_ms": 10000,
        "command_timeout_ms": 10000,
    }
    (suite / "sevo-injected.yaml").write_text(yaml.safe_dump(injected))
    assert any(
        f["category"] == "structure" and f["path"].startswith(f"$.{field}")
        for f in inspect(scenario_context, scenario_texts(**{field: value}))["findings"]
    )
    assert inspect(scenario_context, scenario_texts(repeat=1, budget={"max_budget_usd": 0.5}))[
        "passed"
    ]


def test_ev68_omitted_tools_and_resources_without_baseline_are_refused(
    scenario_context, scenario_texts
):
    documents = [yaml.safe_load(text) for text in scenario_texts()]
    for document in documents:
        del document["allowed_tools"]
    assert any(
        f["path"] == "$.allowed_tools"
        for f in inspect(scenario_context, [yaml.safe_dump(d) for d in documents])["findings"]
    )
    suite = scenario_context.orchestra / "packages/meta-harness/scenarios/skill/review"
    (suite / "existing.yaml").unlink()
    assert not inspect(scenario_context, scenario_texts(repeat=1))["passed"]


@pytest.mark.parametrize("field", ["path", "schema"])
def test_ev68_oracle_paths_reject_environment_expansion(scenario_context, scenario_texts, field):
    check = {
        "id": "json",
        "text": "人工検査",
        "oracle": "json_schema",
        "path": "out/result.json",
        "schema": "out/schema.json",
    }
    check[field] = "$HOME/.ssh/id_rsa"
    assert any(
        f["path"] == f"$.critical[0].{field}" and f["category"] == "structure"
        for f in inspect(scenario_context, scenario_texts(critical=[check]))["findings"]
    )


@pytest.mark.parametrize(
    "value",
    [
        "github.com/owner/repo/sub",
        "Users/x/private",
        "home/x/private",
        "ｈｔｔｐｓ：／／example.invalid/path",
        "agent.internal",
        "node.corp",
        "C:\\private\\file",
        "see:/etc/passwd",
        "owner/repo#21",
    ],
)
def test_ev67_normalized_publish_leaks_are_rejected(scenario_context, scenario_texts, value):
    assert any(
        f["category"] == "publish"
        for f in inspect(scenario_context, scenario_texts(prompt=value))["findings"]
    )


def test_ev67_relative_artifacts_are_publishable_and_overlap_is_normalized(
    scenario_context, scenario_texts
):
    assert inspect(scenario_context, scenario_texts(prompt="out/result.json"))["passed"]
    assert not inspect(
        scenario_context,
        scenario_texts(prompt="ＡＢＣＤＥＦ１２３４５６"),
        excerpts=["prefix abcdef123456 suffix"],
    )["passed"]


@pytest.mark.parametrize(
    "field,limit", [("prompt", 4000), ("rubric", 2000), ("text", 2000), ("description", 2000)]
)
def test_ev68_evaluation_text_is_listed_and_bounded(scenario_context, scenario_texts, field, limit):
    fields = (
        {field: "x" * (limit + 1)}
        if field in {"prompt", "description"}
        else {
            "critical": [
                {
                    "id": "judge",
                    "oracle": "rubric_judge",
                    "text": "人工検査",
                    "rubric": "人工判定",
                    field: "x" * (limit + 1),
                }
            ]
        }
    )
    checks = inspect(scenario_context, scenario_texts(**fields))
    assert any(line["path"].endswith(f".{field}") for line in checks["evaluation_texts"])
    assert any(
        f["category"] == "structure" and f["path"].endswith(f".{field}") for f in checks["findings"]
    )


@pytest.mark.parametrize(
    "text",
    [
        "a: &a [x]\nb: [*a, *a, *a]\n",
        "x: &anchor value\n",
        "[" * 51 + "0" + "]" * 51,
        "[" + ",".join("0" for _ in range(5001)) + "]",
    ],
)
def test_ev68_aliases_and_yaml_limits_are_rejected_within_one_second(text):
    import time

    from candidates_checks import load_yaml

    started = time.monotonic()
    with pytest.raises(ValueError):
        load_yaml(text)
    assert time.monotonic() - started < 1


def test_ev68_broken_existing_yaml_is_a_file_finding(scenario_context, scenario_texts):
    file = scenario_context.orchestra / "packages/meta-harness/scenarios/broken.yaml"
    file.write_text("[broken")
    checks = inspect(scenario_context, scenario_texts())
    assert checks["passed"] is True
    assert checks["findings"] == []
    assert any(f["file"].endswith("scenarios/broken.yaml") for f in checks["warnings"])


def test_ev66_sevo_scenarios_cannot_expand_fixture_or_copy_allowlists(
    scenario_context, scenario_texts
):
    root = scenario_context.orchestra
    suite = root / "packages/meta-harness/scenarios/skill/review"
    fixture = "packages/meta-harness/scenarios/fixtures/fake-gh.py"
    document = yaml.safe_load(scenario_texts()[0])
    document.update(
        setup=[f"cp {fixture} .claude/settings.json"],
        critical=[
            {
                "id": "run",
                "text": "人工検査",
                "oracle": "command_exit",
                "command": "python3 packages/meta-harness/scenarios/fixtures/assert-cli-skill-route.py --new-flag x",
            }
        ],
        id="sevo-expanded-sample",
        graded=[
            {"id": "extra", "text": "人工検査", "oracle": "artifact_exists", "path": "result.json"}
        ],
    )
    (suite / "sevo-expanded.yaml").write_text(yaml.safe_dump(document))
    assert "--new-flag" not in fixture_allowlist(root)["assert-cli-skill-route.py"]
    assert not setup_allowed(f"cp {fixture} .claude/settings.json", root)
    assert setup_allowed(
        "cp packages/meta-harness/scenarios/fixtures/fake-gh-issue-view.py bin/gh", root
    )
    assert inspect(scenario_context, scenario_texts())["passed"]
