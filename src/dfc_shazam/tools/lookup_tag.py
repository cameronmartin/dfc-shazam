"""Tool for looking up best matching Chainguard tag."""

import asyncio
import re
import shutil
from typing import Annotated

from pydantic import Field

from dfc_shazam.chainctl import ChainctlClient, ChainctlError
from dfc_shazam.config import OrgSession
from dfc_shazam.models import TagLookupResult, VariantCapabilities

# Re-export for use by image_docs.py
__all__ = ["lookup_tag", "probe_image_capabilities"]


def _parse_version(tag: str) -> tuple[list[int], str, str]:
    """Parse a version tag into prefix, numeric components, and suffix.

    Returns tuple of (version_parts, suffix, prefix) where:
    - version_parts is a list of integers
    - suffix is the remaining string (e.g., "-dev", "-slim")
    - prefix is any text before the version (e.g., "adoptium-openjdk-")

    Examples:
        "3.12" -> ([3, 12], "", "")
        "3.12-dev" -> ([3, 12], "-dev", "")
        "latest" -> ([], "latest", "")
        "18-alpine" -> ([18], "-alpine", "")
        "adoptium-openjdk-17" -> ([17], "", "adoptium-openjdk-")
        "adoptium-openjdk-17.0.13-dev" -> ([17, 0, 13], "-dev", "adoptium-openjdk-")
        "openjdk-17-jre" -> ([17], "-jre", "openjdk-")
    """
    # First try: Match version numbers at the start (standard format)
    match = re.match(r"^(\d+(?:\.\d+)*)(.*)?$", tag)
    if match:
        version_str = match.group(1)
        suffix = match.group(2) or ""
        parts = [int(p) for p in version_str.split(".")]
        return parts, suffix, ""

    # Second try: Find version numbers after a prefix (e.g., "adoptium-openjdk-17")
    # Look for patterns like "prefix-N" or "prefix-N.N.N"
    match = re.match(r"^(.+?-)(\d+(?:\.\d+)*)(.*)?$", tag)
    if match:
        prefix = match.group(1)
        version_str = match.group(2)
        suffix = match.group(3) or ""
        parts = [int(p) for p in version_str.split(".")]
        return parts, suffix, prefix

    return [], tag, ""


def _get_tag_variant(tag: str) -> str:
    """Determine the variant of a tag (distroless, slim, or dev)."""
    tag_lower = tag.lower()
    if "-dev" in tag_lower:
        return "dev"
    elif "-slim" in tag_lower:
        return "slim"
    return "distroless"


def _extract_jdk_version(tag: str) -> int | None:
    """Extract JDK/Java version from a tag.

    Recognizes patterns like:
    - jdk17, jdk-17, jdk11
    - temurin-17, eclipse-temurin-17
    - openjdk-17, openjdk17
    - corretto-17, corretto17
    - java17, java-17

    Returns the JDK version number or None if not found.
    """
    tag_lower = tag.lower()

    # Patterns for JDK version extraction (order matters - more specific first)
    patterns = [
        r"(?:eclipse-)?temurin-(\d+)",  # temurin-17, eclipse-temurin-17
        r"(?:amazon-)?corretto-?(\d+)",  # corretto-17, amazon-corretto-17
        r"openjdk-?(\d+)",  # openjdk-17, openjdk17
        r"jdk-?(\d+)",  # jdk17, jdk-17
        r"jre-?(\d+)",  # jre17, jre-17
        r"java-?(\d+)",  # java17, java-17
    ]

    for pattern in patterns:
        match = re.search(pattern, tag_lower)
        if match:
            return int(match.group(1))

    return None


