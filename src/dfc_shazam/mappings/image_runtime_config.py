"""Runtime configuration for build-only images.

This module defines which images are "build-only" (should not be used for runtime)
and provides runtime recommendations for multi-stage builds.
"""

# Image runtime configuration
# Types:
#   - compile_to_binary: Languages that compile to native binaries (Go, Rust)
#   - sdk_runtime_pair: SDK images with corresponding runtime images (JDK -> JRE)
#   - build_tool_with_jdk: Build tools that include JDK (Maven, Gradle)
#   - same_family: Interpreted languages where build and runtime use same image family

from typing import Any

IMAGE_RUNTIME_CONFIG: dict[str, Any] = {
    # Compile-to-binary languages (produce static/dynamic binaries)
    "go": {
        "type": "compile_to_binary",
        "runtime_options": [
            {
                "image": "static",
                "description": "For CGO_ENABLED=0 builds (smallest)",
                "default": True,
                "build_flags": ["CGO_ENABLED=0"],
            },
            {
                "image": "glibc-dynamic",
                "description": "For CGO builds or dynamic linking",
            },
        ],
    },
    "rust": {
        "type": "compile_to_binary",
        "runtime_options": [
            {
                "image": "glibc-dynamic",
                "description": "Standard Rust binaries (default linking)",
                "default": True,
            },
            {
                "image": "static",
                "description": "For statically-linked binaries",
                "build_flags": ["RUSTFLAGS='-C target-feature=+crt-static'"],
            },
        ],
    },
    # SDK to Runtime mappings (Java ecosystem)
    "jdk": {
        "type": "sdk_runtime_pair",
        "runtime_image": "jre",
    },
    "adoptium-jdk": {
        "type": "sdk_runtime_pair",
        "runtime_image": "adoptium-jre",
    },
    "amazon-corretto-jdk": {
        "type": "sdk_runtime_pair",
        "runtime_image": "amazon-corretto-jre",
    },
    # Build tools with JDK - runtime depends on JDK version in tag
    "maven": {
        "type": "build_tool_with_jdk",
        "default_runtime": "jre",
        "jdk_runtime_mapping": {
            "eclipse-temurin": "adoptium-jre",
            "temurin": "adoptium-jre",
            "corretto": "amazon-corretto-jre",
            "openjdk": "jre",
        },
    },
    "gradle": {
        "type": "build_tool_with_jdk",
        "default_runtime": "jre",
        "jdk_runtime_mapping": {
            "eclipse-temurin": "adoptium-jre",
            "temurin": "adoptium-jre",
            "corretto": "amazon-corretto-jre",
            "openjdk": "jre",
        },
    },
    # Interpreted languages - same image family, different variants
    "python": {
        "type": "same_family",
        "build_variant": "dev",
        "runtime_variant": "latest",
        "multi_stage_guidance": (
            "Python multi-stage build requires proper environment setup:\n"
            "  1. Copy venv: COPY --chown=nonroot:nonroot --from=builder /app/venv /app/venv\n"
            "  2. Set PATH: ENV PATH=\"/app/venv/bin:$PATH\"\n"
            "  3. CRITICAL: Also set PYTHONPATH for distroless images where the entrypoint is system Python:\n"
            "     ENV PYTHONPATH=\"/app/venv/lib/python3.x/site-packages\"\n"
            "     (Replace 3.x with your Python version, e.g., 3.12)\n"
            "  Alternative: Override entrypoint to use venv Python:\n"
            "     ENTRYPOINT [\"/app/venv/bin/python\"]\n"
            "     CMD [\"app.py\"]"
        ),
    },
    "node": {
        "type": "same_family",
        "build_variant": "dev",
        "runtime_variant": "latest",
        "multi_stage_guidance": (
            "Copy node_modules from builder (use --chown with the runtime image's user, typically nonroot):\n"
            "  COPY --chown=nonroot:nonroot --from=builder /app/node_modules /app/node_modules\n"
            "  COPY --chown=nonroot:nonroot --from=builder /app/dist /app/dist"
        ),
    },
}


# Multi-stage COPY guidance for build-only images
# Uses {user} placeholder - actual user must be determined from runtime image inspection
# Common users: nonroot (most images), postgres, nginx, redis, etc.
MULTI_STAGE_COPY_GUIDANCE = {
    "go": "COPY --chown={user}:{user} --from=builder /app/myapp /app/myapp",
    "rust": "COPY --chown={user}:{user} --from=builder /app/target/release/myapp /app/myapp",
    "jdk": "COPY --chown={user}:{user} --from=builder /app/target/*.jar /app/app.jar",
    "adoptium-jdk": "COPY --chown={user}:{user} --from=builder /app/target/*.jar /app/app.jar",
    "amazon-corretto-jdk": "COPY --chown={user}:{user} --from=builder /app/target/*.jar /app/app.jar",
    "maven": "COPY --chown={user}:{user} --from=builder /app/target/*.jar /app/app.jar",
    "gradle": "COPY --chown={user}:{user} --from=builder /app/build/libs/*.jar /app/app.jar",
}

# Default user for most Chainguard images
DEFAULT_CHAINGUARD_USER = "nonroot"
