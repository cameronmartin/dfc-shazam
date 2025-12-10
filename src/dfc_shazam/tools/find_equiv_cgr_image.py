"""Tool for looking up Chainguard image equivalents."""

from typing import Annotated, Any

from pydantic import Field

from dfc_shazam.chainctl import ChainctlClient, ChainctlError
from dfc_shazam.config import PUBLIC_REGISTRY, OrgSession
from dfc_shazam.mappings.images import (
    is_generic_base_image,
    lookup_chainguard_image as lookup_image_matches,
)
from dfc_shazam.mappings.image_runtime_config import (
    IMAGE_RUNTIME_CONFIG,
    MULTI_STAGE_COPY_GUIDANCE,
    DEFAULT_CHAINGUARD_USER,
)
from dfc_shazam.models import ChainguardImageResult, RuntimeRecommendation, VariantCapabilities
from dfc_shazam.tools.lookup_tag import (
    _extract_jdk_version,
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


def _extract_jdk_vendor_from_tag(tag: str) -> str | None:
    """Extract JDK vendor from Maven/Gradle tag.

    Examples:
        "3.9-eclipse-temurin-17" -> "eclipse-temurin"
        "8.5-jdk17-corretto" -> "corretto"
        "3.9-openjdk-17" -> "openjdk"
    """
    tag_lower = tag.lower()
    if "eclipse-temurin" in tag_lower or "temurin" in tag_lower:
        return "eclipse-temurin"
    if "corretto" in tag_lower:
        return "corretto"
    if "openjdk" in tag_lower:
        return "openjdk"
    return None


def _extract_jdk_version_from_tag(tag: str) -> str | None:
    """Extract JDK version from tag.

    Examples:
        "3.9-eclipse-temurin-17" -> "17"
        "17.0.2" -> "17"
        "latest" -> None
    """
    version = _extract_jdk_version(tag)
    return str(version) if version else None


async def _verify_image_exists(
    client: ChainctlClient, image: str, org: str, tag: str
) -> bool:
    """Verify an image:tag exists in the registry."""
    try:
        tags = await client.list_tags(image, org)
        tag_names = [t.tag for t in tags]
        # Check for exact match, or latest variants
        if tag in tag_names:
            return True
        if tag == "latest" and ("latest" in tag_names or "latest-dev" in tag_names):
            return True
        return False
    except ChainctlError:
        return False


async def _build_runtime_recommendations(
    config: dict[str, Any],
    org: str,
    matched_tag: str | None,
    client: ChainctlClient,
) -> tuple[list[RuntimeRecommendation], str | None]:
    """Build and verify runtime recommendations from config.

    Returns (recommendations, multi_stage_guidance).
    """
    recommendations: list[RuntimeRecommendation] = []
    guidance: str | None = None
    config_type = config["type"]

    if config_type == "compile_to_binary":
        # Go/Rust: recommend static or glibc-dynamic
        for opt in config["runtime_options"]:
            image_ref = f"cgr.dev/{org}/{opt['image']}:latest"
            verified = await _verify_image_exists(client, opt["image"], org, "latest")
            recommendations.append(
                RuntimeRecommendation(
                    image=opt["image"],
                    full_image_ref=image_ref,
                    description=opt["description"],
                    is_default=opt.get("default", False),
                    build_flags=opt.get("build_flags", []),
                    verified=verified,
                )
            )

    elif config_type == "sdk_runtime_pair":
        # JDK -> JRE
        runtime = config["runtime_image"]
        # Try to match version from the matched tag
        version_tag = "latest"
        if matched_tag:
            jdk_version = _extract_jdk_version_from_tag(matched_tag)
            if jdk_version:
                version_tag = jdk_version

        image_ref = f"cgr.dev/{org}/{runtime}:{version_tag}"
        verified = await _verify_image_exists(client, runtime, org, version_tag)
        recommendations.append(
            RuntimeRecommendation(
                image=runtime,
                full_image_ref=image_ref,
                description="Java Runtime Environment for running compiled JAR/WAR files",
                is_default=True,
                verified=verified,
            )
        )

    elif config_type == "build_tool_with_jdk":
        # Maven/Gradle: determine JRE based on JDK vendor in tag
        jdk_vendor = _extract_jdk_vendor_from_tag(matched_tag or "")
        jdk_runtime_mapping = config.get("jdk_runtime_mapping", {})
        runtime = jdk_runtime_mapping.get(jdk_vendor, config["default_runtime"])

        version_tag = "latest"
        if matched_tag:
            jdk_version = _extract_jdk_version_from_tag(matched_tag)
            if jdk_version:
                version_tag = jdk_version

        image_ref = f"cgr.dev/{org}/{runtime}:{version_tag}"
        verified = await _verify_image_exists(client, runtime, org, version_tag)
        vendor_info = f" (matched from {jdk_vendor})" if jdk_vendor else ""
        recommendations.append(
            RuntimeRecommendation(
                image=runtime,
                full_image_ref=image_ref,
                description=f"Java Runtime for compiled artifacts{vendor_info}",
                is_default=True,
                verified=verified,
            )
        )

    elif config_type == "same_family":
        # Python/Node: same image, different variant
        guidance = config.get("multi_stage_guidance")
        # No separate runtime image needed, just variant guidance

    return recommendations, guidance


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

    # Step 8: Check for build-only image and add runtime recommendations
    runtime_config = IMAGE_RUNTIME_CONFIG.get(chainguard_name)
    is_build_only = False
    runtime_recommendations: list[RuntimeRecommendation] = []
    multi_stage_guidance: str | None = None

    if runtime_config:
        config_type = runtime_config["type"]
        is_build_only = config_type in (
            "compile_to_binary",
            "sdk_runtime_pair",
            "build_tool_with_jdk",
        )

        runtime_recommendations, multi_stage_guidance = await _build_runtime_recommendations(
            runtime_config, org, best_tag, client
        )

    messages = []
    if public_warning:
        messages.append(public_warning.rstrip())

    if score < 1.0:
        messages.append(f"Matched '{original_tag}' to '{best_tag}' (confidence: {score:.0%})")

    if variant_lower != matched_variant:
        messages.append(
            f"Note: '{variant_lower}' variant was requested but '{best_tag}' was the best version match."
        )

    # Add runtime guidance to messages for build-only images
    if is_build_only and runtime_recommendations:
        verified_recs = [r for r in runtime_recommendations if r.verified]
        if verified_recs:
            rec_lines = []
            for rec in verified_recs:
                default_marker = " (recommended)" if rec.is_default else ""
                flags = f" [requires: {', '.join(rec.build_flags)}]" if rec.build_flags else ""
                rec_lines.append(
                    f"  - {rec.full_image_ref}{default_marker}{flags}: {rec.description}"
                )

            messages.append(
                f"\n🎯 MULTI-STAGE BUILD RECOMMENDED\n"
                f"'{chainguard_name}' is a build-only image. "
                f"For production, use a separate runtime image:\n"
                + "\n".join(rec_lines)
            )

        # Add COPY guidance for build-only images
        copy_guidance_template = MULTI_STAGE_COPY_GUIDANCE.get(chainguard_name)
        if copy_guidance_template:
            # Use default user as example, but note that actual user should be verified
            copy_example = copy_guidance_template.format(user=DEFAULT_CHAINGUARD_USER)
            messages.append(
                f"\n📋 COPY with --chown (CRITICAL): Chainguard images run as non-root. "
                f"Always use --chown when copying artifacts:\n  {copy_example}\n"
                f"NOTE: The runtime image user may differ (e.g., postgres, nginx). "
                f"Verify with get_migration_instructions_for_chainguard_image."
            )

    if multi_stage_guidance:
        messages.append(f"\n📦 Multi-stage tip: {multi_stage_guidance}")

    messages.append(
        f"\n⚠️ NEXT STEP: Call get_migration_instructions_for_chainguard_image "
        f"with image_reference=\"{full_image_ref}\" to retrieve "
        "best practices and conversion guidance BEFORE modifying any Dockerfile."
    )

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
        is_build_only=is_build_only,
        runtime_recommendations=runtime_recommendations,
        multi_stage_guidance=multi_stage_guidance,
        recommendation=f"Use {full_image_ref}",
        message=" ".join(messages) if messages else None,
    )
