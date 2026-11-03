"""Start the installed package in a child process and exercise real loopback HTTP."""

import json
import http.client
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request


def test_real_http_startup_and_private_query_not_in_access_logs():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {
        **os.environ,
        "APP_ENV": "development", "APP_HOST": "127.0.0.1", "APP_PORT": str(port),
        "APP_MAX_REQUEST_BODY_BYTES": "64",
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "enterprise_ai_platform", "--no-env-file"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 12
        while True:
            assert process.poll() is None, "API exited before accepting requests."
            try:
                with opener.open(url + "/openapi.json", timeout=0.5) as response:
                    schema = json.load(response)
                break
            except (urllib.error.URLError, TimeoutError):
                assert time.monotonic() < deadline, "API startup exceeded deadline."
                time.sleep(0.05)
        assert schema["info"]["version"] == "0.1.0"
        assert set(schema["paths"]) == {"/health/live", "/health/ready"}
        with opener.open(url + "/docs", timeout=2) as response:
            assert response.status == 200
        with opener.open(url + "/health/live", timeout=2) as response:
            assert json.load(response) == {"status": "ok", "scope": "process"}
            assert response.headers["X-Request-ID"]
        with opener.open(url + "/health/ready", timeout=2) as response:
            assert json.load(response) == {"status": "ready", "scope": "bootstrap"}
        try:
            opener.open(url + "/synthetic-private-marker?token=synthetic-private-marker", timeout=2)
            raise AssertionError("Unexpected bootstrap business route.")
        except urllib.error.HTTPError as error:
            assert error.code == 404
            body = json.load(error)
            assert body["error"]["request_id"] == error.headers["X-Request-ID"]
            assert body["error"]["code"] == "not_found"
            error.close()
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            connection.request("POST", "/missing", body=b"x" * 64)
            response = connection.getresponse()
            assert response.status == 404
            response.read()
            connection.request("POST", "/missing", body=b"x" * 65)
            response = connection.getresponse()
            assert response.status == 413
            data = json.loads(response.read())
            assert data["error"]["request_id"] == response.getheader("X-Request-ID")
        finally:
            connection.close()
        chunked = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            chunked.request("POST", "/missing", body=iter([b"x" * 40, b"x" * 25]), encode_chunked=True)
            response = chunked.getresponse()
            assert response.status == 413
            assert json.loads(response.read())["error"]["code"] == "body_too_large"
        finally:
            chunked.close()
    finally:
        process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=5)
    assert "synthetic-private-marker" not in stdout + stderr
    assert "request_complete request_id=" in stderr
