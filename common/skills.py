"""Framework-free skill contracts and SKILL.md parsing helpers."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from common.contracts import SkillSpec


SKILL_CATALOG_CONTEXT_LABEL = "contract.skills.catalog"
SKILL_BODY_CONTEXT_PREFIX = "contract.skills.body."
SKILL_LOAD_TOOL_NAME = "skill__load"


@dataclass(frozen=True)
class SkillCatalogEntry:
    """The low-token card used to advertise one skill."""

    name: str
    description: str
    source: Literal["path", "inline", "mcp"]
    origin: str = ""


@dataclass(frozen=True)
class SkillDocument:
    """A parsed SKILL.md document with its untrusted body kept separate."""

    name: str
    description: str
    body: str
    source: Literal["path", "inline", "mcp"]
    origin: str = ""
    allowed_tools: frozenset[str] = frozenset()
    enable_scripts: bool = False
    frontmatter: Mapping[str, Any] = field(default_factory=dict)
    content_digest: str = ""


class SkillProvider(Protocol):
    async def catalog(self) -> Sequence[SkillCatalogEntry]: ...

    async def load(self, name: str) -> SkillDocument: ...


class SkillScriptPolicy(Protocol):
    def allows(self, doc: SkillDocument) -> bool: ...

    def reason(self, doc: SkillDocument) -> str: ...


class DenyAllScriptPolicy:
    """Conservative default: skills must never enable scripts implicitly."""

    def allows(self, doc: SkillDocument) -> bool:
        return False

    def reason(self, doc: SkillDocument) -> str:
        return f"skill {doc.name} 的脚本执行被默认 DenyAllScriptPolicy 拒绝"


class InlineSkillProvider:
    """Load inline SkillSpec content once and retain the parsed documents."""

    def __init__(self, specs: Sequence[SkillSpec]) -> None:
        self._specs = tuple(spec for spec in specs if spec.source == "inline")
        self._documents: dict[str, SkillDocument] | None = None

    async def catalog(self) -> Sequence[SkillCatalogEntry]:
        return [_catalog_entry(document) for document in (await self._load_all()).values()]

    async def load(self, name: str) -> SkillDocument:
        try:
            return (await self._load_all())[name]
        except KeyError:
            raise KeyError(f"skill {name!r} was not found") from None

    async def _load_all(self) -> dict[str, SkillDocument]:
        if self._documents is None:
            self._documents = {
                spec.name: parse_skill_markdown(
                    spec.content, source="inline", origin="inline", default_name=spec.name,
                )
                for spec in self._specs
            }
        return self._documents


class FilesystemSkillProvider:
    """Read ``SKILL.md`` from configured directories without failing construction."""

    def __init__(self, specs: Sequence[SkillSpec]) -> None:
        self._specs = tuple(spec for spec in specs if spec.source == "path")
        self._documents: dict[str, SkillDocument] | None = None
        self.problems: list[str] = []

    async def catalog(self) -> Sequence[SkillCatalogEntry]:
        return [_catalog_entry(document) for document in (await self._load_all()).values()]

    async def load(self, name: str) -> SkillDocument:
        try:
            return (await self._load_all())[name]
        except KeyError:
            raise KeyError(f"skill {name!r} was not found") from None

    async def _load_all(self) -> dict[str, SkillDocument]:
        if self._documents is not None:
            return self._documents
        documents: dict[str, SkillDocument] = {}
        for spec in self._specs:
            skill_file = Path(spec.path) / "SKILL.md"
            if not skill_file.is_file():
                self.problems.append(f"skill {spec.name} 的 SKILL.md 不存在: {skill_file}")
                continue
            document = parse_skill_markdown(
                skill_file.read_text(encoding="utf-8"),
                source="path",
                origin=f"path:{spec.path}",
                default_name=spec.name,
            )
            documents[spec.name] = document
        self._documents = documents
        return documents


class McpSkillProvider:
    """Receive MCP-discovered documents through a framework adapter supplied loader."""

    def __init__(self, loader, *, origin: str = "") -> None:
        self._loader = loader
        self._origin = origin
        self._documents: dict[str, SkillDocument] | None = None
        self.problems: list[str] = []

    async def catalog(self) -> Sequence[SkillCatalogEntry]:
        return [_catalog_entry(document) for document in (await self._load_all()).values()]

    async def load(self, name: str) -> SkillDocument:
        try:
            return (await self._load_all())[name]
        except KeyError:
            raise KeyError(f"skill {name!r} was not found") from None

    async def _load_all(self) -> dict[str, SkillDocument]:
        if self._documents is not None:
            return self._documents
        try:
            documents = await self._loader()
        except Exception as exc:
            self.problems.append(f"MCP skill 发现失败 ({self._origin}): {exc}")
            self._documents = {}
            return self._documents
        self._documents = {document.name: document for document in documents}
        return self._documents


class SkillRegistry:
    """Aggregate skill providers and reject duplicate names from different origins."""

    def __init__(self, providers: Sequence[SkillProvider]) -> None:
        self._providers = tuple(providers)
        self._documents: dict[str, SkillDocument] | None = None

    async def prepare(self) -> None:
        if self._documents is not None:
            return
        documents: dict[str, SkillDocument] = {}
        for provider in self._providers:
            for entry in await provider.catalog():
                document = await provider.load(entry.name)
                existing = documents.get(document.name)
                if existing is not None:
                    raise ValueError(
                        f"skill {document.name!r} 冲突: {existing.origin!r} 与 {document.origin!r}"
                    )
                documents[document.name] = document
        self._documents = documents

    async def catalog(self) -> Sequence[SkillCatalogEntry]:
        await self.prepare()
        return [_catalog_entry(document) for document in self._documents.values()]

    async def load(self, name: str) -> SkillDocument:
        await self.prepare()
        try:
            return self._documents[name]
        except KeyError:
            raise KeyError(f"skill {name!r} was not found") from None

    async def resolve(self, spec: SkillSpec) -> SkillDocument:
        document = await self.load(spec.name)
        if document.source != spec.source:
            raise KeyError(f"skill {spec.name!r} 的来源与声明不一致")
        return document


def _catalog_entry(document: SkillDocument) -> SkillCatalogEntry:
    return SkillCatalogEntry(document.name, document.description, document.source, document.origin)


def parse_skill_markdown(
    text: str,
    *,
    source: Literal["path", "inline", "mcp"],
    origin: str = "",
    default_name: str = "",
) -> SkillDocument:
    """Parse optional YAML frontmatter without making PyYAML mandatory."""
    frontmatter, body = _split_frontmatter(text)
    name = str(frontmatter.get("name") or default_name)
    description = str(frontmatter.get("description") or _first_body_line(body))
    allowed_tools = _as_frozenset(frontmatter.get("allowed_tools", frontmatter.get("allowed-tools", ())))
    enable_scripts = _as_bool(frontmatter.get("enable_scripts", frontmatter.get("enable-scripts", False)))
    return SkillDocument(
        name=name,
        description=description,
        body=body,
        source=source,
        origin=origin,
        allowed_tools=allowed_tools,
        enable_scripts=enable_scripts,
        frontmatter=frontmatter,
        content_digest=hashlib.sha256(body.encode("utf-8")).hexdigest(),
    )


def _split_frontmatter(text: str) -> tuple[Mapping[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    closing = text.find("\n---", 4)
    if closing < 0:
        return {}, text
    header = text[4:closing]
    body_start = closing + 4
    if body_start < len(text) and text[body_start] == "\n":
        body_start += 1
    return _parse_frontmatter(header), text[body_start:]


def _parse_frontmatter(header: str) -> Mapping[str, Any]:
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        return _parse_simple_frontmatter(header)
    parsed = yaml.safe_load(header)
    return parsed if isinstance(parsed, Mapping) else {}


def _parse_simple_frontmatter(header: str) -> Mapping[str, Any]:
    parsed: dict[str, Any] = {}
    for line in header.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            parsed[key.strip()] = [item.strip() for item in value[1:-1].split(",") if item.strip()]
        else:
            parsed[key.strip()] = value.strip("'\"")
    return parsed


def _first_body_line(body: str) -> str:
    return next((line.strip() for line in body.splitlines() if line.strip()), "")


def _as_frozenset(value: Any) -> frozenset[str]:
    if isinstance(value, str):
        return frozenset({value})
    if isinstance(value, Sequence):
        return frozenset(str(item) for item in value)
    return frozenset()


def _as_bool(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value.lower() == "true")