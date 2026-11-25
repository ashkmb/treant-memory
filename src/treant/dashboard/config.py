"""Dashboard settings from the environment. Errors name a variable, never a value."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

MIN_SESSION_SECRET = 32
AUTH_MODES = ("oidc", "none")


def _https_origin(name: str, value: str) -> str:
    """``value`` without a trailing slash; it must be an ``https://`` URL with a host."""
    value = value.rstrip("/")
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname or parts.query or parts.fragment:
        raise ValueError(f"{name} must be an https:// URL")
    return value


def _issuer(value: str) -> str:
    """An OIDC issuer: ``https://`` with a host, no credentials, query or fragment.

    An issuer with a path (Authentik: ``https://auth.<domain>/application/o/<slug>/``) is kept
    exactly as given, trailing slash included: it is compared byte for byte with the discovery
    document's ``issuer`` and the ID token's ``iss``. An origin-only issuer (Authelia) is
    normalised to no trailing slash, as before."""
    name = "TREANT_OIDC_ISSUER"
    parts = urlsplit(value)
    if parts.path in ("", "/"):
        return _https_origin(name, value)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.query
        or parts.fragment
    ):
        raise ValueError(f"{name} must be an https:// URL")
    return value


@dataclass(frozen=True)
class DashboardSettings:
    public_url: str
    session_secret: str = field(repr=False)
    owners: frozenset[str]
    issuer: str = ""
    client_secret: str = field(default="", repr=False)
    # "oidc": login through the provider at ``issuer``. "none": no login at all; every request
    # acts as the single owner. Rely on the network (LAN only) and CSRF in that mode.
    auth: str = "oidc"
    client_id: str = "treant"
    bind: str = "0.0.0.0:8001"
    group: str = "treant"
    reauth_s: int = 300

    def __post_init__(self) -> None:
        # Normalised here (not only in from_env) so direct construction is checked too.
        object.__setattr__(
            self, "public_url", _https_origin("TREANT_DASHBOARD_URL", self.public_url)
        )
        if self.auth not in AUTH_MODES:
            raise ValueError("TREANT_DASHBOARD_AUTH must be oidc or none")
        if self.auth == "oidc":
            if not self.issuer:
                raise ValueError("missing required setting TREANT_OIDC_ISSUER")
            if not self.client_secret:
                raise ValueError("missing required setting TREANT_OIDC_CLIENT_SECRET")
            object.__setattr__(self, "issuer", _issuer(self.issuer))
        else:
            object.__setattr__(self, "issuer", "")
            object.__setattr__(self, "client_secret", "")
        if urlsplit(self.public_url).path:
            raise ValueError("TREANT_DASHBOARD_URL must not have a path")
        if len(self.session_secret) < MIN_SESSION_SECRET:
            raise ValueError(
                f"TREANT_DASHBOARD_SESSION_SECRET must be at least {MIN_SESSION_SECRET} characters"
            )
        if not self.owners:
            raise ValueError("TREANT_DASHBOARD_OWNERS must name at least one user")
        if self.auth == "none" and len(self.owners) != 1:
            raise ValueError(
                "TREANT_DASHBOARD_OWNERS must name exactly one user when TREANT_DASHBOARD_AUTH=none"
            )
        if self.reauth_s <= 0:
            raise ValueError("TREANT_DASHBOARD_REAUTH_S must be a positive integer")

    @property
    def host(self) -> str:
        """The host part of ``public_url`` (lower case, no port): the only accepted Host."""
        return (urlsplit(self.public_url).hostname or "").lower()

    @property
    def issuer_origin(self) -> str:
        """``scheme://host[:port]`` of ``issuer`` (lower case), or "" without an issuer."""
        if not self.issuer:
            return ""
        parts = urlsplit(self.issuer)
        return f"{parts.scheme}://{parts.netloc}".lower()

    @property
    def owner(self) -> str:
        """The owner every request acts as when ``auth`` is ``none``."""
        return min(self.owners)

    @property
    def redirect_uri(self) -> str:
        return self.public_url + "/auth/callback"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "DashboardSettings":
        def need(name: str) -> str:
            value = env.get(name)
            if not value:
                raise ValueError(f"missing required setting {name}")
            return value

        try:
            reauth_s = int(env.get("TREANT_DASHBOARD_REAUTH_S", "300"))
        except ValueError:
            raise ValueError("TREANT_DASHBOARD_REAUTH_S must be a positive integer") from None
        auth = env.get("TREANT_DASHBOARD_AUTH") or "oidc"
        oidc = auth == "oidc"
        return cls(
            public_url=need("TREANT_DASHBOARD_URL"),
            auth=auth,
            issuer=need("TREANT_OIDC_ISSUER") if oidc else "",
            client_secret=need("TREANT_OIDC_CLIENT_SECRET") if oidc else "",
            session_secret=need("TREANT_DASHBOARD_SESSION_SECRET"),
            owners=frozenset(
                u.strip() for u in need("TREANT_DASHBOARD_OWNERS").split(",") if u.strip()
            ),
            client_id=env.get("TREANT_OIDC_CLIENT_ID") or "treant",
            bind=env.get("TREANT_DASHBOARD_BIND") or "0.0.0.0:8001",
            group=env.get("TREANT_DASHBOARD_GROUP") or "treant",
            reauth_s=reauth_s,
        )
