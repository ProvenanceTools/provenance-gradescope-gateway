# provgate Per-Assignment Ingest Scoping Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let provgate tell Provenance to ingest only one assignment subdirectory out of a monorepo submission, configured per (class, Gradescope assignment).

**Architecture:** A new `assignment_scopes` table maps `(class_id, gs_assignment_id) → provenance_assignment_id [+ optional path_glob]`. The sync engine looks it up per assignment and passes an `IngestScope` to the Provenance client, which renders it as `repo_scoped` + a derived `path_glob` — as query params on the single-shot upload and as a nested JSON object on the chunked upload's `complete` call. Excluded-scope counts are read back off the existing job-status payload and surfaced in the run summary. **No row ⇒ no override ⇒ byte-identical behavior to today.**

**Tech Stack:** Python 3.11, uv, sqlite3 (stdlib), httpx, Typer, pytest + respx, ruff, mypy --strict.

**Spec:** `/Users/aaryanmehta/projects/provenance/docs/superpowers/specs/2026-08-23-monorepo-scope-selection-design.md` (§3 D2/D3, §4.3, §4.4, §5). Read §4.3 and §4.4 before starting.

## Global Constraints

- **Server side is already built. This plan changes NO Provenance server code.** The wire contract (`scope_mode` / `scope_path_glob` / `scope_on_multiple`, and the nested `ingest_scope` object) already exists and is validated server-side.
- **Definition of done (per provgate CLAUDE.md):** `uv run pytest` green, `uv run mypy --strict src` clean, `uv run ruff check .` and `uv run ruff format --check .` clean, diff reviewable.
- **No new runtime dependencies.** Everything here uses stdlib + already-present httpx/Typer.
- **The safety property is testable and MUST have a test:** an assignment with no `assignment_scopes` row sends a request containing no `scope_*` query params and no `ingest_scope` JSON key. This is what guarantees existing classes are unaffected.
- **Schema is applied by `conn.executescript(SCHEMA_SQL)` on every `connect()`.** There is no migration framework. New DDL must be `CREATE TABLE IF NOT EXISTS` and must keep `tests/store/test_db.py::test_connect_is_idempotent` passing.
- **Derived glob is `f"{provenance_assignment_id}/**"`.** Never `**{id}/**` — that also matches `myproj2/`. See spec §4.4.
- House style: frozen dataclasses, `StrEnum`, keyword-only repository args, `?` parameter binding, `self._conn.commit()` after every mutation, `sqlite3.Row` string access, `":memory:"` DBs in store tests, respx for HTTP, hand-written fake port classes for engine tests.
- Commit with `git commit --no-gpg-sign`, conventional-commit prefixes, explicit pathspec. No `Co-Authored-By` trailer.

---

### Task 1: `assignment_scopes` table + `IngestScope` model

**Files:**
- Modify: `src/provgate/store/db.py` (append to `SCHEMA_SQL`)
- Modify: `src/provgate/store/models.py` (append `IngestScope`)
- Test: `tests/store/test_models.py` (append), `tests/store/test_db.py` (append)

**Interfaces:**
- Consumes: nothing.
- Produces: `IngestScope(provenance_assignment_id: str, path_glob: str | None = None)` with `effective_glob(self) -> str`. Tasks 2, 4, 5 and 7 all depend on this exact shape.

- [ ] **Step 1: Write the failing tests**

Append to `tests/store/test_models.py`:

```python
from provgate.store.models import IngestScope


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
```

Add `import pytest` at the top of `tests/store/test_models.py` if it is not already imported.

Append to `tests/store/test_db.py`:

```python
def test_assignment_scopes_table_exists() -> None:
    conn = connect(":memory:")
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='assignment_scopes'"
    ).fetchall()
    assert len(rows) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/store/test_models.py tests/store/test_db.py -v`
Expected: FAIL — `ImportError: cannot import name 'IngestScope'` and the table test failing with `assert 0 == 1`.

- [ ] **Step 3: Add the DDL**

In `src/provgate/store/db.py`, append this block INSIDE the `SCHEMA_SQL` string, after the `runs` table:

```sql
CREATE TABLE IF NOT EXISTS assignment_scopes (
    class_id INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    gs_assignment_id TEXT NOT NULL,
    provenance_assignment_id TEXT NOT NULL,
    path_glob TEXT,
    PRIMARY KEY (class_id, gs_assignment_id)
);
```

- [ ] **Step 4: Add the model**

Append to `src/provgate/store/models.py`:

