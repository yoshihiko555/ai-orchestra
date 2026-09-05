#!/usr/bin/env python3
"""Render and verify an explain-visually HTML page with headless Chrome.

Adapted from https://github.com/keitakn/engineering-skills at commit
f972ef4a1f8fac0410c77d7918998e2bcfaae43c. The upstream work is MIT licensed.
This version makes Chrome path resolution configurable, extracts DOM parsing into
testable functions, adds --dom-file, --skip-screenshot, --chrome, and --timeout
CLI options, and uses three-tier exit codes.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import IO

MACOS_DEFAULT_CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME_BINARY_CANDIDATES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
)
CHROME_ENVIRONMENT_VARIABLE = "EXPLAIN_VISUALLY_CHROME"
DEFAULT_VIEWPORT_WIDTH = 1250
DEFAULT_VIRTUAL_TIME_BUDGET_MS = 25000
DEFAULT_CHROME_TIMEOUT_SECONDS = 40
REAP_TIMEOUT_SECONDS = 5
STDERR_TAIL_CHARS = 2000
DOM_WINDOW_HEIGHT = 1200
FALLBACK_WINDOW_HEIGHT = 12000
SCREENSHOT_HEIGHT_PADDING = 40
TEMPORARY_DIRECTORY_PREFIX = "explain-visually-"
TEMPLATE_FILENAME = "template.html"
EXIT_OK = 0
EXIT_FATAL = 1
EXIT_WARNINGS = 2

# Number of <script> elements the template itself contains (page-height reporter +
# Mermaid CDN loader). Any count above this in the final HTML source indicates
# script content that was injected via unescaped source text.
TEMPLATE_SCRIPT_COUNT = 2
NAVIGATION_ATTRIBUTE_NAMES = frozenset({"href", "src", "action", "formaction"})
META_REFRESH_WARNING = "meta refresh が含まれる。原文の引用は HTML エスケープすること"
FORBIDDEN_ELEMENT_WARNINGS = {
    "base": "<base> が含まれる。原文の引用は HTML エスケープすること",
    "iframe": "<iframe> が含まれる。原文の引用は HTML エスケープすること",
    "object": "<object> が含まれる。原文の引用は HTML エスケープすること",
    "embed": "<embed> が含まれる。原文の引用は HTML エスケープすること",
    "form": "<form> が含まれる。原文の引用は HTML エスケープすること",
    "link": "<link> が含まれる。原文の引用は HTML エスケープすること",
}
UNREPLACED_PLACEHOLDER_WARNINGS = (
    ("{{TITLE}}", "{{TITLE}} が未置換のまま残っている"),
    ("{{BODY}}", "{{BODY}} が未置換のまま残っている"),
)

HtmlAttribute = tuple[str, str | None]
ScriptElement = tuple[tuple[HtmlAttribute, ...], str]


def _attributes_by_name(attributes: list[HtmlAttribute]) -> dict[str, str | None]:
    """Index parsed HTML attributes by their normalized names."""
    return dict(attributes)


def _is_event_handler_attribute(attribute_name: str) -> bool:
    """Return whether an attribute name has the on-word event-handler form."""
    suffix = attribute_name[2:]
    return (
        attribute_name.startswith("on")
        and bool(suffix)
        and all(character == "_" or character.isalnum() for character in suffix)
    )


def _is_rendered_figure_id(element_id: str) -> bool:
    """Return whether an element ID follows the rendered Mermaid figure format."""
    return element_id.startswith("fig-") and element_id.removeprefix("fig-").isdigit()


class MarkupScanner(HTMLParser):
    """Collect verification facts from one HTML parse pass."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.body_attributes: dict[str, str | None] | None = None
        self.csp_content: str | None = None
        self.forbidden_elements: set[str] = set()
        self.has_event_handler_attribute = False
        self.has_javascript_scheme = False
        self.has_meta_refresh = False
        self.head_depth = 0
        self.mermaid_sources = 0
        self.preformatted_depth = 0
        self.rendered_figures = 0
        self.script_count = 0
        self.script_elements: list[ScriptElement] = []
        self.text_outside_preformatted: list[str] = []
        self.title_text = ""
        self._has_seen_title = False
        self._script_attributes: tuple[HtmlAttribute, ...] | None = None
        self._script_body_chunks: list[str] = []
        self._title_chunks: list[str] = []
        self._title_depth = 0

    def handle_starttag(self, tag: str, attrs: list[HtmlAttribute]) -> None:
        """Record facts exposed by one opening tag."""
        attributes = _attributes_by_name(attrs)
        self._enter_context(tag)
        self._record_body(tag, attributes)
        self._record_meta(tag, attributes)
        self._record_markup_risks(tag, attrs)
        self._record_dom_metrics(tag, attributes)
        self._start_script(tag, attrs)
        self._start_title(tag)

    def handle_endtag(self, tag: str) -> None:
        """Finalize element text and leave tracked contexts."""
        self._finish_script(tag)
        self._finish_title(tag)
        if tag == "head":
            self.head_depth = max(self.head_depth - 1, 0)
        if tag in {"pre", "code"}:
            self.preformatted_depth = max(self.preformatted_depth - 1, 0)

    def handle_data(self, data: str) -> None:
        """Accumulate script, title, and placeholder-candidate text."""
        if self._script_attributes is not None:
            self._script_body_chunks.append(data)
        if self._title_depth:
            self._title_chunks.append(data)
        if not self.preformatted_depth:
            self.text_outside_preformatted.append(data)

    def contains_placeholder(self, placeholder: str) -> bool:
        """Return whether visible or title text contains an unreplaced placeholder."""
        candidates = [*self.text_outside_preformatted, self.title_text]
        return any(placeholder in candidate for candidate in candidates)

    def _enter_context(self, tag: str) -> None:
        if tag == "head":
            self.head_depth += 1
        if tag in {"pre", "code"}:
            self.preformatted_depth += 1

    def _record_body(self, tag: str, attributes: dict[str, str | None]) -> None:
        if tag == "body" and self.body_attributes is None:
            self.body_attributes = attributes

    def _record_meta(self, tag: str, attributes: dict[str, str | None]) -> None:
        if tag != "meta":
            return
        http_equiv = attributes.get("http-equiv")
        if http_equiv is None:
            return
        directive = http_equiv.strip().lower()
        if directive == "refresh":
            self.has_meta_refresh = True
        if directive == "content-security-policy" and self.head_depth:
            self._record_first_csp_content(attributes.get("content"))

    def _record_first_csp_content(self, content: str | None) -> None:
        if self.csp_content is None and content is not None:
            self.csp_content = content

    def _record_markup_risks(self, tag: str, attributes: list[HtmlAttribute]) -> None:
        if tag in FORBIDDEN_ELEMENT_WARNINGS:
            self.forbidden_elements.add(tag)
        for attribute_name, value in attributes:
            if _is_event_handler_attribute(attribute_name):
                self.has_event_handler_attribute = True
            if self._is_javascript_navigation(attribute_name, value):
                self.has_javascript_scheme = True

    def _is_javascript_navigation(self, attribute_name: str, value: str | None) -> bool:
        if attribute_name not in NAVIGATION_ATTRIBUTE_NAMES or value is None:
            return False
        return value.strip().lower().startswith("javascript:")

    def _record_dom_metrics(self, tag: str, attributes: dict[str, str | None]) -> None:
        class_value = attributes.get("class")
        if class_value is not None and "mermaid" in class_value.split():
            self.mermaid_sources += 1
        element_id = attributes.get("id")
        if tag == "svg" and element_id is not None and _is_rendered_figure_id(element_id):
            self.rendered_figures += 1

    def _start_script(self, tag: str, attributes: list[HtmlAttribute]) -> None:
        if tag != "script":
            return
        self.script_count += 1
        if self._script_attributes is None:
            self._script_attributes = tuple(attributes)
            self._script_body_chunks = []

    def _finish_script(self, tag: str) -> None:
        if tag != "script" or self._script_attributes is None:
            return
        self.script_elements.append((self._script_attributes, "".join(self._script_body_chunks)))
        self._script_attributes = None
        self._script_body_chunks = []

    def _start_title(self, tag: str) -> None:
        if tag != "title":
            return
        if self._title_depth:
            self._title_depth += 1
            return
        if not self._has_seen_title:
            self._has_seen_title = True
            self._title_depth = 1
            self._title_chunks = []

    def _finish_title(self, tag: str) -> None:
        if tag != "title" or not self._title_depth:
            return
        self._title_depth -= 1
        if not self._title_depth:
            self.title_text = "".join(self._title_chunks)


