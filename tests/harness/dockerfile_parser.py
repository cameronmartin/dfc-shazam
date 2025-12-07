"""Dockerfile parsing utilities."""

import json
import re

from tests.harness.models import ParsedDockerfile

# Regex patterns for Dockerfile parsing
FROM_PATTERN = re.compile(
    r"^FROM\s+(?:--platform=\S+\s+)?(\S+?)(?:\s+AS\s+(\S+))?$",
    re.IGNORECASE | re.MULTILINE,
)
RUN_PATTERN = re.compile(r"^RUN\s+(.+)$", re.IGNORECASE | re.MULTILINE | re.DOTALL)
COPY_PATTERN = re.compile(r"^COPY\s+(.+)$", re.IGNORECASE | re.MULTILINE)
ENV_PATTERN = re.compile(r"^ENV\s+(\w+)(?:=|\s+)(.*)$", re.IGNORECASE | re.MULTILINE)
EXPOSE_PATTERN = re.compile(r"^EXPOSE\s+(\d+)", re.IGNORECASE | re.MULTILINE)
CMD_PATTERN = re.compile(r"^CMD\s+(.+)$", re.IGNORECASE | re.MULTILINE)
ENTRYPOINT_PATTERN = re.compile(r"^ENTRYPOINT\s+(.+)$", re.IGNORECASE | re.MULTILINE)

# Package manager patterns
APT_INSTALL_PATTERN = re.compile(
    r"apt(?:-get)?\s+(?:update\s*(?:&&|;)\s*)?apt(?:-get)?\s+install\s+(?:-y\s+)?(?:--no-install-recommends\s+)?(.+?)(?:\s*&&|\s*\\?\s*$)",
    re.IGNORECASE,
)
APT_SIMPLE_PATTERN = re.compile(
    r"apt(?:-get)?\s+install\s+(?:-y\s+)?(?:--no-install-recommends\s+)?(.+?)(?:\s*&&|\s*\\?\s*$)",
    re.IGNORECASE,
)
APK_PATTERN = re.compile(
    r"apk\s+(?:--no-cache\s+)?add\s+(.+?)(?:\s*&&|\s*\\?\s*$)",
    re.IGNORECASE,
)
YUM_PATTERN = re.compile(
    r"(?:yum|dnf)\s+(?:-y\s+)?install\s+(.+?)(?:\s*&&|\s*\\?\s*$)",
    re.IGNORECASE,
)


def parse_dockerfile(content: str) -> ParsedDockerfile:
    """Parse a Dockerfile and extract key information.

    Args:
        content: Dockerfile content as string

    Returns:
        ParsedDockerfile with extracted information
    """
    # Normalize line continuations
    normalized = normalize_continuations(content)

    # Extract base images and stages
    base_images: list[str] = []
    stages: list[dict[str, str]] = []

    for match in FROM_PATTERN.finditer(normalized):
        image = match.group(1)
        stage_name = match.group(2)
        base_images.append(image)
        stages.append(
            {
                "base": image,
                "name": stage_name or f"stage_{len(stages)}",
            }
        )

    # Extract RUN commands
    run_commands: list[str] = []
    for match in RUN_PATTERN.finditer(normalized):
        run_commands.append(match.group(1).strip())

    # Extract packages by package manager
    packages: dict[str, list[str]] = {
        "apt": [],
        "apk": [],
        "yum": [],
    }

    for run_cmd in run_commands:
        # Parse apt packages
        for pattern in [APT_INSTALL_PATTERN, APT_SIMPLE_PATTERN]:
            for match in pattern.finditer(run_cmd):
                pkg_str = match.group(1)
                packages["apt"].extend(parse_package_list(pkg_str))

        # Parse apk packages
        for match in APK_PATTERN.finditer(run_cmd):
            pkg_str = match.group(1)
            packages["apk"].extend(parse_package_list(pkg_str))

        # Parse yum/dnf packages
        for match in YUM_PATTERN.finditer(run_cmd):
            pkg_str = match.group(1)
            packages["yum"].extend(parse_package_list(pkg_str))

    # Deduplicate packages
    for pm in packages:
        packages[pm] = list(dict.fromkeys(packages[pm]))

    # Extract COPY commands
    copy_commands = [m.group(1).strip() for m in COPY_PATTERN.finditer(normalized)]

    # Extract ENV vars
    env_vars: dict[str, str] = {}
    for match in ENV_PATTERN.finditer(normalized):
        env_vars[match.group(1)] = match.group(2).strip().strip("\"'")

    # Extract exposed ports
    exposed_ports = [int(m.group(1)) for m in EXPOSE_PATTERN.finditer(normalized)]

    # Extract CMD (use last one)
    cmd = None
    cmd_matches = list(CMD_PATTERN.finditer(normalized))
    if cmd_matches:
        cmd = parse_cmd_entrypoint(cmd_matches[-1].group(1))

    # Extract ENTRYPOINT (use last one)
    entrypoint = None
    ep_matches = list(ENTRYPOINT_PATTERN.finditer(normalized))
    if ep_matches:
        entrypoint = parse_cmd_entrypoint(ep_matches[-1].group(1))

    return ParsedDockerfile(
        base_images=base_images,
        stages=stages,
        packages=packages,
        run_commands=run_commands,
        copy_commands=copy_commands,
        env_vars=env_vars,
        exposed_ports=exposed_ports,
        cmd=cmd,
        entrypoint=entrypoint,
    )


