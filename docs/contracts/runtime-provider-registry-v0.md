# Runtime provider registry v0

The registry maps an operator-chosen provider name to a launch argument vector
and declared capabilities. It is pure configuration and does not import an
adapter, discover the network, load plugins, or choose a default provider.

JSON configuration uses a top-level `providers` object. Provider entries require
`argv` and may include `capabilities`; unknown metadata is accepted for forward
compatibility. Repeated CLI flags override same-name file entries explicitly.

The compact flag form accepts `name=argv0:argv1:cap=read,edit`. A JSON-array form,
such as `name=["python","-m","agent:acp"]`, is also accepted when an argument
contains a colon. WeftMark emits and launches argument vectors directly and
never passes them through a shell.

Names, arguments, capabilities, file size, JSON structure, and duplicate keys
are validated. Unknown providers, malformed configuration, empty arguments,
NUL, and ambiguous capability placement fail closed.

Each validated configuration exposes a deterministic SHA-256 fingerprint over
its name, argv, and sorted capabilities. Runtime-session receipts store that
digest, never the raw argv, and require it to match on later status, input, and
stop operations. Reusing a provider name therefore does not silently retarget a
running worker to a different executable or capability declaration.
