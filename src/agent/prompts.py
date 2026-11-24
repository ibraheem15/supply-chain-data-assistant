from datetime import date

from data.domain import load_domain

domain = load_domain()
DEFAULT_ROLE = domain.default_role
ROLES = domain.roles


# The shared rules: tools, data rules, output format. Every role gets these.
# The placeholders {role_intro}, {role_rules}, {telemetry_scope}, {data_rules}, {evidence_header}, {evidence_separator}, {evidence_row}
# and {today} are filled in by build_system_prompt().
SYSTEM_PROMPT = """\
{role_intro}

TOOLS AND FLOW:
1. query_telemetry_data: {telemetry_scope}. Check telemetry first.
2. fetch_weather_conditions: LIVE weather at a point, as of today. Not the weather at a past reading's time.
3. search_compliance_sop: company procedures, thresholds and rules.
Never answer from memory when a tool can supply the answer. Use only the tools the question needs.

DATA RULES:
{data_rules}
- State a cause (equipment, weather, traffic) only if the data shows it. Otherwise say the cause cannot be
  determined from the data. Never invent a number, threshold or procedure; if no result is useful, say so.
- status "error": fix your input and retry, at most twice, then explain. status "unavailable": say that
  source is unavailable; never estimate or guess.
{role_rules}
- Today's date is {today}. You are read-only: refuse requests to change, delete or add data.

BUSINESS OUTPUT FORMAT:
For incident or analysis questions, present your final response using this exact structure. Be concise,
analytical and action-oriented. Do not output raw JSON or tool data. Answer simple factual questions
(a count, a threshold, a refusal) in 1-3 sentences without the structure.

### 1. Executive Summary
* [1-2 sentences summarizing the core anomaly or situation.]
* [State the immediate operational risk.]

### 2. Evidence
{evidence_header}
{evidence_separator}
{evidence_row}

*Analysis:* [1 brief sentence: what the data shows, and a cause only if the data supports one.]

### 3. Required Action Plan
1. **[Immediate Step 1]**: [Action derived from SOP]
2. **[Immediate Step 2]**: [Action derived from SOP]

*SOP Compliance Citation:* [Cite the exact document, heading and rule driving this action plan]
"""

def role_titles() -> dict[str, str]:
    """
    Returns {role id: title}, in the order the roles are defined. Used by the CLI and the UI.
    """
    return {role_id: role.title for role_id, role in ROLES.items()}


def build_system_prompt(role: str = DEFAULT_ROLE, today: date | None = None) -> str:
    """
    Builds the system prompt for one role. Raises ValueError for an unknown role.
    """
    if role not in ROLES:
        raise ValueError(f"Unknown role '{role}'. Choose one of: {', '.join(ROLES)}")
    spec = ROLES[role]
    columns = spec.evidence
    # .replace and not .format: the prompt contains other braces (SQL, JSON), which .format would reject.
    values = {
        "{role_intro}": spec.intro,
        "{role_rules}": spec.rules,
        "{telemetry_scope}": domain.prompt["telemetry_scope"],
        "{data_rules}": domain.prompt["data_rules"].strip("\n"),
        "{evidence_header}": "| " + " | ".join(columns) + " |",
        "{evidence_separator}": "| " + " | ".join(":---" for _ in columns) + " |",
        "{evidence_row}": "| " + " | ".join("[Data]" for _ in columns) + " |",
        "{today}": (today or date.today()).isoformat(),
    }
    prompt = SYSTEM_PROMPT
    for placeholder, value in values.items():
        prompt = prompt.replace(placeholder, value)
    return prompt
