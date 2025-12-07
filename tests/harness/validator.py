"""Validation pipeline for Dockerfile conversion testing."""

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tests.harness.converter import DockerfileConverter
from tests.harness.docker_helpers import DockerClient, run_runtime_test, temp_dockerfile
from tests.harness.linter import HadolintRunner, LintResult
from tests.harness.mcp_client import MCPTestClient
from tests.harness.models import (
    BuildResult,
    ConversionResult,
    DockerfileFixture,
    RuntimeResult,
)


@dataclass
class ValidationConfig:
    """Configuration for validation pipeline.

    Controls which validation steps are executed.
    """

    # Pre-conversion validation
    build_original: bool = True

    # Conversion
    convert: bool = True
    verify_images: bool = True

    # Post-conversion validation
    lint_converted: bool = True
    build_converted: bool = True
    run_converted: bool = True

    # Cleanup
    cleanup_images: bool = True

    # Timeouts
    build_timeout: int = 600  # 10 minutes
    run_timeout: int = 60


@dataclass
class ValidationResult:
    """Result of full validation pipeline."""

    fixture_name: str
    success: bool

    # Pre-conversion
    original_build: BuildResult | None = None

    # Conversion
    conversion: ConversionResult | None = None

    # Post-conversion
    converted_lint: LintResult | None = None
    converted_build: BuildResult | None = None
    converted_runtime: RuntimeResult | None = None

    # Metadata
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped_steps: list[str] = field(default_factory=list)
    mcp_calls: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> str:
        """Generate a summary of the validation.

        Returns:
            Human-readable summary
        """
        lines = [f"Validation: {self.fixture_name}"]
        lines.append("=" * 40)

        # Original build
        if self.original_build:
            status = "PASS" if self.original_build.success else "FAIL"
            lines.append(f"Original Build: {status}")
            if not self.original_build.success and self.original_build.errors:
                for err in self.original_build.errors[:3]:
                    lines.append(f"  - {err[:100]}")

        # Conversion
        if self.conversion:
            status = "PASS" if self.conversion.success else "FAIL"
            lines.append(f"Conversion: {status}")
            if self.conversion.warnings:
                for warn in self.conversion.warnings[:3]:
                    lines.append(f"  - {warn[:100]}")

        # Lint
        if self.converted_lint:
            status = "PASS" if self.converted_lint.success else "FAIL"
            issues = f"({self.converted_lint.error_count}E/{self.converted_lint.warning_count}W)"
            lines.append(f"Lint Check: {status} {issues}")

        # Converted build
        if self.converted_build:
            status = "PASS" if self.converted_build.success else "FAIL"
            lines.append(f"Converted Build: {status}")
            if not self.converted_build.success and self.converted_build.errors:
                for err in self.converted_build.errors[:3]:
                    lines.append(f"  - {err[:100]}")

        # Runtime
        if self.converted_runtime:
            status = "PASS" if self.converted_runtime.success else "FAIL"
            lines.append(f"Runtime Test: {status}")

        # Skipped
        if self.skipped_steps:
            lines.append(f"Skipped: {', '.join(self.skipped_steps)}")

        # Overall
        overall = "SUCCESS" if self.success else "FAILURE"
        lines.append("-" * 40)
        lines.append(f"Overall: {overall}")

        return "\n".join(lines)


