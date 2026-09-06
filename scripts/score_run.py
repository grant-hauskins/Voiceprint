"""Offline scoring of legacy and shared-room JSONL logs; see docs/EVENTS.md."""
import argparse
import json
import math
import re
from collections import Counter, OrderedDict
from pathlib import Path


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def distribution(values):
    values = sorted(value for value in values if number(value))
    if not values:
        return {"n": 0, "min": None, "p50": None, "p95": None, "max": None}

    def percentile(q):
        index = (len(values) - 1) * q
        low, high = math.floor(index), math.ceil(index)
        return round(values[low] + (values[high] - values[low]) * (index - low), 3)

    return {"n": len(values), "min": min(values), "p50": percentile(.5), "p95": percentile(.95), "max": max(values)}


def fraction(correct, total):
    return {"numerator": correct, "denominator": total, "ratio": correct / total if total else None}


def read_runs(path):
    """Explicit run_id groups parallel providers. Legacy session.created splits attempts."""
    runs = OrderedDict()
    current = None
    legacy_count = 0
    with Path(path).open(encoding="utf-8-sig") as source:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError()
            except (ValueError, TypeError):
                raise ValueError(f"Invalid JSON object on line {line_number}; contents omitted") from None
            event = row.get("openai") or {}
            if row.get("run_id"):
                key = str(row["run_id"])
            else:
                if event.get("type") == "session.created" or current is None:
                    legacy_count += 1
                    current = f"legacy-{legacy_count}"
                key = current
            runs.setdefault(key, []).append(dict(row, _line=line_number))
    return runs


