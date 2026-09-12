import httpx
import pytest
from pydantic import ValidationError

from gims_open_data.client import ImportStopped
from gims_open_data.media import MediaDownloader, read_index
from gims_open_data.models import Resource

from .test_sync import PNG

URL = "https://digital.mchs.gov.ru:85/testing_bucket/test.png"


def test_first_download_then_conditional_304_and_cache(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                200,
                content=PNG,
                headers={
                    "content-type": "image/png",
                    "etag": '"v1"',
                    "last-modified": "Sat, 12 Sep 2026 08:00:00 GMT",
                },
            )
        assert request.headers["if-none-match"] == '"v1"'
        assert request.headers["if-modified-since"] == "Sat, 12 Sep 2026 08:00:00 GMT"
        return httpx.Response(304)

    with MediaDownloader(
        tmp_path, transport=httpx.MockTransport(handler), sleep=lambda _: None
    ) as d:
        first = d.download(URL)
    path = tmp_path / "media/objects" / first.sha256
    mtime = path.stat().st_mtime_ns
    with MediaDownloader(
        tmp_path, transport=httpx.MockTransport(handler), sleep=lambda _: None
    ) as d:
        second = d.download(URL)
    assert first.sha256 == second.sha256 and path.stat().st_mtime_ns == mtime
    assert len(calls) == 2 and read_index(tmp_path)["urls"][URL]["bytes"] == len(PNG)


def test_same_url_changed_bytes_keeps_both_objects(tmp_path):
    with MediaDownloader(
        tmp_path,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        ),
        sleep=lambda _: None,
    ) as d:
        first = d.download(URL)
    with MediaDownloader(
        tmp_path,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, content=PNG + b"new", headers={"content-type": "image/png"}
            )
        ),
        sleep=lambda _: None,
    ) as d:
        second = d.download(URL)
    assert first.sha256 != second.sha256
    assert len(read_index(tmp_path)["versions"][URL]) == 2


@pytest.mark.parametrize(
    "headers,body",
    [
        ({"content-type": "text/html"}, b"<html>oops</html>"),
        ({"content-type": "image/png"}, b"<html>oops</html>"),
        ({"content-type": "image/png", "content-length": "100000000"}, PNG),
        ({"content-type": "image/png"}, PNG * 100),
    ],
)
def test_invalid_type_signature_or_oversize(tmp_path, headers, body):
    with MediaDownloader(
        tmp_path,
        max_bytes=500,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body, headers=headers)),
        sleep=lambda _: None,
    ) as d:
        with pytest.raises(ImportStopped):
            d.download(URL)
    assert not list((tmp_path / "media/objects").iterdir())


@pytest.mark.parametrize(
    "url",
    [
        "http://digital.mchs.gov.ru:85/testing_bucket/a.png",
        "https://evil.example:85/testing_bucket/a.png",
        "https://digital.mchs.gov.ru:85/admin/a.png",
        "https://digital.mchs.gov.ru/testing_bucket/a.png",
        "https://digital.mchs.gov.ru:85/testing_bucket/../secret",
        "https://digital.mchs.gov.ru:85/testing_bucket/%2e%2e/secret",
        "https://digital.mchs.gov.ru:85/testing_bucket/%252e%252e/secret",
        "https://name:secret@digital.mchs.gov.ru:85/testing_bucket/a.png",
    ],
)
def test_invalid_foreign_resource_never_requested(tmp_path, url):
    with pytest.raises(ValidationError):
        Resource(url=url)
    with MediaDownloader(
        tmp_path, transport=httpx.MockTransport(lambda _: pytest.fail("network"))
    ) as d:
        with pytest.raises(ValidationError):
            d.download(url)


def test_media_redirect_refused(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://evil.example/image.png"})

    with MediaDownloader(tmp_path, transport=httpx.MockTransport(handler)) as d:
        with pytest.raises(ImportStopped, match="redirects refused"):
            d.download(URL)
    assert len(calls) == 1


def test_safe_media_get_retry(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503)
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    waits = []
    with MediaDownloader(tmp_path, transport=httpx.MockTransport(handler), sleep=waits.append) as d:
        assert d.download(URL).bytes == len(PNG)
    assert len(calls) == 2 and waits[0] == 2


def test_stream_without_content_length_is_bounded(tmp_path):
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield PNG * 1000

    with MediaDownloader(
        tmp_path,
        max_bytes=100,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=Stream(), headers={"content-type": "image/png"})
        ),
    ) as d:
        with pytest.raises(ImportStopped, match="maximum file size"):
            d.download(URL)
