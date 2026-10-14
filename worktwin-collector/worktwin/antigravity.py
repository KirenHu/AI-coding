"""Read Antigravity's persisted Markdown artifacts, not its private chat DB."""
from pathlib import Path


def artifact_files(root: Path):
    root=root.resolve()
    for area in ('brain','knowledge'):
        base=root/area
        if not base.is_dir() or base.is_symlink():
            continue
        for path in base.rglob('*.md'):
            if path.is_file() and not any(p.is_symlink() for p in (path,*path.parents)) and path.resolve().is_relative_to(root):
                yield path


def detect_artifacts(root: Path):
    result={'kind':'antigravity','root':str(root),'exists':root.is_dir(),
            'recognized':0,'inspected':0,'unreadable':0,'sampled':True}
    for path in artifact_files(root):
        result['inspected']+=1
        try:
            with path.open(encoding='utf-8') as stream:
                if stream.read(4096).strip():result['recognized']+=1
        except (OSError,UnicodeError):result['unreadable']+=1
        if result['inspected']>=64:break
    return result
