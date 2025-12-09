"""Tool for mapping package names from apt/yum to APK."""

import re
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import Field

from dfc_shazam.apk import WolfiAPKIndex
from dfc_shazam.models import PackageMatch, PackageMappingBatchResult, PackageMappingResult

# Load builtin mappings from dfc (vendored from https://github.com/chainguard-dev/dfc)
_MAPPINGS_FILE = Path(__file__).parent.parent / "builtin-mappings.yaml"
_BUILTIN_MAPPINGS: dict | None = None


def _load_builtin_mappings() -> dict:
    """Load and cache the builtin package mappings from dfc."""
    global _BUILTIN_MAPPINGS
    if _BUILTIN_MAPPINGS is None:
        with open(_MAPPINGS_FILE) as f:
            _BUILTIN_MAPPINGS = yaml.safe_load(f)
    return _BUILTIN_MAPPINGS


def _lookup_builtin_mapping(package: str, source_distro: str) -> list[str] | None:
    """Look up a package in the builtin mappings.

    Returns a list of APK package names if found, None otherwise.
    """
    mappings = _load_builtin_mappings()
    packages_section = mappings.get("packages", {})

    # Determine which distro mappings to check
    distros_to_check: list[str] = []
    if source_distro == "apt":
        distros_to_check = ["debian"]
    elif source_distro in ("yum", "dnf"):
        distros_to_check = ["fedora"]
    else:  # auto - check all
        distros_to_check = ["debian", "fedora"]

    for distro in distros_to_check:
        distro_mappings = packages_section.get(distro, {})
        if package in distro_mappings:
            result = distro_mappings[package]
            # Handle empty list (package should be dropped) vs list of replacements
            if result is None or result == []:
                return []  # Package has no equivalent / should be dropped
            return result

    return None  # Not found in builtin mappings


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
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    return previous_row[-1]


def _similarity_score(query: str, candidate: str) -> float:
    """Calculate similarity score between query and candidate (0.0 to 1.0)."""
    if query == candidate:
        return 1.0

    # Normalize common suffixes: -dev (apt) vs -devel (yum) both map to -dev
    query_normalized = query.replace("-devel", "-dev")
    candidate_normalized = candidate.replace("-devel", "-dev")

    if query_normalized == candidate_normalized:
        return 0.99

    # Check if removing common prefixes helps (lib prefix is often optional)
    query_no_lib = query_normalized.removeprefix("lib")
    candidate_no_lib = candidate_normalized.removeprefix("lib")

    if query_no_lib == candidate_no_lib:
        return 0.95

    # Use Levenshtein distance normalized by max length
    max_len = max(len(query_normalized), len(candidate_normalized))
    if max_len == 0:
        return 0.0

    distance = _levenshtein_distance(query_normalized, candidate_normalized)
    return 1.0 - (distance / max_len)


def _normalize_package_name(package: str, source_distro: str) -> str:
    """Normalize package name for matching.

    Applies common transformations based on source distro conventions.
    """
    name = package.lower().strip()

    # YUM/DNF uses -devel, APK uses -dev
    if source_distro in ("yum", "dnf", "auto"):
        name = name.replace("-devel", "-dev")

    return name


def _get_candidates(normalized: str, index: WolfiAPKIndex) -> list[tuple[str, str]]:
    """Get candidate packages for matching using cheap operations first.

    Returns list of (name, description) tuples for packages worth scoring.
    Uses a tiered approach to avoid expensive Levenshtein on all packages.
    """
    candidates: list[tuple[str, str]] = []

    # Tier 1: Exact match (O(1) lookup)
    exact = index.get_package(normalized)
    if exact:
        return [(exact.name, exact.description)]

    # Tier 2: Try common transformations
    # Remove version suffixes like "62" from "libjpeg62-turbo-dev"
    base_name = re.sub(r"\d+(-|$)", r"\1", normalized).rstrip("-")
    if base_name != normalized:
        exact = index.get_package(base_name)
        if exact:
            candidates.append((exact.name, exact.description))

    # Try without "lib" prefix
    if normalized.startswith("lib"):
        no_lib = normalized[3:]
        exact = index.get_package(no_lib)
        if exact:
            candidates.append((exact.name, exact.description))

    # Try with "lib" prefix if missing
    if not normalized.startswith("lib"):
        with_lib = "lib" + normalized
        exact = index.get_package(with_lib)
        if exact:
            candidates.append((exact.name, exact.description))

    # Tier 3: Prefix/substring matching (cheaper than Levenshtein)
    # Only check packages that share a common prefix or contain the query
    normalized_no_dev = normalized.removesuffix("-dev")

    for pkg in index.packages:
        name = pkg.name
        name_no_dev = name.removesuffix("-dev")

        # Skip if already found
        if any(c[0] == name for c in candidates):
            continue

        # Check for prefix matches (both directions)
        if name.startswith(normalized[:4]) or normalized.startswith(name[:4]):
            candidates.append((name, pkg.description))
        # Check for substring of core name (without -dev suffix)
        elif normalized_no_dev in name_no_dev or name_no_dev in normalized_no_dev:
            candidates.append((name, pkg.description))

        # Limit candidates to avoid excessive Levenshtein calculations
        if len(candidates) >= 100:
            break

    return candidates


