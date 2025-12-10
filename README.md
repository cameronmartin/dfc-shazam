# dfc-shazam

MCP server for Dockerfile to Chainguard image conversion assistance.

## Overview

dfc-shazam provides AI assistants with tools to help convert Dockerfiles to use Chainguard images. It offers:

- **Image lookup**: Find Chainguard equivalents for Docker Hub images (includes organization and variant selection)
- **Runtime recommendations**: Automatic guidance for build-only images (Go, Rust, JDK, Maven, Gradle) with recommended runtime images for multi-stage builds
- **Migration instructions**: Get best practices, entrypoint guidance, and user/permission documentation
- **Package mapping**: Map apt/yum package names to APK equivalents (uses builtin mappings from [dfc](https://github.com/chainguard-dev/dfc) with fuzzy search fallback against Wolfi APK index and Chainguard extras repository)
- **Package validation**: Verify APK packages install correctly before editing Dockerfiles

## Prerequisites

1. **uv** - Python package manager ([install](https://docs.astral.sh/uv/getting-started/installation/))

2. **chainctl** - Chainguard CLI tool:
   ```bash
   # Install (macOS)
   brew install chainguard-dev/tap/chainctl

   # Or download from https://edu.chainguard.dev/chainguard/chainctl-usage/getting-started-with-chainctl/

   # Authenticate
   chainctl auth login
   ```

3. **crane** (optional) - Required for image configuration inspection (entrypoint, shell/apk availability). Install with `go install github.com/google/go-containerregistry/cmd/crane@latest`

4. **Docker** (optional) - Required for `validate_apk_packages_install` and filesystem inspection

## Installation

```bash
cd dfc-shazam
uv sync
```

## Usage

Copy the example configuration file and update the path:

```bash
cp .mcp.json.example .mcp.json
# Edit .mcp.json to set the correct path to dfc-shazam
```

### With Claude Code

The `.mcp.json` file will be automatically detected. Example configuration:

```json
{
  "mcpServers": {
    "dfc-shazam": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--directory", "/path/to/dfc-shazam", "dfc-shazam"]
    }
  }
}
```

### With Claude Desktop

Add to your Claude Desktop configuration (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "dfc-shazam": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/dfc-shazam", "dfc-shazam"]
    }
  }
}
```

### With MCP Inspector

```bash
uv run mcp dev src/dfc_shazam/server.py
```

## Recommended Workflow

1. **find_equivalent_chainguard_image** - Find the Chainguard image equivalent (handles org and variant selection)
2. **get_migration_instructions_for_chainguard_image** - Get migration guidance and best practices
3. **find_equivalent_apk_packages** - Map apt/yum packages to APK equivalents
4. **validate_apk_packages_install** - Verify packages install before editing Dockerfile

## Tools

### find_equivalent_chainguard_image

Find Chainguard equivalents for Docker Hub images. Handles organization selection, variant selection, and tag matching with JDK-aware version matching.

**Parameters:**
- `source_image_and_tag` (required): Source image with optional tag (e.g., "python:3.12", "maven:3.8-eclipse-temurin-17")
- `organization` (optional): Chainguard organization name
- `variant` (optional): "distroless", "slim", or "dev"

**Behavior:**
- Auto-selects organization if only one is available
- Returns organization list for user selection if multiple are available
- Returns variant capabilities (shell/apk availability) for user selection

**Tag Matching Features:**
- Matches versioned tags with prefixes (e.g., `adoptium-openjdk-17`)
- JDK-aware matching: `maven:3.8-eclipse-temurin-17` correctly matches `3.8-jdk17-dev`, not `3.8-jdk11-dev`
- Penalizes JDK version mismatches to avoid wrong Java version selection

### get_migration_instructions_for_chainguard_image

Verify an image:tag exists and get comprehensive migration guidance.

**Parameters:**
- `image_reference` (required): Full image reference (e.g., "cgr.dev/{org}/python:3.12")

**Prerequisite:** Call `find_equivalent_chainguard_image` first to select an organization and determine the appropriate image:tag.

**Returns:**
- Image digest (verifies the image exists)
- Image configuration (entrypoint, cmd, user, workdir, env, shell/apk availability)
- Entrypoint guidance and compatibility notes
- User/permission guidance (critical for non-root containers)
- Conversion tips and best practices
- Container filesystem tree (if Docker is available)
- Available users from /etc/passwd
- Linked documentation from edu.chainguard.dev

### find_equivalent_apk_packages

Map apt/yum package names to APK equivalents. Uses builtin mappings from [dfc](https://github.com/chainguard-dev/dfc) with fuzzy search fallback against the Wolfi APK index and Chainguard extras repository.

**Parameters:**
- `packages` (required): List of package names (e.g., `["libssl-dev", "build-essential"]`)
- `source_distro` (optional): "apt", "yum", "dnf", or "auto" (default)

**Mapping Sources:**
1. Builtin mappings (vendored from dfc) - exact matches
2. Fuzzy search against Wolfi APK index and Chainguard extras repository - for packages not in builtin mappings

### validate_apk_packages_install

Verify APK packages install correctly using a dry-run simulation (`apk add --simulate`).

**Parameters:**
- `packages` (required): List of APK package names to verify
- `arch` (optional): "x86_64" (default) or "aarch64"

**Prerequisite:** Call `find_equivalent_chainguard_image` first to select an organization.

**Behavior:**
- Uses `chainguard-base:latest` if available, otherwise falls back to any available `-dev` image
- Runs `apk add --simulate` (dry-run) which is faster than actual installation
- Returns which packages succeeded/failed with error details

**Important:** Always validate packages before editing Dockerfiles. Package mappings are suggestions that may be incorrect.

## Package Mappings

Builtin mappings are vendored from [chainguard-dev/dfc](https://github.com/chainguard-dev/dfc):

| apt (Debian/Ubuntu) | APK (Chainguard) |
|---------------------|------------------|
| build-essential | build-base |
| libssl-dev | libssl3 |
| libpq-dev | postgresql-dev |
| python3 | python-3 |
| python3-pip | py3-pip |
| ssh | openssh-client, openssh-server |
| ... | ... |

Packages not in builtin mappings fall back to fuzzy search against the Wolfi APK index and Chainguard extras repository.

## Image Mappings

| Docker Hub | Chainguard |
|------------|------------|
| python | cgr.dev/{org}/python |
| node | cgr.dev/{org}/node |
| golang/go | cgr.dev/{org}/go |
| eclipse-temurin | cgr.dev/{org}/adoptium-jdk or adoptium-jre |
| maven | cgr.dev/{org}/maven |
| alpine/ubuntu/debian | cgr.dev/{org}/chainguard-base |
| ... | ... |

## Development

```bash
# Install with dev dependencies
uv sync --all-extras

# Lint
uv run ruff check src/

# Type check
uv run mypy src/
```

## License

Copyright 2025 Chainguard, Inc. See [LICENSE](LICENSE) for details.
