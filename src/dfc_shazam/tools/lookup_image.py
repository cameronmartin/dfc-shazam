"""Tool for looking up Chainguard image equivalents."""

from typing import Annotated

from pydantic import Field

from dfc_shazam.chainctl import ChainctlClient, ChainctlError
from dfc_shazam.config import PUBLIC_REGISTRY, OrgSession
from dfc_shazam.mappings.images import (
    ImageMatch,
    is_generic_base_image,
    lookup_chainguard_image as lookup_image_matches,
)
from dfc_shazam.models import ChainguardImageResult


def _get_next_step_guidance(org: str, is_public: bool = False) -> str:
    """Generate next step guidance with org-specific messaging."""
    if is_public:
        return (
            "\n\n⚠️ NEXT STEP REQUIRED: Call get_image_overview with this image name to retrieve "
            "best practices, entrypoint details, user/path configurations, and conversion guidance "
            "BEFORE modifying any Dockerfile."
        )
    return (
        f"\n\n🔒 IMPORTANT: All Chainguard images must use cgr.dev/{org}/<image> format. "
        f"Never use cgr.dev/chainguard/<image>."
        "\n\n⚠️ NEXT STEP REQUIRED: Call get_image_overview with this image name to retrieve "
        "best practices, entrypoint details, user/path configurations, and conversion guidance "
        "BEFORE modifying any Dockerfile."
    )


def _format_matches(matches: list[ImageMatch], org: str) -> str:
    """Format match results for display."""
    lines = []
    for match in matches:
        image_ref = f"cgr.dev/{org}/{match.chainguard_image}"
        if match.score == 1.0:
            lines.append(f"  - {image_ref}")
        else:
            lines.append(f"  - {image_ref} (matched '{match.matched_alias}', score: {match.score:.0%})")
    return "\n".join(lines)


def _generate_generic_guidance() -> str:
    """Generate guidance for generic base images."""
    return """This is a generic base image. Chainguard recommends using a workload-specific image instead.

Review the Dockerfile to identify the primary workload installed onto this base image, then call this tool again with that workload type (e.g., "python", "node", "jdk", "nginx", "postgres").

If the Dockerfile only runs shell scripts without installing a runtime, use "chainguard-base".
If it copies in a static binary with no shell needed, use "static"."""


def _get_public_registry_warning() -> str:
    """Generate warning about public registry limitations."""
    return (
        "⚠️ USING PUBLIC REGISTRY (cgr.dev/chainguard/)\n\n"
        "chainctl is not authenticated or no organization is available. "
        "Falling back to the public Chainguard registry.\n\n"
        "LIMITATIONS:\n"
        "- Only 'latest' and 'latest-dev' tags are available\n"
        "- Only a subset of images are publicly available\n"
        "- No access to versioned tags (e.g., python:3.12)\n"
        "- No FIPS or other enterprise variants\n\n"
        "To access versioned tags and the full image catalog, run:\n"
        "  chainctl auth login\n\n"
        "Then re-run this tool to select your organization."
    )


