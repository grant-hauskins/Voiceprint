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

    def test_main_refuses_without_local_credentials(self):
        env = {k: v for k, v in dict(**__import__("os").environ).items() if not k.startswith("VOICEPRINT_")}
        result = subprocess.run([sys.executable, str(Path(launcher.__file__)), "--no-browser"], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertIn("VOICEPRINT_API_TOKEN is not set", result.stderr)


if __name__ == "__main__":
    unittest.main()
