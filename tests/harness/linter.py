"""Hadolint wrapper for Dockerfile linting."""

import asyncio
import json
import re
from dataclasses import dataclass, field
from enum import Enum


class LintSeverity(str, Enum):
    """Lint rule severity levels."""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"
    STYLE = "style"


@dataclass
class LintIssue:
    """A single lint issue."""

    rule: str
    severity: LintSeverity
    line: int
    column: int
    message: str
    file: str = "Dockerfile"


@dataclass
class LintResult:
    """Result of linting a Dockerfile."""

    success: bool
    issues: list[LintIssue] = field(default_factory=list)
    error: str | None = None
    hadolint_available: bool = True

    @property
    def error_count(self) -> int:
        """Count of error-level issues."""
        return sum(1 for i in self.issues if i.severity == LintSeverity.ERROR)

    @property
    def warning_count(self) -> int:
        """Count of warning-level issues."""
        return sum(1 for i in self.issues if i.severity == LintSeverity.WARNING)

    def has_blocking_issues(self) -> bool:
        """Check if there are issues that should block conversion."""
        return self.error_count > 0


class HadolintRunner:
    """Run hadolint on Dockerfiles.

    Hadolint is a Dockerfile linter that validates best practices.
    If hadolint is not installed, falls back to basic validation.
    """

    # Rules to ignore for Chainguard images
    IGNORED_RULES = [
        "DL3007",  # Using latest tag - we use :latest deliberately
        "DL3018",  # Pin versions in apk add - not always needed
        "DL3059",  # Multiple consecutive RUN - sometimes needed for clarity
    ]

    # Trusted registries
    TRUSTED_REGISTRIES = [
        "cgr.dev",
        "ghcr.io",
    ]

    def __init__(
        self,
        ignored_rules: list[str] | None = None,
        trusted_registries: list[str] | None = None,
    ):
        """Initialize linter.

        Args:
            ignored_rules: Additional rules to ignore
            trusted_registries: Additional trusted registries
        """
        self.ignored_rules = list(self.IGNORED_RULES)
        if ignored_rules:
            self.ignored_rules.extend(ignored_rules)

        self.trusted_registries = list(self.TRUSTED_REGISTRIES)
        if trusted_registries:
            self.trusted_registries.extend(trusted_registries)

        self._hadolint_available: bool | None = None

    async def check_hadolint_available(self) -> bool:
        """Check if hadolint is installed.

        Returns:
            True if hadolint is available
        """
        if self._hadolint_available is not None:
            return self._hadolint_available

        try:
            proc = await asyncio.create_subprocess_exec(
                "hadolint",
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            self._hadolint_available = proc.returncode == 0
        except FileNotFoundError:
            self._hadolint_available = False

        return self._hadolint_available

    async def lint(self, dockerfile_content: str) -> LintResult:
        """Lint a Dockerfile.

        Args:
            dockerfile_content: Dockerfile content to lint

        Returns:
            LintResult with issues found
        """
        if await self.check_hadolint_available():
            return await self._lint_with_hadolint(dockerfile_content)
        else:
            return self._lint_basic(dockerfile_content)

    async def _lint_with_hadolint(self, dockerfile_content: str) -> LintResult:
        """Lint using hadolint.

        Args:
            dockerfile_content: Dockerfile content

        Returns:
            LintResult from hadolint
        """
        # Build command with options
        args = ["hadolint", "--format", "json"]

        # Add ignored rules
        for rule in self.ignored_rules:
            args.extend(["--ignore", rule])

        # Add trusted registries
        for registry in self.trusted_registries:
            args.extend(["--trusted-registry", registry])

        # Read from stdin
        args.append("-")

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await asyncio.wait_for(
                proc.communicate(dockerfile_content.encode()),
                timeout=30,
            )

            # Parse JSON output
            issues = self._parse_hadolint_output(stdout.decode())

            # hadolint returns non-zero if issues found
            success = not any(i.severity == LintSeverity.ERROR for i in issues)

            return LintResult(
                success=success,
                issues=issues,
                hadolint_available=True,
            )

        except asyncio.TimeoutError:
            return LintResult(
                success=False,
                error="Hadolint timed out",
                hadolint_available=True,
            )
        except Exception as e:
            return LintResult(
                success=False,
                error=f"Hadolint error: {e}",
                hadolint_available=True,
            )

    def _parse_hadolint_output(self, output: str) -> list[LintIssue]:
        """Parse hadolint JSON output.

        Args:
            output: JSON output from hadolint

        Returns:
            List of lint issues
        """
        if not output.strip():
            return []

        try:
            data = json.loads(output)
        except json.JSONDecodeError:
            return []

        issues = []
        for item in data:
            severity_str = item.get("level", "warning").lower()
            try:
                severity = LintSeverity(severity_str)
            except ValueError:
                severity = LintSeverity.WARNING

            issues.append(
                LintIssue(
                    rule=item.get("code", "unknown"),
                    severity=severity,
                    line=item.get("line", 0),
                    column=item.get("column", 0),
                    message=item.get("message", ""),
                    file=item.get("file", "Dockerfile"),
                )
            )

        return issues

    def _lint_basic(self, dockerfile_content: str) -> LintResult:
        """Basic linting without hadolint.

        Checks for common issues that don't require the full linter.

        Args:
            dockerfile_content: Dockerfile content

        Returns:
            LintResult with basic checks
        """
        issues: list[LintIssue] = []
        lines = dockerfile_content.split("\n")

        has_from = False
        last_user = None

        for line_num, line in enumerate(lines, 1):
            stripped = line.strip()

            # Skip empty lines and comments
            if not stripped or stripped.startswith("#"):
                continue

            # Check for FROM instruction
            if stripped.upper().startswith("FROM "):
                has_from = True

                # Check for latest tag (info only)
                if ":latest" in stripped.lower() or (
                    " AS " not in stripped.upper()
                    and ":" not in stripped.split()[1]
                ):
                    # Only warn if not a trusted registry
                    image_part = stripped.split()[1]
                    if not any(
                        image_part.startswith(reg) for reg in self.trusted_registries
                    ):
                        issues.append(
                            LintIssue(
                                rule="DL3007",
                                severity=LintSeverity.WARNING,
                                line=line_num,
                                column=1,
                                message="Using latest is prone to errors. Pin the version.",
                            )
                        )

            # Track USER instructions
            if stripped.upper().startswith("USER "):
                last_user = stripped.split()[1] if len(stripped.split()) > 1 else None

            # Check for apt-get without -y
            if re.search(r"apt-get\s+install\b", stripped, re.IGNORECASE):
                if "-y" not in stripped and "--yes" not in stripped:
                    issues.append(
                        LintIssue(
                            rule="DL3014",
                            severity=LintSeverity.WARNING,
                            line=line_num,
                            column=1,
                            message="Use apt-get install -y to avoid manual input.",
                        )
                    )

            # Check for curl/wget without fail flags
            if re.search(r"\bcurl\b", stripped) and "-f" not in stripped:
                issues.append(
                    LintIssue(
                        rule="DL4006",
                        severity=LintSeverity.WARNING,
                        line=line_num,
                        column=1,
                        message="Use curl -f to fail on HTTP errors.",
                    )
                )

            # Check for WORKDIR with relative path
            if stripped.upper().startswith("WORKDIR "):
                path = stripped.split(maxsplit=1)[1] if len(stripped.split()) > 1 else ""
                if path and not path.startswith("/") and not path.startswith("$"):
                    issues.append(
                        LintIssue(
                            rule="DL3000",
                            severity=LintSeverity.WARNING,
                            line=line_num,
                            column=1,
                            message="Use absolute WORKDIR.",
                        )
                    )

            # Check for ADD instead of COPY
            if stripped.upper().startswith("ADD "):
                # ADD is ok for URLs and tar files
                content = stripped[4:].strip()
                if not content.startswith("http") and ".tar" not in content.lower():
                    issues.append(
                        LintIssue(
                            rule="DL3020",
                            severity=LintSeverity.INFO,
                            line=line_num,
                            column=1,
                            message="Use COPY instead of ADD for files/folders.",
                        )
                    )

        # Must have at least one FROM
        if not has_from:
            issues.append(
                LintIssue(
                    rule="DL3006",
                    severity=LintSeverity.ERROR,
                    line=1,
                    column=1,
                    message="Dockerfile must have at least one FROM instruction.",
                )
            )

        success = not any(i.severity == LintSeverity.ERROR for i in issues)

        return LintResult(
            success=success,
            issues=issues,
            hadolint_available=False,
        )


async def lint_dockerfile(
    content: str,
    ignored_rules: list[str] | None = None,
) -> LintResult:
    """Convenience function to lint a Dockerfile.

    Args:
        content: Dockerfile content
        ignored_rules: Rules to ignore

    Returns:
        LintResult
    """
    runner = HadolintRunner(ignored_rules=ignored_rules)
    return await runner.lint(content)


def format_lint_issues(result: LintResult) -> str:
    """Format lint issues for display.

    Args:
        result: LintResult to format

    Returns:
        Formatted string
    """
    if not result.issues:
        return "No lint issues found."

    lines = []
    for issue in sorted(result.issues, key=lambda i: (i.line, i.column)):
        severity_icon = {
            LintSeverity.ERROR: "[E]",
            LintSeverity.WARNING: "[W]",
            LintSeverity.INFO: "[I]",
            LintSeverity.STYLE: "[S]",
        }.get(issue.severity, "[?]")

        lines.append(
            f"{severity_icon} Line {issue.line}: {issue.rule} - {issue.message}"
        )

    return "\n".join(lines)