def read_turns(path):
    turns = [line.strip() for line in Path(path).read_text(encoding="utf-8-sig").splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    if any(turn == "AGENT:" for turn in turns):
        raise ValueError("AGENT: turns require a name")
    return turns


def score(run_id, rows, truth=None, names=None):
    roster = dict(names or {})
    default_agent = "unattributed"
    sessions = set()
    configured = 0
    started = set()
    completed = set()
    provider_events = Counter()
    for row in rows:
        if row.get("session_id"):
            sessions.add(row["session_id"])
        if row.get("attribution", {}).get("session_id"):
            sessions.add(row["attribution"]["session_id"])
        event = row.get("openai") or {}
        typ = event.get("type")
        if typ:
            provider_events[typ] += 1
        if typ == "session.updated":
            configured += 1
            # Parse only our historical prompt's names. Never emit a session config.
            prompt = event.get("session", {}).get("instructions", "")
            roster.update({pid: name for name, pid in re.findall(r"([\w-]+) \(id ([\w-]+)\)", prompt)})
            match = re.search(r"You are ([\w-]+)", prompt)
            if match:
                default_agent = match.group(1)
        for agent in row.get("runtime", {}).get("agents", []):
            if agent.get("participant_id") and agent.get("name"):
                roster[agent["participant_id"]] = agent["name"]
        utterance = row.get("utterance", {})
        if utterance.get("speaker_name") and utterance.get("speaker_id"):
            roster[utterance["speaker_id"]] = utterance["speaker_name"]

    agents = {}
    active_response = {}
    responses = OrderedDict()
    tools = {}
    pending_tools = {}
    pending_calls = {}
    tool_latencies = []
    utterances = []
    utterance_ids = set()
    capture_latencies = []
    inference_latencies = []
    overlap_spans = []
    playback_open = {}
    playback_intervals = []
    playback_closed = set()
    playback_starts = {}
    playback_unknown = 0
    playback_duplicates = 0
    server_bytes = []
    server_calls = set()

    def agent_state(name):
        return agents.setdefault(name, {"replies": 0, "replies_with_fresh_result": 0, "get_transcript_calls": 0,
                                       "failed_tool_calls": 0, "tokens": Counter(), "usage_responses": 0})

    def response_key(row, event):
        agent = row.get("agent") or default_agent
        rid = event.get("response_id") or event.get("response", {}).get("id") or active_response.get(agent)
        return agent, rid

    def note_speech(key, row, basis):
        agent, rid = key
        if not rid:
            return
        response = responses.setdefault(key, {"response_id": rid, "agent": agent, "transcripts": [], "start_line": None})
        if response["start_line"] is not None:
            return
        response["start_line"] = row["_line"]
        response["source_line"] = row.get("source_line", row["_line"])
        response["start_basis"] = basis
        response["fresh_tool_items"] = list(pending_tools.pop(agent, []))
        # A completed transcript cannot prove when speech began.
        response["tool_before_speech"] = bool(response["fresh_tool_items"]) if basis != "transcript_done_only" else None
        state = agent_state(agent)
        state["replies"] += 1
        state["replies_with_fresh_result"] += response["tool_before_speech"] is True

    def note_tool(agent, item, row, rid):
        if item.get("type") != "mcp_call":
            return
        iid = item.get("id")
        key = (agent, iid)
        if not iid or key in tools:
            return
        output = item.get("output")
        # output_item.done can arrive with no result; only completed outputs are usable.
        safe_result = type(item.get("succeeded")) is bool
        if output is None and not item.get("error") and not safe_result:
            return
        failed = bool(item.get("error")) or item.get("status") == "failed" or item.get("succeeded") is False
        output_bytes = item.get("output_bytes")
        if not (isinstance(output_bytes, int) and not isinstance(output_bytes, bool) and output_bytes >= 0):
            output_bytes = len((output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)).encode("utf-8")) if output is not None else None
        tools[key] = {"item_id": iid, "response_id": rid, "name": item.get("name"), "failed": failed,
                      "line": row["_line"], "source_line": row.get("source_line", row["_line"]),
                      "bytes": output_bytes}
        state = agent_state(agent)
        state["failed_tool_calls"] += failed
        if item.get("name") == "get_transcript":
            state["get_transcript_calls"] += 1
            if not failed:
                pending_tools.setdefault(agent, []).append(iid)
        begin = pending_calls.pop(key, None)
        if begin is not None and number(row.get("monotonic_ms")) and row["monotonic_ms"] >= begin:
            tool_latencies.append(row["monotonic_ms"] - begin)

    for row in rows:
        agent = row.get("agent") or default_agent
        event = row.get("openai") or {}
        typ = event.get("type")
        key = response_key(row, event)
        if typ == "response.created":
            active_response[agent] = key[1]
            started.add(key)
        elif typ == "response.output_audio.delta":
            note_speech(key, row, "provider_audio_received")
        elif typ == "response.output_audio_transcript.done":
            note_speech(key, row, "transcript_done_only")
            if key in responses:
                text = event.get("transcript") or ""
                part = (event.get("item_id"), event.get("content_index"), text)
                if part not in responses[key]["transcripts"]:
                    responses[key]["transcripts"].append(part)
        elif typ == "response.mcp_call.in_progress":
            if number(row.get("monotonic_ms")):
                pending_calls[(agent, event.get("item_id"))] = row["monotonic_ms"]
        elif typ == "response.output_item.done":
            note_tool(agent, event.get("item", {}), row, key[1])
        elif typ == "response.done":
            if key not in completed:
                completed.add(key)
                usage = event.get("response", {}).get("usage")
                if isinstance(usage, dict):
                    state = agent_state(agent)
                    state["usage_responses"] += 1
                    for field in ("input_tokens", "output_tokens", "total_tokens"):
                        if number(usage.get(field)):
                            state["tokens"][field] += usage[field]
            # Tool results also occur in response.done; IDs prevent double counting.
            for item in event.get("response", {}).get("output", []):
                note_tool(agent, item, row, key[1])
        attribution = row.get("attribution")
        if isinstance(attribution, dict):
            capture_latencies.append(row.get("capture_end_to_response_ms"))
            inference_latencies.append(attribution.get("inference_ms"))
            if attribution.get("overlap") == "detected" and all(number(attribution.get(k)) for k in ("start_ms", "end_ms")):
                overlap_spans.append((attribution["start_ms"], attribution["end_ms"]))
        utterance = row.get("utterance")
        if isinstance(utterance, dict):
            uid = utterance.get("utterance_id")
            if uid is None or uid not in utterance_ids:
                utterance_ids.add(uid)
                utterances.append(dict(utterance, _line=row["_line"]))
                if utterance.get("label") == "overlap" and all(number(utterance.get(k)) for k in ("start_ms", "end_ms")):
                    overlap_spans.append((utterance["start_ms"], utterance["end_ms"]))
        playback = row.get("playback")
        if isinstance(playback, dict):
            pk = (agent, playback.get("response_id"))
            timing = row.get("monotonic_ms")
            if playback.get("timing_basis") != "played" or not number(timing):
                playback_unknown += 1
            elif playback.get("action") == "started":
                if pk in playback_open:
                    playback_duplicates += 1
                else:
                    playback_open[pk] = timing
                playback_starts.setdefault(pk, playback.get("audio_timeline_ms"))
            elif playback.get("action") in ("drained", "cancelled"):
                begin = playback_open.pop(pk, None)
                if begin is not None and timing >= begin:
                    playback_intervals.append((agent, begin, timing))
                    playback_closed.add(pk)
                else:
                    playback_unknown += 1
        call = row.get("mcp_call")
        if isinstance(call, dict) and call.get("call_id") not in server_calls:
            server_calls.add(call.get("call_id"))
            server_bytes.append(call.get("bytes"))

    # A strict order is available for a response only if its first audio event was logged.
    for agent, state in agents.items():
        measured = [r for r in responses.values() if r["agent"] == agent and r["tool_before_speech"] is not None]
        state["tool_before_reply"] = fraction(state.pop("replies_with_fresh_result"), len(measured))
        state["reply_timing_missing"] = state["replies"] - len(measured)
        times = [offset for (name, _), offset in playback_starts.items() if name == agent and number(offset)]
        state["replies_within_3s_of_detected_overlap"] = fraction(
            sum(any(begin <= offset < end + 3000 for begin, end in overlap_spans) for offset in times), len(times))
        state["tokens"] = dict(state["tokens"]) if state["usage_responses"] else None

    # Use stored agent rows when present; fall back to provider transcripts for legacy runs.
    conversation = []
    for utterance in utterances:
        producer = roster.get(utterance.get("speaker_id"), utterance.get("speaker_id") or "unknown")
        observed = "AGENT:" + producer if utterance.get("source") == "agent" else ("OVERLAP" if utterance.get("label") == "overlap" else producer)
        conversation.append({"line": utterance["_line"], "observed": observed, "text": utterance.get("text", ""), "label": utterance.get("label")})
    if not any(u.get("source") == "agent" for u in utterances):
        conversation.extend({"line": reply["start_line"], "observed": "AGENT:" + reply["agent"],
                             "text": " ".join(part[2] for part in reply["transcripts"]), "label": "agent"} for reply in responses.values())
    conversation.sort(key=lambda turn: turn["line"])
    ground_truth = None
    if truth is not None:
        if len(truth) != len(conversation):
            raise ValueError(f"Turn alignment mismatch: {len(truth)} expected labels but {len(conversation)} logged turns; no truncation or automatic matching")
        counts = {}
        tp = fp = fn = 0
        handoffs = handoffs_named = 0
        for index, (expected, observed) in enumerate(zip(truth, conversation)):
            values = counts.setdefault(expected, [0, 0])
            values[0] += expected.casefold() == observed["observed"].casefold()
            values[1] += 1
            tp += expected == "OVERLAP" and observed["observed"] == "OVERLAP"
            fp += expected != "OVERLAP" and observed["observed"] == "OVERLAP"
            fn += expected == "OVERLAP" and observed["observed"] != "OVERLAP"
            if index and expected.startswith("AGENT:") and truth[index - 1].startswith("AGENT:") and expected != truth[index - 1]:
                handoffs += 1
                target = expected.removeprefix("AGENT:")
                handoffs_named += bool(re.search(r"(?<!\w)" + re.escape(target) + r"(?!\w)", conversation[index - 1]["text"], re.I))
        ground_truth = {"per_speaker": {speaker: fraction(*values) for speaker, values in counts.items()},
                        "overlap_precision": fraction(tp, tp + fp), "overlap_recall": fraction(tp, tp + fn),
                        "overlap_counts": {"true_positive": tp, "false_positive": fp, "false_negative": fn},
                        "named_agent_handoffs": fraction(handoffs_named, handoffs)}

    violations = []
    for index, left in enumerate(playback_intervals):
        for right in playback_intervals[index + 1:]:
            duration = min(left[2], right[2]) - max(left[1], right[1])
            if left[0] != right[0] and duration > 0:
                violations.append({"agents": [left[0], right[0]], "overlap_ms": round(duration, 3)})
    missing_playback = len(set(responses) - playback_closed)
    complete_playback = bool(playback_intervals) and not playback_open and not playback_unknown and not playback_duplicates and not missing_playback
    floor = {"violations": len(violations) if complete_playback else None, "observed_pairs": violations,
             "complete_intervals": len(playback_intervals), "unclosed_intervals": len(playback_open),
             "replies_without_complete_playback": missing_playback,
             "unusable_events": playback_unknown, "duplicate_starts": playback_duplicates,
             "timing_basis": "played; drain may include conservative mute tail"}
    warnings = []
    if truth is None:
        warnings.append("No independently supplied turn labels: attribution accuracy, overlap precision/recall and named handoffs are NA.")
    if not complete_playback:
        warnings.append("Complete played-audio intervals unavailable: floor violations are NA, not zero.")
    if any(state["reply_timing_missing"] for state in agents.values()):
        warnings.append("Some replies lack audio-start evidence and are excluded from the before-reply ratio.")
    if not server_calls:
        warnings.append("No API MCP call records in this log: provider tool events are not independent server-side proof.")
    if any(not number(offset) for offset in playback_starts.values()) or not playback_starts:
        warnings.append("Some or all replies lack audio-timeline playback timestamps: overlap timing has incomplete coverage.")
    return {"schema_version": 1, "run_id": run_id, "session_ids": sorted(sessions), "rows": len(rows),
            "provider_sessions": provider_events["session.created"], "configured_sessions": configured,
            "responses_started": len(started), "responses_done": len(completed), "pending_responses": len(started - completed),
            "provider_transcript_parts": provider_events["response.output_audio_transcript.done"],
            "utterances": len(utterances), "human_label_distribution": dict(Counter(u.get("label", "unknown") for u in utterances if u.get("source") != "agent")),
            "aligned_turn_count": len(conversation), "agents": agents, "ground_truth": ground_truth, "floor": floor,
            "provider_tool_output_bytes": distribution(t["bytes"] for t in tools.values()),
            "server_mcp_response_bytes": distribution(server_bytes), "provider_tool_latency_ms": distribution(tool_latencies),
            "capture_end_to_response_ms": distribution(capture_latencies), "inference_ms": distribution(inference_latencies),
            "reply_evidence": [{key: value for key, value in reply.items() if key != "transcripts"} for reply in responses.values()],
            "tool_evidence": list(tools.values()), "warnings": warnings}


