from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class LTspicePolicy:
    exe: str
    sim_root: Path
    model_roots: tuple[Path, ...] = ()
    timeout_s: float = 300.0
    max_parallel: int = 2
    max_points_returned: int = 2000
    exe_prefix: tuple[str, ...] = ()


@dataclass(frozen=True)
class A365Policy:
    client_id: str
    redirect_uri: str = "http://localhost:51789/callback"
    workspaces: tuple[str, ...] = ()
    cache_ttl_s: int = 3600
    token_store: Path | None = None
    release_defaults: dict | None = None
    api_url: str = "https://api.nexar.com/graphql"
    identity_url: str = "https://identity.nexar.com"
    timeout_s: float = 30.0
