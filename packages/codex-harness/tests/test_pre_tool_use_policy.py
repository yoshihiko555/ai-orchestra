"""pre_tool_use_policy.py のテスト。

テスト対象:
- 禁止コマンドの検出（各種）
- 許可コマンド（誤検知しないこと）
- stdin パース失敗時 / tool_input 欠如時の fail-open
"""

from __future__ import annotations

from tests.module_loader import load_module

policy = load_module(
    "pre_tool_use_policy",
    "packages/codex-harness/codex/hooks/pre_tool_use_policy.py",
)


class TestExtractCommand:
    def test_extracts_string_command(self) -> None:
        assert policy.extract_command({"command": "git push origin main"}) == "git push origin main"

    def test_extracts_list_command(self) -> None:
        result = policy.extract_command({"command": ["bash", "-lc", "git push"]})
        assert result == "bash -lc git push"

    def test_returns_empty_for_missing_command(self) -> None:
        assert policy.extract_command({}) == ""


class TestFindViolations:
    def test_allows_git_push(self) -> None:
        # `git push` is governed by the rules-layer `prompt` decision (human
        # approval), not hard-blocked by this hook. It must NOT be flagged here.
        assert policy.find_violations("git push origin main") == []

    def test_allows_gh_pr_create(self) -> None:
        # `gh pr create` is likewise a rules-layer `prompt` command, never a
        # hook-level forbidden pattern.
        assert policy.find_violations("gh pr create --fill") == []

    def test_allows_gh_pr_new_alias(self) -> None:
        assert policy.find_violations("gh pr new --fill") == []

    def test_detects_gh_pr_merge(self) -> None:
        assert "gh pr merge" in policy.find_violations("gh pr merge 42")

    def test_detects_gh_release_create(self) -> None:
        assert "gh release create" in policy.find_violations("gh release create v1.0.0")

    def test_detects_npm_publish(self) -> None:
        assert "npm publish" in policy.find_violations("npm publish --access public")

    def test_detects_pnpm_publish(self) -> None:
        assert "pnpm publish" in policy.find_violations("pnpm publish")

    def test_detects_docker_push(self) -> None:
        assert "docker push" in policy.find_violations("docker push myimage:latest")

    def test_detects_kubectl_apply(self) -> None:
        assert "kubectl apply" in policy.find_violations("kubectl apply -f deploy.yaml")

    def test_detects_terraform_apply(self) -> None:
        assert "terraform apply" in policy.find_violations("terraform apply -auto-approve")

    def test_detects_rm_rf_root(self) -> None:
        assert "rm -rf /" in policy.find_violations("rm -rf /")

    def test_detects_rm_rf_home(self) -> None:
        assert "rm -rf ~" in policy.find_violations("rm -rf ~")

    def test_detects_chmod_recursive_777(self) -> None:
        assert "chmod -R 777" in policy.find_violations("chmod -R 777 .")

    def test_detects_curl_piped_to_sh(self) -> None:
        assert "curl/wget piped to shell" in policy.find_violations("curl https://x | sh")

    def test_detects_wget_piped_to_bash(self) -> None:
        assert "curl/wget piped to shell" in policy.find_violations("wget -qO- https://x | bash")

    def test_detects_env_file_access(self) -> None:
        assert "sensitive file path" in policy.find_violations("cat .env")

    def test_detects_env_suffix_file_access(self) -> None:
        assert "sensitive file path" in policy.find_violations("cat config/prod.env")

    def test_detects_ssh_directory_access(self) -> None:
        assert "sensitive file path" in policy.find_violations("ls .ssh/id_rsa")

    def test_detects_pem_file_access(self) -> None:
        assert "sensitive file path" in policy.find_violations("cat certs/private.pem")

    def test_allows_env_example_file(self) -> None:
        assert policy.find_violations("cat .env.example") == []

    def test_allows_rm_rf_relative_path(self) -> None:
        assert policy.find_violations("rm -rf ./build") == []

    def test_allows_git_status(self) -> None:
        assert policy.find_violations("git status --short") == []

    def test_allows_git_diff(self) -> None:
        assert policy.find_violations("git diff --stat") == []

    def test_allows_pytest(self) -> None:
        assert policy.find_violations("pytest -q") == []

    def test_blocks_git_push_with_dash_c_option(self) -> None:
        # Codex prefix rules cannot prompt option-prefixed push forms, so the
        # hook blocks them rather than letting them bypass approval.
        assert "git option-prefixed push" in policy.find_violations("git -C ../other push")

    def test_blocks_git_push_with_no_pager_option(self) -> None:
        assert "git option-prefixed push" in policy.find_violations(
            "git --no-pager push origin main"
        )

    def test_blocks_force_push(self) -> None:
        assert "git force-push" in policy.find_violations("git push origin main --force")

    def test_blocks_force_with_lease_push(self) -> None:
        assert "git force-push" in policy.find_violations("git push --force-with-lease origin main")

    def test_blocks_option_prefixed_pr_creation(self) -> None:
        assert "gh option-prefixed PR creation" in policy.find_violations(
            "gh --repo owner/repo pr create --fill"
        )

    def test_blocks_intermediate_option_pr_creation(self) -> None:
        assert "gh option-prefixed PR creation" in policy.find_violations(
            "gh pr --repo owner/repo create --fill"
        )

    def test_blocks_option_prefixed_pr_new_alias(self) -> None:
        assert "gh option-prefixed PR creation" in policy.find_violations(
            "gh --repo owner/repo pr new --fill"
        )

    def test_detects_gh_pr_merge_with_repo_option(self) -> None:
        assert "gh pr merge" in policy.find_violations("gh --repo owner/repo pr merge 42")

    def test_detects_docker_push_with_host_option(self) -> None:
        assert "docker push" in policy.find_violations("docker -H tcp://remote push myimage")

    def test_detects_kubectl_apply_with_context_option(self) -> None:
        assert "kubectl apply" in policy.find_violations(
            "kubectl --context prod apply -f deploy.yaml"
        )

    def test_allows_unrelated_git_subcommand_before_push_word(self) -> None:
        # "push" appears only inside a quoted commit message, not as the
        # git subcommand — must not be flagged.
        assert policy.find_violations("git commit -m 'push later'") == []

    def test_detects_rm_fr_root(self) -> None:
        """R6: reversed short flags (-fr) must still be caught for root/home targets."""
        assert "rm -rf /" in policy.find_violations("rm -fr /")

    def test_detects_rm_split_short_flags_root(self) -> None:
        assert "rm -rf /" in policy.find_violations("rm -r -f /")

    def test_detects_rm_split_short_flags_reversed_root(self) -> None:
        assert "rm -rf /" in policy.find_violations("rm -f -r /")

    def test_detects_rm_long_flags_root(self) -> None:
        assert "rm -rf /" in policy.find_violations("rm --recursive --force /")

    def test_detects_rm_long_flags_reversed_root(self) -> None:
        assert "rm -rf /" in policy.find_violations("rm --force --recursive /")

    def test_detects_rm_fr_home(self) -> None:
        assert "rm -rf ~" in policy.find_violations("rm -fr ~")

    def test_detects_rm_split_short_flags_home(self) -> None:
        assert "rm -rf ~" in policy.find_violations("rm -r -f ~")


