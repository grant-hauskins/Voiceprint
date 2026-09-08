"""Write evaluation/v3_events_sample.jsonl: synthetic v3 rows exercising every new kind; no real names or text."""
import json
import sys
from pathlib import Path

RUN, ROOM = "synthetic-v3", "synthetic_room"
rows = []
clock = [1_000]


def row(kind, value, agent, participant_id=None):
    clock[0] += 250
    rows.append({"run_id": RUN, "session_id": ROOM, "agent": agent, "participant_id": participant_id,
                 "timestamp_ms": 1_700_000_000_000 + clock[0], "monotonic_ms": clock[0], kind: value})


def arbitrator(action, trigger=None, tag=None, confirmed=None, ingested_rows=None, generations=None, tier=None, redactions=None):
    row("arbitrator", {"action": action, "trigger": trigger, "tag": tag, "confirmed": confirmed, "ingested_rows": ingested_rows,
                       "generations": generations, "tier": tier, "redactions": redactions}, "Mediator", "participant_3")


# Provider-side rows for the two advocates, in the v2 shape, so the run also has ordinary agent metrics.
for agent, pid, rid in (("Ava", "participant_1", "v3_reply_1"), ("Ben", "participant_2", "v3_reply_2")):
    row("openai", {"type": "response.created", "response": {"id": rid}}, agent, pid)
    row("openai", {"type": "response.output_item.done", "response_id": rid, "item": {"id": rid + "_tool", "type": "mcp_call",
                   "name": "get_transcript", "succeeded": True, "output_bytes": 12}}, agent, pid)
    row("openai", {"type": "response.output_audio.delta", "response_id": rid}, agent, pid)
    row("openai", {"type": "response.done", "response": {"id": rid, "usage": {"total_tokens": 100}}}, agent, pid)

# 30 ingested rows, 4 generations: the §5.1 instrumentation should stay well below 1:1.
ingested = generations = 0
for line in range(1, 31):
    ingested += 1
    arbitrator("ingested", ingested_rows=ingested)
    if line == 8:
        generations += 1
        arbitrator("generated", trigger="contribution", generations=generations)
        arbitrator("posted", trigger="contribution", tier="board", redactions=1)
        row("guard", {"action": "redacted", "stage": "board", "redactions": 1}, "Mediator", "participant_3")
    elif line == 12:
        arbitrator("skipped", trigger="contribution")
    elif line == 15:
        generations += 1
        arbitrator("generated", trigger="manual", generations=generations)
        arbitrator("posted", trigger="manual", tier="raw", redactions=0)
    elif line == 20:
        row("guard", {"action": "cut", "stage": "spoken_delta", "redactions": 1}, "Ava", "participant_1")
        row("guard", {"action": "redacted", "stage": "stored_utterance", "redactions": 2}, "Ava", "participant_1")
        row("guard", {"action": "redacted", "stage": "raw", "redactions": 1}, "Ben", "participant_2")
    elif line == 24:
        generations += 1
        arbitrator("generated", trigger="REFOCUS_NEEDED", tag="REFOCUS_NEEDED", generations=generations)
        arbitrator("override_claimed", trigger="REFOCUS_NEEDED", tag="REFOCUS_NEEDED")
        arbitrator("override_rejected", trigger="REFOCUS_NEEDED", tag="REFOCUS_NEEDED", confirmed=False)
    elif line == 29:
        generations += 1
        arbitrator("generated", trigger="OBJECTIVE_ACHIEVED", tag="OBJECTIVE_ACHIEVED", generations=generations)
        arbitrator("override_claimed", trigger="OBJECTIVE_ACHIEVED", tag="OBJECTIVE_ACHIEVED")
        arbitrator("override_confirmed", trigger="OBJECTIVE_ACHIEVED", tag="OBJECTIVE_ACHIEVED", confirmed=True)
        arbitrator("posted", trigger="OBJECTIVE_ACHIEVED", tag="OBJECTIVE_ACHIEVED", tier="board", redactions=0)

# Summary: one failed attempt, then saved; the summary guard redacted once.
row("summary", {"action": "failed", "attempts": 1, "board_rows": 2, "transcript_rows": 30}, "Mediator", "participant_3")
row("guard", {"action": "redacted", "stage": "summary", "redactions": 1}, "Mediator", "participant_3")
row("summary", {"action": "saved", "attempts": 2, "board_rows": 2, "transcript_rows": 30}, "Mediator", "participant_3")

target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("v3_events_sample.jsonl")
target.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
print(len(rows), "rows ->", target)