def _scan_markup(markup: str) -> MarkupScanner:
    """Parse markup once and return all collected verification facts."""
    scanner = MarkupScanner()
    scanner.feed(markup)
    scanner.close()
    return scanner


def _parse_page_height(value: str | None) -> int:
    """Parse a body page-height attribute with a zero fallback."""
    if value is None:
        return 0
    try:
        return int(value)
    except ValueError:
        return 0


@dataclass(frozen=True)
class DomMetrics:
    """Metrics extracted from a rendered page DOM."""

    rendered: int
    unrendered: int
    sources: int
    ready: bool
    page_height: int
    title: str


EMPTY_DOM_METRICS = DomMetrics(
    rendered=0,
    unrendered=0,
    sources=0,
    ready=False,
    page_height=0,
    title="",
)


@dataclass(frozen=True)
class CliOptions:
    """Command-line options used during page verification."""

    html: Path
    width: int
    wait: int
    timeout: int
    dom_file: Path | None
    skip_screenshot: bool
    chrome: str | None


class VerificationError(RuntimeError):
    """Raised when page verification cannot complete."""


class CliUsageError(VerificationError):
    """Raised when command-line arguments are invalid."""


class _StrictArgumentParser(argparse.ArgumentParser):
    """Argument parser that reports usage errors through the JSON error path."""

    def error(self, message: str) -> None:
        raise CliUsageError(message)