class FixtureValidator:
    """Validates Dockerfile conversion for a fixture.

    Orchestrates the full pipeline:
    1. Build original Dockerfile (pre-conversion)
    2. Convert using MCP tools
    3. Lint converted Dockerfile
    4. Build converted Dockerfile
    5. Run container and health check
    """

    def __init__(
        self,
        docker: DockerClient | None = None,
        mcp: MCPTestClient | None = None,
        linter: HadolintRunner | None = None,
        config: ValidationConfig | None = None,
    ):
        """Initialize validator.

        Args:
            docker: Docker client
            mcp: MCP test client
            linter: Hadolint runner
            config: Validation configuration
        """
        self.docker = docker or DockerClient()
        self.mcp = mcp or MCPTestClient()
        self.linter = linter or HadolintRunner()
        self.config = config or ValidationConfig()

        self._images_to_cleanup: list[str] = []

    async def validate(self, fixture: DockerfileFixture) -> ValidationResult:
        """Run full validation pipeline on a fixture.

        Args:
            fixture: Fixture to validate

        Returns:
            ValidationResult with all outcomes
        """
        result = ValidationResult(
            fixture_name=fixture.metadata.name,
            success=True,
        )

        try:
            # Step 1: Build original Dockerfile
            if self.config.build_original:
                original_build = await self._build_original(fixture)
                result.original_build = original_build

                if not original_build.success:
                    result.success = False
                    result.errors.append(
                        f"Original Dockerfile failed to build: {original_build.errors}"
                    )
                    # Don't continue if original doesn't build
                    return result
            else:
                result.skipped_steps.append("original_build")

            # Step 2: Convert Dockerfile
            if self.config.convert:
                conversion = await self._convert(fixture)
                result.conversion = conversion
                result.mcp_calls = conversion.mcp_calls

                if not conversion.success:
                    result.success = False
                    result.errors.append(
                        f"Conversion failed: {conversion.errors}"
                    )
                    return result

                result.warnings.extend(conversion.warnings)
            else:
                result.skipped_steps.append("convert")
                return result  # Can't continue without conversion

            # Step 3: Lint converted Dockerfile
            if self.config.lint_converted and conversion.converted_dockerfile:
                lint_result = await self._lint(conversion.converted_dockerfile)
                result.converted_lint = lint_result

                if lint_result.has_blocking_issues():
                    result.success = False
                    result.errors.append(
                        f"Lint failed with {lint_result.error_count} errors"
                    )
                    # Continue to try building anyway
            else:
                result.skipped_steps.append("lint")

            # Step 4: Build converted Dockerfile
            if self.config.build_converted and conversion.converted_dockerfile:
                converted_build = await self._build_converted(
                    fixture, conversion.converted_dockerfile
                )
                result.converted_build = converted_build

                if not converted_build.success:
                    result.success = False
                    result.errors.append(
                        f"Converted Dockerfile failed to build: {converted_build.errors}"
                    )
                    return result
            else:
                result.skipped_steps.append("converted_build")

            # Step 5: Run container and health check
            if (
                self.config.run_converted
                and result.converted_build
                and result.converted_build.success
                and fixture.metadata.runtime.enabled
            ):
                runtime_result = await self._run_container(
                    fixture, result.converted_build.image_tag
                )
                result.converted_runtime = runtime_result

                if not runtime_result.success:
                    result.success = False
                    result.errors.append(
                        f"Runtime test failed: {runtime_result.errors}"
                    )
            else:
                result.skipped_steps.append("runtime")

        except Exception as e:
            result.success = False
            result.errors.append(f"Unexpected error: {e}")

        finally:
            # Cleanup
            if self.config.cleanup_images:
                await self._cleanup()

        return result

    async def _build_original(self, fixture: DockerfileFixture) -> BuildResult:
        """Build the original Dockerfile.

        Args:
            fixture: Fixture to build

        Returns:
            BuildResult
        """
        tag = f"dfc-test-original-{fixture.metadata.name}"

        result = await self.docker.build(
            context_path=fixture.path,
            dockerfile=fixture.metadata.build.dockerfile,
            tag=tag,
            build_args=fixture.metadata.build.build_args,
            target=fixture.metadata.build.target,
        )

        if result.success and result.image_tag:
            self._images_to_cleanup.append(result.image_tag)

        return result

    async def _convert(self, fixture: DockerfileFixture) -> ConversionResult:
        """Convert the Dockerfile using MCP tools.

        Args:
            fixture: Fixture to convert

        Returns:
            ConversionResult
        """
        converter = DockerfileConverter(mcp_client=self.mcp)

        return await converter.convert(
            dockerfile_content=fixture.dockerfile_content,
            source_distro=fixture.metadata.source.package_manager or "auto",
            verify_images=self.config.verify_images,
        )

    async def _lint(self, dockerfile_content: str) -> LintResult:
        """Lint the converted Dockerfile.

        Args:
            dockerfile_content: Converted Dockerfile content

        Returns:
            LintResult
        """
        return await self.linter.lint(dockerfile_content)

    async def _build_converted(
        self,
        fixture: DockerfileFixture,
        converted_content: str,
    ) -> BuildResult:
        """Build the converted Dockerfile.

        Args:
            fixture: Original fixture (for source files)
            converted_content: Converted Dockerfile content

        Returns:
            BuildResult
        """
        tag = f"dfc-test-converted-{fixture.metadata.name}"

        # Write converted Dockerfile to fixture directory temporarily
        converted_path = fixture.path / "Dockerfile.converted"
        try:
            converted_path.write_text(converted_content)

            result = await self.docker.build(
                context_path=fixture.path,
                dockerfile="Dockerfile.converted",
                tag=tag,
                build_args=fixture.metadata.build.build_args,
                target=fixture.metadata.build.target,
            )

            if result.success and result.image_tag:
                self._images_to_cleanup.append(result.image_tag)

            return result

        finally:
            # Clean up temporary file
            if converted_path.exists():
                converted_path.unlink()

    async def _run_container(
        self,
        fixture: DockerfileFixture,
        image_tag: str,
    ) -> RuntimeResult:
        """Run the converted container and health check.

        Args:
            fixture: Fixture with runtime config
            image_tag: Image to run

        Returns:
            RuntimeResult
        """
        return await run_runtime_test(
            self.docker,
            image_tag,
            fixture.metadata.runtime,
        )

    async def _cleanup(self) -> None:
        """Clean up created images."""
        for image in self._images_to_cleanup:
            try:
                await self.docker.remove_image(image, force=True)
            except Exception:
                pass  # Best effort cleanup

        self._images_to_cleanup.clear()


