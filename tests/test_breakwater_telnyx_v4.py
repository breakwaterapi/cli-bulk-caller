"""Exercise CallService against a local fake Telnyx API with real telnyx objects."""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

REQUESTS = []
MISSING_CALL = "missing-call-id"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = {}
        path = self.path.split("?")[0]
        REQUESTS.append((path, body, self.headers.get("Authorization")))
        if path == "/v2/calls":
            self._send(200, {"data": {
                "call_control_id": "v3:abc123",
                "call_leg_id": "leg-1",
                "call_session_id": "sess-1",
                "is_alive": False,
                "record_type": "call",
            }})
        elif path.startswith("/v2/calls/") and path.endswith(
            ("/actions/playback_start", "/actions/record_start")
        ):
            if f"/{MISSING_CALL}/" in path:
                self._send(404, {"errors": [{
                    "code": "90018",
                    "title": "Call has already ended",
                    "detail": "The call is no longer active.",
                }]})
            else:
                self._send(200, {"data": {"result": "ok"}})
        else:
            self._send(404, {"errors": [{"code": "10005", "title": "Resource not found"}]})


_server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
threading.Thread(target=_server.serve_forever, daemon=True).start()
_url = f"http://127.0.0.1:{_server.server_address[1]}"

os.environ["TELNYX_BASE_URL"] = _url + "/v2"
os.environ["TELNYX_API_BASE"] = _url

import telnyx  # noqa: E402

telnyx.api_base = _url

from cli_bulk_caller.config import Settings  # noqa: E402
from cli_bulk_caller.exceptions import TelnyxAPIError  # noqa: E402
from cli_bulk_caller.models import CallStatus  # noqa: E402
from cli_bulk_caller.services.call_service import CallService  # noqa: E402


def _service():
    settings = Settings(
        telnyx_api_key="KEY_test_123",
        telnyx_connection_id="conn-1",
        telnyx_from_number="+15550001111",
        audio_url="https://example.com/audio.mp3",
        recording_format="mp3",
        recording_channels="single",
    )
    return CallService(settings)


def teardown_module(module):
    _server.shutdown()


def test_call_flow_against_fake_api():
    REQUESTS.clear()
    service = _service()

    call = service.initiate_call("+15552223333")
    assert call.call_control_id == "v3:abc123"
    assert call.to_number == "+15552223333"
    assert call.status == CallStatus.INITIATED
    assert service.get_call("v3:abc123") is call

    service.start_playback("v3:abc123")
    service.start_recording("v3:abc123")

    paths = [r[0] for r in REQUESTS]
    assert paths[0] == "/v2/calls"
    assert any(p.endswith("/actions/playback_start") for p in paths)
    assert any(p.endswith("/actions/record_start") for p in paths)
    assert all(r[2] == "Bearer KEY_test_123" for r in REQUESTS)

    dial_body = REQUESTS[0][1]
    assert dial_body["connection_id"] == "conn-1"
    assert dial_body["to"] == "+15552223333"
    assert "+15550001111" in dial_body.values()
    playback_body = next(r[1] for r in REQUESTS if r[0].endswith("/actions/playback_start"))
    assert playback_body["audio_url"] == "https://example.com/audio.mp3"
    record_body = next(r[1] for r in REQUESTS if r[0].endswith("/actions/record_start"))
    assert record_body["format"] == "mp3"
    assert record_body["channels"] == "single"


def test_api_errors_become_telnyx_api_error():
    service = _service()
    from cli_bulk_caller.models import Call

    service.active_calls[MISSING_CALL] = Call(
        call_control_id=MISSING_CALL,
        to_number="+15552223333",
        from_number="+15550001111",
        status=CallStatus.INITIATED,
    )
    with pytest.raises(TelnyxAPIError):
        service.start_playback(MISSING_CALL)
    with pytest.raises(TelnyxAPIError):
        service.start_recording(MISSING_CALL)
