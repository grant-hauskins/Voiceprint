"""Application-level turn-taking for a voice agent in a group conversation.

The gate decides *when* the agent may speak. It never decides *what* to say. Inputs are the things Voiceprint
already produces: finished utterances with speaker labels, the current-speaker status (open turn, overlap,
silence), and when the agent last spoke. Rules mirror docs/TURN_TAKING.md; keep them in sync.

decide(...) returns one of:
  "speak"    - a clear opportunity; send response.create
  "clarify"  - the agent was addressed but the last attribution is unreliable; ask who spoke rather than answer
  "wait"     - not the agent's turn
"""
import re
import time
from dataclasses import dataclass, field

SILENCE_AFTER_TURN_S = {"quiet": 2.0, "balanced": 1.2, "eager": 0.7}
AGENT_COOLDOWN_S = {"quiet": 12.0, "balanced": 6.0, "eager": 3.0}
OVERLAP_HOLD_S = 3.0             # stay quiet this long after any overlap row
ADDRESS_WINDOW_S = 8.0           # a direct address stays "live" this long


@dataclass
class GateState:
    agent_names: tuple
    eagerness: str = "balanced"
    agent_speaker_id: str = None             # Voiceprint id of the agent's own enrolled voice, if any
    agent_last_spoke_at: float = -1e9
    last_overlap_at: float = -1e9
    manual: str = None                       # "speak": one reply now (consumed once). "hold": sticky mute until released.
    history: list = field(default_factory=list)   # recent utterances (dicts), newest last

    def note_utterance(self, utterance, now=None):
        now = time.monotonic() if now is None else now
        utterance = dict(utterance, seen_at=now)
        self.history = (self.history + [utterance])[-8:]
        if utterance.get("label") == "overlap" or utterance.get("candidates"):
            self.last_overlap_at = now

    def note_agent_spoke(self, now=None):
        self.agent_last_spoke_at = time.monotonic() if now is None else now


def addressed(text, agent_names):
    """True when the utterance names the agent (start of sentence, or a direct 'Name,' / 'Name?' form)."""
    if not text:
        return False
    lowered = text.lower()
    for name in agent_names:
        n = name.lower()
        if re.search(rf"(^|[\s,.;!?\"'])(hey |ok |okay )?{re.escape(n)}([\s,.;!?\"']|$)", lowered):
            return True
    return False


def question_like(text):
    if not text:
        return False
    t = text.strip().lower()
    return t.endswith("?") or bool(re.match(r"^(what|why|how|when|where|who|which|can|could|should|would|do|does|did|is|are|will)\b", t))


def decide(state, now, current_status, last_utterance_end_at):
    """
    state: GateState. now: monotonic seconds. current_status: one of
      "speaking" (a human turn is open), "overlap", "silence", "unknown".
    last_utterance_end_at: monotonic time the most recent human utterance finished (None if none yet).
    """
    if state.manual == "hold":
        return "wait"                        # sticky: stays until the user releases it
    if state.manual == "speak":
        state.manual = None
        return "speak"
    humans = [u for u in state.history if u.get("speaker_id") != state.agent_speaker_id or u.get("speaker_id") is None]
    if not humans:
        return "wait"
    last = humans[-1]
    # Inhibitors first: never talk over people, never answer into an overlap, never monologue.
    if current_status in ("speaking", "overlap"):
        return "wait"
    if now - state.last_overlap_at < OVERLAP_HOLD_S:
        return "wait"
    # A direct address counts once: only if it arrived after the agent's last reply.
    direct = (addressed(last.get("text"), state.agent_names) and now - last["seen_at"] < ADDRESS_WINDOW_S
              and last["seen_at"] > state.agent_last_spoke_at)
    if now - state.agent_last_spoke_at < AGENT_COOLDOWN_S[state.eagerness] and not direct:
        return "wait"
    silence = (now - last_utterance_end_at) if last_utterance_end_at is not None else 0
    if direct:
        if last.get("label") in ("low", "overlap", "unknown") and last.get("speaker_id") is not None:
            return "clarify"      # we were asked something but are not sure by whom
        return "speak" if silence >= 0.3 else "wait"
    # Soft opportunity: a clean, question-like turn followed by silence.
    if state.eagerness == "quiet":
        return "wait"
    if last.get("label") == "high" and question_like(last.get("text")) and silence >= SILENCE_AFTER_TURN_S[state.eagerness]:
        return "speak"
    if state.eagerness == "eager" and silence >= SILENCE_AFTER_TURN_S["eager"] * 2 and last.get("label") in ("high", "medium"):
        return "speak"
    return "wait"
