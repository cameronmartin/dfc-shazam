"""Pytest configuration and fixtures for dfc-shazam tests."""

from pathlib import Path
from typing import AsyncIterator

import pytest

from tests.harness.docker_helpers import DockerClient
from tests.harness.linter import HadolintRunner
from tests.harness.loader import (
    FIXTURES_DIR,
    discover_fixtures,
    load_fixture,
    load_fixture_by_name,
)
from tests.harness.mcp_client import MCPTestClient, MockMCPClient
from tests.harness.models import DockerfileFixture
from tests.harness.validator import FixtureValidator, ValidationConfig


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add custom command-line options."""
    parser.addoption(
        "--run-slow",
        action="store_true",
        default=False,
        help="Run slow tests (Docker builds)",
    )
    parser.addoption(
        "--run-e2e",
        action="store_true",
        default=False,
        help="Run end-to-end tests",
    )
    parser.addoption(
        "--mock-mcp",
        action="store_true",
        default=False,
        help="Use mock MCP client instead of real tools",
    )
    parser.addoption(
        "--no-cleanup",
        action="store_true",
        default=False,
        help="Don't cleanup Docker images after tests",
    )
    parser.addoption(
        "--fixture",
        action="store",
        default=None,
        help="Run tests for a specific fixture name",
    )
    parser.addoption(
        "--fixture-language",
        action="store",
        default=None,
        help="Run tests for fixtures of a specific language",
    )
    parser.addoption(
        "--fixture-tag",
        action="store",
        default=None,
        help="Run tests for fixtures with a specific tag",
    )


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest markers."""
    config.addinivalue_line("markers", "slow: marks tests as slow")
    config.addinivalue_line("markers", "docker: marks tests requiring Docker")
    config.addinivalue_line("markers", "chainctl: marks tests requiring chainctl auth")
    config.addinivalue_line("markers", "e2e: marks end-to-end tests")


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    """Skip tests based on markers and options."""
    run_slow = config.getoption("--run-slow")
    run_e2e = config.getoption("--run-e2e")

    skip_slow = pytest.mark.skip(reason="need --run-slow option to run")
    skip_e2e = pytest.mark.skip(reason="need --run-e2e option to run")

    for item in items:
        if "slow" in item.keywords and not run_slow:
            item.add_marker(skip_slow)
        if "e2e" in item.keywords and not run_e2e:
            item.add_marker(skip_e2e)


# -----------------------------------------------------------------------------
# Session-scoped fixtures
# -----------------------------------------------------------------------------


@pytest.fixture(scope="session")
def docker_client() -> DockerClient:
    """Get a Docker client for the test session."""
    return DockerClient(timeout=600)


@pytest.fixture(scope="session")
def linter() -> HadolintRunner:
    """Get a Hadolint runner for the test session."""
    return HadolintRunner()


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    """Get the fixtures directory path."""
    return FIXTURES_DIR


# -----------------------------------------------------------------------------
# Function-scoped fixtures
# -----------------------------------------------------------------------------


@pytest.fixture
def use_mock_mcp(request: pytest.FixtureRequest) -> bool:
    """Check if mock MCP should be used."""
    return request.config.getoption("--mock-mcp")


@pytest.fixture
def no_cleanup(request: pytest.FixtureRequest) -> bool:
    """Check if cleanup should be skipped."""
    return request.config.getoption("--no-cleanup")


@pytest.fixture
def mcp_client(use_mock_mcp: bool) -> MCPTestClient:
    """Get an MCP client (mock or real based on options)."""
    if use_mock_mcp:
        mock = MockMCPClient()
        mock.setup_default_mappings()
        return mock
    return MCPTestClient()


@pytest.fixture
def mock_mcp_client() -> MockMCPClient:
    """Get a mock MCP client with default mappings."""
    mock = MockMCPClient()
    mock.setup_default_mappings()
    return mock


@pytest.fixture
def validation_config(no_cleanup: bool) -> ValidationConfig:
    """Get validation configuration."""
    return ValidationConfig(
        cleanup_images=not no_cleanup,
    )


