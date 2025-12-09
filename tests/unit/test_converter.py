"""Tests for Dockerfile converter."""

import pytest

from tests.harness.converter import DockerfileConverter
from tests.harness.mcp_client import MockMCPClient


class TestDockerfileConverter:
    """Tests for Dockerfile conversion."""

    @pytest.fixture
    def mock_client(self):
        """Get a mock MCP client with default mappings."""
        client = MockMCPClient()
        client.setup_default_mappings()
        return client

    @pytest.fixture
    def converter(self, mock_client):
        """Get a converter with mock client."""
        return DockerfileConverter(mcp_client=mock_client)

    @pytest.mark.asyncio
    async def test_convert_simple_python(self, converter):
        """Test converting a simple Python Dockerfile."""
        dockerfile = """FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends build-essential
COPY . .
CMD ["python", "app.py"]
"""
        result = await converter.convert(dockerfile, verify_images=False)

        assert result.success
        assert result.converted_dockerfile is not None
        assert "cgr.dev/chainguard/python" in result.converted_dockerfile
        assert "apk add" in result.converted_dockerfile
        assert "apt-get" not in result.converted_dockerfile

    @pytest.mark.asyncio
    async def test_convert_multistage(self, converter):
        """Test converting a multi-stage Dockerfile."""
        dockerfile = """FROM golang:1.21 AS builder
WORKDIR /app
RUN go build -o server

FROM scratch
COPY --from=builder /app/server /server
CMD ["/server"]
"""
        result = await converter.convert(dockerfile, verify_images=False)

        assert result.success
        assert result.converted_dockerfile is not None
        # Build stage should use -dev variant
        assert "cgr.dev/chainguard/go:latest-dev" in result.converted_dockerfile
        # Runtime stage (scratch) should map to static
        assert "cgr.dev/chainguard/static" in result.converted_dockerfile

    @pytest.mark.asyncio
    async def test_convert_preserves_chainguard_images(self, converter):
        """Test that existing Chainguard images are preserved."""
        dockerfile = """FROM cgr.dev/chainguard/python:latest-dev
WORKDIR /app
COPY . .
CMD ["python", "app.py"]
"""
        result = await converter.convert(dockerfile, verify_images=False)

        assert result.success
        # Should keep the original Chainguard image
        assert "cgr.dev/chainguard/python:latest-dev" in result.converted_dockerfile

    @pytest.mark.asyncio
    async def test_convert_yum_packages(self, converter):
        """Test converting yum packages to apk."""
        dockerfile = """FROM centos:7
RUN yum install -y gcc openssl-devel && yum clean all
"""
        result = await converter.convert(
            dockerfile, source_distro="yum", verify_images=False
        )

        assert result.success
        assert "apk add" in result.converted_dockerfile
        assert "yum" not in result.converted_dockerfile

    @pytest.mark.asyncio
    async def test_convert_tracks_mcp_calls(self, converter):
        """Test that MCP calls are tracked."""
        dockerfile = """FROM python:3.11
RUN apt-get update && apt-get install -y curl
"""
        result = await converter.convert(dockerfile, verify_images=False)

        assert result.success
        assert len(result.mcp_calls) > 0

        # Check that image lookup was called
        tool_names = [call["tool"] for call in result.mcp_calls]
        assert "find_equivalent_chainguard_image" in tool_names
        assert "find_equivalent_apk_packages" in tool_names

    @pytest.mark.asyncio
    async def test_convert_no_packages(self, converter):
        """Test converting Dockerfile without package installs."""
        dockerfile = """FROM node:20-slim
WORKDIR /app
COPY . .
CMD ["node", "server.js"]
"""
        result = await converter.convert(dockerfile, verify_images=False)

        assert result.success
        assert "cgr.dev/chainguard/node" in result.converted_dockerfile

    @pytest.mark.asyncio
    async def test_convert_unknown_image_uses_fallback(self, converter, mock_client):
        """Test that unknown images use chainguard-base as fallback."""
        # Don't set up a mapping for this image
        mock_client.image_mappings = {}

        dockerfile = """FROM someunknown/image:latest
COPY . .
"""
        result = await converter.convert(dockerfile, verify_images=False)

        assert result.success
        # Should have a warning about no mapping
        assert len(result.warnings) > 0
        assert "chainguard-base" in result.converted_dockerfile


class TestDockerfileConverterHelpers:
    """Tests for converter helper methods."""

    @pytest.fixture
    def converter(self):
        """Get a converter with mock client."""
        mock = MockMCPClient()
        mock.setup_default_mappings()
        return DockerfileConverter(mcp_client=mock)

    def test_convert_apt_to_apk_simple(self, converter):
        """Test simple apt to apk conversion."""
        line = "RUN apt-get update && apt-get install -y curl"
        result = converter._convert_apt_to_apk(line, {"curl": ["curl"]})
        assert "apk add --no-cache curl" in result
        assert "apt-get" not in result

    def test_convert_apt_to_apk_multiple_packages(self, converter):
        """Test apt to apk with multiple packages."""
        line = "RUN apt-get install -y curl wget"
        result = converter._convert_apt_to_apk(
            line, {"curl": ["curl"], "wget": ["wget"]}
        )
        assert "apk add --no-cache" in result
        assert "curl" in result
        assert "wget" in result

    def test_convert_yum_to_apk(self, converter):
        """Test yum to apk conversion."""
        line = "RUN yum install -y gcc"
        result = converter._convert_yum_to_apk(line, {"gcc": ["gcc"]})
        assert "apk add --no-cache gcc" in result
        assert "yum" not in result
