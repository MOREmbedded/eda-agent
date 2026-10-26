# SPDX-License-Identifier: Apache-2.0
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
    """Where the Altium 365 data comes from, and how it is authorised.

    TWO ROUTES, and they are not interchangeable.

    NEXAR (the default) is Altium's developer platform: supply-chain data
    plus a proxy over the design domain of an Altium 365 workspace. It
    authorises through an OAuth client with PKCE against
    ``identity.nexar.com`` and refreshes its own token.

    DIRECT is the workspace's own API, documented at
    altium.com/documentation/altium-developer-center/altium-365/api. Set
    ``workspace_domain`` and it posts the same GraphQL to
    ``https://{domain}/api/graphql/`` instead. Its token is a long-lived
    one created in the workspace under Admin > Developer (up to a year),
    passed as ``direct_token``; there is no refresh to do, so none is
    attempted. The schema is the same ``des*`` one Nexar proxies, which
    is why one client serves both, but the direct route also carries the
    contexts Nexar does not proxy: comments, tasks, workflows and script
    executions.

    Setting ``workspace_domain`` wins over ``api_url``.
    """

    client_id: str
    redirect_uri: str = "http://localhost:51789/callback"
    workspaces: tuple[str, ...] = ()
    cache_ttl_s: int = 3600
    token_store: Path | None = None
    release_defaults: dict | None = None
    api_url: str = "https://api.nexar.com/graphql"
    identity_url: str = "https://identity.nexar.com"
    timeout_s: float = 30.0
    #: Workspace host, e.g. "mycompany.365.altium.com". Empty = use Nexar.
    workspace_domain: str = ""
    #: Long-lived token from Admin > Developer, for the direct route only.
    direct_token: str = ""

    @property
    def is_direct(self) -> bool:
        """True when this talks to the workspace rather than to Nexar."""
        return bool(self.workspace_domain)

    @property
    def endpoint(self) -> str:
        """The GraphQL URL this policy posts to.

        The trailing slash is the documented spelling for the direct
        route and some gateways redirect without it, which would drop the
        Authorization header on the hop.
        """
        if not self.workspace_domain:
            return self.api_url
        host = self.workspace_domain.strip().rstrip("/")
        host = host.split("://", 1)[1] if "://" in host else host
        return f"https://{host}/api/graphql/"

    @property
    def files_endpoint(self) -> str:
        """The Files Service, for uploads the GraphQL layer refers to."""
        if not self.workspace_domain:
            return "https://files.nexar.com/File/Upload"
        host = self.workspace_domain.strip().rstrip("/")
        host = host.split("://", 1)[1] if "://" in host else host
        return f"https://{host}/api/files"