def _score_tag_match(
    original_tag: str, candidate_tag: str, preferred_variant: str = "distroless"
) -> float:
    """Score how well a candidate tag matches the original tag.

    Returns a score from 0.0 to 1.0, where 1.0 is a perfect match.

    Args:
        original_tag: The original tag to match
        candidate_tag: The candidate Chainguard tag
        preferred_variant: 'distroless', 'slim', or 'dev'
    """
    # Normalize tags
    orig_lower = original_tag.lower()
    cand_lower = candidate_tag.lower()

    candidate_variant = _get_tag_variant(candidate_tag)
    variant_matches = candidate_variant == preferred_variant

    # Special handling for "latest" - do this first before exact match checks
    if orig_lower == "latest":
        if cand_lower == f"latest-{preferred_variant}" or (
            preferred_variant == "distroless" and cand_lower == "latest"
        ):
            return 1.0
        if cand_lower.startswith("latest"):
            return 0.7 if variant_matches else 0.4
        # Any other tag is a fallback
        return 0.3

    # Check if candidate is the preferred variant version of the original tag
    # e.g., original="18", candidate="18-dev" when preferred_variant="dev"
    if preferred_variant != "distroless":
        if cand_lower == f"{orig_lower}-{preferred_variant}":
            return 1.0

    # Exact match (for distroless, this is the base tag without suffix)
    if original_tag == candidate_tag:
        return 1.0 if variant_matches else 0.5

    # Case-insensitive exact match
    if orig_lower == cand_lower:
        return 0.99 if variant_matches else 0.49

    orig_parts, orig_suffix, _ = _parse_version(orig_lower)
    cand_parts, cand_suffix, _ = _parse_version(cand_lower)

    score = 0.0

    # If both have version numbers, compare them
    if orig_parts and cand_parts:
        # Check if major version matches
        if orig_parts[0] == cand_parts[0]:
            score = 0.6

            # Check minor version if present
            if len(orig_parts) > 1 and len(cand_parts) > 1:
                if orig_parts[1] == cand_parts[1]:
                    score = 0.8

                    # Check patch version if present
                    if len(orig_parts) > 2 and len(cand_parts) > 2:
                        if orig_parts[2] == cand_parts[2]:
                            score = 0.9
            elif len(orig_parts) == 1 and len(cand_parts) >= 1:
                # Original only specified major, candidate has more detail
                score = 0.7

        # Penalize if candidate has extra version specificity we don't want
        if len(cand_parts) > len(orig_parts):
            score *= 0.95

    # Handle JDK version matching (important for Java-based images like maven, gradle)
    orig_jdk = _extract_jdk_version(orig_lower)
    cand_jdk = _extract_jdk_version(cand_lower)

    if orig_jdk is not None and cand_jdk is not None:
        if orig_jdk == cand_jdk:
            score += 0.15  # Significant bonus for matching JDK version
        else:
            score *= 0.3  # Heavy penalty for JDK version mismatch
    elif orig_jdk is not None and cand_jdk is None:
        # Original specifies JDK but candidate doesn't - mild penalty
        score *= 0.8

    # Handle variant preference
    if variant_matches:
        score += 0.1  # Bonus for matching variant
    else:
        score *= 0.5  # Penalize non-matching variant

    # Handle suffix matching (excluding variant suffixes)
    orig_suffix_clean = orig_suffix.replace("-dev", "").replace("-slim", "")
    cand_suffix_clean = cand_suffix.replace("-dev", "").replace("-slim", "")

    if orig_suffix_clean and cand_suffix_clean:
        if orig_suffix_clean == cand_suffix_clean:
            score += 0.05
    elif orig_suffix_clean and not cand_suffix_clean:
        # Original has suffix (like -alpine), candidate doesn't
        # This is expected for Chainguard - don't penalize too much
        pass

    return min(max(score, 0.0), 1.0)


def _find_best_tag(
    original_tag: str, available_tags: list[str], preferred_variant: str = "distroless"
) -> tuple[str | None, float]:
    """Find the best matching tag from available tags.

    Returns tuple of (best_tag, score).
    """
    if not available_tags:
        return None, 0.0

    best_tag = None
    best_score = 0.0

    for tag in available_tags:
        score = _score_tag_match(original_tag, tag, preferred_variant=preferred_variant)
        if score > best_score:
            best_score = score
            best_tag = tag

    return best_tag, best_score


def _get_sorted_tags(
    original_tag: str,
    all_tags: list[str],
    preferred_variant: str,
    limit: int = 20,
) -> list[str]:
    """Sort tags by relevance to original_tag and return top N.

    This ensures the most relevant tags appear in the available_tags list,
    rather than just taking the first N tags in whatever order chainctl returns them.
    """
    if not all_tags:
        return []

    scored = [(tag, _score_tag_match(original_tag, tag, preferred_variant)) for tag in all_tags]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [tag for tag, _ in scored[:limit]]


def _has_slim_tags(tags: list[str]) -> bool:
    """Check if any tags have the -slim variant."""
    return any("-slim" in tag.lower() for tag in tags)


async def probe_image_capabilities(image_reference: str) -> tuple[bool, bool] | None:
    """Probe an image to determine shell and apk availability.

    Returns (has_shell, has_apk) or None if probing fails.
    Results are cached in OrgSession to avoid duplicate crane calls.
    """
    # Check cache first
    cached = OrgSession.get_image_capabilities(image_reference)
    if cached is not None:
        return cached

    crane_path = shutil.which("crane")
    if crane_path is None:
        return None

    try:
        # Run crane export | tar -tf - to list files
        proc = await asyncio.create_subprocess_shell(
            f"{crane_path} export {image_reference} - | tar -tf -",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=120)

        if proc.returncode != 0:
            return None

        file_list = stdout_bytes.decode()

        # Check for shell binaries
        has_shell = False
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
        has_apk = "sbin/apk" in file_list or "usr/bin/apk" in file_list

        # Cache the result
        OrgSession.set_image_capabilities(image_reference, has_shell, has_apk)

        return has_shell, has_apk

    except (asyncio.TimeoutError, Exception):
        return None


