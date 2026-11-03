"""Start the installed package in a child process and exercise real loopback HTTP."""

import json
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
        assert schema["paths"] == {}
        with opener.open(url + "/docs", timeout=2) as response:
            assert response.status == 200
        try:
            opener.open(url + "/synthetic-private-marker?token=synthetic-private-marker", timeout=2)
            raise AssertionError("Unexpected bootstrap business route.")
        except urllib.error.HTTPError as error:
            assert error.code == 404
            error.close()
    finally:
        process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=5)
    assert "synthetic-private-marker" not in stdout + stderr
