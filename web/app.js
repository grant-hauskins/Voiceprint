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
  const GLYPHS = {speak: "\u{1F5E3}", hold: "\u23F8", release: "\u25B6", cancel: "\u2715", start: "\u25B6", stop: "\u25A0"};
  function icon(doc, name) { const el = node(doc, "span", GLYPHS[name] || "", "ico"); el.setAttribute("aria-hidden", "true"); return el; }
  function hue(name) { let h = 0; for (const ch of String(name)) h = (h * 31 + ch.codePointAt(0)) % 360; return h; }
  const when = n => Number.isFinite(n) ? new Date(n).toLocaleString() : "time unknown";
  function channelRow(doc, row) {
    // One agent-channel line (board or raw). Text is already redacted by the API; the count says how many values it withheld.
    const li = node(doc, "li", undefined, row.tier === "raw" ? "raw" : "board"), meta = node(doc, "div", undefined, "meta");
    meta.append(node(doc, "strong", row.sender_name || row.sender_participant_id || "Unknown sender"), node(doc, "span", stamp(row.timestamp_ms)));
    if (row.tag) meta.append(node(doc, "span", row.tag, "chip tag"));
    if (Number.isFinite(row.redactions) && row.redactions > 0) meta.append(node(doc, "span", `${row.redactions} withheld`, "chip withheld"));
    li.append(meta, node(doc, "p", row.text == null ? "Text omitted." : row.text));
    return li;
  }
  function parseObjectiveFile(text, name) {
    // Client-side parse of an uploaded objective. Only the returned fields are ever posted; the file content stays on this computer.
    const clean = s => String(s == null ? "" : s).trim();
    const source = String(text || "");
    if (/\.json$/i.test(String(name || "")) || /^\s*\{/.test(source)) {
      const parsed = JSON.parse(source);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("A JSON objective must be an object with position and constraints.");
      const constraints = (Array.isArray(parsed.constraints) ? parsed.constraints : []).map(c => ({label: clean(c && c.label), value: clean(c && c.value)}));
      return {position: clean(parsed.position).slice(0, 2000), constraints: constraints.filter(c => c.label && c.value).slice(0, 20)};
    }
    const constraints = [], rest = [];
    for (const line of source.split(/\r?\n/)) {
      const m = line.match(/^\s*([^:]{1,80}?)\s*:\s*(.+?)\s*$/);
      if (m) constraints.push({label: m[1], value: m[2].slice(0, 200)}); else if (line.trim()) rest.push(line.trim());
    }
    return {position: rest.join("\n").slice(0, 2000), constraints: constraints.slice(0, 20)};
  }
  function allowedControls(consent, runtime, session) {
    return Boolean(consent && consent.allowed === true && !terminal(consent.state) &&
      consent.scopes && consent.scopes.openai_audio === true && consent.scopes.hosted_mcp === true &&
      runtime && runtime.session_id === session);
  }
  class Feed {
    constructor(doc, transcript, calls, board) { this.doc = doc; this.transcript = transcript; this.calls = calls; this.board = board || doc.createElement("ol"); this.reset(); }
    reset() {
      this.cursor = 0; this.events = new Set(); this.utterances = new Set(); this.callIds = new Set(); this.boardIds = new Set();
      this.transcript.replaceChildren(node(this.doc, "li", "No utterances received.", "empty"));
      this.calls.replaceChildren(node(this.doc, "li", "No server calls received.", "empty"));
      this.board.replaceChildren(node(this.doc, "li", "No board lines received.", "empty"));
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
        } else if (event.type === "agent_channel" && data.tier === "board" && data.row_id != null && !this.boardIds.has(data.row_id)) {
          // Only board rows arrive as events; raw rows are polled separately behind the API's reveal gate.
          if (!this.boardIds.size) this.board.replaceChildren();
          this.boardIds.add(data.row_id);
          this.board.append(channelRow(this.doc, data));
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
      this.runtimeKey = ""; this.autoOpened = ""; this.pollingRuntime = false; this.agentSetupKey = ""; this.gateForced = false;
      // v3: objectives are entered per person and masked after saving; the raw channel is served only when everyone revealed it.
      this.objectiveVersions = {}; this.objectiveBlocks = new Map(); this.objectiveKey = ""; this.revealedBy = new Set(); this.revealKey = "";
      this.rawCursor = 0; this.rawIds = new Set(); this.rawRevealed = false; this.summaryFetched = ""; this.arbitratorSeen = new Map();
      // The launcher opens /ui?control=PORT when 8090 is busy on this computer; the origin stays the loopback API.
      const control = String(new URLSearchParams(location && location.search || "").get("control") || "");
      this.control = `http://127.0.0.1:${/^\d{2,5}$/.test(control) ? control : "8090"}`;
      this.feed = new Feed(doc, this.$("transcript"), this.$("mcp-calls"), this.$("board"));
    }
    $(id) { return this.doc.getElementById(id); }
    say(message) { this.$("message").textContent = message; }
    async request(path, {body, runtime = false, publicRequest = false, method} = {}) {
      const headers = {Accept: "application/json"};
      if (!publicRequest) {
        if (!this.token) throw new Error("Enter the configured operator API token first.");
        headers.Authorization = `Bearer ${this.token}`;
      }
      if (body !== undefined) headers["Content-Type"] = "application/json";
      const response = await this.fetch((runtime ? this.control : "") + path,
        {method: method || (body === undefined ? "GET" : "POST"), headers, body: body === undefined ? undefined : JSON.stringify(body), cache: "no-store", credentials: "omit", redirect: "error", signal: AbortSignal.timeout(12000)});
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
        await this.loadNotice(); await this.loadRooms(); this.$("connection").textContent = "Operator connected"; this.say(""); this.renderStage();
      }));
      this.$("disconnect").addEventListener("click", () => { this.disconnect(); this.$("token").value = ""; });
      this.$("refresh").addEventListener("click", run(() => this.loadRooms()));
      this.$("session-picker").addEventListener("change", run(() => this.open(this.$("session-picker").value)));
      this.$("open-form").addEventListener("submit", run(() => this.open(this.$("room-id").value.trim())));
      this.$("add-person").addEventListener("click", () => this.addPerson());
      this.$("create-form").addEventListener("submit", run(() => this.createRoom()));
      this.$("end-room").addEventListener("click", run(() => this.endRoom()));
      this.$("setup-add").addEventListener("click", () => this.addSetupPerson());
      this.$("agent-add").addEventListener("click", () => this.addAgentCard());
      this.$("setup-form").addEventListener("submit", run(() => this.submitSetup()));
      this.$("start").addEventListener("click", run(() => this.runtimeAction("/start")));
      this.$("gate-stop").addEventListener("click", run(() => this.runtimeAction("/stop")));
      this.$("gate-close").addEventListener("click", () => { this.gateForced = false; this.renderStage(); });
      this.$("review-releases").addEventListener("click", () => { this.gateForced = true; this.renderStage(); });
      this.$("stop-runtime").addEventListener("click", run(() => this.runtimeAction("/stop")));
      this.$("summary-delete").addEventListener("click", run(() => this.deleteSummary()));
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
      // The runtime writes the closing summary before ending the room, so one fetch when the phase turns ended is enough.
      if (r && r.phase === "ended" && this.session && this.summaryFetched !== this.session) { this.summaryFetched = this.session; await this.loadSummary(); }
      if (r && r.session_id && r.phase !== "setup" && r.session_id !== this.session && this.autoOpened !== r.session_id && ID.test(r.session_id)) {
        this.autoOpened = r.session_id;
        // The runtime reports the room a moment before the API has it; retry on the next tick instead of giving up.
        try { await this.loadRooms(); await this.open(r.session_id); this.say(""); } catch (e) { this.autoOpened = ""; this.say(e.message); }
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
    vendorsReviewed() {
      const v = this.notice && this.notice.vendors;
      return Boolean(v && v.openai_reviewed === true && v.cloudflare_reviewed === true);
    }
    renderAgentSetup(configs, options, conversationType) {
      // Rebuilt only when the runtime's agent list changes, so typing in the boxes survives polling.
      const key = JSON.stringify([configs, options, conversationType]);
      if (key === this.agentSetupKey) return; this.agentSetupKey = key;
      this.agentOptions = options; const box = this.$("setup-agents"); box.replaceChildren();
      this.$("conversation-type").value = conversationType === "negotiation" ? "negotiation" : "casual";
      for (const a of configs) this.addAgentCard(a);
    }
    applyRole(card) {
      // An arbitrator is text only: no voice, no eagerness, speaks for nobody. The runtime rejects roles the room type forbids.
      const arbitrator = card.querySelector('[name="role"]').value === "arbitrator";
      for (const name of ["voice", "eagerness", "speaks_for"]) card.querySelector(`[name="${name}"]`).parentLabel.hidden = arbitrator;
      card.querySelector('[name="role_note"]').hidden = !arbitrator;
    }
    addAgentCard(a = {}) {
      const options = this.agentOptions || {voices:["marin"], eagerness_levels:["quiet","balanced","eager"], max_agents:4};
      const box = this.$("setup-agents");
      if (box.children.length >= options.max_agents) { this.say(`At most ${options.max_agents} agents per room.`); return; }
      const doc = this.doc, card = node(doc, "div", undefined, "agent-card");
      const field = (title, name, value, max) => { const label = node(doc, "label", title), input = node(doc, "input"); input.name = name; input.value = value || ""; input.maxLength = max; input.autocomplete = "off"; input.parentLabel = label; label.append(input); return label; };
      const choice = (title, name, values, value) => { const label = node(doc, "label", title), select = node(doc, "select"); select.name = name; for (const v of values) { const option = node(doc, "option", v); option.value = v; select.append(option); } select.value = values.includes(value) ? value : values[0]; select.parentLabel = label; label.append(select); return label; };
      const role = choice("Role", "role", ["voice", "arbitrator"], a.role);
      const note = node(doc, "p", "Arbitrator: text only, never speaks. It sees both objectives, reads the transcript and the agent channel through our MCP server, and posts to the notes board.", "muted role-note");
      note.setAttribute("name", "role_note"); note.hidden = true;
      card.append(field("Agent name (its own separate instance and prompt)", "name", a.name, 60), role, note,
        choice("Voice", "voice", options.voices, a.voice), choice("Eagerness", "eagerness", options.eagerness_levels, a.eagerness),
        field("Speaks for (a full name from the roster, optional)", "speaks_for", a.speaks_for, 200));
      role.querySelector('[name="role"]').addEventListener("change", () => this.applyRole(card));
      const how = node(doc, "label", "Standing instructions (how this agent should respond)"); const text = node(doc, "textarea");
      text.name = "instructions"; text.value = a.instructions || ""; text.rows = 5; text.maxLength = 6000; how.append(text);
      const file = node(doc, "label", "Load instructions from a text file"); const picker = node(doc, "input");
      picker.type = "file"; picker.name = "file"; picker.accept = ".txt,.md,text/plain,text/markdown";
      picker.addEventListener("change", () => {
        const chosen = picker.files && picker.files[0]; if (!chosen) return;
        if (chosen.size > 65536) { this.say("Instruction files are limited to 64 KB."); picker.value = ""; return; }
        const reader = new FileReader(); reader.onload = () => { text.value = String(reader.result || "").slice(0, 6000); }; reader.readAsText(chosen);
      });
      file.append(picker);
      const remove = node(doc, "button", "Remove agent"); remove.type = "button"; remove.addEventListener("click", () => card.remove());
      card.append(how, file, remove, node(doc, "p", "Each agent is its own provider session with only its own instructions. What is listed here is what gets saved on this computer under data\\agents. The room rules (who spoke, when to speak) still apply.", "muted"));
      this.applyRole(card); box.append(card);
    }
    agentSetup() {
      // An arbitrator row keeps the hidden selects' valid defaults (the runtime ignores them) and speaks for nobody.
      return [...this.$("setup-agents").children].map(card => {
        const a = Object.fromEntries(["name", "role", "voice", "eagerness", "speaks_for", "instructions"].map(k => [k, card.querySelector(`[name="${k}"]`).value.trim()]));
        if (a.role === "arbitrator") a.speaks_for = "";
        return a;
      });
    }
    async submitSetup() {
      if (!this.runtime || this.runtime.phase !== "setup") throw new Error("The runtime is not waiting for setup.");
      if (!this.vendorsReviewed()) throw new Error("Hosted agents are blocked until both vendor review flags are set in data\\launcher.env.");
      const participants = [...this.$("setup-roster").children].map(row => Object.fromEntries(["name", "contact"].map(key => [key, row.querySelector(`[name="${key}"]`).value.trim()])));
      if (participants.length < 2 || participants.length > 4 || participants.some(p => !p.name || !p.contact)) throw new Error("Enter a full name and an email or phone for each of the two to four people within microphone range.");
      const agents = this.agentSetup();
      const names = new Set(participants.map(p => p.name.toLowerCase()));
      if (!agents.length) throw new Error("Add at least one agent.");
      const agentNames = new Set();
      for (const a of agents) {
        if (!a.name) throw new Error("Every agent needs a name.");
        if (agentNames.has(a.name.toLowerCase()) || names.has(a.name.toLowerCase())) throw new Error(`Agent name ${a.name} must be distinct from the other agents and from the people in the room.`);
        agentNames.add(a.name.toLowerCase());
        if (a.speaks_for && !names.has(a.speaks_for.toLowerCase())) throw new Error(`${a.name} can only speak for a person on the roster (exact full name).`);
      }
      // Mirrors the runtime's rule: a negotiation is two advocates for two different people plus one arbitrator; casual rooms have no arbitrator.
      const conversation_type = this.$("conversation-type").value === "negotiation" ? "negotiation" : "casual";
      const arbitrators = agents.filter(a => a.role === "arbitrator"), advocates = agents.filter(a => a.role !== "arbitrator");
      if (conversation_type === "casual" && arbitrators.length) throw new Error("An arbitrator is only allowed in a negotiation. Change the conversation type or the agent's role.");
      if (conversation_type === "negotiation") {
        const principals = new Set(advocates.map(a => a.speaks_for.toLowerCase()).filter(Boolean));
        if (advocates.length !== 2 || principals.size !== 2) throw new Error("A negotiation needs exactly two voice agents (the advocates), each speaking for a different person on the roster.");
        if (arbitrators.length !== 1) throw new Error("A negotiation needs exactly one arbitrator agent.");
      }
      const body = {conversation_type, participants, agents};
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
    stage() {
      // Which view the operator should be in. The gate (setup, releases, enrollment) is inescapable until the room is
      // enrolled; afterwards it can be reopened to review or withdraw releases.
      const r = this.runtime, phase = r ? (r.phase || "live") : null;
      if (!this.token) return {gate: true, step: "room", required: true};
      if (phase && ["setup", "consent", "enrollment", "connecting"].includes(phase)) return {gate: true, step: phase === "setup" ? "room" : phase === "consent" ? "releases" : "enrollment", required: true};
      if (phase && ["ready", "live", "ending", "ended", "failed"].includes(phase)) return {gate: this.gateForced, step: "enrollment", required: false};
      if (this.session && this.consent && this.consent.allowed) return {gate: this.gateForced, step: "enrollment", required: false};
      return {gate: true, step: this.session ? "releases" : "room", required: true};
    }
    renderStage() {
      const {gate, step, required} = this.stage(), r = this.runtime, phase = r ? (r.phase || "live") : null;
      this.$("gate").hidden = !gate;
      this.$("gate-close").hidden = required;
      this.$("gate-stop").hidden = !(r && required && !["ended", "failed"].includes(phase));
      this.$("gate-stop").disabled = !this.token;
      const order = ["room", "releases", "enrollment"];
      for (const li of this.$("steps").children || []) { const name = li.getAttribute ? li.getAttribute("data-step") : li["data-step"]; li.className = name === step ? "active" : order.indexOf(name) < order.indexOf(step) ? "done" : ""; }
      this.$("connect-section").hidden = Boolean(this.token) && this.$("connection").textContent.includes("launcher");
      this.$("room-section").hidden = step !== "room";
      this.$("notice-section").hidden = step === "room" && !this.session;
      this.$("releases-section").hidden = step === "room" && !this.session;
      // Objectives follow the releases (the API refuses them until everyone signed with negotiation_text) and stay reachable for new versions.
      this.$("objectives-section").hidden = (step === "room" && !this.session) || !this.negotiation() || !this.humans().length;
      this.$("enrollment-section").hidden = step !== "enrollment";
      this.$("gate-title").textContent = step === "room" ? "Set up this conversation" : step === "releases" ? "Each person signs their written release" : phase === "connecting" ? "Connecting the agents" : "Record each person's enrollment statement";
      const mic = this.$("mic"); const state = phase === "live" ? "on" : phase === "ready" ? "ready" : "off";
      mic.setAttribute("data-state", state);
      this.$("mic-label").textContent = state === "on" ? "Microphone open · shared by everyone in the room" : state === "ready" ? "Ready · press Start to open the microphone" : "Microphone closed";
      this.$("app-message").textContent = !this.token ? "Local room console" : !r ? "Monitoring" : phase === "live" ? `Live · ${r.session_id || ""}` : phase === "ready" ? "Enrolled · ready to start" : phase === "ended" ? "Conversation ended" : phase === "failed" ? "Runtime stopped" : "Setting up";
    }
    negotiation() { return Boolean(this.runtime && this.runtime.conversation_type === "negotiation"); }
    renderRuntime() {
      const r = this.runtime, key = JSON.stringify([this.token ? 1 : 0, this.vendorsReviewed(), r]);
      if (key === this.runtimeKey) return; this.runtimeKey = key;
      this.renderObjectives();
      const phase = r ? (r.phase || "live") : null;
      this.$("phase").textContent = !this.token ? "Connect first" : !r ? `Runtime unavailable on ${this.control.slice(7)}` : phase;
      this.$("phase-detail").textContent = r && r.detail ? r.detail : !r && this.token ? "Start it with Voiceprint.cmd (or scripts\\dev.ps1 up) and this page will connect on its own." : "";
      this.$("mcp-url").textContent = r && r.mcp_url ? `Hosted MCP URL given to the provider: ${r.mcp_url}` : "";
      this.$("setup-form").hidden = phase !== "setup";
      if (phase === "setup") this.renderAgentSetup(r.agent_configs || [], {voices:r.voices || ["marin"], eagerness_levels:r.eagerness_levels || ["quiet","balanced","eager"], max_agents:r.max_agents || 4}, r.conversation_type);
      this.$("key-label").hidden = !(r && r.needs_openai_key);
      // Without both review flags the API computes the hosted scopes as false, so a room created now would fail after everyone signed.
      const reviewed = this.vendorsReviewed();
      this.$("setup-submit").disabled = !reviewed;
      this.$("setup-blocked").textContent = reviewed ? "" : "Hosted agents are blocked: review the OpenAI and Cloudflare account settings, then set VOICEPRINT_OPENAI_REVIEWED=true and VOICEPRINT_CLOUDFLARE_REVIEWED=true in data\\launcher.env and restart the launcher.";
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
      this.$("stop-runtime").prepend(icon(this.doc, "stop"));
      this.renderStage();
    }
    disconnect() {
      this.token = ""; this.session = ""; this.epoch++; this.consent = null; this.runtime = null; this.consentKey = ""; this.agentKey = "";
      this.clearProtected(); this.$("consents").replaceChildren(); this.$("session-picker").replaceChildren(node(this.doc, "option", "Choose a room"));
      this.$("connection").textContent = "Disconnected"; this.$("consent-state").textContent = "No room selected";
      this.$("create-room").disabled = true; this.$("end-room").disabled = true;
      this.$("enrollment-help").textContent = ""; this.$("capture-status").textContent = "Microphone capture requires a current release from everyone.";
      this.$("destruction").textContent = ""; this.$("room-id").value = "";
      this.$("token").value = ""; this.$("new-room-id").value = ""; this.$("new-roster").replaceChildren(); this.addPerson();
      this.$("openai-key").value = ""; this.runtimeKey = ""; this.gateForced = false; this.resetRoomState(); this.renderRuntime();
    }
    resetRoomState() {
      // Per-room memory that must not leak into the next room: objective version counters, reveal buttons, summary.
      this.objectiveVersions = {}; this.objectiveBlocks = new Map(); this.objectiveKey = ""; this.$("objectives").replaceChildren();
      this.revealedBy = new Set(); this.revealKey = ""; this.$("reveal-controls").replaceChildren(); this.summaryFetched = ""; this.arbitratorSeen = new Map();
      this.$("summary-panel").hidden = true; this.$("summary-text").textContent = ""; this.$("summary-meta").textContent = "";
    }
    clearProtected() {
      this.feed.reset(); this.floor = null; this.$("participants").replaceChildren(); this.$("agents").replaceChildren();
      this.$("current").textContent = "Attribution unavailable"; this.$("similarity").hidden = true; this.$("floor").textContent = "Unknown";
      this.$("runtime-state").textContent = "Controls unavailable"; this.$("feed-state").textContent = "Waiting for authorization";
      this.$("call-count").textContent = "0"; this.$("utterance-count").textContent = "0"; this.$("board-count").textContent = "0"; this.agentKey = "";
      this.clearRaw("Waiting for authorization");
    }
    clearRaw(message) {
      // Raw rows are never kept across a hidden response; the cursor restarts so a later reveal shows the whole stream.
      this.rawCursor = 0; this.rawIds = new Set(); this.rawRevealed = false; this.$("raw-count").textContent = "0";
      this.$("raw-channel").replaceChildren(node(this.doc, "li", "No raw rows shown.", "empty")); this.$("raw-state").textContent = message;
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
      this.epoch++; this.session = id; this.consent = null; this.consentKey = ""; this.runtime = null; this.clearProtected(); this.resetRoomState();
      this.$("consents").replaceChildren(); this.$("enrollment-help").textContent = ""; this.$("destruction").textContent = "";
      this.$("end-room").disabled = true; this.$("room-id").value = id; this.$("session-picker").value = id;
      await this.poll();
      // An ended room may still hold its retained closing summary (404 means nothing to show).
      await this.loadSummary();
    }
    path(suffix, session = this.session) { return `/speaker/session/${encodeURIComponent(session)}/${suffix}`; }
    async poll() {
      if (!this.token || !this.session || this.polling) return;
      this.polling = true; const epoch = this.epoch; const session = this.session;
      try {
        const consent = await this.request(this.path("consent", session));
        if (epoch !== this.epoch) return;
        this.consent = consent; this.renderConsent(); this.renderStage();
        if (!consent.allowed || terminal(consent.state)) {
          this.clearProtected();
          if (["revoked", "destroying", "destroyed"].includes(consent.state)) await this.loadDestruction(epoch, session);
          return;
        }
        const results = await Promise.allSettled([this.request(this.path(`events?after_id=${this.feed.cursor}&limit=200&wait_ms=0`, session)), this.request(this.path("participants", session)), this.request(this.path("current", session)), this.request(this.path("floor", session)), this.request("/agents", {runtime:true}),
          this.request(this.path(`agent_channel?after_id=${this.rawCursor}&limit=200&tier=raw`, session))]);
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
        const [events, roster, current, floor, runtime, raw] = results;
        if (events.status === "fulfilled") {
          this.feed.apply(events.value); this.$("feed-state").textContent = "Live · server feed";
          this.$("call-count").textContent = this.feed.callIds.size; this.$("utterance-count").textContent = this.feed.utterances.size; this.$("board-count").textContent = this.feed.boardIds.size;
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
        if (raw.status === "fulfilled") this.renderRaw(raw.value); else this.clearRaw(`Raw channel unavailable: ${raw.reason.message}`);
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
      this.renderObjectives(); this.renderReveal();
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
        const negotiation = checkbox("I also authorize negotiation_text disclosure: my typed negotiation objective is stored, given only to my own advocate agent, sent with the other objective, notes board and named transcript to OpenAI text models for the neutral arbitrator and the closing summary, and that summary is kept for 30 days.");
        form.append(node(doc, "p", "The voice agents need the first two optional disclosures from every person in the room; negotiation rooms need all three from everyone. A release without them still counts locally, but the room then cannot use the agents and must be ended and re-signed.", "muted"));
        const submit = node(doc, "button", "Participant: sign written release"); submit.type = "submit"; submit.disabled = !this.noticeReady;
        const feedback = node(doc, "p", "", "release-feedback"); feedback.setAttribute("role", "status");
        form.append(submit, feedback);
        form.addEventListener("submit", async e => {
          e.preventDefault(); submit.disabled = true;
          try {
            if (!accept.checked || signature.value.trim() !== person.name) throw new Error("The participant must check the release and type their exact full name.");
            await this.sign(person.id, signature.value.trim(), [audio.checked && "openai_audio", mcp.checked && "hosted_mcp", negotiation.checked && "negotiation_text"].filter(Boolean));
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
    humans() { return (this.consent && this.consent.participants || []).filter(p => p && ID.test(String(p.id))); }
    renderObjectives() {
      // One collapsible block per person. Rebuilt only when the roster or room type changes so an entry in progress survives polling.
      const people = this.negotiation() ? this.humans() : [];
      const key = JSON.stringify([this.session, people.map(p => [p.id, p.name])]);
      if (key === this.objectiveKey) return; this.objectiveKey = key;
      const box = this.$("objectives"); box.replaceChildren(); this.objectiveBlocks = new Map();
      for (const person of people) box.append(this.objectiveBlock(person));
    }
    objectiveBlock(person) {
      const doc = this.doc, block = node(doc, "details", undefined, "objective"), summary = node(doc, "summary", `${person.name} · no objective recorded yet`);
      block.open = true; block.append(summary, node(doc, "p", `Hand the keyboard to ${person.name}. What is typed here is stored on the API for this room only, given to ${person.name}'s own advocate and to the arbitrator, and never shown to the other person.`, "muted"));
      const positionLabel = node(doc, "label", "Position (shareable)"), position = node(doc, "textarea");
      position.name = "position"; position.rows = 3; position.maxLength = 2000; positionLabel.append(position);
      const rows = node(doc, "div", undefined, "constraints"); const add = node(doc, "button", "Add constraint"); add.type = "button"; add.name = "add-constraint";
      const fileLabel = node(doc, "label", "Or load a .txt or .json file (parsed on this computer; only the fields are sent)"), picker = node(doc, "input");
      picker.type = "file"; picker.name = "file"; picker.accept = ".txt,.json,text/plain,application/json"; fileLabel.append(picker);
      const save = node(doc, "button", "Save objective"); save.type = "button"; save.name = "save";
      const status = node(doc, "p", "", "status"); status.setAttribute("role", "status");
      const state = {block, summary, position, rows, status, source: "typed", person};
      add.addEventListener("click", () => this.addConstraint(person.id));
      picker.addEventListener("change", () => {
        const chosen = picker.files && picker.files[0]; if (!chosen) return;
        if (chosen.size > 65536) { status.textContent = "Objective files are limited to 64 KB."; picker.value = ""; return; }
        const reader = new FileReader();
        reader.onload = () => { try { this.loadObjective(person.id, parseObjectiveFile(String(reader.result || ""), chosen.name)); } catch (e) { status.textContent = `Could not parse the file: ${e.message}`; } picker.value = ""; };
        reader.readAsText(chosen);
      });
      save.addEventListener("click", async () => { save.disabled = true; try { await this.saveObjective(person.id); } catch (_) { /* shown in the block's status line */ } finally { save.disabled = false; } });
      block.append(positionLabel, node(doc, "h4", "Constraints (never disclosed; the server withholds these values from every channel)"), rows, add, fileLabel, save, status);
      this.objectiveBlocks.set(person.id, state);
      return block;
    }
    addConstraint(id, values = {}) {
      const state = this.objectiveBlocks.get(id); if (!state) return null;
      if (state.rows.children.length >= 20) { state.status.textContent = "At most 20 constraints per objective."; return null; }
      const doc = this.doc, row = node(doc, "div", undefined, "constraint");
      for (const [name, title, max] of [["label", "Label (e.g. floor, deadline)", 80], ["value", "Value", 200]]) {
        const label = node(doc, "label", title), input = node(doc, "input"); input.name = name; input.maxLength = max; input.autocomplete = "off"; input.value = values[name] || ""; label.append(input); row.append(label);
      }
      const remove = node(doc, "button", "Remove"); remove.type = "button"; remove.name = "remove-constraint";
      remove.addEventListener("click", () => state.rows.replaceChildren(...[...state.rows.children].filter(r => r !== row))); row.append(remove);
      state.rows.append(row); return row;
    }
    loadObjective(id, parsed) {
      // Parsed fields from an uploaded file replace the typed entry; the file itself is discarded by the picker's change handler.
      const state = this.objectiveBlocks.get(id); if (!state) return;
      state.position.value = parsed.position || ""; state.rows.replaceChildren();
      for (const c of parsed.constraints || []) this.addConstraint(id, c);
      state.source = "uploaded"; state.status.textContent = `Loaded ${(parsed.constraints || []).length} constraint(s) from the file. Review, then save.`;
    }
    readObjective(id) {
      const state = this.objectiveBlocks.get(id); if (!state) throw new Error("No objective block for that person.");
      const position = state.position.value.trim();
      const constraints = [...state.rows.children].map(row => ({label: row.querySelector('[name="label"]').value.trim(), value: row.querySelector('[name="value"]').value.trim()})).filter(c => c.label || c.value);
      if (!position) throw new Error("Enter the shareable position first.");
      if (constraints.some(c => !c.label || !c.value)) throw new Error("Every constraint needs both a label and a value, or remove the row.");
      return {position, constraints, source: state.source};
    }
    async saveObjective(id) {
      const state = this.objectiveBlocks.get(id); if (!state) throw new Error("No objective block for that person.");
      try {
        const fields = this.readObjective(id), epoch = this.epoch;
        const body = {principal_id: id, position: fields.position, constraints: fields.constraints, source: fields.source, trigger: this.objectiveVersions[id] ? "edited in console" : "initial"};
        const result = await this.request(this.path("objectives"), {body});
        if (epoch !== this.epoch) return;
        // Mask: the next person at the keyboard must not read this entry. Only the server's version number and a count remain.
        this.objectiveVersions[id] = Number.isFinite(result.version) ? result.version : (this.objectiveVersions[id] || 0) + 1;
        state.position.value = ""; state.rows.replaceChildren(); state.source = "typed"; state.block.open = false; state.block.className = "objective recorded";
        const n = fields.constraints.length;
        state.summary.textContent = `${state.person.name} · Objective v${this.objectiveVersions[id]} recorded · ${n} constraint${n === 1 ? "" : "s"}`;
        state.status.textContent = "";
      } catch (e) {
        state.status.textContent = /^403:/.test(e.message || "") ? "Every person must sign with the negotiation_text disclosure first." : e.message;
        throw e;
      }
    }
    renderReveal() {
      // Per-person state is known only from this page's own reveal posts; after a reload every button reads "Reveal" until clicked.
      const people = this.humans(), key = JSON.stringify([this.session, people.map(p => [p.id, p.name]), [...this.revealedBy], Boolean(this.token)]);
      if (key === this.revealKey) return; this.revealKey = key;
      const box = this.$("reveal-controls"); box.replaceChildren();
      for (const person of people) {
        const on = this.revealedBy.has(person.id), button = node(this.doc, "button", on ? `Hide from ${person.name}` : `Reveal to ${person.name}`, "ghost");
        button.type = "button"; button.disabled = !this.token; button.name = `reveal:${person.id}`;
        button.addEventListener("click", async () => { try { await this.toggleReveal(person.id, !on); } catch (e) { this.say(e.message); } });
        box.append(button);
      }
    }
    async toggleReveal(id, revealed) {
      const epoch = this.epoch;
      const result = await this.request(this.path("agent_channel/reveal"), {body: {participant_id: id, revealed}});
      if (epoch !== this.epoch) return;
      this.revealedBy = new Set(Array.isArray(result.revealed_by) ? result.revealed_by : []); this.renderReveal(); await this.poll();
    }
    renderRaw(page) {
      // The API advances next_after_id past withheld rows, so the cursor stays at 0 while hidden: the first revealed
      // response is always a fetch from after_id=0 and shows the earlier raw rows. Any hidden response resets to 0 again.
      if (page.revealed !== true) { this.clearRaw("Hidden until every person in the room reveals it"); return; }
      // Revealed: adopt the server cursor and keep rows until a hidden response clears them.
      this.rawRevealed = true; this.$("raw-state").textContent = "Revealed by everyone · server-served rows";
      for (const row of Array.isArray(page.rows) ? page.rows : []) {
        if (row.row_id == null || this.rawIds.has(row.row_id)) continue;
        if (!this.rawIds.size) this.$("raw-channel").replaceChildren();
        this.rawIds.add(row.row_id); this.$("raw-channel").append(channelRow(this.doc, row));
      }
      if (Number.isSafeInteger(page.next_after_id)) this.rawCursor = Math.max(this.rawCursor, page.next_after_id);
      this.$("raw-count").textContent = this.rawIds.size;
    }
    async loadSummary() {
      if (!this.token || !this.session) return;
      const epoch = this.epoch;
      try {
        const s = await this.request(this.path("summary"));
        if (epoch === this.epoch) this.renderSummary(s);
      } catch (e) {
        if (epoch !== this.epoch) return;
        this.$("summary-panel").hidden = true;
        if (!/^404:/.test(e.message || "")) this.say(`Closing summary unavailable: ${e.message}`);
      }
    }
    renderSummary(s) {
      const panel = this.$("summary-panel");
      if (!s || typeof s.text !== "string" || !s.text) { panel.hidden = true; return; }
      this.$("summary-text").textContent = s.text;
      this.$("summary-meta").textContent = `Model ${s.model || "unknown"} · saved ${when(s.created_ms)} · kept until ${when(s.retention_deadline_ms)} · from ${Number.isFinite(s.board_rows) ? s.board_rows : "?"} board rows and ${Number.isFinite(s.transcript_rows) ? s.transcript_rows : "?"} transcript rows.`;
      panel.hidden = false;
    }
    async deleteSummary() {
      await this.request(this.path("summary"), {method: "DELETE"});
      this.$("summary-panel").hidden = true; this.$("summary-text").textContent = ""; this.$("summary-meta").textContent = ""; this.say("Closing summary deleted from the API.");
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
      const mediating = this.mediating();
      const key = JSON.stringify([this.runtime, enabled, [...this.feed.lastCalls], [...mediating]]);
      if (key === this.agentKey) return; this.agentKey = key; this.$("agents").replaceChildren();
      for (const a of this.runtime?.agents || []) {
        const arbitrator = a.role === "arbitrator", arb = a.arbitrator || {};
        const held = arbitrator ? arb.paused === true : a.held, active = arbitrator ? mediating.has(a.name) : a.responding;
        const card = node(this.doc, "div", undefined, `agent-card${arbitrator ? " arbitrator" : ""}${held ? " held" : active ? " speaking" : ""}`);
        const wrap = node(this.doc, "div", undefined, "orb-wrap"), orb = node(this.doc, "div", undefined, "orb");
        orb.setAttribute("style", `--h:${hue(a.name)}`); orb.setAttribute("aria-hidden", "true"); wrap.append(orb);
        card.append(wrap, node(this.doc, "h3", a.name), node(this.doc, "p", held ? (arbitrator ? "Paused" : "Held") : active ? (arbitrator ? "Mediating" : "Speaking") : "Listening", "state"),
          node(this.doc, "p", arbitrator ? `text only · ${a.model}` : `${a.provider} · ${a.model} · ${a.voice}`, "meta"));
        const actions = node(this.doc, "div", undefined, "actions");
        for (const action of ["speak", "hold", "cancel"]) {
          const text = arbitrator ? {speak: "Post now", hold: held ? "Resume" : "Pause", cancel: "Drop override"}[action] : action === "hold" && held ? "Release hold" : action[0].toUpperCase() + action.slice(1);
          const button = node(this.doc, "button", undefined); button.type = "button"; button.disabled = !enabled; button.title = text;
          button.append(icon(this.doc, action === "hold" && held ? "release" : action), node(this.doc, "span", text));
          button.addEventListener("click", async () => { try { await this.control(a.name, {action}); } catch (e) { this.say(e.message); } }); actions.append(button);
        }
        card.append(actions);
        if (arbitrator) {
          card.append(node(this.doc, "p", `Generations ${Number.isFinite(arb.generations) ? arb.generations : 0} · ingested rows ${Number.isFinite(arb.ingested_rows) ? arb.ingested_rows : 0} · pending tag ${arb.pending_tag || "none"}${arb.last_trigger ? ` · last trigger ${arb.last_trigger}` : ""}. Posts to the notes board; never takes the floor.`, "meta"));
        } else {
          const label = node(this.doc, "label", "Eagerness"), select = node(this.doc, "select");
          for (const value of ["quiet", "balanced", "eager"]) { const option = node(this.doc, "option", value); option.value = value; select.append(option); }
          select.value = a.eagerness; select.disabled = !enabled; select.addEventListener("change", async () => { try { await this.control(a.name, {action:"eagerness",value:select.value}); } catch (e) { this.say(e.message); } }); label.append(select);
          card.append(label);
        }
        const m = a.mcp || {};
        const listing = m.list_tools === "failed" ? `Provider FAILED to list our MCP tools${m.last_error ? `: ${m.last_error}` : ""}. The agent cannot call get_transcript; check the tunnel URL and token.` : m.list_tools === "ok" ? `Provider listed tools ${JSON.stringify(m.tools || [])}` : m.calls ? "Provider is calling our MCP tools." : "No MCP activity reported by the provider yet.";
        const call = this.feed.lastCalls.get(a.participant_id);
        card.append(node(this.doc, "p", `${listing} Provider-declared calls: ${m.calls || 0}${m.failed ? ` (${m.failed} failed: ${m.last_error || "no detail"})` : ""}. ${call ? `Last server-recorded call: ${call.tool} · ${call.bytes} bytes · ${stamp(call.timestamp_ms)}.` : "No server call attributed to this agent."}`, m.list_tools === "failed" || m.failed ? "warn meta" : "meta"));
        this.$("agents").append(card);
      }
    }
    mediating() {
      // An arbitrator is "Mediating" while the runtime says it is responding, or for a few seconds after its generation count rose.
      const names = new Set(), now = Date.now();
      for (const a of this.runtime?.agents || []) {
        if (a.role !== "arbitrator") continue;
        const g = a.arbitrator && Number.isFinite(a.arbitrator.generations) ? a.arbitrator.generations : 0, seen = this.arbitratorSeen.get(a.name) || {generations: g, until: 0};
        const until = g > seen.generations ? now + 4000 : seen.until;
        this.arbitratorSeen.set(a.name, {generations: g, until});
        if (a.responding === true || until > now) names.add(a.name);
      }
      return names;
    }
    async control(name, body) {
      const epoch = this.epoch;
      this.consent = await this.request(this.path("consent")); this.runtime = await this.request("/agents", {runtime:true});
      if (epoch !== this.epoch || !allowedControls(this.consent, this.runtime, this.session)) { this.renderAgents(); throw new Error("Control blocked: room authorization or runtime changed."); }
      await this.request(`/agents/${encodeURIComponent(name)}/control`, {runtime:true,body}); await this.poll();
    }
  }
  const api = {Feed, Console, allowedControls, offset, parseObjectiveFile};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else { global.VoiceprintUI = api; new Console(document, global.fetch.bind(global), global.location).bind(); }
})(typeof window !== "undefined" ? window : globalThis);
