import base64
import json
from pathlib import Path

import httpx
import pytest

from gims_open_data.__main__ import main
from gims_open_data.client import ImportStopped
from gims_open_data.lock import SyncLock
from gims_open_data.media import MediaDownloader
from gims_open_data.snapshot import current_pointer, read_json, snapshot_path, verify_snapshot
from gims_open_data.sync import sync

from .test_importer import mock_client

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a9l8AAAAASUVORK5CYII="
)


def successful_sync(root, initial, next_raw, *, media=True, limit=None, handler=None):
    data = json.loads(next_raw)
    data["questions_count"] = 2
    calls = []
    result = sync(
        root,
        no_media=not media,
        limit=limit,
        client_factory=lambda: mock_client(
            initial, [httpx.Response(200, json=data)], calls, total=2
        ),
        media_factory=lambda: MediaDownloader(
            root,
            transport=httpx.MockTransport(
                handler
                or (
                    lambda _: httpx.Response(
                        200, content=PNG, headers={"content-type": "image/png", "etag": '"png-v1"'}
                    )
                )
            ),
            sleep=lambda _: None,
        ),
        sleep=lambda _: None,
    )
    return result, calls


def test_complete_promotion_and_offline_commands(tmp_path, initial, next_raw, capsys):
    result, calls = successful_sync(tmp_path, initial, next_raw)
    assert result["promoted"] and result["current_total"] == 2
    assert result["media_objects"] == 1 and result["media_bytes"] == len(PNG)
    pointer = current_pointer(tmp_path)
    path = snapshot_path(tmp_path, pointer["snapshot_id"])
    assert len(calls) == 6
    assert verify_snapshot(tmp_path, path)["complete"]
    assert len(list((path / "raw").iterdir())) == 2
    for command in ["status", "verify"]:
        assert main([command, "--output", str(tmp_path), "--json-summary"]) == 0
        assert json.loads(capsys.readouterr().out)
    assert main(["verify", "--snapshot", path.name, "--output", str(tmp_path)]) == 0


def test_failed_questions_keep_current_and_protocol_not_retried(tmp_path, initial, next_raw):
    successful_sync(tmp_path, initial, next_raw)
    before = (tmp_path / "state/current.json").read_bytes()
    calls = []
    with pytest.raises(ImportStopped):
        sync(
            tmp_path,
            client_factory=lambda: mock_client(
                initial, [httpx.Response(200, json={"new_schema": True})], calls, total=2
            ),
            sleep=lambda _: pytest.fail("protocol error must not retry"),
        )
    assert (tmp_path / "state/current.json").read_bytes() == before
    assert len(calls) == 6
    assert len(list((tmp_path / "snapshots").iterdir())) == 1
    assert any(
        p.read_bytes() == b'{"new_schema":true}' for p in tmp_path.glob("runs/**/raw/0002.json")
    )


def test_failed_media_keeps_current(tmp_path, initial, next_raw):
    successful_sync(tmp_path, initial, next_raw)
    before = (tmp_path / "state/current.json").read_bytes()
    with pytest.raises(ImportStopped):
        successful_sync(
            tmp_path,
            initial,
            next_raw,
            handler=lambda _: httpx.Response(
                200, content=b"<html>error</html>", headers={"content-type": "text/html"}
            ),
        )
    assert (tmp_path / "state/current.json").read_bytes() == before
    assert len(list((tmp_path / "snapshots").iterdir())) == 1


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("secret"), 429, 503])
def test_whole_run_transient_retry_fresh_client_no_post_replay(
    tmp_path, initial, next_raw, failure
):
    data = json.loads(next_raw)
    data["questions_count"] = 2
    calls_per_run = []
    clients = []

    def factory():
        calls = []
        calls_per_run.append(calls)
        response = failure if isinstance(failure, Exception) else httpx.Response(failure)
        if len(calls_per_run) > 1:
            response = httpx.Response(200, json=data)
        client = mock_client(initial, [response], calls, total=2)
        clients.append(client)
        return client

    waits = []
    result = sync(tmp_path, no_media=True, client_factory=factory, sleep=waits.append)
    assert result["promoted"] and len(clients) == 2 and clients[0] is not clients[1]
    assert [len(c) for c in calls_per_run] == [6, 6]
    assert waits == [30]
    assert len(list(tmp_path.glob("runs/*/failure.json"))) == 1


def test_keyboard_interrupt_no_restart(tmp_path, initial):
    calls = []

    class InterruptedClient:
        delay = 1.0
        secrets = set()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def create_marathon(self):
            calls.append(1)
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        sync(tmp_path, client_factory=InterruptedClient, sleep=lambda _: pytest.fail("restart"))
    assert calls == [1]
    assert not (tmp_path / "state/current.json").exists()
    assert not (tmp_path / "state/sync.lock").exists()
    assert read_json(next(tmp_path.glob("runs/*/failure.json")))["kind"] == "user_interruption"


def test_smoke_never_promotes_even_if_limit_covers_total(tmp_path, initial, next_raw):
    result, _ = successful_sync(tmp_path, initial, next_raw, limit=20)
    assert not result["promoted"]
    assert not (tmp_path / "state/current.json").exists()
    assert verify_snapshot(tmp_path, Path(result["run_path"]) / "snapshot")["complete"]