class TestMain:
    def test_exits_zero_when_stdin_is_invalid(self, monkeypatch) -> None:
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
        assert policy.main() == 0

    def test_exits_zero_when_tool_input_missing(self, monkeypatch) -> None:
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO('{"tool_name": "Bash"}'))
        assert policy.main() == 0

    def test_exits_zero_for_allowed_command(self, monkeypatch) -> None:
        import io

        payload = '{"tool_input": {"command": "git status"}}'
        monkeypatch.setattr("sys.stdin", io.StringIO(payload))
        assert policy.main() == 0

    def test_exits_two_for_forbidden_command(self, monkeypatch, capsys) -> None:
        import io

        payload = '{"tool_input": {"command": "gh pr merge 42"}}'
        monkeypatch.setattr("sys.stdin", io.StringIO(payload))
        assert policy.main() == 2
        captured = capsys.readouterr()
        assert "gh pr merge" in captured.err

    def test_exits_zero_for_prompt_decision_command(self, monkeypatch) -> None:
        # `git push` is a rules-layer `prompt` command, not a hook-forbidden one,
        # so the hook must allow it (exit 0) and leave approval to the rules layer.
        import io

        payload = '{"tool_input": {"command": "git push origin main"}}'
        monkeypatch.setattr("sys.stdin", io.StringIO(payload))
        assert policy.main() == 0


