"""Test harness for Dockerfile conversion testing."""

from tests.harness.converter import DockerfileConverter
from tests.harness.docker_helpers import DockerClient, running_container, temp_dockerfile
from tests.harness.dockerfile_parser import parse_dockerfile
from tests.harness.linter import HadolintRunner
from tests.harness.linter import LintResult as LinterLintResult
from tests.harness.linter import lint_dockerfile
from tests.harness.loader import (
    discover_fixtures,
    load_fixture,
    load_fixture_by_name,
)
from tests.harness.mcp_client import MCPTestClient, MockMCPClient
from tests.harness.models import (
    BuildResult,
    ConversionResult,
    DockerfileFixture,
    FixtureMetadata,
    LintResult,
    ParsedDockerfile,
    RuntimeResult,
    ValidationResult,
)
from tests.harness.validator import (
    FixtureValidator,
    ValidationConfig,
    validate_fixture,
    validate_fixtures,
)

__all__ = [
    # Converter
    "DockerfileConverter",
    # Docker helpers
    "DockerClient",
    "running_container",
    "temp_dockerfile",
    # Dockerfile parser
    "parse_dockerfile",
    # Linter
    "HadolintRunner",
    "LinterLintResult",
    "lint_dockerfile",
    # Loader
    "discover_fixtures",
    "load_fixture",
    "load_fixture_by_name",
    # MCP client
    "MCPTestClient",
    "MockMCPClient",
    # Models
    "BuildResult",
    "ConversionResult",
    "DockerfileFixture",
    "FixtureMetadata",
    "LintResult",
    "ParsedDockerfile",
    "RuntimeResult",
    "ValidationResult",
    # Validator
    "FixtureValidator",
    "ValidationConfig",
    "validate_fixture",
    "validate_fixtures",
]
