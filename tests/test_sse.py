"""Server-Sent Events over a real Uvicorn server.

Deliberately not through ``TestClient``: it runs the application on a portal
belonging to the calling thread, so a long-lived streaming response cannot
interleave with concurrent requests, and every interesting SSE property is about
exactly that interleaving. The ``live_server`` fixture boots the real thing.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any

import pytest

from tests.paths import junit

pytestmark = pytest.mark.slow


class Listener:
    """Consumes an SSE stream in a background thread."""

    def __init__(self, url: str, headers: dict[str, str] | None = None) -> None:
        self.url = url
        self.headers = headers or {}
        self.lines: list[str] = []
        self.frames: list[dict[str, Any]] = []
        self.stop = threading.Event()
        self.ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        import httpx

        with (
            httpx.Client(timeout=20.0) as client,
            client.stream(
                "GET", f"{self.url}/api/v1/events/stream", headers=self.headers
            ) as response,
        ):
            assert response.status_code == 200
            self.content_type = response.headers["content-type"]
            self.cache_control = response.headers.get("cache-control", "")
            self.accel = response.headers.get("x-accel-buffering", "")
            block: list[str] = []
            for line in response.iter_lines():
                self.ready.set()
                self.lines.append(line)
                if line == "":
                    if block:
                        self.frames.append(self._parse(block))
                        block = []
                else:
                    block.append(line)
                if self.stop.is_set():
                    return

    @staticmethod
    def _parse(block: list[str]) -> dict[str, Any]:
        frame: dict[str, Any] = {}
        for line in block:
            if line.startswith(":"):
                frame["comment"] = line[1:].strip()
            elif line.startswith("event:"):
                frame["event"] = line[6:].strip()
            elif line.startswith("id:"):
                frame["id"] = line[3:].strip()
            elif line.startswith("data:"):
                frame["data"] = line[5:].strip()
            elif line.startswith("retry:"):
                frame["retry"] = line[6:].strip()
        return frame

    def __enter__(self) -> Listener:
        self._thread.start()
        self.ready.wait(timeout=10)
        time.sleep(0.15)
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop.set()
        self._thread.join(timeout=5)

    def wait_for(self, predicate: Any, timeout: float = 5.0) -> dict[str, Any] | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            for frame in list(self.frames):
                if predicate(frame):
                    return frame
            time.sleep(0.02)
        return None

    def events(self) -> list[dict[str, Any]]:
        out = []
        for frame in self.frames:
            if frame.get("event") == "test-hub" and frame.get("data"):
                out.append(json.loads(frame["data"])["event"])
        return out


def test_the_stream_sets_the_headers_a_proxy_must_respect(live_server: Any) -> None:
    with Listener(live_server.url) as listener:
        assert listener.content_type.startswith("text/event-stream")
        assert "no-cache" in listener.cache_control
        # Nginx buffers proxied responses by default, which turns a live stream
        # into one that arrives all at once, at the end.
        assert listener.accel == "no"


def test_the_stream_opens_with_hello_and_a_reconnect_hint(live_server: Any) -> None:
    with Listener(live_server.url) as listener:
        assert listener.wait_for(lambda frame: frame.get("event") == "hello")
        assert any("retry" in frame for frame in listener.frames)


def test_a_live_event_reaches_a_subscriber_within_two_seconds(live_server: Any) -> None:
    """Definition-of-done item 6, asserted rather than assumed."""
    with Listener(live_server.url) as listener, live_server.client() as client:
        sent_at = time.time()
        client.post("/api/v1/runs", json={"project_key": "storefront-web", "test_type": "e2e"})
        frame = listener.wait_for(
            lambda f: f.get("event") == "test-hub" and "run.queued" in (f.get("data") or ""),
            timeout=2.0,
        )
        elapsed = time.time() - sent_at
    assert frame is not None, "the event never arrived on the stream"
    assert elapsed < 2.0


def test_an_imported_report_streams_its_results(live_server: Any) -> None:
    with Listener(live_server.url) as listener, live_server.client() as client:
        with junit("simple.xml").open("rb") as handle:
            client.post(
                "/api/v1/imports",
                files={"file": ("simple.xml", handle, "application/xml")},
                data={"project": "payments-api"},
            )
        assert listener.wait_for(lambda f: "test.finished" in (f.get("data") or ""), timeout=5.0)


def test_every_event_frame_carries_a_complete_cloudevent(live_server: Any) -> None:
    with Listener(live_server.url) as listener, live_server.client() as client:
        run = client.post("/api/v1/runs", json={"project_key": "sse-check"}).json()
        listener.wait_for(lambda f: f.get("event") == "test-hub", timeout=5.0)
        events = listener.events()

    assert events, "no CloudEvent reached the stream"
    first = events[0]
    assert first["specversion"] == "1.0"
    assert first["runid"] == run["id"]
    assert uuid.UUID(first["id"])
    assert "workspaceid" in first
    assert isinstance(first["sequence"], int)
    assert first["type"].startswith("run.")


def test_frames_carry_an_id_so_a_browser_can_resume(live_server: Any) -> None:
    with Listener(live_server.url) as listener, live_server.client() as client:
        client.post("/api/v1/runs", json={"project_key": "p"})
        frame = listener.wait_for(lambda f: f.get("event") == "test-hub", timeout=5.0)
    assert frame is not None
    assert frame.get("id"), "no id: line — EventSource cannot resume without one"
    assert frame["id"].isdigit()


def test_an_idle_connection_receives_a_heartbeat(live_server: Any) -> None:
    """Configured to 0.05s by the settings fixture, so this stays fast."""
    with Listener(live_server.url) as listener:
        frame = listener.wait_for(lambda f: f.get("comment") == "heartbeat", timeout=5.0)
    assert frame is not None
    assert "id" not in frame, "a heartbeat must not move the client's Last-Event-ID"


def test_reconnecting_resumes_from_the_last_event_id(live_server: Any) -> None:
    with live_server.client() as client:
        client.post("/api/v1/runs", json={"project_key": "first"})

    # A client that saw nothing asks for everything it missed.
    with Listener(live_server.url, headers={"Last-Event-ID": "0"}) as listener:
        hello = listener.wait_for(lambda f: f.get("event") == "hello", timeout=5.0)
        assert hello is not None
        assert json.loads(hello["data"])["replaying"] is True
        replayed = listener.wait_for(lambda f: f.get("event") == "test-hub", timeout=5.0)
        assert replayed is not None, "the missed events were not replayed"


def test_a_fresh_connection_is_not_replayed(live_server: Any) -> None:
    with live_server.client() as client:
        client.post("/api/v1/runs", json={"project_key": "first"})
    with Listener(live_server.url) as listener:
        hello = listener.wait_for(lambda f: f.get("event") == "hello", timeout=5.0)
    assert hello is not None
    assert json.loads(hello["data"])["replaying"] is False


def test_a_garbage_last_event_id_is_treated_as_a_fresh_connection(live_server: Any) -> None:
    with Listener(live_server.url, headers={"Last-Event-ID": "not-a-number"}) as listener:
        hello = listener.wait_for(lambda f: f.get("event") == "hello", timeout=5.0)
    assert hello is not None
    assert json.loads(hello["data"])["replaying"] is False


def test_two_subscribers_both_receive_the_same_event(live_server: Any) -> None:
    with Listener(live_server.url) as one, Listener(live_server.url) as two:
        with live_server.client() as client:
            client.post("/api/v1/runs", json={"project_key": "fanout"})
        assert one.wait_for(lambda f: f.get("event") == "test-hub", timeout=5.0)
        assert two.wait_for(lambda f: f.get("event") == "test-hub", timeout=5.0)


def test_subscribers_are_counted_and_released(live_server: Any) -> None:
    with live_server.client() as client:
        assert client.get("/healthz").json()["live_subscribers"] == 0
        with Listener(live_server.url):
            deadline = time.time() + 3
            count = 0
            while time.time() < deadline:
                count = client.get("/healthz").json()["live_subscribers"]
                if count >= 1:
                    break
                time.sleep(0.05)
            assert count >= 1
