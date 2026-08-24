from provgate.store.crypto import SecretBox, generate_key
from provgate.store.db import connect
from provgate.store.models import AssignmentPolicy, IngestScope, PolicyKind
from provgate.store.repository import Repository


def make_repo_with_class() -> tuple[Repository, int]:
    repo = Repository(connect(":memory:"), SecretBox(generate_key()))
    c = repo.add_class(
        label="a",
        gradescope_course_id="1",
        gradescope_email="e",
        provenance_base_url="u",
        provenance_semester_id="s",
        assignment_policy=AssignmentPolicy(PolicyKind.ALL),
    )
    return repo, c.id


def test_unmapped_assignment_has_no_scope() -> None:
    repo, cid = make_repo_with_class()
    assert repo.get_assignment_scope(cid, "409194023") is None


def test_set_then_get_roundtrips() -> None:
    repo, cid = make_repo_with_class()
    repo.set_assignment_scope(cid, "409194023", provenance_assignment_id="proj2")
    assert repo.get_assignment_scope(cid, "409194023") == IngestScope("proj2", None)


def test_explicit_glob_roundtrips() -> None:
    repo, cid = make_repo_with_class()
    repo.set_assignment_scope(
        cid, "409194023", provenance_assignment_id="proj2", path_glob="src/proj2/**"
    )
    scope = repo.get_assignment_scope(cid, "409194023")
    assert scope == IngestScope("proj2", "src/proj2/**")
    assert scope is not None and scope.effective_glob() == "src/proj2/**"


def test_set_is_an_upsert() -> None:
    repo, cid = make_repo_with_class()
    repo.set_assignment_scope(cid, "409194023", provenance_assignment_id="proj2")
    repo.set_assignment_scope(cid, "409194023", provenance_assignment_id="proj3")
    assert repo.get_assignment_scope(cid, "409194023") == IngestScope("proj3", None)


def test_set_clears_a_previously_stored_glob() -> None:
    repo, cid = make_repo_with_class()
    repo.set_assignment_scope(cid, "1", provenance_assignment_id="proj2", path_glob="src/proj2/**")
    repo.set_assignment_scope(cid, "1", provenance_assignment_id="proj2")
    assert repo.get_assignment_scope(cid, "1") == IngestScope("proj2", None)


def test_scopes_are_isolated_per_assignment_and_listed_sorted() -> None:
    repo, cid = make_repo_with_class()
    repo.set_assignment_scope(cid, "222", provenance_assignment_id="lab5")
    repo.set_assignment_scope(cid, "111", provenance_assignment_id="proj2")
    assert repo.list_assignment_scopes(cid) == [
        ("111", IngestScope("proj2", None)),
        ("222", IngestScope("lab5", None)),
    ]


def test_delete_removes_only_the_named_scope() -> None:
    repo, cid = make_repo_with_class()
    repo.set_assignment_scope(cid, "111", provenance_assignment_id="proj2")
    repo.set_assignment_scope(cid, "222", provenance_assignment_id="lab5")
    repo.delete_assignment_scope(cid, "111")
    assert repo.get_assignment_scope(cid, "111") is None
    assert repo.get_assignment_scope(cid, "222") == IngestScope("lab5", None)


def test_delete_of_a_missing_scope_is_a_noop() -> None:
    repo, cid = make_repo_with_class()
    repo.delete_assignment_scope(cid, "nope")
    assert repo.list_assignment_scopes(cid) == []
