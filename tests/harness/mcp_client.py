"""MCP tool invocation wrapper for testing."""

import time
from dataclasses import dataclass, field
from typing import Any

from dfc_shazam.models import (
    ChainguardImageResult,
    MigrationInstructionsResult,
    PackageMatch,
    PackageMappingBatchResult,
    PackageMappingResult,
)
from dfc_shazam.tools.find_equiv_cgr_image import find_equivalent_chainguard_image
from dfc_shazam.tools.image_docs import get_migration_instructions_for_chainguard_image
from dfc_shazam.tools.map_package import find_equivalent_apk_packages


@dataclass
class MCPCall:
    """Record of an MCP tool call."""

    tool: str
    args: dict[str, Any]
    result: Any
    success: bool
    error: str | None = None
    duration_ms: float = 0.0


@dataclass
class MCPSession:
    """Session for tracking MCP calls."""

    calls: list[MCPCall] = field(default_factory=list)

    def record(
        self,
        tool: str,
        args: dict[str, Any],
        result: Any,
        success: bool,
        error: str | None = None,
        duration_ms: float = 0.0,
    ) -> None:
        """Record an MCP call."""
        self.calls.append(
            MCPCall(
                tool=tool,
                args=args,
                result=result,
                success=success,
                error=error,
                duration_ms=duration_ms,
            )
        )

    def get_calls_by_tool(self, tool: str) -> list[MCPCall]:
        """Get all calls to a specific tool."""
        return [c for c in self.calls if c.tool == tool]

    def to_audit_log(self) -> list[dict[str, Any]]:
        """Convert to audit log format."""
        return [
            {
                "tool": c.tool,
                "args": c.args,
                "success": c.success,
                "error": c.error,
                "duration_ms": c.duration_ms,
            }
            for c in self.calls
        ]

    def clear(self) -> None:
        """Clear all recorded calls."""
        self.calls.clear()


class MCPTestClient:
    """Test client for MCP tool invocation.

    Wraps the actual MCP tools to provide:
    - Call tracking/auditing
    - Mock support for testing without external dependencies
    - Retry logic for flaky operations
    """

    def __init__(
        self,
        session: MCPSession | None = None,
    ):
        """Initialize the test client.

        Args:
            session: Session for tracking calls (created if not provided)
        """
        self.session = session or MCPSession()
        self._mock_responses: dict[str, Any] = {}

    def set_mock_response(self, tool: str, response: Any) -> None:
        """Set a mock response for a tool.

        Args:
            tool: Tool name
            response: Response to return
        """
        self._mock_responses[tool] = response

    def clear_mocks(self) -> None:
        """Clear all mock responses."""
        self._mock_responses.clear()

    async def _call_with_tracking(
        self,
        tool_name: str,
        args: dict[str, Any],
        coro: Any,
    ) -> Any:
        """Call a tool with tracking.

        Args:
            tool_name: Name of the tool
            args: Tool arguments
            coro: Coroutine to execute

        Returns:
            Tool result
        """
        start = time.time()
        error = None
        success = True
        result = None

        try:
            if tool_name in self._mock_responses:
                result = self._mock_responses[tool_name]
            else:
                result = await coro
        except Exception as e:
            error = str(e)
            success = False
            raise
        finally:
            duration_ms = (time.time() - start) * 1000
            self.session.record(
                tool=tool_name,
                args=args,
                result=result,
                success=success,
                error=error,
                duration_ms=duration_ms,
            )

        return result

    async def lookup_image(self, source_image: str) -> ChainguardImageResult:
        """Look up Chainguard image equivalent.

        Args:
            source_image: Source Docker Hub image

        Returns:
            ChainguardImageResult
        """
        args = {"source_image": source_image}
        return await self._call_with_tracking(
            "find_equivalent_chainguard_image",
            args,
            find_equivalent_chainguard_image(source_image),
        )

    async def find_equivalent_apk_packages(
        self,
        packages: list[str],
        source_distro: str = "auto",
    ) -> PackageMappingBatchResult:
        """Map package names to APK equivalents.

        Args:
            packages: List of source package names
            source_distro: Source distribution

        Returns:
            PackageMappingBatchResult
        """
        args = {"packages": packages, "source_distro": source_distro}
        return await self._call_with_tracking(
            "find_equivalent_apk_packages",
            args,
            find_equivalent_apk_packages(packages, source_distro),  # type: ignore
        )

    async def get_migration_instructions(
        self, image_reference: str
    ) -> MigrationInstructionsResult:
        """Get migration instructions for a Chainguard image.

        Args:
            image_reference: Full image reference

        Returns:
            MigrationInstructionsResult
        """
        args = {"image_reference": image_reference}
        return await self._call_with_tracking(
            "get_migration_instructions_for_chainguard_image",
            args,
            get_migration_instructions_for_chainguard_image(image_reference),
        )

    def get_session(self) -> MCPSession:
        """Get the current session."""
        return self.session