def render_markdown(result):
    def ratio(value):
        return "NA" if value["ratio"] is None else f"{value['numerator']}/{value['denominator']} ({value['ratio']:.1%})"

    lines = [f"## {result['run_id']}", "", "Sessions: " + (", ".join(result["session_ids"]) or "unrecorded"), "",
             f"Stored utterances: {result['utterances']}; aligned turns: {result['aligned_turn_count']}; responses finished: {result['responses_done']}; pending: {result['pending_responses']}.", "",
             "| Agent | Spoken responses | Transcript calls | Fresh successful result before speech | Within 3 s of detected overlap | Reported tokens |",
             "|---|---:|---:|---|---|---:|"]
    for name, agent in result["agents"].items():
        tokens = (agent["tokens"] or {}).get("total_tokens", "NA")
        lines.append(f"| {name} | {agent['replies']} | {agent['get_transcript_calls']} | {ratio(agent['tool_before_reply'])} | {ratio(agent['replies_within_3s_of_detected_overlap'])} | {tokens} |")
    lines += ["", "Human labels (similarity-based, uncalibrated): " + json.dumps(result["human_label_distribution"], sort_keys=True), "",
              "Floor violations: " + str(result["floor"]["violations"] if result["floor"]["violations"] is not None else "NA"), "",
              "| Measurement | Samples | p50 | p95 |", "|---|---:|---:|---:|"]
    for key in ("provider_tool_output_bytes", "server_mcp_response_bytes", "provider_tool_latency_ms", "capture_end_to_response_ms", "inference_ms"):
        values = result[key]
        lines.append(f"| {key} | {values['n']} | {values['p50'] if values['p50'] is not None else 'NA'} | {values['p95'] if values['p95'] is not None else 'NA'} |")
    if result["ground_truth"]:
        truth = result["ground_truth"]
        lines += ["", "| Ground truth metric | Result |", "|---|---|"]
        lines.extend(f"| {name} attribution | {ratio(value)} |" for name, value in truth["per_speaker"].items())
        lines.extend(f"| {key} | {ratio(truth[key])} |" for key in ("overlap_precision", "overlap_recall", "named_agent_handoffs"))
    lines += ["", *["- " + warning for warning in result["warnings"]]]
    return "\n".join(lines)


