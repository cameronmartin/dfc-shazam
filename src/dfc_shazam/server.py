"""MCP server for Dockerfile to Chainguard conversion assistance."""

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from dfc_shazam.tools.image_docs import get_migration_instructions_for_chainguard_image
from dfc_shazam.tools.find_equiv_cgr_image import find_equivalent_chainguard_image
from dfc_shazam.tools.map_package import find_equivalent_apk_packages
from dfc_shazam.tools.verify_packages import validate_apk_packages_install

# All tools in this server are read-only (they query data, don't modify anything)
READ_ONLY_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    openWorldHint=True,  # They interact with external registries/APIs
)

# Create the MCP server
mcp = FastMCP(
    name="dfc-shazam",
    instructions="""This MCP server helps with converting Dockerfiles to use Chainguard images.

RECOMMENDED TOOL WORKFLOW:
1. find_equivalent_chainguard_image - Find the Chainguard image equivalent for a source image (handles org selection AND variant selection)
2. get_migration_instructions_for_chainguard_image - Get migration guidance, verify image exists, and retrieve best practices
3. find_equivalent_apk_packages - Map apt/yum package names to APK equivalents
4. validate_apk_packages_install - REQUIRED: Verify ALL mapped packages install correctly BEFORE editing the Dockerfile

CRITICAL: Steps 3-4 are MANDATORY when the Dockerfile installs packages. Never edit a Dockerfile with package mappings until you have validated them with validate_apk_packages_install.

Prerequisites:
- chainctl must be installed and authenticated (run 'chainctl auth login')

---

CRITICAL BEHAVIORAL REQUIREMENTS:

1. ORGANIZATION SELECTION:
   - If find_equivalent_chainguard_image returns a list of organizations, you MUST show the FULL list to the user and ask them to choose one
   - Never summarize or truncate the organization list
   - Always use cgr.dev/{org}/<image> format, never cgr.dev/chainguard/<image>

2. VARIANT SELECTION:
   - If find_equivalent_chainguard_image returns variant_capabilities without a matched_tag, you MUST ask the user which variant they need
   - Show the actual capabilities (shell/apk availability) for each variant
   - DO NOT infer or guess the answer from the Dockerfile - explicitly ask the user
   - Call find_equivalent_chainguard_image again with the 'variant' parameter set to their choice

3. AFTER find_equivalent_chainguard_image (with full_image_ref):
   - You MUST call get_migration_instructions_for_chainguard_image to retrieve best practices BEFORE modifying any Dockerfile
   - The result contains essential information about entrypoints, users, paths, and image-specific details

4. ENTRYPOINT REVIEW (from get_migration_instructions_for_chainguard_image):
   - Carefully review the entrypoint_guidance field
   - Chainguard image entrypoints may differ from the original image
   - Distroless images cannot use shell-form commands (must use exec form)
   - CMD arguments are appended to the entrypoint, not executed directly

5. PACKAGE MAPPING AND VALIDATION (CRITICAL):
   - ALWAYS call find_equivalent_apk_packages first to get suggested APK package names
   - ALWAYS call validate_apk_packages_install with the suggested packages BEFORE editing the Dockerfile
   - The validation tool runs `apk add --simulate` to verify packages exist and can be resolved
   - If validation fails for any package, do NOT use that package - find an alternative or ask the user
   - Package mappings are suggestions based on fuzzy matching; they may be wrong. Validation catches errors BEFORE they break the build.
   - Example workflow:
     1. find_equivalent_apk_packages(["liblapack-dev", "curl"]) → suggests ["liblapacke", "curl"]
     2. validate_apk_packages_install(["liblapacke", "curl"]) → shows if packages exist
     3. If validation fails, try alternatives (e.g., "openblas-dev" for LAPACK)
     4. Only edit Dockerfile after ALL packages validate successfully

6. PACKAGE INSTALLATION IN DOCKERFILE:
   - Chainguard images run as non-root by default
   - When installing packages with apk, switch to root first, then IMMEDIATELY switch back:
     USER root
     RUN apk add --no-cache <packages>
     USER nonroot  # ⚠️ MUST be on the VERY NEXT LINE after apk add!
   - 🚨 CRITICAL: `USER nonroot` MUST appear IMMEDIATELY after `apk add`, on the very next line
   - Never leave subsequent Dockerfile instructions running as root - this is a security vulnerability
   - If you have multiple RUN commands after apk add, they will ALL run as root if you forget to switch back

7. MULTI-STAGE BUILDS:
   - If Dockerfile needs packages but user wants distroless for production, use multi-stage:
     FROM cgr.dev/{org}/python:latest-dev AS builder
     USER root
     RUN apk add --no-cache build-base
     RUN pip install --user mypackage
     USER nonroot

     FROM cgr.dev/{org}/python:latest
     COPY --from=builder /home/nonroot/.local /home/nonroot/.local

8. VARIANT CAPABILITIES:
   - Capabilities vary by image - always check variant_capabilities from find_equivalent_chainguard_image
   - dev variants: Always have apk package manager
   - distroless/slim: May or may not have shell depending on the image
   - Do NOT assume based on variant name alone

COMMON IMAGE EQUIVALENTS:
- eclipse-temurin → adoptium-jdk or adoptium-jre
- openjdk → jdk or jre
- amazoncorretto → amazon-corretto-jdk or amazon-corretto-jre
""",
)

# Register tools (all are read-only)
mcp.tool(annotations=READ_ONLY_ANNOTATIONS)(find_equivalent_chainguard_image)
mcp.tool(annotations=READ_ONLY_ANNOTATIONS)(get_migration_instructions_for_chainguard_image)
mcp.tool(annotations=READ_ONLY_ANNOTATIONS)(find_equivalent_apk_packages)
mcp.tool(annotations=READ_ONLY_ANNOTATIONS)(validate_apk_packages_install)


def main() -> None:
    """Run the MCP server."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
