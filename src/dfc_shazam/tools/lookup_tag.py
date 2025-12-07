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


def _score_tag_match(
    original_tag: str, candidate_tag: str, prefer_dev: bool = False
) -> float:
    """Score how well a candidate tag matches the original tag.

    Returns a score from 0.0 to 1.0, where 1.0 is a perfect match.

    Args:
        original_tag: The original tag to match
        candidate_tag: The candidate Chainguard tag
        prefer_dev: If True, prefer -dev variants
    """
    # Normalize tags
    orig_lower = original_tag.lower()
    cand_lower = candidate_tag.lower()

    is_dev_candidate = "-dev" in cand_lower

    # Special handling for "latest" - do this first before exact match checks
    if orig_lower == "latest":
        if cand_lower == "latest-dev":
            return 1.0 if prefer_dev else 0.5
        if cand_lower == "latest":
            return 0.7 if prefer_dev else 1.0
        # Any other tag is a fallback
        return 0.3

    # Check if candidate is the -dev version of the original tag
    # e.g., original="18", candidate="18-dev"
    if cand_lower == f"{orig_lower}-dev":
        return 1.0 if prefer_dev else 0.5

    # Exact match - but if we prefer dev, penalize non-dev exact matches
    if original_tag == candidate_tag:
        return 0.7 if prefer_dev else 1.0

    # Case-insensitive exact match
    if orig_lower == cand_lower:
        return 0.69 if prefer_dev else 0.99

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

    # Handle -dev variant preference (is_dev_candidate defined at top)
    if prefer_dev:
        # We want a -dev variant
        if is_dev_candidate:
            score += 0.1  # Bonus for -dev when we want it
        else:
            score *= 0.7  # Penalize non-dev when we want dev
    else:
        # We prefer distroless (non-dev)
        if is_dev_candidate:
            score *= 0.5  # Penalize -dev when we don't need it

    # Handle suffix matching (excluding -dev which is handled above)
    orig_suffix_no_dev = orig_suffix.replace("-dev", "")
    cand_suffix_no_dev = cand_suffix.replace("-dev", "")

    if orig_suffix_no_dev and cand_suffix_no_dev:
        if orig_suffix_no_dev == cand_suffix_no_dev:
            score += 0.05
    elif orig_suffix_no_dev and not cand_suffix_no_dev:
        # Original has suffix (like -alpine), candidate doesn't
        # This is expected for Chainguard - don't penalize too much
        pass

    return min(max(score, 0.0), 1.0)


def _find_best_tag(
    original_tag: str, available_tags: list[str], prefer_dev: bool = False
) -> tuple[str | None, float]:
    """Find the best matching tag from available tags.

    Returns tuple of (best_tag, score).
    """
    if not available_tags:
        return None, 0.0

    best_tag = None
    best_score = 0.0

    for tag in available_tags:
        score = _score_tag_match(original_tag, tag, prefer_dev=prefer_dev)
        if score > best_score:
            best_score = score
            best_tag = tag

    return best_tag, best_score


def _get_sorted_tags(
    original_tag: str,
    all_tags: list[str],
    prefer_dev: bool,
    limit: int = 20,
) -> list[str]:
    """Sort tags by relevance to original_tag and return top N.

    This ensures the most relevant tags appear in the available_tags list,
    rather than just taking the first N tags in whatever order chainctl returns them.
    """
    if not all_tags:
        return []

    scored = [(tag, _score_tag_match(original_tag, tag, prefer_dev)) for tag in all_tags]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [tag for tag, _ in scored[:limit]]


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
    require_dev: Annotated[
        bool,
        Field(
            description="Whether to use the -dev variant. YOU MUST EXPLICITLY ASK THE USER: "
            "'Do you need shell access or package manager (apk) in your final container image?' "
            "Set True if user says yes, False if user says no. DO NOT GUESS - always ask first."
        ),
    ],
) -> TagLookupResult:
    """Find the best matching Chainguard tag for an original image tag.

    Lists available tags for a Chainguard image using chainctl and finds
    the closest match to the original tag based on version and variant matching.

    CRITICAL: Before calling this tool, you MUST prompt the user with this question:
        "Do you need shell access or a package manager (apk) in your final container image?
        - Choose YES if you need to: run shell scripts, install packages at runtime, or debug interactively
        - Choose NO for a smaller, more secure distroless image (recommended for production)"

    DO NOT infer or guess the answer from the Dockerfile - explicitly ask the user.

    Based on user response:
    - User says YES -> require_dev=True (returns -dev variant with shell/apk)
    - User says NO -> require_dev=False (returns distroless variant, no shell)

    IMPORTANT - Non-dev (distroless) variant limitations:
    The non-dev variants do NOT include a shell or apk package manager. You CANNOT run
    `apk add` in a distroless image. If the Dockerfile needs to install packages, you have
    two options:

    1. Use a multi-stage build pattern:
       - Build stage: Use the -dev variant to install packages and build artifacts
       - Runtime stage: Use the distroless variant and COPY artifacts from build stage

       Example:
           FROM cgr.dev/{org}/python:latest-dev AS builder
           USER root
           RUN apk add --no-cache build-base
           RUN pip install --user mypackage
           USER nonroot

           FROM cgr.dev/{org}/python:latest
           COPY --from=builder /home/nonroot/.local /home/nonroot/.local

    2. Use the -dev variant for the final image (if shell/apk is truly needed at runtime)

    The multi-stage pattern is preferred for production as it results in smaller,
    more secure images without shell or package manager attack surface.

    NOTE: You must call lookup_chainguard_image first to select an organization
    before using this tool.
    """
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

    best_tag, score = _find_best_tag(original_tag, tag_names, prefer_dev=require_dev)

    # Sort tags by relevance for display
    sorted_tags = _get_sorted_tags(original_tag, tag_names, require_dev)

    if best_tag is None or score < 0.3:
        return TagLookupResult(
            found=False,
            chainguard_image=chainguard_image,
            original_image=original_image,
            original_tag=original_tag,
            available_tags=sorted_tags,
            using_dev_variant=require_dev,
            message=f"No suitable tag match found for '{original_tag}'. "
            f"Available tags: {', '.join(sorted_tags[:10])}{'...' if len(sorted_tags) > 10 else ''}",
        )

    messages = []
    if score < 1.0:
        messages.append(f"Matched '{original_tag}' to '{best_tag}' (confidence: {score:.0%})")

    is_dev_tag = "-dev" in best_tag
    if require_dev and not is_dev_tag:
        messages.append(
            f"Note: -dev variant was requested but '{best_tag}' was the best version match. "
            "You may want to append '-dev' to this tag if shell/package manager access is needed."
        )

    return TagLookupResult(
        found=True,
        chainguard_image=chainguard_image,
        original_image=original_image,
        original_tag=original_tag,
        matched_tag=best_tag,
        full_image_ref=f"cgr.dev/{org}/{chainguard_image}:{best_tag}",
        available_tags=sorted_tags,
        using_dev_variant=is_dev_tag,
        message=" ".join(messages) if messages else None,
    )
