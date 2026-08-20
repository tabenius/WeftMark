"""Read-only Gitea adapter for the provider-neutral ForgePort."""

from __future__ import annotations

from weftmark.adapters.gitea_like import (
    GiteaLikeAdapterError,
    GiteaLikeForgeAdapter,
    GiteaLikeHttpResponse,
    GiteaLikeTransport,
    GiteaLikeTransportError,
)


GiteaAdapterError = GiteaLikeAdapterError
GiteaHttpResponse = GiteaLikeHttpResponse
GiteaTransportError = GiteaLikeTransportError


class GiteaForgeAdapter(GiteaLikeForgeAdapter):
    provider = "gitea"

    def __init__(
        self,
        repository: str,
        *,
        token: str | None = None,
        api_base: str = "https://gitea.com/api/v1",
        web_base: str = "https://gitea.com",
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