```python
@dataclass(frozen=True)
class IngestScope:
    """Which assignment subdirectory of a monorepo submission to ingest.

    `path_glob` is an escape hatch for layouts where the directory name is not
    the assignment id (e.g. `src/proj2/`). When it is None the glob is derived
    as `<provenance_assignment_id>/**`, which matches a directory at the repo
    root and nothing else.

    Deliberately NOT `**<id>/**`: that also matches `myproj2/` and `notproj2/`.
    See the design spec §4.4 — the glob grammar has no brace expansion, so no
    single derived glob covers both root-level and nested layouts.
    """

    provenance_assignment_id: str
    path_glob: str | None = None

    def effective_glob(self) -> str:
        return self.path_glob or f"{self.provenance_assignment_id}/**"
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/store/ -v`
Expected: PASS, including the pre-existing `test_connect_is_idempotent`.

- [ ] **Step 6: Commit**

```bash
git add src/provgate/store/db.py src/provgate/store/models.py tests/store/test_models.py tests/store/test_db.py
git commit --no-gpg-sign -m "feat(store): add assignment_scopes table and IngestScope model"
```

---

### Task 2: Repository CRUD for assignment scopes

**Files:**
- Modify: `src/provgate/store/repository.py`
- Test: Create `tests/store/test_repository_scopes.py`

**Interfaces:**
- Consumes: `IngestScope` from Task 1.
- Produces, on `Repository`:
  - `set_assignment_scope(self, class_id: int, gs_assignment_id: str, *, provenance_assignment_id: str, path_glob: str | None = None) -> None`
  - `get_assignment_scope(self, class_id: int, gs_assignment_id: str) -> IngestScope | None`
  - `list_assignment_scopes(self, class_id: int) -> list[tuple[str, IngestScope]]` (sorted by `gs_assignment_id`)
  - `delete_assignment_scope(self, class_id: int, gs_assignment_id: str) -> None`

  Task 5 calls `get_assignment_scope`; Task 7 calls all four.

- [ ] **Step 1: Write the failing test**

Create `tests/store/test_repository_scopes.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/store/test_repository_scopes.py -v`
Expected: FAIL with `AttributeError: 'Repository' object has no attribute 'get_assignment_scope'`.

- [ ] **Step 3: Implement**

Add `IngestScope` to the existing `from provgate.store.models import ...` line in `src/provgate/store/repository.py`, then add these four methods to the `Repository` class:

```python
    def set_assignment_scope(
        self,
        class_id: int,
        gs_assignment_id: str,
        *,
        provenance_assignment_id: str,
        path_glob: str | None = None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO assignment_scopes
                (class_id, gs_assignment_id, provenance_assignment_id, path_glob)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(class_id, gs_assignment_id) DO UPDATE SET
                provenance_assignment_id = excluded.provenance_assignment_id,
                path_glob = excluded.path_glob
            """,
            (class_id, gs_assignment_id, provenance_assignment_id, path_glob),
        )
        self._conn.commit()

    def get_assignment_scope(self, class_id: int, gs_assignment_id: str) -> IngestScope | None:
        row = self._conn.execute(
            """
            SELECT provenance_assignment_id, path_glob FROM assignment_scopes
            WHERE class_id = ? AND gs_assignment_id = ?
            """,
            (class_id, gs_assignment_id),
        ).fetchone()
        if row is None:
            return None
        return IngestScope(
            provenance_assignment_id=row["provenance_assignment_id"],
            path_glob=row["path_glob"],
        )

    def list_assignment_scopes(self, class_id: int) -> list[tuple[str, IngestScope]]:
        rows = self._conn.execute(
            """
            SELECT gs_assignment_id, provenance_assignment_id, path_glob
            FROM assignment_scopes WHERE class_id = ? ORDER BY gs_assignment_id
            """,
            (class_id,),
        )
        return [
            (
                r["gs_assignment_id"],
                IngestScope(
                    provenance_assignment_id=r["provenance_assignment_id"],
                    path_glob=r["path_glob"],
                ),
            )
            for r in rows
        ]

    def delete_assignment_scope(self, class_id: int, gs_assignment_id: str) -> None:
        self._conn.execute(
            "DELETE FROM assignment_scopes WHERE class_id = ? AND gs_assignment_id = ?",
            (class_id, gs_assignment_id),
        )
        self._conn.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/store/ -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/provgate/store/repository.py tests/store/test_repository_scopes.py
git commit --no-gpg-sign -m "feat(store): repository CRUD for per-assignment ingest scopes"
```

---

### Task 3: Read excluded-scope counts off the job status

**Files:**
- Modify: `src/provgate/provenance/client.py` (`JobStatus`)
- Test: `tests/provenance/test_client.py` (append)

**Interfaces:**
- Consumes: nothing.
- Produces: `JobStatus.skipped_counts -> tuple[tuple[str, int], ...]` — reason→count pairs sorted by reason. Tasks 5 and 6 consume this.

**Why no `poll_job` change:** `JobStatus` already captures the whole response body in `raw`, and the server already returns `skipped` on it (`IngestJobStatusResponse.skipped`, nullable). This is a pure accessor over data provgate is already receiving.

- [ ] **Step 1: Write the failing test**

Append to `tests/provenance/test_client.py`:

```python
def test_skipped_counts_aggregates_by_reason() -> None:
    status = JobStatus(
        status="succeeded",
        raw={
            "status": "succeeded",
            "skipped": [
                {"folder_key": "a", "scope_path": "lab5/", "reason": "scope_excluded"},
                {"folder_key": "b", "scope_path": "lab5/", "reason": "scope_excluded"},
                {"folder_key": "c", "scope_path": "", "reason": "submission_type_mismatch"},
            ],
        },
    )
    assert status.skipped_counts == (
        ("scope_excluded", 2),
        ("submission_type_mismatch", 1),
    )


def test_skipped_counts_is_empty_when_null_or_absent() -> None:
    assert JobStatus(status="succeeded", raw={"skipped": None}).skipped_counts == ()
    assert JobStatus(status="succeeded", raw={}).skipped_counts == ()


def test_skipped_counts_ignores_malformed_entries() -> None:
    status = JobStatus(
        status="succeeded",
        raw={"skipped": ["not-a-dict", {"no_reason": 1}, {"reason": "no_seal"}]},
    )
    assert status.skipped_counts == (("no_seal", 1),)
```

Ensure `JobStatus` is imported in that test module.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/provenance/test_client.py -v`
Expected: FAIL with `AttributeError: 'JobStatus' object has no attribute 'skipped_counts'`.

- [ ] **Step 3: Implement**

Add this property to the `JobStatus` dataclass in `src/provgate/provenance/client.py`, after `is_success`:

```python
    @property
    def skipped_counts(self) -> tuple[tuple[str, int], ...]:
        """Excluded/failed scopes from this ingest, aggregated reason -> count.

        The server reports `skipped: null` to mean "not known yet" and `[]` to
        mean "resolution ran and skipped nothing". Both collapse to `()` here:
        the distinction matters to a polling client, and `poll_job` only ever
        returns a TERMINAL status, by which point `null` is not expected.

        Anything malformed is ignored rather than raising — a summary line must
        never be the thing that fails a sync that otherwise succeeded.
        """
        raw_skipped = self.raw.get("skipped")
        if not isinstance(raw_skipped, list):
            return ()
        counts: dict[str, int] = {}
        for entry in raw_skipped:
            if not isinstance(entry, dict):
                continue
            reason = entry.get("reason")
            if isinstance(reason, str):
                counts[reason] = counts.get(reason, 0) + 1
        return tuple(sorted(counts.items()))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/provenance/ -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/provgate/provenance/client.py tests/provenance/test_client.py
git commit --no-gpg-sign -m "feat(provenance): expose per-reason skipped counts on JobStatus"
```

---

### Task 4: Send the scope override on both upload paths

**Files:**
- Modify: `src/provgate/provenance/client.py` (`ingest_gradescope_export`, `_ingest_single`, `_ingest_chunked`, `_complete_upload`)
- Modify: `src/provgate/sync/ports.py` (`ProvenancePort.ingest_gradescope_export`)
- Test: `tests/provenance/test_client.py` (append)

**Interfaces:**
- Consumes: `IngestScope` (Task 1).
- Produces: `ingest_gradescope_export(self, base_url, token, semester_id, zip_bytes, *, scope: IngestScope | None = None) -> JobHandle`, and the matching `ProvenancePort` protocol signature. Task 5 calls it with `scope=`.

**Critical:** `scope` is keyword-only with a `None` default. That default is the safety property — every existing call site keeps working and sends nothing new.

- [ ] **Step 1: Write the failing tests**

Append to `tests/provenance/test_client.py` (follow the existing respx style in that file; adjust the base URL/token constants to match its helpers):

```python
@respx.mock
def test_single_upload_sends_no_scope_params_when_unmapped() -> None:
    route = respx.post("https://p.test/semesters/sem1/ingest:gradescope").mock(
        return_value=httpx.Response(202, json={"job_id": "j1"})
    )
    client = ProvenanceClient(httpx.Client())
    client.ingest_gradescope_export("https://p.test", "tok", "sem1", b"zip")
    assert route.called
    assert route.calls.last.request.url.params == httpx.QueryParams()


@respx.mock
def test_single_upload_sends_derived_glob() -> None:
    route = respx.post("https://p.test/semesters/sem1/ingest:gradescope").mock(
        return_value=httpx.Response(202, json={"job_id": "j1"})
    )
    client = ProvenanceClient(httpx.Client())
    client.ingest_gradescope_export(
        "https://p.test", "tok", "sem1", b"zip", scope=IngestScope("proj2")
    )
    params = route.calls.last.request.url.params
    assert params["scope_mode"] == "repo_scoped"
    assert params["scope_path_glob"] == "proj2/**"
    assert params["scope_on_multiple"] == "ingest_all"


@respx.mock
def test_single_upload_sends_explicit_glob() -> None:
    route = respx.post("https://p.test/semesters/sem1/ingest:gradescope").mock(
        return_value=httpx.Response(202, json={"job_id": "j1"})
    )
    client = ProvenanceClient(httpx.Client())
    client.ingest_gradescope_export(
        "https://p.test", "tok", "sem1", b"zip", scope=IngestScope("proj2", "src/proj2/**")
    )
    assert route.calls.last.request.url.params["scope_path_glob"] == "src/proj2/**"


@respx.mock
def test_chunked_upload_sends_scope_on_complete() -> None:
    """The monorepo exports that need scoping are exactly the ones big enough
    to take the chunked path, so this is the branch that actually matters."""
    import json as _json

    respx.post("https://p.test/semesters/sem1/ingest/uploads").mock(
        return_value=httpx.Response(
            200,
            json={
                "upload_id": "u1",
                "s3_upload_id": "s3-1",
                "chunk_size": 4,
                "total_parts": 2,
            },
        )
    )
    respx.put(url__regex=r".*/parts/\d+").mock(return_value=httpx.Response(200))
    complete = respx.post(
        "https://p.test/semesters/sem1/ingest/uploads/u1/complete"
    ).mock(return_value=httpx.Response(202, json={"job_id": "j1"}))

    client = ProvenanceClient(httpx.Client(), chunk_threshold_bytes=4, chunk_size_bytes=4)
    client.ingest_gradescope_export(
        "https://p.test", "tok", "sem1", b"12345678", scope=IngestScope("proj2")
    )

    body = _json.loads(complete.calls.last.request.content)
    assert body["s3_upload_id"] == "s3-1"
    assert body["ingest_scope"] == {
        "mode": "repo_scoped",
        "path_glob": "proj2/**",
        "on_multiple": "ingest_all",
    }