class MockMCPClient(MCPTestClient):
    """Fully mocked MCP client for unit testing.

    Provides configurable mock responses without calling real tools.
    """

    def __init__(self) -> None:
        """Initialize mock client with empty session."""
        super().__init__()
        # Default mock data
        self.image_mappings: dict[str, str] = {}
        self.package_mappings: dict[str, list[str]] = {}
        self.verified_images: set[str] = set()

    async def lookup_image(self, source_image: str) -> ChainguardImageResult:
        """Mock image lookup.

        Args:
            source_image: Source image name

        Returns:
            Mocked ChainguardImageResult
        """
        # Extract base name without tag
        base_name = source_image.split(":")[0].split("/")[-1]
        original_tag = source_image.split(":")[-1] if ":" in source_image else "latest"

        if base_name in self.image_mappings:
            cg_image = self.image_mappings[base_name]
            result = ChainguardImageResult(
                found=True,
                source_image=source_image,
                chainguard_image=f"cgr.dev/chainguard/{cg_image}",
                chainguard_image_name=cg_image,
                original_tag=original_tag,
                matched_tag="latest",
                full_image_ref=f"cgr.dev/chainguard/{cg_image}:latest",
                variant="distroless",
                available_variants=["distroless", "dev"],
                recommendation=f"Use cgr.dev/chainguard/{cg_image}:latest",
            )
        else:
            result = ChainguardImageResult(
                found=False,
                source_image=source_image,
                message=f"No mapping for {source_image}",
            )

        self.session.record(
            tool="find_equivalent_chainguard_image",
            args={"source_image": source_image},
            result=result,
            success=True,
        )
        return result

    async def find_equivalent_apk_packages(
        self,
        packages: list[str],
        source_distro: str = "auto",
    ) -> PackageMappingBatchResult:
        """Mock package mapping.

        Args:
            packages: List of source package names
            source_distro: Source distribution

        Returns:
            Mocked PackageMappingBatchResult
        """
        results = []
        for package in packages:
            if package in self.package_mappings:
                apk_pkgs = self.package_mappings[package]
                matches = [
                    PackageMatch(
                        apk_package=apk_pkg,
                        matched_name=apk_pkg,
                        score=1.0,
                        description=f"Mock package {apk_pkg}",
                    )
                    for apk_pkg in apk_pkgs
                ]
                result = PackageMappingResult(
                    source_package=package,
                    source_distro=source_distro,
                    matches=matches,
                    best_match=apk_pkgs[0] if apk_pkgs else None,
                    message=f"Direct mapping: {package} -> {apk_pkgs}",
                )
            else:
                result = PackageMappingResult(
                    source_package=package,
                    source_distro=source_distro,
                    matches=[],
                    best_match=None,
                    message=f"No direct mapping for {package}",
                )
            results.append(result)

        apk_packages = [r.best_match for r in results if r.best_match]
        batch_result = PackageMappingBatchResult(
            source_distro=source_distro,
            results=results,
            summary=f"APK packages: {' '.join(apk_packages)}" if apk_packages else "No packages mapped",
        )

        self.session.record(
            tool="find_equivalent_apk_packages",
            args={"packages": packages, "source_distro": source_distro},
            result=batch_result,
            success=True,
        )
        return batch_result

    async def get_migration_instructions(
        self, image_reference: str
    ) -> MigrationInstructionsResult:
        """Mock migration instructions lookup.

        Args:
            image_reference: Image reference to get instructions for

        Returns:
            Mocked MigrationInstructionsResult
        """
        exists = image_reference in self.verified_images
        # Extract image name from reference
        image_name = image_reference.split("/")[-1].split(":")[0]

        result = MigrationInstructionsResult(
            exists=exists,
            image_reference=image_reference,
            digest="sha256:mock123456789" if exists else None,
            image_name=image_name,
            conversion_tips=["Mock conversion tip"],
            message=None if exists else "Image not found",
        )

        self.session.record(
            tool="get_migration_instructions_for_chainguard_image",
            args={"image_reference": image_reference},
            result=result,
            success=True,
        )
        return result

    def setup_default_mappings(self) -> None:
        """Set up common default mappings for testing."""
        self.image_mappings = {
            "python": "python",
            "node": "node",
            "golang": "go",
            "go": "go",
            "nginx": "nginx",
            "postgres": "postgres",
            "redis": "redis",
            "ruby": "ruby",
            "php": "php",
            "rust": "rust",
            "openjdk": "jdk",
            "java": "jdk",
            "maven": "maven",
            "gradle": "gradle",
            "alpine": "chainguard-base",
            "ubuntu": "chainguard-base",
            "debian": "chainguard-base",
            "centos": "chainguard-base",
        }

        self.package_mappings = {
            "build-essential": ["build-base"],
            "gcc": ["gcc"],
            "g++": ["g++"],
            "make": ["make"],
            "cmake": ["cmake"],
            "libssl-dev": ["openssl-dev"],
            "openssl": ["openssl"],
            "ca-certificates": ["ca-certificates"],
            "python3": ["python-3"],
            "python3-dev": ["python-3-dev"],
            "python3-pip": ["py3-pip"],
            "libffi-dev": ["libffi-dev"],
            "zlib1g-dev": ["zlib-dev"],
            "libpq-dev": ["postgresql-dev", "libpq-dev"],
            "curl": ["curl"],
            "wget": ["wget"],
            "git": ["git"],
            "jq": ["jq"],
            "nodejs": ["nodejs"],
            "npm": ["npm"],
            # yum packages
            "gcc-c++": ["g++"],
            "openssl-devel": ["openssl-dev"],
            "python3-devel": ["python-3-dev"],
            "libffi-devel": ["libffi-dev"],
            "zlib-devel": ["zlib-dev"],
            "postgresql-devel": ["postgresql-dev"],
        }

        self.verified_images = {
            "cgr.dev/chainguard/python:latest",
            "cgr.dev/chainguard/python:latest-dev",
            "cgr.dev/chainguard/node:latest",
            "cgr.dev/chainguard/node:latest-dev",
            "cgr.dev/chainguard/go:latest",
            "cgr.dev/chainguard/go:latest-dev",
            "cgr.dev/chainguard/nginx:latest",
            "cgr.dev/chainguard/chainguard-base:latest",
            "cgr.dev/chainguard/chainguard-base:latest-dev",
            "cgr.dev/chainguard/static:latest",
        }