def resolve_chrome_path(
    explicit: str | None,
    env: Mapping[str, str],
    which: Callable[[str], str | None],
    exists: Callable[[str], bool] = os.path.exists,
) -> str | None:
    """Resolve a Chrome executable using explicit and platform fallbacks."""
    if explicit:
        return explicit

    environment_path = env.get(CHROME_ENVIRONMENT_VARIABLE)
    if environment_path:
        return environment_path
    if exists(MACOS_DEFAULT_CHROME_PATH):
        return MACOS_DEFAULT_CHROME_PATH

    for binary_name in CHROME_BINARY_CANDIDATES:
        resolved_path = which(binary_name)
        if resolved_path:
            return resolved_path
    return None


def parse_dom_metrics(dom: str) -> DomMetrics:
    """Parse Mermaid state, page height, and title from a rendered DOM."""
    scanner = _scan_markup(dom)
    sources = scanner.mermaid_sources
    rendered = scanner.rendered_figures
    unrendered = max(sources - rendered, 0)
    body_attributes = scanner.body_attributes or {}

    return DomMetrics(
        rendered=rendered,
        unrendered=unrendered,
        sources=sources,
        ready=body_attributes.get("data-mermaid-ready") == "1",
        page_height=_parse_page_height(body_attributes.get("data-page-height")),
        title=scanner.title_text.strip(),
    )


def build_warnings(metrics: DomMetrics) -> list[str]:
    """Build warnings for incomplete Mermaid rendering or missing page height."""
    warnings: list[str] = []
    if metrics.sources and not metrics.ready:
        warnings.append(
            "Mermaid の描画完了フラグが立っていない。CDN に到達できていないか、記法にエラーがある。"
            "Bash のサンドボックス内では外部ホストに到達できないため、サンドボックス無しで再実行する"
        )
    if metrics.sources and metrics.rendered != metrics.sources:
        warnings.append(
            f"Mermaid の図が {metrics.sources} 個あるのに描画されたのは {metrics.rendered} 個。"
            "記法エラーか id の衝突が疑われる"
        )
    if not metrics.page_height:
        warnings.append(
            "ページ高さを取得できなかった。テンプレートの高さ出力scriptが消えている可能性がある。"
            f"既定の {FALLBACK_WINDOW_HEIGHT}px で撮影したため、末尾が切れていないかスクリーンショットで目視する"
        )
    return warnings


def lint_injected_markup(html: str) -> list[str]:
    """Flag script/event-handler injection risk and a missing CSP meta tag."""
    scanner = _scan_markup(html)
    warnings: list[str] = []
    if scanner.script_count > TEMPLATE_SCRIPT_COUNT:
        extra_count = scanner.script_count - TEMPLATE_SCRIPT_COUNT
        warnings.append(
            f"テンプレート由来以外の <script> が {extra_count} 個含まれる。"
            "原文の引用は HTML エスケープすること"
        )
    if scanner.has_event_handler_attribute:
        warnings.append(
            "イベントハンドラ属性（onXxx=）が含まれる。原文の引用は HTML エスケープすること"
        )
    if scanner.has_javascript_scheme:
        warnings.append("javascript: スキームが含まれる。原文の引用は HTML エスケープすること")
    if scanner.csp_content is None:
        warnings.append("テンプレートの CSP meta が無い")
    if scanner.has_meta_refresh:
        warnings.append(META_REFRESH_WARNING)
    warnings.extend(
        message
        for tag, message in FORBIDDEN_ELEMENT_WARNINGS.items()
        if tag in scanner.forbidden_elements
    )
    warnings.extend(
        message
        for placeholder, message in UNREPLACED_PLACEHOLDER_WARNINGS
        if scanner.contains_placeholder(placeholder)
    )
    return warnings


