"""Build synthetic event-order tests from aggregate counts, without real recordings or IDs."""
import json
from pathlib import Path


def build():
    rows = []
    for run, (replies, calls, human_turns, done_count, tokens) in enumerate(
            [(2, 2, 3, 4, 4261), (6, 4, 7, 8, 17298), (5, 5, 5, 9, 14149)], 1):
        rows.append({'openai': {'type': 'session.created'}})
        rows.append({'openai': {'type': 'session.updated', 'session': {'instructions': 'You are Ava.'}}})
        for turn in range(human_turns):
            rows.append({'utterance': {'utterance_id': turn + 1, 'speaker_id': 'synthetic_human',
                                      'source': 'synthetic_test', 'label': 'unknown', 'text': 'synthetic'}})
        for reply in range(1, replies + 1):
            rid = f'fixture_{run}_reply_{reply}'
            rows.append({'openai': {'type': 'response.created', 'response': {'id': rid}}})
            audio = {'openai': {'type': 'response.output_audio.delta', 'response_id': rid}}
            call = {'openai': {'type': 'response.output_item.done', 'response_id': rid,
                              'item': {'id': f'fixture_{run}_tool_{reply}', 'type': 'mcp_call',
                                       'name': 'get_transcript', 'succeeded': True, 'output_bytes': 10}}}
            if run == 3 and reply == 1:
                rows.extend([audio, call])
            else:
                if reply <= calls:
                    rows.append(call)
                rows.append(audio)
            rows.append({'openai': {'type': 'response.done', 'response': {'id': rid,
                        'usage': {'total_tokens': tokens if reply == 1 else 0}}}})
        for extra in range(done_count - replies):
            rid = f'fixture_{run}_extra_{extra}'
            rows.append({'openai': {'type': 'response.created', 'response': {'id': rid}}})
            rows.append({'openai': {'type': 'response.done', 'response': {'id': rid, 'usage': {'total_tokens': 0}}}})
    return rows


if __name__ == '__main__':
    target = Path(__file__).with_name('synthetic_regression_runs.jsonl')
    target.write_text(''.join(json.dumps(row) + '\n' for row in build()), encoding='utf-8')
