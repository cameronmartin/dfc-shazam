"""Docker Hub to Chainguard image mappings."""

import csv
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

MAPPINGS_DIR = Path(__file__).parent

# Static registry prefixes to strip (order matters - more specific first)
STATIC_REGISTRY_PREFIXES = (
    "docker.io/library/",
    "docker.io/",
    "index.docker.io/library/",
    "index.docker.io/",
    "library/",
    "registry.access.redhat.com/",
    "registry.redhat.io/",
    "quay.io/",
    "gcr.io/",
    "ghcr.io/",
    "public.ecr.aws/",
    "mcr.microsoft.com/",
    "cgr.dev/chainguard/",
    "cgr.dev/",
)

# Patterns for dynamic registry prefixes (compiled for performance)
# These match registries where the hostname varies (e.g., ECR, GCR with project, ACR)
DYNAMIC_REGISTRY_PATTERNS = (
    # AWS ECR: 123456789012.dkr.ecr.us-east-1.amazonaws.com/image
    re.compile(r"^\d+\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/"),
    # GCR with project: gcr.io/project-name/image or us.gcr.io/project/image
    re.compile(r"^(us\.|eu\.|asia\.)?gcr\.io/[^/]+/"),
    # Google Artifact Registry: us-docker.pkg.dev/project/repo/image
    re.compile(r"^[a-z0-9-]+-docker\.pkg\.dev/[^/]+/[^/]+/"),
    # Azure ACR: myregistry.azurecr.io/image
    re.compile(r"^[a-z0-9]+\.azurecr\.io/"),
    # Harbor or generic registry with port: registry.example.com:5000/image
    re.compile(r"^[a-z0-9.-]+:\d+/"),
    # Generic internal registry with path: registry.example.com/org/image
    re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}/[^/]+/"),
)


@dataclass
class ImageMatch:
    """A matched Chainguard image with similarity score."""

    chainguard_image: str
    matched_alias: str
    score: float  # 1.0 = exact match, lower = fuzzy match


@lru_cache
def _load_generic_base_images() -> set[str]:
    """Load generic base images from CSV."""
    csv_path = MAPPINGS_DIR / "generic_base_images.csv"
    images: set[str] = set()
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            images.add(row["image"])
    return images


@lru_cache
def _load_image_aliases() -> dict[str, list[str]]:
    """Load image aliases from CSV, returning alias -> list of chainguard_images."""
    csv_path = MAPPINGS_DIR / "image_aliases.csv"
    aliases: dict[str, list[str]] = {}
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            alias = row["alias"]
            cg_image = row["chainguard_image"]
            if alias not in aliases:
                aliases[alias] = []
            aliases[alias].append(cg_image)
    return aliases


def _strip_registry_prefix(image: str) -> str:
    """Strip common registry prefixes from an image name.

    Handles both well-known registries (docker.io, ghcr.io, quay.io) and
    dynamic registries (ECR, GCR with project, ACR, Harbor, etc.).
    """
    image_lower = image.lower()

    # Try static prefixes first (faster)
    for prefix in STATIC_REGISTRY_PREFIXES:
        if image_lower.startswith(prefix):
            return image[len(prefix) :]

    # Try dynamic patterns for internal/cloud registries
    for pattern in DYNAMIC_REGISTRY_PATTERNS:
        match = pattern.match(image_lower)
        if match:
            return image[match.end() :]

    return image


def _levenshtein_distance(s1: str, s2: str) -> int:
    """Calculate the Levenshtein distance between two strings."""
    if len(s1) < len(s2):
        return _levenshtein_distance(s2, s1)

    if len(s2) == 0:
        return len(s1)

    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            # j+1 instead of j since previous_row and current_row are one character longer
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    return previous_row[-1]


def _normalize_for_comparison(name: str) -> str:
    """Normalize image name for fuzzy comparison.

    Removes hyphens and underscores to handle variations like:
    - amazoncorretto vs amazon-corretto
    - amazon-corretto-jdk vs amazoncorretto
    """
    return name.replace("-", "").replace("_", "")


