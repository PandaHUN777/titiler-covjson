# ADR-0007: A Trajectory's times must be true of the data read, so `/trajectory` requires dated data

## Status

Accepted

Amends [ADR-0005](0005-trajectory-temporal-multipoint-non-temporal.md) (where
`/trajectory` gets its times).

## Context

[ADR-0005](0005-trajectory-temporal-multipoint-non-temporal.md) made
`/trajectory` a temporal verb, because every point of a CoverageJSON Trajectory
carries a time `t`. It took those times from the request, as a `LINESTRING M`
measure or a datetime list, and held that this "is honest over a static 2-D
raster" and "needs no temporal dataset backing".

Two facts undo that reasoning:

- With the factory's default `path_dependency` (`DatasetPathParams`), a
  request reads one file, which the client selects with `url=`, and the file
  carries no date. A request time therefore cannot select what is read. It can
  only be copied into the response next to a value read from an undated file,
  and the response then asserts that the data held that value at that time,
  which we cannot know.
- In OGC API - Environmental Data Retrieval (EDR, the standard for how clients
  ask), `datetime` filters the data by time. It never gives each point its own
  time. Per-point times come only from the `M` coordinate of a `LINESTRINGM`,
  which EDR defines as seconds since the Unix epoch (EDR 1.1, OGC 19-086r6,
  Section 8.2.5). ADR-0005's datetime list has no counterpart in EDR.

The EDR conformance test suite expects a trajectory collection to declare the
time span of its data. Its trajectory test (ets-ogcapi-edr10 at `88656db`) reads
the collection's metadata and fails the collection if that metadata has no
temporal extent. Otherwise, it sends a plain `LINESTRING` with a `datetime`
range taken from that extent. EDR defines the temporal extent as covering the
times of the data in the collection (Requirement A.22), so declaring one for an
undated file would assert times the data does not carry.

## Decision

`/trajectory` requires dated data: a backing that gives each file it reads a
date (e.g., SpatioTemporal Asset Catalog (STAC) items, each carrying a
`datetime`). Because each point may need a different file, a request identifies
a set of dated files rather than one file, and each point is read from the file
whose date matches the point's time. The times in the response are then true of
the values next to them. How a request identifies that set (EDR, for example,
puts a collection ID in the path) and how a point's time selects a file belong
to the design of the dated backing, not to this decision.

`/trajectory` follows these rules:

- Per-point times come only from a `LINESTRINGM` (EDR 1.1, Section 8.2.5).
- A plain `LINESTRING` gets a 400 that points to `LINESTRINGM`, or to
  `/position` with a `MULTIPOINT`. Accepting it is deferred (see Alternatives
  considered).
- A `LINESTRINGM` together with `datetime` gets a 400, because EDR requires an
  error when a request supplies the time both ways (Section 8.2.5).
- `LINESTRINGZ`, `LINESTRINGZM`, `z`, and `MULTILINESTRING` get a 400.
  ADR-0001 rejected accepting `z` that no height axis in the data backs, and EDR
  makes `MULTILINESTRING` optional and says an unsupported geometry SHOULD get a
  400.
- Values are returned only at the points the `LINESTRINGM` supplies, never at
  positions between them, because those positions would need times the request
  does not give.

ADR-0005's other decisions stand: `/trajectory` is temporal and belongs to the
Temporal endpoint surface, `/position` with a `MULTIPOINT` is the non-temporal
multi-position query, and Trajectory reuses the composite-tuple modeler.

## Alternatives considered

- **Keep ADR-0005 and copy request times into the response.** Rejected. The
  response would claim the data held each value at each time, and nothing we
  read supports that. ADR-0005 rejected synthesizing a `t` as dishonest, and a
  time the client chose is no truer of an undated file than one we invent.
- **A deployer setting that declares when the data is valid.** Rejected.
  Because the client selects the file with `url=`, one setting would date every
  file a client can request. Dating each file separately is a catalog, which is
  the dated backing this decision requires.
- **Accept a plain `LINESTRING`.** Deferred, not rejected. EDR treats a plain
  line as the basic trajectory request (Requirement A.43), and it would be
  answered as a `MULTIPOINT` with `datetime` is: a MultiPoint with a single `t`
  for one `datetime`, or a MultiPointSeries for a `datetime` range. Refusing it
  and accepting it later widens what we accept without breaking any client, but
  the reverse would break them, so the refusal keeps the choice open.

## Consequences

- ADR-0005's "or a datetime list" and its claim that `/trajectory` "needs no
  temporal dataset backing" are superseded, as is its revisit gate, which
  described a dated backing as an optional superset. A dated backing is a
  prerequisite. ADR-0005 gains an "Amended by ADR-0007" note.
- A deployment whose backing reads only undated files, as the default
  `path_dependency` does, cannot offer `/trajectory`.
- Accepting `M` is specific to `/trajectory`. `/position` and `/area` keep
  refusing it, because EDR never uses it there.
- `corridor` (a buffered trajectory) inherits the requirement for dated data.