def aggregate(result):
    """Allowlisted operational counts; excludes room/subject/event IDs and biometric metrics."""
    agents = {}
    for index, agent in enumerate(result["agents"].values(), 1):
        agents[f"Agent {index}"] = {key: agent[key] for key in
            ("replies", "get_transcript_calls", "failed_tool_calls", "tool_before_reply", "reply_timing_missing", "tokens")}
    return {"schema_version": 2, "scope": "operational_aggregate", "agents": agents,
            "floor_violations": result["floor"]["violations"],
            "server_mcp_call_samples": result["server_mcp_response_bytes"]["n"]}


def render_aggregate(result):
    lines = []
    for name, agent in result["agents"].items():
        value = agent["tool_before_reply"]
        fresh = f"{value['numerator']}/{value['denominator']}" if value['denominator'] else "NA"
        lines.append(f"{name}: {agent['replies']} replies, {agent['get_transcript_calls']} transcript calls; fresh result before speech {fresh}.")
    violations = result["floor_violations"]
    lines.append("Played-audio floor violations: " + (str(violations) if violations is not None else "NA (incomplete playback evidence)") + ".")
    lines.append(f"Independent server MCP samples: {result['server_mcp_call_samples']}.")
    return "\n".join(lines)


def summarize_ephemeral(path):
    """Called before the runtime deletes its temporary log; never persists identifying evidence."""
    return "\n\n".join(render_aggregate(aggregate(score(key, rows))) for key, rows in read_runs(path).items())


