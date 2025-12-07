"""Tool for looking up best matching Chainguard tag."""

import re
from typing import Annotated

from pydantic import Field

from dfc_shazam.chainctl import ChainctlClient, ChainctlError
from dfc_shazam.config import OrgSession
from dfc_shazam.models import TagLookupResult


def _parse_version(tag: str) -> tuple[list[int], str]:
    """Parse a version tag into numeric components and suffix.

    Returns tuple of (version_parts, suffix) where version_parts is a list
    of integers and suffix is the remaining string (e.g., "-dev", "-slim").

    Examples:
        "3.12" -> ([3, 12], "")
        "3.12-dev" -> ([3, 12], "-dev")
        "latest" -> ([], "latest")
        "18-alpine" -> ([18], "-alpine")
    """
    # Match version numbers at the start
    match = re.match(r"^(\d+(?:\.\d+)*)(.*)?$", tag)
    if match:
        version_str = match.group(1)
        suffix = match.group(2) or ""
        parts = [int(p) for p in version_str.split(".")]
        return parts, suffix
    return [], tag


def _get_tag_variant(tag: str) -> str:
    """Determine the variant of a tag (distroless, slim, or dev)."""
    tag_lower = tag.lower()
    if "-dev" in tag_lower:
        return "dev"
    elif "-slim" in tag_lower:
        return "slim"
    return "distroless"


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

    orig_parts, orig_suffix = _parse_version(orig_lower)
    cand_parts, cand_suffix = _parse_version(cand_lower)

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
        Field(
            description="Image variant to use. YOU MUST ASK THE USER before calling this tool. "
            "Valid values: 'distroless', 'slim', or 'dev'. "
            "Ask: 'Which image variant do you need? (distroless/slim/dev)' - "
            "distroless: Smallest, most secure, no shell or package manager. "
            "slim: Includes shell but no package manager. "
            "dev: Includes shell and apk package manager for building/debugging."
        ),
    ],
) -> TagLookupResult:
    """Find the best matching Chainguard tag for an original image tag.

    Lists available tags for a Chainguard image using chainctl and finds
    the closest match to the original tag based on version and variant matching.

    CRITICAL: Before calling this tool, you MUST prompt the user to choose a variant.

    If -slim tags are available for the image, ask:
        "Which image variant do you need?
        - distroless: Smallest and most secure, no shell or package manager (recommended for production)
        - slim: Includes a shell but no package manager
        - dev: Includes shell and apk package manager (for building apps or debugging)"

    If NO -slim tags are available, ask:
        "Which image variant do you need?
        - distroless: Smallest and most secure, no shell or package manager (recommended for production)
        - dev: Includes shell and apk package manager (for building apps or debugging)"

    DO NOT infer or guess the answer from the Dockerfile - explicitly ask the user.

    Based on user response, set variant to: 'distroless', 'slim', or 'dev'

    IMPORTANT - Variant capabilities:
    - distroless: No shell, no apk. Cannot run shell commands or install packages.
    - slim: Has shell (/bin/sh), but no apk. Can run shell scripts but cannot install packages.
    - dev: Has shell and apk. Can run shell scripts and install packages with apk add.

    If the Dockerfile needs to install packages but user wants distroless for production,
    use a multi-stage build pattern:
        FROM cgr.dev/{org}/python:latest-dev AS builder
        USER root
        RUN apk add --no-cache build-base
        RUN pip install --user mypackage
        USER nonroot

        FROM cgr.dev/{org}/python:latest
        COPY --from=builder /home/nonroot/.local /home/nonroot/.local

    NOTE: You must call lookup_chainguard_image first to select an organization
    before using this tool.
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
            message="No organization selected. Call lookup_chainguard_image first to select an organization.",
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

    if best_tag is None or score < 0.3:
        return TagLookupResult(
            found=False,
            chainguard_image=chainguard_image,
            original_image=original_image,
            original_tag=original_tag,
            available_tags=sorted_tags,
            variant=variant_lower,
            has_slim_variant=has_slim,
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
        message=" ".join(messages) if messages else None,
    )
