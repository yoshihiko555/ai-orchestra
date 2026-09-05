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
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

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

RENDERED_FIGURE_PATTERN = re.compile(r'<svg id="fig-\d+"')
MERMAID_ELEMENT_PATTERN = re.compile(r'<\w+\s+class="mermaid">')
PAGE_HEIGHT_PATTERN = re.compile(r'data-page-height="(\d+)"')
PAGE_TITLE_PATTERN = re.compile(r"<title>(.*?)</title>", re.S)

# Number of <script> elements the template itself contains (page-height reporter +
# Mermaid CDN loader). Any count above this in the final HTML source indicates
# script content that was injected via unescaped source text.
TEMPLATE_SCRIPT_COUNT = 2
SCRIPT_TAG_PATTERN = re.compile(r"<script\b", re.IGNORECASE)
# タグの属性位置に限定して検出する。エスケープ済みの引用（`&lt;img onerror=...&gt;` や
# `element.onclick = fn` のようなコード片）は `<` で始まるタグ文脈にならないため誤検知しない。
EVENT_HANDLER_ATTRIBUTE_PATTERN = re.compile(r"<\w[^>]*\bon\w+\s*=", re.IGNORECASE)
JAVASCRIPT_SCHEME_PATTERN = re.compile(
    r"""<\w[^>]*\b(?:href|src|action|formaction)\s*=\s*["']?\s*javascript:""",
    re.IGNORECASE,
)
CSP_META_PATTERN = re.compile(r"Content-Security-Policy", re.IGNORECASE)
CSP_META_CONTENT_PATTERN = re.compile(
    r"""<meta\b(?=[^>]*\bhttp-equiv\s*=\s*["']Content-Security-Policy["'])"""
    r"""[^>]*\bcontent\s*=\s*(?:"(?P<double>[^"]*)"|'(?P<single>[^']*)')""",
    re.IGNORECASE,
)
SCRIPT_BODY_PATTERN = re.compile(r"<script[^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL)
META_REFRESH_PATTERN = re.compile(
    r"""<meta\b[^>]*\bhttp-equiv\s*=\s*(?:"\s*refresh\s*"|'\s*refresh\s*'|refresh\b)""",
    re.IGNORECASE,
)
BASE_TAG_PATTERN = re.compile(r"<base\b", re.IGNORECASE)
IFRAME_TAG_PATTERN = re.compile(r"<iframe\b", re.IGNORECASE)
OBJECT_TAG_PATTERN = re.compile(r"<object\b", re.IGNORECASE)
EMBED_TAG_PATTERN = re.compile(r"<embed\b", re.IGNORECASE)
FORM_TAG_PATTERN = re.compile(r"<form\b", re.IGNORECASE)
FORBIDDEN_MARKUP_RULES = (
    (META_REFRESH_PATTERN, "meta refresh が含まれる。原文の引用は HTML エスケープすること"),
    (BASE_TAG_PATTERN, "<base> が含まれる。原文の引用は HTML エスケープすること"),
    (IFRAME_TAG_PATTERN, "<iframe> が含まれる。原文の引用は HTML エスケープすること"),
    (OBJECT_TAG_PATTERN, "<object> が含まれる。原文の引用は HTML エスケープすること"),
    (EMBED_TAG_PATTERN, "<embed> が含まれる。原文の引用は HTML エスケープすること"),
    (FORM_TAG_PATTERN, "<form> が含まれる。原文の引用は HTML エスケープすること"),
)
UNREPLACED_PLACEHOLDER_WARNINGS = (
    ("{{TITLE}}", "{{TITLE}} が未置換のまま残っている"),
    ("{{BODY}}", "{{BODY}} が未置換のまま残っている"),
)


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
    sources = len(MERMAID_ELEMENT_PATTERN.findall(dom))
    rendered = len(RENDERED_FIGURE_PATTERN.findall(dom))
    unrendered = max(sources - rendered, 0)
    height_match = PAGE_HEIGHT_PATTERN.search(dom)
    title_match = PAGE_TITLE_PATTERN.search(dom)

    return DomMetrics(
        rendered=rendered,
        unrendered=unrendered,
        sources=sources,
        ready='data-mermaid-ready="1"' in dom,
        page_height=int(height_match.group(1)) if height_match else 0,
        title=title_match.group(1).strip() if title_match else "",
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
    warnings: list[str] = []
    script_count = len(SCRIPT_TAG_PATTERN.findall(html))
    if script_count > TEMPLATE_SCRIPT_COUNT:
        extra_count = script_count - TEMPLATE_SCRIPT_COUNT
        warnings.append(
            f"テンプレート由来以外の <script> が {extra_count} 個含まれる。"
            "原文の引用は HTML エスケープすること"
        )
    if EVENT_HANDLER_ATTRIBUTE_PATTERN.search(html):
        warnings.append(
            "イベントハンドラ属性（onXxx=）が含まれる。原文の引用は HTML エスケープすること"
        )
    if JAVASCRIPT_SCHEME_PATTERN.search(html):
        warnings.append("javascript: スキームが含まれる。原文の引用は HTML エスケープすること")
    if not CSP_META_PATTERN.search(html):
        warnings.append("テンプレートの CSP meta が無い")
    warnings.extend(message for pattern, message in FORBIDDEN_MARKUP_RULES if pattern.search(html))
    warnings.extend(
        message for placeholder, message in UNREPLACED_PLACEHOLDER_WARNINGS if placeholder in html
    )
    return warnings


def _extract_csp_content(html: str) -> str | None:
    """Extract the CSP meta content attribute from HTML."""
    match = CSP_META_CONTENT_PATTERN.search(html)
    if match is None:
        return None
    return match.group("double") if match.group("double") is not None else match.group("single")


def _extract_script_elements(html: str) -> list[str]:
    """Extract full script elements, including opening tag attributes."""
    return [match.group(0) for match in SCRIPT_BODY_PATTERN.finditer(html)]


def lint_template_integrity(html: str, template: str) -> list[str]:
    """Flag generated CSP or script elements that differ from the template."""
    warnings: list[str] = []
    if _extract_csp_content(html) != _extract_csp_content(template):
        warnings.append("CSP meta の内容がテンプレートと一致しない（緩和されている可能性がある）")

    generated_scripts = _extract_script_elements(html)
    template_scripts = _extract_script_elements(template)
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
    """
    command = build_chrome_command(chrome_path, url, profile, extra)
    process = _launch_chrome(command)
    timed_out = False
    try:
        output, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        output, stderr = _kill_and_reap(process)
    if timed_out and expect_output and not output:
        raise VerificationError(f"Chrome が {timeout} 秒でタイムアウトしました（出力なし）")
    if not timed_out and process.returncode:
        detail = stderr[-STDERR_TAIL_CHARS:] if stderr else ""
        suffix = f"\n--- stderr (tail) ---\n{detail}" if detail else ""
        raise VerificationError(f"Chrome が終了コード {process.returncode} で失敗しました{suffix}")
    return output or ""


def _launch_chrome(command: list[str]) -> subprocess.Popen[str]:
    """Launch Chrome with captured output in a dedicated process group."""
    try:
        return subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError as error:
        raise VerificationError(f"Chrome の起動に失敗しました: {error}") from error


def _kill_and_reap(process: subprocess.Popen[str]) -> tuple[str, str]:
    """Kill Chrome's process group and reap it within a bounded interval."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        return process.communicate(timeout=REAP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as error:
        raise VerificationError(
            f"Chrome プロセスの終了待機が {REAP_TIMEOUT_SECONDS} 秒でタイムアウトしました"
        ) from error


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
    parser.add_argument("--dom-file", type=Path, help="Chromeの代わりに読み込むDOM HTMLファイル")
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


def _validate_html_path(html: Path) -> Path:
    """Resolve an HTML path after enforcing cwd containment and no symlinks."""
    cwd = Path.cwd().resolve()
    absolute_html = html.absolute()
    current_path = absolute_html
    while current_path.resolve() != cwd:
        if current_path.is_symlink():
            raise VerificationError(
                f"シンボリックリンク経由のパスは許可されていません: {current_path}"
            )
        parent = current_path.parent
        if parent == current_path:
            break
        current_path = parent

    resolved_html = absolute_html.resolve()
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
    if options.skip_screenshot:
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
