import pytest

from provgate.store.models import AssignmentPolicy, IngestScope, PolicyKind


def test_parse_all() -> None:
    p = AssignmentPolicy.parse("all")
    assert p == AssignmentPolicy(PolicyKind.ALL, ())
    assert p.serialize() == "all"


def test_parse_include() -> None:
    p = AssignmentPolicy.parse("include:872677, 872690")
    assert p == AssignmentPolicy(PolicyKind.INCLUDE, ("872677", "872690"))
    assert p.serialize() == "include:872677,872690"


def test_parse_exclude() -> None:
    p = AssignmentPolicy.parse("exclude:900001")
    assert p == AssignmentPolicy(PolicyKind.EXCLUDE, ("900001",))
    assert p.serialize() == "exclude:900001"


def test_parse_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        AssignmentPolicy.parse("include:")
    with pytest.raises(ValueError):
        AssignmentPolicy.parse("nonsense")


def test_ingest_scope_derives_glob_from_assignment_id() -> None:
    assert IngestScope(provenance_assignment_id="proj2").effective_glob() == "proj2/**"


def test_ingest_scope_explicit_glob_wins() -> None:
    scope = IngestScope(provenance_assignment_id="proj2", path_glob="src/proj2/**")
    assert scope.effective_glob() == "src/proj2/**"


def test_ingest_scope_is_frozen() -> None:
    import dataclasses

    scope = IngestScope(provenance_assignment_id="proj2")
    with pytest.raises(dataclasses.FrozenInstanceError):
        scope.provenance_assignment_id = "lab5"  # type: ignore[misc]
