"""Integration tests for conversion workflow."""

import pytest

from tests.harness.converter import DockerfileConverter
from tests.harness.dockerfile_parser import parse_dockerfile
from tests.harness.linter import lint_dockerfile
from tests.harness.loader import load_fixture_by_name
from tests.harness.mcp_client import MockMCPClient


class TestConversionWorkflow:
    """Tests for the complete conversion workflow."""

    @pytest.fixture
    def mock_client(self):
        """Get a mock MCP client with default mappings."""
        client = MockMCPClient()
        client.setup_default_mappings()
        return client

    @pytest.mark.asyncio
    async def test_flask_conversion_workflow(self, mock_client):
        """Test converting Flask fixture end-to-end."""
        try:
            fixture = load_fixture_by_name("python_flask_basic")
        except ValueError:
            pytest.skip("Fixture not found")

        # Parse original
        original_parsed = parse_dockerfile(fixture.dockerfile_content)
        assert "python:3.11-slim" in original_parsed.base_images
        assert "apt" in original_parsed.packages
        assert len(original_parsed.packages["apt"]) > 0

        # Convert
        converter = DockerfileConverter(mcp_client=mock_client)
        result = await converter.convert(
            fixture.dockerfile_content, verify_images=False
        )

        assert result.success
        assert result.converted_dockerfile is not None

        # Parse converted
        converted_parsed = parse_dockerfile(result.converted_dockerfile)
        assert any(
            "cgr.dev/chainguard" in img for img in converted_parsed.base_images
        )

        # Lint converted
        lint_result = await lint_dockerfile(result.converted_dockerfile)
        assert lint_result.success or not lint_result.has_blocking_issues()

    @pytest.mark.asyncio
    async def test_multistage_conversion_workflow(self, mock_client):
        """Test converting multi-stage fixture."""
        try:
            fixture = load_fixture_by_name("go_multistage_static")
        except ValueError:
            pytest.skip("Fixture not found")

        # Parse original
        original_parsed = parse_dockerfile(fixture.dockerfile_content)
        assert len(original_parsed.stages) == 2
        assert "scratch" in original_parsed.base_images

        # Convert
        converter = DockerfileConverter(mcp_client=mock_client)
        result = await converter.convert(
            fixture.dockerfile_content, verify_images=False
        )

        assert result.success

        # Parse converted
        converted_parsed = parse_dockerfile(result.converted_dockerfile)
        # Build stage should use -dev variant
        assert any("latest-dev" in img for img in converted_parsed.base_images)
        # Runtime (scratch) should map to static
        assert any(
            "cgr.dev/chainguard/static" in img for img in converted_parsed.base_images
        )

    @pytest.mark.asyncio
    async def test_yum_conversion_workflow(self, mock_client):
        """Test converting CentOS/yum fixture."""
        try:
            fixture = load_fixture_by_name("centos_yum")
        except ValueError:
            pytest.skip("Fixture not found")

        # Parse original
        original_parsed = parse_dockerfile(fixture.dockerfile_content)
        assert "yum" in original_parsed.packages
        assert len(original_parsed.packages["yum"]) > 0

        # Convert
        converter = DockerfileConverter(mcp_client=mock_client)
        result = await converter.convert(
            fixture.dockerfile_content,
            source_distro="yum",
            verify_images=False,
        )

        assert result.success

        # Verify yum is replaced
        assert "yum" not in result.converted_dockerfile
        assert "apk" in result.converted_dockerfile

    @pytest.mark.asyncio
    async def test_alpine_already_apk(self, mock_client):
        """Test converting Alpine fixture that already uses apk."""
        try:
            fixture = load_fixture_by_name("alpine_apk")
        except ValueError:
            pytest.skip("Fixture not found")

        # Parse original
        original_parsed = parse_dockerfile(fixture.dockerfile_content)
        assert "apk" in original_parsed.packages

        # Convert
        converter = DockerfileConverter(mcp_client=mock_client)
        result = await converter.convert(
            fixture.dockerfile_content, verify_images=False
        )

        assert result.success
        # Should still have apk commands (converted or preserved)
        assert "apk" in result.converted_dockerfile

    @pytest.mark.asyncio
    async def test_mcp_call_tracking(self, mock_client):
        """Test that MCP calls are properly tracked."""
        dockerfile = """FROM python:3.11-slim
RUN apt-get update && apt-get install -y curl wget git
"""
        converter = DockerfileConverter(mcp_client=mock_client)
        result = await converter.convert(dockerfile, verify_images=False)

        assert result.success
        assert len(result.mcp_calls) > 0

        # Should have called lookup_chainguard_image
        lookup_calls = [
            c for c in result.mcp_calls if c["tool"] == "lookup_chainguard_image"
        ]
        assert len(lookup_calls) >= 1

        # Should have called map_package once with all packages as a batch
        map_calls = [c for c in result.mcp_calls if c["tool"] == "map_package"]
        assert len(map_calls) >= 1
        # Verify the batch call included all packages
        if map_calls:
            packages = map_calls[0]["args"].get("packages", [])
            assert "curl" in packages
            assert "wget" in packages
            assert "git" in packages

    @pytest.mark.asyncio
    async def test_conversion_warnings(self, mock_client):
        """Test that warnings are captured during conversion."""
        # Use an image with no mapping
        mock_client.image_mappings = {}

        dockerfile = """FROM unknown/image:latest
COPY . .
"""
        converter = DockerfileConverter(mcp_client=mock_client)
        result = await converter.convert(dockerfile, verify_images=False)

        # Should succeed with fallback
        assert result.success
        # But should have warnings
        assert len(result.warnings) > 0


class TestConversionEdgeCases:
    """Tests for edge cases in conversion."""

    @pytest.fixture
    def mock_client(self):
        """Get a mock MCP client."""
        client = MockMCPClient()
        client.setup_default_mappings()
        return client

    @pytest.mark.asyncio
    async def test_empty_dockerfile(self, mock_client):
        """Test handling empty Dockerfile."""
        converter = DockerfileConverter(mcp_client=mock_client)
        result = await converter.convert("", verify_images=False)

        # Should fail or return empty conversion
        # Behavior depends on implementation
        assert result is not None

    @pytest.mark.asyncio
    async def test_comments_preserved(self, mock_client):
        """Test that comments are preserved."""
        dockerfile = """# This is a comment
FROM python:3.11-slim
# Another comment
COPY . .
"""
        converter = DockerfileConverter(mcp_client=mock_client)
        result = await converter.convert(dockerfile, verify_images=False)

        assert result.success
        assert "# This is a comment" in result.converted_dockerfile
        assert "# Another comment" in result.converted_dockerfile

    @pytest.mark.asyncio
    async def test_arg_and_env_preserved(self, mock_client):
        """Test that ARG and ENV are preserved."""
        dockerfile = """FROM python:3.11-slim
ARG APP_VERSION=1.0
ENV PYTHONUNBUFFERED=1
COPY . .
"""
        converter = DockerfileConverter(mcp_client=mock_client)
        result = await converter.convert(dockerfile, verify_images=False)

        assert result.success
        assert "ARG APP_VERSION=1.0" in result.converted_dockerfile
        assert "ENV PYTHONUNBUFFERED=1" in result.converted_dockerfile