async def validate_fixture(
    fixture: DockerfileFixture,
    config: ValidationConfig | None = None,
    mcp: MCPTestClient | None = None,
) -> ValidationResult:
    """Convenience function to validate a single fixture.

    Args:
        fixture: Fixture to validate
        config: Validation configuration
        mcp: MCP client (uses real client if not provided)

    Returns:
        ValidationResult
    """
    validator = FixtureValidator(config=config, mcp=mcp)
    return await validator.validate(fixture)


async def validate_fixtures(
    fixtures: list[DockerfileFixture],
    config: ValidationConfig | None = None,
    mcp: MCPTestClient | None = None,
    parallel: bool = False,
) -> list[ValidationResult]:
    """Validate multiple fixtures.

    Args:
        fixtures: Fixtures to validate
        config: Validation configuration
        mcp: MCP client
        parallel: Run validations in parallel

    Returns:
        List of ValidationResults
    """
    validator = FixtureValidator(config=config, mcp=mcp)

    if parallel:
        # Run all validations concurrently
        tasks = [validator.validate(f) for f in fixtures]
        return await asyncio.gather(*tasks)
    else:
        # Run sequentially
        results = []
        for fixture in fixtures:
            result = await validator.validate(fixture)
            results.append(result)
        return results


def compare_dockerfiles(original: str, converted: str) -> dict[str, Any]:
    """Compare original and converted Dockerfiles.

    Args:
        original: Original Dockerfile content
        converted: Converted Dockerfile content

    Returns:
        Comparison dict with changes
    """
    original_lines = original.strip().split("\n")
    converted_lines = converted.strip().split("\n")

    changes = {
        "lines_original": len(original_lines),
        "lines_converted": len(converted_lines),
        "from_changes": [],
        "run_changes": [],
        "other_changes": [],
    }

    # Simple line-by-line comparison
    for i, (orig, conv) in enumerate(
        zip(original_lines, converted_lines, strict=False)
    ):
        if orig.strip() != conv.strip():
            if orig.strip().upper().startswith("FROM"):
                changes["from_changes"].append(
                    {"line": i + 1, "original": orig, "converted": conv}
                )
            elif orig.strip().upper().startswith("RUN"):
                changes["run_changes"].append(
                    {"line": i + 1, "original": orig, "converted": conv}
                )
            else:
                changes["other_changes"].append(
                    {"line": i + 1, "original": orig, "converted": conv}
                )

    return changes
