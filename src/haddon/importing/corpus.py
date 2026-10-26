"""A corpus manifest: where a set of source journals is, which entities it holds, and which years to read.

These are facts about a set of journals, not about a ledger, so they live in a manifest outside ``src/haddon``
(issue 0006).  Before that they were constants in the importer and the oracles, which made a generic ledger name
one company's checkout, entities and calendar year.  The sample corpus is one such manifest: a verification
fixture, not the definition of a valid Haddon installation (``docs/architecture.md``).

There is deliberately no default path in this module.  A fallback to the repository's own ``corpus/`` would put
the checkout back in the product by a longer route, and would silently pick up whatever manifest happened to be in
the working directory.  ``HADDON_CORPUS`` names the manifest.  The ``bin/`` wrappers and the test suite set it to
this repository's copy; anything else is told which variable to set.

Nothing in the journal core reads any of this.  Initialising an entity, posting, correcting and closing need no
corpus at all: ``bin/haddon-init`` and ``docs/quickstart.md`` are the whole of the bootstrap.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from haddon.domain.errors import validation

#: the environment variable naming the manifest to use
CORPUS_ENV = "HADDON_CORPUS"


@dataclass(frozen=True)
class CorpusEntity:
    """One entity in a corpus: the directory it is read from, and the identity it is provisioned with."""

    key: str  # directory name in the checkout
    id: str
    name: str
    legal_form: str
    country: str
    base_currency: str
    #: the years this company's books run over, oldest first, when they are not the corpus's own
    #: ``continuity_years``.  How far back a company's journals go is a fact about that company (the sample
    #: corpus's Example S.L. was constituted in August 2024 and Example B.V. has no 2024 journal at all), so a
    #: single corpus-wide chain cannot be right for every entity.
    continuity_years: tuple[int, ...] | None = None


@dataclass(frozen=True)
class Corpus:
    """One set of source journals: where they are, what is in them and which years the tooling reads by default."""

    name: str
    path: Path                       # the manifest itself
    checkout: Path
    default_year: int
    #: the default years to load into one journal, oldest first.  A continuity run needs a boundary to check, so
    #: two is the minimum.  An entity that declares its own overrides this one.
    continuity_years: tuple[int, ...]
    read_only_roots: tuple[Path, ...]  # trees no Haddon command may write into
    entities: dict[str, CorpusEntity]
    baselines: Path | None           # the oracle's pinned figures, measured from this corpus

    @property
    def entity_keys(self) -> tuple[str, ...]:
        return tuple(self.entities)

    def entity(self, key: str) -> CorpusEntity:
        found = self.entities.get(key)
        if found is None:
            raise validation(f"unknown entity {key!r} in corpus {self.name!r}; known: {sorted(self.entities)}",
                             path="entity", reason="enum", value=key)
        return found

    def continuity_years_for(self, key: str) -> tuple[int, ...]:
        """The years to load beside each other for one entity: its own chain if it declares one, else the
        corpus's.  Every boundary in the returned chain is one a continuity run checks."""
        return self.entity(key).continuity_years or self.continuity_years

    def journal(self, key: str, year: int | None = None) -> Path:
        """The main journal of one entity's year, as this corpus lays its checkout out."""
        year = self.default_year if year is None else year
        return self.checkout.joinpath(self.entity(key).key, str(year), "ledger", f"{year}.journal")


#: the fields one entity entry declares, in the order the error message lists them
ENTITY_FIELDS = ("key", "id", "name", "legal_form", "country", "base_currency")

#: fields an entity entry may declare.  Absent is not the same as empty: absent means "the corpus's own".
ENTITY_OPTIONAL_FIELDS = ("continuity_years",)


def _require(body: dict, field: str, path: Path):
    if field not in body:
        raise validation(f"corpus manifest {path} has no {field!r}", path=field, reason="required")
    return body[field]


def _text(body: dict, field: str, path: Path) -> str:
    value = _require(body, field, path)
    if not isinstance(value, str) or not value.strip():
        raise validation(f"corpus manifest {path}: {field!r} is a non-empty string, not {value!r}",
                         path=field, reason="string_required", value=value)
    return value


