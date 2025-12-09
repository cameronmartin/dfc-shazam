"""Wrapper for chainctl CLI commands."""

import asyncio
import json
import shutil
from dataclasses import dataclass

from dfc_shazam.config import settings


class ChainctlError(Exception):
    """Error from chainctl command."""

    pass


class ChainctlNotFoundError(ChainctlError):
    """chainctl is not installed."""

    pass


class ChainctlAuthError(ChainctlError):
    """chainctl authentication error."""

    pass


@dataclass
class AuthStatus:
    """Authentication status from chainctl."""

    valid: bool
    email: str | None = None
    organizations: list[str] | None = None  # List of org names from capabilities


@dataclass
class ImageInfo:
    """Information about a Chainguard image."""

    name: str
    repo: str


@dataclass
class TagInfo:
    """Information about an image tag."""

    tag: str
    digest: str | None = None


@dataclass
class ResolvedTag:
    """Result of resolving a tag."""

    digest: str
    exists: bool = True


class ChainctlClient:
    """Wrapper for chainctl CLI commands."""

    def __init__(self) -> None:
        self._chainctl_path: str | None = None

    def _get_chainctl_path(self) -> str:
        """Get the path to chainctl, raising if not found."""
        if self._chainctl_path is None:
            path = shutil.which("chainctl")
            if path is None:
                raise ChainctlNotFoundError(
                    "chainctl is not installed. Install it from "
                    "https://edu.chainguard.dev/chainguard/chainctl-usage/getting-started-with-chainctl/"
                )
            self._chainctl_path = path
        return self._chainctl_path

    async def _run_command(
        self, args: list[str], timeout: float | None = None
    ) -> dict | list:
        """Run a chainctl command and return JSON output."""
        chainctl = self._get_chainctl_path()
        cmd = [chainctl, *args, "--output", "json"]

        if timeout is None:
            timeout = settings.chainctl_timeout_seconds

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            raise ChainctlError(f"chainctl command timed out: {' '.join(cmd)}")
        except Exception as e:
            raise ChainctlError(f"Failed to run chainctl: {e}")

        if proc.returncode != 0:
            stderr_text = stderr.decode().strip()
            if "not authenticated" in stderr_text.lower() or "login" in stderr_text.lower():
                raise ChainctlAuthError(
                    "chainctl is not authenticated. Run 'chainctl auth login' first."
                )
            raise ChainctlError(f"chainctl command failed: {stderr_text}")

        try:
            return json.loads(stdout.decode())
        except json.JSONDecodeError as e:
            raise ChainctlError(f"Failed to parse chainctl output: {e}")

    async def get_auth_status(self) -> AuthStatus:
        """Get authentication status and available organizations.

        Returns:
            AuthStatus with validity, email, and list of organizations
        """
        result = await self._run_command(["auth", "status"])

        if not isinstance(result, dict):
            return AuthStatus(valid=False)

        valid = result.get("valid", False)
        email = result.get("email")

        # Extract organization names from capabilities
        capabilities = result.get("capabilities", {})
        organizations = list(capabilities.keys()) if capabilities else None

        return AuthStatus(valid=valid, email=email, organizations=organizations)

    async def list_images(
        self, repo: str | None = None, org: str | None = None, public: bool = False
    ) -> list[ImageInfo]:
        """List available Chainguard images.

        Args:
            repo: Optional repository name to filter by
            org: Organization name to list images from (uses --parent flag)
            public: If True, list public images (default False)

        Returns:
            List of ImageInfo objects
        """
        args = ["images", "list"]
        if org:
            args.extend(["--parent", org])
        elif public:
            args.append("--public")
        if repo:
            args.extend(["--repo", repo])

        # Use longer timeout for listing images (can be slow with large catalogs)
        result = await self._run_command(args, timeout=120.0)

        images = []
        if isinstance(result, list):
            for item in result:
                if isinstance(item, dict):
                    # Handle nested structure: item.repo.name
                    repo_data = item.get("repo", {})
                    if isinstance(repo_data, dict):
                        name = repo_data.get("name", "")
                    else:
                        name = item.get("name", "")
                    images.append(
                        ImageInfo(
                            name=name,
                            repo=name,
                        )
                    )
        return images

    async def list_tags(self, repo: str, org: str) -> list[TagInfo]:
        """List tags for a repository.

        Args:
            repo: Repository name (e.g., "python")
            org: Organization name (e.g., "chainguard-private")

        Returns:
            List of TagInfo objects
        """
        args = ["images", "tags", "list", "--repo", repo, "--parent", org]

        result = await self._run_command(args)

        tags = []
        if isinstance(result, list):
            for item in result:
                if isinstance(item, dict):
                    tags.append(
                        TagInfo(
                            tag=item.get("name", item.get("tag", "")),
                            digest=item.get("digest"),
                        )
                    )
                elif isinstance(item, str):
                    tags.append(TagInfo(tag=item))
        return tags

    async def resolve_tag(self, image_ref: str) -> ResolvedTag:
        """Resolve an image reference to its digest.

        Args:
            image_ref: Full image reference (e.g., "cgr.dev/{org}/python:3.12")

        Returns:
            ResolvedTag with digest information

        Raises:
            ChainctlError: If the tag cannot be resolved
        """
        args = ["images", "tags", "resolve", image_ref]

        try:
            result = await self._run_command(args)
        except ChainctlError as e:
            if "not found" in str(e).lower():
                return ResolvedTag(digest="", exists=False)
            raise

        digest = ""
        if isinstance(result, list) and len(result) > 0:
            # chainctl returns a list of tag info objects
            first = result[0]
            if isinstance(first, dict):
                digest = first.get("digest", first.get("Digest", ""))
        elif isinstance(result, dict):
            digest = result.get("digest", result.get("Digest", ""))
        elif isinstance(result, str):
            digest = result

        return ResolvedTag(digest=digest, exists=bool(digest))

    async def get_history(self, image_ref: str) -> list[dict]:
        """Get the history of an image tag.

        Args:
            image_ref: Full image reference (e.g., "cgr.dev/{org}/python:latest")

        Returns:
            List of history entries
        """
        args = ["images", "history", image_ref]

        result = await self._run_command(args)

        if isinstance(result, list):
            return result
        return []
