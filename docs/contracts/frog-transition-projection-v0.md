# Frog transition projection v0

`weftmark.frog-transition-projection.v0` is a read-only board projection over
one verified, immutable Frog snapshot receipt. It exists to make Frog backlog
intent visible while WeftMark becomes the coordination authority.

The projection exposes deterministic task lanes, hard dependencies, semantic
conflicts, advisory eligibility, source assignment and lock observations, and
snapshot age. It never converts source assignments or locks into WeftMark
claims. Promotion and claim acquisition remain explicit native WeftMark
actions.

## Lane mapping

- `idea`, `todo`, `planned`, and `backlog` map to `backlog`.
- active spellings such as `in_progress`, `doing`, and `wip` map to `active`.
- `review`, `blocked`, and unknown statuses map to `review`; blocked and unknown
  values carry explicit attention markers.
- terminal statuses map to `done`.

Unknown status values fail safe into review rather than appearing complete.
Cards are sorted by lane, priority, source creation value, and stable task slug.

## Authority and freshness

The payload names the source snapshot digest, capture/import times, projection
time, and staleness threshold. Its `authority` object states that Frog state is
observed intent only. A stale projection remains inspectable but must not be
mistaken for a current coordination decision.

Only locks with an exact `task:<slug>` source scope are attached to a card.
They and source assignments are observations; neither gates or grants local
work. Dependency/conflict eligibility is advisory and uses the existing Frog
planning contract.

Malformed optional assignment and lock observations are ignored and counted in
the payload rather than making otherwise valid task intent unavailable. Unknown
workflow statuses fail closed: they appear in review with an attention marker
and are never advertised as eligible.