@respx.mock
def test_chunked_upload_omits_ingest_scope_when_unmapped() -> None:
    import json as _json

    respx.post("https://p.test/semesters/sem1/ingest/uploads").mock(
        return_value=httpx.Response(
            200,
            json={
                "upload_id": "u1",
                "s3_upload_id": "s3-1",
                "chunk_size": 4,
                "total_parts": 2,
            },
        )
    )
    respx.put(url__regex=r".*/parts/\d+").mock(return_value=httpx.Response(200))
    complete = respx.post(
        "https://p.test/semesters/sem1/ingest/uploads/u1/complete"
    ).mock(return_value=httpx.Response(202, json={"job_id": "j1"}))

    client = ProvenanceClient(httpx.Client(), chunk_threshold_bytes=4, chunk_size_bytes=4)
    client.ingest_gradescope_export("https://p.test", "tok", "sem1", b"12345678")

    body = _json.loads(complete.calls.last.request.content)
    assert "ingest_scope" not in body
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/provenance/test_client.py -v`
Expected: FAIL with `TypeError: ingest_gradescope_export() got an unexpected keyword argument 'scope'`.

- [ ] **Step 3: Implement the wire mapping**

Add to `src/provgate/provenance/client.py`, importing `IngestScope`:

```python
from provgate.store.models import IngestScope
```

and two module-level helpers (place them after the `_SUCCESS` constants):

```python
def _scope_query_params(scope: IngestScope) -> dict[str, str]:
    """Flat query form, for the multipart routes whose body is the archive."""
    return {
        "scope_mode": "repo_scoped",
        "scope_path_glob": scope.effective_glob(),
        "scope_on_multiple": "ingest_all",
    }


