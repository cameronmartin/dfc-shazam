#!/usr/bin/env python3
"""Extract image aliases from chainguard-images metadata files into a CSV."""

import csv
import sys
from pathlib import Path

import yaml


def extract_aliases(vendor_dirs: list[Path], output_path: Path) -> None:
    """Parse all metadata.yaml files and extract aliases to CSV."""
    rows: set[tuple[str, str]] = set()

    for vendor_dir in vendor_dirs:
        images_dir = vendor_dir / "images"
        if not images_dir.exists():
            print(f"Warning: {images_dir} does not exist, skipping", file=sys.stderr)
            continue

        for metadata_file in images_dir.glob("*/metadata.yaml"):
            image_name = metadata_file.parent.name

            # Skip FIPS, iamguarded, and request- images
            if "-fips" in image_name or image_name.endswith("fips"):
                continue
            if "-iamguarded" in image_name or image_name.endswith("iamguarded"):
                continue
            if image_name.startswith("request-"):
                continue

            with open(metadata_file) as f:
                metadata = yaml.safe_load(f)

            aliases = metadata.get("aliases", [])
            if aliases:
                for alias in aliases:
                    # Strip tag from alias (everything after the colon)
                    alias_image = alias.rsplit(":", 1)[0]
                    # Normalize registry prefixes
                    for prefix in (
                        "docker.io/library/",
                        "docker.io/",
                        "index.docker.io/library/",
                        "index.docker.io/",
                        "library/",
                        "registry.access.redhat.com/",
                        "registry.redhat.io/",
                        "quay.io/",
                        "gcr.io/",
                        "ghcr.io/",
                        "public.ecr.aws/",
                        "mcr.microsoft.com/",
                    ):
                        if alias_image.startswith(prefix):
                            alias_image = alias_image[len(prefix) :]
                            break
                    rows.add((image_name, alias_image))

    # Sort by alias, then chainguard image name
    sorted_rows = sorted(rows, key=lambda x: (x[1], x[0]))

    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["alias", "chainguard_image"])
        # Swap columns: (chainguard, alias) -> (alias, chainguard)
        writer.writerows((alias, cg) for cg, alias in sorted_rows)

    print(f"Wrote {len(sorted_rows)} aliases to {output_path}")


def main() -> None:
    script_dir = Path(__file__).parent
    project_root = script_dir.parent

    vendor_dirs = [
        project_root / "vendor" / "chainguard-images",
        project_root / "vendor" / "chainguard-images-private",
    ]

    output_path = project_root / "src" / "dfc_shazam" / "mappings" / "image_aliases.csv"

    extract_aliases(vendor_dirs, output_path)


if __name__ == "__main__":
    main()
