"""The real fetcher, against a local web server: what it sends, follows, keeps and refuses.

A request carries no cookie even when a redirect sets one, names this tool as its User-Agent, records
every redirect hop, honours Retry-After given as a date, notices a body shorter than announced,
stops at the size limit, and never follows a redirect to a private address. The host policy itself
refuses anything but https to a recorded CDN host.

Everything runs on 127.0.0.1; nothing leaves the machine.
"""
import email.utils
import http.server
import threading
import time

import pytest

from scripts import cloud_download as cd

SEEN = []


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        SEEN.append((self.path, dict(self.headers)))
        if self.path == "/ok":
            body = b"ENCRYPTED-BYTES"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("ETag", '"abc"')
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.send_header("Set-Cookie", "session=secret; Path=/")
            self.end_headers()
        elif self.path == "/busy":
            self.send_response(429)
            later = email.utils.formatdate(time.time() + 120, usegmt=True)
            self.send_header("Retry-After", later)
            self.end_headers()
        elif self.path == "/short":
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.write(b"only a little")
        elif self.path == "/big":
            body = b"x" * 5000
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/to-private":
            self.send_response(302)
            self.send_header("Location", "http://10.1.2.3/x")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture(scope="module")
def server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def fetcher():
    return cd.UrllibFetcher(cd.HostPolicy(("127.0.0.1",), allow_loopback=True), "Snapchat_Auto/test")


def test_a_redirect_is_recorded_and_its_cookie_is_not_sent_back(server, fetcher, tmp_path):
    SEEN.clear()
    resp = fetcher.fetch(server + "/redirect", timeout=5, max_bytes=1 << 20,
                         dest=str(tmp_path / "out.bin"))
    assert resp.status == 200 and resp.bytes == len(b"ENCRYPTED-BYTES")
    assert [hop["status"] for hop in resp.redirects] == [302]
    assert resp.final_url.endswith("/ok") and resp.headers["ETag"] == '"abc"'
    assert (tmp_path / "out.bin").read_bytes() == b"ENCRYPTED-BYTES"
    assert all("Cookie" not in headers for _path, headers in SEEN)
    assert all(headers.get("User-Agent") == "Snapchat_Auto/test" for _path, headers in SEEN)


def test_retry_after_as_a_date(server, fetcher, tmp_path):
    with pytest.raises(cd.FetchError) as caught:
        fetcher.fetch(server + "/busy", timeout=5, max_bytes=1 << 20, dest=str(tmp_path / "b"))
    assert caught.value.status == 429 and not caught.value.permanent
    assert 100 < caught.value.retry_after_s <= 121


def test_refused_short_and_too_big(server, fetcher, tmp_path):
    with pytest.raises(cd.FetchError) as caught:
        fetcher.fetch(server + "/gone", timeout=5, max_bytes=1 << 20, dest=str(tmp_path / "g"))
    assert caught.value.permanent and caught.value.status == 404
    with pytest.raises(cd.FetchError) as caught:
        fetcher.fetch(server + "/short", timeout=5, max_bytes=1 << 20, dest=str(tmp_path / "s"))
    assert "short body" in str(caught.value) and not caught.value.permanent
    with pytest.raises(cd.FetchError) as caught:
        fetcher.fetch(server + "/big", timeout=5, max_bytes=1000, dest=str(tmp_path / "x"))
    assert caught.value.permanent
    assert not list(tmp_path.glob("*.part"))


def test_a_redirect_to_a_private_address_is_refused(server, fetcher, tmp_path):
    with pytest.raises(cd.FetchError) as caught:
        fetcher.fetch(server + "/to-private", timeout=5, max_bytes=1 << 20, dest=str(tmp_path / "p"))
    assert "non-public" in str(caught.value) and caught.value.permanent
    assert [hop["status"] for hop in caught.value.redirects] == [302]


def test_the_default_policy():
    policy = cd.HostPolicy()
    assert "https" in policy.refuse("http://cf-st.sc-cdn.net/d/x", first=True)
    assert "not one of the recorded CDN hosts" in policy.refuse("https://example.org/x", first=True)
    assert "non-public" in (policy.refuse("https://127.0.0.1/x", first=False) or "")
    assert policy.refuse("s3://bucket/x", first=True)