# Alias for backward compatibility within this module
_probe_image_capabilities = probe_image_capabilities


def _find_representative_tags(
    tags: list[str], base_version: str
) -> dict[str, str | None]:
    """Find representative tags for each variant based on a base version.

    Returns dict mapping variant name to tag (or None if not available).
    """
    # We want to find tags for distroless, slim, dev variants
    # For a base_version like "23", we look for "23", "23-slim", "23-dev"
    # For "latest", we look for "latest", "latest-slim", "latest-dev"

    result: dict[str, str | None] = {
        "distroless": None,
        "slim": None,
        "dev": None,
    }

    tags_lower = {t.lower(): t for t in tags}

    # Normalize base version
    base = base_version.lower()

    # Remove any existing variant suffix from base
    for suffix in ["-dev", "-slim"]:
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break

    # Look for exact matches first
    if base in tags_lower:
        result["distroless"] = tags_lower[base]
    if f"{base}-slim" in tags_lower:
        result["slim"] = tags_lower[f"{base}-slim"]
    if f"{base}-dev" in tags_lower:
        result["dev"] = tags_lower[f"{base}-dev"]

    # For any variant not found, try "latest" variants as fallback
    if base != "latest":
        if result["distroless"] is None and "latest" in tags_lower:
            result["distroless"] = tags_lower["latest"]
        if result["slim"] is None and "latest-slim" in tags_lower:
            result["slim"] = tags_lower["latest-slim"]
        if result["dev"] is None and "latest-dev" in tags_lower:
            result["dev"] = tags_lower["latest-dev"]

    # If slim is still not found but slim tags exist, find any slim tag to probe
    if result["slim"] is None:
        for tag_lower, tag_original in tags_lower.items():
            if tag_lower.endswith("-slim"):
                result["slim"] = tag_original
                break

    return result


def _get_variant_description(has_shell: bool, has_apk: bool) -> tuple[str, str | None]:
    """Generate description and recommendation based on actual probed capabilities.

    Returns (description, recommended_for) tuple.
    recommended_for is 'production', 'development', or None.
    """
    if has_apk:
        return (
            "Full image with shell and apk package manager. Use for building or when you need to install packages.",
            "development",
        )
    elif has_shell:
        return (
            "Minimal image with shell but no package manager. Good for apps requiring shell.",
            None,
        )
    else:
        return (
            "Smallest image, no shell, no apk. Best for production with minimal attack surface.",
            "production",
        )


async def _probe_variant_capabilities(
    image_name: str, org: str, tags: list[str], base_version: str
) -> list[VariantCapabilities]:
    """Probe available variants to determine their actual capabilities.

    Returns list of VariantCapabilities for each variant that can be probed.
    """
    representative_tags = _find_representative_tags(tags, base_version)

    capabilities: list[VariantCapabilities] = []

    # Probe each variant in parallel
    async def probe_variant(variant: str, tag: str | None) -> VariantCapabilities | None:
        if tag is None:
            return None

        image_ref = f"cgr.dev/{org}/{image_name}:{tag}"
        result = await _probe_image_capabilities(image_ref)

        if result is None:
            return None

        has_shell, has_apk = result
        description, recommended_for = _get_variant_description(has_shell, has_apk)
        return VariantCapabilities(
            variant=variant,
            has_shell=has_shell,
            has_apk=has_apk,
            probed_tag=tag,
            description=description,
            recommended_for=recommended_for,
        )

    # Probe all variants in parallel
    tasks = [
        probe_variant(variant, tag)
        for variant, tag in representative_tags.items()
    ]
    results = await asyncio.gather(*tasks)

    for result in results:
        if result is not None:
            capabilities.append(result)

    return capabilities


