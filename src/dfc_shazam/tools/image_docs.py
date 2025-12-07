"""Tool for fetching Chainguard image overview text from images.chainguard.dev."""

import asyncio
import re
import shutil
from typing import Annotated

import httpx
from pydantic import Field

from dfc_shazam.config import OrgSession
from dfc_shazam.mappings.images import lookup_chainguard_image
from dfc_shazam.models import ContainerUserInfo, ImageOverviewResult, LinkedDocContent

# Version for user agent - should match pyproject.toml
VERSION = "0.1.0"
USER_AGENT = f"dfc-shazam/{VERSION}"

# Base URL for Chainguard documentation
CHAINGUARD_DOCS_BASE = "https://edu.chainguard.dev"
CHAINGUARD_IMAGES_BASE = "https://images.chainguard.dev"

# Timeout for Docker operations (pull + run)
DOCKER_TIMEOUT_SECONDS = 120.0

# Static conversion tips returned with every get_image_overview call
CONVERSION_TIPS = [
    "Review any `curl | sh` or `wget` commands that download and install software - "
    "check if there's a Wolfi APK package available instead using search_apk_packages. "
    "Installing via apk is more secure and maintainable.",
    "Replace `apt-get`, `yum`, or `dnf` package installs with `apk add --no-cache`. "
    "Use map_package to find APK equivalents for packages.",
    "Chainguard images run as non-root by default. Add `USER root` before `apk add`, "
    "then switch back with `USER nonroot` (or the image-specific user).",
    "Every COPY/ADD command MUST include `--chown=nonroot:nonroot` (or appropriate user) "
    "to ensure files are accessible to the non-root runtime user.",
    "Paths like `/root` are not accessible to non-root users. Use the user's home "
    "directory (typically `/home/nonroot`) for application files.",
    "For distroless (non-dev) images: there is NO shell or package manager. Use multi-stage "
    "builds to install dependencies in a -dev stage, then COPY artifacts to the final image.",
]


def _is_docker_available() -> bool:
    """Check if Docker CLI is available on the system."""
    return shutil.which("docker") is not None


async def _inspect_container_filesystem(image_ref: str) -> str | None:
    """Pull image and inspect directory structure with ownership.

    Runs the -dev variant of the image and uses find to list directories
    with their permissions and ownership.

    Args:
        image_ref: Full image reference (e.g., cgr.dev/org/python:latest-dev)

    Returns:
        Directory tree string or None if Docker unavailable/fails.
    """
    if not _is_docker_available():
        return None

    # Command to list directories with permissions and ownership
    # Uses find + ls because busybox find doesn't support -printf
    # -type d: directories only
    # -maxdepth 2: limit depth to keep output manageable
    find_cmd = (
        "find / -type d -maxdepth 2 2>/dev/null | head -100 | "
        "while read dir; do ls -ld \"$dir\" 2>/dev/null; done"
    )

    cmd = [
        "docker", "run", "--rm",
        "--entrypoint", "",
        image_ref,
        "sh", "-c", find_cmd,
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=DOCKER_TIMEOUT_SECONDS,
        )

        if proc.returncode != 0:
            # Silently skip on failure
            return None

        output = stdout.decode("utf-8", errors="replace").strip()
        if not output:
            return None

        return output

    except asyncio.TimeoutError:
        # Silently skip on timeout
        return None
    except Exception:
        # Silently skip on any other error
        return None


async def _inspect_container_users(image_ref: str) -> list[ContainerUserInfo]:
    """Extract user information from container's /etc/passwd.

    Args:
        image_ref: Full image reference (e.g., cgr.dev/org/python:latest-dev)

    Returns:
        List of ContainerUserInfo objects, or empty list if unavailable.
    """
    if not _is_docker_available():
        return []

    cmd = [
        "docker", "run", "--rm",
        "--entrypoint", "",
        image_ref,
        "cat", "/etc/passwd",
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=DOCKER_TIMEOUT_SECONDS,
        )

        if proc.returncode != 0:
            return []

        output = stdout.decode("utf-8", errors="replace").strip()
        if not output:
            return []

        users = []
        for line in output.split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            # /etc/passwd format: username:x:uid:gid:comment:home:shell
            parts = line.split(":")
            if len(parts) >= 7:
                try:
                    users.append(
                        ContainerUserInfo(
                            username=parts[0],
                            uid=int(parts[2]),
                            gid=int(parts[3]),
                            home=parts[5],
                            shell=parts[6],
                        )
                    )
                except (ValueError, IndexError):
                    # Skip malformed lines
                    continue

        return users

    except asyncio.TimeoutError:
        return []
    except Exception:
        return []


