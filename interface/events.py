"""Reading one Interface event: who sent it, what it says, what it carries.

Every accessor here is defensive on purpose. The wire shape has changed more
than once, and a missing field must degrade to an empty string rather than
stop a Carbon's message from being delivered.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def _event_content(event: dict[str, Any]) -> dict[str, Any]:
    content = event.get("content")
    return content if isinstance(content, dict) else {}


def _first_text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value)
        if text:
            return text
    return ""


def _event_type(event: dict[str, Any]) -> str:
    content = _event_content(event)
    return _first_text(event.get("type"), event.get("event_type"), event.get("eventType"), content.get("msgtype"), content.get("type"))


def _event_id(event: dict[str, Any]) -> str:
    content = _event_content(event)
    return _first_text(event.get("event_id"), event.get("eventId"), event.get("id"), content.get("event_id"), content.get("id"))


def _event_room_id(event: dict[str, Any]) -> str:
    content = _event_content(event)
    room_id = _first_text(event.get("room_id"), event.get("roomId"), content.get("room_id"))
    if room_id:
        return room_id
    room = event.get("room")
    return room if isinstance(room, str) else ""


def _event_sender_candidates(event: dict[str, Any]) -> list[str]:
    content = _event_content(event)
    values: list[Any] = []
    sender = event.get("sender")
    if isinstance(sender, dict):
        values.extend(
            [
                sender.get("id"),
                sender.get("carbon_id"),
                sender.get("carbonId"),
                sender.get("silicon_id"),
                sender.get("siliconId"),
                sender.get("username"),
                sender.get("handle"),
                sender.get("public_id"),
                sender.get("publicId"),
                sender.get("name"),
            ]
        )
    else:
        values.append(sender)

    values.extend(
        [
            event.get("sender_id"),
            event.get("senderId"),
            event.get("sender_handle"),
            event.get("senderHandle"),
            event.get("sender_username"),
            event.get("senderUsername"),
            event.get("sender_public_id"),
            event.get("senderPublicId"),
            event.get("carbon_id"),
            event.get("carbonId"),
            event.get("silicon_id"),
            event.get("siliconId"),
            content.get("sender"),
            content.get("sender_id"),
            content.get("senderId"),
            content.get("sender_handle"),
            content.get("senderHandle"),
            content.get("sender_username"),
            content.get("senderUsername"),
            content.get("carbon_id"),
            content.get("carbonId"),
            content.get("silicon_id"),
            content.get("siliconId"),
        ]
    )
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            out.append(text)
            seen.add(text)
    return out


def _identity_set(values: Any) -> set[str]:
    out: set[str] = set()
    for value in values or []:
        text = str(value or "").strip()
        if not text:
            continue
        out.add(text)
        out.add(text.lower())
    return out


def _event_is_self(event: dict[str, Any], state: dict[str, Any]) -> bool:
    if event.get("is_self") or event.get("self") or event.get("sender_is_self"):
        return True
    own_ids = _identity_set(state.get("own_ids") or [])
    if not own_ids:
        return False
    senders = _identity_set(_event_sender_candidates(event))
    return bool(senders and senders.intersection(own_ids))


def _event_body(event: dict[str, Any]) -> str:
    content = _event_content(event)
    return _first_text(
        event.get("body"),
        event.get("text"),
        event.get("message"),
        event.get("caption"),
        content.get("body"),
        content.get("text"),
        content.get("message"),
        content.get("caption"),
    ).strip()


def _event_display_time(event: dict[str, Any]) -> str:
    content = _event_content(event)
    return _first_text(event.get("display_time"), event.get("displayTime"), content.get("display_time"), event.get("created_at"), event.get("createdAt"))


def _event_media_id(event: dict[str, Any]) -> str:
    content = _event_content(event)
    for obj in (event, content, event.get("file"), event.get("attachment"), content.get("file"), content.get("attachment")):
        if isinstance(obj, dict):
            value = _first_text(obj.get("media_id"), obj.get("mediaId"), obj.get("id"))
            if value:
                return value
    return ""


def _event_media_references(event: dict[str, Any]) -> list[tuple[str, str]]:
    """Return all attachment IDs and filenames in display order."""
    content = _event_content(event)
    if _event_type(event) != "m.album":
        media_id = _event_media_id(event)
        return [(media_id, _event_filename(event, media_id))] if media_id else []

    references: list[tuple[str, str]] = []
    seen: set[str] = set()
    collections = (
        content.get("items"),
        event.get("media_items"),
        content.get("media_items"),
        event.get("items"),
    )
    for collection in collections:
        if not isinstance(collection, list):
            continue
        for item in collection:
            if not isinstance(item, dict):
                continue
            media_id = _first_text(item.get("media_id"), item.get("mediaId"), item.get("id"))
            if not media_id or media_id in seen:
                continue
            filename = _first_text(item.get("filename"), item.get("file_name"), item.get("name"))
            references.append((media_id, Path(filename).name if filename else media_id))
            seen.add(media_id)

    if references:
        return references
    media_id = _event_media_id(event)
    return [(media_id, _event_filename(event, media_id))] if media_id else []


def _event_filename(event: dict[str, Any], media_id: str) -> str:
    content = _event_content(event)
    for obj in (event, content, event.get("file"), event.get("attachment"), content.get("file"), content.get("attachment")):
        if isinstance(obj, dict):
            value = _first_text(obj.get("filename"), obj.get("file_name"), obj.get("name"))
            if value:
                return Path(value).name
    return f"{media_id or 'media'}"


def _event_reply_to(event: dict[str, Any]) -> str:
    content = _event_content(event)
    for obj in (event, content):
        value = _first_text(obj.get("reply_to"), obj.get("reply_to_event_id"), obj.get("replyToEventId"))
        if value:
            return value
        reply = obj.get("reply")
        if isinstance(reply, dict):
            value = _first_text(reply.get("event_id"), reply.get("eventId"), reply.get("id"), reply.get("body"), reply.get("text"))
            if value:
                return value
    relates_to = content.get("m.relates_to") or content.get("relates_to")
    if isinstance(relates_to, dict):
        return _first_text(relates_to.get("m.in_reply_to", {}).get("event_id") if isinstance(relates_to.get("m.in_reply_to"), dict) else "", relates_to.get("event_id"))
    return ""


def _event_take_back_request_id(event: dict[str, Any]) -> str:
    content = _event_content(event)
    for obj in (event, content):
        value = _first_text(obj.get("take_back_request_id"), obj.get("takeBackRequestId"), obj.get("take_back_id"))
        if value:
            return value
        take_back = obj.get("take_back") or obj.get("takeBack")
        if isinstance(take_back, dict):
            value = _first_text(take_back.get("request_id"), take_back.get("requestId"), take_back.get("id"))
            if value:
                return value
    return ""


