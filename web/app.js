/* No storage, analytics, microphone API, or external assets. API enforces authority. */
(function (global) {
  "use strict";
  const ID = /^[A-Za-z0-9_-]{1,80}$/;
  const SAFE_LABELS = new Set(["high", "medium", "low", "unknown", "overlap", "agent"]);
  const terminal = state => ["revoked", "destroying", "destroyed", "legacy_blocked"].includes(state);
  const stamp = n => Number.isFinite(n) ? new Date(n).toLocaleTimeString() : "Time unavailable";
  const offset = n => Number.isFinite(n) ? `${Math.floor(n / 60000)}:${(n / 1000 % 60).toFixed(1).padStart(4, "0")}` : "—";
  function node(doc, tag, text, cls) {
    const el = doc.createElement(tag);
    if (text !== undefined) el.textContent = String(text);
    if (cls) el.className = cls;
    return el;
  }
  function allowedControls(consent, runtime, session) {
    return Boolean(consent && consent.allowed === true && !terminal(consent.state) &&
      consent.scopes && consent.scopes.openai_audio === true && consent.scopes.hosted_mcp === true &&
      runtime && runtime.session_id === session);
  }
  class Feed {
    constructor(doc, transcript, calls) { this.doc = doc; this.transcript = transcript; this.calls = calls; this.reset(); }
    reset() {
      this.cursor = 0; this.events = new Set(); this.utterances = new Set(); this.callIds = new Set();
      this.transcript.replaceChildren(node(this.doc, "li", "No utterances received.", "empty"));
      this.calls.replaceChildren(node(this.doc, "li", "No server calls received.", "empty"));
      this.lastCalls = new Map();
    }
    apply(page) {
      for (const event of Array.isArray(page.events) ? page.events : []) {
        if (!Number.isSafeInteger(event.event_id) || event.event_id <= 0) continue;
        this.cursor = Math.max(this.cursor, event.event_id);
        if (this.events.has(event.event_id)) continue;
        this.events.add(event.event_id);
        const data = event.data || {};
        if (event.type === "utterance" && data.utterance_id != null && !this.utterances.has(data.utterance_id)) {
          if (!this.utterances.size) this.transcript.replaceChildren();
          this.utterances.add(data.utterance_id);
          const label = SAFE_LABELS.has(data.label) ? data.label : "unknown";
          const row = node(this.doc, "li", undefined, label);
          const meta = node(this.doc, "div", undefined, "meta");
          meta.append(node(this.doc, "strong", data.speaker_name || data.speaker_id || "Unknown speaker"),
            node(this.doc, "span", `${offset(data.start_ms)}–${offset(data.end_ms)}`), node(this.doc, "span", label, "chip"));
          if (Number.isFinite(data.similarity)) meta.append(node(this.doc, "span", `similarity ${data.similarity.toFixed(3)}`));
          row.append(meta, node(this.doc, "p", data.text == null ? "Transcript omitted from this event." : data.text));
          this.transcript.append(row);
        } else if (event.type === "mcp_call" && data.call_id != null && !this.callIds.has(data.call_id)) {
          if (!this.callIds.size) this.calls.replaceChildren();
          this.callIds.add(data.call_id);
          const row = node(this.doc, "li");
          const meta = node(this.doc, "div", undefined, "meta");
          meta.append(node(this.doc, "span", stamp(data.timestamp_ms || event.timestamp_ms)),
            node(this.doc, "strong", data.tool || "Unknown tool"),
            node(this.doc, "span", data.participant_id ? `Declared caller: ${data.participant_id}` : "Unattributed caller"),
            node(this.doc, "span", `${Number.isFinite(data.bytes) ? data.bytes : "?"} bytes · ${data.failed ? "failed" : "completed"}`));
          row.append(meta, node(this.doc, "p", data.arguments == null ? "Arguments redacted" : JSON.stringify(data.arguments), "muted"));
          this.calls.append(row);
          if (data.participant_id) this.lastCalls.set(data.participant_id, data);
        }
      }
      if (Number.isSafeInteger(page.next_after_id)) this.cursor = Math.max(this.cursor, page.next_after_id);
    }
  }
  class Console {
    constructor(doc, fetcher, location) {
      this.doc = doc; this.fetch = fetcher; this.location = location;
      this.token = ""; this.session = ""; this.epoch = 0; this.notice = null; this.consent = null; this.runtime = null;
      this.consentKey = ""; this.agentKey = ""; this.polling = false; this.floor = null; this.busy = false;
      this.runtimeKey = ""; this.autoOpened = ""; this.pollingRuntime = false;
      // The launcher opens /ui?control=PORT when 8090 is busy on this computer; the origin stays the loopback API.
      const control = String(new URLSearchParams(location && location.search || "").get("control") || "");
      this.control = `http://127.0.0.1:${/^\d{2,5}$/.test(control) ? control : "8090"}`;
      this.feed = new Feed(doc, this.$("transcript"), this.$("mcp-calls"));
    }
    $(id) { return this.doc.getElementById(id); }
    say(message) { this.$("message").textContent = message; }
    async request(path, {body, runtime = false, publicRequest = false} = {}) {
      const headers = {Accept: "application/json"};
      if (!publicRequest) {
        if (!this.token) throw new Error("Enter the configured operator API token first.");
        headers.Authorization = `Bearer ${this.token}`;
      }
      if (body !== undefined) headers["Content-Type"] = "application/json";
      const response = await this.fetch((runtime ? this.control : "") + path,
        {method: body === undefined ? "GET" : "POST", headers, body: body === undefined ? undefined : JSON.stringify(body), cache: "no-store", credentials: "omit", redirect: "error", signal: AbortSignal.timeout(12000)});
      let data;
      try { data = await response.json(); } catch (_) { throw new Error(`Service returned an invalid response (${response.status}).`); }
      if (!response.ok) throw new Error(`${response.status}: ${data.message || data.error || "Request refused"}`);
      return data;
    }
    async loadNotice() {
      try {
        this.notice = await this.request("/privacy/notice", {publicRequest: true});
        const n = this.notice;
        const configured = n.configured === true && n.controller_name && n.controller_address && n.controller_email;
        this.noticeReady = Boolean(configured && n.notice_text && n.notice_sha256 && n.retention_text);
        this.$("configuration").textContent = this.noticeReady ? `Policy ${n.policy_version} · release ${n.consent_method_version}` : "Consent unavailable. Configure VOICEPRINT_CONTROLLER_NAME, VOICEPRINT_CONTROLLER_ADDRESS and VOICEPRINT_CONTROLLER_EMAIL on the API, with a current notice.";
        this.$("controller").textContent = [n.controller_name, n.controller_address, n.controller_email].filter(Boolean).join(" · ");
        this.$("notice-text").textContent = n.notice_text || "No configured notice. Do not collect audio.";
        this.$("retention").textContent = n.retention_text || "No retention policy available. Collection is blocked.";
        this.$("vendors").textContent = `Provider settings: ${JSON.stringify(n.vendors || {})}. Review flags record the operator's attestation; signed vendor terms are not verified by this page.`;
      } catch (e) {
        this.notice = null; this.noticeReady = false; this.$("configuration").textContent = `Notice unavailable: ${e.message}`;
      }
      this.$("create-room").disabled = !this.noticeReady || !this.token;
    }
    bind() {
      if (!["127.0.0.1", "localhost"].includes(this.location.hostname) || this.location.protocol !== "http:") {
        this.say("Open this console at http://127.0.0.1:8080/ui on the operator's computer.");
        for (const el of this.doc.querySelectorAll("button,input,select")) el.disabled = true;
        return;
      }
      const run = fn => async event => { event.preventDefault(); try { await fn(); } catch (e) { this.say(e.message); } };
      this.$("connect-form").addEventListener("submit", run(async () => {
        const token = this.$("token").value.trim(); this.disconnect(); this.token = token;
        await this.loadNotice(); await this.loadRooms(); this.$("connection").textContent = "Operator connected"; this.say("");
      }));
      this.$("disconnect").addEventListener("click", () => { this.disconnect(); this.$("token").value = ""; });
      this.$("refresh").addEventListener("click", run(() => this.loadRooms()));
      this.$("session-picker").addEventListener("change", run(() => this.open(this.$("session-picker").value)));
      this.$("open-form").addEventListener("submit", run(() => this.open(this.$("room-id").value.trim())));
      this.$("add-person").addEventListener("click", () => this.addPerson());
      this.$("create-form").addEventListener("submit", run(() => this.createRoom()));
      this.$("end-room").addEventListener("click", run(() => this.endRoom()));
      this.$("setup-add").addEventListener("click", () => this.addSetupPerson());
      this.$("setup-form").addEventListener("submit", run(() => this.submitSetup()));
      this.$("start").addEventListener("click", run(() => this.runtimeAction("/start")));
      this.$("stop-runtime").addEventListener("click", run(() => this.runtimeAction("/stop")));
      this.addPerson(); this.addPerson(); this.addSetupPerson(); this.addSetupPerson(); this.loadNotice();
      this.bootstrap().then(() => this.pollRuntime());
      this.timer = setInterval(() => { this.tickFloor(); this.poll(); this.pollRuntime(); }, 1500);
      global.addEventListener("pagehide", () => this.disconnect());
    }
    async bootstrap() {
      // A runtime started by the launcher (--gui) hands the operator token to this loopback page; otherwise paste it.
      try {
        const data = await this.request("/bootstrap", {runtime:true, publicRequest:true});
        if (data && typeof data.api_token === "string" && data.api_token) {
          this.disconnect(); this.token = data.api_token;
          await this.loadNotice(); await this.loadRooms(); this.$("connection").textContent = "Operator connected (launcher)"; this.say("");
        }
      } catch (_) { /* no launcher-started runtime: manual token entry remains available */ }
    }
    async pollRuntime() {
      if (!this.token || this.pollingRuntime) return;
      this.pollingRuntime = true;
      try {
        this.runtime = await this.request("/agents", {runtime:true});
      } catch (_) { this.runtime = null; }
      finally { this.pollingRuntime = false; }
      this.renderRuntime();
      const r = this.runtime;
      if (r && r.session_id && r.phase !== "setup" && r.session_id !== this.session && this.autoOpened !== r.session_id && ID.test(r.session_id)) {
        this.autoOpened = r.session_id;
        // The runtime reports the room a moment before the API has it; retry on the next tick instead of giving up.
        try { await this.loadRooms(); await this.open(r.session_id); } catch (e) { this.autoOpened = ""; this.say(e.message); }
      }
    }
    addSetupPerson() {
      const row = node(this.doc, "div", undefined, "person inline");
      for (const [field, title] of [["name", "Full name"], ["contact", "Email or phone (unverified)"]]) {
        const label = node(this.doc, "label", title); const input = node(this.doc, "input");
        input.name = field; input.required = true; input.maxLength = 200; input.autocomplete = "off"; label.append(input); row.append(label);
      }
      const remove = node(this.doc, "button", "Remove"); remove.type = "button"; remove.addEventListener("click", () => row.remove()); row.append(remove);
      this.$("setup-roster").append(row);
    }
    async submitSetup() {
      if (!this.runtime || this.runtime.phase !== "setup") throw new Error("The runtime is not waiting for setup.");
      const participants = [...this.$("setup-roster").children].map(row => Object.fromEntries(["name", "contact"].map(key => [key, row.querySelector(`[name="${key}"]`).value.trim()])));
      if (participants.length < 2 || participants.length > 4 || participants.some(p => !p.name || !p.contact)) throw new Error("Enter a full name and an email or phone for each of the two to four people within microphone range.");
      const body = {participants};
      const key = this.$("openai-key").value.trim(); this.$("openai-key").value = "";
      if (this.runtime.needs_openai_key) { if (!key) throw new Error("Paste the OpenAI API key."); body.openai_api_key = key; }
      await this.request("/setup", {runtime:true, body});
      this.say(""); await this.pollRuntime();
    }
    async recordParticipant(id) {
      await this.request("/enrollment/record", {runtime:true, body:{participant_id:id}}); await this.pollRuntime();
    }
    async runtimeAction(path) {
      await this.request(path, {runtime:true, body:{}}); await this.pollRuntime();
    }
    renderRuntime() {
      const r = this.runtime, key = JSON.stringify([this.token ? 1 : 0, r]);
      if (key === this.runtimeKey) return; this.runtimeKey = key;
      const phase = r ? (r.phase || "live") : null;
      this.$("phase").textContent = !this.token ? "Connect first" : !r ? `Runtime unavailable on ${this.control.slice(7)}` : phase;
      this.$("phase-detail").textContent = r && r.detail ? r.detail : !r && this.token ? "Start it with Voiceprint.cmd (or scripts\\dev.ps1 up) and this page will connect on its own." : "";
      this.$("setup-form").hidden = phase !== "setup";
      this.$("key-label").hidden = !(r && r.needs_openai_key);
      const people = r && r.participants || [];
      const enrollment = this.$("enrollment"); enrollment.hidden = !["enrollment", "connecting", "ready", "live"].includes(phase) || !people.length; enrollment.replaceChildren();
      for (const p of people) {
        const e = p.enrollment || {}, row = node(this.doc, "div", undefined, "person inline");
        row.append(node(this.doc, "strong", p.name), node(this.doc, "span", `${e.state || "pending"}${Number.isFinite(e.peak) ? ` · peak ${e.peak}${e.peak < 1500 ? " (too quiet)" : ""}` : ""}`, "chip"));
        if (phase === "enrollment") {
          const button = node(this.doc, "button", r.awaiting === p.id ? `Record ${p.name} now (8 s)` : "Waiting"); button.type = "button"; button.disabled = r.awaiting !== p.id;
          button.addEventListener("click", async () => { button.disabled = true; try { await this.recordParticipant(p.id); } catch (err) { this.say(err.message); } }); row.append(button);
        }
        enrollment.append(row);
      }
      this.$("start").hidden = phase !== "ready";
      this.$("stop-runtime").hidden = !r || ["ended"].includes(phase);
      this.$("stop-runtime").textContent = phase === "failed" ? "Dismiss failed runtime" : phase === "live" ? "End conversation & close microphone" : "Stop runtime";
    }
    disconnect() {
      this.token = ""; this.session = ""; this.epoch++; this.consent = null; this.runtime = null; this.consentKey = ""; this.agentKey = "";
      this.clearProtected(); this.$("consents").replaceChildren(); this.$("session-picker").replaceChildren(node(this.doc, "option", "Choose a room"));
      this.$("connection").textContent = "Disconnected"; this.$("consent-state").textContent = "No room selected";
      this.$("create-room").disabled = true; this.$("end-room").disabled = true;
      this.$("enrollment-help").textContent = ""; this.$("capture-status").textContent = "Microphone capture requires a current release from everyone.";
      this.$("destruction").textContent = ""; this.$("room-id").value = "";
      this.$("token").value = ""; this.$("new-room-id").value = ""; this.$("new-roster").replaceChildren(); this.addPerson();
      this.$("openai-key").value = ""; this.runtimeKey = ""; this.renderRuntime();
    }
    clearProtected() {
      this.feed.reset(); this.floor = null; this.$("participants").replaceChildren(); this.$("agents").replaceChildren();
      this.$("current").textContent = "Attribution unavailable"; this.$("similarity").hidden = true; this.$("floor").textContent = "Unknown";
      this.$("runtime-state").textContent = "Controls unavailable"; this.$("feed-state").textContent = "Waiting for authorization";
      this.$("call-count").textContent = "0"; this.$("utterance-count").textContent = "0"; this.agentKey = "";
    }
    addPerson() {
      const row = node(this.doc, "div", undefined, "person inline");
      for (const [field, title, value] of [["id", "Participant ID", `participant_${this.$("new-roster").children.length + 1}`], ["name", "Full name", ""], ["contact", "Email or phone (unverified)", ""]]) {
        const label = node(this.doc, "label", title); const input = node(this.doc, "input");
        input.name = field; input.value = value; input.required = true; input.maxLength = field === "id" ? 80 : 200;
        input.autocomplete = "off"; if (field === "id") input.pattern = ID.source.slice(1, -1);
        label.append(input); row.append(label);
      }
      const remove = node(this.doc, "button", "Remove"); remove.type = "button"; remove.addEventListener("click", () => row.remove()); row.append(remove);
      this.$("new-roster").append(row);
    }
    async createRoom() {
      if (!this.noticeReady) throw new Error("Controller configuration and notice are required.");
      const session_id = this.$("new-room-id").value.trim();
      const participants = [...this.$("new-roster").children].map(row => Object.fromEntries(["id", "name", "contact"].map(key => [key, row.querySelector(`[name="${key}"]`).value.trim()])));
      if (!ID.test(session_id) || !participants.length || participants.some(p => !ID.test(p.id) || !p.name || !p.contact) || new Set(participants.map(p => p.id)).size !== participants.length) throw new Error("Use a valid room ID and a distinct ID, full name, and contact for every participant.");
      await this.request("/privacy/rooms", {body: {session_id, participants, purpose_id: "live_conversation_v1"}});
      this.$("new-roster").replaceChildren(); this.$("new-room-id").value = ""; this.addPerson();
      await this.loadRooms(); await this.open(session_id);
    }
    async loadRooms() {
      const rooms = new Map();
      const responses = await Promise.allSettled([this.request("/speaker/sessions?limit=100"), this.request("/privacy/rooms")]);
      for (const r of responses) if (r.status === "fulfilled") {
        const items = r.value.sessions || r.value.rooms || [];
        for (const item of items) if (ID.test(item.session_id)) rooms.set(item.session_id, item);
      }
      if (responses.every(r => r.status === "rejected")) throw responses[0].reason;
      const picker = this.$("session-picker"); picker.replaceChildren(node(this.doc, "option", "Choose a room")); picker.children[0].value = "";
      for (const item of rooms.values()) { const option = node(this.doc, "option", `${item.session_id}${item.state ? ` · ${item.state}` : ""}`); option.value = item.session_id; picker.append(option); }
      picker.value = this.session;
    }
    async open(id) {
      if (!ID.test(id)) throw new Error("Enter the exact valid room ID.");
      this.epoch++; this.session = id; this.consent = null; this.consentKey = ""; this.runtime = null; this.clearProtected();
      this.$("consents").replaceChildren(); this.$("enrollment-help").textContent = ""; this.$("destruction").textContent = "";
      this.$("end-room").disabled = true; this.$("room-id").value = id; this.$("session-picker").value = id;
      await this.poll();
    }
    path(suffix, session = this.session) { return `/speaker/session/${encodeURIComponent(session)}/${suffix}`; }
    async poll() {
      if (!this.token || !this.session || this.polling) return;
      this.polling = true; const epoch = this.epoch; const session = this.session;
      try {
        const consent = await this.request(this.path("consent", session));
        if (epoch !== this.epoch) return;
        this.consent = consent; this.renderConsent();
        if (!consent.allowed || terminal(consent.state)) {
          this.clearProtected();
          if (["revoked", "destroying", "destroyed"].includes(consent.state)) await this.loadDestruction(epoch, session);
          return;
        }
        const results = await Promise.allSettled([this.request(this.path(`events?after_id=${this.feed.cursor}&limit=200&wait_ms=0`, session)), this.request(this.path("participants", session)), this.request(this.path("current", session)), this.request(this.path("floor", session)), this.request("/agents", {runtime:true})]);
        if (epoch !== this.epoch) return;
        // Recheck after parallel reads so withdrawal cannot restore a protected view.
        this.consent = await this.request(this.path("consent", session));
        if (epoch !== this.epoch) return;
        this.renderConsent();
        if (!this.consent.allowed || terminal(this.consent.state)) {
          this.clearProtected();
          if (["revoked", "destroying", "destroyed"].includes(this.consent.state)) await this.loadDestruction(epoch, session);
          return;
        }
        const [events, roster, current, floor, runtime] = results;
        if (events.status === "fulfilled") {
          this.feed.apply(events.value); this.$("feed-state").textContent = "Live · server feed";
          this.$("call-count").textContent = this.feed.callIds.size; this.$("utterance-count").textContent = this.feed.utterances.size;
        } else if (/^404: Session does not exist\.?$/.test(events.reason.message || "")) {
          // Releases authorize enrollment but deliberately do not create a speaker session.
          // Do not present that safe pre-capture state as a protected-feed failure.
          this.clearProtected();
          this.$("feed-state").textContent = "Awaiting runtime enrollment · microphone remains closed";
          this.$("runtime-state").textContent = "Start enrollment in the runtime after every participant has released.";
          return;
        } else { this.clearProtected(); this.say(`Protected feed unavailable: ${events.reason.message}`); return; }
        if (roster.status === "fulfilled") this.renderRoster(roster.value.participants || []);
        if (current.status === "fulfilled") this.renderCurrent(current.value);
        else { this.$("current").textContent = "Current attribution unavailable"; this.$("similarity").hidden = true; }
        if (floor.status === "fulfilled") { this.floor = {...floor.value, received_ms:Date.now()}; this.tickFloor(); }
        else { this.floor = null; this.$("floor").textContent = "Floor unavailable"; }
        this.runtime = runtime.status === "fulfilled" ? runtime.value : null; this.renderAgents();
      } catch (e) {
        if (epoch === this.epoch) { this.consent = null; this.clearProtected(); this.consentKey = ""; this.$("consents").replaceChildren(); this.$("end-room").disabled = true; this.$("capture-status").textContent = "Consent status unavailable. Collection and controls must remain stopped."; this.say(e.message); }
      } finally { this.polling = false; }
    }
    async loadDestruction(epoch, session) {
      try {
        const result = await this.request(this.path("destruction", session));
        if (epoch !== this.epoch) return;
        const items = (result.items || []).map(item => `${item.destination}: ${item.state}`).join(" · ");
        const failures = (result.failures || []).map(item => typeof item === "string" ? item : JSON.stringify(item)).join(" · ");
        this.$("destruction").textContent = `${result.verified === true ? "All registered destinations report verified destruction." : "Destruction incomplete; collection remains blocked."}${items ? ` ${items}.` : ""}${failures ? ` Failures: ${failures}` : ""}`;
      } catch (error) {
        if (epoch === this.epoch) this.$("destruction").textContent = `Destruction could not be verified: ${error.message}`;
      }
    }
    renderConsent() {
      const c = this.consent;
      this.$("consent-state").textContent = c.state || "Unknown";
      this.$("capture-status").textContent = c.allowed ? "Everyone has a current local release. The runtime may now perform enrollment; provider access also requires disclosure scopes and reviewed vendor settings." : "Capture blocked. Every participant must personally grant a current release before enrollment.";
      this.$("end-room").disabled = terminal(c.state) || !this.token;
      this.$("destruction").textContent = c.state === "destroyed" ? "Server reports destruction complete for registered destinations. Deployment and vendor erasure evidence remain separate." : ["destroying", "revoked"].includes(c.state) ? "Room authorization revoked. Destruction is pending; completion has not yet been verified." : "";
      const key = JSON.stringify([this.session, c.state, c.policy_version, c.participants, this.noticeReady]);
      if (key === this.consentKey) return;
      this.consentKey = key; this.$("consents").replaceChildren();
      for (const person of c.participants || []) this.renderPerson(person);
      this.$("enrollment-help").textContent = c.allowed ? "Next, at the runtime's enrollment prompt, each participant begins their eight-second statement:\n" + (c.participants || []).map(p => `I, ${p.name}, consent to Voiceprint collecting my voiceprint for identifying consenting speakers and providing a speaker-attributed transcript during this room conversation today.`).join("\n") + "\nThe server links the SHA-256 of submitted enrollment PCM to the already signed release. This page never opens the microphone." : "";
    }
    renderPerson(person) {
      const doc = this.doc, section = node(doc, "div", undefined, "person");
      section.append(node(doc, "h3", `${person.name} · ${person.id}`));
      if (person.bipa_consent_granted === true) {
        section.append(node(doc, "p", `Written release recorded ${stamp(person.consent_timestamp)}. Contact and signer identity are unverified.`));
        const revoke = node(doc, "button", "Withdraw release & stop entire room", "danger"); revoke.type = "button"; revoke.disabled = terminal(this.consent.state);
        revoke.addEventListener("click", async () => { try { await this.revoke(person.id); } catch (e) { this.say(e.message); } });
        section.append(revoke);
      } else if (!terminal(this.consent.state)) {
        const form = node(doc, "form");
        const signatureLabel = node(doc, "label", "Participant: type your full name to sign");
        const signature = node(doc, "input"); signature.required = true; signature.autocomplete = "off"; signatureLabel.append(signature);
        const checkbox = (text) => { const label = node(doc, "label", undefined, "checkbox"), input = node(doc, "input"); input.type = "checkbox"; input.checked = false; label.append(input, node(doc, "span", text)); form.append(label); return input; };
        form.append(signatureLabel);
        const accept = checkbox("I personally reviewed the controller, purpose and retention notice above. I consent to the collection, storage and local processing of my voiceprint for this room and intend my typed name and this submission as my written electronic release."); accept.required = true;
        const audio = checkbox("I also authorize disclosure of my voice audio to OpenAI for the room's voice agent features, as described in the notice.");
        const mcp = checkbox("I also authorize hosted MCP disclosure of my named transcript and speaker attribution to OpenAI through Cloudflare, as described in the notice.");
        const submit = node(doc, "button", "Participant: sign written release"); submit.type = "submit"; submit.disabled = !this.noticeReady;
        const feedback = node(doc, "p", "", "release-feedback"); feedback.setAttribute("role", "status");
        form.append(submit, feedback);
        form.addEventListener("submit", async e => {
          e.preventDefault(); submit.disabled = true;
          try {
            if (!accept.checked || signature.value.trim() !== person.name) throw new Error("The participant must check the release and type their exact full name.");
            await this.sign(person.id, signature.value.trim(), [audio.checked && "openai_audio", mcp.checked && "hosted_mcp"].filter(Boolean));
            feedback.textContent = "Written release recorded. Wait for everyone before audio enrollment.";
          } catch (error) { feedback.textContent = error.message; accept.checked = false; }
          finally { submit.disabled = !this.noticeReady; }
        });
        section.append(form);
      } else section.append(node(doc, "p", "Collection disabled. This room cannot be restarted with another checkbox."));
      this.$("consents").append(section);
    }
    async sign(id, signature, scopes) {
      if (!this.noticeReady || !this.consent || terminal(this.consent.state)) throw new Error("A current notice and pending release are required.");
      const epoch = this.epoch, hash = this.notice.notice_sha256;
      const base = this.path(`consents/${encodeURIComponent(id)}`);
      const challenge = await this.request(`${base}/challenge`, {body:{}});
      if (epoch !== this.epoch || challenge.notice_sha256 !== hash || !challenge.challenge || challenge.expires_at_ms <= Date.now()) throw new Error("Room or notice changed, or challenge expired. Refresh and review the current notice before signing.");
      await this.request(base, {body:{challenge:challenge.challenge, notice_sha256:hash, signature_text:signature, accepted:true, disclosure_scopes:scopes}});
      if (epoch === this.epoch) { this.consentKey = ""; await this.poll(); }
    }
    async revoke(id) {
      const path = this.path(`consents/${encodeURIComponent(id)}/revoke`);
      this.epoch++; this.clearProtected(); this.consent = null; this.$("consents").replaceChildren(); this.$("end-room").disabled = true;
      this.$("destruction").textContent = "Submitting withdrawal. Stop the runtime immediately if the server is unavailable.";
      await this.request(path, {body:{}}); this.consentKey = ""; await this.poll();
    }
    async endRoom() {
      const path = this.path("end"); this.epoch++; this.clearProtected(); this.consent = null; this.$("consents").replaceChildren(); this.$("end-room").disabled = true;
      this.$("destruction").textContent = "Submitting purpose completion and destruction request…";
      await this.request(path, {body:{}}); this.consentKey = ""; await this.poll();
    }
    renderRoster(people) {
      this.people = people; this.$("participants").replaceChildren();
      for (const p of people) this.$("participants").append(node(this.doc, "li", `${p.name} · ${p.kind === "agent" ? "Agent (declared)" : "Human"}`));
    }
    renderCurrent(data) {
      const name = (this.people || []).find(p => p.id === data.speaker_id)?.name || data.speaker_id;
      this.$("current").textContent = name ? `Current attribution: ${name} · ${data.status || "uncertain"}${Number.isFinite(data.similarity) ? ` · similarity ${data.similarity.toFixed(3)}` : ""}` : `Current attribution: ${data.status || "waiting"}`;
      this.$("similarity").hidden = !Number.isFinite(data.similarity); if (Number.isFinite(data.similarity)) this.$("similarity").value = data.similarity;
    }
    tickFloor() {
      if (!this.floor) return;
      const f = this.floor, remaining = Math.max(0, (f.expires_at_ms - f.server_time_ms - (Date.now() - f.received_ms)) / 1000);
      this.$("floor").textContent = !f.held_by ? "Free" : remaining <= 0 ? "Lease elapsed · awaiting server confirmation" : `${(this.people || []).find(p => p.id === f.held_by)?.name || f.held_by} · ${remaining.toFixed(1)}s remaining`;
    }
    renderAgents() {
      const enabled = allowedControls(this.consent, this.runtime, this.session);
      this.$("runtime-state").textContent = !this.runtime ? `Runtime unavailable on ${this.control.slice(7)}` : this.runtime.session_id !== this.session ? `Runtime is serving a different room (${this.runtime.session_id}). Controls disabled.` : !enabled ? "Controls blocked: current local release, disclosure scopes and vendor review are required." : "Runtime connected to this room";
      const key = JSON.stringify([this.runtime, enabled, [...this.feed.lastCalls]]);
      if (key === this.agentKey) return; this.agentKey = key; this.$("agents").replaceChildren();
      for (const a of this.runtime?.agents || []) {
        const card = node(this.doc, "div", undefined, "agent-card");
        card.append(node(this.doc, "h3", a.name), node(this.doc, "p", `${a.provider} · ${a.model} · ${a.voice}`, "muted"), node(this.doc, "p", a.held ? "Held" : a.responding ? "Responding" : "Listening"));
        const actions = node(this.doc, "div", undefined, "inline");
        for (const action of ["speak", "hold", "cancel"]) {
          const button = node(this.doc, "button", action === "hold" && a.held ? "Release hold" : action[0].toUpperCase() + action.slice(1)); button.type = "button"; button.disabled = !enabled;
          button.addEventListener("click", async () => { try { await this.control(a.name, {action}); } catch (e) { this.say(e.message); } }); actions.append(button);
        }
        const label = node(this.doc, "label", "Eagerness"), select = node(this.doc, "select");
        for (const value of ["quiet", "balanced", "eager"]) { const option = node(this.doc, "option", value); option.value = value; select.append(option); }
        select.value = a.eagerness; select.disabled = !enabled; select.addEventListener("change", async () => { try { await this.control(a.name, {action:"eagerness",value:select.value}); } catch (e) { this.say(e.message); } }); label.append(select);
        const call = this.feed.lastCalls.get(a.participant_id);
        card.append(actions, label, node(this.doc, "p", call ? `Last declared MCP call: ${call.tool} · ${call.bytes} bytes · ${stamp(call.timestamp_ms)}` : "No server call attributed to this agent.", "muted")); this.$("agents").append(card);
      }
    }
    async control(name, body) {
      const epoch = this.epoch;
      this.consent = await this.request(this.path("consent")); this.runtime = await this.request("/agents", {runtime:true});
      if (epoch !== this.epoch || !allowedControls(this.consent, this.runtime, this.session)) { this.renderAgents(); throw new Error("Control blocked: room authorization or runtime changed."); }
      await this.request(`/agents/${encodeURIComponent(name)}/control`, {runtime:true,body}); await this.poll();
    }
  }
  const api = {Feed, Console, allowedControls, offset};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else { global.VoiceprintUI = api; new Console(document, global.fetch.bind(global), global.location).bind(); }
})(typeof window !== "undefined" ? window : globalThis);
