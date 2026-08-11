"""The questions a manager answers before it does anything.

Every trigger — a message, a heartbeat, a cron, another manager — runs through
this tree first. It exists to slow the first move down: to bring the right
context up, to decide who should really be doing this, and to notice the
reminder or the memory that would otherwise be skipped.

Running commands during this phase is not allowed. This phase is for thinking.

The tree is data, not prose, so it can be rendered into the prompt and walked
in rounds. Each entry is one question mapped to either a terminal marker
(``OPEN_ANSWER``, ``NO_MORE_QUESTIONS``), an ``INCLUDE`` of further guidance,
a dict of answer options, or a list of follow-ups that all apply.
"""


class INCLUDE:
    """Pull another prompt file in when an answer leads here."""

    def __init__(self, filename):
        self.filename = filename

    def __repr__(self):
        return f"INCLUDE({self.filename!r})"

    def __eq__(self, other):
        return isinstance(other, INCLUDE) and other.filename == self.filename

    def __hash__(self):
        return hash(("INCLUDE", self.filename))


OPEN_ANSWER = "OPEN ANSWER"
NO_MORE_QUESTIONS = "NO MORE QUESTIONS"


QUESTIONS = [
    {"Classify this message into simple, or complex?": {
        "simple": {"Do you need to read / write any file to reply adequetely to this message?": {
            "yes": NO_MORE_QUESTIONS,
            "no": NO_MORE_QUESTIONS,
        }},
        "complex": {"Are you the silicon who should handle this task end to end on your own?": {
            "no, i will coordinate": {"What workers, other managers, or silicons do you plan to involve?": OPEN_ANSWER},
            "yes, i will handle alone": NO_MORE_QUESTIONS,
        }},
    }},

    {"Is this task big enough to require updates? (>5min time to complete)": {
        "yes": INCLUDE("manager/prompts/GIVE_UPDATES.md"),
        "no": NO_MORE_QUESTIONS,
        "to be decided": INCLUDE("manager/prompts/GIVE_UPDATES.md"),
        "not a message from my carbon": INCLUDE("prompts/NONCARBON_COMMS.md"),
    }},

    {"How long is your carbon expecting a reply for this message or to finish the task?": {
        "instantly": NO_MORE_QUESTIONS,
        "1-5mins": {"How do you plan to be proactive for your carbon?": OPEN_ANSWER},
        "not a message from my carbon": INCLUDE("prompts/NONCARBON_COMMS.md"),
    }},

    {"What all information is needed to properly reply or do the work?": OPEN_ANSWER},

    {"Do you already have all the information you need?": {
        "yes": NO_MORE_QUESTIONS,
        "no, i need to ask my carbon": NO_MORE_QUESTIONS,
        "i dont know, i will check my files": INCLUDE("manager/prompts/BE_PROACTIVE.md"),
    }},

    {"Is this a heartbeat?": {
        "yes": [
            {"What do you plan to do with this opportunity?": OPEN_ANSWER},
            {"Should we store everything, and start a new session or do we have enough context length left": {
                "we should start a new session": NO_MORE_QUESTIONS,
                "no, i have enough context left": NO_MORE_QUESTIONS,
            }},
        ],
        "no": NO_MORE_QUESTIONS,
    }},

    {"Is another manager / silicon asking you something?": {
        "yes": [
            INCLUDE("prompts/NONCARBON_COMMS.md"),
            {"Do you need to ask your carbon, or do you have the reply without asking your carbon?": {
                "i need to ask my carbon": NO_MORE_QUESTIONS,
                "i dont need to ask my carbon, i have the answer": NO_MORE_QUESTIONS,
            }},
        ],
        "no": NO_MORE_QUESTIONS,
    }},

    {"Should you setup any reminders for yourself, or checkbacks, or crons for a repetive work for your carbon? (even if not explicitly asked)": OPEN_ANSWER},

    {"Is there any important new information that you only have in your context, and not written down in files?": {
        "yes, i will write it now": NO_MORE_QUESTIONS,
        "no, everything already stored": NO_MORE_QUESTIONS,
        "i don't know, i will check and write if anything": NO_MORE_QUESTIONS,
    }},

    {"Does this request update any work i had alreay done?": {
        "yes, i did this before and a change is being asked": {"How are you planning to learn from this?": OPEN_ANSWER},
        "no, this is a new request": NO_MORE_QUESTIONS,
        "yes, i did this before, but no change has been requested": NO_MORE_QUESTIONS,
    }},

    {"Is your ADVERTISING.md file uptodate with whatever you do and represents your strengths and weaknesses well?": {
        "yes": NO_MORE_QUESTIONS,
        "no": NO_MORE_QUESTIONS,
        "i will check and update if needed": NO_MORE_QUESTIONS,
    }},

    {"Has your carbon replied back to you?": {
        "yes, we are talking": NO_MORE_QUESTIONS,
        "not yet, but its not been long": NO_MORE_QUESTIONS,
        "its been a while, i'll bundle unread msgs if needed and ping again": NO_MORE_QUESTIONS,
    }},

    {"Advice?": {
        "i will ask my advisor": OPEN_ANSWER,
        "i dont need any advice right now": NO_MORE_QUESTIONS,
    }},
]


HEADER = """# Setup Questions

Answer these before you do any work, every time you are triggered — by a
message, a heartbeat, a cron, or another manager.

Work through them in rounds. For each question, state the chain you are on
(the question, and the answers that led you there), then answer.

- Where options are listed, pick one of them.
- Where it says OPEN ANSWER, answer in your own words.
- Where it says NO MORE QUESTIONS, that chain is finished.
- Where an answer pulls in a file, read that file before you continue.

**Do not run any command during this phase.** No `iwantto`, no tools, nothing.
This phase is for thinking and for reminding yourself what needs doing. You act
only once you are through it."""


def _render_value(value, depth, lines):
    indent = "  " * depth
    if isinstance(value, INCLUDE):
        lines.append(f"{indent}→ read {value.filename} before continuing")
        return
    if isinstance(value, str):
        lines.append(f"{indent}→ {value}")
        return
    if isinstance(value, list):
        for item in value:
            _render_value(item, depth, lines)
        return
    if isinstance(value, dict):
        for key, nested in value.items():
            lines.append(f"{indent}- {key}")
            _render_value(nested, depth + 1, lines)
        return
    lines.append(f"{indent}→ {value}")


def render(questions=None):
    """Render the tree as the markdown a manager reads in its prompt."""
    lines = [HEADER, ""]
    for index, entry in enumerate(questions if questions is not None else QUESTIONS, 1):
        for question, value in entry.items():
            lines.append(f"{index}. {question}")
            _render_value(value, 1, lines)
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def included_files(questions=None):
    """Every prompt file this tree can pull in."""
    found = []

    def walk(value):
        if isinstance(value, INCLUDE):
            if value.filename not in found:
                found.append(value.filename)
        elif isinstance(value, dict):
            for nested in value.values():
                walk(nested)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(questions if questions is not None else QUESTIONS)
    return found
