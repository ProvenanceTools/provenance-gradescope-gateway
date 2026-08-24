import re

import httpx
import pytest
import respx

from provgate.provenance.client import JobStatus, ProvenanceClient, ProvenanceError
from provgate.store.models import IngestScope

BASE = "https://prov.example.edu/api/v1"


def make_client(
    *,
    chunk_threshold_bytes: int = 16 * 1024 * 1024,
    chunk_size_bytes: int = 16 * 1024 * 1024,
    part_max_attempts: int = 4,
) -> ProvenanceClient:
    # sleep is a no-op; monotonic advances so timeout logic is deterministic
    ticks = iter(range(0, 10_000, 1))
    return ProvenanceClient(
        httpx.Client(),
        poll_interval_s=0.0,
        poll_timeout_s=5.0,
        sleep=lambda _s: None,
        monotonic=lambda: float(next(ticks)),
        chunk_threshold_bytes=chunk_threshold_bytes,
        chunk_size_bytes=chunk_size_bytes,
        part_max_attempts=part_max_attempts,
    )


@respx.mock
def test_ingest_returns_job_handle() -> None:
    route = respx.post(f"{BASE}/semesters/sem-1/ingest:gradescope").mock(
        return_value=httpx.Response(202, json={"job_id": "job-9"})
    )
    handle = make_client().ingest_gradescope_export(BASE, "tok", "sem-1", b"zip-bytes")
    assert handle.job_id == "job-9"
    assert route.calls.last.request.headers["authorization"] == "Bearer tok"


@respx.mock
def test_ingest_non_202_raises() -> None:
    respx.post(f"{BASE}/semesters/sem-1/ingest:gradescope").mock(
        return_value=httpx.Response(403, json={"error": "forbidden"})
    )
    with pytest.raises(ProvenanceError):
        make_client().ingest_gradescope_export(BASE, "tok", "sem-1", b"z")


@respx.mock
def test_poll_until_terminal() -> None:
    url = f"{BASE}/semesters/sem-1/ingest/jobs/job-9"
    respx.get(url).mock(
        side_effect=[
            httpx.Response(200, json={"status": "running"}),
            httpx.Response(200, json={"status": "running"}),
            httpx.Response(200, json={"status": "succeeded"}),
        ]
    )
    status = make_client().poll_job(BASE, "tok", "sem-1", "job-9")
    assert status.status == "succeeded"
    assert status.is_success


@respx.mock
def test_poll_times_out() -> None:
    url = f"{BASE}/semesters/sem-1/ingest/jobs/job-9"
    respx.get(url).mock(return_value=httpx.Response(200, json={"status": "running"}))
    with pytest.raises(ProvenanceError):
        make_client().poll_job(BASE, "tok", "sem-1", "job-9")


@respx.mock
def test_poll_returns_failed_status() -> None:
    url = f"{BASE}/semesters/sem-1/ingest/jobs/job-9"
    respx.get(url).mock(return_value=httpx.Response(200, json={"status": "failed"}))
    status = make_client().poll_job(BASE, "tok", "sem-1", "job-9")
    assert status.status == "failed"
    assert status.is_success is False


@respx.mock
def test_verify_token_true_on_200() -> None:
    route = respx.get(f"{BASE}/me").mock(
        return_value=httpx.Response(200, json={"email": "a@b.edu"})
    )
    assert make_client().verify_token(BASE, "tok") is True
    assert route.calls.last.request.headers["authorization"] == "Bearer tok"


@respx.mock
def test_verify_token_false_on_401() -> None:
    respx.get(f"{BASE}/me").mock(return_value=httpx.Response(401))
    assert make_client().verify_token(BASE, "tok") is False


@respx.mock
def test_verify_token_false_on_403() -> None:
    respx.get(f"{BASE}/me").mock(return_value=httpx.Response(403))
    assert make_client().verify_token(BASE, "tok") is False


@respx.mock
def test_verify_token_raises_on_other_error() -> None:
    respx.get(f"{BASE}/me").mock(return_value=httpx.Response(500))
    with pytest.raises(ProvenanceError):
        make_client().verify_token(BASE, "tok")


@respx.mock
def test_ingest_non_json_body_raises_provenance_error() -> None:
    respx.post(f"{BASE}/semesters/sem-1/ingest:gradescope").mock(
        return_value=httpx.Response(
            202, content=b"not json", headers={"content-type": "text/plain"}
        )
    )
    with pytest.raises(ProvenanceError):
        make_client().ingest_gradescope_export(BASE, "tok", "sem-1", b"z")


