"""Tool for looking up Chainguard image equivalents."""

from typing import Annotated

from pydantic import Field

from dfc_shazam.chainctl import ChainctlClient, ChainctlError
from dfc_shazam.config import PUBLIC_REGISTRY, OrgSession
from dfc_shazam.mappings.images import (
    is_generic_base_image,
    lookup_chainguard_image as lookup_image_matches,
)
from dfc_shazam.models import ChainguardImageResult, VariantCapabilities
from dfc_shazam.tools.lookup_tag import (
    _find_best_tag,
    _get_tag_variant,
    _has_slim_tags,
    _probe_variant_capabilities,
)


def _parse_image_reference(source_image_and_tag: str) -> tuple[str, str]:
    """Parse a source image reference into image name and tag.

    Strips registry prefixes and extracts the tag.

    Examples:
        "python:3.12" -> ("python", "3.12")
        "python" -> ("python", "latest")
        "ghcr.io/grafana/grafana:10.2.3" -> ("grafana", "10.2.3")
        "docker.io/library/nginx:alpine" -> ("nginx", "alpine")
        "node:18-alpine" -> ("node", "18-alpine")
    """
    ref = source_image_and_tag

    # Strip common registry prefixes
    for prefix in ["docker.io/library/", "docker.io/", "ghcr.io/", "quay.io/", "gcr.io/", "registry.k8s.io/"]:
        if ref.startswith(prefix):
            ref = ref[len(prefix):]
            break

    # Handle scoped images like ghcr.io/org/image:tag
    # Take the last path component as the image name
    if "/" in ref:
        ref = ref.split("/")[-1]

    # Split image and tag
    if ":" in ref:
        image_name, tag = ref.rsplit(":", 1)
    else:
        image_name = ref
        tag = "latest"

    return image_name, tag


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


def _format_variant_capabilities(capabilities: list[VariantCapabilities]) -> str:
    """Format variant capabilities for display in prompt.

    Sorts by recommendation (production first, then others, then development)
    and includes descriptions and badges.
    """
    # Sort: production recommended first, then no recommendation, then development
    def sort_key(c: VariantCapabilities) -> tuple[int, int]:
        rec_order = {"production": 0, None: 1, "development": 2}
        variant_order = {"distroless": 0, "slim": 1, "dev": 2}
        return (
            rec_order.get(c.recommended_for, 3),
            variant_order.get(c.variant, 99),
        )

    lines = []
    for cap in sorted(capabilities, key=sort_key):
        shell_status = "shell" if cap.has_shell else "no shell"
        apk_status = "apk" if cap.has_apk else "no apk"
        rec_badge = ""
        if cap.recommended_for == "production":
            rec_badge = " [RECOMMENDED for production]"
        elif cap.recommended_for == "development":
            rec_badge = " [RECOMMENDED for development]"
        lines.append(f"  - {cap.variant}: {shell_status}, {apk_status}{rec_badge}")
        if cap.description:
            lines.append(f"      {cap.description}")
    return "\n".join(lines)


