from collections.abc import Mapping
from dataclasses import dataclass

from treant import names

JEV_URL = "https://api.typesafe.ai"
JEV_MODEL = "jev-1.13.0"


def jev_endpoint(env: Mapping[str, str]) -> tuple[str, str]:
    """(url, model) of the Jev judge: ``TREANT_JEV_URL`` / ``TREANT_JEV_MODEL`` or the defaults."""
    return env.get("TREANT_JEV_URL") or JEV_URL, env.get("TREANT_JEV_MODEL") or JEV_MODEL


@dataclass(frozen=True)
class Settings:
    database_url: str
    admin_database_url: str
    embed_url: str
    role_prefix: str = "treant"
    embed_model: str = "embeddinggemma-300m"
    embed_dims: int = 768
    bind: str = "127.0.0.1:8000"
    allowed_hosts: tuple[str, ...] = ()
    litellm_url: str | None = None
    jev_url: str = JEV_URL
    jev_model: str = JEV_MODEL

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        def need(name: str) -> str:
            value = env.get(name)
            if not value:
                raise ValueError(f"missing required setting {name}")
            return value

        try:
            dims = int(env.get("TREANT_EMBED_DIMS", "768"))
        except ValueError:
            raise ValueError("TREANT_EMBED_DIMS must be an integer") from None
        prefix = env.get("TREANT_ROLE_PREFIX", "treant")
        if not names.valid_prefix(prefix):
            raise ValueError("TREANT_ROLE_PREFIX must match [a-z][a-z0-9_]{0,23}")
        return cls(
            database_url=need("TREANT_DATABASE_URL"),
            admin_database_url=need("TREANT_ADMIN_DATABASE_URL"),
            embed_url=need("TREANT_EMBED_URL"),
            role_prefix=prefix,
            embed_model=env.get("TREANT_EMBED_MODEL", "embeddinggemma-300m"),
            embed_dims=dims,
            bind=env.get("TREANT_BIND", "127.0.0.1:8000"),
            allowed_hosts=tuple(
                h.strip() for h in env.get("TREANT_ALLOWED_HOSTS", "").split(",") if h.strip()
            ),
            litellm_url=env.get("TREANT_LITELLM_URL") or None,
            jev_url=jev_endpoint(env)[0],
            jev_model=jev_endpoint(env)[1],
        )