@pytest.mark.parametrize("target", ["questions", "media", "raw", "index"])
def test_verify_detects_corruption(tmp_path, initial, next_raw, target):
    result, _ = successful_sync(tmp_path, initial, next_raw)
    path = snapshot_path(tmp_path, result["snapshot_id"])
    if target == "questions":
        (path / "questions.jsonl").write_bytes(b"{}\n")
    elif target == "media":
        next((tmp_path / "media/objects").iterdir()).write_bytes(b"broken")
    elif target == "raw":
        (path / "raw/0001.html").write_bytes(b"broken")
    else:
        (tmp_path / "media/index.json").write_text(
            '{"schema_version":"1.0","urls":{},"versions":{}}'
        )
    with pytest.raises(ImportStopped):
        verify_snapshot(tmp_path, path)


def test_atomic_pointer_failure_rolls_back_candidate(tmp_path, initial, next_raw, monkeypatch):
    import gims_open_data.snapshot as module

    successful_sync(tmp_path, initial, next_raw)
    old = (tmp_path / "state/current.json").read_bytes()
    real_replace = module.os.replace

    def replace(source, target):
        if Path(target).name == "current.json":
            assert Path(source).name == "current.json.tmp"
            assert (tmp_path / "state/current.json").read_bytes() == old
            raise OSError("injected commit failure")
        return real_replace(source, target)

    monkeypatch.setattr(module.os, "replace", replace)
    with pytest.raises(ImportStopped):
        successful_sync(tmp_path, initial, next_raw)
    assert (tmp_path / "state/current.json").read_bytes() == old
    assert len(list((tmp_path / "snapshots").iterdir())) == 1


def test_lock_prevents_concurrent_and_stale_not_stolen(tmp_path):
    with SyncLock(tmp_path):
        body = (tmp_path / "state/sync.lock").read_bytes()
        assert json.loads(body)["pid"] > 0
        with pytest.raises(ImportStopped, match="lock"):
            with SyncLock(tmp_path):
                pytest.fail("second owner")
        assert (tmp_path / "state/sync.lock").read_bytes() == body
    (tmp_path / "state/sync.lock").write_text('{"pid":999999999}')
    with pytest.raises(ImportStopped, match="stale"):
        with SyncLock(tmp_path):
            pytest.fail("stale lock stolen")


def test_previous_immutable_snapshot_verifies_after_url_changes(tmp_path, initial, next_raw):
    first, _ = successful_sync(tmp_path, initial, next_raw)
    old_path = snapshot_path(tmp_path, first["snapshot_id"])
    old_files = {p.name: p.read_bytes() for p in old_path.iterdir() if p.is_file()}
    second, _ = successful_sync(
        tmp_path,
        initial,
        next_raw,
        handler=lambda _: httpx.Response(
            200, content=PNG + b"changed", headers={"content-type": "image/png", "etag": '"v2"'}
        ),
    )
    assert second["current_total"] == 2 and second["delta_total"] == 0
    assert verify_snapshot(tmp_path, old_path)["complete"]
    assert old_files == {p.name: p.read_bytes() for p in old_path.iterdir() if p.is_file()}
    diff = read_json(snapshot_path(tmp_path, second["snapshot_id"]) / "diff.json")
    assert len(diff["media_changed"]) == 2


def test_count_drift_inside_marathon_aborts(tmp_path, initial, next_raw):
    calls = []
    data = json.loads(next_raw)
    data["questions_count"] = 3
    with pytest.raises(ImportStopped, match="total changed"):
        sync(
            tmp_path,
            no_media=True,
            client_factory=lambda: mock_client(
                initial, [httpx.Response(200, json=data)], calls, total=2
            ),
            sleep=lambda _: pytest.fail("protocol restart"),
        )
    assert not (tmp_path / "state/current.json").exists()


def test_signal_immediately_after_pointer_commit_preserves_valid_target(
    tmp_path, initial, next_raw, monkeypatch
):
    import gims_open_data.snapshot as module

    original = module.atomic_json

    def interrupted(path, value):
        original(path, value)
        if path.name == "current.json":
            raise KeyboardInterrupt

    monkeypatch.setattr(module, "atomic_json", interrupted)
    result, _ = successful_sync(tmp_path, initial, next_raw)
    assert result["promoted"]
    assert verify_snapshot(tmp_path, snapshot_path(tmp_path, result["snapshot_id"]))["complete"]


def test_verify_named_snapshot_independent_of_broken_current(tmp_path, initial, next_raw):
    result, _ = successful_sync(tmp_path, initial, next_raw)
    (tmp_path / "state/current.json").write_bytes(b"corrupt")
    assert main(["verify", "--snapshot", result["snapshot_id"], "--output", str(tmp_path)]) == 0


def test_new_total_between_snapshots_is_normal(tmp_path, initial, next_raw):
    successful_sync(tmp_path, initial, next_raw, media=False)
    result = sync(
        tmp_path, no_media=True, client_factory=lambda: mock_client(initial, [], [], total=1)
    )
    assert result["promoted"] and result["previous_total"] == 2
    assert result["current_total"] == 1 and result["delta_total"] == -1
