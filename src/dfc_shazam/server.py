"""MCP server for Dockerfile to Chainguard conversion assistance."""

from mcp.server.fastmcp import FastMCP

from dfc_shazam.tools.image_docs import get_image_overview
from dfc_shazam.tools.lookup_image import lookup_chainguard_image
from dfc_shazam.tools.lookup_tag import lookup_tag
from dfc_shazam.tools.map_package import map_package
from dfc_shazam.tools.search_packages import search_apk_packages
from dfc_shazam.tools.verify_packages import verify_apk_packages
from dfc_shazam.tools.verify_tag import verify_image_tag

# Create the MCP server
mcp = FastMCP(
    name="dfc-shazam",
    instructions="""This MCP server helps with converting Dockerfiles to use Chainguard images.

Available tools:
- lookup_chainguard_image: Find Chainguard equivalents for Docker Hub images
- lookup_tag: Find the best matching Chainguard tag for an original image tag
- verify_image_tag: Verify an image:tag exists in the Chainguard registry
- search_apk_packages: Search the Wolfi APK package index
- map_package: Map apt/yum package names to APK equivalents
- verify_apk_packages: Verify APK packages install correctly in a Chainguard container
- get_image_overview: Get documentation and overview from images.chainguard.dev

Prerequisites:
- chainctl must be installed and authenticated (run 'chainctl auth login')
""",
)

# Register tools
mcp.tool()(lookup_chainguard_image)
mcp.tool()(lookup_tag)
mcp.tool()(verify_image_tag)
mcp.tool()(search_apk_packages)
mcp.tool()(map_package)
mcp.tool()(verify_apk_packages)
mcp.tool()(get_image_overview)


def main() -> None:
    """Run the MCP server."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