_PARTS_RE = rf"{re.escape(BASE)}/semesters/sem-1/ingest/uploads/up-1/parts/\d+"


@respx.mock
def test_small_payload_uses_single_post_not_chunked() -> None:
    single = respx.post(f"{BASE}/semesters/sem-1/ingest:gradescope").mock(
        return_value=httpx.Response(202, json={"job_id": "job-1"})
    )
    uploads = respx.post(f"{BASE}/semesters/sem-1/ingest/uploads").mock(
        return_value=httpx.Response(201, json={})
    )
    handle = make_client(chunk_threshold_bytes=1000).ingest_gradescope_export(
        BASE, "tok", "sem-1", b"small"
    )
    assert handle.job_id == "job-1"
    assert single.called
    assert not uploads.called


@respx.mock
def test_large_payload_is_chunked_and_reassembles() -> None:
    payload = b"abcdefghij"  # 10 bytes
    respx.post(f"{BASE}/semesters/sem-1/ingest/uploads").mock(
        return_value=httpx.Response(
            201,
            json={
                "upload_id": "up-1",
                "s3_upload_id": "s3-1",
                "chunk_size": 4,
                "total_parts": 3,
            },
        )
    )
    parts = respx.put(url__regex=_PARTS_RE).mock(
        return_value=httpx.Response(200, json={"part_number": 1, "received": True})
    )
    complete = respx.post(f"{BASE}/semesters/sem-1/ingest/uploads/up-1/complete").mock(
        return_value=httpx.Response(202, json={"job_id": "job-42"})
    )

    handle = make_client(chunk_threshold_bytes=4, chunk_size_bytes=4).ingest_gradescope_export(
        BASE, "tok", "sem-1", payload
    )

    assert handle.job_id == "job-42"
    assert parts.call_count == 3
    # Parts, reassembled in part-number order, reproduce the original bytes.
    ordered = sorted(parts.calls, key=lambda c: int(c.request.url.path.rsplit("/", 1)[1]))
    assert b"".join(c.request.content for c in ordered) == payload
    # Every part carries the s3_upload_id and the auth header.
    assert parts.calls.last.request.url.params["s3_upload_id"] == "s3-1"
    assert parts.calls.last.request.headers["authorization"] == "Bearer tok"
    assert complete.calls.last.request.headers["authorization"] == "Bearer tok"


@respx.mock
def test_uses_server_returned_chunk_size_not_requested() -> None:
    payload = b"x" * 10
    respx.post(f"{BASE}/semesters/sem-1/ingest/uploads").mock(
        return_value=httpx.Response(
            201,
            json={
                "upload_id": "up-1",
                "s3_upload_id": "s3-1",
                "chunk_size": 5,  # server clamps/overrides our requested 4
                "total_parts": 2,
            },
        )
    )
    parts = respx.put(url__regex=_PARTS_RE).mock(return_value=httpx.Response(200, json={}))
    respx.post(f"{BASE}/semesters/sem-1/ingest/uploads/up-1/complete").mock(
        return_value=httpx.Response(202, json={"job_id": "job-7"})
    )

    make_client(chunk_threshold_bytes=4, chunk_size_bytes=4).ingest_gradescope_export(
        BASE, "tok", "sem-1", payload
    )

    # Honored the server's chunk_size=5 (=> 2 parts), not our requested 4 (=> 3 parts).
    assert parts.call_count == 2
    first = min(parts.calls, key=lambda c: int(c.request.url.path.rsplit("/", 1)[1]))
    assert len(first.request.content) == 5


@respx.mock
def test_part_retried_on_transient_failure_with_backoff() -> None:
    payload = b"abcd"  # one 4-byte part
    respx.post(f"{BASE}/semesters/sem-1/ingest/uploads").mock(
        return_value=httpx.Response(
            201,
            json={
                "upload_id": "up-1",
                "s3_upload_id": "s3-1",
                "chunk_size": 4,
                "total_parts": 1,
            },
        )
    )
    parts = respx.put(url__regex=_PARTS_RE).mock(
        side_effect=[
            httpx.Response(500),
            httpx.Response(500),
            httpx.Response(200, json={}),
        ]
    )
    respx.post(f"{BASE}/semesters/sem-1/ingest/uploads/up-1/complete").mock(
        return_value=httpx.Response(202, json={"job_id": "job-9"})
    )

    sleeps: list[float] = []
    client = ProvenanceClient(
        httpx.Client(),
        poll_interval_s=0.0,
        poll_timeout_s=5.0,
        sleep=sleeps.append,
        monotonic=lambda: 0.0,
        chunk_threshold_bytes=4,
        chunk_size_bytes=4,
        part_max_attempts=4,
    )

    handle = client.ingest_gradescope_export(BASE, "tok", "sem-1", payload)

    assert handle.job_id == "job-9"
    assert parts.call_count == 3  # two 500s, then success
    assert sleeps == [0.5, 1.0]  # exponential backoff after attempts 0 and 1


