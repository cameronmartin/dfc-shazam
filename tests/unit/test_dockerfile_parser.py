"""Tests for Dockerfile parser."""

import pytest

from tests.harness.dockerfile_parser import (
    get_package_manager,
    is_multistage,
    normalize_continuations,
    parse_cmd_entrypoint,
    parse_dockerfile,
    parse_package_list,
)


class TestNormalizeContinuations:
    """Tests for line continuation normalization."""

    def test_simple_continuation(self):
        content = "RUN apt-get install \\\n    curl"
        result = normalize_continuations(content)
        # Continuation character removed, whitespace joined
        assert "RUN apt-get install" in result
        assert "curl" in result
        assert "\\" not in result

    def test_no_continuation(self):
        content = "FROM python:3.11\nWORKDIR /app"
        result = normalize_continuations(content)
        assert result == content

    def test_multiple_continuations(self):
        content = "RUN apt-get update && \\\n    apt-get install -y \\\n    curl wget"
        result = normalize_continuations(content)
        assert "\\" not in result
        assert "curl wget" in result


class TestParseDockerfile:
    """Tests for Dockerfile parsing."""

    def test_parse_simple_dockerfile(self):
        content = """FROM python:3.11-slim
WORKDIR /app
COPY . .
CMD ["python", "app.py"]
"""
        parsed = parse_dockerfile(content)
        assert parsed.base_images == ["python:3.11-slim"]
        assert len(parsed.stages) == 1
        assert parsed.cmd == ["python", "app.py"]

    def test_parse_multistage_dockerfile(self):
        content = """FROM golang:1.21 AS builder
WORKDIR /app
RUN go build -o app

FROM scratch
COPY --from=builder /app/app /app
"""
        parsed = parse_dockerfile(content)
        assert parsed.base_images == ["golang:1.21", "scratch"]
        assert len(parsed.stages) == 2
        assert parsed.stages[0]["name"] == "builder"

    def test_parse_apt_packages(self):
        content = """FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends \\
    build-essential \\
    libpq-dev
"""
        parsed = parse_dockerfile(content)
        assert "build-essential" in parsed.packages["apt"]
        assert "libpq-dev" in parsed.packages["apt"]

    def test_parse_apk_packages(self):
        content = """FROM python:3.11-alpine
RUN apk add --no-cache build-base libffi-dev
"""
        parsed = parse_dockerfile(content)
        assert "build-base" in parsed.packages["apk"]
        assert "libffi-dev" in parsed.packages["apk"]

    def test_parse_yum_packages(self):
        content = """FROM centos:7
RUN yum install -y python3 gcc openssl-devel && yum clean all
"""
        parsed = parse_dockerfile(content)
        assert "python3" in parsed.packages["yum"]
        assert "gcc" in parsed.packages["yum"]
        assert "openssl-devel" in parsed.packages["yum"]

    def test_parse_exposed_ports(self):
        content = """FROM python:3.11
EXPOSE 8000
EXPOSE 9000
"""
        parsed = parse_dockerfile(content)
        assert 8000 in parsed.exposed_ports
        assert 9000 in parsed.exposed_ports

    def test_parse_env_vars(self):
        content = """FROM python:3.11
ENV PYTHONUNBUFFERED=1
ENV APP_HOME /app
"""
        parsed = parse_dockerfile(content)
        assert parsed.env_vars.get("PYTHONUNBUFFERED") == "1"
        assert parsed.env_vars.get("APP_HOME") == "/app"

    def test_parse_entrypoint_json(self):
        content = """FROM python:3.11
ENTRYPOINT ["python", "-m", "app"]
"""
        parsed = parse_dockerfile(content)
        assert parsed.entrypoint == ["python", "-m", "app"]

    def test_parse_entrypoint_shell(self):
        content = """FROM python:3.11
ENTRYPOINT python app.py
"""
        parsed = parse_dockerfile(content)
        assert parsed.entrypoint == ["python", "app.py"]


class TestParsePackageList:
    """Tests for package list parsing."""

    def test_simple_packages(self):
        packages = parse_package_list("curl wget git")
        assert packages == ["curl", "wget", "git"]

    def test_filter_options(self):
        packages = parse_package_list("-y --no-install-recommends curl wget")
        assert "-y" not in packages
        assert "--no-install-recommends" not in packages
        assert packages == ["curl", "wget"]

    def test_filter_special_chars(self):
        packages = parse_package_list("curl ${VERSION} /path/to/file")
        assert packages == ["curl"]


class TestParseCmdEntrypoint:
    """Tests for CMD/ENTRYPOINT parsing."""

    def test_json_format(self):
        result = parse_cmd_entrypoint('["python", "app.py"]')
        assert result == ["python", "app.py"]

    def test_shell_format(self):
        result = parse_cmd_entrypoint("python app.py")
        assert result == ["python", "app.py"]


class TestGetPackageManager:
    """Tests for package manager detection."""

    def test_detect_apt(self):
        content = """FROM python:3.11
RUN apt-get install -y curl
"""
        parsed = parse_dockerfile(content)
        assert get_package_manager(parsed) == "apt"

    def test_detect_yum(self):
        content = """FROM centos:7
RUN yum install -y curl
"""
        parsed = parse_dockerfile(content)
        assert get_package_manager(parsed) == "yum"

    def test_detect_apk(self):
        content = """FROM python:3.11-alpine
RUN apk add curl
"""
        parsed = parse_dockerfile(content)
        assert get_package_manager(parsed) == "apk"

    def test_no_package_manager(self):
        content = """FROM python:3.11
COPY . .
"""
        parsed = parse_dockerfile(content)
        assert get_package_manager(parsed) is None


class TestIsMultistage:
    """Tests for multi-stage detection."""

    def test_single_stage(self):
        content = "FROM python:3.11\nCOPY . ."
        parsed = parse_dockerfile(content)
        assert not is_multistage(parsed)

    def test_multi_stage(self):
        content = "FROM golang:1.21 AS builder\nFROM scratch"
        parsed = parse_dockerfile(content)
        assert is_multistage(parsed)
