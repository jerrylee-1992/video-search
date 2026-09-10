from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path


@dataclass(frozen=True)
class SourceMetadata:
    source_root: str
    relative_path: str
    project_name: str
    event_date: str | None
    media_type: str | None
    edit_version: str | None
    search_text: str

    def as_database_values(self) -> dict[str, str | None]:
        values = asdict(self)
        values["metadata_search_text"] = values.pop("search_text")
        return values


_MEDIA_TYPES = (
    (re.compile(r"ceremony", re.IGNORECASE), "ceremony", "ceremony 仪式 婚礼仪式"),
    (re.compile(r"(?:^|[_\-\s])hl(?:[_\-\s]|$)", re.IGNORECASE), "highlight", "highlight 精剪 婚礼精剪"),
    (re.compile(r"tea", re.IGNORECASE), "tea", "tea 茶礼 敬茶"),
    (re.compile(r"(?:^|[_\-\s])ts(?:[_\-\s]|$)", re.IGNORECASE), "teaser", "teaser trailer 预告"),
)


def _event_date(text: str) -> str | None:
    for match in re.finditer(r"(?<!\d)(\d{2})(\d{2})(\d{2})(?!\d)", text):
        year, month, day = (int(value) for value in match.groups())
        try:
            return date(2_000 + year, month, day).isoformat()
        except ValueError:
            continue
    return None


def infer_source_metadata(root: Path, path: Path) -> SourceMetadata:
    root = Path(root).resolve()
    path = Path(path).resolve()
    relative = path.relative_to(root)
    project_name = path.parent.name if path.parent != root else path.stem
    media_type = None
    media_aliases = ""
    for pattern, candidate, aliases in _MEDIA_TYPES:
        if pattern.search(path.stem):
            media_type = candidate
            media_aliases = aliases
            break
    version_match = re.search(
        r"(?:^|[_\-\s])(v\d+)(?:[_\-\s]|$)", path.stem, re.IGNORECASE
    )
    edit_version = version_match.group(1).lower() if version_match else None
    event_date = _event_date(str(relative))
    readable_path = re.sub(r"[_/\\]+", " ", str(relative))
    search_text = " ".join(
        value
        for value in (
            project_name,
            readable_path,
            event_date,
            media_aliases,
            edit_version,
        )
        if value
    )
    return SourceMetadata(
        source_root=str(root),
        relative_path=str(relative),
        project_name=project_name,
        event_date=event_date,
        media_type=media_type,
        edit_version=edit_version,
        search_text=search_text,
    )
