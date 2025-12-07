# dfc-shazam

MCP server for Dockerfile to Chainguard image conversion assistance.

## Overview

dfc-shazam provides AI assistants with tools to help convert Dockerfiles to use Chainguard images. It offers:

- **Image lookup**: Find Chainguard equivalents for Docker Hub images
- **Tag lookup**: Find the best matching Chainguard tag for an original image tag
- **Tag verification**: Verify image:tag combinations exist in the Chainguard registry
- **Package search**: Search the Chainguard APK package index
- **Package mapping**: Map apt/yum package names to APK equivalents
- **Package verification**: Verify APK packages install correctly in a container
- **Image documentation**: Get best practices and documentation for Chainguard images

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

3. **Docker** (optional) - Required for `verify_apk_packages` and `get_image_overview` filesystem inspection

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

## Organization Selection

On first use, the `lookup_chainguard_image` tool will retrieve your available Chainguard organizations from `chainctl auth status` and prompt you to select one. This organization is used for all subsequent image operations in the session.

All Chainguard images are referenced using `cgr.dev/{org}/<image>` format, where `{org}` is your selected organization. Never use `cgr.dev/chainguard/<image>`.

## Tools

### lookup_chainguard_image

Find Chainguard equivalents for Docker Hub images. On first call, prompts for organization selection.

**Parameters:**
- `source_image` (required): Source Docker Hub image name (e.g., "python", "node:18", "nginx")
- `organization` (optional): Chainguard organization name. If not provided on first call, available organizations will be listed for selection.

**Example (first call):**
```
Input: source_image="python"
Output:
  - found: false
  - message: "ORGANIZATION SELECTION REQUIRED - You have access to: org1, org2, org3..."
```

**Example (with organization):**
```
Input: source_image="python", organization="my-org"
Output:
  - chainguard_image: cgr.dev/my-org/python
  - recommendation: Use cgr.dev/my-org/python
```

### verify_image_tag

Verify an image:tag exists in the Chainguard registry.

**Parameters:**
- `image_reference` (required): Full image reference (e.g., "cgr.dev/chainguard/python:3.12")

**Example:**
```
Input: image_reference="cgr.dev/chainguard/python:3.12"
Output:
  - exists: true
  - digest: sha256:abc123...
```

### search_apk_packages

Search the Chainguard APK package index.

**Parameters:**
- `query` (required): Package name or search term
- `arch` (optional): Architecture, "x86_64" (default) or "aarch64"
- `limit` (optional): Maximum results (default: 20, max: 100)
- `search_type` (optional): "name" (default), "cmd" (find package providing a command), or "so" (find package providing a shared library)

**Example:**
```
Input: query="openssl"
Output:
  - packages: [{name: "openssl", version: "3.2.1-r0", ...}, ...]
  - total_count: 5
```

### map_package

Map apt/yum package names to APK equivalents. Accepts multiple packages in a single call for efficiency.

**Parameters:**
- `packages` (required): List of source package names (e.g., `["libssl-dev", "build-essential", "curl"]`)
- `source_distro` (optional): "apt", "yum", "dnf", or "auto" (default)

**Example:**
```
Input: packages=["libssl-dev", "curl"], source_distro="apt"
Output:
  - source_distro: "apt"
  - results: [
      {source_package: "libssl-dev", best_match: "openssl-dev", ...},
      {source_package: "curl", best_match: "curl", ...}
    ]
  - summary: "APK packages: openssl-dev curl"
```

### lookup_tag

Find the best matching Chainguard tag for an original image tag.

**Parameters:**
- `chainguard_image` (required): Chainguard image name (e.g., "python", "node")
- `original_image` (required): Original source image name (e.g., "python", "node:18-alpine")
- `original_tag` (required): Original tag to match (e.g., "3.12", "18-alpine", "latest")
- `require_dev` (required): Whether to prefer -dev variant (includes shell/apk)

**Example:**
```
Input: chainguard_image="python", original_image="python", original_tag="3.12", require_dev=false
Output:
  - found: true
  - matched_tag: "3.12"
  - full_image_ref: "cgr.dev/my-org/python:3.12"
  - available_tags: ["3.12", "3.12-dev", "3.11", ...]
```

### verify_apk_packages

Verify that APK packages install correctly in a Chainguard container using a dry-run.

**Parameters:**
- `packages` (required): List of APK package names to verify (e.g., `["openssl", "curl", "git"]`)
- `arch` (optional): Architecture, "x86_64" (default) or "aarch64"

**Example:**
```
Input: packages=["openssl", "curl"]
Output:
  - success: true
  - packages: ["openssl", "curl"]
  - installed: ["openssl", "curl"]
  - message: "All 2 package(s) verified successfully (dry-run)."
```

### get_image_overview

Get overview documentation and best practices for a Chainguard image. Automatically follows links to retrieve content from related documentation pages (getting started guides, best practices, migration guides). Also inspects the container filesystem to show directory ownership and permissions.

**Parameters:**
- `image_name` (required): Chainguard image name (e.g., "python", "node", "nginx")

**Example:**
```
Input: image_name="python"
Output:
  - found: true
  - image_name: "python"
  - overview_url: "https://images.chainguard.dev/directory/image/python/overview"
  - overview_text: "Chainguard Container for Python development..."
  - best_practices: [
      {url: "https://edu.chainguard.dev/...", title: "Getting Started", content: "..."},
      {url: "https://edu.chainguard.dev/...", title: "Best Practices", content: "..."}
    ]
  - filesystem_tree: |
      drwxr-xr-x root:root /
      drwxr-xr-x root:root /etc
      drwxr-xr-x root:root /home
      drwxr-xr-x nonroot:nonroot /home/nonroot
      ...
```

Note: `filesystem_tree` requires Docker to be available. If Docker is not installed, this field will be `null` but other information will still be returned.

## Package Mappings

The tool includes mappings for common packages:

| apt (Debian/Ubuntu) | APK (Chainguard) |
|---------------------|-------------|
| build-essential | build-base |
| libssl-dev | openssl-dev |
| python3-dev | python-3-dev |
| libpq-dev | postgresql-dev, libpq-dev |
| ... | ... |

Package mappings are computed dynamically using fuzzy matching against the Chainguard APK index.

## Image Mappings

| Docker Hub | Chainguard |
|------------|------------|
| python | cgr.dev/{org}/python |
| node | cgr.dev/{org}/node |
| golang/go | cgr.dev/{org}/go |
| nginx | cgr.dev/{org}/nginx |
| postgres | cgr.dev/{org}/postgres |
| alpine/ubuntu/debian | cgr.dev/{org}/chainguard-base |
| ... | ... |

Image aliases are loaded from [src/dfc_shazam/mappings/image_aliases.csv](src/dfc_shazam/mappings/image_aliases.csv).

## Development

```bash
# Install with dev dependencies
uv sync --all-extras

# Run tests
uv run pytest

# Lint
uv run ruff check src/

# Type check
uv run mypy src/
```

## License

Copyright 2025 Chainguard, Inc. See [LICENSE](LICENSE) for details.
