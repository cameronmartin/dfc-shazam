"""Tests for fixture loader."""

import pytest
from pathlib import Path

from tests.harness.loader import (
    FIXTURES_DIR,
    discover_fixtures,
    get_fixture_ids,
    load_fixture,
    load_fixture_by_name,
    load_metadata,
)
from tests.harness.models import Complexity


class TestDiscoverFixtures:
    """Tests for fixture discovery."""

    def test_discover_all_fixtures(self):
        """Test that we can discover all fixtures."""
        fixtures = list(discover_fixtures())
        # We created 20 fixtures
        assert len(fixtures) >= 20

    def test_discover_by_language(self):
        """Test filtering by language."""
        fixtures = list(discover_fixtures(languages=["python"]))
        assert len(fixtures) >= 4  # We have 4 Python fixtures
        for fixture_dir in fixtures:
            metadata = load_metadata(fixture_dir / "fixture.yaml")
            assert metadata.language == "python"

    def test_discover_by_tag(self):
        """Test filtering by tag."""
        fixtures = list(discover_fixtures(tags=["multistage"]))
        assert len(fixtures) >= 1
        for fixture_dir in fixtures:
            metadata = load_metadata(fixture_dir / "fixture.yaml")
            assert "multistage" in metadata.tags

    def test_discover_by_complexity(self):
        """Test filtering by complexity."""
        fixtures = list(discover_fixtures(complexity="simple"))
        assert len(fixtures) >= 1
        for fixture_dir in fixtures:
            metadata = load_metadata(fixture_dir / "fixture.yaml")
            assert metadata.complexity == Complexity.SIMPLE


class TestLoadMetadata:
    """Tests for metadata loading."""

    def test_load_flask_metadata(self):
        """Test loading Flask fixture metadata."""
        fixture_dir = FIXTURES_DIR / "python" / "01_flask_basic"
        if not fixture_dir.exists():
            pytest.skip("Fixture not found")

        metadata = load_metadata(fixture_dir / "fixture.yaml")

        assert metadata.name == "python_flask_basic"
        assert metadata.language == "python"
        assert metadata.complexity == Complexity.SIMPLE
        assert "flask" in metadata.tags
        assert metadata.source.base_image == "python:3.11-slim"
        assert metadata.source.package_manager == "apt"

    def test_load_multistage_metadata(self):
        """Test loading multi-stage fixture metadata."""
        fixture_dir = FIXTURES_DIR / "go" / "09_multistage_static"
        if not fixture_dir.exists():
            pytest.skip("Fixture not found")

        metadata = load_metadata(fixture_dir / "fixture.yaml")

        assert metadata.name == "go_multistage_static"
        assert "multistage" in metadata.tags
        assert metadata.multistage.stages == 2


class TestLoadFixture:
    """Tests for fixture loading."""

    def test_load_flask_fixture(self):
        """Test loading complete Flask fixture."""
        fixture_dir = FIXTURES_DIR / "python" / "01_flask_basic"
        if not fixture_dir.exists():
            pytest.skip("Fixture not found")

        fixture = load_fixture(fixture_dir)

        assert fixture.metadata.name == "python_flask_basic"
        assert "FROM python:3.11-slim" in fixture.dockerfile_content
        assert "app.py" in fixture.source_files
        assert "requirements.txt" in fixture.source_files


class TestLoadFixtureByName:
    """Tests for loading fixtures by name."""

    def test_load_by_name(self):
        """Test loading fixture by name."""
        try:
            fixture = load_fixture_by_name("python_flask_basic")
            assert fixture.metadata.name == "python_flask_basic"
        except ValueError:
            pytest.skip("Fixture not found")

    def test_load_nonexistent_fixture(self):
        """Test loading non-existent fixture raises error."""
        with pytest.raises(ValueError, match="Fixture not found"):
            load_fixture_by_name("nonexistent_fixture")


class TestGetFixtureIds:
    """Tests for fixture ID retrieval."""

    def test_get_all_fixture_ids(self):
        """Test getting all fixture IDs."""
        ids = get_fixture_ids()
        assert len(ids) >= 20
        assert "python_flask_basic" in ids
        assert "go_multistage_static" in ids