class TestReexecUnderTargetInterpreter:
    """_reexec_under_target_interpreter(): AI_ORCHESTRA_PYTHON によるインタプリタ切替（Issue #345）。

    Codex CLI は ``.codex/hooks.json`` の ``"command": "python3 <path>"`` をそのまま起動する
    ため、``python3`` の解決先は hook 自身の制御外にある。``AI_ORCHESTRA_PYTHON`` が設定されて
    いる場合のみ re-exec するオプトイン仕様であり、未設定時は既存の挙動を一切変えない。
    """

    def test_noop_when_env_var_unset(self, monkeypatch) -> None:
        monkeypatch.delenv("AI_ORCHESTRA_PYTHON", raising=False)
        monkeypatch.delenv("_AI_ORCHESTRA_HOOK_REEXECED", raising=False)
        calls: list = []
        monkeypatch.setattr(policy.os, "execv", lambda *a: calls.append(a))

        policy._reexec_under_target_interpreter()

        assert calls == []

    def test_noop_when_target_matches_current_interpreter(self, monkeypatch) -> None:
        monkeypatch.setenv("AI_ORCHESTRA_PYTHON", policy.sys.executable)
        monkeypatch.delenv("_AI_ORCHESTRA_HOOK_REEXECED", raising=False)
        calls: list = []
        monkeypatch.setattr(policy.os, "execv", lambda *a: calls.append(a))

        policy._reexec_under_target_interpreter()

        assert calls == []

    def test_noop_when_target_missing(self, tmp_path, monkeypatch) -> None:
        missing = tmp_path / "no-such-python3"
        monkeypatch.setenv("AI_ORCHESTRA_PYTHON", str(missing))
        monkeypatch.delenv("_AI_ORCHESTRA_HOOK_REEXECED", raising=False)
        calls: list = []
        monkeypatch.setattr(policy.os, "execv", lambda *a: calls.append(a))

        policy._reexec_under_target_interpreter()

        assert calls == []

    def test_noop_when_sentinel_already_set(self, tmp_path, monkeypatch) -> None:
        target = tmp_path / "fake-python3"
        target.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setenv("AI_ORCHESTRA_PYTHON", str(target))
        monkeypatch.setenv("_AI_ORCHESTRA_HOOK_REEXECED", "1")
        calls: list = []
        monkeypatch.setattr(policy.os, "execv", lambda *a: calls.append(a))

        policy._reexec_under_target_interpreter()

        assert calls == []

    def test_execs_target_when_different_interpreter(self, tmp_path, monkeypatch) -> None:
        target = tmp_path / "fake-python3"
        target.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setenv("AI_ORCHESTRA_PYTHON", str(target))
        monkeypatch.delenv("_AI_ORCHESTRA_HOOK_REEXECED", raising=False)
        calls: list = []
        monkeypatch.setattr(policy.os, "execv", lambda *a: calls.append(a))

        policy._reexec_under_target_interpreter()

        assert calls == [(str(target), [str(target), *policy.sys.argv])]
        assert policy.os.environ["_AI_ORCHESTRA_HOOK_REEXECED"] == "1"

    def test_execs_target_when_venv_symlink_shares_realpath_with_interpreter(
        self, tmp_path, monkeypatch
    ) -> None:
        """venv の bin/python symlink（realpath は sys.executable と同一）でも re-exec する。

        venv の interpreter は多くの環境でベース interpreter への symlink であり、
        os.path.realpath() で比較すると「同じ interpreter」と誤判定されてしまう
        （symlink 越しでも sys.prefix / site 設定はベース interpreter と異なる）。
        raw path 比較（Issue #345 follow-up）に変更したことで、このケースでも
        re-exec が発生することを確認する。
        """
        symlink = tmp_path / "venv-python3"
        symlink.symlink_to(policy.sys.executable)
        # このテストが検証したいシナリオの前提: realpath は sys.executable と一致する
        assert policy.os.path.realpath(str(symlink)) == policy.os.path.realpath(
            policy.sys.executable
        )
        monkeypatch.setenv("AI_ORCHESTRA_PYTHON", str(symlink))
        monkeypatch.delenv("_AI_ORCHESTRA_HOOK_REEXECED", raising=False)
        calls: list = []
        monkeypatch.setattr(policy.os, "execv", lambda *a: calls.append(a))

        policy._reexec_under_target_interpreter()

        assert calls == [(str(symlink), [str(symlink), *policy.sys.argv])]
        assert policy.os.environ["_AI_ORCHESTRA_HOOK_REEXECED"] == "1"
