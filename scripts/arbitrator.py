"""Text-only arbitrator: a sibling of Agent that never claims the floor, never plays audio and never opens a realtime session.

It ingests the transcript and the agent channel through the local MCP endpoint (so its reads are proof rows), decides
on its own schedule whether a generation is worth it (tiered like turn_gate.decide), and writes to the notes board or
prompts an advocate through that advocate's pre-reply note. Override tags are never self-judged: a second Responses
call sees only the evidence lines. Every text field is pre-redacted here; the server redacts again before storing.
"""
import asyncio
import json
import re
import time
import urllib.error
import urllib.request
from collections import deque

import objectives as ob
from participation import OVERRIDE_TAGS
from providers.openai_realtime import participant_url

ARBITRATOR_COOLDOWN_S = 20.0
SOFT_NEW_LINES = 3                   # a contribution is worth considering after this many new lines
STALE_AFTER_S = 90.0                 # or after this long with at least one new line
MCP_TIMEOUT_S = 10
OFFER_RE = re.compile(r"\d|\$|\b(?:offer|accept|deal|agree|counter)\w*", re.IGNORECASE)
LINE_RE = re.compile(r"^#\d+\s+\S+\s+(?P<name>.+?)\s+\[(?P<label>[^\]]*)\](?:\s*\((?P<tag>[A-Z_]+)\))?:\s?(?P<body>.*)$")

TURN_SCHEMA = {"type": "object", "additionalProperties": False,
               "required": ["board", "raw_note", "prompt_to", "prompt", "tag", "reason"],
               "properties": {"board": {"type": ["string", "null"], "description": "Public notes board entry: agreed points, open points, compromise directions; null when nothing new."},
                              "raw_note": {"type": ["string", "null"], "description": "Private note to the advocates on the raw tier, or null."},
                              "prompt_to": {"type": ["string", "null"], "description": "Participant id of the advocate who should raise something now, or null."},
                              "prompt": {"type": ["string", "null"], "description": "What that advocate should raise, in one or two sentences, or null."},
                              "tag": {"type": ["string", "null"], "enum": [None, "OBJECTIVE_ACHIEVED", "REFOCUS_NEEDED"]},
                              "reason": {"type": "string"}}}
VERDICT_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["confirmed", "reason"],
                  "properties": {"confirmed": {"type": "boolean"}, "reason": {"type": "string"}}}


def parse_line(line):
    """Compact transcript/channel line -> (sender name, label or tier, tag, body); None for anything else."""
    match = LINE_RE.match(line.strip())
    if not match:
        return None
    return match["name"], match["label"], match["tag"], match["body"]


class McpClient:
    """JSON-RPC tools/call against the LOCAL MCP HTTP endpoint, tagged with the arbitrator's participant id."""

    def __init__(self, url, token, participant_id, transport=None):
        self.url = participant_url(url, participant_id)
        self.token, self.participant_id = token, participant_id
        self.transport = transport or self._post
        self.next_id = 0

    @staticmethod
    def _post(request):
        with urllib.request.urlopen(request, timeout=MCP_TIMEOUT_S) as response:
            return json.load(response)

    def call(self, tool, arguments):
        self.next_id += 1
        body = {"jsonrpc": "2.0", "id": self.next_id, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        request = urllib.request.Request(self.url, json.dumps(body).encode(), headers, method="POST")
        try:
            response = self.transport(request)
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"MCP {tool} returned HTTP {error.code}") from None
        if not isinstance(response, dict):
            raise RuntimeError(f"MCP {tool} returned a non-object response")
        if "error" in response:
            code = response["error"].get("code") if isinstance(response["error"], dict) else None
            raise RuntimeError(f"MCP {tool} failed (code {code})")
        result = response.get("result") or {}
        if result.get("isError"):
            raise RuntimeError(f"MCP {tool} reported a tool error")
        return result.get("structuredContent") or {}