def summarize_events(rows):
    """Score the runtime's bounded RAM metadata; no disk or identifying output."""
    indexed = [dict(row, _line=index) for index, row in enumerate(rows, 1)]
    return render_aggregate(aggregate(score("ephemeral", indexed)))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("events", type=Path)
    parser.add_argument("--run", help="Run ID or session ID; omit to score every run")
    parser.add_argument("--turns", type=Path, help="Exactly one truth label per logged turn, including agents")
    parser.add_argument("--name", action="append", default=[], metavar="ID=Name", help="Participant name mapping; repeat as needed")
    parser.add_argument("--output", type=Path, default=Path("data/runs.jsonl"))
    parser.add_argument("--no-append", action="store_true", help="Print only; do not append JSON")
    parser.add_argument("--details", action="store_true", help="Print detailed evidence for an authorized offline audit; persisted JSON remains aggregate-only")
    args = parser.parse_args(argv)
    try:
        names = dict(value.split("=", 1) for value in args.name)
        runs = read_runs(args.events)
        candidates = [(key, rows) for key, rows in runs.items() if not args.run or args.run == key or any(
            args.run == row.get("session_id") or args.run == row.get("attribution", {}).get("session_id") for row in rows)]
        if not candidates:
            raise ValueError("No matching run")
        if args.turns and len(candidates) != 1:
            raise ValueError("--turns requires exactly one selected run; use --run")
        truth = read_turns(args.turns) if args.turns else None
        results = [score(key, rows, truth, names) for key, rows in candidates]
    except (ValueError, OSError) as error:
        parser.error(str(error))
    if not args.no_append:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("a", encoding="utf-8") as output:
            for result in results:
                output.write(json.dumps(aggregate(result), ensure_ascii=False, allow_nan=False) + "\n")
    print("\n\n".join(render_markdown(result) if args.details else render_aggregate(aggregate(result)) for result in results))
    return results


if __name__ == "__main__":
    main()
