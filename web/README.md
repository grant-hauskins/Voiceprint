# Local room console

Serve this directory through the Java API at `http://127.0.0.1:8080/ui`.
There is no build step. Do not expose port 8080 or this directory through a tunnel.
The existing Cloudflare tunnel remains exclusively for hosted MCP on port 8082.

Before collecting releases, configure the API's controller name, address, email,
and operator token. The notice is read from `/privacy/notice`; this page never
invents an entity or policy. Enter the operator token in the page. It is kept in
memory and cleared on disconnect/navigation, and must not be shared as a
participant credential.

Use the exact room and participant IDs from the runtime. Open the pending room,
or create it here with each person's full name and unverified email/phone.
Each participant personally reviews the notice, types their full name, checks
the written release and optional disclosure scopes, and submits. Nothing is
checked by default. A one-use server challenge binds the submission to that
participant and notice. No browser audio is captured. The runtime may begin the
eight-second statement only after every written release has been committed.
Withdrawal ends authorization for the entire room; displayed destruction state
does not infer successful erasure from a request acknowledgement.

API consent status `scopes` must contain effective booleans including vendor
review controls. Speak, Hold, Cancel and eagerness controls target only the
runtime whose session matches the selected room and whose effective scopes
permit hosted operation. The server remains authoritative on every operation.

The MCP panel displays the API's persisted evidence, not claims made by an
agent. Its agent label is caller declared. Human labels are similarity based
and uncalibrated. Correction controls remain deferred. Protected views clear
on withdrawal, ending, disconnection or an unavailable consent check. The
browser renders returned text as text, not markup.

Run `python -m unittest discover -s web -p "test_*.py"`. Node is used only by the
test suite to execute production JavaScript with a small deterministic DOM and
fake API. All fixtures are authored synthetic text, with no real audio or
participant data. These tests establish rendering and client gates, not a
browser security boundary or legal sufficiency. Real Java API/browser/runtime
integration and human live behavior must be verified separately after merge.

For the optional headless Chrome check, set `VOICEPRINT_BROWSER_SMOKE=1` before
running the same suite. It loads the real page, signs a synthetic release,
checks four rendered transcript rows and two MCP calls, both agents' controls,
deduplication, and safe text rendering. All API requests are intercepted by a
test-only fixture; no real runtime, microphone, provider or tunnel is contacted.

The loopback notice is not a substitute for publishing the operator's public
retention policy. Individual accounts, verified identity/contact and legally
authorized representative signing are deferred; the page must not be used
when those unsupported capabilities are needed.