def _scope_json_object(scope: IngestScope) -> dict[str, str]:
    """Nested object form, for the JSON-bodied resumable-upload complete call."""
    return {
        "mode": "repo_scoped",
        "path_glob": scope.effective_glob(),
        "on_multiple": "ingest_all",
    }
```

- [ ] **Step 4: Thread `scope` through the four methods**

Replace the four method signatures/bodies in `src/provgate/provenance/client.py`:

```python
    def ingest_gradescope_export(
        self,
        base_url: str,
        token: str,
        semester_id: str,
        zip_bytes: bytes,
        *,
        scope: IngestScope | None = None,
    ) -> JobHandle:
        if len(zip_bytes) < self._chunk_threshold_bytes:
            return self._ingest_single(base_url, token, semester_id, zip_bytes, scope)
        return self._ingest_chunked(base_url, token, semester_id, zip_bytes, scope)

    def _ingest_single(
        self,
        base_url: str,
        token: str,
        semester_id: str,
        zip_bytes: bytes,
        scope: IngestScope | None = None,
    ) -> JobHandle:
        url = f"{base_url}/semesters/{semester_id}/ingest:gradescope"
        try:
            resp = self._http.post(
                url,
                headers=self._auth(token),
                files={"archive": ("export.zip", zip_bytes, "application/zip")},
                params=_scope_query_params(scope) if scope is not None else None,
            )
        except httpx.HTTPError as e:
            raise ProvenanceError(f"ingest request failed: {e}") from e
        # ... rest of the method is UNCHANGED ...
