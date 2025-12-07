"""Tool for verifying image tag existence."""

from typing import Annotated

from pydantic import Field

from dfc_shazam.chainctl import ChainctlClient, ChainctlError
from dfc_shazam.config import OrgNotSelectedError, OrgSession
from dfc_shazam.models import ImageVerificationResult


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
    along with its digest if found.

    IMPORTANT: Always use cgr.dev/{org}/<image> format where {org} is your
    Chainguard organization name. Never use cgr.dev/chainguard/<image>.

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
            return ImageVerificationResult(
                exists=True,
                image_reference=image_reference,
                digest=result.digest,
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
