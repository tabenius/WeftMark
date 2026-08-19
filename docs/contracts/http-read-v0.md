# HTTP read surface contract v0

The first WeftMark HTTP surface exists only to make the Kanban projection consumable by a separately deployed board/PWA client. It is not a general remote-control API and it does not introduce a second source of truth.

## Authority and side effects

The HTTP process composes the existing local `WorkspaceService`, `ClaimService`, `LocalWorkflowService`, and `StatusService`, then projects that status through `weftmark.kanban-projection.v0`.

A GET request must not refresh Git state, change a Change Set, acquire/release a claim, record evidence, create a review/handoff, or write the ledger.

## Endpoints

```text
GET /healthz
GET /v0/kanban
GET /v0/kanban/changes/{change_set_id}
```

All mutation methods on the v0 surface return `405 Method Not Allowed` with `Allow: GET`.

Projection responses use:

```text
Content-Type: application/json; charset=utf-8
Cache-Control: no-store
X-Content-Type-Options: nosniff
```

`/healthz` reveals only service/schema identity and is intentionally available without bearer authentication.

## Binding policy

**V0 is loopback-only.** The server accepts `localhost`, `127.0.0.0/8`, or `::1` and refuses non-loopback binds including `0.0.0.0` and `::`.

Default:

```bash
python -m weftmark.http.server --repo .
```

which listens on:

```text
127.0.0.1:8765
```

This refusal is deliberate. A bearer token over plaintext HTTP is not a sufficient remote security boundary.

For a phone/tablet or another machine, put a secure transport in front of the loopback service, for example:

- an authenticated TLS reverse proxy;
- Tailscale Serve or an equivalent authenticated private-network proxy;
- an SSH port forward.

A Kanban/PWA integration should normally proxy WeftMark under the same origin rather than enabling permissive CORS on this server.

## Optional local bearer token

`--token-file PATH` enables bearer authentication for projection endpoints. The file must contain a non-empty token. Tokens are read at process startup and are not written to the ledger.

Example:

```bash
python -m weftmark.http.server \
  --repo /srv/project \
  --token-file /run/secrets/weftmark-http-token
```

Clients then send:

```text
Authorization: Bearer <token>
```

This is defense in depth for the local hop; remote confidentiality/authentication remains the responsibility of the secure proxy/tunnel.

## Deliberate omissions

V0 does not provide:

- CORS configuration;
- TLS termination;
- user accounts or RBAC;
- terminal or diff streaming;
- claim/start/stop/review/handoff mutations;
- Git refresh or repository scanning on request;
- merge/release actions.

Those capabilities require separate contracts and security review. In particular, adding a write endpoint must go through WeftMark application services and must not grant a board client direct ledger or Git authority.
