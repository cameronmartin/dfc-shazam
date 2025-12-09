"""Tool for verifying APK packages install correctly in a Chainguard container."""

import asyncio
from typing import Annotated

from pydantic import Field

from dfc_shazam.chainctl import ChainctlClient, ChainctlError
from dfc_shazam.config import OrgSession
from dfc_shazam.models import PackageVerificationResult


async def _run_docker_command(cmd: list[str], timeout: float = 120.0) -> tuple[str, str, int]:
    """Run a docker command and return stdout, stderr, return code."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return stdout.decode(), stderr.decode(), proc.returncode or 0
    except asyncio.TimeoutError:
        proc.kill()
        return "", "Command timed out", 1


async def _image_exists(image_ref: str, platform: str) -> bool:
    """Check if a Docker image exists by attempting to pull it."""
    cmd = ["docker", "pull", "--quiet", "--platform", platform, image_ref]
    _, _, returncode = await _run_docker_command(cmd, timeout=60.0)
    return returncode == 0


async def _find_base_image(org: str, platform: str) -> str | None:
    """Find a suitable base image with apk for package verification.

    Tries chainguard-base first, then falls back to any available -dev image.

    Args:
        org: Organization name
        platform: Docker platform (e.g., "linux/amd64")

    Returns:
        Full image reference (e.g., "cgr.dev/org/chainguard-base:latest") or None
    """
    # Try chainguard-base first (preferred - minimal base with apk)
    base_image = f"cgr.dev/{org}/chainguard-base:latest"
    if await _image_exists(base_image, platform):
        return base_image

    # Fallback: find any image with a -dev tag
    try:
        client = ChainctlClient()
        images = await client.list_images(org=org)

        # Look for any image - we'll use its latest-dev tag
        for image in images:
            if image.name:
                dev_image = f"cgr.dev/{org}/{image.name}:latest-dev"
                if await _image_exists(dev_image, platform):
                    return dev_image

    except ChainctlError:
        pass

    return None


async def verify_apk_packages(
    packages: Annotated[
        list[str],
        Field(description="List of APK package names to verify (e.g., ['openssl', 'curl', 'git'])"),
    ],
    arch: Annotated[
        str, Field(description="Architecture (x86_64 or aarch64)")
    ] = "x86_64",
) -> PackageVerificationResult:
    """Verify that APK packages install correctly in a Chainguard container.

    Spins up a chainguard-base container and performs a dry-run installation
    (apk add --simulate) to verify the packages exist and can be resolved.
    This is faster than actual installation since no packages are downloaded.
    Returns details about which packages succeeded or failed.

    NOTE: You must call lookup_chainguard_image first to select an organization
    before using this tool.
    """
    org = OrgSession.get_org()
    if org is None:
        return PackageVerificationResult(
            success=False,
            packages=packages,
            message="No organization selected. Call lookup_chainguard_image first to select an organization.",
        )

    if not packages:
        return PackageVerificationResult(
            success=False,
            packages=[],
            message="No packages specified to verify.",
        )

    if arch not in ("x86_64", "aarch64"):
        return PackageVerificationResult(
            success=False,
            packages=packages,
            message=f"Invalid architecture '{arch}'. Use 'x86_64' or 'aarch64'.",
        )

    # Map arch to docker platform
    platform = "linux/amd64" if arch == "x86_64" else "linux/arm64"

    # Find a suitable base image with apk
    base_image = await _find_base_image(org, platform)
    if base_image is None:
        return PackageVerificationResult(
            success=False,
            packages=packages,
            message=f"No suitable base image found in organization '{org}'. "
            "Need chainguard-base or any image with a -dev variant that includes apk.",
        )

    # Build the apk add command with --simulate for dry-run (faster, no actual install)
    pkg_list = " ".join(packages)
    install_cmd = f"apk update && apk add --no-cache --simulate {pkg_list}"

    cmd = [
        "docker", "run", "--rm",
        "--platform", platform,
        base_image,
        "sh", "-c", install_cmd,
    ]

    stdout, stderr, returncode = await _run_docker_command(cmd)

    # Combine stdout and stderr for analysis
    output = stdout + stderr

    if returncode == 0:
        return PackageVerificationResult(
            success=True,
            packages=packages,
            installed=packages,
            failed=[],
            message=f"All {len(packages)} package(s) verified successfully (dry-run).",
        )

    # Parse the error to identify which packages failed
    failed: list[str] = []
    installed: list[str] = []

    for pkg in packages:
        # Common error patterns
        if f"unable to select packages:\n  {pkg}" in output or f"ERROR: unable to select packages:\n  {pkg}" in output:
            failed.append(pkg)
        elif f"{pkg} (no such package)" in output.lower():
            failed.append(pkg)
        elif f"unsatisfiable constraints" in output and pkg in output:
            failed.append(pkg)
        else:
            # Assume installed if not explicitly failed
            # This is a heuristic - if one package fails, apk may not install others
            installed.append(pkg)

    # If we couldn't parse specific failures, mark all as failed
    if not failed and returncode != 0:
        failed = packages
        installed = []

    # Clean up error output for display
    error_lines = []
    for line in output.split("\n"):
        line = line.strip()
        if line and not line.startswith("fetch ") and not line.startswith("OK:"):
            error_lines.append(line)
    error_output = "\n".join(error_lines[-20:])  # Last 20 relevant lines

    return PackageVerificationResult(
        success=False,
        packages=packages,
        installed=installed,
        failed=failed,
        error_output=error_output if error_output else None,
        message=f"Installation failed. {len(failed)} package(s) failed: {', '.join(failed)}",
    )
