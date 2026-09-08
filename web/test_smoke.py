"""Standard-library GUI smoke suite. Synthetic data only; no audio/provider calls."""
import functools
import http.server
import os
import pathlib
import shutil
import subprocess
import threading
import tempfile
import unittest
import urllib.request

WEB = pathlib.Path(__file__).resolve().parent


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class GuiSmoke(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("VOICEPRINT_BROWSER_SMOKE") == "1", "Opt-in real browser smoke; set VOICEPRINT_BROWSER_SMOKE=1")
    def test_real_browser_consent_and_replay(self):
        browser = shutil.which("chrome") or "C:/Program Files/Google/Chrome/Application/chrome.exe"
        self.assertTrue(pathlib.Path(browser).exists(), "Chrome is required for opt-in browser smoke")

        class BrowserHandler(QuietHandler):
            def do_GET(self):
                if self.path == "/ui/":
                    html = (WEB / "index.html").read_text(encoding="utf-8").replace(
                        '<script src="/ui/app.js" defer>',
                        '<script src="/ui/fixtures/browser-smoke.js" defer></script><script src="/ui/app.js" defer>')
                    encoded = html.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded)
                elif self.path.startswith("/ui/"):
                    self.path = self.path[3:]
                    super().do_GET()
                else:
                    self.send_error(404)

        handler = functools.partial(BrowserHandler, directory=str(WEB))
        with http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler) as server, tempfile.TemporaryDirectory(prefix="voiceprint-gui-browser-") as profile:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                args = [browser, "--headless=new", "--disable-gpu", "--disable-background-networking", "--disable-sync", "--no-first-run", "--no-default-browser-check", "--user-data-dir=" + profile, "--virtual-time-budget=12000", "--dump-dom", "http://127.0.0.1:" + str(server.server_port) + "/ui/"]
                result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", timeout=40)
                self.assertEqual(0, result.returncode, result.stderr[-2000:])
                marker = result.stdout[result.stdout.find('<output id="browser-smoke-result"'):]
                self.assertIn("PASS: written release, 4 transcript rows, 2 MCP calls", marker, marker[:1000] or result.stderr[-2000:])
            finally:
                server.shutdown()
                thread.join(timeout=5)

    def test_static_page_and_assets(self):
        handler = functools.partial(QuietHandler, directory=str(WEB))
        with http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                origin = "http://127.0.0.1:" + str(server.server_port)
                for name, expected in (("index.html", "Participant releases"), ("app.js", "class Feed"), ("style.css", "prefers-color-scheme")):
                    with urllib.request.urlopen(origin + "/" + name, timeout=5) as response:
                        self.assertEqual(200, response.status)
                        self.assertIn(expected, response.read().decode("utf-8"))
            finally:
                server.shutdown()
                thread.join(timeout=5)

    def test_production_renderer_and_consent_mock_api(self):
        executable = shutil.which("node")
        self.assertIsNotNone(executable, "Node is required for the production JavaScript renderer smoke test.")
        result = subprocess.run([executable, str(WEB / "test_dom.js")], capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("4 replay rows, 2 calls, 1 board row", result.stdout)
        self.assertIn("transcript review, retained voiceprints", result.stdout)

    def test_no_audio_or_persistent_credential_storage(self):
        source = (WEB / "app.js").read_text(encoding="utf-8")
        for forbidden in ("localStorage", "sessionStorage", "indexedDB", "getUserMedia", "MediaRecorder", "innerHTML", "eval("):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
