import json
from uuid import UUID

import httpx
import pytest

from gims_open_data.client import ImportStopped, MarathonClient
from gims_open_data.importer import run_import
from gims_open_data.models import SIMULATOR_URL
from gims_open_data.storage import save_raw

from .conftest import FIXTURES

INSTANCE = "00000000-0000-0000-0000-000000000099"
SECRET = "DO_NOT_PERSIST_SESSION_COOKIE"


def mock_client(initial, responses, calls, *, total=1513):
    initial = initial.replace(b"INSTANCE_A", INSTANCE.encode()).replace(
        b"1513", str(total).encode()
    )

    def handle(request):
        calls.append(request)
        path = request.url.path
        if path == "/gims/simulator" and request.method == "GET":
            return httpx.Response(
                200,
                content=(FIXTURES / "simulator-form.html").read_bytes(),
                headers={"set-cookie": f"PHPSESSID={SECRET}; Path=/"},
            )
        if path == "/gims/simulator":
            assert SECRET in request.headers["cookie"]
            assert b"marathon" in request.content
            return httpx.Response(302, headers={"location": "/testing/activate/SECRET_ACTIVATION"})
        if path == "/testing/activate/SECRET_ACTIVATION":
            return httpx.Response(
                302, headers={"location": "/testing/test/template/start/SECRET_START"}
            )
        if path == "/testing/test/template/start/SECRET_START":
            return httpx.Response(302, headers={"location": f"/testing/instance/{INSTANCE}"})
        if path == f"/testing/instance/{INSTANCE}":
            return httpx.Response(200, content=initial)
        assert path == f"/testing/api/instance/{INSTANCE}"
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    return MarathonClient(transport=httpx.MockTransport(handle), sleep=lambda _: None)


def test_smoke_limit_raw_checkpoint_and_no_last_post(tmp_path, initial, next_raw):
    calls = []
    with mock_client(initial, [httpx.Response(200, content=next_raw)], calls) as client:
        run, summary = run_import(client, root=tmp_path, limit=2)
    assert len(calls) == 6
    assert json.loads(calls[-1].content) == {"answer": "95902c7d-14ea-4950-a64c-044dece028a4"}
    assert (run / "raw/0002.json").read_bytes() == next_raw
    checkpoint = json.loads((run / "checkpoint.json").read_text())
    assert checkpoint["status"] == "limited"
    assert checkpoint["last_confirmed_position"] == 2
    assert checkpoint["instance_uuid"] == INSTANCE
    assert checkpoint["pending_answer_position"] is None
    assert summary["positions"] == 2 and not summary["complete"]
    for path in run.rglob("*"):
        if path.is_file():
            body = path.read_bytes()
            for secret in [SECRET, "REDACTED_CSRF_TOKEN", "SECRET_ACTIVATION", "SECRET_START"]:
                assert secret.encode() not in body
    assert INSTANCE not in (run / "normalized/questions.jsonl").read_text()


@pytest.mark.parametrize("status", [403, 429, 500, 503])
def test_stop_http_checkpoint_no_retry(tmp_path, initial, status):
    calls = []
    with mock_client(initial, [httpx.Response(status)], calls) as client:
        with pytest.raises(ImportStopped, match=f"HTTP {status}"):
            run_import(client, root=tmp_path, limit=3)
    assert len(calls) == 6
    checkpoint = json.loads(next(tmp_path.glob("*/checkpoint.json")).read_text())
    assert checkpoint["last_confirmed_position"] == 1
    assert checkpoint["status"] == "stopped_uncertain_post"


def test_post_timeout_no_retry(tmp_path, initial):
    calls = []
    with mock_client(initial, [httpx.ReadTimeout("sensitive URL")], calls) as client:
        with pytest.raises(ImportStopped, match="NOT retried"):
            run_import(client, root=tmp_path, limit=2)
    assert len(calls) == 6
    checkpoint = json.loads(next(tmp_path.glob("*/checkpoint.json")).read_text())
    assert checkpoint["pending_answer_position"] == 1
    assert "sensitive" not in json.dumps(checkpoint)


