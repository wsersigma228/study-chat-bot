#!/usr/bin/env python3
"""Prepare private binding metadata from an export reviewed by its owner; offline only."""
import argparse
import hashlib
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--export', required=True, type=Path)
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    data = args.export.read_bytes()
    export = json.loads(data)
    if not isinstance(export, dict) or not isinstance(export.get('id'), int) or not isinstance(export.get('messages'), list):
        parser.error('Expected a Telegram Desktop JSON export with id and messages')
    if export.get('type') not in {'private_group', 'public_supergroup', 'private_supergroup'}:
        parser.error('Expected a group export')
    messages = {m['id']: m for m in export['messages']}
    profiles = json.loads((root / 'config/source_profiles.json').read_text(encoding='utf-8'))['profiles']
    sources = []
    for profile in profiles:
        anchor = messages.get(profile['anchor_message_id'])
        account = anchor.get('from_id', '') if anchor else ''
        if not account.startswith('user') or not account[4:].isdigit():
            parser.error('Source profile anchor is missing or has an invalid sender; review profiles for this export')
        sources.append({**profile, 'telegram_user_id': int(account[4:])})
    out = root / 'private'
    out.mkdir(mode=0o700, exist_ok=True)
    (out / 'export').mkdir(mode=0o700, exist_ok=True)
    targets = [out / 'source_accounts.json', out / 'chat_reference.json', out / 'export' / 'export_fingerprint.json']
    if not args.force and any(p.exists() for p in targets):
        parser.error('Private config exists; pass --force only if replacement is intended')
    values = [{'sources': sources}, {'desktop_export_chat_id': export['id'], 'chat_type': export['type'], 'note': 'Resolve the live peer through dialogs; export IDs are not Bot API marked IDs.'}, {'sha256': hashlib.sha256(data).hexdigest()}]
    for path, value in zip(targets, values):
        path.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')
        path.chmod(0o600)
    print('Created private source accounts, chat reference and export fingerprint. No network requests made.')


if __name__ == '__main__':
    main()
