from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest

from scripts.require_test_reports import (
    ReportError,
    require_playwright_json,
    require_vitest_json,
)

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
NODE = "node"
VITEST = WEB / "node_modules" / "vitest" / "vitest.mjs"
PLAYWRIGHT = WEB / "node_modules" / "@playwright" / "test" / "cli.js"
pytestmark = pytest.mark.slow


def _child_env() -> dict[str, str]:
    env = {**os.environ, "NO_COLOR": "1"}
    env.pop("FORCE_COLOR", None)
    return env


def _run_vitest_fixture(fixture: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            NODE,
            str(VITEST),
            "run",
            "--config",
            "tests/fixtures/runtime-error-guard/vitest.config.ts",
            "--no-color",
            "--allowOnly=false",
            fixture,
        ],
        cwd=WEB,
        env=_child_env(),
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )


def _lint_required_test_source(
    source: str, *, file_path: str = "tests/unit/fail-closed.fixture.test.ts"
) -> list[dict[str, object]]:
    program = """
import { ESLint } from "eslint";
const eslint = new ESLint({ cwd: process.cwd() });
const [result] = await eslint.lintText(process.argv[1], {
  filePath: process.argv[2],
});
process.stdout.write(JSON.stringify(result.messages));
"""
    result = subprocess.run(
        [NODE, "--input-type=module", "--eval", program, source, file_path],
        cwd=WEB,
        env=_child_env(),
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def _native_guard_result(guard: Callable[[Path], int], report: Path) -> tuple[int | None, str | None]:
    try:
        return guard(report), None
    except ReportError as exc:
        return None, str(exc)


def _run_vitest_native_source(
    source: str, *, spec_suffix: str = ".ts"
) -> tuple[subprocess.CompletedProcess[str], int | None, str | None]:
    with tempfile.TemporaryDirectory(prefix="native-report-", dir=WEB / "tests" / "unit") as raw_tmp:
        tmp = Path(raw_tmp)
        spec = tmp / f"fault.test{spec_suffix}"
        report = tmp / "vitest.json"
        spec.write_text(source, encoding="utf-8")
        result = subprocess.run(
            [
                NODE,
                str(VITEST),
                "run",
                "--config=vite.config.ts",
                "--no-color",
                "--allowOnly=false",
                "--reporter=json",
                f"--outputFile={report}",
                str(spec.relative_to(WEB)),
            ],
            cwd=WEB,
            env=_child_env(),
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
        count, error = _native_guard_result(require_vitest_json, report)
    return result, count, error


def _run_vitest_native_helper_case(
    helper_source: str, spec_source: str, *, helper_suffix: str = ".ts"
) -> tuple[
    subprocess.CompletedProcess[str],
    int | None,
    str | None,
    list[dict[str, object]],
    list[dict[str, object]],
]:
    with (
        tempfile.TemporaryDirectory(prefix="native-helper-", dir=WEB / "tests" / "fixtures") as raw_helper,
        tempfile.TemporaryDirectory(prefix="native-report-", dir=WEB / "tests" / "unit") as raw_spec,
    ):
        helper_dir = Path(raw_helper)
        spec_dir = Path(raw_spec)
        helper = helper_dir / f"helper{helper_suffix}"
        spec = spec_dir / "fault.test.ts"
        report = spec_dir / "vitest.json"
        helper.write_text(helper_source, encoding="utf-8")
        helper_import = Path(os.path.relpath(helper, spec.parent)).as_posix()
        rendered_spec = spec_source.replace("__HELPER_IMPORT__", helper_import)
        spec.write_text(rendered_spec, encoding="utf-8")
        result = subprocess.run(
            [
                NODE,
                str(VITEST),
                "run",
                "--config=vite.config.ts",
                "--no-color",
                "--allowOnly=false",
                "--reporter=json",
                f"--outputFile={report}",
                str(spec.relative_to(WEB)),
            ],
            cwd=WEB,
            env=_child_env(),
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
        count, error = _native_guard_result(require_vitest_json, report)
        helper_messages = _lint_required_test_source(helper_source, file_path=str(helper.relative_to(WEB)))
        spec_messages = _lint_required_test_source(rendered_spec, file_path=str(spec.relative_to(WEB)))
    return result, count, error, helper_messages, spec_messages


def _run_playwright_native_source(
    source: str, *, retries: int = 0
) -> tuple[subprocess.CompletedProcess[str], int | None, str | None]:
    with tempfile.TemporaryDirectory(prefix="native-report-", dir=WEB / "tests" / "e2e" / "full-stack") as raw_tmp:
        tmp = Path(raw_tmp)
        spec = tmp / "fault.spec.ts"
        report = tmp / "playwright.json"
        marker = tmp / "retry-marker"
        spec.write_text(source, encoding="utf-8")
        env = {
            **_child_env(),
            "PLAYWRIGHT_BROWSERS_PATH": str(tmp / "no-browsers"),
            "PLAYWRIGHT_JSON_OUTPUT_NAME": str(report),
            "TRACEFOLD_FULL_STACK_URL": "http://127.0.0.1:1",
            "TRACEFOLD_PLAYWRIGHT_RETRY_MARKER": str(marker),
        }
        config_argument = "--config=playwright.full-stack.config.ts"
        if retries:
            config = tmp / "playwright.config.ts"
            config.write_text(
                'import { defineConfig } from "@playwright/test";\n'
                "export default defineConfig({\n"
                f"  testDir: {json.dumps(str(tmp))},\n"
                "  failOnFlakyTests: true,\n"
                "  forbidOnly: true,\n"
                "  fullyParallel: false,\n"
                "  repeatEach: 1,\n"
                f"  retries: {retries},\n"
                '  updateSnapshots: "none",\n'
                f'  reporter: [["json", {{ outputFile: {json.dumps(str(report))} }}]],\n'
                "  workers: 1,\n"
                '  projects: [{ name: "required-chromium" }],\n'
                "});\n",
                encoding="utf-8",
            )
            config_argument = f"--config={config}"
        result = subprocess.run(
            [NODE, str(PLAYWRIGHT), "test", config_argument, str(spec)],
            cwd=WEB,
            env=env,
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
        count, error = _native_guard_result(require_playwright_json, report)
    return result, count, error


def test_runtime_error_guard_fails_closed_and_keeps_allowances_case_local() -> None:
    failed = _run_vitest_fixture("tests/fixtures/runtime-error-guard/fail-closed.fixture.ts")
    failed_output = failed.stdout + failed.stderr

    assert failed.returncode != 0
    assert "Unexpected console.error in test case" in failed_output
    assert "Unexpected unhandled rejection in test case" in failed_output
    assert "Runtime error allowlists require a non-empty reason" in failed_output

    allowed = _run_vitest_fixture("tests/fixtures/runtime-error-guard/allowed-errors.fixture.ts")
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr

    global_allowlist = _run_vitest_fixture("tests/fixtures/runtime-error-guard/global-allowlist.fixture.ts")
    assert global_allowlist.returncode != 0
    assert "Runtime error allowlists are case-local" in global_allowlist.stdout + global_allowlist.stderr


def test_runtime_error_guard_fails_on_lifecycle_console_errors() -> None:
    result = _run_vitest_fixture("tests/fixtures/runtime-error-guard/after-all.fixture.ts")

    assert result.returncode != 0
    assert "Unexpected console.error outside a test case" in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (
            'test.concurrent.fails("expected failure", () => {});',
            "Required tests must be plain passes",
        ),
        (
            'test("expected failure option", { fails: true }, () => {});',
            "cannot mark an options object as an expected failure",
        ),
        (
            'test("computed expected failure", { ["fails"]: true }, () => {});',
            "cannot hide an expected failure behind a computed options key",
        ),
        (
            'const key = "fails"; test("dynamic expected failure", { [key]: true }, () => {});',
            "take exactly two arguments",
        ),
        (
            'const options = JSON.parse(\'{"fails":true}\'); test("parsed expected failure", options, () => {});',
            "take exactly two arguments",
        ),
        (
            'test["fails"]("computed member", () => {});',
            "computed expected failures",
        ),
        (
            'const modifier = "fails"; test[modifier]("dynamic member", () => {});',
            "modifiers must be statically named",
        ),
        (
            'const check = test; check("local alias", { fails: true }, () => {});',
            "cannot be aliased",
        ),
        (
            'import * as vitest from "vitest"; vitest.test("namespace", { fails: true }, () => {});',
            "namespace and dynamic runner imports",
        ),
        (
            'const check = test.extend({}); check("extended", { fails: true }, () => {});',
            "cannot derive or extend",
        ),
        (
            'const check = test.bind(null, "bound"); check({ fails: true }, () => {});',
            "cannot derive or extend",
        ),
        (
            'const source = "vitest"; const runner = await import(source);',
            "namespace and dynamic runner imports",
        ),
        (
            'const runner = await import("vite" + "st");',
            "namespace and dynamic runner imports",
        ),
        (
            'export { expect, test as check } from "vitest";',
            "cannot be aliased",
        ),
        (
            'const { fails } = test; fails("destructured member", () => {});',
            "cannot mark an options object as an expected failure",
        ),
        (
            'test.skip("disabled", () => {});',
            "Required tests must be plain passes",
        ),
        (
            'test.only("focused", () => {});',
            "Required tests must be plain passes",
        ),
        (
            'test.concurrent("retried", { retry: 1 }, () => {});',
            "Required tests cannot repeat or retry to green",
        ),
        (
            'it("repeated", { repeats: 2 }, () => {});',
            "Required tests cannot repeat or retry to green",
        ),
        (
            "test.describe.configure({ retries: 2 });",
            "Required tests cannot repeat or retry to green",
        ),
    ],
)
def test_required_frontend_test_policy_rejects_pseudo_green_syntax(source: str, message: str) -> None:
    messages = _lint_required_test_source(source)

    assert any(
        lint_message.get("severity") == 2 and message in str(lint_message.get("message")) for lint_message in messages
    ), messages


@pytest.mark.parametrize(
    "source",
    [
        'test("plain pass", () => {});',
        'it.each([1])("plain %s", (_value) => {});',
        'it.each`value\n${1}`("plain $value", ({ value: _value }) => {});',
    ],
)
def test_required_frontend_test_policy_allows_fixed_shape_tests(source: str) -> None:
    messages = _lint_required_test_source(source)

    assert messages == []


def test_shared_playwright_fixture_policy_allows_only_the_guard_factory() -> None:
    allowed = (
        'import { test as base } from "@playwright/test";\nexport const test = base.extend<{ guard: void }>({});\n'
    )
    rejected = 'import { test as base } from "@playwright/test";\nbase("hidden case", { fails: true }, () => {});\n'

    assert _lint_required_test_source(allowed, file_path="tests/e2e/fixtures.ts") == []
    messages = _lint_required_test_source(rejected, file_path="tests/e2e/fixtures.ts")
    assert any(
        message.get("ruleId") == "tracefold-required-tests/playwright-fixture-factory" and message.get("severity") == 2
        for message in messages
    ), messages


def test_real_vitest_native_report_accepts_one_plain_pass() -> None:
    source = 'import { test } from "vitest";\ntest("plain pass", () => {});\n'
    result, count, error = _run_vitest_native_source(source)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (count, error) == (1, None)
    assert _lint_required_test_source(source) == []


@pytest.mark.parametrize(
    "source",
    [
        (
            'import { expect, test } from "vitest";\n'
            'test("expected failure", { fails: true }, () => expect(1).toBe(2));\n'
        ),
        (
            'import { expect, test } from "vitest";\n'
            'const key = "fails";\n'
            'test("dynamic expected failure", { [key]: true }, () => expect(1).toBe(2));\n'
        ),
        (
            'import { expect, test } from "vitest";\n'
            "const options = JSON.parse('{\"fails\":true}');\n"
            'test("parsed expected failure", options, () => expect(1).toBe(2));\n'
        ),
        (
            'import { expect, test as check } from "vitest";\n'
            "const options = JSON.parse('{\"fails\":true}');\n"
            'check("aliased expected failure", options, () => expect(1).toBe(2));\n'
        ),
        (
            'import { expect, test } from "vitest";\n'
            "const check = test;\n"
            "const options = JSON.parse('{\"fails\":true}');\n"
            'check("local alias expected failure", options, () => expect(1).toBe(2));\n'
        ),
        (
            'import { expect } from "vitest";\n'
            'import * as vitest from "vitest";\n'
            "const options = JSON.parse('{\"fails\":true}');\n"
            'vitest.test("namespace expected failure", options, () => expect(1).toBe(2));\n'
        ),
        (
            'import { expect, test } from "vitest";\n'
            "const check = test.extend({});\n"
            "const options = JSON.parse('{\"fails\":true}');\n"
            'check("extended expected failure", options, () => expect(1).toBe(2));\n'
        ),
        (
            'import { expect, test } from "vitest";\n'
            'const check = test.bind(null, "bound expected failure");\n'
            "const options = JSON.parse('{\"fails\":true}');\n"
            "check(options, () => expect(1).toBe(2));\n"
        ),
        (
            'const moduleName = "vitest";\n'
            "const { expect, test: check } = await import(moduleName);\n"
            "const options = JSON.parse('{\"fails\":true}');\n"
            'check("dynamic import expected failure", options, () => expect(1).toBe(2));\n'
        ),
        (
            'const { expect, test: check } = await import("vite" + "st");\n'
            "const options = JSON.parse('{\"fails\":true}');\n"
            'check("computed import expected failure", options, () => expect(1).toBe(2));\n'
        ),
    ],
)
def test_real_vitest_expected_failure_option_is_stopped_by_required_lint(source: str) -> None:
    result, _, _ = _run_vitest_native_source(source)
    messages = _lint_required_test_source(source)

    # Vitest intentionally returns green for this expected failure. The
    # required-test lint is therefore a necessary part of the fixed job.
    assert result.returncode == 0, result.stdout + result.stderr
    assert any(
        message.get("ruleId") == "tracefold-required-tests/fixed-declaration" and message.get("severity") == 2
        for message in messages
    ), messages


@pytest.mark.parametrize("spec_suffix", [".js", ".mjs"])
def test_real_vitest_javascript_specs_cannot_register_a_pseudo_green_case(spec_suffix: str) -> None:
    source = (
        'import { expect, test } from "vitest";\n'
        "const options = JSON.parse('{\"fails\":true}');\n"
        'test("JavaScript expected failure", options, () => expect(1).toBe(2));\n'
    )
    result, count, error = _run_vitest_native_source(source, spec_suffix=spec_suffix)
    messages = _lint_required_test_source(source, file_path=f"tests/unit/fail-closed.fixture.test{spec_suffix}")

    assert result.returncode == 0, result.stdout + result.stderr
    assert (count, error) == (1, None)
    assert any(
        message.get("ruleId") == "tracefold-required-tests/fixed-declaration" and message.get("severity") == 2
        for message in messages
    ), messages


@pytest.mark.parametrize(
    ("helper_source", "spec_source", "helper_suffix"),
    [
        (
            (
                'import { expect, test } from "vitest";\n'
                "const options = JSON.parse('{\"fails\":true}');\n"
                'test("helper expected failure", options, () => expect(1).toBe(2));\n'
            ),
            'import "__HELPER_IMPORT__";\n',
            ".ts",
        ),
        (
            'export { expect, test as check } from "vitest";\n',
            (
                'import { check, expect } from "__HELPER_IMPORT__";\n'
                "const options = JSON.parse('{\"fails\":true}');\n"
                'check("re-export expected failure", options, () => expect(1).toBe(2));\n'
            ),
            ".ts",
        ),
        (
            (
                'import { expect, test } from "vitest";\n'
                "const options = JSON.parse('{\"fails\":true}');\n"
                'test("JavaScript helper expected failure", options, () => expect(1).toBe(2));\n'
            ),
            'import "__HELPER_IMPORT__";\n',
            ".js",
        ),
        (
            (
                'import { expect, test } from "vitest";\n'
                "const options = JSON.parse('{\"fails\":true}');\n"
                'test("ES module helper expected failure", options, () => expect(1).toBe(2));\n'
            ),
            'import "__HELPER_IMPORT__";\n',
            ".mjs",
        ),
    ],
)
def test_real_vitest_imported_helpers_cannot_register_a_pseudo_green_case(
    helper_source: str, spec_source: str, helper_suffix: str
) -> None:
    result, count, error, helper_messages, spec_messages = _run_vitest_native_helper_case(
        helper_source, spec_source, helper_suffix=helper_suffix
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (count, error) == (1, None)
    assert any(
        message.get("ruleId") == "tracefold-required-tests/fixed-declaration" and message.get("severity") == 2
        for message in helper_messages
    ), helper_messages
    assert not [message for message in spec_messages if message.get("severity") == 2], spec_messages


@pytest.mark.parametrize(
    "source",
    [
        'import { test } from "vitest";\ntest.skip("disabled", () => {});\n',
        'import { test } from "vitest";\n',
        (
            'import { expect, test } from "vitest";\n'
            "let attempts = 0;\n"
            'test("retried", { retry: 1 }, () => { attempts += 1; expect(attempts).toBe(2); });\n'
        ),
    ],
)
def test_real_vitest_faults_cannot_pass_the_required_pipeline(source: str) -> None:
    result, _, guard_error = _run_vitest_native_source(source)
    lint_errors = [message for message in _lint_required_test_source(source) if message.get("severity") == 2]

    assert result.returncode != 0 or guard_error is not None or lint_errors, result.stdout + result.stderr


def test_real_playwright_native_report_accepts_one_plain_pass_without_a_browser() -> None:
    source = 'import { test } from "@playwright/test";\ntest("plain pass", async () => {});\n'
    result, count, error = _run_playwright_native_source(source)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (count, error) == (1, None)
    assert _lint_required_test_source(source) == []


@pytest.mark.parametrize(
    "source",
    [
        'import { test } from "@playwright/test";\ntest.skip("disabled", async () => {});\n',
        (
            'import { expect, test } from "@playwright/test";\n'
            'test("expected failure", async () => { test.fail(); expect(1).toBe(2); });\n'
        ),
        'import { test } from "@playwright/test";\n',
        'throw new Error("module fault");\n',
    ],
)
def test_real_playwright_faults_cannot_pass_the_required_pipeline(source: str) -> None:
    result, _, guard_error = _run_playwright_native_source(source)
    lint_errors = [message for message in _lint_required_test_source(source) if message.get("severity") == 2]

    assert result.returncode != 0 or guard_error is not None or lint_errors, result.stdout + result.stderr


def test_real_playwright_flaky_retry_is_non_green() -> None:
    source = """
import { existsSync, writeFileSync } from "node:fs";
import { expect, test } from "@playwright/test";
const marker = process.env.TRACEFOLD_PLAYWRIGHT_RETRY_MARKER!;
test("flaky retry", async () => {
  if (!existsSync(marker)) {
    writeFileSync(marker, "first failure");
    expect(1).toBe(2);
  }
  expect(1).toBe(1);
});
"""
    result, _, guard_error = _run_playwright_native_source(source, retries=1)

    assert result.returncode != 0, result.stdout + result.stderr
    assert guard_error is not None and "retry_policy_invalid" in guard_error
