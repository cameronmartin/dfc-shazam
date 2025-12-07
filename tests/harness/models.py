"""Data models for the test harness."""

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


class Complexity(str, Enum):
    """Fixture complexity level."""

    SIMPLE = "simple"
    MODERATE = "moderate"
    COMPLEX = "complex"


class PackageManager(str, Enum):
    """Package manager type."""

    APT = "apt"
    APK = "apk"
    YUM = "yum"
    DNF = "dnf"
    NONE = "none"


class HealthCheckType(str, Enum):
    """Health check type."""

    HTTP = "http"
    TCP = "tcp"
    EXEC = "exec"


class HealthCheckConfig(BaseModel):
    """Health check configuration for runtime testing."""

    type: HealthCheckType = HealthCheckType.HTTP
    port: int = 8080
    path: str = "/health"
    timeout: int = 30
    interval: int = 1
    retries: int = 30


class RuntimeConfig(BaseModel):
    """Runtime test configuration."""

    enabled: bool = True
    command: list[str] | None = None
    healthcheck: HealthCheckConfig = Field(default_factory=HealthCheckConfig)
    environment: dict[str, str] = Field(default_factory=dict)
    ports: list[int] = Field(default_factory=list)


class MultistageConfig(BaseModel):
    """Multi-stage build configuration."""

    enabled: bool = False
    stages: int = 1  # Number of stages
    final_stage_name: str | None = None


class SourceConfig(BaseModel):
    """Source Dockerfile configuration."""

    base_image: str
    package_manager: PackageManager | None = PackageManager.NONE
    packages: list[str] = Field(default_factory=list)


class ExpectedConfig(BaseModel):
    """Expected conversion result."""

    base_image: str
    runtime_image: str | None = None
    packages: list[str] = Field(default_factory=list)


class BuildConfig(BaseModel):
    """Docker build configuration."""

    context: str = "."
    dockerfile: str = "Dockerfile"
    args: dict[str, str] = Field(default_factory=dict)
    target: str | None = None


class SkipConfig(BaseModel):
    """Skip configuration for conditional test execution."""

    reason: str | None = None
    platforms: list[str] = Field(default_factory=list)


class FixtureMetadata(BaseModel):
    """Complete fixture metadata from fixture.yaml."""

    name: str
    description: str
    language: str
    complexity: Complexity = Complexity.SIMPLE
    tags: list[str] = Field(default_factory=list)
    source: SourceConfig
    expected: ExpectedConfig
    build: BuildConfig = Field(default_factory=BuildConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    multistage: MultistageConfig = Field(default_factory=MultistageConfig)
    notes: str | None = None
    skip: SkipConfig = Field(default_factory=SkipConfig)


@dataclass
class DockerfileFixture:
    """Loaded fixture with all files."""

    metadata: FixtureMetadata
    path: Path
    dockerfile_content: str
    source_files: dict[str, str]  # filename -> content
    expected_dockerfile: str | None = None


@dataclass
class ParsedDockerfile:
    """Parsed Dockerfile information."""

    base_images: list[str]
    stages: list[dict[str, str]]
    packages: dict[str, list[str]]  # package_manager -> packages
    run_commands: list[str]
    copy_commands: list[str]
    env_vars: dict[str, str]
    exposed_ports: list[int]
    cmd: list[str] | None = None
    entrypoint: list[str] | None = None


@dataclass
class ConversionResult:
    """Result of Dockerfile conversion."""

    success: bool
    original_dockerfile: str
    converted_dockerfile: str | None = None
    image_mappings: dict[str, str] = field(default_factory=dict)
    package_mappings: dict[str, list[str]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    mcp_calls: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class BuildResult:
    """Result of Docker build."""

    success: bool
    image_id: str | None = None
    image_tag: str | None = None
    build_time_seconds: float = 0.0
    logs: str = ""
    errors: list[str] = field(default_factory=list)


@dataclass
class RuntimeResult:
    """Result of container runtime test."""

    success: bool
    container_id: str | None = None
    health_check_passed: bool = False
    logs: str = ""
    errors: list[str] = field(default_factory=list)
    exit_code: int | None = None


@dataclass
class LintResult:
    """Result of Dockerfile linting."""

    success: bool
    warnings: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    info: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ValidationResult:
    """Combined validation result for a fixture."""

    fixture_name: str
    pre_build: BuildResult | None = None
    conversion: ConversionResult | None = None
    lint: LintResult | None = None
    post_build: BuildResult | None = None
    runtime: RuntimeResult | None = None
    passed: bool = False
    duration_seconds: float = 0.0
