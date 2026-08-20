"""Read-only Forgejo/Codeberg adapter for the provider-neutral ForgePort."""

from __future__ import annotations

from weftmark.adapters.gitea_like import (
    GiteaLikeAdapterError,
    GiteaLikeForgeAdapter,
    GiteaLikeHttpResponse,
    GiteaLikeTransport,
    GiteaLikeTransportError,
)


ForgejoAdapterError = GiteaLikeAdapterError
ForgejoHttpResponse = GiteaLikeHttpResponse
ForgejoTransportError = GiteaLikeTransportError


class ForgejoForgeAdapter(GiteaLikeForgeAdapter):
    """Forgejo dialect; kept separate so Forgejo/Codeberg can diverge safely."""

    provider = "forgejo"

    def __init__(
        self,
        repository: str,
        *,
        token: str | None = None,
        api_base: str = "https://codeberg.org/api/v1",
        web_base: str = "https://codeberg.org",
        actions_supported: bool = True,
        transport: GiteaLikeTransport | None = None,
    ) -> None:
        super().__init__(
            repository,
            token=token,
            api_base=api_base,
            web_base=web_base,
            actions_supported=actions_supported,
            transport=transport,
        )
