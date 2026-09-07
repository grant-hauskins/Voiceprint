r"""One-window launcher: worker, API, MCP tunnel and the GUI-driven runtime, then the browser.

Started by Voiceprint.cmd / `scripts\dev.ps1 up`, which resolve the JDK, provision the local credentials and
export the controller identity. Everything else the operator needs (provider key, roster, releases, enrollment,
start, controls) happens at http://127.0.0.1:8080/ui. Nothing here writes a provider key to disk.

Environment (all optional): VOICEPRINT_JAVA, VOICEPRINT_PORT (8080), VOICEPRINT_MCP_PORT (8082),
VOICEPRINT_WORKER_PORT (8091), VOICEPRINT_CONTROL_PORT (8090 or next free), VOICEPRINT_MCP_URL (skip the
tunnel), VOICEPRINT_DEVICE (microphone preference), OPENAI_API_KEY (otherwise entered in the GUI).
"""
import argparse
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TUNNEL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def listening(port, host="127.0.0.1"):
    with socket.socket() as probe:
        probe.settimeout(.3)
        return probe.connect_ex((host, port)) == 0


def free_port(preferred, limit=20):
    """First port at or after `preferred` that nothing is bound to on any local address (8090 is often taken)."""
    for port in range(preferred, preferred + limit):
        if listening(port):
            continue
        try:
            for address in ("127.0.0.1", "0.0.0.0"):
                with socket.socket() as probe:
                    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                        probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                    probe.bind((address, port))
            return port
        except OSError:
            continue
    raise RuntimeError(f"No free port in {preferred}-{preferred + limit - 1}")


def tunnel_url(line):
    match = TUNNEL_RE.search(line)
    return match.group(0) + "/mcp" if match else None


def find_cloudflared():
    found = shutil.which("cloudflared")
    if found:
        return found
    for candidate in (Path(os.environ.get("ProgramFiles", "")) / "cloudflared" / "cloudflared.exe",
                      Path(os.environ.get("ProgramFiles(x86)", "")) / "cloudflared" / "cloudflared.exe",
                      Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / "cloudflared.exe"):
        if candidate.is_file():
            return str(candidate)
    return None


def wait_for(predicate, timeout, what):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.5)
    raise RuntimeError(f"Timed out after {timeout:.0f}s waiting for {what}")


def health(url, token=None):
    """True once the API answers /health; it requires the operator bearer, so a wrong token also reports why."""
    request = urllib.request.Request(url, headers={"Authorization": "Bearer " + token} if token else {})
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status == 200
    except urllib.error.HTTPError as error:
        if error.code == 401:
            raise RuntimeError("The API on this port rejects this launcher's VOICEPRINT_API_TOKEN; stop the other API or use its token") from None
        return False
    except Exception:
        return False