class Arbitrator:
    """State per docs/API.md "Runtime control API additions"; controls: speak = generate now, hold = pause posting,
    cancel = drop a pending override."""

    def __init__(self, config, participant_id, room, responses, mcp, poll_s=2.0):
        self.config, self.participant_id, self.room = config, participant_id, room
        self.responses, self.mcp, self.poll_s = responses, mcp, poll_s
        self.player = None                      # never: the arbitrator has no voice
        self.objectives = {}
        self.generations, self.ingested_rows = 0, 0
        self.last_trigger, self.pending_tag = None, None
        self.cooldown_until = 0.0               # monotonic seconds
        self.paused, self.busy, self.manual = False, False, False
        self.mcp_state = {"calls": 0, "failed": 0, "last_error": None}
        self.transcript_cursor, self.channel_cursor = 0, 0
        self.recent = deque(maxlen=60)          # transcript lines for prompt context
        self.notes = deque(maxlen=20)           # other agents' channel lines
        self.board = deque(maxlen=40)           # what this arbitrator posted to the board
        self.new_lines = []                     # since the last generation
        self.started_at, self.last_generation_at, self.next_poll_at = None, None, 0.0

    def emit(self, kind, value):
        self.room.log.emit(kind, value, self.config.name, self.participant_id)

    def state(self):
        remaining = max(0.0, self.cooldown_until - time.monotonic())
        return {"name": self.config.name, "participant_id": self.participant_id, "role": "arbitrator",
                "provider": self.config.provider, "model": self.config.model, "eagerness": self.config.eagerness,
                "held": self.paused, "responding": self.busy, "mcp": dict(self.mcp_state),
                "arbitrator": {"generations": self.generations, "ingested_rows": self.ingested_rows, "last_trigger": self.last_trigger,
                               "pending_tag": self.pending_tag, "cooldown_until_ms": int(time.time() * 1000 + remaining * 1000) if remaining else 0,
                               "paused": self.paused}}

    async def control(self, action, value=None):
        if action == "speak":
            self.manual = True
        elif action == "hold":
            self.paused = not self.paused
        elif action == "cancel":
            self.pending_tag = None
            self.room.cancel_override()
        self.emit("control", {"action": action, "value": value, "held": self.paused})

    # -- intake ------------------------------------------------------------------------------------------------------
    async def _read(self, tool, cursor, key):
        arguments = {"session_id": self.room.session_id, "after_id": cursor, "limit": 100}
        self.mcp_state["calls"] += 1
        try:
            result = await asyncio.to_thread(self.mcp.call, tool, arguments)
        except Exception as error:
            self.mcp_state["failed"] += 1
            self.mcp_state["last_error"] = re.sub(r"[^\x20-\x7e]", "?", str(error))[:200]
            return cursor, []
        try:
            next_cursor = max(cursor, int(result.get("next_after_id", cursor)))
        except (TypeError, ValueError):
            next_cursor = cursor
        count = result.get("count")
        text = result.get(key) if isinstance(result.get(key), str) else ""
        lines = [line for line in text.splitlines() if line.strip()] if count else []
        return next_cursor, lines

    async def ingest(self):
        before = self.ingested_rows
        self.transcript_cursor, lines = await self._read("get_transcript", self.transcript_cursor, "transcript")
        for line in lines:
            self.ingested_rows += 1
            self.recent.append(line)
            self.new_lines.append(line)
        self.channel_cursor, lines = await self._read("get_agent_channel", self.channel_cursor, "channel")
        for line in lines:
            parsed = parse_line(line)
            if parsed is not None and parsed[0] == self.config.name:
                continue                     # own board/raw rows echo back; they are not new evidence
            self.ingested_rows += 1
            self.notes.append(line)
            self.new_lines.append(line)
        if self.ingested_rows != before:
            self.emit("arbitrator", {"action": "ingested", "ingested_rows": self.ingested_rows})

    # -- decision ----------------------------------------------------------------------------------------------------
    def trigger(self, now):
        """Hard: a manual speak request. Inhibitor: cooldown. Soft: enough new lines, an offer-like line, or staleness."""
        if self.manual:
            self.manual = False
            return "manual"
        if now < self.cooldown_until:
            return None
        if not self.new_lines:
            return None
        if len(self.new_lines) >= SOFT_NEW_LINES:
            return "contribution"
        bodies = [(parse_line(line) or (None, None, None, line))[3] for line in self.new_lines]
        if any(OFFER_RE.search(body or "") for body in bodies):
            return "contribution"
        since = now - (self.last_generation_at if self.last_generation_at is not None else self.started_at)
        if since >= STALE_AFTER_S:
            return "contribution"
        return None

    async def tick(self, now):
        if self.started_at is None:
            self.started_at = now
        if now >= self.next_poll_at:
            self.next_poll_at = now + self.poll_s
            await self.ingest()
        if self.paused or self.busy or getattr(self.room, "objectives_forbidden", False):
            return                       # a 403 on objectives means the room lacks negotiation_text: never call the vendor
        trigger = self.trigger(now)
        if trigger is not None:
            await self.generate(trigger, now)

    # -- generation --------------------------------------------------------------------------------------------------
    def principals(self):
        """principal id -> (human name, advocate participant id or None)."""
        result = {}
        for agent in self.room.agents:
            pid = getattr(agent, "principal_id", None)
            if pid:
                result[pid] = (self.room.names.get(pid, pid), agent.participant_id)
        for pid in self.objectives:
            result.setdefault(pid, (self.room.names.get(pid, pid), None))
        return result

    def instructions(self):
        parts = [f"You are {self.config.name}, the neutral arbitrator in a negotiation. You never negotiate for either side. "
                 "You know both parties' private objectives below so you can judge whether a zone of agreement exists, but you must "
                 "NEVER state, quote, approximate, round, bracket or confirm either party's private constraint values, in any field "
                 "of your output, in any form (digits, words, currency, ranges, 'close to', 'above', 'below'). Write the notes board "
                 "for both humans: agreed points, open points and compromise directions in words, never derived figures. You may ask "
                 "one advocate (by participant id) to raise something in their own words. Set tag OBJECTIVE_ACHIEVED only when the "
                 "transcript shows both sides accepting the same terms; REFOCUS_NEEDED only when the conversation has drifted from "
                 "the negotiation for several lines. Otherwise tag is null. Keep every field short. Output JSON only."]
        for pid, (name, advocate) in self.principals().items():
            objective = self.objectives.get(pid)
            who = f"{name} (id {pid})" + (f", represented by advocate id {advocate}" if advocate else "")
            if objective is None:
                parts.append(f"{who}: no objective recorded yet.")
                continue
            constraints = "; ".join(f"{label}: {value}" for label, value in objective.constraints) or "none"
            parts.append(f"{who}. Position: {objective.position}. PRIVATE constraints (never disclose): {constraints}.")
        if self.config.instructions_extra:
            parts.append("Standing instructions from your operator: " + self.config.instructions_extra)
        return "\n\n".join(parts)

    def input_text(self, lines):
        roster = ", ".join(f"{n} (id {pid})" for pid, n in self.room.names.items())
        board = "\n".join(self.board) or "(empty)"
        return (f"Participants: {roster}.\n\nNotes board so far:\n{board}\n\nNew lines since your last contribution:\n"
                + ("\n".join(lines) or "(none)"))

    @staticmethod
    def _text(value, limit=4000):
        return value.strip()[:limit] if isinstance(value, str) and value.strip() else None

    async def generate(self, trigger, now=None):
        now = time.monotonic() if now is None else now
        self.busy = True
        try:
            lines, self.new_lines = list(self.new_lines), []
            self.generations += 1
            self.last_trigger, self.last_generation_at = trigger, now
            self.cooldown_until = now + ARBITRATOR_COOLDOWN_S
            self.emit("arbitrator", {"action": "generated", "trigger": trigger, "generations": self.generations})
            try:
                result = await asyncio.to_thread(self.responses.generate, self.instructions(), self.input_text(lines), "arbitrator_turn", TURN_SCHEMA)
            except Exception as error:
                self.emit("arbitrator", {"action": "skipped", "trigger": trigger})
                print(f"{self.config.name}: arbitrator generation failed ({type(error).__name__})", flush=True)
                return
            values = ob.constraint_values(self.objectives)
            board, raw = self._text(result.get("board")), self._text(result.get("raw_note"))
            prompt, prompt_to = self._text(result.get("prompt"), 1000), self._text(result.get("prompt_to"), 80)
            tag = result.get("tag") if result.get("tag") in OVERRIDE_TAGS else None
            redacted = {}
            for stage, text in (("board", board), ("raw", raw), ("prompt", prompt)):
                if text is not None:
                    text, hits = ob.redact(text, values)
                    if hits:
                        self.emit("guard", {"action": "redacted", "stage": stage if stage != "prompt" else "raw", "redactions": hits})
                redacted[stage] = text
            board, raw, prompt = redacted["board"], redacted["raw"], redacted["prompt"]
            confirmed = False
            if tag is not None:
                self.emit("arbitrator", {"action": "override_claimed", "trigger": trigger, "tag": tag})
                # Evidence is a bounded recent window, not only the lines that triggered this generation.
                evidence = list(self.recent)[-12:] + list(self.notes)[-6:]
                confirmed = await self.verify_override_claim(tag, evidence)
                self.emit("arbitrator", {"action": "override_confirmed" if confirmed else "override_rejected", "tag": tag, "confirmed": confirmed})
            if board is not None:
                if await self.post("board", board, tag if confirmed else None):
                    self.board.append(board)
            if raw is not None:
                await self.post("raw", raw, None)
            if confirmed and prompt and prompt_to:
                if self.room.request_override(prompt_to, prompt, tag):
                    self.pending_tag = tag
                    self.last_trigger = tag
        finally:
            self.busy = False

    async def verify_override_claim(self, tag, evidence_lines):
        """Second, separate call: judge the claim only from the evidence. Any failure rejects (fail closed)."""
        instructions = ("You verify a claim made by a negotiation arbitrator. Judge ONLY from the evidence lines given; do not assume "
                        "anything that is not written there. OBJECTIVE_ACHIEVED is confirmed only if the lines show both sides explicitly "
                        "accepting the same terms. REFOCUS_NEEDED is confirmed only if several consecutive lines are clearly off the "
                        "negotiation topic. Otherwise it is not confirmed. Output JSON only.")
        text = f"Claim: {tag}\n\nEvidence:\n" + ("\n".join(evidence_lines) or "(no lines)")
        try:
            verdict = await asyncio.to_thread(self.responses.generate, instructions, text, "override_verdict", VERDICT_SCHEMA)
        except Exception:
            return False
        return verdict.get("confirmed") is True

    async def post(self, tier, text, tag):
        try:
            await self.room.authorized()
            stored = await self.room.api.request("POST", self.room.path + "/agent_channel",
                                                 {"sender_participant_id": self.participant_id, "tier": tier, "text": text, "tag": tag})
        except Exception as error:
            self.emit("arbitrator", {"action": "skipped", "tier": tier})
            print(f"{self.config.name}: could not store a {tier} row ({type(error).__name__})", flush=True)
            return False
        self.emit("arbitrator", {"action": "posted", "tier": tier, "tag": tag, "redactions": stored.get("redactions") if isinstance(stored, dict) else None})
        print(f"{self.config.name}: posted a {tier} row; text available in the local GUI.", flush=True)
        return True
