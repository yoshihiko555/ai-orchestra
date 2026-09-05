"""facets/scripts/explain-visually/verify_page.py（explain-visually skill の検証スクリプト）の
単体テスト。

Chrome を実際に起動しないテストのみを対象とする（`dump_dom` / `screenshot` は Chrome プロセス起動を
伴うため対象外。`run_chrome` は `subprocess.Popen` を monkeypatch し、タイムアウト時の致命的化のみ
決定的に検証する。CI・sandbox 環境で決定的に実行できる範囲に限定する）。

対応 EV（docs/evaluation/core.md）:
- EV-30（must）: `resolve_chrome_path` の解決順序（明示指定 → 環境変数 → macOS 既定パス → PATH）。
  macOS 既定パスの存在判定は `exists: Callable[[str], bool]` の DI で決定的にテストする
- EV-31（must）: `main` の三段階 exit code（EXIT_OK / EXIT_WARNINGS / EXIT_FATAL）。存在しない
  Chrome バイナリでの起動失敗と、タイムアウト時に出力が空の場合の致命的化を含む
- EV-32（must）: `parse_dom_metrics` による DOM 解析（描画済み/未描画の図・ready フラグ・高さ・title）。
  未描画検出はタグ非依存（`<pre>`/`<div>` 等の `class="mermaid"`）で `mermaid-box` とは区別する
- EV-33（must）: `build_warnings` による警告生成（sources/rendered 不一致・未 ready・高さ欠落）
- EV-34（should）: template.html ↔ verify_page.py の契約（fig- id・data-* フラグ・プレースホルダ）
- EV-35（must）: 生成 HTML の注入可能な markup・未置換 placeholder と、テンプレート由来の
  CSP・script 本文の改変検出
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.module_loader import REPO_ROOT, load_module

SCRIPT_PATH = REPO_ROOT / "facets" / "scripts" / "explain-visually" / "verify_page.py"
TEMPLATE_PATH = REPO_ROOT / "facets" / "scripts" / "explain-visually" / "template.html"

verify_page = load_module(
    "explain_visually_verify_page", "facets/scripts/explain-visually/verify_page.py"
)


def _run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        capture_output=True,
        text=True,
        cwd=cwd,
    )


def _render_template(title: str = "Example", body: str = "<main>body</main>") -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    return template.replace("{{TITLE}}", title).replace("{{BODY}}", body)


def _options_for(
    html: Path,
    *,
    dom_file: Path | None = None,
    skip_screenshot: bool = True,
    chrome: str | None = None,
) -> verify_page.CliOptions:
    return verify_page.CliOptions(
        html=html,
        width=verify_page.DEFAULT_VIEWPORT_WIDTH,
        wait=verify_page.DEFAULT_VIRTUAL_TIME_BUDGET_MS,
        timeout=verify_page.DEFAULT_CHROME_TIMEOUT_SECONDS,
        dom_file=dom_file,
        skip_screenshot=skip_screenshot,
        chrome=chrome,
    )


class _FakeChromeProcess:
    """`subprocess.Popen` の代役。Popen と同じ引数で呼ばれ、渡された stdout / stderr の
    ファイルへ出力を書き込んでから自身を返す（run_chrome はパイプではなくファイル経由で
    出力を回収するため）。"""

    pid = 12345

    def __init__(
        self,
        output: str = "",
        stderr: str = "",
        returncode: int | None = None,
        timeout_calls: int = 0,
    ) -> None:
        self.output = output
        self.stderr = stderr
        self.returncode = returncode
        self.timeout_calls = timeout_calls
        self.call_count = 0

    def __call__(self, *args: object, **kwargs: object) -> _FakeChromeProcess:
        for key, text in (("stdout", self.output), ("stderr", self.stderr)):
            handle = kwargs.get(key)
            if handle is not None and hasattr(handle, "write"):
                handle.write(text.encode("utf-8"))
                handle.flush()
        return self

    def wait(self, timeout: float | None = None) -> int | None:
        self.call_count += 1
        if self.call_count <= self.timeout_calls:
            raise subprocess.TimeoutExpired(cmd="chrome", timeout=timeout)
        return self.returncode


# ---------------------------------------------------------------------------
# EV-30: resolve_chrome_path
# ---------------------------------------------------------------------------


class TestResolveChromePath:
    """`resolve_chrome_path` の解決順序: 明示指定 → 環境変数 → macOS 既定パス → PATH → None。"""

    def test_explicit_argument_wins_over_env_and_which(self) -> None:
        result = verify_page.resolve_chrome_path(
            "/explicit/chrome",
            {verify_page.CHROME_ENVIRONMENT_VARIABLE: "/env/chrome"},
            which=lambda _name: "/path/chrome",
        )
        assert result == "/explicit/chrome"

    def test_env_var_used_when_no_explicit_argument(self) -> None:
        result = verify_page.resolve_chrome_path(
            None,
            {verify_page.CHROME_ENVIRONMENT_VARIABLE: "/env/chrome"},
            which=lambda _name: None,
            exists=lambda _path: False,
        )

        assert result == "/env/chrome"

    def test_macos_default_path_used_when_no_explicit_or_env(self) -> None:
        result = verify_page.resolve_chrome_path(
            None, {}, which=lambda _name: None, exists=lambda _path: True
        )

        assert result == verify_page.MACOS_DEFAULT_CHROME_PATH

    def test_which_fallback_when_macos_default_path_missing(self) -> None:
        result = verify_page.resolve_chrome_path(
            None,
            {},
            which=lambda name: "/usr/bin/chromium" if name == "chromium" else None,
            exists=lambda _path: False,
        )

        assert result == "/usr/bin/chromium"

    def test_which_is_probed_in_candidate_priority_order(self) -> None:
        probed: list[str] = []

        def fake_which(name: str) -> str | None:
            probed.append(name)
            return "/usr/bin/google-chrome-stable" if name == "google-chrome-stable" else None

        result = verify_page.resolve_chrome_path(
            None, {}, which=fake_which, exists=lambda _path: False
        )

        assert result == "/usr/bin/google-chrome-stable"
        assert probed == ["google-chrome", "google-chrome-stable"]

    def test_returns_none_when_nothing_resolves(self) -> None:
        result = verify_page.resolve_chrome_path(
            None, {}, which=lambda _name: None, exists=lambda _path: False
        )

        assert result is None


# ---------------------------------------------------------------------------
# EV-32: parse_dom_metrics
# ---------------------------------------------------------------------------


class TestParseDomMetrics:
    """描画済み svg 数・未描画 mermaid 要素数・ready フラグ・page_height・title の抽出。"""

    def test_extracts_rendered_svgs_ready_flag_height_and_title(self) -> None:
        dom = (
            "<html><head><title>Example Page</title></head>"
            '<body data-mermaid-ready="1" data-page-height="1234">'
            '<div class="mermaid"><svg id="fig-0">a</svg></div>'
            '<div class="mermaid"><svg id="fig-1">b</svg></div>'
            "</body></html>"
        )

        metrics = verify_page.parse_dom_metrics(dom)

        assert metrics.rendered == 2
        assert metrics.unrendered == 0
        assert metrics.sources == 2
        assert metrics.ready is True
        assert metrics.page_height == 1234
        assert metrics.title == "Example Page"

    def test_body_attributes_override_attribute_like_title_text(self) -> None:
        dom = (
            '<html><head><title>Example data-page-height="1" Page</title></head>'
            '<body data-page-height="1234" data-mermaid-ready="1">'
            '<div class="mermaid"><svg id="fig-0">a</svg></div>'
            "</body></html>"
        )

        metrics = verify_page.parse_dom_metrics(dom)

        assert metrics.page_height == 1234
        assert metrics.ready is True
        assert metrics.title == 'Example data-page-height="1" Page'

    def test_counts_unrendered_pre_mermaid_blocks_as_sources(self) -> None:
        dom = '<body data-page-height="500"><pre class="mermaid">graph TD; a --> b</pre></body>'

        metrics = verify_page.parse_dom_metrics(dom)

        assert metrics.rendered == 0
        assert metrics.unrendered == 1
        assert metrics.sources == 1
        assert metrics.ready is False

    def test_counts_unrendered_div_mermaid_blocks_as_sources(self) -> None:
        # template.html は querySelectorAll('.mermaid') でタグ非依存に描画対象を探すため、
        # <div class="mermaid"> の未描画も <pre class="mermaid"> と同様に検出できる必要がある。
        dom = '<body data-page-height="500"><div class="mermaid">graph TD; a --> b</div></body>'

        metrics = verify_page.parse_dom_metrics(dom)

        assert metrics.rendered == 0
        assert metrics.unrendered == 1
        assert metrics.sources == 1
        assert metrics.ready is False

    def test_counts_unrendered_mermaid_starting_with_percent_comment(self) -> None:
        # %%{init: ...}%% ディレクティブから始まる Mermaid ソースも未描画として数えられる必要がある
        dom = (
            '<body data-page-height="500">'
            '<pre class="mermaid">%%{init: {"theme": "base"}}%%\ngraph TD; a --> b</pre>'
            "</body>"
        )

        metrics = verify_page.parse_dom_metrics(dom)

        assert metrics.rendered == 0
        assert metrics.unrendered == 1
        assert metrics.sources == 1

    def test_counts_unrendered_mermaid_starting_with_frontmatter_dashes(self) -> None:
        # --- フロントマターから始まる Mermaid ソースも未描画として数えられる必要がある
        dom = (
            '<body data-page-height="500">'
            '<div class="mermaid">---\ntitle: x\n---\ngraph TD; a --> b</div>'
            "</body>"
        )

        metrics = verify_page.parse_dom_metrics(dom)

        assert metrics.rendered == 0
        assert metrics.unrendered == 1
        assert metrics.sources == 1

    def test_mermaid_box_wrapper_is_not_counted_as_unrendered_source(self) -> None:
        # `.mermaid-box` は外枠のコンテナクラスであり、`.mermaid` 本体とは区別する必要がある。
        dom = (
            '<body data-page-height="500">'
            '<div class="mermaid-box"><div class="mermaid"><svg id="fig-0">x</svg></div></div>'
            "</body>"
        )

        metrics = verify_page.parse_dom_metrics(dom)

        assert metrics.rendered == 1
        assert metrics.unrendered == 0
        assert metrics.sources == 1

    def test_empty_dom_returns_zeroed_defaults(self) -> None:
        metrics = verify_page.parse_dom_metrics("")

        assert metrics == verify_page.DomMetrics(
            rendered=0,
            unrendered=0,
            sources=0,
            ready=False,
            page_height=0,
            title="",
        )


# ---------------------------------------------------------------------------
# EV-33: build_warnings
# ---------------------------------------------------------------------------


class TestBuildWarnings:
    """sources と rendered の一致有無・ready フラグ・page_height 欠落による警告生成。"""

    def test_no_warnings_when_fully_rendered_ready_and_measured(self) -> None:
        metrics = verify_page.DomMetrics(
            rendered=2, unrendered=0, sources=2, ready=True, page_height=1000, title="t"
        )

        assert verify_page.build_warnings(metrics) == []

    def test_warns_when_sources_present_but_not_ready(self) -> None:
        metrics = verify_page.DomMetrics(
            rendered=0, unrendered=1, sources=1, ready=False, page_height=1000, title="t"
        )

        warnings = verify_page.build_warnings(metrics)

        assert any("描画完了フラグ" in warning for warning in warnings)

    def test_warns_when_rendered_count_mismatches_sources(self) -> None:
        metrics = verify_page.DomMetrics(
            rendered=1, unrendered=1, sources=2, ready=True, page_height=1000, title="t"
        )

        warnings = verify_page.build_warnings(metrics)

        assert any("記法エラーか id の衝突" in warning for warning in warnings)

    def test_warns_when_page_height_missing(self) -> None:
        metrics = verify_page.DomMetrics(
            rendered=1, unrendered=0, sources=1, ready=True, page_height=0, title="t"
        )

        warnings = verify_page.build_warnings(metrics)

        assert any("ページ高さを取得できなかった" in warning for warning in warnings)

    def test_no_mermaid_sources_and_no_height_warns_only_about_height(self) -> None:
        metrics = verify_page.DomMetrics(
            rendered=0, unrendered=0, sources=0, ready=False, page_height=0, title="t"
        )

        warnings = verify_page.build_warnings(metrics)

        assert len(warnings) == 1
        assert "ページ高さを取得できなかった" in warnings[0]


# ---------------------------------------------------------------------------
# EV-35: lint_injected_markup
# ---------------------------------------------------------------------------


class TestLintInjectedMarkup:
    """生成 HTML 本文への危険な markup と未置換 placeholder の検出。"""

    def _html_with_template_scripts(self, extra_body: str = "") -> str:
        return (
            "<html><head>"
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'">'
            "</head><body>"
            "<script>const a = 1;</script>"
            '<script type="module">const b = 2;</script>'
            f"{extra_body}"
            "</body></html>"
        )

    def test_no_injection_returns_no_warnings(self) -> None:
        html = self._html_with_template_scripts()

        assert verify_page.lint_injected_markup(html) == []

    def test_extra_script_tags_warn(self) -> None:
        html = self._html_with_template_scripts("<script>alert(1)</script>")

        warnings = verify_page.lint_injected_markup(html)

        assert any("<script>" in warning for warning in warnings)

    def test_event_handler_attribute_warns(self) -> None:
        html = self._html_with_template_scripts('<img src="x" onerror="alert(1)">')

        warnings = verify_page.lint_injected_markup(html)

        assert any("イベントハンドラ" in warning for warning in warnings)

    def test_javascript_scheme_warns(self) -> None:
        html = self._html_with_template_scripts('<a href="javascript:alert(1)">x</a>')

        warnings = verify_page.lint_injected_markup(html)

        assert any("javascript:" in warning for warning in warnings)

    def test_escaped_quotation_and_code_snippet_do_not_warn(self) -> None:
        """エスケープ済みの原文引用・コード片（タグ文脈外）は誤検知しない。"""
        html = self._html_with_template_scripts(
            '<pre class="code">element.onclick = fn; location.href = "javascript:void(0)";</pre>'
            "<p>&lt;img src=x onerror=alert(1)&gt; と &lt;a href=&quot;javascript:x&quot;&gt;</p>"
        )

        assert verify_page.lint_injected_markup(html) == []

    def test_missing_csp_meta_warns(self) -> None:
        html = "<html><body>hello</body></html>"

        warnings = verify_page.lint_injected_markup(html)

        assert any("CSP" in warning for warning in warnings)

    def test_csp_hidden_in_comment_is_not_recognized(self) -> None:
        html = (
            "<html><head>"
            '<!-- <meta http-equiv="Content-Security-Policy" '
            "content=\"default-src 'none'\"> -->"
            "</head><body>"
            "<script>const a = 1;</script>"
            '<script type="module">const b = 2;</script>'
            "</body></html>"
        )

        warnings = verify_page.lint_injected_markup(html)

        assert any("CSP" in warning for warning in warnings)

    @pytest.mark.parametrize(
        "http_equiv",
        ["re&#102;resh", "&#x72;efresh"],
        ids=["decimal-entity", "hex-entity"],
    )
    def test_entity_obfuscated_meta_refresh_warns(self, http_equiv: str) -> None:
        markup = f'<meta http-equiv="{http_equiv}" content="0; url=evil">'

        warnings = verify_page.lint_injected_markup(self._html_with_template_scripts(markup))

        assert any("meta refresh" in warning for warning in warnings)

    @pytest.mark.parametrize(
        ("markup", "expected"),
        [
            ('<meta http-equiv = "Refresh" content="0; url=x">', "meta refresh"),
            ('<base href="https://example.com/">', "<base>"),
            ('<iframe src="https://example.com/"></iframe>', "<iframe>"),
            ('<object data="https://example.com/"></object>', "<object>"),
            ('<embed src="https://example.com/">', "<embed>"),
            ('<form action="https://example.com/"></form>', "<form>"),
            ('<link rel="stylesheet" href="https://attacker.example/x.css">', "<link>"),
        ],
        ids=["meta-refresh", "base", "iframe", "object", "embed", "form", "link"],
    )
    def test_navigation_or_embedding_markup_warns(self, markup: str, expected: str) -> None:
        warnings = verify_page.lint_injected_markup(self._html_with_template_scripts(markup))
        assert any(expected in warning for warning in warnings)

    @pytest.mark.parametrize("placeholder", ["{{TITLE}}", "{{BODY}}"], ids=["title", "body"])
    def test_unreplaced_placeholder_warns(self, placeholder: str) -> None:
        warnings = verify_page.lint_injected_markup(self._html_with_template_scripts(placeholder))
        assert any(placeholder in warning for warning in warnings)

    def test_placeholder_inside_pre_is_not_flagged(self) -> None:
        html = self._html_with_template_scripts('<pre class="code">{{BODY}}</pre>')

        assert verify_page.lint_injected_markup(html) == []

    def test_unreplaced_title_placeholder_still_flagged(self) -> None:
        html = self._html_with_template_scripts("{{BODY}}").replace(
            "</head>", "<title>{{TITLE}}</title></head>"
        )

        warnings = verify_page.lint_injected_markup(html)

        assert all(
            any(placeholder in warning for warning in warnings)
            for placeholder in ("{{TITLE}}", "{{BODY}}")
        )


class TestLintTemplateIntegrity:
    """生成 HTML の CSP 値と script 本文を実テンプレートに照合する。"""

    def test_relaxed_csp_warns_while_actual_template_matches(self) -> None:
        template = TEMPLATE_PATH.read_text(encoding="utf-8")
        generated = _render_template()
        assert verify_page.lint_template_integrity(generated, template) == []
        relaxed = generated.replace("form-action 'none'", "form-action *", 1)
        warnings = verify_page.lint_template_integrity(relaxed, template)
        assert any("CSP meta の内容" in warning for warning in warnings)

    @pytest.mark.parametrize(
        ("original", "replacement"),
        [
            ("const reportHeight", "const changedReportHeight"),
            ("</body>", "<script>bad()</script></body>"),
        ],
        ids=["modified-body", "extra-script"],
    )
    def test_script_content_outside_template_prefix_warns(
        self, original: str, replacement: str
    ) -> None:
        template = TEMPLATE_PATH.read_text(encoding="utf-8")
        generated = _render_template().replace(original, replacement, 1)
        warnings = verify_page.lint_template_integrity(generated, template)
        assert any("<script> 本文" in warning for warning in warnings)

    def test_actual_template_with_substituted_placeholders_matches(self) -> None:
        template = TEMPLATE_PATH.read_text(encoding="utf-8")
        assert verify_page.lint_template_integrity(_render_template(), template) == []

    def test_script_src_attribute_added_to_unchanged_body_warns(self) -> None:
        template = TEMPLATE_PATH.read_text(encoding="utf-8")
        generated = _render_template().replace(
            "<script>",
            '<script src="https://cdn.jsdelivr.net/gh/attacker/repo/payload.js">',
            1,
        )

        assert verify_page.lint_template_integrity(generated, template)

    def test_mermaid_script_removal_keeps_valid_script_prefix(self) -> None:
        template = TEMPLATE_PATH.read_text(encoding="utf-8")
        generated = _render_template()
        mermaid_script = re.search(r'<script type="module">.*?</script>', generated, re.S)
        assert mermaid_script is not None
        without_mermaid = generated.replace(mermaid_script.group(0), "", 1)
        assert verify_page.lint_template_integrity(without_mermaid, template) == []

    def test_template_load_failure_is_fatal_json_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.chdir(tmp_path)
        html_file = tmp_path / "page.html"
        html_file.write_text(_render_template(), encoding="utf-8")
        monkeypatch.setattr(verify_page, "__file__", str(tmp_path / "missing" / "verify_page.py"))
        exit_code = verify_page.main([str(html_file), "--skip-screenshot"])
        payload = json.loads(capsys.readouterr().out)
        assert exit_code == verify_page.EXIT_FATAL
        assert payload["ok"] is False
        assert "error" in payload


class TestMarkupWarningsBlockChromeLaunch:
    """安全性警告がある HTML は Chrome 起動前に拒否する（fail-closed ゲート）。"""

    def test_injected_script_blocks_chrome_launch_in_process(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        html_file = tmp_path / "page.html"
        html_file.write_text(
            "<html><body>"
            "<script>alert(1)</script><script>alert(2)</script><script>alert(3)</script>"
            "</body></html>",
            encoding="utf-8",
        )

        def _fail_popen(*args: object, **kwargs: object) -> None:
            raise AssertionError("Chrome must not be launched when markup warnings exist")

        monkeypatch.setattr(verify_page.subprocess, "Popen", _fail_popen)

        result = verify_page.verify_page(_options_for(html_file))

        assert result["ok"] is False
        assert result["screenshot"] is None
        assert result["mermaidRendered"] == 0
        assert result["warnings"]

    def test_injected_markup_exits_with_warnings_via_main_without_touching_chrome(
        self, tmp_path: Path
    ) -> None:
        html_file = tmp_path / "page.html"
        html_file.write_text(
            "<html><body>"
            "<script>a()</script><script>b()</script><script>c()</script>"
            "</body></html>",
            encoding="utf-8",
        )

        proc = _run(
            [str(html_file), "--chrome", "/nonexistent/binary", "--skip-screenshot"],
            cwd=tmp_path,
        )

        assert proc.returncode == verify_page.EXIT_WARNINGS, proc.stderr
        payload = json.loads(proc.stdout)
        assert payload["ok"] is False
        assert payload["mermaidRendered"] == 0
        assert payload["screenshot"] is None


class TestSuppliedDomSkipsScreenshot:
    """Supplied DOM metrics and screenshots never come from different renders."""

    def test_dom_file_skips_screenshot_without_explicit_skip_flag(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        html_file = tmp_path / "page.html"
        html_file.write_text(_render_template(), encoding="utf-8")
        dom_file = tmp_path / "dom.html"
        dom_file.write_text(
            '<body data-mermaid-ready="1" data-page-height="900">'
            '<div class="mermaid"><svg id="fig-0">x</svg></div></body>',
            encoding="utf-8",
        )

        def _fail_popen(*args: object, **kwargs: object) -> None:
            raise AssertionError("Chrome must not be launched")

        monkeypatch.setattr(verify_page.subprocess, "Popen", _fail_popen)

        result = verify_page.verify_page(
            _options_for(html_file, dom_file=dom_file, skip_screenshot=False)
        )

        assert result["ok"] is True
        assert result["screenshot"] is None


class TestRenderScreenshotStaleFile:
    """古いスクリーンショットが残っていても、今回の撮影失敗を隠さない。"""

    def test_stale_screenshot_file_does_not_count_as_success(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        html_file = tmp_path / "page.html"
        html_file.write_text("<html></html>", encoding="utf-8")
        stale_output = tmp_path / "page-shot.png"
        stale_output.write_bytes(b"stale")

        def _fake_screenshot(*args: object, **kwargs: object) -> None:
            return None  # Chrome timed out and never wrote a new screenshot

        monkeypatch.setattr(verify_page, "screenshot", _fake_screenshot)

        options = _options_for(html_file, skip_screenshot=False, chrome="/fake/chrome")

        with pytest.raises(
            verify_page.VerificationError, match="スクリーンショットを生成できなかった"
        ):
            verify_page.render_screenshot(
                options, html_file, "file:///x", tmp_path, "/fake/chrome", 1000
            )

        assert not stale_output.exists()


# ---------------------------------------------------------------------------
# EV-31 拡張: run_chrome のタイムアウト時致命的化
# ---------------------------------------------------------------------------


class TestRunChromeTimeoutWithoutOutput:
    """タイムアウトかつ出力が空の場合は VerificationError で致命的エラーにする。"""

    def test_timeout_with_empty_output_raises_verification_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        process = _FakeChromeProcess(timeout_calls=1)
        monkeypatch.setattr(verify_page.subprocess, "Popen", process)
        monkeypatch.setattr(verify_page.os, "killpg", lambda pgid, sig: None)

        with pytest.raises(verify_page.VerificationError, match="タイムアウト"):
            verify_page.run_chrome("chrome", "file:///x", tmp_path, [], timeout=1)

    def test_timeout_with_empty_output_is_tolerated_when_output_not_expected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """--screenshot のように stdout が空で正常な呼び出しはタイムアウトを許容する。"""
        process = _FakeChromeProcess(timeout_calls=1)
        monkeypatch.setattr(verify_page.subprocess, "Popen", process)
        monkeypatch.setattr(verify_page.os, "killpg", lambda pgid, sig: None)

        result = verify_page.run_chrome(
            "chrome", "file:///x", tmp_path, [], timeout=1, expect_output=False
        )

        assert result == ""

    def test_process_already_gone_during_timeout_kill_is_tolerated(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def _raise_process_lookup_error(pid: int, sig: int) -> None:
            raise ProcessLookupError("no such process")

        process = _FakeChromeProcess(output="<html>done</html>", returncode=0, timeout_calls=1)
        monkeypatch.setattr(verify_page.subprocess, "Popen", process)
        monkeypatch.setattr(verify_page.os, "killpg", _raise_process_lookup_error)

        result = verify_page.run_chrome("chrome", "file:///x", tmp_path, [], timeout=1)

        assert result == "<html>done</html>"

    def test_reap_timeout_raises_verification_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        process = _FakeChromeProcess(timeout_calls=2)
        monkeypatch.setattr(verify_page.subprocess, "Popen", process)
        monkeypatch.setattr(verify_page.os, "killpg", lambda pgid, sig: None)
        with pytest.raises(verify_page.VerificationError, match="終了待機が 5 秒"):
            verify_page.run_chrome("chrome", "file:///x", tmp_path, [], timeout=1)

    def test_nonzero_exit_includes_stderr_tail(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        process = _FakeChromeProcess(stderr="boom: something went wrong", returncode=1)
        monkeypatch.setattr(verify_page.subprocess, "Popen", process)
        with pytest.raises(verify_page.VerificationError) as error_info:
            verify_page.run_chrome("chrome", "file:///x", tmp_path, [], timeout=1)
        assert "boom: something went wrong" in str(error_info.value)


# ---------------------------------------------------------------------------
# EV-31: main() の三段階 exit code
# ---------------------------------------------------------------------------


class TestMainExitCodes:
    """`--dom-file` + `--skip-screenshot` で Chrome 起動なしに三段階の exit code を検証する。"""

    def test_fully_rendered_dom_exits_ok_with_expected_json_keys(self, tmp_path: Path) -> None:
        html_file = tmp_path / "page.html"
        html_file.write_text(_render_template(), encoding="utf-8")
        dom_file = tmp_path / "dom.html"
        dom_file.write_text(
            '<body data-mermaid-ready="1" data-page-height="900">'
            '<div class="mermaid"><svg id="fig-0">x</svg></div></body>',
            encoding="utf-8",
        )

        proc = _run(
            [str(html_file), "--dom-file", str(dom_file), "--skip-screenshot"],
            cwd=tmp_path,
        )

        assert proc.returncode == verify_page.EXIT_OK, proc.stderr
        payload = json.loads(proc.stdout)
        assert payload["ok"] is True
        assert payload["mermaidRendered"] == 1
        assert payload["mermaidSources"] == 1
        assert payload["pageHeight"] == 900
        assert payload["screenshot"] is None
        assert payload["warnings"] == []

    def test_dom_with_warnings_exits_with_warnings_code(self, tmp_path: Path) -> None:
        html_file = tmp_path / "page.html"
        html_file.write_text(_render_template(), encoding="utf-8")
        dom_file = tmp_path / "dom.html"
        # ready フラグが立っていない DOM は build_warnings が警告を返す
        dom_file.write_text(
            '<body data-page-height="900"><pre class="mermaid">graph TD; a --> b</pre></body>',
            encoding="utf-8",
        )

        proc = _run(
            [str(html_file), "--dom-file", str(dom_file), "--skip-screenshot"],
            cwd=tmp_path,
        )

        assert proc.returncode == verify_page.EXIT_WARNINGS, proc.stderr
        payload = json.loads(proc.stdout)
        assert payload["ok"] is False
        assert payload["warnings"]

    def test_missing_html_file_exits_fatal_with_error_key(self, tmp_path: Path) -> None:
        missing_html = tmp_path / "does-not-exist.html"

        proc = _run([str(missing_html), "--skip-screenshot"], cwd=tmp_path)

        assert proc.returncode == verify_page.EXIT_FATAL, proc.stderr
        payload = json.loads(proc.stdout)
        assert payload["ok"] is False
        assert "error" in payload

    def test_nonexistent_chrome_binary_exits_fatal_with_error_key(self, tmp_path: Path) -> None:
        html_file = tmp_path / "page.html"
        html_file.write_text(_render_template(), encoding="utf-8")

        proc = _run(
            [str(html_file), "--chrome", "/nonexistent/binary", "--skip-screenshot"],
            cwd=tmp_path,
        )

        assert proc.returncode == verify_page.EXIT_FATAL, proc.stderr
        payload = json.loads(proc.stdout)
        assert payload["ok"] is False
        assert "error" in payload

    @pytest.mark.parametrize(
        "args",
        [[], ["--timeout", "abc"]],
        ids=["missing-html", "invalid-timeout"],
    )
    def test_cli_usage_errors_exit_fatal_with_json(self, args: list[str], tmp_path: Path) -> None:
        proc = _run(args, cwd=tmp_path)
        assert proc.returncode == verify_page.EXIT_FATAL, proc.stderr
        payload = json.loads(proc.stdout)
        assert payload["ok"] is False
        assert "error" in payload

    def test_help_keeps_argparse_success_exit(self, tmp_path: Path) -> None:
        proc = _run(["--help"], cwd=tmp_path)
        assert proc.returncode == verify_page.EXIT_OK
        assert "usage:" in proc.stdout


class TestHtmlPathValidation:
    """HTML は cwd 内の実体ファイルだけを受け入れる。"""

    def test_symlinked_directory_alias_of_cwd_rejects_absolute_symlink_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        real_directory = tmp_path / "real"
        real_directory.mkdir()
        alias_directory = tmp_path / "alias"
        alias_directory.symlink_to(real_directory, target_is_directory=True)
        target = real_directory / "target.html"
        target.write_text(_render_template(), encoding="utf-8")
        (real_directory / "link.html").symlink_to(target)
        monkeypatch.chdir(alias_directory)

        with pytest.raises(verify_page.VerificationError, match="シンボリックリンク"):
            verify_page._validate_html_path(alias_directory / "link.html")

    def test_symlinked_directory_alias_of_cwd_rejects_relative_symlink_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        real_directory = tmp_path / "real"
        real_directory.mkdir()
        alias_directory = tmp_path / "alias"
        alias_directory.symlink_to(real_directory, target_is_directory=True)
        target = real_directory / "target.html"
        target.write_text(_render_template(), encoding="utf-8")
        (real_directory / "link.html").symlink_to(target)
        monkeypatch.chdir(alias_directory)
        monkeypatch.setattr(verify_page.os, "getcwd", lambda: str(alias_directory))

        with pytest.raises(verify_page.VerificationError, match="シンボリックリンク"):
            verify_page._validate_html_path(Path("link.html"))

    def test_symlinked_html_file_is_fatal(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "target.html"
        target.write_text(_render_template(), encoding="utf-8")
        symlink = tmp_path / "page.html"
        symlink.symlink_to(target)
        with pytest.raises(verify_page.VerificationError, match="シンボリックリンク"):
            verify_page.verify_page(_options_for(symlink))

    def test_html_beneath_symlinked_directory_is_fatal(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        target_directory = tmp_path / "target"
        target_directory.mkdir()
        (target_directory / "page.html").write_text(_render_template(), encoding="utf-8")
        symlinked_directory = tmp_path / "linked"
        symlinked_directory.symlink_to(target_directory, target_is_directory=True)
        with pytest.raises(verify_page.VerificationError, match="シンボリックリンク"):
            verify_page.verify_page(_options_for(symlinked_directory / "page.html"))

    def test_html_outside_cwd_is_fatal(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        working_directory = tmp_path / "working"
        working_directory.mkdir()
        outside_directory = tmp_path / "outside"
        outside_directory.mkdir()
        outside_html = outside_directory / "page.html"
        outside_html.write_text(_render_template(), encoding="utf-8")
        monkeypatch.chdir(working_directory)
        with pytest.raises(verify_page.VerificationError, match="作業ディレクトリ外"):
            verify_page.verify_page(_options_for(outside_html))

    def test_symlink_loop_exits_fatal_with_json_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        loop_path = tmp_path / "loop"
        loop_path.symlink_to(loop_path)
        monkeypatch.chdir(tmp_path)

        exit_code = verify_page.main([str(loop_path), "--skip-screenshot"])
        payload = json.loads(capsys.readouterr().out)

        assert exit_code == verify_page.EXIT_FATAL
        assert payload["ok"] is False
        assert "error" in payload


# ---------------------------------------------------------------------------
# EV-34: template.html ↔ verify_page.py の契約（should）
# ---------------------------------------------------------------------------


class TestTemplateContract:
    """template.html が verify_page.py の走査前提（fig- id / data-* フラグ）と
    プレースホルダ・Mermaid CDN スクリプトを保持し続けているかを検証する。"""

    def test_template_assigns_fig_prefixed_ids_to_mermaid_render(self) -> None:
        template = TEMPLATE_PATH.read_text(encoding="utf-8")

        # DOM 走査は `<svg id="fig-N">` を前提とする。id を渡す箇所が
        # 'fig-' プレフィックスを使い続けているかを確認する。
        assert "mermaid.render('fig-' + i" in template

    def test_template_emits_ready_flag_and_page_height_dataset_hooks(self) -> None:
        template = TEMPLATE_PATH.read_text(encoding="utf-8")

        # ブラウザは `dataset.mermaidReady` / `dataset.pageHeight`（camelCase）への代入を
        # `data-mermaid-ready` / `data-page-height`（kebab-case）属性としてレンダリング済み DOM に
        # 反映する。DOM 走査の page height / ready 判定はこのレンダリング結果を前提にしており、
        # 静的なテンプレートソースには kebab-case 属性文字列そのものは現れない。
        assert "document.body.dataset.mermaidReady = '1'" in template
        assert (
            "document.body.dataset.pageHeight = String(document.documentElement.scrollHeight)"
            in template
        )

    def test_template_keeps_title_and_body_placeholders(self) -> None:
        template = TEMPLATE_PATH.read_text(encoding="utf-8")

        assert "{{TITLE}}" in template
        assert "{{BODY}}" in template

    def test_template_keeps_mermaid_cdn_module_script(self) -> None:
        template = TEMPLATE_PATH.read_text(encoding="utf-8")

        assert '<script type="module">' in template
        assert "cdn.jsdelivr.net/npm/mermaid" in template

    def test_csp_hashes_match_inline_script_blocks(self) -> None:
        """CSP meta の sha256 ハッシュが inline script 2 本の内容と一致する（EV-14）。

        script を編集してハッシュを再計算し忘れると CSP が Mermaid 描画をブロックし、
        症状は不透明な「sources != rendered」警告だけになるため、ここで固定する。
        """
        template = TEMPLATE_PATH.read_text(encoding="utf-8")
        csp_match = re.search(r'http-equiv="Content-Security-Policy" content="([^"]*)"', template)
        assert csp_match is not None
        csp = csp_match.group(1)

        scripts = re.findall(r"<script[^>]*>(.*?)</script>", template, re.S)
        assert len(scripts) == verify_page.TEMPLATE_SCRIPT_COUNT
        for body in scripts:
            digest = base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode()
            assert f"'sha256-{digest}'" in csp

    def test_csp_blocks_external_connections_and_images(self) -> None:
        template = TEMPLATE_PATH.read_text(encoding="utf-8")

        assert "connect-src 'none'" in template
        assert "img-src data:" in template
        assert "default-src 'none'" in template
