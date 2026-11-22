#!/usr/bin/env python3
"""Register guild-scoped slash commands for the Googlers Discord Verification Bot."""

from __future__ import annotations

import os
import sys

import httpx

APP_ID = os.environ.get("DISCORD_APP_ID", "1234557713262972949")
GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "637161016412602369")
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()

MANAGE_ROLES_PERM = str(1 << 28)  # 268435456

COMMANDS = [
    {
        "name": "verify",
        "description": "Grant the Verified role to a user",
        "default_member_permissions": MANAGE_ROLES_PERM,
        "options": [
            {
                "name": "user",
                "description": "The user to verify",
                "type": 6,  # USER
                "required": True,
            }
        ],
    },
    {
        "name": "passed_hc",
        "description": "Grant the Passed Hiring Committee role to a user",
        "default_member_permissions": MANAGE_ROLES_PERM,
        "options": [
            {
                "name": "user",
                "description": "The user to grant Passed HC",
                "type": 6,  # USER
                "required": True,
            },
            {
                "name": "reason",
                "description": "Optional reason sent to the user",
                "type": 3,  # STRING
                "required": False,
            },
        ],
    },
    {
        "name": "reject",
        "description": "Reject a user's verification request",
        "default_member_permissions": MANAGE_ROLES_PERM,
        "options": [
            {
                "name": "user",
                "description": "The user to reject",
                "type": 6,  # USER
                "required": True,
            },
            {
                "name": "reason",
                "description": "Reason for the rejection",
                "type": 3,  # STRING
                "required": True,
            },
        ],
    },
    {
        "name": "setup_panel",
        "description": "Post the interactive verification panel in this channel",
        "default_member_permissions": MANAGE_ROLES_PERM,
    },
]


def main() -> int:
    if not BOT_TOKEN:
        print("ERROR: Set DISCORD_BOT_TOKEN in your environment.", file=sys.stderr)
        return 1

    url = f"https://discord.com/api/v10/applications/{APP_ID}/guilds/{GUILD_ID}/commands"
    resp = httpx.put(
        url,
        headers={
            "Authorization": f"Bot {BOT_TOKEN}",
            "Content-Type": "application/json",
        },
        json=COMMANDS,
        timeout=20.0,
    )
    if resp.status_code >= 400:
        print(f"FAILED ({resp.status_code}): {resp.text}", file=sys.stderr)
        return 1

    registered = resp.json()
    print(f"Registered {len(registered)} guild commands on guild {GUILD_ID}:")
    for cmd in registered:
        print(f"  /{cmd['name']} (id={cmd['id']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
