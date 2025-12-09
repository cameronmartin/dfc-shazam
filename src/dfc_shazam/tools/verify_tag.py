"""Tool for verifying image tag existence and retrieving image configuration."""

import asyncio
import json
import shutil
from typing import Annotated

from pydantic import Field

from dfc_shazam.chainctl import ChainctlClient, ChainctlError
from dfc_shazam.config import OrgSession
from dfc_shazam.models import ImageConfig, ImageVerificationResult


def _generate_entrypoint_guidance(config: ImageConfig, image_reference: str) -> str:
    """Generate guidance about the image's entrypoint configuration.

    Provides both specific details about the actual entrypoint/cmd values
    and general best practices for working with the image.
    """
    lines = ["ENTRYPOINT CONFIGURATION:"]

    # Part 1: Specific details
    if config.entrypoint:
        lines.append(f"  Entrypoint: {config.entrypoint}")
    else:
        lines.append("  Entrypoint: None (not set)")

    if config.cmd:
        lines.append(f"  Cmd: {config.cmd}")
    else:
        lines.append("  Cmd: None (not set)")

    if config.user:
        lines.append(f"  User: {config.user}")

    lines.append(f"  Shell available: {'Yes' if config.has_shell else 'No'}")
    lines.append(f"  Apk available: {'Yes' if config.has_apk else 'No'}")

    # Part 2: Guidance based on configuration
    lines.append("")
    lines.append("GUIDANCE:")

    if config.entrypoint:
        lines.append(f"- This image has ENTRYPOINT {config.entrypoint}")
        lines.append("- Any CMD you set will be passed as arguments to the entrypoint")
        lines.append("- Review get_image_overview for image-specific usage patterns and best practices")
    else:
        lines.append("- This image has NO entrypoint set")
        lines.append("- CMD will be executed directly as the container command")
        lines.append("- You may need to set ENTRYPOINT in your Dockerfile")

    # Shell availability guidance
    if not config.has_shell:
        lines.append("- This is a distroless image - shell-form commands will NOT work")
        lines.append("- Use exec form: CMD [\"executable\", \"arg1\"] not CMD \"executable arg1\"")
    else:
        lines.append("- Shell is available - both exec form and shell form commands will work")

    # General reminder
    lines.append("- IMPORTANT: Compare with your original image's entrypoint to ensure compatible behavior")

    return "\n".join(lines)


async def _get_crane_config(image_reference: str) -> ImageConfig | None:
    """Get image configuration using crane config.

    Returns ImageConfig with entrypoint, cmd, user, workdir, env, and shell/apk availability.
    """
    crane_path = shutil.which("crane")
    if crane_path is None:
        return None

    try:
        proc = await asyncio.create_subprocess_exec(
            crane_path, "config", image_reference,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)

        if proc.returncode != 0:
            return None

        config_data = json.loads(stdout.decode())
        container_config = config_data.get("config", {})

        # Extract basic config
        entrypoint = container_config.get("Entrypoint")
        cmd = container_config.get("Cmd")
        user = container_config.get("User")
        workdir = container_config.get("WorkingDir")
        env = container_config.get("Env", [])

        # Check for shell and apk by listing the filesystem
        has_shell = False
        has_apk = False

        try:
            # Run crane export | tar -tf - to list files
            proc = await asyncio.create_subprocess_shell(
                f"{crane_path} export {image_reference} - | tar -tf -",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
            file_list = stdout_bytes.decode()

            # Check for shell binaries
            shell_paths = [
                "bin/sh", "usr/bin/sh",
                "bin/bash", "usr/bin/bash",
                "bin/ash", "usr/bin/ash",
                "bin/busybox", "usr/bin/busybox",
            ]
            for shell_path in shell_paths:
                if shell_path in file_list:
                    has_shell = True
                    break

            # Check for apk
            if "sbin/apk" in file_list or "usr/bin/apk" in file_list:
                has_apk = True

        except (asyncio.TimeoutError, Exception):
            # If we can't check filesystem, leave has_shell/has_apk as False
            pass

        return ImageConfig(
            entrypoint=entrypoint,
            cmd=cmd,
            user=user,
            workdir=workdir,
            env=env,
            has_shell=has_shell,
            has_apk=has_apk,
        )

    except (asyncio.TimeoutError, json.JSONDecodeError, Exception):
        return None


async def verify_image_tag(
    image_reference: Annotated[
        str,
        Field(
            description="Full image reference (e.g., 'cgr.dev/{org}/python:3.12', 'cgr.dev/{org}/node:latest')"
        ),
    ],
) -> ImageVerificationResult:
    """Verify that an image:tag combination exists in the Chainguard registry.

    Uses chainctl to resolve the tag and returns whether the image exists
    along with its digest if found. Also retrieves image configuration including
    entrypoint, user, working directory, and whether shell/apk are available.

    IMPORTANT: Always use cgr.dev/{org}/<image> format where {org} is your
    Chainguard organization name. Never use cgr.dev/chainguard/<image>.

    CRITICAL - ENTRYPOINT REVIEW: When the image exists, carefully review the
    `entrypoint_guidance` field in the response. Chainguard image entrypoints
    may differ from the original image you're converting from. Key considerations:
    - Check if the entrypoint matches your original image's behavior
    - Distroless images cannot use shell-form commands
    - CMD arguments are appended to the entrypoint, not executed directly
    - You may need to adjust your Dockerfile's CMD or ENTRYPOINT accordingly

    NOTE: You must call lookup_chainguard_image first to select an organization
    before using this tool.
    """
    org = OrgSession.get_org()
    if org is None:
        return ImageVerificationResult(
            exists=False,
            image_reference=image_reference,
            message="No organization selected. Call lookup_chainguard_image first to select an organization.",
        )

    # Validate it looks like a Chainguard image reference
    if not image_reference.startswith("cgr.dev/"):
        return ImageVerificationResult(
            exists=False,
            image_reference=image_reference,
            message=f"Image reference must start with 'cgr.dev/'. "
            f"Example: cgr.dev/{org}/python:3.12",
        )

    # Warn if using cgr.dev/chainguard/ instead of org
    if image_reference.startswith("cgr.dev/chainguard/"):
        return ImageVerificationResult(
            exists=False,
            image_reference=image_reference,
            message=f"Do not use 'cgr.dev/chainguard/'. Use your organization: "
            f"cgr.dev/{org}/<image>:<tag>",
        )

    client = ChainctlClient()

    try:
        result = await client.resolve_tag(image_reference)

        if result.exists:
            # Get image configuration using crane
            config = await _get_crane_config(image_reference)

            # Generate entrypoint guidance if we have config
            entrypoint_guidance = None
            if config:
                entrypoint_guidance = _generate_entrypoint_guidance(config, image_reference)

            return ImageVerificationResult(
                exists=True,
                image_reference=image_reference,
                digest=result.digest,
                config=config,
                entrypoint_guidance=entrypoint_guidance,
            )
        else:
            return ImageVerificationResult(
                exists=False,
                image_reference=image_reference,
                message="Image or tag not found in the Chainguard registry.",
            )

    except ChainctlError as e:
        return ImageVerificationResult(
            exists=False,
            image_reference=image_reference,
            message=f"Failed to verify image: {e}",
        )
