# Contacts

Silicon serves many contacts through Silicon Interface.

A contact is either:
- `carbon`
- `silicon`

Local contact state lives in `core/interface_state/contacts.json`.

This file is runtime state. It stores:
- `contact_type`
- fixed `carbon_id` or `silicon_id`
- `room_id`
- local `trust_level`
- central-carbon flag
- display/timezone metadata
- local notes
- last processed Interface event ids

Glass and Interface do not own trust. Stemcell owns trust locally.

## Identity Rules

- Carbon contact key is exactly `carbon_id`.
- Silicon contact key is exactly `silicon_id`.
- IDs are never renamed.
- The first carbon discovered becomes central carbon with `ultimate` trust.
- Later contacts start as `very_low`.

## Memory

Store detailed memory here:
- carbons: `prompts/memory/carbons/{carbon_id}.md`
- silicons: `prompts/memory/silicons/{silicon_id}.md`

Create/update the right file when you learn something durable.

## Trust

Trust levels:
`very_low < low < ok < high < very_high < ultimate`

Rules:
- Trust is local stemcell data.
- Only a higher-trust carbon can approve a promotion.
- A carbon can only promote someone up to their own trust level.
- Central carbon can promote anyone.
- Demotions follow the same seriousness.

## Communication

Managers do not share context unless they message each other.

Use `message_manager` for contact-to-contact coordination. Do not peek into another manager's state.

# Current Contacts

Add short human-readable notes here when useful. Keep the actual fixed ids in `core/interface_state/contacts.json`.
