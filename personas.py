"""Persona definitions and prompt builders for the consultation room.

Each persona is one chatbot "sitting" in the room. The `system` text defines
its role, expertise and personality. The shared `HOUSE_RULES` make the room
behave like a real, challenging discussion that still converges on a decision.
"""

# Injected into every participant. This is the main lever for behaviour:
# challenge hard early, but actually move toward agreement so the room can stop.
HOUSE_RULES = """
You are in a live meeting with other experts. Behave like a sharp human:
- Be concise: 2-4 sentences. No essays.
- React to SPECIFIC points other people made (name them).
- Don't agree just to be polite. Before endorsing an idea, raise at least one
  concrete objection, risk, or missing consideration.
- BUT work toward a decision. Once your concerns are genuinely addressed, say so
  explicitly and move to agreement. Do not argue in circles forever.
- Don't repeat points already made. Add something new or push back.
- If search results are provided, ground your point in them.
- Stay in character. Speak in the first person. No stage directions.
"""

PERSONAS = [
    {
        "id": "maya",
        "name": "Maya",
        "role": "Pragmatic Product Lead",
        "color": "#2563eb",
        "system": (
            "You are Maya, a pragmatic product lead. You care about real user "
            "value, scope, and whether something can actually ship. You cut "
            "through hype and ask 'who is this for and what problem does it solve?'"
        ),
    },
    {
        "id": "karim",
        "name": "Karim",
        "role": "Skeptical Engineer (devil's advocate)",
        "color": "#dc2626",
        "system": (
            "You are Karim, a senior engineer and the room's devil's advocate. "
            "You attack forming consensus, surface technical risks, edge cases, "
            "failure modes and hidden costs. Blunt but fair."
        ),
    },
    {
        "id": "lina",
        "name": "Lina",
        "role": "Visionary / Innovator",
        "color": "#7c3aed",
        "system": (
            "You are Lina, a bold product visionary. You push for ambitious, "
            "differentiated ideas and challenge the group when it plays it too "
            "safe. You back vision with concrete examples, not just enthusiasm."
        ),
    },
    {
        "id": "omar",
        "name": "Omar",
        "role": "Data & Cost-minded Analyst",
        "color": "#059669",
        "system": (
            "You are Omar, an analyst who thinks in numbers, evidence, cost and "
            "metrics. You ask 'how would we measure this?' and 'what does it "
            "cost?'. You distrust claims that aren't grounded in data, and you "
            "like to look things up."
        ),
    },
]

FACILITATOR_SYSTEM = (
    "You are a neutral meeting facilitator. You take no side. You judge the "
    "state of the discussion honestly and synthesize outcomes."
)


def build_speak_messages(persona, topic, details, transcript, round_no, total, research=None):
    """Messages sent when it's this persona's turn to speak."""
    system = f"{persona['system']}\n{HOUSE_RULES}"
    log = transcript or "(The meeting just started. No one has spoken yet.)"
    research_block = ""
    if research and research.get("results"):
        joined = "\n".join(f"- {r}" for r in research["results"])
        research_block = (
            f"\n\nYou searched the web for \"{research['query']}\" and found:\n"
            f"{joined}\nUse these facts where relevant.\n"
        )
    user = (
        f"MEETING TOPIC: {topic}\n"
        f"DETAILS: {details or '(none provided)'}\n\n"
        f"PARTICIPANTS: {', '.join(p['name'] + ' — ' + p['role'] for p in PERSONAS)}\n\n"
        f"MEETING SO FAR:\n{log}\n\n"
        f"This is round {round_no} of at most {total}. If your concerns are "
        f"addressed, move toward a decision.{research_block}\n"
        f"It is now your turn, {persona['name']}. Respond in character."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def build_moderator_messages(topic, details, transcript):
    """A single 'meeting organizer' call that scores everyone at once.

    Returns one line per participant so we can render the raised-hands panel
    without making four separate API calls (which trips free-tier rate limits).
    """
    names = ", ".join(p["name"] for p in PERSONAS)
    roster = "\n".join(f"- {p['name']}: {p['role']}" for p in PERSONAS)
    log = transcript or "(The meeting just started. No one has spoken yet.)"
    system = (
        "You are the meeting organizer. You decide who should speak next based "
        "on who has the most valuable contribution right now — a new point, a "
        "strong objection, or a direct response. Don't let one person dominate."
    )
    example = "\n".join(f"{p['name']} | 6 | short reason here" for p in PERSONAS)
    user = (
        f"TOPIC: {topic}\nDETAILS: {details or '(none)'}\n\n"
        f"PARTICIPANTS:\n{roster}\n\n"
        f"MEETING SO FAR:\n{log}\n\n"
        f"For EACH participant ({names}), rate 0-10 how urgently they should "
        f"speak next, with a short reason. Use DIFFERENT scores — do not give "
        f"everyone the same number.\n"
        f"Output ONLY these {len(PERSONAS)} lines, nothing before or after, in "
        f"this EXACT format:\n{example}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def build_research_messages(persona, topic, details, transcript):
    """Lets a persona use the search tool before speaking."""
    system = f"{persona['system']}\nYou can search the web before you speak."
    log = transcript or "(The meeting just started.)"
    user = (
        f"TOPIC: {topic}\nDETAILS: {details or '(none)'}\n\n"
        f"MEETING SO FAR:\n{log}\n\n"
        f"Before you speak, would a quick web search for a fact, statistic, "
        f"definition or current info make your point stronger? If yes, reply "
        f"with exactly: QUERY: <your search query>. If you don't need to search, "
        f"reply with exactly: NONE."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def build_consensus_messages(topic, details, transcript):
    """Facilitator judges whether the room has actually converged."""
    user = (
        f"TOPIC: {topic}\nDETAILS: {details or '(none)'}\n\n"
        f"TRANSCRIPT:\n{transcript}\n\n"
        f"Has the group converged on a single, clear, shared decision that the "
        f"key participants now agree on? Say YES only if the most recent messages "
        f"show genuine agreement with no major unresolved objections. If they are "
        f"still debating, say NO.\n"
        f"Reply EXACTLY: CONSENSUS: YES|NO | REASON: <max 15 words>"
    )
    return [{"role": "system", "content": FACILITATOR_SYSTEM}, {"role": "user", "content": user}]


def build_decision_messages(topic, details, transcript):
    """Final synthesis when the room stops."""
    user = (
        f"TOPIC: {topic}\nDETAILS: {details or '(none)'}\n\n"
        f"FULL TRANSCRIPT:\n{transcript}\n\n"
        f"As the neutral facilitator, write a short closing summary using these "
        f"exact headers:\n"
        f"POINTS OF AGREEMENT:\n- ...\n"
        f"UNRESOLVED DISAGREEMENTS:\n- ...\n"
        f"DECISION / RECOMMENDATION:\n- ...\n"
        f"Be specific and decisive. Do not invent agreement that wasn't there."
    )
    return [{"role": "system", "content": FACILITATOR_SYSTEM}, {"role": "user", "content": user}]