```

For `_ingest_chunked`, add the same trailing `scope: IngestScope | None = None` parameter and forward it to `_complete_upload` at its call site (leave `_create_upload`, `_put_part` and `_abort_upload` untouched — the scope belongs only on `complete`).

For `_complete_upload`:

```python
    def _complete_upload(
        self,
        base_url: str,
        token: str,
        semester_id: str,
        upload_id: str,
        s3_upload_id: str,
        scope: IngestScope | None = None,
    ) -> JobHandle:
        url = f"{base_url}/semesters/{semester_id}/ingest/uploads/{upload_id}/complete"
        body: dict[str, object] = {"s3_upload_id": s3_upload_id}
        if scope is not None:
            body["ingest_scope"] = _scope_json_object(scope)
        try:
            resp = self._http.post(url, headers=self._auth(token), json=body)
        except httpx.HTTPError as e:
            raise ProvenanceError(f"complete upload failed: {e}") from e
        # ... rest of the method is UNCHANGED ...
```

Note `_abort_upload`'s call to `_complete_upload` does not exist; only `_ingest_chunked` calls it.

- [ ] **Step 5: Update the port protocol**

In `src/provgate/sync/ports.py`, add the import and widen the protocol:

```python
from provgate.store.models import IngestScope


class ProvenancePort(Protocol):
    def ingest_gradescope_export(
        self,
        base_url: str,
        token: str,
        semester_id: str,
        zip_bytes: bytes,
        *,
        scope: IngestScope | None = None,
    ) -> JobHandle: ...
    def poll_job(self, base_url: str, token: str, semester_id: str, job_id: str) -> JobStatus: ...
```

- [ ] **Step 6: Run tests and the type checker**

Run: `uv run pytest tests/provenance/ -v && uv run mypy --strict src`
Expected: PASS and clean. If mypy reports that `tests/sync/test_engine.py`'s `FakeProv` no longer satisfies `ProvenancePort`, that is Task 5's job — do not fix it here.

- [ ] **Step 7: Commit**

```bash
git add src/provgate/provenance/client.py src/provgate/sync/ports.py tests/provenance/test_client.py
git commit --no-gpg-sign -m "feat(provenance): send repo_scoped override on both upload paths"
```

---

### Task 5: Look up and apply the scope during sync

**Files:**
- Modify: `src/provgate/sync/engine.py` (`AssignmentOutcome`, `_sync_assignment`)
- Test: `tests/sync/test_engine.py` (update `FakeProv`, append tests)

**Interfaces:**
- Consumes: `Repository.get_assignment_scope` (Task 2), `JobStatus.skipped_counts` (Task 3), `ingest_gradescope_export(..., scope=)` (Task 4).
- Produces: `AssignmentOutcome.skipped_counts: tuple[tuple[str, int], ...] = ()`. Task 6 renders it.

**Why a tuple of pairs and not a dict:** `AssignmentOutcome` is `@dataclass(frozen=True)`, so a `dict` field would break hashing; a sorted tuple is also deterministic, which keeps the rendered summary stable in tests.

- [ ] **Step 1: Write the failing tests**

In `tests/sync/test_engine.py`, first update the existing `FakeProv` so it records the scope and satisfies the widened protocol:

```python
class FakeProv:
    def __init__(self, status: str = "succeeded", skipped: list[dict[str, str]] | None = None):
        self.status = status
        self.skipped = skipped
        self.scopes: list[IngestScope | None] = []

    def ingest_gradescope_export(
        self,
        base_url: str,
        token: str,
        semester_id: str,
        zip_bytes: bytes,
        *,
        scope: IngestScope | None = None,
    ) -> JobHandle:
        self.scopes.append(scope)
        return JobHandle(job_id="job-1")

    def poll_job(self, base_url: str, token: str, semester_id: str, job_id: str) -> JobStatus:
        return JobStatus(status=self.status, raw={"status": self.status, "skipped": self.skipped})