def _extract_csp_content(html: str) -> str | None:
    """Extract the CSP meta content attribute from HTML."""
    return _scan_markup(html).csp_content


def _extract_script_elements(html: str) -> list[ScriptElement]:
    """Extract script attributes and raw bodies in document order."""
    return _scan_markup(html).script_elements


def lint_template_integrity(html: str, template: str) -> list[str]:
    """Flag generated CSP or script elements that differ from the template."""
    generated = _scan_markup(html)
    expected = _scan_markup(template)
    warnings: list[str] = []
    if generated.csp_content != expected.csp_content:
        warnings.append("CSP meta の内容がテンプレートと一致しない（緩和されている可能性がある）")

    generated_scripts = generated.script_elements
    template_scripts = expected.script_elements
    if generated_scripts != template_scripts[: len(generated_scripts)]:
        warnings.append(
            "生成 HTML の <script> 本文がテンプレートと一致しない（改変されている可能性がある）"
        )
    return warnings


def build_chrome_command(
    chrome_path: str,
    url: str,
    profile: Path,
    extra: list[str],
) -> list[str]:
    """Build the headless Chrome command for one rendering operation."""
    return [
        chrome_path,
        "--headless=new",
        "--disable-gpu",
        "--disable-crash-reporter",
        "--no-first-run",
        f"--user-data-dir={profile}",
        "--hide-scrollbars",
        *extra,
        url,
    ]


def run_chrome(
    chrome_path: str,
    url: str,
    profile: Path,
    extra: list[str],
    timeout: int,
    expect_output: bool = True,
) -> str:
    """Run Chrome in its own process group and return captured stdout.

    Chrome は処理完了後も終了が遅れることがあるため、タイムアウト自体は許容する。
    ただし stdout を期待する呼び出し（--dump-dom）でタイムアウト時に出力が空なら、
    描画が完了していないので致命的エラーとして扱う。--screenshot のように
    stdout が空で正常な呼び出しは expect_output=False で呼ぶ。

    stdout / stderr はパイプではなく一時ファイルへ書かせる。パイプだと、プロセス
    グループ外に逃げた Chrome の補助プロセス（crashpad 等）が書き込み端を握り続け、
    親を SIGKILL した後も EOF が来ず回収が終わらないことがあるため。
    """
    command = build_chrome_command(chrome_path, url, profile, extra)
    capture_dir = profile.parent
    with (
        tempfile.NamedTemporaryFile(dir=capture_dir, suffix=".out", delete=False) as stdout_file,
        tempfile.NamedTemporaryFile(dir=capture_dir, suffix=".err", delete=False) as stderr_file,
    ):
        process = _launch_chrome(command, stdout_file, stderr_file)
        timed_out = _wait_or_kill(process, timeout)
    output = _read_capture(Path(stdout_file.name))
    stderr = _read_capture(Path(stderr_file.name))
    if timed_out and expect_output and not output:
        raise VerificationError(f"Chrome が {timeout} 秒でタイムアウトしました（出力なし）")
    if not timed_out and process.returncode:
        detail = stderr[-STDERR_TAIL_CHARS:] if stderr else ""
        suffix = f"\n--- stderr (tail) ---\n{detail}" if detail else ""
        raise VerificationError(f"Chrome が終了コード {process.returncode} で失敗しました{suffix}")
    return output


def _launch_chrome(
    command: list[str], stdout: IO[bytes], stderr: IO[bytes]
) -> subprocess.Popen[bytes]:
    """Launch Chrome in a dedicated process group, capturing output into files."""
    try:
        return subprocess.Popen(command, stdout=stdout, stderr=stderr, start_new_session=True)
    except OSError as error:
        raise VerificationError(f"Chrome の起動に失敗しました: {error}") from error


def _wait_or_kill(process: subprocess.Popen[bytes], timeout: int) -> bool:
    """Wait for Chrome; on timeout kill its process group. Returns whether it timed out."""
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_and_reap(process)
        return True
    return False


