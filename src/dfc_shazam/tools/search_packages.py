"""Tool for searching APK packages."""

from typing import Annotated, Literal

from pydantic import Field

from dfc_shazam.apk import WolfiAPKIndex
from dfc_shazam.models import APKPackageInfo, APKSearchResult


async def search_apk_packages(
    query: Annotated[str, Field(description="Package name or search term")],
    arch: Annotated[
        str, Field(description="Architecture (x86_64 or aarch64)")
    ] = "x86_64",
    limit: Annotated[
        int, Field(description="Maximum number of results", ge=1, le=100)
    ] = 20,
    search_type: Annotated[
        Literal["name", "cmd", "so"],
        Field(
            description="Search type: 'name' for package names (default), "
            "'cmd' for commands (e.g., 'useradd'), 'so' for shared libraries (e.g., 'libxml2.so')"
        ),
    ] = "name",
) -> APKSearchResult:
    """Search the Wolfi APK package index for packages.

    Searches by package names/descriptions, commands, or shared libraries.
    Results ordered by relevance: exact matches first, then prefix, then substring.

    Examples:
        search_apk_packages("python")  # Search package names
        search_apk_packages("useradd", search_type="cmd")  # Find package providing useradd command
        search_apk_packages("libxml2.so", search_type="so")  # Find package providing libxml2.so
    """
    if arch not in ("x86_64", "aarch64"):
        return APKSearchResult(
            query=query,
            arch=arch,
            packages=[],
            total_count=0,
            warning=f"Invalid architecture '{arch}'. Use 'x86_64' or 'aarch64'.",
        )

    try:
        index = await WolfiAPKIndex.load(arch=arch)
    except Exception as e:
        return APKSearchResult(
            query=query,
            arch=arch,
            packages=[],
            total_count=0,
            warning=f"Failed to load APK index: {e}",
        )

    # Search the index based on search type
    if search_type == "name":
        results = index.search(query, limit=limit)
    else:
        # search_type is "cmd" or "so"
        results = index.search_provides(query, prefix=search_type, limit=limit)

    # Convert to response models
    packages = [
        APKPackageInfo(
            name=pkg.name,
            version=pkg.version,
            description=pkg.description,
            architecture=pkg.architecture,
            size=pkg.size,
            installed_size=pkg.installed_size,
            dependencies=pkg.dependencies,
            provides=pkg.provides,
            origin=pkg.origin,
        )
        for pkg in results
    ]

    return APKSearchResult(
        query=query,
        arch=arch,
        packages=packages,
        total_count=len(packages),
    )
