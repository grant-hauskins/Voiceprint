"""Synthetic worker boundary tests: no model loading or recorded human audio."""
import http.client
import os
import socket
import threading
import unittest
from unittest.mock import patch

from worker import create_server


class FakeModels:
    model_id = "synthetic"
    calls = 0

    def analyze(self, request):
        self.calls += 1
        return {"ok": True}

    match = analyze


class WorkerPrivacyTests(unittest.TestCase):
    def launch(self, token):
        models = FakeModels()
        with patch.dict(os.environ, {}, clear=True):
            server = create_server(models, 0, token)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server, models

    def post(self, server, token=None, origin=None, path="/analyze", send_body=True):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        if origin is not None:
            headers["Origin"] = origin
        if send_body:
            connection.request("POST", path, "{}", headers)
        else:
            # Do not send unauthorized PCM: admission must answer before reading the declared body.
            connection.putrequest("POST", path)
            for key, value in headers.items():
                connection.putheader(key, value)
            connection.putheader("Content-Length", "600000")
            connection.endheaders()
        response = connection.getresponse()
        response.read()
        status = response.status
        connection.close()
        return status

    def test_unconfigured_and_wrong_credentials_never_infer(self):
        server, models = self.launch(None)
        self.assertEqual(503, self.post(server, "anything", send_body=False))
        self.assertEqual(0, models.calls)
        secured, model2 = self.launch("internal-test")
        for endpoint in ("/analyze", "/match"):
            self.assertEqual(401, self.post(secured, path=endpoint, send_body=False))
            self.assertEqual(401, self.post(secured, "wrong", path=endpoint, send_body=False))
        self.assertEqual(0, model2.calls)

    def test_authorized_loopback_and_origin_rejection(self):
        server, models = self.launch("internal-test")
        self.assertEqual(200, self.post(server, "internal-test"))
        self.assertEqual(200, self.post(server, "internal-test", path="/match"))
        self.assertEqual(403, self.post(server, "internal-test", "http://127.0.0.1:8080"))
        self.assertEqual(2, models.calls)

    def test_rejects_before_reading_audio_body(self):
        server, models = self.launch("internal-test")
        with socket.create_connection(("127.0.0.1", server.server_port), timeout=2) as connection:
            connection.sendall((f"POST /analyze HTTP/1.1\r\nHost: 127.0.0.1:{server.server_port}\r\n"
                                "Content-Type: application/json\r\nContent-Length: 600000\r\n\r\n").encode())
            self.assertIn(b"401", connection.recv(1024).split(b"\r\n", 1)[0])
        self.assertEqual(0, models.calls)


if __name__ == "__main__":
    unittest.main()
