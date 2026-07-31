"""The order the files in a package have to be imported in, and why.

Importing a program before the UDTs it references fails in Studio 5000 with an
error that does not name the missing type, so getting this wrong costs the
customer an hour of guessing. This module turns the dependency edges the package
builder knows about into a total order, and writes ``IMPORT_ORDER.md`` stating
the *reason* for every constraint — because a customer who imports in a
different order should be able to find out exactly what will break rather than
be told to follow the numbers.

Two properties matter as much as the order itself:

* **It is deterministic.** Ties are broken by an explicit rank and then by path,
  never by whatever order the edges happened to arrive in. The same package
  always produces the same sequence, and therefore the same manifest bytes.
* **A cycle is an error that names the cycle.** Emitting an arbitrary order for
  a graph that has no valid one would produce a package whose numbers are
  confidently wrong, and the failure would surface at the customer's import
  dialog with nothing to point at. :class:`ImportOrderCycleError` names the
  files in the loop.

An edge whose prerequisite is *not* in the package — an Add-On Instruction the
customer already has, a module defined in a controller file we did not generate
— is carried as a constraint with no target. It orders nothing and it appears in
``IMPORT_ORDER.md`` as a prerequisite to satisfy beforehand, which is the only
place a reader would otherwise never learn about it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from app.core.enums import ExportArtifactKind, ImportDependencyKind
from app.generators.rockwell.l5x import RockwellGenerationError

__all__ = [
    "ImportConstraint",
    "ImportItem",
    "ImportOrderCycleError",
    "render_import_order_markdown",
    "resolve_import_order",
]


class ImportOrderCycleError(RockwellGenerationError):
    """The dependency edges describe a loop, so no import order exists."""

    code = "rockwell_import_order_cycle"

    def __init__(self, cycle: Sequence[str]) -> None:
        rendered = " depends on ".join([*cycle, cycle[0]]) if cycle else "(empty)"
        super().__init__(
            "The import dependencies contain a cycle, so there is no order that "
            f"satisfies all of them: {rendered}. One of these constraints is wrong; "
            "an arbitrary order would be a package whose numbers cannot be trusted.",
            details={"cycle": list(cycle)},
        )
        self.cycle = tuple(cycle)


@dataclass(frozen=True, slots=True)
class ImportItem:
    """One file that gets imported, and where it naturally sits.

    ``rank`` is the tie-break between files that no constraint separates — data
    types before tags before programs — so the sequence reads the way an
    engineer expects even where nothing formally requires it.
    """

    key: str
    label: str
    kind: ExportArtifactKind
    rank: int = 0


@dataclass(frozen=True, slots=True)
class ImportConstraint:
    """One "import this after that", with the sentence explaining it.

    ``depends_on_key`` is ``None`` when the prerequisite is outside the package.
    Such a constraint still has to be stated: a null foreign key would make an
    external prerequisite vanish, which is exactly the dependency a customer
    discovers at the import dialog.
    """

    item_key: str
    depends_on_key: str | None
    depends_on_label: str
    kind: ImportDependencyKind
    reason: str


@dataclass(slots=True)
class _Node:
    item: ImportItem
    after: set[str] = field(default_factory=set)


def _index(items: Sequence[ImportItem]) -> dict[str, _Node]:
    nodes: dict[str, _Node] = {}
    for item in items:
        if item.key in nodes:
            raise RockwellGenerationError(
                f"Two files in this package claim the same path: {item.key}. "
                "One would overwrite the other.",
                details={"key": item.key},
            )
        nodes[item.key] = _Node(item=item)
    return nodes


def _find_cycle(nodes: Mapping[str, _Node], remaining: set[str]) -> list[str]:
    """One concrete cycle among ``remaining``, for the error message.

    An iterative depth-first walk with an explicit path, so a deeply chained
    package cannot exhaust the interpreter stack while reporting an error.
    """
    colour: dict[str, int] = dict.fromkeys(remaining, 0)
    for start in sorted(remaining):
        if colour[start] != 0:
            continue
        path: list[str] = []
        stack: list[tuple[str, list[str]]] = [(start, sorted(nodes[start].after & remaining))]
        colour[start] = 1
        path.append(start)
        while stack:
            node, pending = stack[-1]
            if not pending:
                colour[node] = 2
                stack.pop()
                path.pop()
                continue
            nxt = pending.pop()
            if colour.get(nxt) == 1:
                # ``path`` runs along ``after`` edges, so each element depends on
                # the next and the slice from ``nxt`` is the loop itself.
                return path[path.index(nxt) :]
            if colour.get(nxt) == 0:
                colour[nxt] = 1
                path.append(nxt)
                stack.append((nxt, sorted(nodes[nxt].after & remaining)))
    return sorted(remaining)


def resolve_import_order(
    items: Sequence[ImportItem], constraints: Iterable[ImportConstraint]
) -> tuple[ImportItem, ...]:
    """Order ``items`` so every constraint's prerequisite comes first.

    Kahn's algorithm over a ready set that is always taken in ``(rank, key)``
    order, which is what makes the result a function of the input rather than of
    the order the edges were appended.
    """
    nodes = _index(items)
    for constraint in constraints:
        if constraint.item_key not in nodes:
            raise RockwellGenerationError(
                f"An import constraint names a file that is not in this package: "
                f"{constraint.item_key}.",
                details={"item_key": constraint.item_key},
            )
        target = constraint.depends_on_key
        if target is None:
            continue
        if target not in nodes:
            raise RockwellGenerationError(
                f"{constraint.item_key} is ordered after {target}, which is not in "
                "this package. A prerequisite outside the package is recorded with no "
                "target rather than with a name nothing resolves.",
                details={"item_key": constraint.item_key, "depends_on_key": target},
            )
        if target != constraint.item_key:
            nodes[constraint.item_key].after.add(target)

    remaining = set(nodes)
    satisfied: set[str] = set()
    ordered: list[ImportItem] = []
    while remaining:
        ready = sorted(
            (key for key in remaining if nodes[key].after <= satisfied),
            key=lambda key: (nodes[key].item.rank, key),
        )
        if not ready:
            raise ImportOrderCycleError(_find_cycle(nodes, remaining))
        chosen = ready[0]
        ordered.append(nodes[chosen].item)
        satisfied.add(chosen)
        remaining.discard(chosen)
    return tuple(ordered)


def render_import_order_markdown(
    ordered: Sequence[ImportItem],
    constraints: Sequence[ImportConstraint],
    *,
    release_name: str,
    preamble: Sequence[str] = (),
) -> bytes:
    """``IMPORT_ORDER.md``: the sequence, with the reason for each constraint."""
    by_item: dict[str, list[ImportConstraint]] = {}
    for constraint in constraints:
        by_item.setdefault(constraint.item_key, []).append(constraint)

    lines: list[str] = [
        f"# Import order - {release_name}",
        "",
        "These files are import-ready candidates. No Studio 5000 import has been",
        "verified; see `LIMITATIONS.md` before you begin.",
        "",
        "Import them in the order below. Each entry lists what it depends on and why,",
        "so that importing out of order tells you what will break rather than leaving",
        "you to work it out from an error that does not name the missing object.",
        "",
    ]
    lines.extend(preamble)
    if preamble:
        lines.append("")

    external: list[ImportConstraint] = []
    for position, item in enumerate(ordered, start=1):
        lines.append(f"{position}. `{item.key}` - {item.label}")
        entries = sorted(
            by_item.get(item.key, []),
            key=lambda constraint: (constraint.depends_on_key or "", str(constraint.kind)),
        )
        if not entries:
            lines.append("   - No prerequisite inside this package.")
        for constraint in entries:
            if constraint.depends_on_key is None:
                external.append(constraint)
                lines.append(
                    f"   - Needs, from outside this package: {constraint.depends_on_label} "
                    f"({constraint.kind}) - {constraint.reason}"
                )
                continue
            lines.append(
                f"   - After `{constraint.depends_on_key}` ({constraint.kind}) - "
                f"{constraint.reason}"
            )
        lines.append("")

    lines.append("## Prerequisites this package does not contain")
    lines.append("")
    if external:
        for constraint in sorted(external, key=lambda item: (item.depends_on_label, item.item_key)):
            lines.append(
                f"- {constraint.depends_on_label} - required by `{constraint.item_key}`. "
                f"{constraint.reason}"
            )
    else:
        lines.append("- None. Every prerequisite named above is a file in this package.")
    lines.append("")
    return "\n".join(lines).encode("utf-8")
