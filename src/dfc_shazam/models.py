"""Pydantic models for dfc-shazam."""

from pydantic import BaseModel, Field


class ChainguardImageResult(BaseModel):
    """Result of Chainguard image lookup."""

    found: bool
    source_image: str
    chainguard_image: str | None = None
    chainguard_image_name: str | None = Field(
        default=None,
        description="Just the image name (e.g., 'amazon-corretto-jdk') without registry prefix. "
        "Use this value when calling lookup_tag.",
    )
    recommendation: str | None = None
    message: str | None = None
    is_generic_base: bool = Field(
        default=False,
        description="True if source is a generic base image (Ubuntu, Alpine, UBI, etc.). "
        "For these, re-search with the workload type (e.g., 'python', 'node', 'jdk').",
    )


class ImageConfig(BaseModel):
    """Container image configuration from crane config."""

    entrypoint: list[str] | None = Field(
        default=None,
        description="Container entrypoint command",
    )
    cmd: list[str] | None = Field(
        default=None,
        description="Default command arguments",
    )
    user: str | None = Field(
        default=None,
        description="User the container runs as (UID or username)",
    )
    workdir: str | None = Field(
        default=None,
        description="Working directory inside the container",
    )
    env: list[str] = Field(
        default_factory=list,
        description="Environment variables set in the image",
    )
    has_shell: bool = Field(
        default=False,
        description="True if /bin/sh or similar shell is available",
    )
    has_apk: bool = Field(
        default=False,
        description="True if apk package manager is available",
    )


class ImageVerificationResult(BaseModel):
    """Result of image tag verification."""

    exists: bool
    image_reference: str
    digest: str | None = None
    config: ImageConfig | None = Field(
        default=None,
        description="Container configuration (entrypoint, user, shell availability, etc.)",
    )
    entrypoint_guidance: str | None = Field(
        default=None,
        description="Actionable guidance about the image's entrypoint configuration and how it may differ from original images",
    )
    message: str | None = None


class APKPackageInfo(BaseModel):
    """APK package information."""

    name: str
    version: str
    description: str
    architecture: str
    size: int = 0
    installed_size: int = 0
    dependencies: list[str] = Field(default_factory=list)
    provides: list[str] = Field(default_factory=list)
    origin: str | None = None


class APKSearchResult(BaseModel):
    """Result of APK package search."""

    query: str
    arch: str
    packages: list[APKPackageInfo]
    total_count: int
    warning: str | None = None


class PackageMatch(BaseModel):
    """A matched APK package with similarity score."""

    apk_package: str
    matched_name: str
    score: float = Field(description="1.0 = exact match, lower = fuzzy match")
    description: str = ""


class PackageMappingResult(BaseModel):
    """Result of package name mapping for a single package."""

    source_package: str
    source_distro: str
    matches: list[PackageMatch] = Field(default_factory=list)
    best_match: str | None = Field(
        default=None,
        description="The recommended APK package name (highest scoring match)",
    )
    message: str | None = None


class PackageMappingBatchResult(BaseModel):
    """Result of batch package name mapping."""

    source_distro: str
    results: list[PackageMappingResult] = Field(
        description="Mapping results for each input package"
    )
    summary: str = Field(
        description="Summary of all mappings in a format suitable for Dockerfile conversion"
    )


class PackageVerificationResult(BaseModel):
    """Result of APK package installation verification."""

    success: bool
    packages: list[str] = Field(description="List of packages that were tested")
    installed: list[str] = Field(
        default_factory=list, description="Packages that installed successfully"
    )
    failed: list[str] = Field(
        default_factory=list, description="Packages that failed to install"
    )
    error_output: str | None = Field(
        default=None, description="Error output from apk if installation failed"
    )
    message: str | None = None


class LinkedDocContent(BaseModel):
    """Content from a linked documentation page."""

    url: str
    title: str
    content: str


class ContainerUserInfo(BaseModel):
    """Information about a user in the container image."""

    username: str
    uid: int
    gid: int
    home: str
    shell: str


class ImageOverviewResult(BaseModel):
    """Result of image overview lookup."""

    found: bool
    image_name: str
    overview_url: str | None = None

    # CRITICAL: Actionable guidance and container info first (before potentially truncated content)
    user_guidance: str | None = Field(
        default=None,
        description="Actionable guidance about container users, ownership, and required Dockerfile changes",
    )
    conversion_tips: list[str] = Field(
        default_factory=list,
        description="General Dockerfile conversion tips applicable to all images",
    )
    available_users: list[ContainerUserInfo] = Field(
        default_factory=list,
        description="Users available in the container image (from /etc/passwd)",
    )
    filesystem_tree: str | None = Field(
        default=None,
        description="Container directory tree showing ownership and permissions",
    )

    # Reference content (may be truncated in long responses)
    overview_text: str | None = None
    best_practices: list[LinkedDocContent] = Field(
        default_factory=list,
        description="Content fetched from best practices and getting started links",
    )
    message: str | None = None


class TagLookupResult(BaseModel):
    """Result of tag lookup/matching."""

    found: bool
    chainguard_image: str
    original_image: str
    original_tag: str
    matched_tag: str | None = None
    full_image_ref: str | None = Field(
        default=None,
        description="Full image reference (e.g., 'cgr.dev/org/python:3.12'). "
        "Use this value when calling verify_image_tag.",
    )
    available_tags: list[str] = Field(default_factory=list)
    variant: str | None = Field(
        default=None,
        description="The variant of the matched tag: 'distroless', 'slim', or 'dev'",
    )
    has_slim_variant: bool = Field(
        default=False,
        description="True if -slim tags are available for this image",
    )
    message: str | None = None