def _map_single_package(
    package: str,
    source_distro: str,
    index: WolfiAPKIndex,
) -> PackageMappingResult:
    """Map a single package name to its APK equivalent.

    Internal function used by map_package for batch processing.
    First checks builtin mappings from dfc, then falls back to fuzzy search.
    """
    # First, check builtin mappings from dfc
    builtin_result = _lookup_builtin_mapping(package, source_distro)
    if builtin_result is not None:
        if not builtin_result:  # Empty list - package should be dropped
            return PackageMappingResult(
                source_package=package,
                source_distro=source_distro,
                matches=[],
                message=f"Package '{package}' has no APK equivalent (can be safely removed).",
            )
        # Found in builtin mappings - return all mapped packages
        matches = [
            PackageMatch(
                apk_package=apk_pkg,
                matched_name=apk_pkg,
                score=1.0,
                description=f"Builtin mapping from {package}",
            )
            for apk_pkg in builtin_result
        ]
        apk_list = " ".join(builtin_result)
        return PackageMappingResult(
            source_package=package,
            source_distro=source_distro,
            matches=matches,
            best_match=builtin_result[0],
            message=f"Builtin mapping: {package} → {apk_list}",
        )

    # Fall back to fuzzy search against APK index
    normalized = _normalize_package_name(package, source_distro)

    # Get candidate packages using cheap operations first
    candidates = _get_candidates(normalized, index)

    # Score only the candidate packages (much faster than scoring all)
    scored_matches: list[tuple[float, str, str]] = []  # (score, name, description)

    for name, description in candidates:
        score = _similarity_score(normalized, name)

        # Boost score if description contains the query
        if normalized in description.lower():
            score = min(1.0, score + 0.1)

        if score >= 0.5:  # Threshold for fuzzy matches
            scored_matches.append((score, name, description))

    # Sort by score descending
    scored_matches.sort(key=lambda x: x[0], reverse=True)

    # Deduplicate by package name (keep highest score)
    seen: set[str] = set()
    matches: list[PackageMatch] = []
    for score, name, description in scored_matches:
        if name not in seen:
            seen.add(name)
            matches.append(
                PackageMatch(
                    apk_package=name,
                    matched_name=name,
                    score=score,
                    description=description,
                )
            )
        if len(matches) >= 5:
            break

    if not matches:
        return PackageMappingResult(
            source_package=package,
            source_distro=source_distro,
            matches=[],
            message=f"No matching packages found for '{package}' in Wolfi APK index.",
        )

    best = matches[0]

    if best.score == 1.0:
        message = f"Exact match found: {package} → {best.apk_package}"
    elif best.score >= 0.9:
        message = f"Close match found: {package} → {best.apk_package} (score: {best.score:.0%})"
    else:
        message = f"Best fuzzy match: {package} → {best.apk_package} (score: {best.score:.0%})"
        if len(matches) > 1:
            others = ", ".join(m.apk_package for m in matches[1:])
            message += f". Other candidates: {others}"

    return PackageMappingResult(
        source_package=package,
        source_distro=source_distro,
        matches=matches,
        best_match=best.apk_package,
        message=message,
    )


async def find_equivalent_apk_packages(
    packages: Annotated[
        list[str],
        Field(description="List of source package names (e.g., ['libssl-dev', 'build-essential', 'curl'])"),
    ],
    source_distro: Annotated[
        Literal["apt", "yum", "dnf", "auto"],
        Field(
            description="Source distribution type: 'apt' (Debian/Ubuntu), 'yum'/'dnf' (RHEL/Fedora), or 'auto' to try both"
        ),
    ] = "auto",
) -> PackageMappingBatchResult:
    """Map package names from apt/yum to their APK (Wolfi) equivalents.

    Uses fuzzy matching against both the Wolfi APK package index and
    Chainguard extras repository to find the best matching package(s).
    Returns scored matches ordered by relevance.

    Accepts a list of packages to map in a single call for efficiency.
    """
    try:
        index = await WolfiAPKIndex.load(arch="x86_64", include_extras=True)
    except Exception as e:
        return PackageMappingBatchResult(
            source_distro=source_distro,
            results=[
                PackageMappingResult(
                    source_package=pkg,
                    source_distro=source_distro,
                    matches=[],
                    message=f"Failed to load APK index: {e}",
                )
                for pkg in packages
            ],
            summary=f"Failed to load APK index: {e}",
        )

    # Map each package
    results = [_map_single_package(pkg, source_distro, index) for pkg in packages]

    # Build summary
    mappings: list[str] = []
    unmapped: list[str] = []

    for result in results:
        if result.best_match:
            if result.best_match != result.source_package:
                mappings.append(f"{result.source_package} → {result.best_match}")
            else:
                mappings.append(result.source_package)
        else:
            unmapped.append(result.source_package)

    summary_parts = []
    if mappings:
        apk_packages = [r.best_match for r in results if r.best_match]
        summary_parts.append(f"APK packages: {' '.join(apk_packages)}")
    if unmapped:
        summary_parts.append(f"No matches found for: {', '.join(unmapped)}")

    return PackageMappingBatchResult(
        source_distro=source_distro,
        results=results,
        summary="\n".join(summary_parts) if summary_parts else "No packages processed",
    )