def _generate_user_guidance(users: list[ContainerUserInfo]) -> str | None:
    """Generate actionable guidance based on available container users."""
    if not users:
        return None

    # Find the primary non-root user (typically 'nonroot' in Chainguard images)
    nonroot_user = next((u for u in users if u.username == "nonroot"), None)

    # Find any other non-root, non-system users that might be image-specific
    app_users = [u for u in users if u.uid >= 1000 and u.username != "nonroot" and u.username != "nobody"]

    # Determine recommended user - prefer app-specific user if present, otherwise nonroot
    if app_users:
        recommended_user = app_users[0]
        user_note = f"This image has an application-specific user `{recommended_user.username}` which may be more appropriate than nonroot."
    elif nonroot_user:
        recommended_user = nonroot_user
        user_note = None
    else:
        recommended_user = None
        user_note = None

    if nonroot_user or app_users:
        user = recommended_user or nonroot_user
        assert user is not None

        guidance = f"""⚠️ CRITICAL - Container User & File Ownership Configuration:

This Chainguard image runs as a non-root user by default.
Available users: {', '.join(f'`{u.username}` (uid={u.uid})' for u in ([nonroot_user] if nonroot_user else []) + app_users[:3])}

🚨 COPY/ADD COMMANDS MUST ALWAYS SPECIFY --chown:
- NEVER omit --chown from COPY/ADD commands
- Files without explicit ownership will be owned by root and inaccessible
- Think carefully about which user is appropriate for each file:
  - Application code/configs -> use the runtime user
  - Static assets -> use the runtime user
  - If unsure, default to `{user.username}:{user.username}`

📋 FILE OWNERSHIP CHECKLIST:
1. Review EVERY COPY/ADD command - each MUST have --chown
2. Consider which user should own each file (app-specific user vs nonroot)
3. Ensure WORKDIR and target directories are writable by the runtime user
4. Use home directory `{user.home}` for application files (NOT /root)

REQUIRED Dockerfile changes:
- Add `USER {user.username}` before the final CMD/ENTRYPOINT
- Example: `COPY --chown={user.username}:{user.username} ./app /app`
- If installing packages with apk, temporarily switch to root:
  ```
  USER root
  RUN apk add --no-cache <packages>
  USER {user.username}
  ```

COMMON PITFALLS:
- COPY without --chown creates root-owned files that are inaccessible
- npm/pip install to default locations may fail - use --prefix or install to {user.home}
- Log directories must be writable by the runtime user
- Config files must be readable by the runtime user"""

        if user_note:
            guidance += f"\n\nNOTE: {user_note}"

        return guidance

    # Generic fallback
    user_list = ", ".join(f"`{u.username}`" for u in users[:5])
    return f"""Available users in this image: {user_list}.

🚨 COPY/ADD COMMANDS MUST ALWAYS SPECIFY --chown:
- NEVER omit --chown from COPY/ADD commands
- Check which user the container runs as by default
- Ensure all COPY/ADD commands include appropriate --chown flags for that user"""


def _extract_doc_links(html: str, image_name: str) -> list[tuple[str, str]]:
    """Extract documentation links from the overview page.

    Looks for links to:
    - Getting started guides
    - Best practices
    - Migration guides
    - How-to guides

    Returns list of (url, title) tuples.
    """
    links: list[tuple[str, str]] = []
    seen_urls: set[str] = set()

    # Patterns for relevant documentation links
    # Look for anchor tags with href containing relevant paths
    link_pattern = re.compile(
        r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>([^<]+)</a>',
        re.IGNORECASE,
    )

    # Only include "getting started" guides - other content is less valuable
    useful_keywords = [
        "getting-started",
        "getting started",
    ]

    for match in link_pattern.finditer(html):
        url = match.group(1)
        title = match.group(2).strip()

        # Skip empty titles or very short ones
        if len(title) < 3:
            continue

        # Normalize URL
        if url.startswith("/"):
            # Determine base URL from context
            if "/chainguard/" in url or "/open-source/" in url:
                url = CHAINGUARD_DOCS_BASE + url
            else:
                url = CHAINGUARD_IMAGES_BASE + url

        # Skip non-http links, anchors, and already seen URLs
        if not url.startswith("http") or url in seen_urls:
            continue

        # Only follow links to Chainguard domains
        if not ("edu.chainguard.dev" in url or "images.chainguard.dev" in url):
            continue

        # Check if URL or title contains useful keywords
        url_lower = url.lower()
        title_lower = title.lower()

        is_useful = any(
            kw in url_lower or kw in title_lower for kw in useful_keywords
        )

        # Also include links specifically about this image
        if image_name.lower() in url_lower:
            is_useful = True

        # Include edu.chainguard.dev links about images
        if "edu.chainguard.dev" in url and "/chainguard/chainguard-images/" in url:
            is_useful = True

        if is_useful:
            seen_urls.add(url)
            links.append((url, title))

    return links[:5]  # Limit to 5 most relevant links


