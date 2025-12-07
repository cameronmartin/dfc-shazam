"""Dockerfile conversion orchestration."""

import re
from typing import Literal

from tests.harness.dockerfile_parser import get_package_manager, parse_dockerfile
from tests.harness.mcp_client import MCPTestClient
from tests.harness.models import ConversionResult, ParsedDockerfile


class DockerfileConverter:
    """Orchestrates Dockerfile conversion using MCP tools.

    This class simulates the workflow an AI assistant would use
    to convert a Dockerfile from a standard base image to Chainguard.
    """

    def __init__(self, mcp_client: MCPTestClient | None = None):
        """Initialize converter.

        Args:
            mcp_client: MCP client for tool invocation
        """
        self.mcp = mcp_client or MCPTestClient()

    async def convert(
        self,
        dockerfile_content: str,
        source_distro: Literal["apt", "yum", "dnf", "auto"] = "auto",
        verify_images: bool = True,
    ) -> ConversionResult:
        """Convert a Dockerfile to use Chainguard images.

        Args:
            dockerfile_content: Original Dockerfile content
            source_distro: Source package manager type
            verify_images: Whether to verify target images exist

        Returns:
            ConversionResult with converted Dockerfile
        """
        # Parse the original Dockerfile
        parsed = parse_dockerfile(dockerfile_content)

        image_mappings: dict[str, str] = {}
        package_mappings: dict[str, list[str]] = {}
        warnings: list[str] = []
        errors: list[str] = []

        # Step 1: Map base images to Chainguard equivalents
        await self._map_images(
            parsed, image_mappings, warnings, errors, verify_images
        )

        if errors:
            return ConversionResult(
                success=False,
                original_dockerfile=dockerfile_content,
                errors=errors,
                warnings=warnings,
                mcp_calls=self.mcp.get_session().to_audit_log(),
            )

        # Step 2: Map packages
        detected_pm = get_package_manager(parsed)
        if detected_pm and source_distro == "auto":
            source_distro = detected_pm  # type: ignore

        await self._map_packages(parsed, package_mappings, warnings, source_distro)

        # Step 3: Generate converted Dockerfile
        converted = self._generate_dockerfile(
            dockerfile_content, parsed, image_mappings, package_mappings
        )

        return ConversionResult(
            success=True,
            original_dockerfile=dockerfile_content,
            converted_dockerfile=converted,
            image_mappings=image_mappings,
            package_mappings=package_mappings,
            errors=errors,
            warnings=warnings,
            mcp_calls=self.mcp.get_session().to_audit_log(),
        )

    async def _map_images(
        self,
        parsed: ParsedDockerfile,
        image_mappings: dict[str, str],
        warnings: list[str],
        errors: list[str],
        verify: bool = True,
    ) -> None:
        """Map base images to Chainguard equivalents."""
        for i, stage in enumerate(parsed.stages):
            base_image = stage["base"]

            # Skip if already a Chainguard image
            if base_image.startswith("cgr.dev/"):
                image_mappings[base_image] = base_image
                continue

            # Handle scratch
            if base_image == "scratch":
                image_mappings[base_image] = "cgr.dev/chainguard/static:latest"
                continue

            # Look up Chainguard equivalent
            result = await self.mcp.lookup_image(base_image)

            if not result.found:
                warnings.append(
                    f"No Chainguard equivalent found for '{base_image}'. "
                    "Using chainguard-base as fallback."
                )
                # Use chainguard-base as fallback
                target_image = "cgr.dev/chainguard/chainguard-base:latest-dev"
            else:
                chainguard_image = result.chainguard_image

                # Determine appropriate tag
                # Use -dev variant for build stages, latest for runtime
                if self._is_build_stage(i, parsed):
                    target_image = f"{chainguard_image}:latest-dev"
                else:
                    target_image = f"{chainguard_image}:latest"

            # Verify the target image exists
            if verify and result.found:
                verify_result = await self.mcp.verify_tag(target_image)
                if not verify_result.exists:
                    warnings.append(
                        f"Target image '{target_image}' could not be verified. "
                        "It may not exist or chainctl auth may be required."
                    )

            image_mappings[base_image] = target_image

    async def _map_packages(
        self,
        parsed: ParsedDockerfile,
        package_mappings: dict[str, list[str]],
        warnings: list[str],
        source_distro: str,
    ) -> None:
        """Map packages to APK equivalents."""
        all_packages: list[str] = []

        for pm, packages in parsed.packages.items():
            if pm in ("apt", "yum"):
                all_packages.extend(packages)

        # Deduplicate while preserving order
        all_packages = list(dict.fromkeys(all_packages))

        if not all_packages:
            return

        # Map all packages in a single batch call
        batch_result = await self.mcp.map_package(all_packages, source_distro)

        for result in batch_result.results:
            pkg = result.source_package
            if result.best_match:
                # Use all matches from the result, or just best_match
                apk_packages = [m.apk_package for m in result.matches[:1]] if result.matches else [result.best_match]
                package_mappings[pkg] = apk_packages
            else:
                # No match found - use package name as-is and warn
                package_mappings[pkg] = [pkg]
                warnings.append(
                    f"Package '{pkg}' has no direct APK mapping. "
                    f"Using package name as-is: {pkg}"
                )

    def _is_build_stage(self, stage_idx: int, parsed: ParsedDockerfile) -> bool:
        """Determine if a stage is a build stage (not final).

        Args:
            stage_idx: Stage index
            parsed: Parsed Dockerfile

        Returns:
            True if this is a build stage
        """
        if len(parsed.stages) == 1:
            # Single-stage: check if it has package installs or build commands
            has_packages = bool(
                parsed.packages.get("apt") or parsed.packages.get("yum")
            )
            # Check for common build patterns
            has_build = any(
                cmd
                for cmd in parsed.run_commands
                if any(
                    pattern in cmd.lower()
                    for pattern in ["pip install", "npm install", "go build", "make"]
                )
            )
            return has_packages or has_build

        # Multi-stage: any stage except the last is a build stage
        return stage_idx < len(parsed.stages) - 1

    def _generate_dockerfile(
        self,
        original: str,
        parsed: ParsedDockerfile,
        image_mappings: dict[str, str],
        package_mappings: dict[str, list[str]],
    ) -> str:
        """Generate the converted Dockerfile.

        Args:
            original: Original Dockerfile content
            parsed: Parsed Dockerfile info
            image_mappings: Image name mappings
            package_mappings: Package name mappings

        Returns:
            Converted Dockerfile content
        """
        lines = original.split("\n")
        converted_lines: list[str] = []
        in_run_block = False
        run_block_lines: list[str] = []

        for line in lines:
            stripped = line.strip()

            # Handle multi-line RUN commands
            if in_run_block:
                run_block_lines.append(line)
                if not stripped.endswith("\\"):
                    # End of RUN block
                    full_run = "\n".join(run_block_lines)
                    converted_run = self._convert_run_command(
                        full_run, package_mappings
                    )
                    converted_lines.append(converted_run)
                    in_run_block = False
                    run_block_lines = []
                continue

            # Check for RUN command start
            if stripped.upper().startswith("RUN "):
                if stripped.endswith("\\"):
                    in_run_block = True
                    run_block_lines = [line]
                    continue
                else:
                    converted_lines.append(
                        self._convert_run_command(line, package_mappings)
                    )
                    continue

            # Replace FROM instructions
            from_match = re.match(
                r"^(\s*FROM\s+)(?:--platform=\S+\s+)?(\S+)(.*)$",
                line,
                re.IGNORECASE,
            )
            if from_match:
                prefix, image, suffix = from_match.groups()
                # Try to find mapping for image with or without tag
                image_base = image.split(":")[0]
                if image in image_mappings:
                    converted_lines.append(f"{prefix}{image_mappings[image]}{suffix}")
                elif image_base in image_mappings:
                    converted_lines.append(f"{prefix}{image_mappings[image_base]}{suffix}")
                else:
                    converted_lines.append(line)
                continue

            # Keep other lines unchanged
            converted_lines.append(line)

        return "\n".join(converted_lines)

    def _convert_run_command(
        self,
        line: str,
        package_mappings: dict[str, list[str]],
    ) -> str:
        """Convert a RUN command.

        Args:
            line: RUN command line
            package_mappings: Package name mappings

        Returns:
            Converted RUN command
        """
        # Check for apt-get install
        if re.search(r"\bapt-get\s+install\b", line, re.IGNORECASE):
            return self._convert_apt_to_apk(line, package_mappings)

        # Check for apt install
        if re.search(r"\bapt\s+install\b", line, re.IGNORECASE):
            return self._convert_apt_to_apk(line, package_mappings)

        # Check for yum/dnf install
        if re.search(r"\b(?:yum|dnf)\s+install\b", line, re.IGNORECASE):
            return self._convert_yum_to_apk(line, package_mappings)

        return line

    def _convert_apt_to_apk(
        self,
        line: str,
        package_mappings: dict[str, list[str]],
    ) -> str:
        """Convert apt-get install to apk add.

        Args:
            line: RUN command with apt-get
            package_mappings: Package name mappings

        Returns:
            Converted RUN command
        """
        # Find packages in the line
        packages: list[str] = []
        for pkg in package_mappings:
            if pkg in line:
                packages.extend(package_mappings[pkg])

        if not packages:
            # Basic replacement if no specific packages found
            converted = line
            converted = re.sub(r"apt-get\s+update\s*&&\s*", "", converted)
            converted = re.sub(r"apt-get", "apk", converted)
            converted = re.sub(r"install\s+-y", "add --no-cache", converted)
            converted = re.sub(r"--no-install-recommends\s*", "", converted)
            converted = re.sub(
                r"&&\s*rm\s+-rf\s+/var/lib/apt/lists/\*", "", converted
            )
            return converted

        # Build new command with mapped packages
        unique_packages = list(dict.fromkeys(packages))  # Preserve order, remove dupes
        return f"RUN apk add --no-cache {' '.join(unique_packages)}"

    def _convert_yum_to_apk(
        self,
        line: str,
        package_mappings: dict[str, list[str]],
    ) -> str:
        """Convert yum/dnf install to apk add.

        Args:
            line: RUN command with yum/dnf
            package_mappings: Package name mappings

        Returns:
            Converted RUN command
        """
        packages: list[str] = []
        for pkg in package_mappings:
            if pkg in line:
                packages.extend(package_mappings[pkg])

        if not packages:
            # Basic replacement
            converted = line
            converted = re.sub(r"\b(?:yum|dnf)\b", "apk", converted)
            converted = re.sub(r"install\s+-y", "add --no-cache", converted)
            converted = re.sub(r"&&\s*(?:yum|dnf)\s+clean\s+all", "", converted)
            return converted

        unique_packages = list(dict.fromkeys(packages))
        return f"RUN apk add --no-cache {' '.join(unique_packages)}"
