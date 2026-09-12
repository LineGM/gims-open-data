"""One cookie session, explicit redirects, no automatic POST retries."""

import math
import time
from urllib.parse import urljoin, urlsplit
from uuid import UUID

import httpx

from .models import SIMULATOR_URL
from .parse_html import marathon_form

ORIGIN = "https://digital.mchs.gov.ru"


class ImportStopped(RuntimeError):
    """Safe message: never includes a request URL, headers, body or secret."""


class MarathonClient:
    def __init__(self, delay: float = 1.0, *, transport=None, clock=None, sleep=None):
        if not math.isfinite(delay) or delay < 1:
            raise ValueError("delay must be finite and at least 1.0 seconds")
        self.delay = delay
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
        self.last_finished = None
        self.secrets: set[str] = set()
        self.http = httpx.Client(
            follow_redirects=False,
            timeout=httpx.Timeout(30, connect=15),
            transport=transport,
            headers={"User-Agent": "gims-open-data/0.1.0", "Accept": "*/*"},
        )

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.http.close()
        self.secrets.clear()

    def request(self, method: str, url: str, *, retry_get: bool = False, **kwargs):
        if not self.same_origin(url):
            raise ImportStopped("Refused request outside the official HTTPS origin")
        for attempt in range(3 if method == "GET" and retry_get else 1):
            if self.last_finished is not None:
                self.sleep(max(0, self.delay - (self.clock() - self.last_finished)))
            try:
                response = self.http.request(method, url, **kwargs)
            except httpx.TransportError:
                if method == "GET" and retry_get and attempt < 2:
                    self.sleep(2**attempt)
                    continue
                message = (
                    "POST result is uncertain; request was NOT retried. Start a new run."
                    if method == "POST"
                    else "GET failed; import stopped."
                )
                raise ImportStopped(message) from None
            finally:
                self.last_finished = self.clock()
                self.secrets.update(cookie.value for cookie in self.http.cookies.jar)
            if response.status_code >= 400:
                raise ImportStopped(f"HTTP {response.status_code}; import stopped without retry")
            return response
        raise AssertionError("unreachable")

    @staticmethod
    def same_origin(url: str) -> bool:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname == "digital.mchs.gov.ru"
            and parsed.port in {None, 443}
            and parsed.username is None
            and parsed.password is None
        )

    def create_marathon(self) -> tuple[str, UUID, bytes]:
        response = self.request("GET", SIMULATOR_URL, retry_get=True)
        if response.status_code != 200:
            raise ImportStopped("Unexpected simulator response")
        action, form = marathon_form(response.content)
        self.secrets.add(form["gims_simulator_form[_token]"])
        url = urljoin(SIMULATOR_URL, action)
        if url != SIMULATOR_URL:
            raise ImportStopped("Simulator form action changed")
        response = self.request("POST", url, data=form)
        # Creation POST may never be replayed by 307/308 handling.
        if response.status_code not in {301, 302, 303}:
            raise ImportStopped("Marathon creation did not return an expected redirect")
        for _ in range(10):
            location = response.headers.get("location")
            if not location:
                raise ImportStopped("Missing server Location redirect")
            url = urljoin(str(response.url), location)
            path = urlsplit(url).path
            if "/activate/" in path or "/start/" in path:
                self.secrets.add(path.rsplit("/", 1)[-1])
            # Activation/start GETs also mutate state: do not retry them.
            response = self.request("GET", url)
            if response.status_code == 200:
                break
            if response.status_code not in {301, 302, 303, 307, 308}:
                raise ImportStopped("Unexpected redirect response")
        else:
            raise ImportStopped("Too many server redirects")
        path = urlsplit(url).path
        prefix = "/testing/instance/"
        if not path.startswith(prefix) or urlsplit(url).query:
            raise ImportStopped("Creation did not reach an instance page")
        try:
            instance = UUID(path.removeprefix(prefix))
        except ValueError:
            raise ImportStopped("Invalid instance URL") from None
        return url, instance, response.content

    def answer(self, instance: UUID, correct_ids: list[UUID]) -> bytes:
        if not correct_ids:
            raise ImportStopped("Refused to submit an empty answer")
        values = [str(value) for value in correct_ids]
        response = self.request(
            "POST",
            f"{ORIGIN}/testing/api/instance/{instance}",
            json={"answer": values[0] if len(values) == 1 else values},
        )
        if response.status_code != 200:
            raise ImportStopped("Unexpected answer response; POST was NOT retried")
        return response.content