def _kill_and_reap(process: subprocess.Popen[bytes]) -> None:
    """Kill Chrome's process group and reap it within a bounded interval."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=REAP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as error:
        raise VerificationError(
            f"Chrome プロセスの終了待機が {REAP_TIMEOUT_SECONDS} 秒でタイムアウトしました"
        ) from error


def _read_capture(path: Path) -> str:
    """Read a capture file written by Chrome and remove it."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    finally:
        path.unlink(missing_ok=True)


def dump_dom(
    chrome_path: str,
    url: str,
    profile: Path,
    width: int,
    budget_ms: int,
    timeout: int,
) -> str:
    """Dump the rendered DOM at the screenshot viewport width."""
    extra = [
        f"--window-size={width},{DOM_WINDOW_HEIGHT}",
        f"--virtual-time-budget={budget_ms}",
        "--dump-dom",
    ]
    return run_chrome(chrome_path, url, profile, extra, timeout)


def screenshot(
    chrome_path: str,
    url: str,
    profile: Path,
    width: int,
    height: int,
    output_path: Path,
    budget_ms: int,
    timeout: int,
) -> None:
    """Capture a full-page screenshot using the measured page height."""
    extra = [
        f"--virtual-time-budget={budget_ms}",
        f"--window-size={width},{height}",
        f"--screenshot={output_path}",
    ]
    run_chrome(chrome_path, url, profile, extra, timeout, expect_output=False)


def build_argument_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""
    parser = _StrictArgumentParser(description=__doc__)
    parser.add_argument("html", type=Path, help="検証するHTMLファイルのパス")
    parser.add_argument(
        "--width",
        type=int,
        default=DEFAULT_VIEWPORT_WIDTH,
        help=f"ビューポート幅（既定: {DEFAULT_VIEWPORT_WIDTH}）",
    )
    parser.add_argument(
        "--wait",
        type=int,
        default=DEFAULT_VIRTUAL_TIME_BUDGET_MS,
        help=f"描画を待つ仮想時間のミリ秒（既定: {DEFAULT_VIRTUAL_TIME_BUDGET_MS}）",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_CHROME_TIMEOUT_SECONDS,
        help=f"Chrome1回あたりの打ち切り秒数（既定: {DEFAULT_CHROME_TIMEOUT_SECONDS}）",
    )
    parser.add_argument(
        "--dom-file",
        type=Path,
        help="Chromeの代わりに読み込むDOM HTMLファイル（指定時はスクリーンショットも省略する）",
    )
    parser.add_argument(
        "--skip-screenshot", action="store_true", help="スクリーンショット撮影を省略する"
    )
    parser.add_argument("--chrome", help="使用するChromeバイナリのパス")
    return parser


def parse_cli_options(argv: list[str] | None) -> CliOptions:
    """Parse command-line arguments into immutable options."""
    arguments = build_argument_parser().parse_args(argv)
    return CliOptions(
        html=arguments.html,
        width=arguments.width,
        wait=arguments.wait,
        timeout=arguments.timeout,
        dom_file=arguments.dom_file,
        skip_screenshot=arguments.skip_screenshot,
        chrome=arguments.chrome,
    )


def resolve_required_chrome(explicit: str | None) -> str:
    """Resolve Chrome or raise a fatal verification error."""
    chrome_path = resolve_chrome_path(explicit, os.environ, shutil.which)
    if chrome_path:
        return chrome_path
    raise VerificationError(
        "Google Chrome が見つかりません。--chrome または EXPLAIN_VISUALLY_CHROME を指定してください"
    )


def read_dom_file(dom_file: Path) -> str:
    """Read a previously dumped DOM file."""
    try:
        return dom_file.read_text(encoding="utf-8")
    except OSError as error:
        raise VerificationError(f"DOMファイルを読み込めません: {dom_file}: {error}") from error


def read_html_source(html: Path) -> str:
    """Read the original HTML source for injected-markup linting."""
    try:
        return html.read_text(encoding="utf-8")
    except OSError as error:
        raise VerificationError(f"HTMLファイルを読み込めません: {html}: {error}") from error


def read_template_source() -> str:
    """Read the HTML template distributed beside this verifier."""
    template_path = Path(__file__).resolve().parent / TEMPLATE_FILENAME
    try:
        return template_path.read_text(encoding="utf-8")
    except OSError as error:
        raise VerificationError(
            f"テンプレートを読み込めません: {template_path}: {error}"
        ) from error


