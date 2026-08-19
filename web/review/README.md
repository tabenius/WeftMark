# WeftMark review surface

This is a dependency-free, read-only browser client for `weftmark.kanban-projection.v0`.
It is intended for tablet/phone review and deliberately does not own or mutate
WeftMark state.

## Live mode

By default the client requests:

```text
GET /v0/kanban
```

from the same origin. For remote/mobile use, serve the static files and proxy
that path to the loopback-only WeftMark HTTP read surface behind an authenticated
TLS boundary. The client does not require or enable permissive cross-origin
access.

A different same-origin/readable projection URL can be supplied for development:

```text
/web/review/?source=/some/read-only/projection.json
```

## Snapshot mode

The **Open projection** button loads an exported projection JSON file entirely in
the browser. A simple local export from the WeftMark HTTP read surface is:

```bash
curl --fail --silent --show-error \
  http://127.0.0.1:8765/v0/kanban \
  -o weftmark-projection.json
```

Then open `weftmark-projection.json` with the file button. This mode needs no
cloud service and no API connection after the file has been selected.

`sample-projection.json` is a committed demonstration fixture covering evidence,
review, and semantic-scope blockers.

## Static development server

For snapshot/demo work, any static server is sufficient. From the repository
root, for example:

```bash
python -m http.server 8080
```

then browse to:

```text
http://127.0.0.1:8080/web/review/
```

The static server alone does not provide live WeftMark data. Either load an
exported JSON file or put `/v0/kanban` behind the same origin via a local proxy.

## Deliberate limits

The review surface has no write operations, Git/ledger access, terminal control,
agent launch controls, merge actions, external JavaScript dependencies, or
proprietary cloud dependency. Cards and lanes are projections of authoritative
WeftMark lifecycle/readiness state; moving visual elements in this client cannot
change a Change Set.
