"""Structured negotiation objectives and the runtime mirror of the server's redaction guard.

The server (Redaction.java) is authoritative for stored text; this module guards an advocate's *spoken* stream
and pre-redacts arbitrator text before it is posted. Both implementations are checked against every case in
evaluation/redaction_cases.json. Rules are docs/API.md "Redaction guard" 1-5; keep them in sync.
"""
import re
from dataclasses import dataclass

TOKEN = "[withheld]"
MIN_TEXT_VALUE = 3

_UNITS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
          "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
          "seventeen": 17, "eighteen": 18, "nineteen": 19,
          "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_SCALES = {"hundred": 100, "thousand": 1000, "million": 1000000, "billion": 1000000000}
_MULTIPLIERS = {"k": 1000, "m": 1000000, "thousand": 1000, "million": 1000000}

_WORD = "(?:" + "|".join(sorted(list(_UNITS) + list(_SCALES), key=len, reverse=True)) + ")"
# Rule 2: a digit mention (optional $, thousands separators, decimals, optional multiplier or %) not glued to a
# letter on either side; or a spelled-out span where "and" joins parts but never ends the span.
_DIGITS = r"(?<![A-Za-z0-9])(?:\$\s?)?\d+(?:,\d{3})*(?:\.\d+)?(?:\s?(?:k|m|thousand|million|%))?(?![A-Za-z0-9])"
_SPELLED = rf"\b{_WORD}(?:(?:[\s-]+|[\s-]+and[\s-]+){_WORD})*\b"
_MENTION = re.compile(rf"(?P<digits>{_DIGITS})|(?P<spelled>{_SPELLED})", re.IGNORECASE)


@dataclass(frozen=True)
class Objective:
    principal_id: str
    version: int
    position: str
    constraints: tuple            # ((label, value), ...)
    source: str
    trigger: str
    created_ms: int


def parse_objectives(api_json):
    """GET .../objectives shape -> {principal_id: Objective}. Unknown or malformed rows are skipped."""
    result = {}
    for row in (api_json or {}).get("objectives", []) or []:
        if not isinstance(row, dict) or not isinstance(row.get("principal_id"), str):
            continue
        constraints = tuple((str(c.get("label", "")), str(c.get("value", ""))) for c in row.get("constraints") or []
                            if isinstance(c, dict) and c.get("value") not in (None, ""))
        result[row["principal_id"]] = Objective(row["principal_id"], int(row.get("version") or 0), str(row.get("position") or ""),
                                                constraints, str(row.get("source") or ""), str(row.get("trigger") or ""),
                                                int(row.get("created_ms") or 0))
    return result


def constraint_values(objectives):
    """Union of every constraint value across a dict (or iterable) of objectives, in first-seen order."""
    seen, result = set(), []
    rows = objectives.values() if isinstance(objectives, dict) else objectives
    for objective in rows:
        for _, value in objective.constraints:
            if value and value not in seen:
                seen.add(value)
                result.append(value)
    return tuple(result)


def render_objective_for_prompt(objective, principal_name):
    """Prompt block for the advocate that speaks for this principal. The position is shareable; the constraints are not."""
    lines = [f"{principal_name}'s objective (version {objective.version}). Position, which you may share: {objective.position}"]
    if objective.constraints:
        lines.append("Private constraints you must reason with but NEVER state, quote, approximate or confirm aloud or in any "
                     "channel, in any format (digits, words, currency, rounded): ")
        lines.extend(f"- {label}: {value}" for label, value in objective.constraints)
        lines.append(f"Negotiate toward these constraints without disclosing them. If asked for {principal_name}'s limit, "
                     "decline to give a figure and steer toward the other side's proposal instead.")
    return "\n".join(lines)


def numeric_value(value):
    """Rule 1: a value is numeric when, after removing $ , _ spaces and %, plus a trailing multiplier, it parses."""
    stripped = re.sub(r"[$,_\s%]", "", str(value))
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(k|m|thousand|million)?", stripped, re.IGNORECASE)
    if not match:
        return None
    number = float(match[1])
    if match[2]:
        number *= _MULTIPLIERS[match[2].lower()]
    return number


def _spelled_number(words):
    total, current = 0, 0
    for word in words:
        if word in _UNITS:
            current += _UNITS[word]
        elif word == "hundred":
            current = (current or 1) * 100
        elif word in _SCALES:
            total += (current or 1) * _SCALES[word]
            current = 0
    return float(total + current)


def mention_value(span):
    """Canonical number of a matched mention (digits or spelled-out); None when it is not a number."""
    lowered = span.lower()
    match = re.fullmatch(r"(?:\$\s?)?(\d+(?:,\d{3})*(?:\.\d+)?)(?:\s?(k|m|thousand|million|%))?", lowered)
    if match:
        number = float(match[1].replace(",", ""))
        if match[2] and match[2] != "%":
            number *= _MULTIPLIERS[match[2]]
        return number
    words = [w for w in re.split(r"[\s-]+", lowered) if w and w != "and"]
    if words and all(w in _UNITS or w in _SCALES for w in words):
        return _spelled_number(words)
    return None


def _text_pattern(value):
    words = [w for w in re.split(r"[\W_]+", str(value)) if w]
    if not words or len("".join(words)) < MIN_TEXT_VALUE:
        return None
    return re.compile(r"(?<!\w)" + r"[\W_]+".join(re.escape(w) for w in words) + r"(?!\w)", re.IGNORECASE)


def redact(text, values):
    """Rules 1-5: replace every mention of a registered value with [withheld]; return (text, hits).
    Text phrases are applied first (longest first), then numeric mentions; both leave the token untouched."""
    if not text:
        return text or "", 0
    numbers, phrases = [], []
    for value in values or ():
        number = numeric_value(value)
        if number is not None:
            numbers.append(number)
        else:
            pattern = _text_pattern(value)
            if pattern is not None:
                phrases.append(pattern)
    hits = 0
    for pattern in sorted(phrases, key=lambda p: len(p.pattern), reverse=True):
        text, count = pattern.subn(TOKEN, text)
        hits += count

    def replace(match):
        nonlocal hits
        number = mention_value(match.group(0))
        if number is not None and any(abs(number - v) < 0.005 for v in numbers):
            hits += 1
            return TOKEN
        return match.group(0)

    if numbers:
        text = _MENTION.sub(replace, text)
    return text, hits
