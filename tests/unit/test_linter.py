"""Tests for Dockerfile linter."""

import pytest

from tests.harness.linter import (
    HadolintRunner,
    LintResult,
    LintSeverity,
    format_lint_issues,
    lint_dockerfile,
)


class TestBasicLinter:
    """Tests for basic linting (without hadolint)."""

    @pytest.fixture
    def linter(self):
        """Get a linter instance."""
        runner = HadolintRunner()
        runner._hadolint_available = False  # Force basic linting
        return runner

    @pytest.mark.asyncio
    async def test_lint_valid_dockerfile(self, linter):
        """Test linting a valid Dockerfile."""
        dockerfile = """FROM python:3.11-slim
WORKDIR /app
COPY . .
CMD ["python", "app.py"]
"""
        result = await linter.lint(dockerfile)
        assert result.success
        assert result.error_count == 0

    @pytest.mark.asyncio
    async def test_lint_missing_from(self, linter):
        """Test linting Dockerfile without FROM."""
        dockerfile = """WORKDIR /app
COPY . .
"""
        result = await linter.lint(dockerfile)
        assert not result.success
        assert result.error_count > 0

    @pytest.mark.asyncio
    async def test_lint_relative_workdir(self, linter):
        """Test warning for relative WORKDIR."""
        dockerfile = """FROM python:3.11
WORKDIR app
COPY . .
"""
        result = await linter.lint(dockerfile)
        assert result.warning_count > 0
        assert any("WORKDIR" in i.message for i in result.issues)

    @pytest.mark.asyncio
    async def test_lint_curl_without_fail(self, linter):
        """Test warning for curl without -f flag."""
        dockerfile = """FROM python:3.11
RUN curl https://example.com/script.sh | sh
"""
        result = await linter.lint(dockerfile)
        assert result.warning_count > 0
        assert any("curl" in i.message.lower() for i in result.issues)

    @pytest.mark.asyncio
    async def test_lint_add_vs_copy(self, linter):
        """Test info about ADD vs COPY."""
        dockerfile = """FROM python:3.11
ADD app.py /app/
"""
        result = await linter.lint(dockerfile)
        assert any(i.rule == "DL3020" for i in result.issues)

    @pytest.mark.asyncio
    async def test_trusted_registry_no_latest_warning(self, linter):
        """Test that trusted registries don't warn about latest."""
        dockerfile = """FROM cgr.dev/chainguard/python:latest
WORKDIR /app
"""
        result = await linter.lint(dockerfile)
        # Should not warn about :latest for trusted registries
        assert not any(
            i.rule == "DL3007" and "cgr.dev" in i.message for i in result.issues
        )


class TestLintResult:
    """Tests for LintResult class."""

    def test_error_count(self):
        """Test error counting."""
        from tests.harness.linter import LintIssue

        result = LintResult(
            success=False,
            issues=[
                LintIssue(
                    rule="DL3006",
                    severity=LintSeverity.ERROR,
                    line=1,
                    column=1,
                    message="Error",
                ),
                LintIssue(
                    rule="DL3007",
                    severity=LintSeverity.WARNING,
                    line=2,
                    column=1,
                    message="Warning",
                ),
            ],
        )
        assert result.error_count == 1
        assert result.warning_count == 1

    def test_has_blocking_issues(self):
        """Test blocking issue detection."""
        from tests.harness.linter import LintIssue

        result_with_errors = LintResult(
            success=False,
            issues=[
                LintIssue(
                    rule="DL3006",
                    severity=LintSeverity.ERROR,
                    line=1,
                    column=1,
                    message="Error",
                ),
            ],
        )
        assert result_with_errors.has_blocking_issues()

        result_warnings_only = LintResult(
            success=True,
            issues=[
                LintIssue(
                    rule="DL3007",
                    severity=LintSeverity.WARNING,
                    line=1,
                    column=1,
                    message="Warning",
                ),
            ],
        )
        assert not result_warnings_only.has_blocking_issues()


class TestFormatLintIssues:
    """Tests for lint issue formatting."""

    def test_format_no_issues(self):
        """Test formatting with no issues."""
        result = LintResult(success=True, issues=[])
        output = format_lint_issues(result)
        assert "No lint issues found" in output

    def test_format_with_issues(self):
        """Test formatting with issues."""
        from tests.harness.linter import LintIssue

        result = LintResult(
            success=False,
            issues=[
                LintIssue(
                    rule="DL3006",
                    severity=LintSeverity.ERROR,
                    line=1,
                    column=1,
                    message="Missing FROM",
                ),
            ],
        )
        output = format_lint_issues(result)
        assert "[E]" in output
        assert "Line 1" in output
        assert "DL3006" in output


class TestLintDockerfileConvenience:
    """Tests for convenience function."""

    @pytest.mark.asyncio
    async def test_lint_dockerfile_function(self):
        """Test the convenience function."""
        dockerfile = """FROM python:3.11
WORKDIR /app
"""
        result = await lint_dockerfile(dockerfile)
        assert isinstance(result, LintResult)