@respx.mock
def test_part_exhaustion_aborts_upload_and_raises() -> None:
    payload = b"abcd"
    respx.post(f"{BASE}/semesters/sem-1/ingest/uploads").mock(
        return_value=httpx.Response(
            201,
            json={
                "upload_id": "up-1",
                "s3_upload_id": "s3-1",
                "chunk_size": 4,
                "total_parts": 1,
            },
        )
    )
    respx.put(url__regex=_PARTS_RE).mock(return_value=httpx.Response(500))
    abort = respx.delete(f"{BASE}/semesters/sem-1/ingest/uploads/up-1").mock(
        return_value=httpx.Response(204)
    )

    with pytest.raises(ProvenanceError):
        make_client(
            chunk_threshold_bytes=4, chunk_size_bytes=4, part_max_attempts=2
        ).ingest_gradescope_export(BASE, "tok", "sem-1", payload)

    assert abort.called
    assert abort.calls.last.request.url.params["s3_upload_id"] == "s3-1"


@respx.mock
def test_complete_failure_aborts_upload_and_raises() -> None:
    payload = b"abcd"
    respx.post(f"{BASE}/semesters/sem-1/ingest/uploads").mock(
        return_value=httpx.Response(
            201,
            json={
                "upload_id": "up-1",
                "s3_upload_id": "s3-1",
                "chunk_size": 4,
                "total_parts": 1,
            },
        )
    )
    respx.put(url__regex=_PARTS_RE).mock(return_value=httpx.Response(200, json={}))
    respx.post(f"{BASE}/semesters/sem-1/ingest/uploads/up-1/complete").mock(
        return_value=httpx.Response(500)
    )
    abort = respx.delete(f"{BASE}/semesters/sem-1/ingest/uploads/up-1").mock(
        return_value=httpx.Response(204)
    )

    with pytest.raises(ProvenanceError):
        make_client(chunk_threshold_bytes=4, chunk_size_bytes=4).ingest_gradescope_export(
            BASE, "tok", "sem-1", payload
        )

    assert abort.called


@respx.mock
def test_create_upload_failure_raises_without_abort() -> None:
    payload = b"abcd"
    respx.post(f"{BASE}/semesters/sem-1/ingest/uploads").mock(return_value=httpx.Response(500))
    abort = respx.delete(url__regex=rf"{re.escape(BASE)}/semesters/sem-1/ingest/uploads/.*").mock(
        return_value=httpx.Response(204)
    )

    with pytest.raises(ProvenanceError):
        make_client(chunk_threshold_bytes=4, chunk_size_bytes=4).ingest_gradescope_export(
            BASE, "tok", "sem-1", payload
        )

    # No upload was created, so nothing to abort.
    assert not abort.called


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
            201,
            json={
                "upload_id": "u1",
                "s3_upload_id": "s3-1",
                "chunk_size": 4,
                "total_parts": 2,
            },
        )
    )
    respx.put(url__regex=r".*/parts/\d+").mock(return_value=httpx.Response(200))
    complete = respx.post("https://p.test/semesters/sem1/ingest/uploads/u1/complete").mock(
        return_value=httpx.Response(202, json={"job_id": "j1"})
    )

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
            201,
            json={
                "upload_id": "u1",
                "s3_upload_id": "s3-1",
                "chunk_size": 4,
                "total_parts": 2,
            },
        )
    )
    respx.put(url__regex=r".*/parts/\d+").mock(return_value=httpx.Response(200))
    complete = respx.post("https://p.test/semesters/sem1/ingest/uploads/u1/complete").mock(
        return_value=httpx.Response(202, json={"job_id": "j1"})
    )

    client = ProvenanceClient(httpx.Client(), chunk_threshold_bytes=4, chunk_size_bytes=4)
    client.ingest_gradescope_export("https://p.test", "tok", "sem1", b"12345678")

    body = _json.loads(complete.calls.last.request.content)
    assert "ingest_scope" not in body