def _similarity_score(query: str, candidate: str) -> float:
    """Calculate similarity score between query and candidate (0.0 to 1.0)."""
    if query == candidate:
        return 1.0

    # Also check if the last component matches (e.g., "bitnami/python" matches "python")
    query_base = query.split("/")[-1]
    candidate_base = candidate.split("/")[-1]

    if query_base == candidate_base:
        return 0.95  # Very high score for base name match

    # Normalize for comparison (remove hyphens/underscores)
    query_normalized = _normalize_for_comparison(query_base)
    candidate_normalized = _normalize_for_comparison(candidate_base)

    # Check if normalized versions match exactly
    if query_normalized == candidate_normalized:
        return 0.98  # Very high score for normalized match

    # Check if one contains the other after normalization
    if query_normalized in candidate_normalized or candidate_normalized in query_normalized:
        # Score based on length ratio
        shorter = min(len(query_normalized), len(candidate_normalized))
        longer = max(len(query_normalized), len(candidate_normalized))
        return 0.8 + (0.15 * shorter / longer)

    # Use Levenshtein distance on normalized names
    max_len = max(len(query_normalized), len(candidate_normalized))
    if max_len == 0:
        return 0.0

    distance = _levenshtein_distance(query_normalized, candidate_normalized)
    return 1.0 - (distance / max_len)


def _normalize_image_name(source_image: str) -> str:
    """Normalize an image reference to a canonical name for lookup.

    Strips tags and registry prefixes to get the core image name.

    Args:
        source_image: Full image reference (e.g., "registry.access.redhat.com/ubi9/ubi-minimal:latest")

    Returns:
        Normalized image name (e.g., "ubi9/ubi-minimal")
    """
    image_name = source_image.lower()

    # Remove tag if present (but be careful with ports like :5000)
    # Tags come after the last : that follows a /
    if "/" in image_name:
        # Find the last slash, then look for : after it
        last_slash = image_name.rfind("/")
        colon_after_slash = image_name.find(":", last_slash)
        if colon_after_slash > last_slash:
            image_name = image_name[:colon_after_slash]
    elif ":" in image_name:
        # Simple case: no slash, so : must be a tag
        image_name = image_name.split(":")[0]

    # Strip common registry prefixes
    image_name = _strip_registry_prefix(image_name)

    return image_name


def is_generic_base_image(source_image: str) -> bool:
    """Check if the source image is a generic base image.

    Generic base images (like Ubuntu, Alpine, UBI) are typically used as a
    starting point where users install their actual workload. For these,
    it's better to use a workload-specific Chainguard image instead.

    Args:
        source_image: Source image reference

    Returns:
        True if this is a generic base image that should prompt workload analysis
    """
    image_name = _normalize_image_name(source_image)

    generic_base_images = _load_generic_base_images()

    # Check direct match
    if image_name in generic_base_images:
        return True

    # Check base name for library images
    if "/" in image_name:
        base_name = image_name.split("/")[-1]
        if base_name in generic_base_images:
            return True

    return False


def lookup_chainguard_image(
    source_image: str,
    fuzzy_threshold: float = 0.6,
    max_results: int = 5,
) -> list[ImageMatch]:
    """Look up Chainguard equivalents for a source image.

    Performs exact matching first, then fuzzy matching if no exact match found.

    Args:
        source_image: Source image name (e.g., "python", "node:18", "ubuntu:22.04",
                      "registry.access.redhat.com/ubi9/ubi-minimal")
        fuzzy_threshold: Minimum similarity score for fuzzy matches (0.0 to 1.0)
        max_results: Maximum number of fuzzy matches to return

    Returns:
        List of ImageMatch objects, sorted by score (highest first).
        Empty list if no matches found.
    """
    image_name = _normalize_image_name(source_image)
    image_aliases = _load_image_aliases()
    matches: list[ImageMatch] = []

    # Try exact match first
    if image_name in image_aliases:
        for cg_image in image_aliases[image_name]:
            matches.append(ImageMatch(cg_image, image_name, 1.0))
        return matches

    # Try without leading path component (e.g., "bitnami/python" -> "python")
    if "/" in image_name:
        base_name = image_name.split("/")[-1]
        if base_name in image_aliases:
            for cg_image in image_aliases[base_name]:
                matches.append(ImageMatch(cg_image, base_name, 0.95))
            return matches

    # Fuzzy search across all aliases
    scored_matches: list[ImageMatch] = []
    seen_images: set[str] = set()

    for alias, cg_images in image_aliases.items():
        score = _similarity_score(image_name, alias)
        if score >= fuzzy_threshold:
            for cg_image in cg_images:
                # Deduplicate by chainguard image
                if cg_image not in seen_images:
                    seen_images.add(cg_image)
                    scored_matches.append(ImageMatch(cg_image, alias, score))

    # Sort by score descending, take top results
    scored_matches.sort(key=lambda m: m.score, reverse=True)
    return scored_matches[:max_results]


