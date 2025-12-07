"""APK index parser for Wolfi packages."""

import io
import tarfile
import time
from dataclasses import dataclass, field

import httpx

from dfc_shazam.config import settings


@dataclass
class APKPackage:
    """Represents an APK package from the index."""

    name: str
    version: str
    description: str = ""
    architecture: str = ""
    size: int = 0
    installed_size: int = 0
    dependencies: list[str] = field(default_factory=list)
    provides: list[str] = field(default_factory=list)
    origin: str | None = None
    maintainer: str | None = None


class WolfiAPKIndex:
    """Parser and searcher for Wolfi APK package index."""

    BASE_URL = "https://packages.wolfi.dev/os"

    # Class-level cache
    _cache: dict[str, tuple[float, "WolfiAPKIndex"]] = {}

    def __init__(self, packages: list[APKPackage], arch: str) -> None:
        self.packages = packages
        self.arch = arch
        self._name_index: dict[str, APKPackage] = {p.name: p for p in packages}
        # Index for provides entries (cmd:, so:, etc.)
        self._provides_index: dict[str, list[APKPackage]] = {}
        for pkg in packages:
            for provides in pkg.provides:
                if provides not in self._provides_index:
                    self._provides_index[provides] = []
                self._provides_index[provides].append(pkg)

    @classmethod
    async def load(cls, arch: str = "x86_64") -> "WolfiAPKIndex":
        """Download and parse the APK index.

        Args:
            arch: Architecture (x86_64 or aarch64)

        Returns:
            WolfiAPKIndex instance with parsed packages
        """
        # Check cache
        cache_key = arch
        if cache_key in cls._cache:
            cached_time, cached_index = cls._cache[cache_key]
            if time.time() - cached_time < settings.apk_cache_ttl_seconds:
                return cached_index

        url = f"{cls.BASE_URL}/{arch}/APKINDEX.tar.gz"

        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=30.0)
            response.raise_for_status()

        packages = cls._parse_index(response.content, arch)
        index = cls(packages, arch)

        # Cache the result
        cls._cache[cache_key] = (time.time(), index)

        return index

    @classmethod
    def _parse_index(cls, data: bytes, arch: str) -> list[APKPackage]:
        """Parse APKINDEX.tar.gz content."""
        packages = []

        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            apkindex = tar.extractfile("APKINDEX")
            if apkindex is None:
                raise ValueError("APKINDEX file not found in archive")

            content = apkindex.read().decode("utf-8")

        # Split by blank lines to get individual package records
        records = content.split("\n\n")

        for record in records:
            if not record.strip():
                continue

            pkg = cls._parse_record(record, arch)
            if pkg:
                packages.append(pkg)

        return packages

    @classmethod
    def _parse_record(cls, record: str, arch: str) -> APKPackage | None:
        """Parse a single package record.

        APK index format uses single-letter prefixes:
        P: Package name
        V: Version
        T: Description
        A: Architecture
        S: Size
        I: Installed size
        D: Dependencies (space-separated)
        p: Provides (space-separated)
        o: Origin
        m: Maintainer
        """
        fields: dict[str, str] = {}
        for line in record.strip().split("\n"):
            if ":" in line and len(line) >= 2:
                key = line[0]
                value = line[2:] if len(line) > 2 else ""
                fields[key] = value

        if "P" not in fields:
            return None

        return APKPackage(
            name=fields.get("P", ""),
            version=fields.get("V", ""),
            description=fields.get("T", ""),
            architecture=fields.get("A", arch),
            size=int(fields.get("S", 0) or 0),
            installed_size=int(fields.get("I", 0) or 0),
            dependencies=fields.get("D", "").split() if fields.get("D") else [],
            provides=fields.get("p", "").split() if fields.get("p") else [],
            origin=fields.get("o"),
            maintainer=fields.get("m"),
        )

    def search(self, query: str, limit: int = 50) -> list[APKPackage]:
        """Search packages by name or description.

        Args:
            query: Search term
            limit: Maximum number of results

        Returns:
            List of matching packages, ordered by relevance
        """
        query_lower = query.lower()
        exact_matches = []
        prefix_matches = []
        contains_matches = []
        description_matches = []

        for pkg in self.packages:
            name_lower = pkg.name.lower()

            # Exact name match - highest priority
            if name_lower == query_lower:
                exact_matches.append(pkg)
            # Name prefix match
            elif name_lower.startswith(query_lower):
                prefix_matches.append(pkg)
            # Name contains query
            elif query_lower in name_lower:
                contains_matches.append(pkg)
            # Description contains query
            elif query_lower in pkg.description.lower():
                description_matches.append(pkg)

        # Combine results in priority order
        results = exact_matches + prefix_matches + contains_matches + description_matches
        return results[:limit]

    def search_provides(
        self, query: str, prefix: str | None = None, limit: int = 50
    ) -> list[APKPackage]:
        """Search packages by what they provide (commands, shared libraries, etc.).

        Args:
            query: Search term (e.g., "useradd", "libxml2.so")
            prefix: Optional prefix filter ("cmd", "so", or None for all)
            limit: Maximum number of results

        Returns:
            List of packages that provide matching entries

        Examples:
            search_provides("useradd", prefix="cmd")  # Find packages providing cmd:useradd
            search_provides("libxml2.so", prefix="so")  # Find packages providing so:libxml2.so*
        """
        query_lower = query.lower()

        # Build the full search pattern with prefix if provided
        if prefix:
            search_prefix = f"{prefix}:"
        else:
            search_prefix = ""

        exact_matches: list[APKPackage] = []
        prefix_matches: list[APKPackage] = []
        contains_matches: list[APKPackage] = []
        seen: set[str] = set()  # Track package names to avoid duplicates

        # First try exact match in the provides index
        exact_key = f"{search_prefix}{query}" if search_prefix else query
        if exact_key in self._provides_index:
            for pkg in self._provides_index[exact_key]:
                if pkg.name not in seen:
                    seen.add(pkg.name)
                    exact_matches.append(pkg)

        # Then search through all provides entries
        for provides_entry, pkgs in self._provides_index.items():
            # Skip if prefix doesn't match
            if search_prefix and not provides_entry.startswith(search_prefix):
                continue

            # Get the value part (after prefix if present)
            if ":" in provides_entry:
                entry_value = provides_entry.split(":", 1)[1].lower()
            else:
                entry_value = provides_entry.lower()

            # Skip exact matches (already handled)
            if entry_value == query_lower:
                continue

            for pkg in pkgs:
                if pkg.name in seen:
                    continue

                # Prefix match on the value
                if entry_value.startswith(query_lower):
                    seen.add(pkg.name)
                    prefix_matches.append(pkg)
                # Contains match
                elif query_lower in entry_value:
                    seen.add(pkg.name)
                    contains_matches.append(pkg)

        results = exact_matches + prefix_matches + contains_matches
        return results[:limit]

    def get_package(self, name: str) -> APKPackage | None:
        """Get a package by exact name.

        Args:
            name: Exact package name

        Returns:
            APKPackage if found, None otherwise
        """
        return self._name_index.get(name)

    def list_all(self) -> list[str]:
        """List all package names."""
        return list(self._name_index.keys())
