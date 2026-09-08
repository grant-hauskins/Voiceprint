"""One-shot OpenAI Responses API text calls with a strict JSON schema, for the arbitrator and the summarizer.

Every call is gated on the room's negotiation_text disclosure scope. Requests use store:false; response bodies
are parsed and never printed or logged; HTTP failures surface only as a status code.
"""
import json
import os
import urllib.error
import urllib.request

import voiceprint_client as vp

RESPONSES_URL = "https://api.openai.com/v1/responses"
TIMEOUT_S = 90


def _open(request):
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        return json.load(response)


class OpenAIResponses:
    def __init__(self, model, consent, opener=None):
        self.model, self.consent = model, consent
        self.opener = opener or _open
        self.calls = 0

    def _generate(self, instructions, input_text, schema_name, schema):
        vp.require_consent(self.consent, "negotiation_text")
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("Set OPENAI_API_KEY before the arbitrator or summarizer can run")
        body = {"model": self.model, "store": False, "instructions": instructions, "input": input_text,
                "text": {"format": {"type": "json_schema", "name": schema_name, "schema": schema, "strict": True}}}
        request = urllib.request.Request(RESPONSES_URL, json.dumps(body).encode(),
                                         {"Content-Type": "application/json", "Authorization": "Bearer " + key}, method="POST")
        self.calls += 1
        try:
            result = self.opener(request)
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"OpenAI Responses returned HTTP {error.code}") from None
        for item in result.get("output", []) or []:
            if item.get("type") != "message":
                continue
            for part in item.get("content", []) or []:
                if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                    parsed = json.loads(part["text"])
                    if not isinstance(parsed, dict):
                        raise RuntimeError("OpenAI Responses output was not a JSON object")
                    return parsed
        raise RuntimeError("OpenAI Responses returned no output_text")

    def generate(self, instructions, input_text, schema_name, schema):
        """Synchronous; callers wrap it in asyncio.to_thread. Returns the parsed JSON object."""
        return self._generate(instructions, input_text, schema_name, schema)
