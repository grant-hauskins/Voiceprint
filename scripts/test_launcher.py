"""Launcher helpers: pure functions and process plumbing, no services, audio or provider."""
import socket
import subprocess
import sys
import unittest
from pathlib import Path

import launcher


class LauncherHelpersTest(unittest.TestCase):
    def test_tunnel_url_parsed_from_cloudflared_banner_only(self):
        line = "2026-09-07T10:00:00Z INF |  https://quick-brown-fox-jumps.trycloudflare.com                                   |"
        self.assertEqual(launcher.tunnel_url(line), "https://quick-brown-fox-jumps.trycloudflare.com/mcp")
        self.assertIsNone(launcher.tunnel_url("INF Requesting new quick Tunnel on trycloudflare.com..."))
        self.assertIsNone(launcher.tunnel_url("https://evil.example/mcp trycloudflare.com"))

    def test_free_port_skips_bound_ports(self):
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen(5)
            port = taken.getsockname()[1]
            self.assertTrue(launcher.listening(port))
            self.assertNotEqual(launcher.free_port(port, limit=5), port)
            self.assertGreater(launcher.free_port(port, limit=5), port)

    def test_wait_for_times_out_with_reason(self):
        with self.assertRaisesRegex(RuntimeError, "waiting for the thing"):
            launcher.wait_for(lambda: False, 0.6, "the thing")
        launcher.wait_for(lambda: True, 1, "immediate")

    def test_child_echoes_prefixed_lines_and_watch_sees_them(self):
        seen = []
        child = launcher.Child("probe", [sys.executable, "-c", "print('hello'); print('https://abc-def.trycloudflare.com')"],
                               cwd=Path(__file__).resolve().parent, watch=seen.append)
        child.process.wait(20)
        child.thread.join(5)
        self.assertEqual(child.process.returncode, 0)
        self.assertIn("hello", seen)
        self.assertEqual(launcher.tunnel_url(seen[-1]), "https://abc-def.trycloudflare.com/mcp")
        child.stop()

    def test_netstat_parse_and_config_mismatch(self):
        line = "  TCP    127.0.0.1:8080         0.0.0.0:0              LISTENING       60056"
        self.assertEqual(launcher.parse_netstat_line(line, {8080, 8091}), {8080: 60056})
        self.assertEqual(launcher.parse_netstat_line(line.replace("LISTENING", "ESTABLISHED"), {8080}), {})
        self.assertEqual(launcher.parse_netstat_line("  TCP    0.0.0.0:8090  0.0.0.0:0  LISTENING  22880", {8080}), {})
        env = {"VOICEPRINT_CONTROLLER_NAME": "Synthetic Controller", "VOICEPRINT_CONTROLLER_ADDRESS": "1 Test St", "VOICEPRINT_CONTROLLER_EMAIL": "c@example.invalid",
               "VOICEPRINT_OPENAI_REVIEWED": "true", "VOICEPRINT_CLOUDFLARE_REVIEWED": "true"}
        matching = {"controller_name": "Synthetic Controller", "controller_address": "1 Test St", "controller_email": "c@example.invalid",
                    "vendors": {"openai_reviewed": True, "cloudflare_reviewed": True}}
        self.assertIsNone(launcher.api_config_mismatch(matching, env))
        stale = dict(matching, vendors={"openai_reviewed": False, "cloudflare_reviewed": False})
        self.assertEqual(launcher.api_config_mismatch(stale, env), "openai_reviewed, cloudflare_reviewed")
        self.assertIn("controller_name", launcher.api_config_mismatch(dict(matching, controller_name="Someone Else"), env))

    @unittest.skipUnless(sys.platform == "win32", "Windows job objects")
    def test_children_die_with_the_launcher_job(self):
        import ctypes
        job = launcher.KillOnClose()
        self.assertIsNotNone(job.handle, "job object could not be created")
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            job.add(process)
            self.assertIsNone(process.poll())
            ctypes.WinDLL("kernel32").CloseHandle(job.handle)       # what happens when the launcher process ends
            process.wait(10)
            self.assertIsNotNone(process.returncode)
        finally:
            if process.poll() is None:
                process.kill()

    def test_mcp_tools_listed_reports_reasons(self):
        import http.server, json, threading
        answers = {"status": 200, "tools": ["get_transcript", "get_current_speaker", "list_sessions"]}

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                assert json.loads(body)["method"] == "tools/list"
                assert self.headers.get("Authorization") == "Bearer tok"
                payload = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": n} for n in answers["tools"]]}}).encode()
                self.send_response(answers["status"]); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload)

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/mcp"
            self.assertIsNone(launcher.mcp_tools_listed(url, "tok"))
            answers["tools"] = ["get_transcript"]
            self.assertIn("unexpected tools", launcher.mcp_tools_listed(url, "tok"))
            answers["status"] = 401
            self.assertEqual(launcher.mcp_tools_listed(url, "tok"), "HTTP 401")
        finally:
            server.shutdown(); server.server_close()
        self.assertEqual(launcher.mcp_tools_listed("http://127.0.0.1:1/mcp", "tok", timeout=2), "URLError")

    def test_main_refuses_without_local_credentials(self):
        env = {k: v for k, v in dict(**__import__("os").environ).items() if not k.startswith("VOICEPRINT_")}
        result = subprocess.run([sys.executable, str(Path(launcher.__file__)), "--no-browser"], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertIn("VOICEPRINT_API_TOKEN is not set", result.stderr)


if __name__ == "__main__":
    unittest.main()