class Child:
    """A child process whose output is echoed with a prefix; `watch` sees each line first."""

    def __init__(self, name, args, env=None, cwd=ROOT, stdin=subprocess.DEVNULL, watch=None):
        self.name, self.watch = name, watch
        self.process = subprocess.Popen(args, cwd=str(cwd), env=env, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.thread = threading.Thread(target=self.pump, daemon=True)
        self.thread.start()

    def pump(self):
        for raw in iter(self.process.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if self.watch:
                self.watch(line)
            print(f"[{self.name}] {line}", flush=True)
        self.process.stdout.close()

    def alive(self):
        return self.process.poll() is None

    def stop(self, grace=15):
        if not self.alive():
            return
        try:
            self.process.terminate()
            self.process.wait(grace)
        except Exception:
            try:
                self.process.kill()
            except Exception:
                pass


def python():
    venv = ROOT / ".venv" / "Scripts" / "python.exe"
    return str(venv) if venv.exists() else sys.executable


def java():
    for candidate in (os.environ.get("VOICEPRINT_JAVA"), os.environ.get("JAVA_HOME") and str(Path(os.environ["JAVA_HOME"]) / "bin" / "java.exe")):
        if candidate and Path(candidate).is_file():
            return candidate
    raise RuntimeError("JDK 21 not found: start through scripts\\dev.ps1 up, or set VOICEPRINT_JAVA")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-tunnel", action="store_true", help="do not start cloudflared (needs VOICEPRINT_MCP_URL)")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--control-port", type=int, default=int(os.environ.get("VOICEPRINT_CONTROL_PORT", "0") or 0))
    parser.add_argument("--device", default=os.environ.get("VOICEPRINT_DEVICE"), help="microphone preference passed to the runtime")
    parser.add_argument("--once", action="store_true", help="exit when the first conversation ends instead of offering another")
    args = parser.parse_args(argv)
    api_port = int(os.environ.get("VOICEPRINT_PORT", "8080"))
    mcp_port = int(os.environ.get("VOICEPRINT_MCP_PORT", "8082"))
    worker_port = int(os.environ.get("VOICEPRINT_WORKER_PORT", "8091"))
    api = f"http://127.0.0.1:{api_port}"
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", VOICEPRINT_PORT=str(api_port),
               VOICEPRINT_MCP_PORT=str(mcp_port), VOICEPRINT_WORKER_URL=f"http://127.0.0.1:{worker_port}")
    for name in ("VOICEPRINT_API_TOKEN", "VOICEPRINT_WORKER_TOKEN", "VOICEPRINT_MCP_TOKEN"):
        if not env.get(name):
            raise RuntimeError(f"{name} is not set: start through scripts\\dev.ps1 up, which provisions the local credentials")
    missing = [name for name in ("VOICEPRINT_CONTROLLER_NAME", "VOICEPRINT_CONTROLLER_ADDRESS", "VOICEPRINT_CONTROLLER_EMAIL") if not env.get(name)]
    if missing:
        raise RuntimeError("Controller identity is required before any release can be collected: " + ", ".join(missing))
    if env.get("VOICEPRINT_OPENAI_REVIEWED") != "true" or env.get("VOICEPRINT_CLOUDFLARE_REVIEWED") != "true":
        print("NOTE: VOICEPRINT_OPENAI_REVIEWED / VOICEPRINT_CLOUDFLARE_REVIEWED are not both 'true' in data\\launcher.env. "
              "Releases can be collected, but hosted agents stay blocked until the operator has reviewed those vendor settings.", flush=True)
    children, tunnel = [], {"url": os.environ.get("VOICEPRINT_MCP_URL")}
    run_dir = ROOT / "data" / "run" / f"launcher-{os.getpid()}"
    try:
        if listening(worker_port):
            print(f"Worker already listening on {worker_port}; reusing it (its VOICEPRINT_WORKER_TOKEN must match this launcher's).", flush=True)
        else:
            children.append(Child("worker", [python(), str(ROOT / "worker" / "worker.py"), "--port", str(worker_port)], env))
        if listening(api_port):
            print(f"API already listening on {api_port}; reusing it (its controller identity and operator token apply, not this launcher's).", flush=True)
        else:
            jar = ROOT / "target" / "voiceprint-0.1.0.jar"
            if not jar.exists():
                raise RuntimeError("target\\voiceprint-0.1.0.jar is missing: run scripts\\dev.ps1 build")
            run_dir.mkdir(parents=True, exist_ok=True)
            private = run_dir / "voiceprint.jar"       # a rebuild under a running JVM breaks lazy class loading
            shutil.copyfile(jar, private)
            children.append(Child("api", [java(), "-jar", str(private)], env))
        wait_for(lambda: health(api + "/health", env["VOICEPRINT_API_TOKEN"]), 90, f"the API on {api_port}")
        if not tunnel["url"]:
            if args.no_tunnel:
                raise RuntimeError("--no-tunnel needs VOICEPRINT_MCP_URL, the public https URL ending /mcp that OpenAI can reach")
            cloudflared = find_cloudflared()
            if not cloudflared:
                raise RuntimeError("Install cloudflared (winget install --id Cloudflare.cloudflared) or set VOICEPRINT_MCP_URL")

            def watch(line):
                found = tunnel_url(line)
                if found and not tunnel["url"]:
                    tunnel["url"] = found
            children.append(Child("tunnel", [cloudflared, "tunnel", "--url", f"http://127.0.0.1:{mcp_port}"], env, watch=watch))
            wait_for(lambda: bool(tunnel["url"]), 60, "the cloudflared quick tunnel URL")
        print(f"Hosted MCP for OpenAI: {tunnel['url']} (port {mcp_port} only; the API and GUI are never tunnelled)", flush=True)
        control_port = args.control_port or free_port(8090)
        first = True
        while True:
            command = [python(), str(ROOT / "scripts" / "agent_runtime.py"), "--gui", "--api", api, "--mcp-url", tunnel["url"], "--control-port", str(control_port)]
            if args.device:
                command += ["--device", args.device]
            runtime = Child("runtime", command, env, stdin=None)
            children.append(runtime)
            wait_for(lambda: listening(control_port) or not runtime.alive(), 30, "the runtime control port")
            if not runtime.alive():
                raise RuntimeError("The runtime stopped before its control port opened; see the [runtime] lines above")
            url = f"{api}/ui" + (f"?control={control_port}" if control_port != 8090 else "")
            print(f"Open {url}  (this page connects to the runtime on its own)", flush=True)
            if first and not args.no_browser:
                webbrowser.open(url)
            first = False
            while runtime.alive():
                time.sleep(.5)
            children.remove(runtime)
            code = runtime.process.returncode
            print(f"Runtime exited ({'ended' if code == 0 else f'code {code}'}).", flush=True)
            if args.once:
                break
            print("Press Enter to start another conversation in the same services, or Ctrl+C to stop everything.", flush=True)
            input()
    except KeyboardInterrupt:
        print("Stopping.", flush=True)
    finally:
        for child in reversed(children):
            child.stop()
        shutil.rmtree(run_dir, ignore_errors=True)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        main()
    except RuntimeError as error:
        print(f"Launcher stopped: {error}", file=sys.stderr, flush=True)
        sys.exit(1)
