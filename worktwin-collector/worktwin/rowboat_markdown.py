"""Small Python port of Rowboat's Markdown index field parsers.

SPDX-License-Identifier: Apache-2.0
Derived from rowboatlabs/rowboat/apps/x/packages/core/src/knowledge/knowledge_index.ts
at f07c3fcd7793e99d850ee61363a91eafa16d6afb.
Changes: TypeScript -> Python; escape field names; omit filesystem/Node runtime.
See third_party/rowboat/LICENSE and NOTICE.md.
"""
import re


def extract_field(content: str, field_name: str) -> str | None:
    # Spaces/tabs only: an empty field must not consume the next line's value.
    match=re.search(r'\*\*'+re.escape(field_name)+r':\*\*[ \t]*([^\r\n]+)(?:\r?\n|$)',content,re.I)
    if not match:return None
    value=match.group(1).strip()
    link=re.search(r'\[\[(?:[^\]|]+\|)?([^\]]+)\]\]',value)
    if link:value=link.group(1)
    return value or None


def extract_list(content: str, field_name: str) -> list[str]:
    value=extract_field(content,field_name)
    return [part.strip() for part in value.split(',') if part.strip()] if value else []


def extract_title(content: str) -> str:
    match=re.search(r'^#\s+(.+?)$',content,re.M)
    return match.group(1).strip() if match else ''
