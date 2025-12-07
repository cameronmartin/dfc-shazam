"""Configuration for dfc-shazam."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration from environment variables."""

    model_config = SettingsConfigDict(env_prefix="DFC_SHAZAM_")

    # APK index caching
    apk_cache_ttl_seconds: int = 3600  # 1 hour

    # chainctl timeout
    chainctl_timeout_seconds: int = 30

    @property
    def chainguard_org(self) -> str:
        """Get the selected Chainguard organization.

        Raises OrgNotSelectedError if no org has been selected yet.
        """
        org = OrgSession.get_org()
        if org is None:
            raise OrgNotSelectedError(
                "No Chainguard organization selected. Call the lookup_chainguard_image "
                "tool first - it will prompt you to select an organization."
            )
        return org


settings = Settings()


class OrgNotSelectedError(Exception):
    """Raised when an operation requires an org but none has been selected."""

    pass


PUBLIC_REGISTRY = "chainguard"  # Public registry org name


class OrgSession:
    """Session state for the selected Chainguard organization.

    The org is set dynamically by prompting the user to choose from
    their available organizations (retrieved from chainctl auth status).
    """

    _selected_org: str | None = None
    _available_orgs: list[str] | None = None

    @classmethod
    def get_org(cls) -> str | None:
        """Get the selected organization, or None if not set."""
        return cls._selected_org

    @classmethod
    def set_org(cls, org: str) -> None:
        """Set the selected organization."""
        cls._selected_org = org

    @classmethod
    def get_available_orgs(cls) -> list[str] | None:
        """Get cached list of available organizations."""
        return cls._available_orgs

    @classmethod
    def set_available_orgs(cls, orgs: list[str]) -> None:
        """Cache the list of available organizations."""
        cls._available_orgs = orgs

    @classmethod
    def is_org_selected(cls) -> bool:
        """Check if an organization has been selected."""
        return cls._selected_org is not None

    @classmethod
    def is_public_registry(cls) -> bool:
        """Check if using the public registry (no org authenticated)."""
        return cls._selected_org == PUBLIC_REGISTRY

    @classmethod
    def clear(cls) -> None:
        """Clear the session state."""
        cls._selected_org = None
        cls._available_orgs = None