async def lookup_chainguard_image(
    source_image: Annotated[
        str,
        Field(description="Source image name (e.g., 'python', 'node:18', 'nginx', 'ghcr.io/grafana/grafana')"),
    ],
    organization: Annotated[
        str | None,
        Field(
            description="Chainguard organization name. If not provided on first call, "
            "available organizations will be listed for the user to choose from."
        ),
    ] = None,
) -> ChainguardImageResult:
    """Find Chainguard image equivalents for a source image.

    Looks up the Chainguard equivalent for a given source image.
    Returns the best match(es) and guidance on selection.

    Supports exact matching and fuzzy matching for typos/partial names.
    Registry prefixes (docker.io, ghcr.io, quay.io, ECR, ACR, etc.) are automatically stripped.

    ORGANIZATION SELECTION:
    On first call, if no organization is specified, this tool will retrieve the list of
    organizations you have access to (from chainctl auth status) and ask you to select one.
    You MUST then call this tool again with the organization parameter set to the user's choice.

    IMPORTANT: Always use cgr.dev/{org}/<image> format, never cgr.dev/chainguard/<image>.
    The {org} should be the customer's Chainguard organization name.

    CRITICAL: After determining the Chainguard image, you MUST call get_image_overview to
    retrieve best practices and conversion guidance specific to that image before modifying
    any Dockerfile. The overview contains essential information about entrypoints, users,
    paths, and other image-specific details required for a successful conversion.

    IMPORTANT - Package Installation with apk:
    Chainguard images run as non-root by default. When installing packages with apk add,
    you MUST switch to root first, then switch back to nonroot:

        USER root
        RUN apk add --no-cache <packages>
        USER nonroot

    Never leave the container running as root. Always drop back to USER nonroot after
    package installation for security best practices.
    """
    # Handle organization selection
    if organization:
        # User provided an org - validate and store it
        available_orgs = OrgSession.get_available_orgs()
        if available_orgs and organization not in available_orgs:
            return ChainguardImageResult(
                found=False,
                source_image=source_image,
                message=f"Organization '{organization}' is not in your available organizations. "
                f"Available: {', '.join(available_orgs)}",
            )
        OrgSession.set_org(organization)

    # Check if we have an org selected
    use_public_registry = False
    if not OrgSession.is_org_selected():
        # Need to fetch available orgs and prompt user
        client = ChainctlClient()
        try:
            auth_status = await client.get_auth_status()
        except ChainctlError:
            # chainctl failed - fall back to public registry
            use_public_registry = True
            auth_status = None

        if auth_status is not None:
            if not auth_status.valid:
                # Not authenticated - fall back to public registry
                use_public_registry = True
            elif not auth_status.organizations:
                # No orgs available - fall back to public registry
                use_public_registry = True
            else:
                # Cache the available orgs
                OrgSession.set_available_orgs(auth_status.organizations)

                # Auto-select if only one org available
                if len(auth_status.organizations) == 1:
                    OrgSession.set_org(auth_status.organizations[0])
                else:
                    # Multiple orgs - prompt user to select
                    org_list = "\n".join(f"  - {org}" for org in auth_status.organizations)

                    return ChainguardImageResult(
                        found=False,
                        source_image=source_image,
                        message=f"🔐 ORGANIZATION SELECTION REQUIRED\n\n"
                        f"You have access to the following Chainguard organizations:\n{org_list}\n\n"
                        f"Please ask the user which organization they want to use, then call this tool again "
                        f"with the 'organization' parameter set to their choice.\n\n"
                        f"Example: lookup_chainguard_image(source_image=\"{source_image}\", organization=\"<chosen_org>\")",
                    )

        if use_public_registry:
            OrgSession.set_org(PUBLIC_REGISTRY)

    org = OrgSession.get_org()
    assert org is not None  # We checked is_org_selected above

    # Determine if we're using the public registry
    is_public = org == PUBLIC_REGISTRY

    # Check if this is a generic base image
    is_generic = is_generic_base_image(source_image)

    # Look up matches
    matches = lookup_image_matches(source_image)

    if not matches:
        return ChainguardImageResult(
            found=False,
            source_image=source_image,
            message=f"No known Chainguard equivalent for '{source_image}'. "
            "Try searching on https://images.chainguard.dev/ or describe the workload type.",
        )

    # Get the best match
    best_match = matches[0]
    chainguard_name = best_match.chainguard_image

    # Build public registry warning if needed
    public_warning = _get_public_registry_warning() + "\n\n" if is_public else ""

    # For generic base images, return guidance to narrow down
    if is_generic:
        return ChainguardImageResult(
            found=True,
            source_image=source_image,
            chainguard_image=f"cgr.dev/{org}/{chainguard_name}",
            chainguard_image_name=chainguard_name,
            is_generic_base=True,
            recommendation=_generate_generic_guidance(),
            message=public_warning + f"Matched to '{chainguard_name}' but this is a generic base image."
            + _get_next_step_guidance(org, is_public),
        )

    # For specific images, return the match(es)
    if len(matches) == 1 and best_match.score == 1.0:
        # Single exact match - simple case
        return ChainguardImageResult(
            found=True,
            source_image=source_image,
            chainguard_image=f"cgr.dev/{org}/{chainguard_name}",
            chainguard_image_name=chainguard_name,
            is_generic_base=False,
            recommendation=f"Use cgr.dev/{org}/{chainguard_name}" + _get_next_step_guidance(org, is_public),
            message=public_warning.rstrip() if public_warning else None,
        )

    # Multiple matches or fuzzy match - provide options
    formatted = _format_matches(matches, org)

    if best_match.score < 1.0:
        message = f"No exact match. Best fuzzy matches:\n{formatted}"
    else:
        message = f"Multiple Chainguard images match:\n{formatted}"

    return ChainguardImageResult(
        found=True,
        source_image=source_image,
        chainguard_image=f"cgr.dev/{org}/{chainguard_name}",
        chainguard_image_name=chainguard_name,
        is_generic_base=False,
        recommendation=f"Recommended: cgr.dev/{org}/{chainguard_name}" + _get_next_step_guidance(org, is_public),
        message=public_warning + message if public_warning else message,
    )
