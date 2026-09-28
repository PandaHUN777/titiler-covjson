"""Parse the Well-Known Text (WKT) a request names its geometry with.

Hand-rolled and deliberately dependency-free. The grammars accepted here are
small, and a geometry library backed by GEOS would be a heavy dependency to add
for two floats and a ring list; confining WKT handling to this module keeps that
a contained decision, since only these function bodies would change if one ever
became worthwhile.

What is accepted is narrower than WKT itself. These parsers refuse well-formed
input the grammar allows whenever accepting it would promise something this
service cannot deliver: a 3-D or measured geometry parses as WKT but names a
vertical level no single 2-D raster can sample. Refusals of both kinds report the
same way, so a caller does not have to distinguish "not WKT" from "not something
we serve".

Each parser hands its coordinates to a geometry type, which owns the invariants
of the value itself (finiteness, ring closure). This module owns only the
grammar.

A parser returns :class:`InvalidCoords` rather than raising, because unusable
``coords`` is an ordinary outcome of serving a request, not an exceptional one:
rejecting it is the endpoint's job, so the parser is total and every input maps
to a value. That keeps this module free of the web framework, testable without
one, and makes handling a rejection the caller's obligation by type rather than
by memory. The caller turns it into whatever its own boundary requires.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from titiler_covjson.geometry import MultiPoint, Polygon, Position


def _geometry_wkt(keyword: str, body: str) -> re.Pattern[str]:
    r"""Compile the WKT pattern for one geometry keyword and its body grammar.

    Args:
        keyword: The WKT geometry keyword, e.g., ``POINT``.
        body: The pattern for the text between the outermost parentheses. It
            must be unambiguous with the ``\)`` that follows it: a lazy
            quantifier, or one flanked by ``\s*``, lets the engine try every
            split of the body before failing, which is superlinear in its
            length. The body arrives from a public query string, so that is a
            denial of service rather than a slow parse.

    Returns:
        re.Pattern[str]: The compiled pattern, with named groups ``tag`` and
            ``body``.

    Examples:
        >>> match = _geometry_wkt("POINT", r"[^()]*").match("POINT Z (0 0 5)")
        >>> match["tag"], match["body"]
        ('Z', '0 0 5')

        A geometry whose keyword merely ends in this one is not a match, so
        MULTIPOINT is not a POINT:

        >>> print(_geometry_wkt("POINT", r"[^()]*").match("MULTIPOINT(0 0)"))
        None
    """
    return re.compile(
        rf"^\s*{keyword}\s*(?:(?P<tag>ZM|Z|M)\s*)?\((?P<body>{body})\)\s*$",
        re.IGNORECASE | re.DOTALL,
    )


# A point's body holds no parentheses of its own, so `[^()]*` both bounds it and
# rejects `POINT((0 0))`. parse_point_wkt inspects the tag and coordinate count
# to reject 3-D/measured geometries; the comma (the MULTIPOINT coordinate
# separator) is deliberately not allowed inside a single point.
_POINT_WKT = _geometry_wkt("POINT", r"[^()]*")

# A polygon's body is its parenthesized ring list `((x y, ...), (x y, ...))`, so
# it takes the parentheses a point's body forbids. parse_polygon_wkt splits it
# with _POLYGON_RING (each parenthesized group is one ring); MULTIPOLYGON fails
# this pattern (the leading `MULTI`), keeping a single polygon.
_POLYGON_WKT = _geometry_wkt("POLYGON", r".*")

# One ring, and the comma-separated list of rings a polygon body must be: findall
# extracts the rings, fullmatch says the list holding them is well-formed. The
# `_SRC` suffix marks raw pattern text, since only text can be interpolated into
# another pattern. The list repeats that text and so repeats its group, which is
# harmless: the list is only ever read for whether it matched, never for groups.
_RING_SRC = r"\(([^()]*)\)"
_POLYGON_RING = re.compile(_RING_SRC)
_POLYGON_RING_LIST = re.compile(rf"\s*{_RING_SRC}(?:\s*,\s*{_RING_SRC})*\s*")

# WKT for a multipoint: `MULTIPOINT`, an optional Z/M/ZM tag, then the point list
# in parentheses. parse_multipoint_wkt inspects the tag and reads each point's
# `x y` in either form, parenthesized or bare. POINT fails this pattern (no
# leading `MULTI`) and MULTIPOINT fails _POINT_WKT (anchored on `POINT`), so the
# two are disjoint. `MULTIPOINT EMPTY` fails it too (no parentheses), which is
# the rejection.
_MULTIPOINT_WKT = _geometry_wkt("MULTIPOINT", r".*")

# One point of a multipoint's list, one pattern per form: parenthesized, with
# its coordinate text captured, or bare. Each matches a single point and not the
# list, because a pattern spanning the list would let a bare point's `[^(),]*` and
# the separator's own whitespace consume one run of spaces two different ways, so
# rejecting a long malformed list would walk every split (about a minute for a
# 4 KB value). Splitting on WKT's separator first and matching each point alone
# keeps the work linear, which matters because `coords` arrives straight off a
# public query string.
#
# An empty parenthesized point `()` matches on purpose, so it reaches
# _parse_xy_pair and is reported as the empty vertex it is. A bare point is never
# empty, so a stray comma fails as structure instead. `MULTIPOINT()` holds no
# points at all and reaches MultiPoint's own "at least one position" rule, because
# parse_multipoint_wkt does not split an empty body. One pattern requires `(` and
# the other forbids it, so no point matches both.
_MULTIPOINT_PARENTHESIZED = re.compile(r"\((?P<xy>[^(),]*)\)")
_MULTIPOINT_BARE = re.compile(r"[^(),]+")

# A coordinate token, checked before `float` reads it: `float` also accepts PEP
# 515 underscores (`1_000`) and any Unicode decimal digit (`١٢`), silently
# yielding a coordinate the requester never wrote -- hence `[0-9]`, not `\d`.
# Matching is ASCII-only, because Unicode case folding reads `ı` (dotless i) as
# `i`, so `ınf` would pass here and then make `float` raise. The non-finite
# spellings are admitted deliberately, so finiteness keeps its single home in the
# geometry types.
#
# Only a dot admits the digits after one, so a run of digits splits exactly one
# way. Writing the pattern as `[0-9]+\.?[0-9]*` instead lets the run divide at
# every position, which takes seconds to reject a long token that is not a
# number.
_COORDINATE_TOKEN = re.compile(
    r"""
      [+-]? (?: [0-9]+ (?: \. [0-9]* )? | \. [0-9]+ )   # 1, 1., 1.5, .5
            (?: [eE] [+-]? [0-9]+ )?                    # optional exponent
    | [+-]? (?: nan | inf (?: inity )? )                # non-finite spellings
    """,
    re.ASCII | re.IGNORECASE | re.VERBOSE,
)

# How many numbers a coordinate holds when it carries a vertical or measured
# value without its Z / M / ZM tag: `x y z` or `x y m` is three, `x y z m` is
# four. A lone point and a vertex in a list read these counts alike.
_VERTICAL_OR_MEASURED_TOKEN_COUNTS = frozenset({3, 4})


@dataclass(frozen=True)
class InvalidCoords:
    """A ``coords`` value that could not be used, and why.

    Returned by every parser here in place of a parsed geometry. The name is
    deliberately not ``InvalidWkt``: most refusals are of *well-formed* WKT
    (a 3-D ``POINT Z``, a geometry the endpoint does not accept), so the input
    is not necessarily invalid WKT, it is merely unusable as ``coords``.

    Attributes:
        message: Why the value was refused, phrased for the requester and
            quoting what they sent.
    """

    message: str


def parse_point_wkt(coords: str) -> Position | InvalidCoords:
    """Parse a 2-D WKT ``POINT(x y)`` into a :class:`Position`.

    Accepts a plain 2-D ``POINT(x y)`` with whitespace-separated coordinates.
    Everything else yields an :class:`InvalidCoords`:

    - a 3-D or measured geometry (a ``Z`` / ``M`` / ``ZM`` tag, or three or four
      numeric coordinates): the 2-D raster backing cannot sample a vertical
      level, so echoing the coordinate back or dropping it would both be
      dishonest
    - a non-POINT geometry, ``POINT EMPTY``, the wrong coordinate count, or any
      other malformed input
    - a coordinate that is not a number, including a comma-separated
      ``POINT(1, 2)`` (the comma is the ``MULTIPOINT`` separator, not an
      intra-point one, so it leaves the unparseable token ``1,``)
    - a non-finite coordinate (NaN or infinity), which would otherwise serialize
      to a silent ``null`` domain axis

    Args:
        coords: The raw ``coords`` query value.

    Returns:
        Position | InvalidCoords: The parsed 2-D position, or why it was refused.

    Examples:
        >>> parse_point_wkt("POINT(0 0)")
        Position(x=0.0, y=0.0, z=None)
        >>> parse_point_wkt("  point ( -5.0   2.5 ) ")
        Position(x=-5.0, y=2.5, z=None)

        A 3-D point is refused (vertical selection is unsupported here):

        >>> parse_point_wkt("POINT Z (0 0 5)")
        InvalidCoords(message="Vertical or measured coordinates are not ...")

        So is a malformed one:

        >>> parse_point_wkt("POINT(0)")
        InvalidCoords(message="Invalid position 'POINT(0)': expected two ...")
    """
    if (match := _POINT_WKT.match(coords)) is None:
        return InvalidCoords(
            f"Invalid position {coords!r}: expected WKT POINT(x y), e.g., POINT(0 0)."
        )

    tokens = match["body"].split()

    # One refusal reached by two routes, so it has one wording; the guards stay
    # apart only because the tag reads before the coordinates and the count after.
    if match["tag"]:
        return _vertical_point_refusal(coords)

    # Check the tokens before counting them, so the count guard below speaks only
    # for tokens that are coordinates. Counting first would call `POINT(1 , 2)`
    # vertical, since a spaced comma splits into three tokens without any of them
    # naming a level.
    if not all(map(_COORDINATE_TOKEN.fullmatch, tokens)):
        return InvalidCoords(
            f"Invalid position {coords!r}: coordinates must be numbers, "
            "e.g., POINT(0 0)."
        )

    if len(tokens) in _VERTICAL_OR_MEASURED_TOKEN_COUNTS:
        return _vertical_point_refusal(coords)

    if len(tokens) != 2:
        return InvalidCoords(
            f"Invalid position {coords!r}: expected two coordinates, e.g., POINT(0 0)."
        )

    x, y = (float(token) for token in tokens)

    # Position owns the finiteness invariant, so its message carries through
    # verbatim: a rule added there then reports itself instead of being
    # mislabeled as a finiteness failure. The appended example names no rule,
    # for the same reason.
    try:
        return Position(x, y)
    except ValueError as exc:
        return InvalidCoords(
            f"Invalid position {coords!r}: {exc} A usable position looks like "
            "POINT(0 0)."
        )


def parse_polygon_wkt(coords: str) -> Polygon | InvalidCoords:
    """Parse a 2-D WKT ``POLYGON((x y, ...), ...)`` into a :class:`Polygon`.

    Splits the parenthesized ring list and reads each vertex as two floats,
    handing the rings to :class:`Polygon`, which owns the ring invariants (closed,
    at least four vertices, finite coordinates). Accepts a single 2-D ``POLYGON``
    with one exterior ring and zero or more interior rings (holes). Everything
    else yields an :class:`InvalidCoords`:

    - a 3-D or measured geometry (a ``Z`` / ``M`` / ``ZM`` tag, or a vertex with
      three or four numeric coordinates): the 2-D raster backing has no vertical
      level to reduce over, so echoing the coordinate back or dropping it would
      both be dishonest
    - a non-POLYGON geometry (including ``MULTIPOLYGON``, whose ``MULTI`` prefix
      fails the pattern), ``POLYGON EMPTY``, an empty ring, a non-finite or
      non-numeric coordinate, an unclosed ring, a ring with fewer than four
      vertices, or any other malformed input

    Args:
        coords: The raw ``coords`` query value.

    Returns:
        Polygon | InvalidCoords: The parsed 2-D polygon, or why it was refused.

    Examples:
        >>> parse_polygon_wkt("POLYGON((0 0, 1 0, 1 1, 0 0))").rings
        (((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 0.0)),)

        A 3-D polygon is refused (vertical selection is unsupported here):

        >>> parse_polygon_wkt("POLYGON Z ((0 0 1, 1 0 1, 1 1 1, 0 0 1))")
        InvalidCoords(message="Vertical or measured coordinates are not ...")

        So is an unclosed ring, which :class:`Polygon` itself catches:

        >>> parse_polygon_wkt("POLYGON((0 0, 1 0, 1 1, 0 1))")
        InvalidCoords(message="Invalid polygon 'POLYGON((0 0, 1 0, 1 1, ...")
    """
    if (match := _POLYGON_WKT.match(coords)) is None:
        return InvalidCoords(
            f"Invalid polygon {coords!r}: expected WKT POLYGON((x y, x y, ...)), "
            "e.g., POLYGON((0 0, 1 0, 1 1, 0 0))."
        )

    # Rejected for the reason given at the same guard in parse_point_wkt.
    if match["tag"]:
        return InvalidCoords(
            "Vertical or measured coordinates are not supported: this endpoint "
            f"reduces a single 2-D raster. Provide a 2-D POLYGON; got {coords!r}."
        )

    if not (ring_strings := _POLYGON_RING.findall(match["body"])):
        return InvalidCoords(
            f"Invalid polygon {coords!r}: expected at least one parenthesized ring, "
            "e.g., POLYGON((0 0, 1 0, 1 1, 0 0))."
        )

    # findall keeps only the parenthesized groups, so anything between or after
    # them would otherwise be dropped in silence and a `POLYGON((...) JUNK (...))`
    # would read as a well-formed two-ring polygon.
    if _POLYGON_RING_LIST.fullmatch(match["body"]) is None:
        return InvalidCoords(
            f"Invalid polygon {coords!r}: malformed ring list; expected "
            "parenthesized rings separated by single commas, e.g., "
            "POLYGON((0 0, 4 0, 4 4, 0 0), (1 1, 2 1, 2 2, 1 1))."
        )

    # Ring by ring, so a vertex fault names the ring it came from; Polygon's own
    # faults already carry that index, and its messages carry through verbatim.
    rings: list[tuple[tuple[float, float], ...]] = []

    for ring_index, ring_string in enumerate(ring_strings):
        try:
            # A blank ring is no vertices, not one blank vertex. `"".split(",")`
            # yields one blank entry, which would report the empty ring as a bad
            # vertex instead of letting Polygon report a ring of zero vertices.
            rings.append(
                tuple(map(_parse_xy_pair, ring_string.split(",")))
                if ring_string.strip()
                else ()
            )
        except ValueError as exc:
            return InvalidCoords(
                f"Invalid polygon {coords!r}: in ring {ring_index}, {exc}"
            )

    try:
        return Polygon(rings=tuple(rings))
    except ValueError as exc:
        return InvalidCoords(f"Invalid polygon {coords!r}: {exc}")


def parse_multipoint_wkt(coords: str) -> MultiPoint | InvalidCoords:
    """Parse a 2-D WKT ``MULTIPOINT`` into a :class:`MultiPoint`.

    Accepts ``MULTIPOINT((x y), (x y), ...)``, the only form OGC 06-103r4
    Section 7.2.2 admits and the one GEOS and PostGIS write, and also the bare
    ``MULTIPOINT(x y, x y, ...)``, which those readers accept though they do not
    write it. One list may not mix the two. Each point is read as one ``x y``
    pair in either form, and the pairs go to :class:`MultiPoint`, which owns the
    set invariants (at least one position, finite, distinct). Everything else
    yields an :class:`InvalidCoords`:

    - a 3-D or measured geometry (a ``Z`` / ``M`` / ``ZM`` tag, or a point with
      three or four numeric coordinates): the 2-D raster backing cannot sample a
      vertical level, so echoing the coordinate back or dropping it would both be
      dishonest
    - a non-MULTIPOINT geometry, ``MULTIPOINT EMPTY``, an empty point list, an
      empty point, a list mixing the parenthesized and bare forms, a
      non-finite or non-numeric coordinate, a repeated position, or any other
      malformed input

    Args:
        coords: The raw ``coords`` query value.

    Returns:
        MultiPoint | InvalidCoords: The parsed positions, or why they were refused.

    Examples:
        >>> parse_multipoint_wkt("MULTIPOINT((0 0), (1 1))").positions
        ((0.0, 0.0), (1.0, 1.0))
        >>> parse_multipoint_wkt("MULTIPOINT(0 0, 1 1)").positions
        ((0.0, 0.0), (1.0, 1.0))

        A repeated position is refused, which :class:`MultiPoint` itself catches:

        >>> parse_multipoint_wkt("MULTIPOINT((0 0), (0 0))")
        InvalidCoords(message="Invalid multipoint 'MULTIPOINT((0 0), (0 0))': ...")
    """
    if (match := _MULTIPOINT_WKT.match(coords)) is None:
        return InvalidCoords(
            f"Invalid multipoint {coords!r}: expected WKT MULTIPOINT((x y), ...), "
            "e.g., MULTIPOINT((0 0), (1 1))."
        )

    # Rejected for the reason given at the same guard in parse_point_wkt.
    if match["tag"]:
        return InvalidCoords(
            "Vertical or measured coordinates are not supported: this endpoint "
            f"samples a single 2-D raster. Provide a 2-D MULTIPOINT; got {coords!r}."
        )

    # An empty body is no points, not one empty point, because splitting it would yield
    # a blank item that reads as malformed and hides MultiPoint's emptiness rule.
    body = match["body"].strip()
    points = [point.strip() for point in body.split(",")] if body else []

    # Sort the points by form, taking a parenthesized point's coordinates from
    # the match that recognized it.
    parenthesized = [
        point_match["xy"]
        for point in points
        if (point_match := _MULTIPOINT_PARENTHESIZED.fullmatch(point))
    ]
    bare = [*filter(_MULTIPOINT_BARE.fullmatch, points)]

    # This checks each point's form, not what it holds. A point is one
    # parenthesized group or one bare run of text, never an empty item, so a
    # stray paren leaves a point in neither list, and so does a stray comma, which
    # leaves an empty item beside a real one. A comma missing between
    # parenthesized points does too, fusing them into one unmatchable item. What a
    # point holds, e.g., `()` or `(1 2 3)`, is _parse_xy_pair's to judge, and so
    # is the `x y x y` that a comma missing between bare points leaves.
    if len(parenthesized) + len(bare) != len(points):
        return InvalidCoords(
            f"Invalid multipoint {coords!r}: malformed point list (check the "
            "parentheses and the commas); expected comma-separated points, "
            "e.g., MULTIPOINT((0 0), (1 1))."
        )

    # As a list, the points must agree on one form. Either is valid on its own,
    # but no WKT grammar permits mixing them and GEOS refuses it.
    if parenthesized and bare:
        return InvalidCoords(
            f"Invalid multipoint {coords!r}: a multipoint must not mix "
            "parenthesized and bare points; use one form throughout, "
            "e.g., MULTIPOINT((0 0), (1 1)) or MULTIPOINT(0 0, 1 1)."
        )

    # `parenthesized or bare` is every point, in request order, only because the
    # mixed-form check has proved one of the two lists empty. Without that check
    # it would silently drop the points of the other form.
    #
    # _parse_xy_pair rejects an empty, non-2-D, or non-numeric point and MultiPoint
    # rejects an empty, non-finite, or duplicate set, so one handler covers every
    # ValueError. MultiPoint owns the set invariants as the single source of
    # truth, and its message carries through verbatim (mirroring parse_point_wkt
    # and Position).
    try:
        return MultiPoint(positions=tuple(map(_parse_xy_pair, parenthesized or bare)))
    except ValueError as exc:
        return InvalidCoords(f"Invalid multipoint {coords!r}: {exc}")


def parse_position_coords(coords: str) -> Position | MultiPoint | InvalidCoords:
    """Parse the ``coords`` of a position request: a ``POINT`` or ``MULTIPOINT``.

    The single entry point the position endpoint dispatches on: a ``POINT`` yields
    a :class:`Position`, a ``MULTIPOINT`` a :class:`MultiPoint`, and anything else
    an :class:`InvalidCoords` naming both accepted forms. A well-formed ``POINT``
    or ``MULTIPOINT`` that is nonetheless unusable (a 3-D tag, a duplicate
    position) surfaces that grammar's own specific message.

    Named for the ``coords`` parameter it reads rather than a single grammar: it
    dispatches over the two geometries the verb accepts, so it is not one more
    ``parse_*_wkt`` sibling.

    Args:
        coords: The raw ``coords`` query value.

    Returns:
        Position | MultiPoint | InvalidCoords: The parsed geometry, or why it was
            refused.

    Examples:
        >>> parse_position_coords("POINT(0 0)")
        Position(x=0.0, y=0.0, z=None)
        >>> parse_position_coords("MULTIPOINT((0 0), (1 1))").positions
        ((0.0, 0.0), (1.0, 1.0))

        A geometry that is neither names both accepted forms:

        >>> parse_position_coords("POLYGON((0 0, 1 0, 1 1, 0 0))")
        InvalidCoords(message="Invalid coords 'POLYGON((0 0, 1 0, 1 1, 0 0))': ...")
    """
    if _MULTIPOINT_WKT.match(coords):
        return parse_multipoint_wkt(coords)

    if _POINT_WKT.match(coords):
        return parse_point_wkt(coords)

    return InvalidCoords(
        f"Invalid coords {coords!r}: expected WKT POINT(x y) or MULTIPOINT((x y), ...)."
    )


def _vertical_point_refusal(coords: str) -> InvalidCoords:
    """Refuse a point that names a vertical or measured coordinate.

    A ``Z`` / ``M`` / ``ZM`` tag and a third or fourth coordinate are one
    decision reached two ways, so they share this wording. A single 2-D raster
    cannot honor a vertical level, and accepting one would promise a selection
    the response does not make.

    Args:
        coords: The raw ``coords`` query value, quoted back to the requester.

    Returns:
        InvalidCoords: The refusal, phrased for the requester.

    Examples:
        >>> print(_vertical_point_refusal("POINT Z (0 0 5)").message)
        Vertical or measured coordinates are not supported: this endpoint samples
        a single 2-D raster. Provide a 2-D POINT(x y); got 'POINT Z (0 0 5)'.
    """
    return InvalidCoords(
        "Vertical or measured coordinates are not supported: this endpoint "
        f"samples a single 2-D raster. Provide a 2-D POINT(x y); got {coords!r}."
    )


def _parse_xy_pair(pair: str) -> tuple[float, float]:
    """Parse one WKT ``x y`` coordinate pair into an ``(x, y)`` vertex.

    Args:
        pair: One vertex's coordinate text, without the separator or the
            parentheses around it (e.g., ``"0 1"``).

    Returns:
        tuple[float, float]: The parsed vertex.

    Raises:
        ValueError: If the vertex is not a 2-D ``x y`` pair (including a 3-D or
            measured vertex), or a coordinate is not a number. The caller turns
            this into an :class:`InvalidCoords`.

    Examples:
        Whitespace around and within the pair is insignificant:

        >>> _parse_xy_pair(" 1   2 ")
        (1.0, 2.0)

        A pair that is not two numbers raises, quoting the text at fault for the
        caller to pass on. Three or four numbers are one 3-D or measured
        coordinate, refused as it is for a lone point:

        >>> _parse_xy_pair("0 0 5 1")
        Traceback (most recent call last):
            ...
        ValueError: vertical or measured coordinates are not supported: ...

        No coordinate has five or more numbers, so those are vertices run
        together, whatever separator between them was lost:

        >>> _parse_xy_pair("0 0 1 1 2")
        Traceback (most recent call last):
            ...
        ValueError: each vertex must be an 'x y' pair (this looks like vertices ...

        An empty pair can come from a stray comma or from an empty point, which
        this function cannot tell apart, so its message carries no hint:

        >>> _parse_xy_pair("")
        Traceback (most recent call last):
            ...
        ValueError: each vertex must be an 'x y' pair; got ''.
    """
    tokens = pair.split()

    # Grammars validate a polygon's ring list and a multipoint's point list
    # (_POLYGON_RING_LIST and the two multipoint point patterns), but a vertex
    # stays in logic, deliberately. A misplaced parenthesis in a list has no
    # diagnosis better than "malformed", whereas a vertex's token count identifies
    # its fault exactly, and a grammar strict enough to reject every wrong count
    # would report one "malformed vertex list" for all of them.
    #
    # Checked before counting, and checked rather than caught, both as in
    # parse_point_wkt.
    if not all(map(_COORDINATE_TOKEN.fullmatch, tokens)):
        msg = f"each vertex coordinate must be a number; got {pair.strip()!r}."
        raise ValueError(msg)

    # Three or four numbers are one coordinate with a vertical or measured value,
    # so `0 0 1 1` is refused even when it is two 2-D vertices with the separator
    # between them lost.
    if len(tokens) in _VERTICAL_OR_MEASURED_TOKEN_COUNTS:
        msg = (
            "vertical or measured coordinates are not supported: each vertex "
            f"must be a 2-D 'x y' pair; got {pair.strip()!r}."
        )
        raise ValueError(msg)

    # With three and four refused as vertical, more than two numbers here means
    # five or more, and no coordinate has that many, so those are vertices run
    # together. The hint gives that diagnosis rather than a repair, because the
    # lost separator depends on the caller's list: a comma in a ring or a bare
    # multipoint, but `), (` between parenthesized points. Fewer than two
    # numbers is a missing coordinate, or an empty vertex, which in a ring comes
    # from a stray comma and in a multipoint from an empty `()`. This function
    # cannot tell those apart, so it gives no hint.
    if len(tokens) != 2:
        hint = " (this looks like vertices run together)" if len(tokens) > 2 else ""
        msg = f"each vertex must be an 'x y' pair{hint}; got {pair.strip()!r}."
        raise ValueError(msg)

    x, y = (float(token) for token in tokens)

    return x, y