def test_malformed_response_is_saved_before_parse(tmp_path, initial):
    calls = []
    with mock_client(
        initial, [httpx.Response(200, content=b'{"unexpected":true}')], calls
    ) as client:
        with pytest.raises(ImportStopped):
            run_import(client, root=tmp_path, limit=2)
    assert next(tmp_path.glob("*/raw/0002.json")).read_bytes() == b'{"unexpected":true}'
    assert (
        json.loads(next(tmp_path.glob("*/checkpoint.json")).read_text())["last_confirmed_position"]
        == 1
    )


def test_full_uses_reported_total_and_leaves_final_unanswered(tmp_path, initial, next_raw):
    data = json.loads(next_raw)
    data["questions_count"] = 2
    calls = []
    with mock_client(initial, [httpx.Response(200, json=data)], calls, total=2) as client:
        run, summary = run_import(client, root=tmp_path, limit=None)
    assert summary["complete"] and summary["total_changed_from_1513"]
    assert len(calls) == 6
    assert json.loads((run / "checkpoint.json").read_text())["status"] == "complete"


def test_multiple_post_array_and_raw(tmp_path, initial, next_raw, caplog):
    data = json.loads(next_raw)
    data["current_question"]["type"] = "multiple"
    ids = [a["id"] for a in data["current_answers"][:2]]
    data["valid_answers"] = ",".join(ids)
    raw = json.dumps(data, indent=3).encode()
    third = (FIXTURES / "marathon-third-question.json").read_bytes()
    calls = []
    with (
        caplog.at_level("INFO"),
        mock_client(
            initial, [httpx.Response(200, content=raw), httpx.Response(200, content=third)], calls
        ) as client,
    ):
        run, summary = run_import(client, root=tmp_path, limit=3)
    assert summary["multiple"] == 1
    assert (run / "raw/0002.json").read_bytes() == raw
    assert json.loads(calls[-1].content) == {"answer": ids}
    assert "Multiple question at position 2" in caplog.text


def test_rate_limit_every_request_and_get_backoff():
    moment = [0.0]
    starts = []

    def sleep(seconds):
        moment[0] += seconds

    def handler(request):
        starts.append(moment[0])
        if len(starts) == 1:
            raise httpx.ConnectError("no connection")
        return httpx.Response(200)

    with MarathonClient(
        transport=httpx.MockTransport(handler), clock=lambda: moment[0], sleep=sleep
    ) as client:
        client.request("GET", SIMULATOR_URL, retry_get=True)
        client.request("GET", SIMULATOR_URL)
    assert len(starts) == 3
    assert all(b - a >= 1 for a, b in zip(starts, starts[1:], strict=False))


@pytest.mark.parametrize("status", [403, 429, 500])
def test_get_http_errors_not_retried(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status)

    with MarathonClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ImportStopped):
            client.request("GET", SIMULATOR_URL, retry_get=True)
    assert len(calls) == 1


def test_raw_secret_masking_does_not_reserialize(tmp_path):
    body = b'<input value="secret-csrf" name="csrf_token">\r\n  <p>Keep spacing</p>'
    path = tmp_path / "0001.html"
    metadata = save_raw(path, body, set())
    assert path.read_bytes() == body.replace(b"secret-csrf", b"REDACTED_SECRET")
    assert metadata["secrets_masked"]
    with pytest.raises(FileExistsError):
        save_raw(path, body, set())


def test_cross_origin_and_creation_post_replay_refused():
    with MarathonClient(transport=httpx.MockTransport(lambda _: httpx.Response(200))) as client:
        with pytest.raises(ImportStopped, match="outside"):
            client.request("GET", "https://example.com/secret")
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, content=(FIXTURES / "simulator-form.html").read_bytes())
        return httpx.Response(307, headers={"location": "/testing/activate/secret"})

    with MarathonClient(transport=httpx.MockTransport(handler), sleep=lambda _: None) as client:
        with pytest.raises(ImportStopped):
            client.create_marathon()
    assert len(calls) == 2


def test_empty_answer_never_submitted():
    with MarathonClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("unexpected request"))
    ) as c:
        with pytest.raises(ImportStopped):
            c.answer(UUID(INSTANCE), [])