def _resolve_or_raise(path: Path) -> Path:
    """Resolve a path or convert filesystem resolution failures to JSON errors."""
    try:
        return path.resolve()
    except (RuntimeError, OSError) as error:
        raise VerificationError(f"パスを解決できません: {path}: {error}") from error


def _validate_html_path(html: Path) -> Path:
    """Resolve an HTML path after enforcing cwd containment and no symlinks."""
    cwd = _resolve_or_raise(Path.cwd())
    absolute_html = html.absolute()
    current_path = absolute_html
    while _resolve_or_raise(current_path) != cwd:
        if current_path.is_symlink():
            raise VerificationError(
                f"シンボリックリンク経由のパスは許可されていません: {current_path}"
            )
        parent = current_path.parent
        if parent == current_path:
            break
        current_path = parent

    resolved_html = _resolve_or_raise(absolute_html)
    if not resolved_html.is_relative_to(cwd):
        raise VerificationError(f"作業ディレクトリ外のパスは許可されていません: {resolved_html}")
    if not resolved_html.is_file():
        raise VerificationError(f"ファイルが見つかりません: {resolved_html}")
    return resolved_html


def load_rendered_dom(
    options: CliOptions,
    url: str,
    profile: Path,
) -> tuple[str, str | None]:
    """Load a supplied DOM or render one with Chrome."""
    if options.dom_file is not None:
        return read_dom_file(options.dom_file), None

    chrome_path = resolve_required_chrome(options.chrome)
    dom = dump_dom(chrome_path, url, profile, options.width, options.wait, options.timeout)
    return dom, chrome_path


def render_screenshot(
    options: CliOptions,
    html: Path,
    url: str,
    profile: Path,
    chrome_path: str | None,
    page_height: int,
) -> Path | None:
    """Capture a screenshot unless the caller requested DOM-only verification."""
    if options.skip_screenshot or options.dom_file is not None:
        return None

    resolved_chrome = chrome_path or resolve_required_chrome(options.chrome)
    output_path = html.parent / f"{html.stem}-shot.png"
    output_path.unlink(missing_ok=True)
    screenshot_height = page_height + SCREENSHOT_HEIGHT_PADDING
    screenshot(
        resolved_chrome,
        url,
        profile,
        options.width,
        screenshot_height,
        output_path,
        options.wait,
        options.timeout,
    )
    if output_path.exists():
        return output_path
    raise VerificationError("スクリーンショットを生成できなかった")


def build_output(
    html: Path,
    metrics: DomMetrics,
    page_height: int,
    screenshot_path: Path | None,
    warnings: list[str],
) -> dict[str, object]:
    """Build the upstream-compatible JSON output object."""
    return {
        "ok": not warnings,
        "html": str(html),
        "title": metrics.title,
        "pageHeight": page_height,
        "mermaidSources": metrics.sources,
        "mermaidRendered": metrics.rendered,
        "mermaidReady": metrics.ready,
        "screenshot": str(screenshot_path) if screenshot_path else None,
        "warnings": warnings,
    }


def verify_page(options: CliOptions) -> dict[str, object]:
    """Verify one HTML page and return its JSON-ready report."""
    html = _validate_html_path(options.html)
    html_source = read_html_source(html)
    template_source = read_template_source()
    markup_warnings = [
        *lint_injected_markup(html_source),
        *lint_template_integrity(html_source, template_source),
    ]
    if markup_warnings:
        return build_output(html, EMPTY_DOM_METRICS, 0, None, markup_warnings)

    url = html.as_uri()
    with tempfile.TemporaryDirectory(prefix=TEMPORARY_DIRECTORY_PREFIX) as temporary_directory:
        profile = Path(temporary_directory) / "profile"
        dom, chrome_path = load_rendered_dom(options, url, profile)
        metrics = parse_dom_metrics(dom)
        warnings = build_warnings(metrics)
        page_height = metrics.page_height or FALLBACK_WINDOW_HEIGHT
        screenshot_path = render_screenshot(options, html, url, profile, chrome_path, page_height)

    return build_output(html, metrics, page_height, screenshot_path, warnings)


def print_json(payload: Mapping[str, object]) -> None:
    """Print a JSON payload using the upstream output formatting."""
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    """Run page verification and return its three-tier exit status."""
    try:
        options = parse_cli_options(argv)
        output = verify_page(options)
    except VerificationError as error:
        print_json({"ok": False, "error": str(error)})
        return EXIT_FATAL

    print_json(output)
    return EXIT_WARNINGS if output["warnings"] else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