```

Keep any other behavior the existing `FakeProv` had (failure injection etc.) — extend it, do not replace it wholesale.

Then append:

```python
def test_unmapped_assignment_sends_no_scope() -> None:
    # The safety property: a class that never configured scoping is untouched.
    repo, cfg, gs, prov = make_sync_fixture()
    sync_class(repo, gs, prov, cfg, "tok", now_iso=fixed_now, dry_run=False)
    assert prov.scopes == [None]


def test_mapped_assignment_sends_its_scope() -> None:
    repo, cfg, gs, prov = make_sync_fixture()
    repo.set_assignment_scope(cfg.id, "872677", provenance_assignment_id="proj2")
    sync_class(repo, gs, prov, cfg, "tok", now_iso=fixed_now, dry_run=False)
    assert prov.scopes == [IngestScope("proj2", None)]


def test_skipped_counts_reach_the_outcome() -> None:
    repo, cfg, gs, prov = make_sync_fixture(
        skipped=[
            {"reason": "scope_excluded"},
            {"reason": "scope_excluded"},
        ]
    )
    repo.set_assignment_scope(cfg.id, "872677", provenance_assignment_id="proj2")
    outcomes = sync_class(repo, gs, prov, cfg, "tok", now_iso=fixed_now, dry_run=False)
    assert outcomes[0].skipped_counts == (("scope_excluded", 2),)
```

Adapt `make_sync_fixture` / `fixed_now` to whatever the existing helpers in that module are called — read the file first and reuse them rather than inventing new ones. The assignment id `"872677"` must match whatever `FakeGs.list_assignments` returns in the existing fixture.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/sync/test_engine.py -v`
Expected: FAIL — `prov.scopes` empty/absent, and `AttributeError` on `skipped_counts`.

- [ ] **Step 3: Implement**

In `src/provgate/sync/engine.py`, add `IngestScope` to the `provgate.store.models` import, then extend the dataclass:

```python
@dataclass(frozen=True)
class AssignmentOutcome:
    gs_assignment_id: str
    outcome: str  # succeeded | partial | failed | skipped | dry_run | error
    delta_count: int
    total_submissions: int
    job_id: str | None
    error: str | None
    skipped_counts: tuple[tuple[str, int], ...] = ()
```

In `_sync_assignment`, look the scope up before the ingest call and thread the counts into both success and failure outcomes:

```python
    scope = repo.get_assignment_scope(cfg.id, aid)
    handle = prov.ingest_gradescope_export(
        cfg.provenance_base_url,
        token,
        cfg.provenance_semester_id,
        pruned.zip_bytes,
        scope=scope,
    )
    status = prov.poll_job(
        cfg.provenance_base_url, token, cfg.provenance_semester_id, handle.job_id
    )
    if status.is_success:
        repo.mark_forwarded(cfg.id, aid, pruned.forwarded_keys, handle.job_id, now_iso())
        out = AssignmentOutcome(
            aid,
            status.status,
            delta,
            pruned.total_submissions,
            handle.job_id,
            None,
            status.skipped_counts,
        )
    else:
        out = AssignmentOutcome(
            aid,
            "failed",
            delta,
            pruned.total_submissions,
            handle.job_id,
            f"job status {status.status}",
            status.skipped_counts,
        )
```

