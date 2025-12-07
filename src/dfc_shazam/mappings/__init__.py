"""Image mappings for Dockerfile conversion."""

from dfc_shazam.mappings.images import (
    ImageMatch,
    is_generic_base_image,
    lookup_chainguard_image,
)

__all__ = [
    "ImageMatch",
    "is_generic_base_image",
    "lookup_chainguard_image",
]
