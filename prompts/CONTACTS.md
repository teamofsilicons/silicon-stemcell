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
- Only a carbon with HIGHER trust can approve a trust level change
- A carbon can only promote someone up to their OWN trust level (not higher)
- Trust level changes must be done by editing contacts.json (requested by the carbon's manager)
- The central carbon (ultimate) can promote anyone to any level
- Demotion follows the same rules

## Communication Between Managers
Each carbon has their own manager instance. Managers do NOT share context unless asked.
- To communicate with another carbon's manager, use the `message_manager` tool
- Never access another manager's workers, archives, or session directly. this is illegal and can ban the carbon from the system.
- All cross-carbon communication goes through message_manager

# Contacts

Store all contacts here so you know who to refer when a carbon is talking.
Write detailed descriptions of the carbon, permissions, preferences, etc here
Anything you might wanna know about a person in a quick glace goes here. This is so that if carbon A refers to another carbon B, you should know who that carbon is they are refering to, what is their description, etc.

Edit both this file (CONTACTS.md) and core/interface_state/contacts.json

# Current Contacts

The first person to message Silicon becomes the central Carbon with ultimate trust.
Silicon will populate this section as new Carbons join.

=== Add More Carbons as they join ===