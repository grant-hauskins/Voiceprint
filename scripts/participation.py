"""Participation policy: the default mode is a lookup by (conversation type, role), layered on the live gate.

BUILD_SPEC_V3 §5. The gate in turn_gate.py still decides *whether to speak now*; this table only decides how a
request reaches it: raise-hand advocates wait for a direct address or a verified override, the arbitrator posts
proactively (it has no voice, so "speak" means writing to the board or prompting an advocate), and everyone in a
casual room keeps the v2 low-threshold behavior.
"""
CONVERSATION_TYPES = ("casual", "negotiation")
ROLES = ("voice", "arbitrator")
OVERRIDE_TAGS = ("OBJECTIVE_ACHIEVED", "REFOCUS_NEEDED")
MODES = ("raise_hand", "proactive", "low_threshold")


def default_mode(conversation_type, role, speaks_for=""):
    if conversation_type not in CONVERSATION_TYPES:
        raise ValueError(f"Unknown conversation type {conversation_type!r}")
    if role not in ROLES:
        raise ValueError(f"Unknown role {role!r}")
    if conversation_type == "negotiation":
        if role == "arbitrator":
            return "proactive"
        if speaks_for:
            return "raise_hand"          # an advocate waits for the floor
    return "low_threshold"


def apply_mode(gate_state, mode):
    """Only raise-hand changes the live gate: soft opportunities are off until a direct address or an override."""
    if mode not in MODES:
        raise ValueError(f"Unknown participation mode {mode!r}")
    gate_state.raise_hand = mode == "raise_hand"
    return gate_state