async def _fetch_doc_content(
    client: httpx.AsyncClient, url: str, title: str
) -> LinkedDocContent | None:
    """Fetch and extract content from a documentation URL."""
    try:
        response = await client.get(url)
        if response.status_code != 200:
            return None

        html = response.text
        content = _extract_doc_text(html)

        if not content or len(content) < 50:
            return None

        return LinkedDocContent(url=url, title=title, content=content)

    except (httpx.TimeoutException, httpx.RequestError):
        return None


def _extract_doc_text(html: str) -> str:
    """Extract main text content from a documentation page."""
    # Remove script and style tags
    html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<nav[^>]*>.*?</nav>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<header[^>]*>.*?</header>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<footer[^>]*>.*?</footer>", "", html, flags=re.DOTALL | re.IGNORECASE)

    # Try to find main content area
    # Look for article or main tags first
    main_match = re.search(
        r"<(?:article|main)[^>]*>(.*?)</(?:article|main)>",
        html,
        re.DOTALL | re.IGNORECASE,
    )
    if main_match:
        html = main_match.group(1)

    # Convert to text
    return _html_to_text(html)


def _normalize_image_name(image_ref: str) -> str:
    """Extract and normalize the base image name from various image reference formats.

    Handles:
    - Simple names: python, node, nginx
    - Docker Hub: python:3.12, library/python
    - cgr.dev: cgr.dev/{org}/python:latest
    - Other registries: registry.access.redhat.com/ubi9/ubi-minimal
    - gcr.io, ghcr.io, quay.io, etc.

    Returns the base image name (e.g., 'python', 'ubi-minimal').
    """
    image_ref = image_ref.lower().strip()

    # Remove tag and digest
    image_ref = image_ref.split("@")[0]  # Remove digest
    image_ref = image_ref.split(":")[0]  # Remove tag

    # Check if it looks like a registry URL (contains dots before first slash)
    if "/" in image_ref:
        parts = image_ref.split("/")
        first_part = parts[0]

        # If first part looks like a registry (has dots or is localhost)
        if "." in first_part or first_part == "localhost" or ":" in first_part:
            # It's a full registry URL, get the last part as image name
            image_name = parts[-1]
        elif first_part == "library":
            # Docker Hub official image: library/python
            image_name = parts[-1]
        else:
            # Could be user/image on Docker Hub or just path segments
            # Take the last part as the image name
            image_name = parts[-1]
    else:
        # Simple image name
        image_name = image_ref

    return image_name


async def get_image_overview(
    image_name: Annotated[
        str,
        Field(
            description="Chainguard image name (e.g., 'python', 'node', 'nginx')"
        ),
    ],
) -> ImageOverviewResult:
    """Get overview text for a Chainguard image from images.chainguard.dev.

    Fetches the overview page and extracts the main text content describing
    the image, its usage, and key information. Also follows links to best
    practices, getting started guides, and other relevant documentation,
    returning their content as well.

    Accepts various image reference formats:
    - Simple names: python, node, nginx
    - Full references: cgr.dev/{org}/python:latest
    - Other registries: registry.access.redhat.com/ubi9/ubi-minimal (will lookup Chainguard equivalent)
    """
    original_input = image_name
    image_name = _normalize_image_name(image_name)

    # Try to find Chainguard equivalent if this might be a non-Chainguard image
    matches = lookup_chainguard_image(image_name)
    if matches and matches[0].score >= 0.9:
        image_name = matches[0].chainguard_image

    overview_url = f"https://images.chainguard.dev/directory/image/{image_name}/overview"

    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(
        timeout=30.0, follow_redirects=True, headers=headers
    ) as client:
        try:
            response = await client.get(overview_url)

            if response.status_code == 404:
                return ImageOverviewResult(
                    found=False,
                    image_name=image_name,
                    message=f"Image '{image_name}' not found on images.chainguard.dev",
                )

            if response.status_code != 200:
                return ImageOverviewResult(
                    found=False,
                    image_name=image_name,
                    message=f"Failed to fetch overview: HTTP {response.status_code}",
                )

            html = response.text
            overview_text = _extract_overview_text(html)

            # Extract links to best practices and documentation
            doc_links = _extract_doc_links(html, image_name)

            # Fetch linked documentation in parallel
            best_practices: list[LinkedDocContent] = []
            if doc_links:
                fetch_tasks = [
                    _fetch_doc_content(client, url, title)
                    for url, title in doc_links
                ]
                results = await asyncio.gather(*fetch_tasks, return_exceptions=True)

                for result in results:
                    if isinstance(result, LinkedDocContent):
                        best_practices.append(result)

            # Inspect container (silently skip if Docker unavailable or no org selected)
            org = OrgSession.get_org()
            filesystem_tree = None
            available_users: list[ContainerUserInfo] = []
            if org:
                dev_image_ref = f"cgr.dev/{org}/{image_name}:latest-dev"
                # Run both inspections in parallel
                fs_task = _inspect_container_filesystem(dev_image_ref)
                users_task = _inspect_container_users(dev_image_ref)
                filesystem_tree, available_users = await asyncio.gather(
                    fs_task, users_task
                )

            # Generate actionable user guidance based on detected users
            user_guidance = _generate_user_guidance(available_users)

            return ImageOverviewResult(
                found=True,
                image_name=image_name,
                overview_url=overview_url,
                user_guidance=user_guidance,
                conversion_tips=CONVERSION_TIPS,
                available_users=available_users,
                filesystem_tree=filesystem_tree,
                overview_text=overview_text,
                best_practices=best_practices,
            )

        except httpx.TimeoutException:
            return ImageOverviewResult(
                found=False,
                image_name=image_name,
                message="Request timed out fetching overview",
            )
        except httpx.RequestError as e:
            return ImageOverviewResult(
                found=False,
                image_name=image_name,
                message=f"Failed to fetch overview: {e}",
            )


