"""Classify explicitly selected guidance files without turning them into business facts."""
from pathlib import Path


def reference_role(path):
    path=Path(path)
    name=path.name.lower()
    if name in ('agents.md','claude.md','gemini.md'):
        return 'project_rules'
    if name=='skill.md':
        return 'skill'
    if name in ('memory.md','preferences.md'):
        return 'memory'
    return ''


def reference_scope(path, root):
    path,root=Path(path),Path(root)
    # A selected home configuration folder is personal; a project directory is local.
    personal={Path.home()/'.codex',Path.home()/'.claude',Path.home()/'.gemini',Path.home()/'.agents'}
    return 'personal' if root in personal and path.parent==root else str(path.parent)


def vendor_reference(path):
    parts=Path(path).parts
    return any(parts[i:i+2] in (('plugins','cache'),('skills','.system')) for i in range(len(parts)-1))
