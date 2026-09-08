r"""One shared microphone, a stored conversation bus, and N floor-controlled voice agents.

Normal start: double-click Voiceprint.cmd (or `scripts\dev.ps1 up`), which runs worker, API, tunnel and this
runtime with --gui, then opens the console at /ui where the operator enters the roster and provider key,
each person signs, each enrollment is recorded, and the conversation is started and controlled.

Manual PowerShell (worker/API/MCP tunnel already running; OPENAI_API_KEY in this shell):
  .venv\Scripts\python.exe scripts\agent_runtime.py --names Grant Kyle --mcp-url https://HOST/mcp

Keys: 1/2 select an agent, Space speaks once, H toggles hold, C cancels, Q quits.
Local controls: http://127.0.0.1:8090/agents. Provider keys are never written to logs.
"""
import argparse
import dataclasses
import asyncio
import base64
import hashlib
import hmac
import json
import os
import queue
import re
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import objectives as ob
import participation as pp
import turn_gate as tg
import voiceprint_client as vp
from arbitrator import Arbitrator, McpClient
from providers import make_provider
from providers.openai_responses import OpenAIResponses
from realtime_openai import Player, compose_prompt, describe_device, resample_16k_to_24k, resolve_device


@dataclass(frozen=True)
class AgentConfig:
    name: str
    provider: str = "openai_realtime"
    model: str = "gpt-realtime-2.1"
    voice: str = "marin"
    eagerness: str = "balanced"
    output_device: str = "HD 4.40,BenQ"
    instructions_extra: str = ""
    speaks_for: str = ""          # full name of the person in the room this agent represents, or empty
    role: str = "voice"           # "voice" (realtime, may be an advocate) or "arbitrator" (text only, negotiation rooms)


MAX_INSTRUCTIONS = 6000
PERSONA_DIR = Path(__file__).resolve().parents[1] / "data" / "agents"
PROVIDERS = ("openai_realtime", "xai_speech", "gemini_live", "openai_responses")
ARBITRATOR_MODEL = "gpt-5"
TOOL_NAMES = ("get_transcript", "get_current_speaker", "get_agent_channel", "post_agent_channel")


def with_role(config, role):
    """An arbitrator is a text model on the Responses API; a voice agent never is. Provider/model follow the role."""
    if role not in pp.ROLES:
        raise ValueError("Role must be voice or arbitrator")
    if role == "arbitrator":
        if config.provider != "openai_responses":
            return dataclasses.replace(config, role=role, provider="openai_responses", model=ARBITRATOR_MODEL)
    elif config.provider == "openai_responses":
        return dataclasses.replace(config, role=role, provider=AgentConfig.provider, model=AgentConfig.model)
    return dataclasses.replace(config, role=role)


def validate_room(conversation_type, configs):
    """docs/API.md V3 /setup rules: a negotiation has exactly two advocates for different people and one arbitrator;
    a casual room has no arbitrator. Human count and speaks_for membership are checked by the caller."""
    if conversation_type not in pp.CONVERSATION_TYPES:
        raise ValueError("Conversation type must be casual or negotiation")
    arbitrators = [c for c in configs if c.role == "arbitrator"]
    voices = [c for c in configs if c.role == "voice"]
    if conversation_type == "casual":
        if arbitrators:
            raise ValueError("An arbitrator only joins a negotiation; choose that conversation type or remove the arbitrator")
        return
    if len(voices) != 2:
        raise ValueError("A negotiation needs exactly two voice agents, one speaking for each side")
    principals = [c.speaks_for.strip().casefold() for c in voices]
    if not all(principals) or len(set(principals)) != 2:
        raise ValueError("Each negotiation voice agent must speak for a different person in the room")
    if len(arbitrators) != 1:
        raise ValueError("A negotiation needs exactly one arbitrator")


def clean_instructions(text):
    """Operator-authored standing instructions: plain text, bounded, no control characters except newlines and tabs."""
    if not isinstance(text, str):
        raise ValueError("Instructions must be text")
    text = re.sub(r"[^\t\n\x20-\x7e\u00a0-\uffff]", "", text.replace("\r\n", "\n")).strip()
    if len(text) > MAX_INSTRUCTIONS:
        raise ValueError(f"Instructions are limited to {MAX_INSTRUCTIONS} characters")
    return text


VOICES = ("alloy", "ash", "ballad", "cedar", "coral", "echo", "marin", "sage", "shimmer", "verse")
MAX_AGENTS = 4


def persona_path(name):
    """Collision-free per-agent file: readable slug plus a hash of the exact name, so 'A B' and 'A_B' never share."""
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:10]
    return PERSONA_DIR / (re.sub(r"[^A-Za-z0-9_-]", "_", name)[:40] + "-" + digest + ".json")


def persona_record(config):
    return {"name": config.name, "voice": config.voice, "eagerness": config.eagerness, "speaks_for": config.speaks_for,
            "instructions": config.instructions_extra, "role": config.role}


def load_personas():
    """Every persisted agent (operator text, not participant data), keyed by exact name. An explicit empty
    instructions value is kept as empty, so clearing a TOML default sticks between runs."""
    result = {}
    try:
        files = sorted(PERSONA_DIR.glob("*.json"))
    except OSError:
        return result
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            name = data.get("name")
            if not isinstance(name, str) or not NAME_RE.match(name) or persona_path(name) != path:
                continue
            result[name] = {"name": name, "voice": data.get("voice") if data.get("voice") in VOICES else None,
                            "eagerness": data.get("eagerness") if data.get("eagerness") in tg.SILENCE_AFTER_TURN_S else None,
                            "speaks_for": data.get("speaks_for") if isinstance(data.get("speaks_for"), str) else "",
                            "instructions": clean_instructions(data.get("instructions", "")),
                            "role": data.get("role") if data.get("role") in pp.ROLES else "voice"}
        except (OSError, ValueError):
            continue
    return result