def _extract_overview_text(html: str) -> str:
    """Extract the main overview text content from the HTML page."""
    # Remove script and style tags first
    html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)

    # The images.chainguard.dev site renders markdown content.
    # Look for content between "Chainguard Container for" and the footer/end markers
    content_match = re.search(
        r"(Chainguard Container for.*?)(?:Contact Us|©\s*\d{4}|$)",
        html,
        re.DOTALL | re.IGNORECASE,
    )

    if content_match:
        content = content_match.group(1)
        return _html_to_text(content)

    # Fallback: try to find "Minimal" description pattern
    minimal_match = re.search(
        r"(Minimal [^<]+image based on Wolfi.*?)(?:Contact Us|©\s*\d{4}|$)",
        html,
        re.DOTALL | re.IGNORECASE,
    )
    if minimal_match:
        content = minimal_match.group(1)
        return _html_to_text(content)

    return ""


def _html_to_text(html: str) -> str:
    """Convert HTML to plain text."""
    # Replace common block elements with newlines
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    html = re.sub(r"</p>", "\n\n", html, flags=re.IGNORECASE)
    html = re.sub(r"</div>", "\n", html, flags=re.IGNORECASE)
    html = re.sub(r"</li>", "\n", html, flags=re.IGNORECASE)
    html = re.sub(r"<h[1-6][^>]*>", "\n\n## ", html, flags=re.IGNORECASE)
    html = re.sub(r"</h[1-6]>", "\n\n", html, flags=re.IGNORECASE)

    # Handle code blocks
    html = re.sub(r"<pre[^>]*>", "\n```\n", html, flags=re.IGNORECASE)
    html = re.sub(r"</pre>", "\n```\n", html, flags=re.IGNORECASE)
    html = re.sub(r"<code[^>]*>", "`", html, flags=re.IGNORECASE)
    html = re.sub(r"</code>", "`", html, flags=re.IGNORECASE)

    # Handle lists
    html = re.sub(r"<ul[^>]*>", "\n", html, flags=re.IGNORECASE)
    html = re.sub(r"<ol[^>]*>", "\n", html, flags=re.IGNORECASE)
    html = re.sub(r"<li[^>]*>", "- ", html, flags=re.IGNORECASE)

    # Remove all remaining HTML tags
    html = re.sub(r"<[^>]+>", "", html)

    # Decode common HTML entities
    html = html.replace("&nbsp;", " ")
    html = html.replace("&amp;", "&")
    html = html.replace("&lt;", "<")
    html = html.replace("&gt;", ">")
    html = html.replace("&quot;", '"')
    html = html.replace("&#39;", "'")
    html = html.replace("&#x27;", "'")
    html = html.replace("&apos;", "'")

    # Clean up whitespace
    # Replace multiple spaces with single space
    html = re.sub(r"[ \t]+", " ", html)
    # Replace multiple newlines with double newline
    html = re.sub(r"\n\s*\n\s*\n+", "\n\n", html)
    # Strip leading/trailing whitespace from lines
    lines = [line.strip() for line in html.split("\n")]
    html = "\n".join(lines)

    # Clean up empty code blocks (``` followed by ``` with just whitespace)
    html = re.sub(r"```\s*```", "", html)
    # Clean up remaining empty backticks
    html = re.sub(r"``", "", html)

    # Final cleanup of multiple newlines
    html = re.sub(r"\n\s*\n\s*\n+", "\n\n", html)

    return html.strip()
