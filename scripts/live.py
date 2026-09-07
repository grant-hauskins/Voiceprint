"""Enroll participants, stream a shared microphone to the Java API, and transcribe each finished turn.

Prints one line per finished utterance: "[Grant 0:12.5-0:18.0 high] words" or
"[OVERLAP Grant+Kyle 0:20.0-0:21.5 overlap] words". Text is stored through the API so an MCP client can read it
with get_transcript. See scripts/voiceprint_client.py for the grouping logic.
"""
import argparse
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import voiceprint_client as vp  # noqa: E402


def run(args):
    if args.events is not None:
        raise vp.ConsentError("--events disk logs are disabled pending encrypted artifact registration and verified destruction")
    if not 2 <= len(args.names) <= 4:
        raise ValueError("Choose 2–4 participants")
    replay = args.stream_wav is not None
    if replay and len(args.enroll_wavs or []) != len(args.names):
        raise ValueError("--enroll-wavs needs one WAV per name when --stream-wav is used")
    if not args.no_transcribe:
        print(f"Loading faster-whisper {args.model} (first run downloads the model)...", flush=True)
    session = args.session or "live_" + uuid.uuid4().hex[:12]
    if replay:
        raise vp.ConsentError("Human-audio replay is blocked in this entry point until a provenance/permission workflow exists; use synthetic offline tests")
    consent = vp.prepare_room(args.api, session, args.names, args.contacts)
    names = vp.enroll(args.api, session, args.names, vp.record_from_mic(args.device, consent),
                      args.enroll_wavs if replay else None, consent=consent)
    print("Session", session, "ready:", ", ".join(f"{pid}={name}" for pid, name in names.items()), flush=True)
    from agent_runtime import EventLog
    log = EventLog(None, session, secrets=(vp.api_token(),))
    transcriber = None if args.no_transcribe else vp.Transcriber(args.api, session, names, args.model, log, consent=consent)
    sink = transcriber.submit if transcriber else (lambda utterance, pcm: print(vp.format_line(dict(utterance, text="(not transcribed)"), names), flush=True))
    stream = vp.Stream(args.api, session, vp.Turns(sink, args.verbose), log, verbose=args.verbose, consent=consent)
    print("Lines starting with #id are stored and readable by agents via get_transcript under that id.", flush=True)
    if not replay:
        input("Press Enter to start the conversation. Ctrl+C ends the session. ")
    try:
        if replay:
            for pcm, captured_at in vp.file_chunks(args.stream_wav, args.realtime, consent):
                if stream.sequence >= args.seconds * 4:
                    break
                stream.feed(pcm, captured_at)
        else:
            with vp.Microphone(args.device, consent=consent) as mic:
                while stream.sequence < args.seconds * 4:
                    pcm, captured_at = mic.get()
                    stream.feed(pcm, captured_at)
    except KeyboardInterrupt:
        print("Conversation stopped.", flush=True)
    finally:
        if consent.failed.is_set():
            stream.turns.chunks.clear()
            stream.turns.current = stream.turns.overlap = None
            if transcriber:
                transcriber.discard()
        else:
            stream.turns.flush()
        if transcriber:
            print("Finishing transcription...", flush=True)
            transcriber.finish()
        vp.api(args.api, f"/speaker/session/{session}/end", {})
        if log:
            log.summarize()
            log.close()
        print("Session ended:", session, flush=True)
        print("Session purpose complete; protected transcript destruction requested.", flush=True)


def show(args):
    query = f"after_id={args.after}&limit=200" + (f"&min_label={args.min_label}" if args.min_label else "")
    print(vp.api(args.api, f"/speaker/session/{args.session}/utterances?{query}")["text"], end="")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default="http://127.0.0.1:8080")
    commands = parser.add_subparsers(dest="command", required=True)
    live = commands.add_parser("run", help="enroll, stream and transcribe")
    live.add_argument("--names", nargs="+", required=True)
    live.add_argument("--contacts", nargs="+", help="one typed email/phone per full name; otherwise prompt")
    live.add_argument("--session")
    live.add_argument("--seconds", type=int, default=60)
    live.add_argument("--device", type=int, help="sounddevice input index")
    live.add_argument("--events", type=Path, help="JSONL log of attributions and utterances")
    live.add_argument("--model", default="base.en", help="faster-whisper model (tiny.en, base.en, small.en)")
    live.add_argument("--no-transcribe", action="store_true")
    live.add_argument("--verbose", action="store_true", help="print every 250 ms attribution")
    live.add_argument("--enroll-wavs", nargs="+", type=Path, help="replay mode: one enrollment WAV per name")
    live.add_argument("--stream-wav", type=Path, help="replay mode: conversation WAV instead of the microphone")
    live.add_argument("--realtime", action="store_true", help="replay at capture cadence")
    transcript = commands.add_parser("transcript", help="print stored utterances")
    transcript.add_argument("session"); transcript.add_argument("--after", type=int, default=0)
    transcript.add_argument("--min-label", choices=["high", "medium", "low"])
    correction = commands.add_parser("correct")
    correction.add_argument("session"); correction.add_argument("segment"); correction.add_argument("speaker")
    args = parser.parse_args()
    if args.command == "run":
        if not 1 <= args.seconds <= 3600:
            parser.error("seconds must be 1–3600")
        run(args)
    elif args.command == "transcript":
        show(args)
    else:
        print(json.dumps(vp.api(args.api, f"/speaker/session/{args.session}/correct", {"segment_id": args.segment, "actual_speaker": args.speaker}), indent=2))