def normalize_continuations(content: str) -> str:
    """Normalize line continuations in Dockerfile.

    Args:
        content: Raw Dockerfile content

    Returns:
        Content with line continuations joined
    """
    lines = content.split("\n")
    normalized_lines: list[str] = []
    current_line = ""

    for line in lines:
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            current_line += stripped[:-1] + " "
        else:
            current_line += stripped
            normalized_lines.append(current_line)
            current_line = ""

    if current_line:
        normalized_lines.append(current_line)

    return "\n".join(normalized_lines)


def parse_package_list(pkg_str: str) -> list[str]:
    """Parse a space-separated package list, filtering options.

    Args:
        pkg_str: Package string from RUN command

    Returns:
        List of package names
    """
    packages: list[str] = []
    skip_tokens = {
        "&&",
        "||",
        ";",
        "|",
        "rm",
        "apt-get",
        "apt",
        "clean",
        "autoremove",
        "update",
        "install",
        "-y",
        "-q",
        "-qq",
        "\\",
    }

    for token in pkg_str.split():
        token = token.strip().rstrip("\\")
        # Skip options and empty tokens
        if not token or token.startswith("-") or token in skip_tokens:
            continue
        # Skip if it contains special characters indicating it's not a package
        if any(c in token for c in ["=", "/", "$", "(", ")"]):
            continue
        packages.append(token)

    return packages


def parse_cmd_entrypoint(value: str) -> list[str]:
    """Parse CMD or ENTRYPOINT value.

    Args:
        value: CMD/ENTRYPOINT value string

    Returns:
        List of command parts
    """
    value = value.strip()

    # JSON array format
    if value.startswith("["):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            pass

    # Shell format
    return value.split()


def get_package_manager(parsed: ParsedDockerfile) -> str | None:
    """Determine the primary package manager used.

    Args:
        parsed: Parsed Dockerfile

    Returns:
        Package manager name or None
    """
    for pm in ["apt", "yum", "apk"]:
        if parsed.packages.get(pm):
            return pm
    return None


def is_multistage(parsed: ParsedDockerfile) -> bool:
    """Check if Dockerfile uses multi-stage builds.

    Args:
        parsed: Parsed Dockerfile

    Returns:
        True if multi-stage
    """
    return len(parsed.stages) > 1


def get_final_stage(parsed: ParsedDockerfile) -> dict[str, str] | None:
    """Get the final stage of a Dockerfile.

    Args:
        parsed: Parsed Dockerfile

    Returns:
        Final stage info or None
    """
    if parsed.stages:
        return parsed.stages[-1]
    return None
