"""Fixture loading utilities."""

from pathlib import Path
from typing import Iterator

import yaml

from tests.harness.models import DockerfileFixture, FixtureMetadata

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "dockerfiles"
EXPECTED_DIR = Path(__file__).parent.parent / "fixtures" / "expected"


def discover_fixtures(
    fixtures_dir: Path | None = None,
    tags: list[str] | None = None,
    languages: list[str] | None = None,
    complexity: str | None = None,
) -> Iterator[Path]:
    """Discover all fixture directories.

    Args:
        fixtures_dir: Root fixtures directory (defaults to FIXTURES_DIR)
        tags: Filter by tags (any match)
        languages: Filter by language
        complexity: Filter by complexity level

    Yields:
        Paths to fixture directories
    """
    fixtures_dir = fixtures_dir or FIXTURES_DIR

    if not fixtures_dir.exists():
        return

    for category_dir in sorted(fixtures_dir.iterdir()):
        if not category_dir.is_dir() or category_dir.name.startswith(("_", ".")):
            continue

        for fixture_dir in sorted(category_dir.iterdir()):
            if not fixture_dir.is_dir():
                continue

            fixture_yaml = fixture_dir / "fixture.yaml"
            if not fixture_yaml.exists():
                continue

            # Apply filters if specified
            if tags or languages or complexity:
                try:
                    metadata = load_metadata(fixture_yaml)
                except Exception:
                    continue

                if tags and not any(t in metadata.tags for t in tags):
                    continue
                if languages and metadata.language not in languages:
                    continue
                if complexity and metadata.complexity.value != complexity:
                    continue

            yield fixture_dir


def load_metadata(fixture_yaml: Path) -> FixtureMetadata:
    """Load fixture metadata from YAML.

    Args:
        fixture_yaml: Path to fixture.yaml file

    Returns:
        FixtureMetadata instance
    """
    with open(fixture_yaml) as f:
        data = yaml.safe_load(f)
    return FixtureMetadata(**data)


def load_fixture(fixture_dir: Path) -> DockerfileFixture:
    """Load a complete fixture.

    Args:
        fixture_dir: Path to fixture directory

    Returns:
        DockerfileFixture with all files loaded
    """
    metadata = load_metadata(fixture_dir / "fixture.yaml")

    # Load Dockerfile
    dockerfile_path = fixture_dir / metadata.build.dockerfile
    dockerfile_content = dockerfile_path.read_text()

    # Load all source files (exclude fixture.yaml and Dockerfile)
    source_files: dict[str, str] = {}
    excluded = {"fixture.yaml", metadata.build.dockerfile}

    for file_path in fixture_dir.rglob("*"):
        if file_path.is_file():
            rel_path = file_path.relative_to(fixture_dir)
            if str(rel_path) not in excluded and rel_path.name not in excluded:
                # Limit to text files
                try:
                    source_files[str(rel_path)] = file_path.read_text()
                except UnicodeDecodeError:
                    pass  # Skip binary files

    # Load expected Dockerfile if exists
    expected_path = EXPECTED_DIR / metadata.name / "Dockerfile.expected"
    expected_dockerfile = None
    if expected_path.exists():
        expected_dockerfile = expected_path.read_text()

    return DockerfileFixture(
        metadata=metadata,
        path=fixture_dir,
        dockerfile_content=dockerfile_content,
        source_files=source_files,
        expected_dockerfile=expected_dockerfile,
    )


def get_fixture_ids(fixtures_dir: Path | None = None) -> list[str]:
    """Get all fixture names for pytest parametrization.

    Args:
        fixtures_dir: Root fixtures directory

    Returns:
        List of fixture names
    """
    fixtures = []
    for fixture_dir in discover_fixtures(fixtures_dir):
        try:
            metadata = load_metadata(fixture_dir / "fixture.yaml")
            fixtures.append(metadata.name)
        except Exception:
            continue
    return fixtures


def load_fixture_by_name(name: str, fixtures_dir: Path | None = None) -> DockerfileFixture:
    """Load a fixture by its name.

    Args:
        name: Fixture name
        fixtures_dir: Root fixtures directory

    Returns:
        DockerfileFixture

    Raises:
        ValueError: If fixture not found
    """
    for fixture_dir in discover_fixtures(fixtures_dir):
        try:
            metadata = load_metadata(fixture_dir / "fixture.yaml")
            if metadata.name == name:
                return load_fixture(fixture_dir)
        except Exception:
            continue

    raise ValueError(f"Fixture not found: {name}")


def get_fixtures_by_language(
    language: str,
    fixtures_dir: Path | None = None,
) -> list[DockerfileFixture]:
    """Get all fixtures for a specific language.

    Args:
        language: Programming language
        fixtures_dir: Root fixtures directory

    Returns:
        List of fixtures
    """
    return [
        load_fixture(d)
        for d in discover_fixtures(fixtures_dir, languages=[language])
    ]


def get_fixtures_by_tag(
    tag: str,
    fixtures_dir: Path | None = None,
) -> list[DockerfileFixture]:
    """Get all fixtures with a specific tag.

    Args:
        tag: Tag to filter by
        fixtures_dir: Root fixtures directory

    Returns:
        List of fixtures
    """
    return [
        load_fixture(d)
        for d in discover_fixtures(fixtures_dir, tags=[tag])
    ]