@pytest.fixture
def validator(
    docker_client: DockerClient,
    mcp_client: MCPTestClient,
    linter: HadolintRunner,
    validation_config: ValidationConfig,
) -> FixtureValidator:
    """Get a configured fixture validator."""
    return FixtureValidator(
        docker=docker_client,
        mcp=mcp_client,
        linter=linter,
        config=validation_config,
    )


# -----------------------------------------------------------------------------
# Fixture parametrization helpers
# -----------------------------------------------------------------------------


def get_fixture_params(
    fixture_name: str | None = None,
    language: str | None = None,
    tag: str | None = None,
) -> list[str]:
    """Get fixture names for parametrization.

    Args:
        fixture_name: Specific fixture name
        language: Filter by language
        tag: Filter by tag

    Returns:
        List of fixture names
    """
    if fixture_name:
        return [fixture_name]

    params = []
    tags = [tag] if tag else None
    languages = [language] if language else None

    for fixture_dir in discover_fixtures(tags=tags, languages=languages):
        try:
            fixture = load_fixture(fixture_dir)
            params.append(fixture.metadata.name)
        except Exception:
            continue

    return params


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Generate test parameters for fixtures."""
    if "dockerfile_fixture" in metafunc.fixturenames:
        fixture_name = metafunc.config.getoption("--fixture")
        language = metafunc.config.getoption("--fixture-language")
        tag = metafunc.config.getoption("--fixture-tag")

        params = get_fixture_params(fixture_name, language, tag)

        if params:
            metafunc.parametrize(
                "dockerfile_fixture",
                params,
                indirect=True,
            )


@pytest.fixture
def dockerfile_fixture(request: pytest.FixtureRequest) -> DockerfileFixture:
    """Load a Dockerfile fixture by name.

    This fixture is parametrized by pytest_generate_tests.
    """
    fixture_name = request.param
    return load_fixture_by_name(fixture_name)


# -----------------------------------------------------------------------------
# Sample fixtures for quick testing
# -----------------------------------------------------------------------------


@pytest.fixture
def sample_dockerfile() -> str:
    """Get a simple sample Dockerfile for testing."""
    return """FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \\
    build-essential \\
    libpq-dev \\
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["python", "app.py"]
"""


@pytest.fixture
def sample_multistage_dockerfile() -> str:
    """Get a multi-stage sample Dockerfile."""
    return """FROM golang:1.21 AS builder

WORKDIR /app

COPY go.mod go.sum ./
RUN go mod download

COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -o /app/server

FROM scratch

COPY --from=builder /app/server /server

ENTRYPOINT ["/server"]
"""


@pytest.fixture
def sample_node_dockerfile() -> str:
    """Get a sample Node.js Dockerfile."""
    return """FROM node:20-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \\
    python3 \\
    make \\
    g++ \\
    && rm -rf /var/lib/apt/lists/*

COPY package*.json ./
RUN npm ci --only=production

COPY . .

EXPOSE 3000

CMD ["node", "server.js"]
"""


# -----------------------------------------------------------------------------
# Async test helpers
# -----------------------------------------------------------------------------


@pytest.fixture
async def async_docker_client() -> AsyncIterator[DockerClient]:
    """Get an async Docker client."""
    client = DockerClient()
    yield client


# -----------------------------------------------------------------------------
# Skip conditions
# -----------------------------------------------------------------------------


def docker_available() -> bool:
    """Check if Docker is available."""
    import subprocess

    try:
        result = subprocess.run(
            ["docker", "version"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def chainctl_available() -> bool:
    """Check if chainctl is available and authenticated."""
    import subprocess

    try:
        result = subprocess.run(
            ["chainctl", "auth", "status"],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


requires_docker = pytest.mark.skipif(
    not docker_available(),
    reason="Docker not available",
)

requires_chainctl = pytest.mark.skipif(
    not chainctl_available(),
    reason="chainctl not available or not authenticated",
)