Leave the `delta == 0` and `dry_run` early-return outcomes alone — they never ingested, so `()` (the default) is correct for them.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/sync/ -v && uv run mypy --strict src`
Expected: PASS and clean.

- [ ] **Step 5: Commit**

```bash
git add src/provgate/sync/engine.py tests/sync/test_engine.py
git commit --no-gpg-sign -m "feat(sync): apply the per-assignment ingest scope and record skip counts"
```

---

### Task 6: Surface skipped counts in the run summary

**Files:**
- Modify: `src/provgate/notify/render.py`
- Test: `tests/notify/test_render.py` (append)

**Interfaces:**
- Consumes: `AssignmentOutcome.skipped_counts` (Task 5).
- Produces: no new API — `render_summary`'s signature is unchanged.

**Why this is required, not cosmetic:** a typo'd or wrong-layout glob excludes every scope and ingests zero submissions while the job still reports success. Without this line that is indistinguishable from a quiet cohort. See spec §4.4.

- [ ] **Step 1: Write the failing test**

Append to `tests/notify/test_render.py`:

```python
def test_summary_reports_scope_skips() -> None:
    outcomes = {
        "cs61b": [
            AssignmentOutcome(
                gs_assignment_id="872677",
                outcome="succeeded",
                delta_count=0,
                total_submissions=300,
                job_id="job-1",
                error=None,
                skipped_counts=(("scope_excluded", 300),),
            )
        ]
    }
    out = render_summary(outcomes, now_iso="2026-08-24T00:00:00Z")
    assert "scope_excluded=300" in out


def test_summary_omits_the_skip_line_when_nothing_was_skipped() -> None:
    outcomes = {
        "cs61b": [
            AssignmentOutcome(
                gs_assignment_id="872677",
                outcome="succeeded",
                delta_count=5,
                total_submissions=5,
                job_id="job-1",
                error=None,
            )
        ]
    }
    out = render_summary(outcomes, now_iso="2026-08-24T00:00:00Z")
    assert "scope_excluded" not in out
    assert "skipped scopes" not in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/notify/test_render.py -v`
Expected: FAIL — `assert "scope_excluded=300" in out`.

- [ ] **Step 3: Implement**

In `src/provgate/notify/render.py`, inside the per-assignment loop that already emits a line per outcome, append a scope-skip fragment when present. Read the existing loop first and match its formatting exactly; the addition is:

```python
        if o.skipped_counts:
            detail = ", ".join(f"{reason}={count}" for reason, count in o.skipped_counts)
            lines.append(f"    skipped scopes: {detail}")
```

Use whatever the surrounding code calls its accumulator (`lines` above is illustrative) and match its indentation convention for sub-lines.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/notify/ -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/provgate/notify/render.py tests/notify/test_render.py
git commit --no-gpg-sign -m "feat(notify): report per-reason scope skips in the run summary"
```

---

### Task 7: `provgate assignment-scope` CLI

**Files:**
- Modify: `src/provgate/cli/main.py`
- Test: `tests/cli/test_cli.py` (append)

**Interfaces:**
- Consumes: all four repository methods from Task 2, `Repository.get_class` (existing).
- Produces: `provgate assignment-scope set|list|unset`.

- [ ] **Step 1: Write the failing test**

Append to `tests/cli/test_cli.py`, matching the existing CliRunner/monkeypatch conventions in that file (read it first — it already has a pattern for pointing `load_settings` at a temp DB):

```python
def test_assignment_scope_set_list_unset(tmp_path, monkeypatch) -> None:
    # Reuse whatever helper the module already uses to seed a class + env.
    env = make_cli_env(tmp_path, monkeypatch)

    result = runner.invoke(
        app,
        [
            "assignment-scope", "set",
            "--class", "cs61b",
            "--gs-assignment", "872677",
            "--assignment-id", "proj2",
        ],
    )
    assert result.exit_code == 0

    result = runner.invoke(app, ["assignment-scope", "list", "--class", "cs61b"])
    assert result.exit_code == 0
    assert "872677" in result.stdout
    assert "proj2/**" in result.stdout

    result = runner.invoke(
        app, ["assignment-scope", "unset", "--class", "cs61b", "--gs-assignment", "872677"]
    )
    assert result.exit_code == 0

    result = runner.invoke(app, ["assignment-scope", "list", "--class", "cs61b"])
    assert "872677" not in result.stdout


def test_assignment_scope_set_rejects_unknown_class(tmp_path, monkeypatch) -> None:
    make_cli_env(tmp_path, monkeypatch)
    result = runner.invoke(
        app,
        [
            "assignment-scope", "set",
            "--class", "nope",
            "--gs-assignment", "1",
            "--assignment-id", "proj2",
        ],
    )
    assert result.exit_code != 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/cli/test_cli.py -v`
Expected: FAIL — Typer exits non-zero on an unknown command.

- [ ] **Step 3: Implement**

In `src/provgate/cli/main.py`, register a sub-typer next to the existing `class_app` registration:

```python
scope_app = typer.Typer(help="Per-assignment ingest scoping for monorepo submissions.")
app.add_typer(scope_app, name="assignment-scope")


def _require_class(repo: Repository, label: str) -> ClassConfig:
    cfg = repo.get_class(label)
    if cfg is None:
        raise typer.BadParameter(f"no such class: {label!r}")
    return cfg


@scope_app.command("set")
def scope_set(
    class_: str = typer.Option(..., "--class"),
    gs_assignment: str = typer.Option(..., "--gs-assignment"),
    assignment_id: str = typer.Option(
        ..., "--assignment-id", help="Provenance assignment id, e.g. proj2"
    ),
    path_glob: str | None = typer.Option(
        None,
        "--path-glob",
        help="Override the derived '<assignment-id>/**' glob (for nested layouts).",
    ),
) -> None:
    """Ingest only one assignment subdirectory from this Gradescope assignment's exports."""
    repo = open_repo(load_settings())
    cfg = _require_class(repo, class_)
    repo.set_assignment_scope(
        cfg.id,
        gs_assignment,
        provenance_assignment_id=assignment_id,
        path_glob=path_glob,
    )
    scope = IngestScope(assignment_id, path_glob)
    typer.echo(f"scoped gs assignment {gs_assignment} -> {scope.effective_glob()}")


@scope_app.command("list")
def scope_list(class_: str = typer.Option(..., "--class")) -> None:
    repo = open_repo(load_settings())
    cfg = _require_class(repo, class_)
    scopes = repo.list_assignment_scopes(cfg.id)
    if not scopes:
        typer.echo("no assignment scopes configured (every export ingests every sealed scope)")
        return
    for gs_assignment_id, scope in scopes:
        derived = "" if scope.path_glob else " (derived)"
        typer.echo(
            f"{gs_assignment_id}\t{scope.provenance_assignment_id}"
            f"\t{scope.effective_glob()}{derived}"
        )


@scope_app.command("unset")
def scope_unset(
    class_: str = typer.Option(..., "--class"),
    gs_assignment: str = typer.Option(..., "--gs-assignment"),
) -> None:
    repo = open_repo(load_settings())
    cfg = _require_class(repo, class_)
    repo.delete_assignment_scope(cfg.id, gs_assignment)
    typer.echo(f"unscoped gs assignment {gs_assignment}")
```

Add `IngestScope` and `ClassConfig` to the existing `provgate.store.models` import, and `Repository` to the imports if it is not already there.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/cli/ -v && uv run mypy --strict src`
Expected: PASS and clean.

- [ ] **Step 5: Commit**

```bash
git add src/provgate/cli/main.py tests/cli/test_cli.py
git commit --no-gpg-sign -m "feat(cli): add provgate assignment-scope set/list/unset"
```

---

### Task 8: Docs + full green gate

**Files:**
- Modify: `README.md`, `CLAUDE.md` (provgate repo)
- Modify: `brand/architecture.svg` / `brand/architecture-dark.svg` **only if** they depict the store schema — check first; if they do not, say so explicitly in the final report rather than editing them.

- [ ] **Step 1: Document the feature in README.md**

Add a section after the class-management docs:

````markdown
### Monorepo submissions: ingesting one assignment directory

Some courses keep every assignment in one repo and push the whole repo to
Gradescope at each submission. Point provgate at the directory that matters:

```bash
provgate assignment-scope set \
    --class cs61b-fa26 \
    --gs-assignment 409194023 \
    --assignment-id proj2
```

That derives the glob `proj2/**` and sends it with every ingest for that
Gradescope assignment, so `lab5/`, `proj1/` and the rest of the repo are not
ingested as extra submissions.

The derived glob matches a directory at the **repo root**. If the course nests
assignments (`src/proj2/`), pass `--path-glob 'src/proj2/**'` explicitly — the
glob grammar has no brace expansion, so no single pattern covers both layouts.

Assignments with no scope row are unaffected: they ingest exactly as before.
Watch the run summary's `skipped scopes:` line — `scope_excluded=<everything>`
with zero ingested means the glob did not match the layout.
````

- [ ] **Step 2: Note the new table in CLAUDE.md**

Add `assignment_scopes` to whatever store/schema listing CLAUDE.md keeps, with a one-line description.

- [ ] **Step 3: Run the full gate**

Run:
```bash
uv run pytest
uv run mypy --strict src
uv run ruff check .
uv run ruff format --check .
```
Expected: all four clean. Fix anything that is not.

- [ ] **Step 4: Commit**

```bash
git add README.md CLAUDE.md
git commit --no-gpg-sign -m "docs: document per-assignment ingest scoping"
```
