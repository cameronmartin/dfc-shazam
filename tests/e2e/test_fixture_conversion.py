"""End-to-end tests for fixture conversion.

These tests require Docker to be running and may take significant time.
Run with: pytest tests/e2e -v --run-slow --run-e2e
"""

import pytest

from tests.harness.docker_helpers import DockerClient, temp_dockerfile
from tests.harness.loader import discover_fixtures, load_fixture
from tests.harness.mcp_client import MockMCPClient
from tests.harness.validator import (
    FixtureValidator,
    ValidationConfig,
    validate_fixture,
)


@pytest.mark.e2e
@pytest.mark.slow
@pytest.mark.docker
class TestFixtureConversionE2E:
    """End-to-end tests for fixture conversion."""

    @pytest.fixture
    def docker_client(self):
        """Get Docker client."""
        return DockerClient(timeout=600)

    @pytest.fixture
    def mock_mcp(self):
        """Get mock MCP client with default mappings."""
        client = MockMCPClient()
        client.setup_default_mappings()
        return client

    @pytest.mark.asyncio
    async def test_flask_basic_e2e(self, docker_client, mock_mcp):
        """Test Flask basic fixture end-to-end."""
        fixtures = list(discover_fixtures(tags=["flask"]))
        if not fixtures:
            pytest.skip("Flask fixture not found")

        fixture = load_fixture(fixtures[0])

        config = ValidationConfig(
            build_original=True,
            convert=True,
            lint_converted=True,
            build_converted=False,  # Skip for speed
            run_converted=False,
            cleanup_images=True,
        )

        validator = FixtureValidator(
            docker=docker_client,
            mcp=mock_mcp,
            config=config,
        )

        result = await validator.validate(fixture)

        # Original should build
        if result.original_build:
            assert result.original_build.success, f"Original build failed: {result.original_build.errors}"

        # Conversion should succeed
        assert result.conversion is not None
        assert result.conversion.success, f"Conversion failed: {result.conversion.errors}"

        # Lint should pass (or have no blocking issues)
        if result.converted_lint:
            assert not result.converted_lint.has_blocking_issues()

    @pytest.mark.asyncio
    async def test_validate_fixture_helper(self, mock_mcp):
        """Test the validate_fixture helper function."""
        fixtures = list(discover_fixtures(languages=["python"]))
        if not fixtures:
            pytest.skip("No Python fixtures found")

        fixture = load_fixture(fixtures[0])

        config = ValidationConfig(
            build_original=False,  # Skip Docker builds
            convert=True,
            lint_converted=True,
            build_converted=False,
            run_converted=False,
        )

        result = await validate_fixture(fixture, config=config, mcp=mock_mcp)

        assert result.fixture_name == fixture.metadata.name
        assert result.conversion is not None


@pytest.mark.e2e
@pytest.mark.slow
@pytest.mark.docker
class TestDockerBuildE2E:
    """E2E tests for Docker build functionality."""

    @pytest.fixture
    def docker_client(self):
        """Get Docker client."""
        return DockerClient(timeout=600)

    @pytest.mark.asyncio
    async def test_build_simple_dockerfile(self, docker_client):
        """Test building a simple Dockerfile."""
        dockerfile_content = """FROM python:3.11-slim
WORKDIR /app
RUN echo "Hello" > hello.txt
CMD ["cat", "hello.txt"]
"""
        async with temp_dockerfile(dockerfile_content) as context_path:
            result = await docker_client.build(
                context_path=context_path,
                tag="dfc-test-simple",
            )

            assert result.success, f"Build failed: {result.errors}"
            assert result.image_id is not None

            # Cleanup
            await docker_client.remove_image(result.image_tag, force=True)


@pytest.mark.e2e
@pytest.mark.slow
@pytest.mark.docker
class TestParametrizedFixtures:
    """Parametrized tests for all fixtures."""

    @pytest.mark.asyncio
    async def test_conversion_only(self, dockerfile_fixture, mock_mcp_client):
        """Test conversion for each fixture (no Docker builds)."""
        config = ValidationConfig(
            build_original=False,
            convert=True,
            verify_images=False,
            lint_converted=True,
            build_converted=False,
            run_converted=False,
        )

        result = await validate_fixture(
            dockerfile_fixture,
            config=config,
            mcp=mock_mcp_client,
        )

        # Conversion should succeed
        assert result.conversion is not None, f"No conversion result for {dockerfile_fixture.metadata.name}"
        assert result.conversion.success, (
            f"Conversion failed for {dockerfile_fixture.metadata.name}: "
            f"{result.conversion.errors}"
        )

        # Converted Dockerfile should be valid
        assert result.converted_lint is not None
        assert not result.converted_lint.has_blocking_issues(), (
            f"Lint errors for {dockerfile_fixture.metadata.name}: "
            f"{result.converted_lint.issues}"
        )


@pytest.mark.e2e
@pytest.mark.slow
@pytest.mark.docker
class TestFullValidationPipeline:
    """Tests for full validation pipeline (very slow)."""

    @pytest.fixture
    def docker_client(self):
        """Get Docker client."""
        return DockerClient(timeout=600)

    @pytest.fixture
    def mock_mcp(self):
        """Get mock MCP client."""
        client = MockMCPClient()
        client.setup_default_mappings()
        return client

    @pytest.mark.asyncio
    async def test_full_pipeline_simple_fixture(self, docker_client, mock_mcp):
        """Test full pipeline with a simple fixture."""
        # Find a simple fixture
        fixtures = list(discover_fixtures(complexity="simple", languages=["python"]))
        if not fixtures:
            pytest.skip("No simple Python fixtures found")

        fixture = load_fixture(fixtures[0])

        # Skip runtime tests for simplicity
        config = ValidationConfig(
            build_original=True,
            convert=True,
            lint_converted=True,
            build_converted=True,
            run_converted=False,  # Skip runtime
            cleanup_images=True,
        )

        validator = FixtureValidator(
            docker=docker_client,
            mcp=mock_mcp,
            config=config,
        )

        result = await validator.validate(fixture)

        print(result.summary())

        # All steps should succeed
        if result.original_build:
            assert result.original_build.success, f"Original build failed: {result.errors}"
        assert result.conversion.success, f"Conversion failed: {result.errors}"
        if result.converted_lint:
            assert not result.converted_lint.has_blocking_issues()
        if result.converted_build:
            assert result.converted_build.success, f"Converted build failed: {result.errors}"
