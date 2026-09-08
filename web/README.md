# Local room console

Serve this directory through the Java API at `http://127.0.0.1:8080/ui`.
The page has two views. Setup, the collection notice, each person's release and
enrollment recording live in a dialog that cannot be dismissed until the room is
enrolled (steps Room, Releases, Enrollment); afterwards *Releases* in the header
reopens it for review or withdrawal. Behind it, the console proper: a toolbar with
the microphone state and Start/End controls, and a grid of agent cards (a gradient
orb per agent that pulses while that agent speaks), room status, the transcript and
the server-recorded MCP evidence. The palette follows the operating system's light
or dark preference. Element IDs and client logic are shared by both views and by
the tests below.
There is no build step. Do not expose port 8080 or this directory through a tunnel.
The existing Cloudflare tunnel remains exclusively for hosted MCP on port 8082.

Before collecting releases, configure the API's controller name, address, email,
and operator token. The notice is read from `/privacy/notice`; this page never
invents an entity or policy. Enter the operator token in the page. It is kept in
memory and cleared on disconnect/navigation, and must not be shared as a
participant credential. When the runtime was started by the launcher
(`agent_runtime.py --gui`), the page fetches that token from the runtime's
loopback `/bootstrap` endpoint instead; only this page's exact origin is allowed
to read it, and a runtime started without `--gui` reveals nothing.

The **Conversation runtime** panel mirrors the runtime's phase (setup, consent,
enrollment, connecting, ready, live, ending, ended, failed). In setup it posts
the roster, contacts and, if the runtime has no key, the OpenAI API key to the
runtime's loopback control port; the key field is cleared immediately and the
runtime keeps it in memory only. The runtime then creates the pending room and
this page opens it. After every release, the panel offers one *Record* button at
a time for the participant the runtime is waiting on, shows peak levels and
rejections, then *Start conversation*, then *End conversation*. The control
port defaults to 8090; the launcher passes `?control=PORT` when it had to pick
another one.

Otherwise use the exact room and participant IDs from the runtime. Open the pending room,
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

## v3: negotiation rooms

Setup gains a **Conversation type** (casual or negotiation) and a **Role** per
agent card (voice or arbitrator). An arbitrator card hides the voice, eagerness
and speaks-for fields: that agent is text only, sees both objectives, reads the
transcript and the agent channel through our MCP server and posts to the notes
board. Before posting `/setup` the page mirrors the runtime's rule (a
negotiation is exactly two voice agents speaking for two different people plus
one arbitrator; a casual room has no arbitrator); the runtime remains the
authority.

The release form has a fourth checkbox, the optional `negotiation_text`
disclosure. Negotiation rooms need all three optional disclosures from everyone;
the API computes the effective scope and refuses objectives, the arbitrator's
vendor calls and the summary otherwise.

**Objectives** appear in the gate after the releases whenever the runtime says
the room is a negotiation (reopen with *Releases* to add a version). Each person
takes the keyboard in turn and types a shareable position plus labelled
constraint values, or loads a `.txt`/`.json` file that is parsed on this
computer into those fields; the file itself is never sent. Saving posts only the
fields; every save is a new server version (`trigger` is `initial` then
`edited in console`). After a save the block collapses, its inputs are cleared
and only "Objective v<n> recorded · <k> constraints" remains, so the next person
cannot read it. The page never reads objectives back. A 403 is shown as
"Every person must sign with the negotiation_text disclosure first."

The **Notes board** panel lists the arbitrator's board lines from the event
feed with the sender, time, a tag chip and an "n withheld" chip when the server
redacted registered values before storing the line. The **Raw agent channel**
beside it is polled with `tier=raw`; the API omits those rows and reports
`revealed:false` until every person in the room has clicked their own
*Reveal to <name>* button (each click posts that person's `revealed` flag from
this page's origin). While hidden the page shows no rows, keeps nothing and
restarts its cursor at zero, so a later reveal shows the whole stream. A
hidden response clears any rows already shown. Per-person button state is known
only from this page's own posts; after a reload every button reads *Reveal*
until clicked again.

Arbitrator cards show "Mediating" while the runtime reports it responding or
shortly after its generation count rises, "Paused" when held, otherwise
"Listening", with *Post now*, *Pause*/*Resume* and *Drop override* controls and
the same server-recorded MCP evidence line as voice agents.

When the runtime phase turns `ended` (and whenever a room is opened) the page
fetches the retained **Closing summary** once; 404 means nothing to show. The
panel shows the text, model, saved time and retention deadline, with a
*Delete summary* button that issues the DELETE. Attribution labels remain
uncalibrated; this build is a demo, not for consequential negotiations.