def _year(value: object, path: Path, field: str) -> int:
    """A calendar year as a whole number, refused with the manifest and the field named.

    Calling ``int()`` on it instead raised a bare ``ValueError`` that named neither (the 0006 review)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise validation(f"corpus manifest {path}: {field} is a calendar year as a whole number, not {value!r}",
                         path=field, reason="year_required", value=value)
    return value


def _year_chain(value: object, path: Path, field: str) -> tuple[int, ...]:
    """Two or more calendar years to load into one journal, oldest first.

    Strictly increasing is checked here rather than sorted silently.  The order is the order the years are
    imported in, and a manifest listing them the other way round describes a load that would post a later year's
    opening before the year it restates.  That is the double count the continuity oracle exists to catch,
    arriving by a different door.
    """
    if not isinstance(value, list) or len(value) < 2:
        raise validation(f"corpus manifest {path}: {field} is two or more years to load into one journal, "
                         f"oldest first", path=field, reason="shape", value=value)
    chain = tuple(_year(y, path, f"{field}[{i}]") for i, y in enumerate(value))
    if list(chain) != sorted(chain) or len(set(chain)) != len(chain):
        raise validation(f"corpus manifest {path}: {field} is strictly increasing -- a journal is loaded "
                         f"oldest year first -- not {list(chain)}", path=field, reason="order", value=list(chain))
    return chain


def _entity(item: object, index: int, path: Path) -> CorpusEntity:
    field = f"entities[{index}]"
    if not isinstance(item, dict):
        raise validation(f"corpus manifest {path}: {field} is an object declaring "
                         f"{', '.join(ENTITY_FIELDS)}, not {type(item).__name__}",
                         path=field, reason="object_required", value=item)
    missing = [f for f in ENTITY_FIELDS if f not in item]
    if missing:
        raise validation(f"corpus manifest {path}: {field} has no {', '.join(repr(f) for f in missing)}",
                         path=field, reason="required", value=missing)
    unknown = [f for f in item if f not in ENTITY_FIELDS + ENTITY_OPTIONAL_FIELDS]
    if unknown:
        raise validation(f"corpus manifest {path}: {field} declares {', '.join(repr(f) for f in unknown)}, "
                         f"which is not one of {', '.join(ENTITY_FIELDS + ENTITY_OPTIONAL_FIELDS)}",
                         path=field, reason="unknown_field", value=unknown)
    for name in ENTITY_FIELDS:
        if not isinstance(item[name], str) or not item[name].strip():
            raise validation(f"corpus manifest {path}: {field}.{name} is a non-empty string, not {item[name]!r}",
                             path=f"{field}.{name}", reason="string_required", value=item[name])
    years = item.get("continuity_years")
    return CorpusEntity(**{**item, "continuity_years":
                           None if years is None else _year_chain(years, path, f"{field}.continuity_years")})


def _beside(manifest: Path, value: str) -> Path:
    """A path the manifest names: absolute as written, or relative to the manifest's own directory.

    Never relative to the directory the process started in, which would let the same manifest name two checkouts."""
    given = Path(value)
    return given if given.is_absolute() else manifest.parent.joinpath(given).resolve()


def read_corpus(path: str | Path) -> Corpus:
    """Read one manifest.  Every field is required except ``baselines`` and ``read_only_roots``.

    Every refusal names this file and the field in it.  Issue 0006 made ``HADDON_CORPUS`` the single source with
    no fallback, so a manifest's errors are what the operator has to go on: refusing to guess only helps if it
    says what to fix.  The 0006 review found malformed JSON escaping as a raw ``JSONDecodeError``, an entity that
    was not an object as a ``TypeError`` about ``**``, and a non-integer year as ``invalid literal for int()``.
    None of them named the file, and two named no field.
    """
    path = Path(path)
    if not path.is_file():
        raise validation(f"corpus manifest {path} does not exist", path="corpus", reason="missing")
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise validation(f"corpus manifest {path} is not valid JSON: {exc.msg} at line {exc.lineno} "
                         f"column {exc.colno}", path="corpus", reason="malformed_json",
                         value=f"line {exc.lineno} column {exc.colno}") from None
    except OSError as exc:
        raise validation(f"corpus manifest {path} could not be read: {exc.strerror}",
                         path="corpus", reason="unreadable") from None
    if not isinstance(body, dict):
        raise validation(f"corpus manifest {path} is a JSON object, not {type(body).__name__}",
                         path="corpus", reason="object_required")
    years = _year_chain(_require(body, "continuity_years", path), path, "continuity_years")
    declared = _require(body, "entities", path)
    if not isinstance(declared, list):
        raise validation(f"corpus manifest {path}: entities is a list of entity objects, "
                         f"not {type(declared).__name__}", path="entities", reason="list_required")
    entities: dict[str, CorpusEntity] = {}
    for index, item in enumerate(declared):
        entity = _entity(item, index, path)
        # A duplicate key used to resolve silently to the last entry (the 0006 review).  An operator who
        # duplicated a block while adding a company would then import the wrong entity's journal under the right
        # entity's name, and nothing would say so.
        if entity.key in entities:
            first = entities[entity.key]
            raise validation(f"corpus manifest {path} declares entity key {entity.key!r} twice, as {first.id!r} "
                             f"({first.name}) and as {entity.id!r} ({entity.name}); a key names one entity",
                             path=f"entities[{index}].key", reason="duplicate_key", value=entity.key)
        entities[entity.key] = entity
    if not entities:
        raise validation(f"corpus manifest {path} declares no entities", path="entities", reason="required")
    roots = body.get("read_only_roots", ())
    if not isinstance(roots, (list, tuple)) or any(not isinstance(r, str) for r in roots):
        raise validation(f"corpus manifest {path}: read_only_roots is a list of directory paths",
                         path="read_only_roots", reason="shape", value=roots)
    baselines = body.get("baselines")
    if baselines is not None and not isinstance(baselines, str):
        raise validation(f"corpus manifest {path}: baselines is a path relative to this manifest, "
                         f"not {type(baselines).__name__}", path="baselines", reason="string_required")
    return Corpus(name=_text(body, "name", path), path=path,
                  checkout=_beside(path, _text(body, "checkout", path)),
                  default_year=_year(_require(body, "default_year", path), path, "default_year"),
                  continuity_years=years,
                  read_only_roots=tuple(_beside(path, r) for r in roots),
                  entities=entities,
                  baselines=None if baselines is None else path.parent.joinpath(baselines))


@lru_cache(maxsize=4)
def _cached(path: str) -> Corpus:
    return read_corpus(path)


def configured_path() -> str | None:
    """The manifest path ``HADDON_CORPUS`` names, or ``None`` when it is unset or blank."""
    value = os.environ.get(CORPUS_ENV, "").strip()
    return value or None


def load_corpus(path: str | Path | None = None) -> Corpus:
    """The corpus to use: the one named, else the one ``HADDON_CORPUS`` names.

    Raises rather than guessing.  A ledger that guessed which company's books it was verifying against would be
    worse than one that refuses to run without being told.
    """
    chosen = str(path) if path is not None else configured_path()
    if chosen is None:
        raise validation(
            f"no corpus manifest: set {CORPUS_ENV} to one (this repository ships corpus/example.json). "
            f"Importing and the oracle read a corpus; initialising, posting and closing do not",
            path=CORPUS_ENV, reason="required")
    return _cached(chosen)
