import os
import json
import time

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MANAGER_MESSAGES_FILE = os.path.join(PROJECT_ROOT, "core", "interface_state", "manager_queue.json")


def _load_manager_messages():
    if os.path.exists(MANAGER_MESSAGES_FILE):
        with open(MANAGER_MESSAGES_FILE) as f:
            return json.load(f)
    return {}


def _save_manager_messages(messages):
    os.makedirs(os.path.dirname(MANAGER_MESSAGES_FILE), exist_ok=True)
    with open(MANAGER_MESSAGES_FILE, "w") as f:
        json.dump(messages, f, indent=2)


def send_manager_message(from_contact_id, to_contact_id, message):
    """Queue a message from one manager to another. Delivered on next event loop tick."""
    messages = _load_manager_messages()
    if to_contact_id not in messages:
        messages[to_contact_id] = []
    messages[to_contact_id].append({
        "from_contact_id": from_contact_id,
        "message": message,
        "timestamp": time.time(),
    })
    _save_manager_messages(messages)
    return "Done. Message queued for delivery to the other manager."


def check_manager_messages():
    """Check for pending inter-manager messages. Returns {contact_id: context_string}."""
    messages = _load_manager_messages()
    if not messages:
        return {}

    result = {}
    for contact_id, msgs in messages.items():
        if not msgs:
            continue
        parts = []
        for m in msgs:
            sender = m.get("from_contact_id") or m.get("from_carbon_id") or "unknown"
            parts.append(f"Message from manager of {sender}:\n{m['message']}")
        result[contact_id] = "Inter-manager messages:\n" + "\n---\n".join(parts)

    # Clear delivered messages
    _save_manager_messages({})
    return result