def save_personas(configs):
    """The setup panel is what is saved: files for these agents are written, files for agents no longer listed are removed."""
    PERSONA_DIR.mkdir(parents=True, exist_ok=True)
    keep = set()
    for config in configs:
        path = persona_path(config.name)
        path.write_text(json.dumps(persona_record(config), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        keep.add(path)
    for path in PERSONA_DIR.glob("*.json"):
        if path not in keep:
            try:
                path.unlink()
            except OSError:
                pass


def merge_personas(configs):
    """agents.toml defaults with persisted values on top, plus persisted agents that are not in the file."""
    saved = load_personas()
    result, seen = [], set()
    for config in configs:
        record = saved.get(config.name)
        seen.add(config.name)
        if record is None:
            result.append(config)
        else:
            result.append(with_role(dataclasses.replace(config, voice=record["voice"] or config.voice, eagerness=record["eagerness"] or config.eagerness,
                                                        speaks_for=record["speaks_for"], instructions_extra=record["instructions"]), record["role"]))
    template = configs[0] if configs else AgentConfig("template")
    for name, record in saved.items():
        if name not in seen:
            result.append(with_role(dataclasses.replace(template, name=name, voice=record["voice"] or template.voice, eagerness=record["eagerness"] or template.eagerness,
                                                        speaks_for=record["speaks_for"], instructions_extra=record["instructions"]), record["role"]))
    return result[:MAX_AGENTS]


def load_config(path):
    with open(path, "rb") as stream:
        data = tomllib.load(stream)
    rows = data.get("agents")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Config needs at least one [[agents]] entry")
    configs, names = [], set()
    for row in rows:
        if not isinstance(row, dict) or set(row) - set(AgentConfig.__dataclass_fields__):
            raise ValueError("Unknown agent config field")
        config = AgentConfig(**row)
        for field in ("name", "provider", "model", "voice"):
            value = getattr(config, field)
            if not isinstance(value, str) or not value.strip() or len(value) > 200:
                raise ValueError(f"Agent {field} must be a nonempty string of at most 200 characters")
        if config.name.casefold() in names:
            raise ValueError("Agent names must be unique (ignoring case)")
        if config.provider not in PROVIDERS:
            raise ValueError("Unknown provider")
        if config.role not in pp.ROLES:
            raise ValueError("Role must be voice or arbitrator")
        if (config.provider == "openai_responses") != (config.role == "arbitrator"):
            raise ValueError("Provider openai_responses is for the arbitrator role only, and an arbitrator uses openai_responses")
        if config.eagerness not in tg.SILENCE_AFTER_TURN_S:
            raise ValueError("Eagerness must be quiet, balanced, or eager")
        if not isinstance(config.output_device, str) or not isinstance(config.instructions_extra, str) or not isinstance(config.speaks_for, str):
            raise ValueError("output_device, instructions_extra and speaks_for must be strings")
        if config.role == "arbitrator" and config.speaks_for.strip():
            raise ValueError("An arbitrator is neutral and cannot speak for anyone")
        clean_instructions(config.instructions_extra)
        names.add(config.name.casefold())
        configs.append(config)
    return configs


def misheard_name(state, agent_name):
    """The transcript's spelling of this agent's name in the newest human line when it addressed the agent inexactly
    (docs/API.md "Fuzzy addressing"), else None. Agent rows and the agent's own echo are skipped."""
    for row in reversed(state.history):
        if row.get("source") == "agent" or row.get("label") == "agent" or row.get("speaker_id") == state.agent_speaker_id:
            continue
        match = tg.addressed_as(row.get("text"), (agent_name,))
        if match and match[1].casefold() != agent_name.casefold():
            return match[1]
        return None
    return None


def reply_note(session_id, names, state, decision, arbitrator_prompt=None, spelled_as=None):
    """Exact live-earned pre-reply wording from realtime_openai.py; change only after a live run.
    Sentences are appended after the unchanged text only under new conditions (a reviewed label, clarify, a misspelled
    address, a verified arbitrator prompt), never woven into it; the arbitrator prompt stays last."""
    roster = ", ".join(names.values())
    last = state.history[-1] if state.history else None
    who = names.get(last.get("speaker_id"), "unknown") if last else "unknown"
    note = (f"(system) Voiceprint session_id is {session_id}. People in this room: {roster}. "
            f"The most recent line was spoken by {who} (label {last.get('label') if last else 'none'}). "
            "Call get_transcript with after_id from your last call, then answer that person by name. "
            "Only the newest line's label matters; earlier OVERLAP or low lines are history, not a reason to refuse. "
            "Labels high and medium are reliable enough to name the speaker.")
    if last and last.get("label") == "reviewed":
        note += " Label reviewed means the operator confirmed that speaker; treat it as reliable."
    if decision == "clarify":
        note += " The newest line's attribution is uncertain: ask who just spoke instead of answering."
    if spelled_as:
        note += f" The transcript wrote your name as '{spelled_as}'; that line is addressed to you."
    if arbitrator_prompt:
        note += f" The arbitrator asks you to raise this now: {arbitrator_prompt}"
    return note


class EventLog:
    def __init__(self, path, session_id, run_id=None, secrets=(), max_records=20000):
        if path is not None:
            raise vp.ConsentError("Disk --events logs are disabled until encrypted artifact registration and verified destruction exist; aggregate scoring uses bounded memory")
        self.session_id, self.run_id = session_id, run_id or uuid.uuid4().hex
        self.secrets = tuple(s for s in secrets if s)
        self.lock = threading.Lock()
        self.records = deque(maxlen=max_records)
        self.dropped = 0
        self.closed = False

    def clean(self, value):
        if isinstance(value, dict):
            return {k: self.clean(v) for k, v in value.items()
                    if k.lower() not in ("authorization", "api_key", "access_token", "client_secret", "token")}
        if isinstance(value, list):
            return [self.clean(v) for v in value]
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "[redacted]")
        return value

    def emit(self, kind, value, agent=None, participant_id=None, **extra):
        if self.closed:
            return
        # Positive allowlist: provider payloads can echo keys and human words in many fields.
        fields = {
            "openai": ("type", "response_id", "item_id", "output_index", "content_index"),
            "utterance": ("utterance_id", "response_id", "source", "start_ms", "end_ms", "label"),
            "attribution": ("sequence", "start_ms", "end_ms", "status", "overlap", "confidence_kind"),
            "floor": ("action", "granted", "held_by", "expires_at_ms", "server_time_ms", "released", "reason"),
            "control": ("action", "value", "held", "reason"),
            "playback": ("action", "response_id", "audio_timeline_ms", "timing_basis", "includes_mute_tail"),
            "runtime": ("action",), "gate": ("decision", "audio_timeline_ms"),
            "arbitrator": ("action", "trigger", "tag", "confirmed", "ingested_rows", "generations", "tier", "redactions"),
            "guard": ("action", "stage", "redactions"),
            "summary": ("action", "attempts", "board_rows", "transcript_rows", "reason"),
        }
        original = value
        value = {k: original[k] for k in fields.get(kind, ()) if k in original}
        if kind == "openai" and isinstance(original.get("response"), dict):
            response = original["response"]
            value["response"] = {k: response[k] for k in ("id", "status") if k in response}
            usage = response.get("usage") or {}
            value["response"]["usage"] = {k: usage[k] for k in ("input_tokens", "output_tokens", "total_tokens")
                                             if type(usage.get(k)) in (int, float)}
            value["response"]["output"] = [{k: item[k] for k in ("id", "type") if k in item}
                                             for item in response.get("output", [])]
            for safe, item in zip(value["response"]["output"], response.get("output", [])):
                if item.get("type") == "mcp_call":
                    safe.update(self.tool_evidence(item))
        if kind == "openai" and isinstance(original.get("item"), dict):
            item = original["item"]
            value["item"] = {k: item[k] for k in ("id", "type") if k in item}
            if item.get("type") == "mcp_call":
                value["item"].update(self.tool_evidence(item))
        row = {"run_id": self.run_id, "session_id": self.session_id, "agent": agent,
               "participant_id": participant_id, "timestamp_ms": int(time.time() * 1000),
               "monotonic_ms": time.monotonic() * 1000, kind: value}
        if "capture_end_to_response_ms" in extra:
            row["capture_end_to_response_ms"] = extra["capture_end_to_response_ms"]
        with self.lock:
            if not self.closed:
                if len(self.records) == self.records.maxlen:
                    self.dropped += 1
                self.records.append(self.clean(row))

    @staticmethod
    def tool_evidence(item):
        output = item.get("output")
        return {"name": item.get("name") if item.get("name") in TOOL_NAMES else "other",
                "output_bytes": len(str(output).encode("utf-8")) if output is not None else 0,
                "succeeded": item.get("error") is None and output is not None}

    def write(self, line):
        row = json.loads(line)
        kind = "utterance" if "utterance" in row else "attribution"
        self.emit(kind, row.pop(kind), **row)

    def flush(self):
        pass  # RAM records append atomically; there is no disk destination

    def snapshot(self):
        with self.lock:
            return list(self.records)

    def close(self):
        with self.lock:
            self.closed = True
            self.records.clear()

    def summarize(self):
        try:
            from score_run import summarize_events
            print(summarize_events(self.snapshot()), flush=True)
        except ImportError:
            print("Aggregate scorer unavailable; in-memory events will still be cleared.", flush=True)
        except Exception:
            print("Aggregate scoring failed; in-memory events will still be cleared.", flush=True)
        if self.dropped:
            print(f"Scoring incomplete: {self.dropped} oldest metadata records evicted from bounded memory; floor totals are not complete.", flush=True)


class RestClient:
    def __init__(self, base, token=None, timeout=3):
        self.base, self.token, self.timeout = base.rstrip("/"), token, timeout

    def _request(self, method, path, body):
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        raw = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, raw, headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            # The server's status suffices; do not reflect credentials or arbitrary response text.
            failure = RuntimeError(f"Voiceprint {method} {path.split('?')[0]} returned HTTP {error.code}")
            failure.status = error.code
            raise failure from None

    async def request(self, method, path, body=None):
        return await asyncio.to_thread(self._request, method, path, body)


class LoggedPlayer(Player):
    """Original PCM callback with actual first-play evidence and an aborting cancel."""
    def __init__(self, device, tail_s, on_playback, timeline):
        self.on_playback, self.timeline = on_playback, timeline
        self.response_id, self.started = None, False
        super().__init__(device, tail_s)

    def begin(self, response_id):
        self.response_id = response_id

    def _callback(self, out, frames, timing, status):
        need = frames * 2
        with self.lock:
            buf = b"".join(self.queue)
            self.queue = [buf[need:]] if len(buf) > need else []
        chunk = buf[:need]
        if chunk:
            self.last_audio_at = time.monotonic()
            if not self.started:
                self.started = True
                self.on_playback({"action": "started", "response_id": self.response_id,
                                  "audio_timeline_ms": self.timeline(), "timing_basis": "played"})
        out[:] = chunk + b"\x00" * (need - len(chunk))

    def drained(self, cancelled=False):
        if self.started:
            self.on_playback({"action": "cancelled" if cancelled else "drained", "response_id": self.response_id,
                              "audio_timeline_ms": self.timeline(), "timing_basis": "played",
                              "includes_mute_tail": not cancelled})
        self.started = False

    def flush(self):
        self.stream.abort()
        super().flush()
        self.drained(cancelled=True)
        # Continue muting the room for the configured hardware tail after abort.
        self.last_audio_at = time.monotonic()
        self.stream.start()

    def close(self):
        self.stream.abort()
        self.stream.close()


class Agent:
    LEASE_MS = 15000
    MAX_CONTINUATIONS = 3

    def __init__(self, config, participant_id, provider, player, room):
        self.config, self.participant_id = config, participant_id
        self.provider, self.player, self.room = provider, player, room
        self.gate = tg.GateState((config.name,), config.eagerness, participant_id,
                                room_agent_names=tuple(c.name for c in room.configs))
        pp.apply_mode(self.gate, pp.default_mode(room.conversation_type, config.role, config.speaks_for))
        # An advocate holds only its OWN principal's constraint values; it never sees the other side's.
        self.principal_id = room.principal_of(config)
        self.guard_values = ob.constraint_values([room.objectives[self.principal_id]]) if self.principal_id in room.objectives else ()
        self.pending_prompt = None        # verified arbitrator prompt, delivered through the next pre-reply note
        self.deltas = {}                  # (response_id, item_id) -> streamed transcript so far, for the leak guard
        self.active = False
        self.final_done = False
        self.cancelled = False
        self.floor_owned = False
        self.deadline = 0
        self.renew_at = 0
        self.continuations = 0
        self.continue_pending = False
        self.tool_calls = 0
        self.response_id = None
        self.turn_response_ids = set()
        self.ignored_response_ids = set()
        self.pending_posts = 0
        self.transcript_ids = set()
        self.start_ms = 0
        self.mcp = {"list_tools": "unknown", "tools": [], "calls": 0, "failed": 0, "last_error": None}

    @property
    def floor_path(self):
        return self.room.path + "/floor"

    def emit(self, kind, value):
        self.room.log.emit(kind, value, self.config.name, self.participant_id)

    def state(self):
        return {"name": self.config.name, "participant_id": self.participant_id, "role": self.config.role,
                "provider": self.config.provider, "model": self.config.model, "voice": self.config.voice,
                "eagerness": self.gate.eagerness, "held": self.gate.manual == "hold",
                "responding": self.active or self.player.busy(), "mcp": dict(self.mcp)}

    async def update_objective(self, objective):
        """A new version of this advocate's own principal's objective: refresh the guard and the open session's prompt."""
        self.guard_values = ob.constraint_values([objective])
        principal_name = self.room.names.get(self.principal_id)
        if hasattr(self.provider, "objective"):
            self.provider.objective, self.provider.principal_name = objective, principal_name
        await self.provider.update_instructions(compose_prompt(self.config, self.room.session_id, self.room.names, objective, principal_name))
        self.emit("runtime", {"action": "objective_updated"})

    @staticmethod
    def safe_error(item):
        error = item.get("error")
        if error is None:
            return None
        text = error if isinstance(error, str) else json.dumps(error, sort_keys=True)
        return re.sub(r"[^\x20-\x7e]", "?", text)[:200]

    def note_tool_item(self, item):
        """Provider-declared MCP outcomes. This is what OpenAI says it did; the server's own proof rows are the evidence.
        A failed tools listing means the model has no tool and will only talk about fetching the transcript."""
        kind = item.get("type")
        if kind == "mcp_list_tools":
            error = self.safe_error(item)
            tools = [t.get("name") for t in item.get("tools") or [] if isinstance(t, dict)]
            self.mcp["list_tools"] = "failed" if error or item.get("status") == "failed" else "ok"
            self.mcp["tools"] = [t for t in tools if t in TOOL_NAMES]
            self.mcp["last_error"] = error or (item.get("status") if item.get("status") == "failed" else None)
            print(f"{self.config.name}: provider {'FAILED to list' if self.mcp['list_tools'] == 'failed' else 'listed'} the MCP tools"
                  + (f" ({self.mcp['last_error']})" if self.mcp["last_error"] else f" {self.mcp['tools']}") + "; the server log is the independent record", flush=True)
        elif kind == "mcp_call":
            error = self.safe_error(item)
            name = item.get("name") if item.get("name") in TOOL_NAMES else "other"
            self.mcp["calls"] += 1
            if error:
                self.mcp["failed"] += 1
                self.mcp["last_error"] = error
            print(f"{self.config.name}: provider reports MCP {name} -> {'failed: ' + error if error else 'ok'}", flush=True)

    async def claim(self, renew=False):
        sent_at = time.monotonic()
        result = await self.room.api.request("POST", self.floor_path,
                                             {"participant_id": self.participant_id, "lease_ms": self.LEASE_MS})
        self.emit("floor", {"action": "renew" if renew else "claim", **result})
        granted = result.get("granted") and result.get("held_by") == self.participant_id
        if granted:
            # Subtract the whole request duration, never extending a lease using the local wall clock.
            duration = max(0, result["expires_at_ms"] - result["server_time_ms"]) / 1000
            self.deadline = sent_at + duration
            self.renew_at = sent_at + min(duration / 3, 5)
            granted = self.deadline > time.monotonic()
        self.floor_owned = bool(granted)
        return self.floor_owned

    async def release(self):
        if self.floor_owned:
            result = await self.room.api.request("DELETE", self.floor_path + "?participant_id=" + quote(self.participant_id))
            self.emit("floor", {"action": "release", **result})
        self.floor_owned = False

    async def cancel(self, reason="manual"):
        self.cancelled = True
        self.continue_pending = False
        self.tool_calls = 0
        self.ignored_response_ids.update(self.turn_response_ids)
        self.deltas.clear()
        self.player.flush()
        self.final_done = True
        if self.active:
            try:
                await asyncio.wait_for(self.provider.cancel(), timeout=2)
            except Exception:
                self.emit("runtime", {"action": "cancel_transport_failed"})
        self.emit("control", {"action": "cancel", "reason": reason})

    async def begin(self, decision):
        if not await self.claim():
            return False
        self.active, self.final_done, self.cancelled = True, False, False
        self.continuations, self.tool_calls = 0, 0
        self.continue_pending = False
        self.response_id = None
        self.turn_response_ids.clear()
        self.deltas.clear()
        self.start_ms = self.room.timeline_ms
        if self.gate.manual in ("speak", "override"):
            self.gate.manual = None
        prompt, self.pending_prompt = self.pending_prompt, None
        if prompt and self.room.arbitrator is not None:
            self.room.arbitrator.pending_tag = None
        self.emit("gate", {"decision": decision, "audio_timeline_ms": self.start_ms})
        print(f"  [{self.config.name} gate: {decision}]", flush=True)
        try:
            await self.provider.request_reply(reply_note(self.room.session_id, self.room.names, self.gate, decision, prompt,
                                                         misheard_name(self.gate, self.config.name)))
        except Exception:
            await self.cancel("request_failed")
            raise
        return True

    async def tick(self, now):
        if self.active or self.player.busy():
            if self.floor_owned and now >= self.deadline:
                self.floor_owned = False
                self.emit("floor", {"action": "lost", "reason": "lease_expired"})
                await self.cancel("lease_expired")
            elif self.floor_owned and now >= self.renew_at:
                try:
                    granted = await self.claim(renew=True)
                except Exception:
                    granted = False
                if not granted:
                    self.floor_owned = False
                    self.emit("floor", {"action": "lost", "reason": "renewal_failed"})
                    await self.cancel("lease_lost")
            if self.continue_pending and self.tool_calls == 0 and not self.cancelled and self.floor_owned:
                self.continue_pending = False
                await self.provider.request_reply()  # wait for completed MCP, preserve the same floor and turn
            if self.final_done and self.pending_posts == 0 and not self.player.busy():
                if hasattr(self.player, "drained"):
                    self.player.drained()
                await self.release()
                self.active = False
            return
        if self.gate.others_speaking or self.room.any_active() or not self.room.transcriber_idle():
            return
        # decide consumes a manual one-shot; preserve it until the API actually grants the floor.
        manual = self.gate.manual
        decision = tg.decide(self.gate, now, self.room.status, self.room.last_end_at)
        if manual in ("speak", "override"):
            self.gate.manual = manual
        if decision != "wait":
            await self.begin(decision)

    async def event(self, event):
        kind = event.get("type")
        self.emit("openai", event)
        response_id = event.get("response_id") or event.get("response", {}).get("id")
        if response_id in self.ignored_response_ids:
            return
        if kind == "error":
            print(f"{self.config.name}: provider error ({event.get('error', {}).get('code', 'unknown')})", file=sys.stderr, flush=True)
            # An already-cancelled turn never re-cancels: OpenAI answers a response.cancel sent after the response finished
            # with an error (response_cancel_not_active), and cancelling again would send another and loop until release.
            if self.active and not self.cancelled:
                await self.cancel("provider_error")
            return
        if not self.active or self.cancelled:
            return
        if self.room.consent is not None and self.room.consent.failed.is_set():
            await self.cancel("consent_stopped")
            return
        if response_id:
            self.turn_response_ids.add(response_id)
        if kind == "response.created":
            self.response_id = response_id
            self.start_ms = self.room.timeline_ms
        elif kind == "response.output_audio.delta":
            if not self.floor_owned or time.monotonic() >= self.deadline:
                await self.cancel("audio_without_lease")
                return
            if hasattr(self.player, "begin"):
                self.player.begin(response_id or self.response_id)
            self.player.play(base64.b64decode(event["delta"]))
        elif kind == "response.output_audio_transcript.delta":
            # Voice-leak guard (best effort: audio already rendered cannot be recalled, docs/TURN_TAKING.md §9).
            if self.guard_values:
                key = (response_id or self.response_id, event.get("item_id"))
                self.deltas[key] = self.deltas.get(key, "") + str(event.get("delta") or "")
                _, hits = ob.redact(self.deltas[key], self.guard_values)
                if hits:
                    self.emit("guard", {"action": "cut", "stage": "spoken_delta", "redactions": hits})
                    print(f"{self.config.name}: spoken output matched a private constraint; reply cut", flush=True)
                    await self.cancel("leak_guard")
        elif kind == "response.output_audio_transcript.done":
            key = (response_id or self.response_id, event.get("item_id"), event.get("content_index", 0))
            self.deltas.pop(key[:2], None)
            if key in self.transcript_ids or not event.get("transcript", "").strip():
                return
            self.transcript_ids.add(key)
            text, hits = ob.redact(event["transcript"], self.guard_values) if self.guard_values else (event["transcript"], 0)
            if hits:
                self.emit("guard", {"action": "redacted", "stage": "stored_utterance", "redactions": hits})
            row = {"speaker_id": self.participant_id, "start_ms": self.start_ms,
                   "end_ms": max(self.start_ms, self.room.timeline_ms), "text": text, "source": "agent"}
            self.pending_posts += 1
            try:
                await self.room.authorized()
                stored = await self.room.api.request("POST", self.room.path + "/utterances", row)
                row.update(utterance_id=stored["utterance_id"], label=stored["label"], response_id=key[0])
                self.emit("utterance", row)
                print(f"{self.config.name}: stored agent turn #{row['utterance_id']}; text available in local GUI.", flush=True)
                # Only the GET bus publishes this row to gates, after it was persisted successfully.
            finally:
                self.pending_posts -= 1
        elif kind == "response.mcp_call.in_progress":
            self.tool_calls += 1
        elif kind in ("response.mcp_call.completed", "response.mcp_call.failed"):
            self.tool_calls = max(0, self.tool_calls - 1)
        elif kind == "response.output_item.done":
            self.note_tool_item(event.get("item") or {})
        elif kind == "response.done":
            response = event.get("response", {})
            output = response.get("output", [])
            spoke = any(item.get("type") == "message" for item in output)
            called_tool = any(item.get("type") == "mcp_call" for item in output)
            if response.get("status") in ("cancelled", "failed", "incomplete"):
                await self.cancel("response_" + response["status"])
            elif called_tool and not spoke and self.continuations < self.MAX_CONTINUATIONS:
                self.continuations += 1
                self.continue_pending = True
            else:
                self.final_done = True
                if spoke:
                    self.gate.note_agent_spoke()


class Room:
    def __init__(self, configs, session_id, names, api, log, consent=None, conversation_type="casual"):
        self.configs, self.session_id, self.names = configs, session_id, names
        self.api, self.log = api, log
        self.path = "/speaker/session/" + quote(session_id)
        self.agents = []                  # voice agents only; the arbitrator has no floor, player or provider events
        self.arbitrator = None
        self.conversation_type = conversation_type
        self.objectives = {}              # principal_id -> objectives.Objective (latest version)
        self.objectives_forbidden = False
        self.status, self.timeline_ms, self.last_end_at = "silence", 0, None
        self.cursor, self.seen = 0, set()
        self.controls = queue.Queue()
        self.transcriber_idle = lambda: True
        self.consent = consent

    async def authorized(self):
        if self.consent is not None:
            return await asyncio.to_thread(vp.require_consent, self.consent)

    def any_active(self):
        return any(a.active or a.player.busy() for a in self.agents)

    def principal_of(self, config):
        """Participant id of the human this agent speaks for, or None."""
        wanted = (config.speaks_for or "").strip().casefold()
        if not wanted:
            return None
        return next((pid for pid, name in self.names.items() if name.casefold() == wanted), None)

    def state(self):
        agents = [a.state() for a in self.agents]
        if self.arbitrator is not None:
            agents.append(self.arbitrator.state())
        return {"session_id": self.session_id, "conversation_type": self.conversation_type, "agents": agents}

    def request_override(self, prompt_to, prompt, tag):
        """A verified arbitrator prompt: the advocate's next opportunity is an override turn carrying the prompt."""
        agent = next((a for a in self.agents if a.participant_id == prompt_to or a.config.name == prompt_to), None)
        if agent is None or agent.gate.manual == "hold":
            return False
        agent.gate.manual = "override"
        agent.pending_prompt = prompt
        agent.emit("control", {"action": "override", "value": tag, "held": False})
        return True

    def cancel_override(self):
        for agent in self.agents:
            if agent.gate.manual == "override":
                agent.gate.manual = None
            agent.pending_prompt = None

    async def refresh_objectives(self):
        """Latest objective per principal from the API (the runtime is the only reader). Hands both to the arbitrator and
        each advocate only its own; a new version re-prompts that advocate's open session."""
        if self.objectives_forbidden:
            return
        try:
            result = await self.api.request("GET", self.path + "/objectives")
        except RuntimeError as error:
            if getattr(error, "status", None) == 403:
                self.objectives_forbidden = True
                print("Objectives are not readable for this room (negotiation_text scope missing); agents run without them", flush=True)
                return
            raise
        parsed = ob.parse_objectives(result)
        changed = {pid for pid, o in parsed.items() if pid not in self.objectives or self.objectives[pid].version != o.version}
        self.objectives = parsed
        if self.arbitrator is not None:
            self.arbitrator.objectives = parsed
        for agent in self.agents:
            if agent.principal_id in parsed:
                if agent.principal_id in changed:
                    await agent.update_objective(parsed[agent.principal_id])
                else:
                    agent.guard_values = ob.constraint_values([parsed[agent.principal_id]])

    async def register(self):
        next_id = 1
        for config in self.configs:
            if config.name.casefold() in (n.casefold() for n in self.names.values()):
                raise ValueError("Human and agent names must be distinct")
            while f"participant_{next_id}" in self.names:
                next_id += 1
            participant_id = f"participant_{next_id}"
            await self.api.request("POST", self.path + "/participants", {"id": participant_id, "name": config.name,
                                   "kind": "agent", "provider": config.provider, "model": config.model})
            self.names[participant_id] = config.name
        return [pid for pid, name in self.names.items() if name in {c.name for c in self.configs}]

    async def poll_bus(self):
        await self.authorized()
        while True:
            result = await self.api.request("GET", self.path + f"/utterances?after_id={self.cursor}&limit=200")
            rows = result.get("utterances", [])
            for row in rows:
                uid = row["utterance_id"]
                if uid in self.seen:
                    continue
                self.seen.add(uid)
                for agent in self.agents:
                    agent.gate.note_utterance(row)
                self.last_end_at = time.monotonic()
            previous = self.cursor
            self.cursor = result.get("next_after_id", self.cursor)
            if len(rows) < 200 or self.cursor <= previous:
                break

    async def apply_controls(self):
        while not self.controls.empty():
            name, action, value = self.controls.get_nowait()
            agent = next((a for a in self.agents if a.config.name == name), None)
            if agent is None:
                if self.arbitrator is not None and self.arbitrator.config.name == name:
                    await self.arbitrator.control(action, value)
                continue
            if action == "speak":
                agent.gate.manual = "speak"
            elif action == "hold":
                if agent.gate.manual == "hold":
                    agent.gate.manual = None
                else:
                    agent.gate.manual = "hold"
                    await agent.cancel("hold")
            elif action == "cancel":
                await agent.cancel()
            elif action == "eagerness":
                agent.gate.eagerness = value
            agent.emit("control", {"action": action, "value": value, "held": agent.gate.manual == "hold"})


PHASES = ("setup", "consent", "enrollment", "connecting", "ready", "live", "ending", "ended", "failed")
NAME_RE = re.compile(r"^[^\x00-\x1f]{1,200}$")


class Runtime:
    """Lifecycle shared between main() and the local control server, so the GUI can drive every step
    that used to be a console prompt: roster/contacts (and the provider key), each enrollment recording,
    the start of the conversation, and shutdown. Consent itself is still signed by each person in the GUI
    against the API; this object never bypasses that check."""

    def __init__(self, room=None, gui=False, mcp_url=None, api_token=None, configs=()):
        self.lock = threading.Lock()
        self.room = room
        self.gui = gui
        # Persisted personas win over agents.toml so each person's agent keeps its flavor between runs.
        self.configs = merge_personas(list(configs))
        self.session_configs = None
        self.conversation_type = room.conversation_type if room is not None else "casual"
        self.mcp_url, self.api_token = mcp_url, api_token
        self.phase = "live" if room is not None else "setup"
        self.detail = ""
        self.session_id = room.session_id if room is not None else None
        self.setup = queue.Queue()
        self.records = queue.Queue()
        self.start = threading.Event()
        self.stop = threading.Event()
        self.participants = []       # [{"id","name"}] humans, in enrollment order
        self.enrollment = {}         # participant_id -> {"state": pending|waiting|recording|recorded|rejected, "peak": int|None}
        self.awaiting = None         # participant_id whose recording the runtime is waiting to be triggered

    def set_phase(self, phase, detail=""):
        if phase not in PHASES:
            raise ValueError(phase)
        with self.lock:
            self.phase, self.detail = phase, detail
        print(f"[{phase}] {detail}" if detail else f"[{phase}]", flush=True)

    def needs_openai_key(self):
        return not os.environ.get("OPENAI_API_KEY")

    def state(self):
        with self.lock:
            agents = self.room.state()["agents"] if self.room is not None else []
            return {"session_id": self.session_id, "conversation_type": self.conversation_type, "agents": agents,
                    "phase": self.phase, "detail": self.detail,
                    "participants": [dict(p, enrollment=self.enrollment.get(p["id"], {"state": "pending", "peak": None}))
                                     for p in self.participants],
                    "awaiting": self.awaiting, "needs_openai_key": self.needs_openai_key(),
                    "agent_configs": [{"name": c.name, "role": c.role, "voice": c.voice, "model": c.model, "eagerness": c.eagerness,
                                       "instructions": c.instructions_extra, "speaks_for": c.speaks_for} for c in (self.session_configs or self.configs)],
                    "voices": list(VOICES), "eagerness_levels": list(tg.SILENCE_AFTER_TURN_S), "max_agents": MAX_AGENTS,
                    "roles": list(pp.ROLES), "conversation_types": list(pp.CONVERSATION_TYPES),
                    "mcp_configured": bool(self.mcp_url), "mcp_url": self.mcp_url, "gui": self.gui}

    def bootstrap(self):
        """Loopback-GUI convenience: only a runtime started with --gui hands the operator token to the page
        served by the same API, so the operator does not paste it. Non-GUI runtimes reveal nothing."""
        with self.lock:
            return {"phase": self.phase, "session_id": self.session_id, "gui": self.gui,
                    "api_token": self.api_token if self.gui else None}

    def submit_setup(self, body):
        with self.lock:
            if self.phase != "setup":
                raise ValueError("Roster can only be set while the runtime is waiting for setup")
        people = body.get("participants")
        if not isinstance(people, list) or not 2 <= len(people) <= 4:
            raise ValueError("A room needs two to four people within microphone range")
        names, contacts = [], []
        for person in people:
            if not isinstance(person, dict):
                raise ValueError("Invalid participant")
            name, contact = person.get("name"), person.get("contact")
            if not isinstance(name, str) or not isinstance(contact, str) or not NAME_RE.match(name.strip()) or not NAME_RE.match(contact.strip()):
                raise ValueError("Each participant needs a full name and an email or phone")
            names.append(name.strip()); contacts.append(contact.strip())
        if len({n.casefold() for n in names}) != len(names):
            raise ValueError("Participant names must be distinct")
        session = body.get("session_id")
        if session is not None and (not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", session)):
            raise ValueError("Invalid room ID")
        key = body.get("openai_api_key")
        if key is not None and (not isinstance(key, str) or not re.fullmatch(r"[\x21-\x7e]{20,400}", key.strip())):
            raise ValueError("Invalid provider key")
        if self.needs_openai_key() and not key:
            raise ValueError("The OpenAI API key is required to start the agents")
        conversation_type = body.get("conversation_type", "casual")
        if conversation_type not in pp.CONVERSATION_TYPES:
            raise ValueError("Conversation type must be casual or negotiation")
        session_configs = self.configs
        if "agents" in body:
            session_configs = self.build_agents(body.get("agents"), names)
        validate_room(conversation_type, session_configs)
        if key:
            # Held only in this process's environment for the provider transport; never logged or written.
            os.environ["OPENAI_API_KEY"] = key.strip()
        with self.lock:
            self.session_configs = session_configs
            self.conversation_type = conversation_type
        if "agents" in body:
            save_personas(session_configs)
        self.setup.put({"names": names, "contacts": contacts, "session_id": session})

    def build_agents(self, rows, human_names):
        """One separate agent instance per row, each with its own prompt. Unknown names become new agents based on the
        first configured one (provider, model, output device); nothing carries over from another agent's instructions."""
        if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_AGENTS:
            raise ValueError(f"Configure one to {MAX_AGENTS} agents")
        by_name = {c.name: c for c in self.configs}
        template = self.configs[0] if self.configs else AgentConfig("template")
        humans = {n.casefold() for n in human_names}
        result, seen = [], set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("Invalid agent")
            name = row.get("name")
            if not isinstance(name, str) or not NAME_RE.match(name.strip()) or len(name.strip()) > 60:
                raise ValueError("Each agent needs a name of at most 60 characters")
            name = name.strip()
            if name.casefold() in seen or name.casefold() in humans:
                raise ValueError(f"Agent name {name} must be distinct from the other agents and from the people in the room")
            seen.add(name.casefold())
            base = by_name.get(name, dataclasses.replace(template, name=name, speaks_for="", instructions_extra=""))
            voice = row.get("voice", base.voice)
            eagerness = row.get("eagerness", base.eagerness)
            if voice not in VOICES or eagerness not in tg.SILENCE_AFTER_TURN_S:
                raise ValueError(f"{name}: voice must be one of {', '.join(VOICES)} and eagerness quiet, balanced or eager")
            role = row.get("role", base.role)
            if role not in pp.ROLES:
                raise ValueError(f"{name}: role must be voice or arbitrator")
            speaks_for = row.get("speaks_for", "")
            if not isinstance(speaks_for, str):
                raise ValueError("speaks_for must be a name")
            speaks_for = speaks_for.strip()
            if speaks_for and speaks_for.casefold() not in humans:
                raise ValueError(f"{name} can only speak for a person in the room")
            if speaks_for and role == "arbitrator":
                raise ValueError(f"{name} is the arbitrator and stays neutral; it cannot speak for anyone")
            result.append(with_role(dataclasses.replace(base, name=name, voice=voice, eagerness=eagerness, speaks_for=speaks_for,
                                                        instructions_extra=clean_instructions(row.get("instructions", base.instructions_extra))), role))
        return result

    def active_configs(self):
        return list(self.session_configs if self.session_configs is not None else self.configs)

    def request_record(self, participant_id):
        with self.lock:
            if self.phase != "enrollment" or participant_id != self.awaiting:
                raise ValueError("That participant is not the one the runtime is waiting to record")
        self.records.put(participant_id)

    def request_start(self):
        with self.lock:
            if self.phase != "ready":
                raise ValueError("The runtime is not ready to start")
        self.start.set()

    def request_stop(self):
        self.stop.set()
        self.start.set()
        self.records.put(None)
        self.setup.put(None)

    def wait_setup(self):
        while not self.stop.is_set():
            try:
                item = self.setup.get(timeout=.5)
            except queue.Empty:
                continue
            if item is None:
                break
            return item
        raise RuntimeError("Stopped before setup")

    def recorder(self, microphone, consent, console=True):
        """Per-name recorder for vp.enroll: waits for the GUI (or Enter in the console) before opening the mic."""
        ids = {p["name"]: p["id"] for p in self.participants}
        sentence = ("I, {name}, consent to Voiceprint collecting my voiceprint for identifying consenting speakers and "
                    "providing a speaker-attributed transcript during the current room conversation today.")

        def record(name):
            import numpy as np
            pid = ids[name]
            vp.require_consent(consent)
            with self.lock:
                self.enrollment[pid] = {"state": "waiting", "peak": None}
                self.awaiting = pid
            self.set_phase("enrollment", f"Waiting to record {name}")
            print(f"{name}: press Record in the GUI" + (" or Enter here" if console else "") + f", then speak for 8 seconds, starting: {sentence.format(name=name)}", flush=True)
            if console:
                threading.Thread(target=lambda: (input(), self.records.put(pid)), daemon=True).start()
            while True:
                if self.stop.is_set():
                    raise RuntimeError("Stopped during enrollment")
                try:
                    item = self.records.get(timeout=.5)
                except queue.Empty:
                    continue
                if item == pid:
                    break
            with self.lock:
                self.enrollment[pid] = {"state": "recording", "peak": None}
                self.awaiting = None
            self.set_phase("enrollment", f"Recording {name} for 8 seconds")
            with vp.Microphone(microphone, consent=consent) as capture:
                pcm = b"".join(capture.get()[0] for _ in range(32))
            vp.require_consent(consent)
            audio = np.frombuffer(pcm, dtype="<i2")
            level = max(abs(int(audio.min())), int(audio.max()))
            with self.lock:
                self.enrollment[pid] = {"state": "recorded", "peak": level}
            self.set_phase("enrollment", f"Recorded {name}: peak level {level} of 32767" + ("  (too quiet: move closer or raise input gain)" if level < 1500 else ""))
            return pcm
        return record

    def reset_enrollment(self, message):
        with self.lock:
            for p in self.participants:
                self.enrollment[p["id"]] = {"state": "rejected", "peak": None}
        self.set_phase("enrollment", message)

    def note_enrolled(self, response):
        """The accepted init response (vp.enroll on_response): participants[].profile_seeded says whose retained
        voiceprint seeded this session's profile (V3.1 contract), surfaced as enrollment.profile_seeded in GET /agents."""
        with self.lock:
            for p in (response or {}).get("participants") or []:
                pid = p.get("id")
                if not isinstance(pid, str):
                    continue
                entry = dict(self.enrollment.get(pid) or {"state": "recorded", "peak": None})
                entry["profile_seeded"] = p.get("profile_seeded") is True
                self.enrollment[pid] = entry


def control_server(target, port=8090, api_origin="http://127.0.0.1:8080", token=None):
    runtime = target if isinstance(target, Runtime) else Runtime(room=target)
    origin = urlsplit(api_origin)
    if origin.scheme != "http" or origin.hostname not in ("127.0.0.1", "localhost") or origin.path or origin.query or origin.fragment:
        raise ValueError("The control API origin must be a loopback HTTP origin without a path")
    allowed_origins = {"http://127.0.0.1:8080", "http://localhost:8080", api_origin}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def answer(self, status, body=None):
            data = json.dumps(body if body is not None else {}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            request_origin = self.headers.get("Origin")
            if request_origin in allowed_origins:
                self.send_header("Access-Control-Allow-Origin", request_origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Methods", "GET, POST")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.end_headers()
            self.wfile.write(data)

        def allowed(self, preflight=False, public=False):
            hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            if self.headers.get("Host") not in hosts:
                self.answer(403, {"error": "invalid_host"})
                return False
            if self.headers.get("Origin") is not None and self.headers["Origin"] not in allowed_origins:
                self.answer(403, {"error": "invalid_origin"})
                return False
            if token and not preflight and not public and not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                self.answer(401, {"error": "unauthorized"})
                return False
            return True

        def body(self, limit=4096):
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= limit:
                raise ValueError()
            data = json.loads(self.rfile.read(length)) if length else {}
            if not isinstance(data, dict):
                raise ValueError()
            return data

        def do_OPTIONS(self):
            if self.allowed(preflight=True):
                self.answer(200)

        def do_GET(self):
            if self.path == "/bootstrap":
                # Only the page served by the local API (exact loopback Origin) can read this; it is the
                # no-paste handoff from the launcher-started runtime to the console on the same computer.
                if self.allowed(public=True):
                    self.answer(200, runtime.bootstrap())
                return
            if self.allowed():
                if self.path == "/agents":
                    self.answer(200, runtime.state())
                else:
                    self.answer(404, {"error": "not_found"})

        def do_POST(self):
            if not self.allowed():
                return
            try:
                if self.path == "/setup":
                    runtime.submit_setup(self.body(262144))
                    self.answer(200, {"ok": True})
                    return
                if self.path == "/enrollment/record":
                    participant = self.body().get("participant_id")
                    if not isinstance(participant, str):
                        raise ValueError()
                    runtime.request_record(participant)
                    self.answer(200, {"ok": True})
                    return
                if self.path == "/start":
                    self.body(); runtime.request_start(); self.answer(200, {"ok": True})
                    return
                if self.path == "/stop":
                    self.body(); runtime.request_stop(); self.answer(200, {"ok": True})
                    return
            except ValueError as error:
                self.answer(409 if str(error) else 400, {"error": "invalid_request", "message": str(error) or "Invalid request"})
                return
            match = re.fullmatch(r"/agents/([^/]+)/control", self.path)
            name = unquote(match[1]) if match else None
            room = runtime.room
            # Read the body before answering: a reply sent while the request body is unread resets the socket on Windows.
            try:
                body = self.body()
                if not 0 < int(self.headers.get("Content-Length", "0")):
                    raise ValueError()
            except (ValueError, TypeError):
                body = None
            agent = next((a for a in room.agents if a.config.name == name), None) if room is not None else None
            if agent is None and room is not None and room.arbitrator is not None and room.arbitrator.config.name == name:
                agent = room.arbitrator
            if agent is None:
                self.answer(404, {"error": "unknown_agent"})
                return
            actions = ("speak", "hold", "cancel") if agent is room.arbitrator else ("speak", "hold", "cancel", "eagerness")
            action, value = (body.get("action"), body.get("value")) if body is not None else (None, None)
            if action not in actions or (action == "eagerness" and value not in tg.SILENCE_AFTER_TURN_S):
                self.answer(400, {"error": "invalid_control"})
                return
            room.controls.put((name, action, value))
            self.answer(200, {"ok": True, "agent": agent.state()})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


async def main(args):
    if args.events is not None:
        raise vp.ConsentError("--events disk logs are disabled pending encrypted artifact registration/destruction; aggregate scoring uses memory")
    configs = getattr(args, "configs", None) or load_config(args.config)
    if any(c.provider != "openai_realtime" and not (c.provider == "openai_responses" and c.role == "arbitrator") for c in configs):
        raise RuntimeError("Only openai_realtime (voice) and openai_responses (arbitrator) are live; xai_speech and gemini_live are stubs")
    if not os.environ.get("OPENAI_API_KEY") and not getattr(args, "gui", False):
        raise RuntimeError("Set OPENAI_API_KEY in this shell before starting the runtime, or start with --gui and enter it there")
    mcp_token = getattr(args, "mcp_token", None) or os.environ.get("VOICEPRINT_MCP_TOKEN")
    token_path = Path(__file__).resolve().parents[1] / "data" / "mcp-token.txt"
    if not mcp_token and token_path.exists():
        mcp_token = token_path.read_text(encoding="utf-8").strip()
    gui = bool(getattr(args, "gui", False))
    runtime = Runtime(gui=gui, mcp_url=args.mcp_url, api_token=vp.api_token(), configs=configs)
    server = control_server(runtime, args.control_port, args.api.rstrip("/"), vp.api_token())
    api_ui = args.api.rstrip("/") + "/ui" + (f"?control={server.server_port}" if server.server_port != 8090 else "")
    print(f"GUI {api_ui}; controls 127.0.0.1:{server.server_port}.", flush=True)
    microphone = resolve_device(args.device, "input")
    print("Microphone:", describe_device(microphone, "input"), flush=True)
    stop = threading.Event()
    try:
        await run_session(args, configs, runtime, server, microphone, mcp_token, stop)
    except Exception as error:
        message = str(error)
        for secret in (os.environ.get("OPENAI_API_KEY"), mcp_token, vp.api_token()):
            if secret:
                message = message.replace(secret, "[redacted]")
        if runtime.stop.is_set() and runtime.phase in ("setup", "consent", "enrollment", "connecting", "ready"):
            runtime.set_phase("ended", f"Stopped by the operator before the conversation started ({message})")
            return
        runtime.set_phase("failed", message)
        if gui and not runtime.stop.is_set():
            print("Runtime failed. The GUI shows the reason; press Stop there or Ctrl+C here.", file=sys.stderr, flush=True)
            while not runtime.stop.is_set():
                await asyncio.sleep(.5)
        raise
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()


async def run_session(args, configs, runtime, server, microphone, mcp_token, stop):
    gui = runtime.gui
    if args.contacts:
        names, contacts, session_id = list(args.names), list(args.contacts), args.session
    elif gui:
        runtime.set_phase("setup", "Waiting for the roster in the GUI")
        setup = await asyncio.to_thread(runtime.wait_setup)
        names, contacts, session_id = setup["names"], setup["contacts"], setup["session_id"] or args.session
    else:
        names, contacts, session_id = list(args.names), None, args.session
    session_id = session_id or "room_" + uuid.uuid4().hex[:10]
    with runtime.lock:
        runtime.session_id = session_id
        runtime.participants = [{"id": f"participant_{i}", "name": name} for i, name in enumerate(names, 1)]
    configs = runtime.active_configs()
    conversation_type = runtime.conversation_type if gui else getattr(args, "conversation_type", None) or runtime.conversation_type
    for config in configs:
        if config.speaks_for and config.speaks_for.casefold() not in {n.casefold() for n in names}:
            raise RuntimeError(f"{config.name} is configured to speak for {config.speaks_for}, who is not in this room")
    validate_room(conversation_type, configs)
    with runtime.lock:
        runtime.conversation_type = conversation_type
    runtime.set_phase("consent", "Each person signs their written release in the GUI; microphone closed")
    try:
        consent = await asyncio.to_thread(vp.prepare_room, args.api, session_id, names, contacts, True, runtime.stop)
    except vp.ConsentError:
        if runtime.stop.is_set():
            # The pending room was created; end it now rather than leaving it to the inactivity sweeper.
            try:
                await asyncio.to_thread(vp.api, args.api, f"/speaker/session/{session_id}/end", {})
            except Exception:
                pass
        raise
    try:
        if conversation_type == "negotiation":
            # Fail before anyone records: without this scope from everyone the arbitrator's first vendor call would latch
            # the guard mid-conversation and stop the whole room. Read through an already-granted scope, no latch.
            state = await asyncio.to_thread(consent.require, "openai_audio")
            if (state.get("scopes") or {}).get("negotiation_text") is not True:
                raise vp.ConsentError("A negotiation room needs every release to include the negotiation text disclosure; "
                                      "end this room and sign again with that box checked")
        record = runtime.recorder(microphone, consent, console=not gui)
        names = await asyncio.to_thread(vp.enroll, args.api, session_id, names, record, None, consent, runtime.reset_enrollment,
                                        on_response=runtime.note_enrolled)
    except BaseException:
        consent.deny()
        try:
            await asyncio.to_thread(vp.api, args.api, f"/speaker/session/{session_id}/end", {})
        except Exception:
            print("Enrollment stopped; room-end request failed. API destruction reconciliation must resolve it.", file=sys.stderr, flush=True)
        raise
    runtime.set_phase("connecting", "Registering agents and connecting to the provider")
    log = EventLog(args.events, session_id, secrets=(os.environ.get("OPENAI_API_KEY"), mcp_token, vp.api_token()))
    room = Room(configs, session_id, names, RestClient(args.api, vp.api_token()), log, consent, conversation_type)
    with runtime.lock:
        runtime.room = room
    loop = asyncio.get_running_loop()
    audio_out = asyncio.Queue(maxsize=8)
    failures = queue.Queue()
    tasks, providers, players = [], [], []
    transcriber, stream, capture_thread = None, None, None
    operator_end = False
    mcp_local = getattr(args, "mcp_local", None) or "http://127.0.0.1:%s/mcp" % os.environ.get("VOICEPRINT_MCP_PORT", "8082")
    try:
        ids = await room.register()
        if room.conversation_type == "negotiation":
            try:
                await room.refresh_objectives()   # advocates connect with their own objective already in the prompt
            except Exception as error:
                # The live poll retries every 2 s and re-prompts an advocate as soon as its objective is readable.
                print(f"Objectives not readable yet ({type(error).__name__}); the runtime keeps polling once live", flush=True)
                log.emit("runtime", {"action": "objectives_poll_failed"})
        for config, participant_id in zip(configs, ids):
            if config.role == "arbitrator":
                # Text only: no player, no floor, no realtime session. Reads go through the local MCP endpoint as proof rows.
                room.arbitrator = Arbitrator(config, participant_id, room, OpenAIResponses(config.model, consent),
                                             McpClient(mcp_local, mcp_token, participant_id))
                room.arbitrator.objectives = room.objectives
                print(f"{config.name}: arbitrator ready (text only, model={config.model}); reads via {mcp_local}", flush=True)
                continue
            output_spec = args.output_device if args.output_device is not None else config.output_device
            output = resolve_device(output_spec, "output")
            if output is None and output_spec:
                print(f"{config.name}: preferred output unavailable; using Windows default", flush=True)
            print(f"{config.name} speaker:", describe_device(output, "output"), flush=True)
            player = LoggedPlayer(output, args.mute_tail,
                                  lambda value, c=config, pid=participant_id: log.emit("playback", value, c.name, pid),
                                  lambda: room.timeline_ms)
            players.append(player)
            principal_id = room.principal_of(config)
            provider = make_provider(config, session_id=session_id, names=names, participant_id=participant_id,
                                     mcp_url=args.mcp_url, mcp_token=mcp_token, consent=consent,
                                     objective=room.objectives.get(principal_id), principal_name=names.get(principal_id))
            providers.append(provider)
            await provider.connect()
            room.agents.append(Agent(config, participant_id, provider, player, room))
            print(f"{config.name}: session configured, voice={config.voice}, eagerness={config.eagerness}, MCP registered", flush=True)
        print(f"Loading faster-whisper {args.model}...", flush=True)
        transcriber = vp.Transcriber(args.api, session_id, names, args.model, log, consent=consent)
        room.transcriber_idle = transcriber.idle

        def enqueue(pcm):
            try:
                audio_out.put_nowait(pcm)
            except asyncio.QueueFull:
                failures.put(RuntimeError("Provider cannot keep up with microphone; stopping instead of dropping audio"))
                stop.set()

        def on_chunk(pcm, attribution):
            room.timeline_ms = attribution["end_ms"]
            if attribution.get("overlap") == "detected":
                room.status = "overlap"
                for agent in room.agents:
                    agent.gate.last_overlap_at = time.monotonic()
            elif attribution.get("speaker_id") or attribution.get("status") == "unknown":
                room.status = "speaking"
            else:
                room.status = "silence"
            loop.call_soon_threadsafe(enqueue, pcm)

        stream = vp.Stream(args.api, session_id, vp.Turns(transcriber.submit, args.verbose), log,
                           on_chunk=on_chunk, verbose=args.verbose, consent=consent)

        def microphone_loop():
            try:
                with vp.Microphone(microphone, consent=consent) as capture:
                    while not stop.is_set():
                        pcm, captured_at = capture.get()
                        # Exactly one capture. All consumers receive zeros during playback/tail.
                        if any(player.busy() for player in players):
                            pcm = b"\x00" * len(pcm)
                        stream.feed(pcm, captured_at)
            except Exception as error:
                failures.put(error)
                stop.set()

        def keyboard_loop():
            try:
                import msvcrt
            except ImportError:
                return
            selected = next((i for i, a in enumerate(room.agents) if a.config.name == args.agent), 0)
            while not stop.is_set():
                if msvcrt.kbhit():
                    ch = msvcrt.getwch().lower()
                    if ch.isdigit() and 1 <= int(ch) <= len(room.agents):
                        selected = int(ch) - 1
                        print("Selected:", room.agents[selected].config.name, flush=True)
                    elif ch in (" ", "h", "c"):
                        room.controls.put((room.agents[selected].config.name, {" ": "speak", "h": "hold", "c": "cancel"}[ch], None))
                    elif ch == "q":
                        stop.set()
                time.sleep(.05)

        async def sender():
            while not stop.is_set():
                pcm = await audio_out.get()
                await room.authorized()
                pcm24k = resample_16k_to_24k(pcm)
                await asyncio.gather(*(a.provider.send_audio(pcm24k) for a in room.agents))

        async def receiver(agent):
            async for event in agent.provider.events():
                await agent.event(event)
            if not stop.is_set():
                raise RuntimeError(f"{agent.config.name}: provider connection closed")

        async def gate_loop():
            while not stop.is_set():
                await room.apply_controls()
                await room.poll_bus()  # persisted rows only; fail closed if the conversation bus is unavailable
                floor = await room.api.request("GET", room.path + "/floor")
                for agent in room.agents:
                    if agent.floor_owned and floor.get("held_by") != agent.participant_id:
                        agent.floor_owned = False
                        agent.emit("floor", {"action": "lost", "reason": "holder_changed"})
                        await agent.cancel("holder_changed")
                    agent.gate.others_speaking = floor.get("held_by") not in (None, agent.participant_id)
                    await agent.tick(time.monotonic())
                await asyncio.sleep(.2)

        async def arbitrator_loop():
            # Own task: a text generation can take tens of seconds and must never stall floor renewals or controls.
            while not stop.is_set():
                if room.arbitrator is not None:
                    await room.arbitrator.tick(time.monotonic())
                await asyncio.sleep(.2)

        async def objectives_loop():
            while not stop.is_set():
                if room.conversation_type == "negotiation":
                    try:
                        await room.refresh_objectives()
                    except Exception:
                        log.emit("runtime", {"action": "objectives_poll_failed"})
                await asyncio.sleep(2)

        async def consent_monitor():
            try:
                while not stop.is_set():
                    await room.authorized()
                    await asyncio.sleep(.25)
            except Exception:
                consent.deny()
                while not audio_out.empty():
                    audio_out.get_nowait()
                transcriber.discard()
                for agent in room.agents:
                    await agent.cancel("consent_stopped")
                raise

        runtime.set_phase("ready", f"Session {session_id}. Press Start in the GUI" + ("" if gui else " or Enter here") + " to open the microphone")
        if not gui:
            print("1/2 select agent, Space = speak, H = hold, C = cancel, Q = quit.", flush=True)
            threading.Thread(target=lambda: (input(), runtime.start.set()), daemon=True).start()
        await asyncio.to_thread(runtime.start.wait)
        if runtime.stop.is_set():
            raise RuntimeError("Stopped before the conversation started")
        runtime.set_phase("live", "Conversation running")
        log.emit("runtime", {"action": "started", "agents": room.state()["agents"]})
        capture_thread = threading.Thread(target=microphone_loop, daemon=True)
        capture_thread.start()
        threading.Thread(target=keyboard_loop, daemon=True).start()
        tasks = [asyncio.create_task(sender()), asyncio.create_task(gate_loop()), asyncio.create_task(consent_monitor()),
                 asyncio.create_task(objectives_loop()), asyncio.create_task(arbitrator_loop())]
        tasks += [asyncio.create_task(receiver(a)) for a in room.agents]
        while not stop.is_set() and not runtime.stop.is_set():
            for task in tasks:
                if task.done():
                    task.result()
                    raise RuntimeError("A runtime task stopped unexpectedly")
            await asyncio.sleep(.2)
        if not failures.empty():
            raise failures.get()
        operator_end = True          # reached only when the operator ended the room (GUI Stop or Q), never on a failure
    finally:
        stop.set()
        runtime.set_phase("ending", "Finishing transcription and ending the room")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for agent in room.agents:
            try:
                await agent.cancel("shutdown")
                await agent.release()
            except Exception:
                log.emit("runtime", {"action": "shutdown_floor_release_failed"}, agent.config.name, agent.participant_id)
        for provider in providers:
            try:
                await asyncio.wait_for(provider.close(), timeout=5)
            except Exception:
                log.emit("runtime", {"action": "provider_close_failed"})
        for player in players:
            try:
                player.close()
            except Exception:
                log.emit("runtime", {"action": "player_close_failed"})
        if capture_thread:
            await asyncio.to_thread(capture_thread.join, 45)
        if stream:
            print(stream.timing_summary(), flush=True)
        if stream and not consent.failed.is_set():
            try:
                stream.turns.flush()
            except vp.ConsentError:
                consent.deny()
        if stream:
            stream.turns.chunks.clear()
            stream.turns.current = stream.turns.overlap = None
        if transcriber:
            if consent.failed.is_set():
                transcriber.discard()
            await asyncio.to_thread(transcriber.finish)
        while not audio_out.empty():
            audio_out.get_nowait()
        # All final human turns are persisted above; the summary (negotiation, operator end only) is written before /end.
        try:
            await end_room(room, log, consent, operator_end and not consent.failed.is_set(), summary_file=summary_file_from_args(args))
        finally:
            log.summarize()
            log.close()
            room.names.clear()
            for agent in room.agents:
                agent.gate.history.clear()
        runtime.set_phase("ended", f"Session ended: {session_id}")


SUMMARY_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["agreements", "open_items", "next_steps", "summary"],
                  "properties": {"agreements": {"type": "array", "items": {"type": "string"}},
                                 "open_items": {"type": "array", "items": {"type": "string"}},
                                 "next_steps": {"type": "array", "items": {"type": "string"}},
                                 "summary": {"type": "string"}}}
SUMMARY_INSTRUCTIONS = ("You write the closing record of a negotiation from its public notes board and named transcript. List the "
                        "agreements reached, the open items and the recommended next steps, then a short summary. Never state, quote, "
                        "approximate or confirm any party's private constraint value; describe outcomes in words. Attribution labels "
                        "in the transcript are similarity-based, not calibrated. Output JSON only.")
SUMMARY_ATTEMPTS = 3
MAX_SUMMARY_CHARS = 20000
SUMMARY_DIR = Path(__file__).resolve().parents[1] / "data" / "summaries"   # data/ is git-ignored (docs/API.md "Runtime and console")
DEFAULT_SUMMARY_FILE = object()      # sentinel: write data/summaries/<session_id>.md; None disables the file


def default_summary_path(session_id):
    """data/summaries/<session_id>.md; characters outside [A-Za-z0-9_.-] become "_" so a typed --session cannot leave the directory."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(session_id or "session")).strip(".") or "session"
    return SUMMARY_DIR / f"{safe}.md"


def summary_file_from_args(args):
    """None (--no-summary-file), the operator's --summary-file path, or the default sentinel."""
    if getattr(args, "no_summary_file", False):
        return None
    return getattr(args, "summary_file", None) or DEFAULT_SUMMARY_FILE


def render_summary(result):
    def items(key):
        return [str(x).strip() for x in (result.get(key) or []) if str(x).strip()] or ["(none)"]
    sections = [("Agreements", items("agreements")), ("Open items", items("open_items")), ("Next steps", items("next_steps"))]
    text = "\n\n".join(f"{title}:\n" + "\n".join(f"- {line}" for line in lines) for title, lines in sections)
    return text + "\n\nSummary: " + str(result.get("summary") or "").strip()


async def fetch_all(api, path, key, limit=200, max_rows=1000, extra=""):
    rows, after = [], 0
    while len(rows) < max_rows:
        page = await api.request("GET", f"{path}?after_id={after}&limit={limit}{extra}")
        batch = page.get(key) or []
        rows.extend(batch)
        cursor = page.get("next_after_id", after)
        if len(batch) < limit or cursor <= after:
            break
        after = cursor
    return rows


async def write_summary(room, responses, log, consent, summary_file=DEFAULT_SUMMARY_FILE, attempts=SUMMARY_ATTEMPTS):
    """One Responses call over the board rows and transcript, redacted, then POST .../summary (retried). True when saved.
    The same text is then written to summary_file (default data/summaries/<session_id>.md; None disables it), only after
    the API record is confirmed. A file error never changes the outcome: the record is the summary, the file is a copy."""
    try:
        await asyncio.to_thread(consent.require, "negotiation_text")
    except vp.ConsentError:
        log.emit("summary", {"action": "skipped", "reason": "scope_missing"})
        return False
    board_rows = await fetch_all(room.api, room.path + "/agent_channel", "rows", extra="&tier=board")
    board_rows = [r for r in board_rows if r.get("tier", "board") == "board"]
    transcript_rows = await fetch_all(room.api, room.path + "/utterances", "utterances")
    counts = {"board_rows": len(board_rows), "transcript_rows": len(transcript_rows)}
    board_text = "\n".join(str(r.get("text") or "") for r in board_rows) or "(empty)"
    transcript_text = "\n".join(f"{room.names.get(r.get('speaker_id'), 'unknown')} [{r.get('label')}]: {r.get('text') or ''}"
                                for r in transcript_rows) or "(empty)"
    try:
        result = await asyncio.to_thread(responses.generate, SUMMARY_INSTRUCTIONS,
                                         f"Notes board:\n{board_text}\n\nTranscript:\n{transcript_text}", "negotiation_summary", SUMMARY_SCHEMA)
    except Exception as error:
        log.emit("summary", {"action": "failed", "attempts": 0, **counts})
        print(f"SUMMARY NOT SAVED: generation failed ({type(error).__name__}); the room still ends", file=sys.stderr, flush=True)
        return False
    text, hits = ob.redact(render_summary(result), ob.constraint_values(room.objectives))
    if hits:
        log.emit("guard", {"action": "redacted", "stage": "summary", "redactions": hits})
    text = text[:MAX_SUMMARY_CHARS]
    body = {"text": text, "model": responses.model, **counts}
    for attempt in range(1, attempts + 1):
        try:
            await room.api.request("POST", room.path + "/summary", body)
        except Exception:
            await asyncio.sleep(.2 * attempt)
            continue
        log.emit("summary", {"action": "saved", "attempts": attempt, **counts})
        print(f"Summary saved to the API ({counts['board_rows']} board rows, {counts['transcript_rows']} transcript rows).", flush=True)
        if summary_file is DEFAULT_SUMMARY_FILE:
            summary_file = default_summary_path(room.session_id)
        if summary_file:
            # Only after the database record is confirmed (docs/API.md "Summary"); the event carries no path.
            try:
                path = Path(summary_file)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
                print(f"Summary also written to {path}.", flush=True)
            except OSError as error:
                print(f"Summary file not written ({type(error).__name__}); the API record is saved", file=sys.stderr, flush=True)
        return True
    log.emit("summary", {"action": "failed", "attempts": attempts, **counts})
    print("SUMMARY NOT SAVED after %d attempts; the room still ends so its purpose data is destroyed on time." % attempts, file=sys.stderr, flush=True)
    return False


async def end_room(room, log, consent, operator_end, responses=None, summary_file=DEFAULT_SUMMARY_FILE):
    """Summary first (negotiation room, arbitrator present, operator-initiated end, consent intact), then POST /end.
    The summary is a derived artifact; /end, which starts purpose-data destruction, always follows even when the
    summary could not be generated or stored. Privacy destruction wins over a derived artifact."""
    if room.conversation_type != "negotiation" or room.arbitrator is None:
        log.emit("summary", {"action": "skipped", "reason": "not_negotiation"})
    elif consent is None or consent.failed.is_set():
        log.emit("summary", {"action": "skipped", "reason": "consent_failed"})
    elif not operator_end:
        log.emit("summary", {"action": "skipped", "reason": "not_operator_end"})
    else:
        responses = responses or OpenAIResponses(room.arbitrator.config.model, consent)
        try:
            await write_summary(room, responses, log, consent, summary_file)
        except Exception as error:
            log.emit("summary", {"action": "failed", "attempts": 0})
            print(f"SUMMARY NOT SAVED ({type(error).__name__}); the room still ends", file=sys.stderr, flush=True)
    try:
        await room.api.request("POST", room.path + "/end", {})
    finally:
        log.emit("runtime", {"action": "ended", "agents": room.state()["agents"]})


def parser():
    result = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    result.add_argument("--config", type=Path, default=Path(__file__).with_name("agents.toml"))
    result.add_argument("--api", default="http://127.0.0.1:8080")
    result.add_argument("--mcp-url", help="public HTTPS URL ending /mcp from the port 8082 tunnel")
    result.add_argument("--mcp-local", help="local MCP HTTP endpoint for the arbitrator's reads (default http://127.0.0.1:$VOICEPRINT_MCP_PORT/mcp)")
    result.add_argument("--conversation-type", choices=list(pp.CONVERSATION_TYPES), default="casual", help="without --gui; the GUI setup sends it")
    result.add_argument("--summary-file", type=Path, help="negotiation only: write the closing summary here instead of data/summaries/<session_id>.md (after the API record is confirmed)")
    result.add_argument("--no-summary-file", action="store_true", help="negotiation only: keep the closing summary as the API record only, no file")
    result.add_argument("--names", nargs="+", default=["Grant", "Kyle"])
    result.add_argument("--contacts", nargs="+", help="one typed email/phone per full name; otherwise prompt before consent")
    result.add_argument("--session")
    result.add_argument("--agent", help="initial keyboard selection (default first configured agent)")
    result.add_argument("--device", default="Razer Seiren")
    result.add_argument("--output-device", help="override each agent's configured output preference")
    result.add_argument("--mute-tail", type=float, default=.4)
    result.add_argument("--model", default="base.en")
    result.add_argument("--events", type=Path, help="disabled by privacy policy; aggregate scoring uses bounded memory")
    result.add_argument("--control-port", type=int, default=8090)
    result.add_argument("--gui", action="store_true", help="roster, provider key, enrollment and start come from the /ui console instead of console prompts")
    result.add_argument("--verbose", action="store_true")
    result.add_argument("--list-devices", action="store_true")
    result.add_argument("--check-config", action="store_true", help="validate TOML without provider credentials or devices")
    return result


if __name__ == "__main__":
    argument_parser = parser()
    args = argument_parser.parse_args()
    if args.list_devices:
        import sounddevice as sd
        print(sd.query_devices())
    elif args.check_config:
        for agent in load_config(args.config):
            print(f"{agent.name}: {agent.provider} / {agent.model} / {agent.voice} / {agent.eagerness} / output {agent.output_device}"
                  + (f" / role {agent.role}" if agent.role != "voice" else ""))
    else:
        if not args.mcp_url:
            argument_parser.error("--mcp-url is required (port 8082 tunnel URL ending /mcp)")
        if args.mute_tail < 0:
            argument_parser.error("--mute-tail must be nonnegative")
        try:
            asyncio.run(main(args))
        except KeyboardInterrupt:
            pass
        except Exception as error:
            print(f"Runtime stopped: {error}", file=sys.stderr, flush=True)
            sys.exit(1)