async def find_equivalent_chainguard_image(
    source_image_and_tag: Annotated[
        str,
        Field(description="Source image name with optional tag (e.g., 'python', 'node:18', 'nginx:alpine', 'ghcr.io/grafana/grafana:latest')"),
    ],
    organization: Annotated[
        str | None,
        Field(
            description="Chainguard organization name. If not provided, available organizations will be listed."
        ),
    ] = None,
    variant: Annotated[
        str | None,
        Field(
            description="Image variant: 'distroless' (smallest, no shell), 'slim' (with shell), or 'dev' (shell + apk). "
            "If not provided, available variants will be listed for selection."
        ),
    ] = None,
) -> ChainguardImageResult:
    """Find Chainguard image equivalents for a source image.

    Returns the best match(es) with fuzzy matching support.
    Registry prefixes (docker.io, ghcr.io, quay.io, etc.) are automatically stripped.

    If no organization is selected, returns available organizations for user selection.
    If no variant is selected, returns available variants with their capabilities.
    """
    # Step 1: Handle organization selection
    if organization:
        # User provided an org - validate and store it
        available_orgs = OrgSession.get_available_orgs()
        if available_orgs and organization not in available_orgs:
            return ChainguardImageResult(
                found=False,
                source_image=source_image_and_tag,
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
                        source_image=source_image_and_tag,
                        message=f"🔐 ORGANIZATION SELECTION REQUIRED\n\n"
                        f"You have access to the following Chainguard organizations (SHOW ALL TO USER):\n{org_list}\n\n"
                        f"Ask the user which organization they want to use, then call this tool again "
                        f"with the 'organization' parameter set to their choice.\n\n"
                        f"Example: find_equivalent_chainguard_image(source_image_and_tag=\"{source_image_and_tag}\", organization=\"<chosen_org>\")",
                    )

        if use_public_registry:
            OrgSession.set_org(PUBLIC_REGISTRY)

    org = OrgSession.get_org()
    assert org is not None  # We checked is_org_selected above

    # Determine if we're using the public registry
    is_public = org == PUBLIC_REGISTRY

    # Step 2: Parse source_image_and_tag into image name and tag
    _, original_tag = _parse_image_reference(source_image_and_tag)

    # Step 3: Check if this is a generic base image
    is_generic = is_generic_base_image(source_image_and_tag)

    # Look up matches
    matches = lookup_image_matches(source_image_and_tag)

    if not matches:
        return ChainguardImageResult(
            found=False,
            source_image=source_image_and_tag,
            original_tag=original_tag,
            message=f"No known Chainguard equivalent for '{source_image_and_tag}'. "
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
            source_image=source_image_and_tag,
            chainguard_image=f"cgr.dev/{org}/{chainguard_name}",
            chainguard_image_name=chainguard_name,
            original_tag=original_tag,
            is_generic_base=True,
            recommendation=_generate_generic_guidance(),
            message=public_warning + f"Matched to '{chainguard_name}' but this is a generic base image.",
        )

    # Step 4: Fetch available tags and probe variant capabilities
    client = ChainctlClient()
    try:
        tags = await client.list_tags(chainguard_name, org)
        tag_names = [t.tag for t in tags]
    except ChainctlError as e:
        # If we can't fetch tags, return basic image match without variant info
        return ChainguardImageResult(
            found=True,
            source_image=source_image_and_tag,
            chainguard_image=f"cgr.dev/{org}/{chainguard_name}",
            chainguard_image_name=chainguard_name,
            original_tag=original_tag,
            is_generic_base=False,
            message=public_warning + f"Found match but failed to list tags: {e}",
        )

    # Determine available variants
    has_slim = _has_slim_tags(tag_names)
    available_variants = ["distroless", "dev"]
    if has_slim:
        available_variants.insert(1, "slim")

    # Probe variant capabilities
    variant_capabilities = await _probe_variant_capabilities(
        chainguard_name, org, tag_names, original_tag
    )

    # Validate variant if provided
    if variant is not None:
        variant_lower = variant.lower()
        if variant_lower not in ("distroless", "slim", "dev"):
            return ChainguardImageResult(
                found=True,
                source_image=source_image_and_tag,
                chainguard_image=f"cgr.dev/{org}/{chainguard_name}",
                chainguard_image_name=chainguard_name,
                original_tag=original_tag,
                is_generic_base=False,
                available_variants=available_variants,
                variant_capabilities=variant_capabilities,
                message=f"Invalid variant '{variant}'. Must be 'distroless', 'slim', or 'dev'.",
            )

        if variant_lower == "slim" and not has_slim:
            return ChainguardImageResult(
                found=True,
                source_image=source_image_and_tag,
                chainguard_image=f"cgr.dev/{org}/{chainguard_name}",
                chainguard_image_name=chainguard_name,
                original_tag=original_tag,
                is_generic_base=False,
                available_variants=available_variants,
                variant_capabilities=variant_capabilities,
                message=f"No -slim tags available for {chainguard_name}. "
                "Choose 'distroless' (no shell) or 'dev' (shell + apk).",
            )

    # Step 5: If no variant specified, prompt user with real capabilities
    if variant is None:
        caps_msg = _format_variant_capabilities(variant_capabilities)
        return ChainguardImageResult(
            found=True,
            source_image=source_image_and_tag,
            chainguard_image=f"cgr.dev/{org}/{chainguard_name}",
            chainguard_image_name=chainguard_name,
            original_tag=original_tag,
            is_generic_base=False,
            available_variants=available_variants,
            variant_capabilities=variant_capabilities,
            message=f"🎯 VARIANT SELECTION REQUIRED\n\n"
            f"Found Chainguard image: cgr.dev/{org}/{chainguard_name}\n"
            f"Original tag: {original_tag}\n\n"
            f"Available variants with capabilities:\n{caps_msg}\n\n"
            f"Ask the user which variant they need, then call this tool again with the 'variant' parameter.\n\n"
            f"Example: find_equivalent_chainguard_image(source_image_and_tag=\"{source_image_and_tag}\", variant=\"distroless\")",
        )

    # Step 6: Find best matching tag for the variant
    variant_lower = variant.lower()
    best_tag, score = _find_best_tag(original_tag, tag_names, preferred_variant=variant_lower)

    if best_tag is None or score < 0.3:
        return ChainguardImageResult(
            found=True,
            source_image=source_image_and_tag,
            chainguard_image=f"cgr.dev/{org}/{chainguard_name}",
            chainguard_image_name=chainguard_name,
            original_tag=original_tag,
            is_generic_base=False,
            variant=variant_lower,
            available_variants=available_variants,
            variant_capabilities=variant_capabilities,
            message=f"No suitable tag match found for '{original_tag}' with variant '{variant_lower}'. "
            f"Available tags: {', '.join(tag_names[:10])}{'...' if len(tag_names) > 10 else ''}",
        )

    # Step 7: Return full result with matched tag
    full_image_ref = f"cgr.dev/{org}/{chainguard_name}:{best_tag}"
    matched_variant = _get_tag_variant(best_tag)

    messages = []
    if public_warning:
        messages.append(public_warning.rstrip())

    if score < 1.0:
        messages.append(f"Matched '{original_tag}' to '{best_tag}' (confidence: {score:.0%})")

    if variant_lower != matched_variant:
        messages.append(
            f"Note: '{variant_lower}' variant was requested but '{best_tag}' was the best version match."
        )

    messages.append(f"\n⚠️ NEXT STEP: Call get_image_overview with image_name=\"{chainguard_name}\" to retrieve "
                   "best practices and conversion guidance BEFORE modifying any Dockerfile.")

    return ChainguardImageResult(
        found=True,
        source_image=source_image_and_tag,
        chainguard_image=f"cgr.dev/{org}/{chainguard_name}",
        chainguard_image_name=chainguard_name,
        original_tag=original_tag,
        matched_tag=best_tag,
        full_image_ref=full_image_ref,
        variant=matched_variant,
        is_generic_base=False,
        available_variants=available_variants,
        variant_capabilities=variant_capabilities,
        recommendation=f"Use {full_image_ref}",
        message=" ".join(messages) if messages else None,
    )