async def lookup_tag(
    chainguard_image: Annotated[
        str,
        Field(description="Chainguard image name (e.g., 'python', 'node', 'nginx')"),
    ],
    original_image: Annotated[
        str,
        Field(description="Original source image name (e.g., 'python', 'node:18-alpine')"),
    ],
    original_tag: Annotated[
        str,
        Field(description="Original tag to find a match for (e.g., '3.12', '18-alpine', 'latest')"),
    ],
    variant: Annotated[
        str,
        Field(description="Image variant: 'distroless', 'slim', or 'dev'"),
    ],
) -> TagLookupResult:
    """Find the best matching Chainguard tag for an original image tag.

    Lists available tags and finds the closest match based on version and variant.
    Returns variant_capabilities showing actual shell/apk availability for each variant.

    Requires find_equivalent_chainguard_image to be called first to select an organization.
    """
    # Validate variant parameter
    variant_lower = variant.lower()
    if variant_lower not in ("distroless", "slim", "dev"):
        return TagLookupResult(
            found=False,
            chainguard_image=chainguard_image,
            original_image=original_image,
            original_tag=original_tag,
            message=f"Invalid variant '{variant}'. Must be 'distroless', 'slim', or 'dev'.",
        )

    org = OrgSession.get_org()
    if org is None:
        return TagLookupResult(
            found=False,
            chainguard_image=chainguard_image,
            original_image=original_image,
            original_tag=original_tag,
            message="No organization selected. Call find_equivalent_chainguard_image first to select an organization.",
        )

    client = ChainctlClient()

    try:
        tags = await client.list_tags(chainguard_image, org)
        tag_names = [t.tag for t in tags]
    except ChainctlError as e:
        return TagLookupResult(
            found=False,
            chainguard_image=chainguard_image,
            original_image=original_image,
            original_tag=original_tag,
            message=f"Failed to list tags: {e}",
        )

    if not tag_names:
        return TagLookupResult(
            found=False,
            chainguard_image=chainguard_image,
            original_image=original_image,
            original_tag=original_tag,
            message=f"No tags found for cgr.dev/{org}/{chainguard_image}",
        )

    # Check if slim tags are available and warn if requested but not available
    has_slim = _has_slim_tags(tag_names)
    if variant_lower == "slim" and not has_slim:
        return TagLookupResult(
            found=False,
            chainguard_image=chainguard_image,
            original_image=original_image,
            original_tag=original_tag,
            available_tags=_get_sorted_tags(original_tag, tag_names, "distroless"),
            variant=variant_lower,
            has_slim_variant=False,
            message=f"No -slim tags available for {chainguard_image}. "
            "Choose 'distroless' (no shell) or 'dev' (shell + apk).",
        )

    best_tag, score = _find_best_tag(original_tag, tag_names, preferred_variant=variant_lower)

    # Sort tags by relevance for display
    sorted_tags = _get_sorted_tags(original_tag, tag_names, variant_lower)

    # Probe variant capabilities in parallel (use best_tag or original_tag as base)
    base_version = best_tag if best_tag else original_tag
    variant_capabilities = await _probe_variant_capabilities(
        chainguard_image, org, tag_names, base_version
    )

    if best_tag is None or score < 0.3:
        return TagLookupResult(
            found=False,
            chainguard_image=chainguard_image,
            original_image=original_image,
            original_tag=original_tag,
            available_tags=sorted_tags,
            variant=variant_lower,
            has_slim_variant=has_slim,
            variant_capabilities=variant_capabilities,
            message=f"No suitable tag match found for '{original_tag}'. "
            f"Available tags: {', '.join(sorted_tags[:10])}{'...' if len(sorted_tags) > 10 else ''}",
        )

    messages = []
    if score < 1.0:
        messages.append(f"Matched '{original_tag}' to '{best_tag}' (confidence: {score:.0%})")

    matched_variant = _get_tag_variant(best_tag)
    if variant_lower != matched_variant:
        suffix = f"-{variant_lower}" if variant_lower != "distroless" else ""
        messages.append(
            f"Note: '{variant_lower}' variant was requested but '{best_tag}' was the best version match. "
            f"You may want to use '{original_tag}{suffix}' if available."
        )

    # Add variant capabilities summary to messages
    if variant_capabilities:
        caps_summary = []
        for cap in sorted(variant_capabilities, key=lambda c: c.variant):
            shell_status = "shell" if cap.has_shell else "no shell"
            apk_status = "apk" if cap.has_apk else "no apk"
            caps_summary.append(f"{cap.variant}({cap.probed_tag}): {shell_status}, {apk_status}")
        messages.append(f"Variant capabilities: {'; '.join(caps_summary)}")

    return TagLookupResult(
        found=True,
        chainguard_image=chainguard_image,
        original_image=original_image,
        original_tag=original_tag,
        matched_tag=best_tag,
        full_image_ref=f"cgr.dev/{org}/{chainguard_image}:{best_tag}",
        available_tags=sorted_tags,
        variant=matched_variant,
        has_slim_variant=has_slim,
        variant_capabilities=variant_capabilities,
        message=" ".join(messages) if messages else None,
    )
