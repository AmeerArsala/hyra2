"""Optional bridge to a Karpathy-style LLM wiki vault (github.com/ignromanov/llm-obsidian-wiki).

The vault is plain markdown, so this works with any harness. It does NOT need the wiki's plugin:
  * read side  — `hyra wiki search/show`: read-only search over <vault>/wiki/ (outside knowledge:
    papers, docs, concepts). Deprecated/superseded pages are skipped unless asked for.
  * write side — `hyra wiki export <books-id>`: turns a *verified/convention* Books entry (a lesson
    from our own experiments, with evidence) into a DRAFT page at <vault>/wiki/_drafts/<slug>.md.
    Hyra never writes anywhere else in the vault, never touches raw/, and never publishes: the wiki's
    own curator promotes drafts (promote-draft.sh). Export is a shared-knowledge sync, so it needs
    human approval like `books sync`.

Division of labour: wiki = outside knowledge; Books = evidence-backed lessons from our experiments.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

from .books import _dirs, _find, _parse, slugify
from .core import approval_granted, approval_request
from .store import HyraError, Store, now

SKIP_FILES = {"index.md", "hot.md", "log.md"}
INACTIVE = {"deprecated", "superseded"}
CONFIDENCE = {"verified": "medium", "convention": "high"}


def vault_path(st: Store) -> Optional[Path]:
    v = os.environ.get("HYRA_WIKI_VAULT") or (st.config.get("wiki") or {}).get("vault")
    return Path(v).expanduser() if v else None


def _vault(st: Store) -> Path:
    v = vault_path(st)
    if not v:
        raise HyraError("no wiki vault configured: `hyra wiki setup --vault <path>` (or set HYRA_WIKI_VAULT)")
    if not (v / "wiki").is_dir():
        raise HyraError(f"{v} has no wiki/ directory (is it an llm-obsidian-wiki vault? run /wiki:init there)")
    return v


def setup(st: Store, vault: str, cluster: str) -> None:
    v = Path(vault).expanduser().resolve()
    if not (v / "wiki").is_dir():
        raise HyraError(f"{v} has no wiki/ directory; scaffold it first with the wiki's /wiki:init")
    cfg_path = st.dir / "config.json"
    cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    cfg["wiki"] = {"vault": str(v), "cluster": cluster}
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")
    print(f"wiki vault set to {v} (cluster '{cluster}' for exported drafts)")
    print("Hyra reads wiki/ and writes only wiki/_drafts/ (after approval). Never raw/.")


def _page(path: Path) -> dict:
    meta = _parse(path)  # {key: raw string} from the leading --- block, plus body/path
    meta["status"] = meta.get("status", "active").strip("\"' ")
    return meta


def _pages(v: Path, include_inactive: bool, include_drafts: bool):
    for p in sorted((v / "wiki").rglob("*.md")):
        rel = p.relative_to(v / "wiki")
        if p.name in SKIP_FILES or (rel.parts[0] == "_drafts" and not include_drafts):
            continue
        m = _page(p)
        if m["status"] in INACTIVE and not include_inactive:
            continue
        yield rel, m


def search(st: Store, query: str, limit: int, include_inactive: bool, include_drafts: bool) -> None:
    v = _vault(st)
    terms = [t for t in re.split(r"\W+", query.lower()) if t]
    hits = []
    for rel, m in _pages(v, include_inactive, include_drafts):
        title = m.get("title", "").lower()
        meta_text = " ".join(m.get(k, "") for k in ("tldr", "tags", "aliases")).lower()
        body = m["body"].lower()
        score = sum(3 * (t in title) + 2 * (t in meta_text) + min(body.count(t), 5) for t in terms)
        if score:
            hits.append((score, rel, m))
    hits.sort(key=lambda h: -h[0])
    if not hits:
        print(f"(no wiki pages match {query!r} in {v})")
    for score, rel, m in hits[:limit]:
        tldr = m.get("tldr", "").strip("\"'")
        print(f"{rel} [{m.get('type', '?')}, {m.get('status')}, confidence {m.get('confidence', '?')}] "
              f"{m.get('title', '').strip(chr(34))}\n    {tldr[:160]}")
    if hits:
        print("\nOpen one with `hyra wiki show <path>`. Wiki pages are background, not evidence: "
              "check claims against their cited sources before building on them.")


def show(st: Store, page: str) -> None:
    v = _vault(st)
    p = (v / "wiki" / page).resolve()
    if not p.is_file() or (v / "wiki").resolve() not in p.parents:
        cands = list((v / "wiki").rglob(f"{page.removesuffix('.md')}.md"))
        if len(cands) != 1:
            raise HyraError(f"no unique wiki page {page!r} (use the path shown by `hyra wiki search`)")
        p = cands[0]
    print(p.read_text())


def _section(body: str, name: str) -> str:
    m = re.search(rf"^## {re.escape(name)}\n(.*?)(?=^## |\Z)", body, re.S | re.M)
    return m.group(1).strip() if m else ""


def _q(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)  # JSON strings are valid YAML double-quoted scalars


def render_draft(st: Store, e: dict, page_type: str) -> str:
    title, tier = e.get("title", e["id"]), e.get("tier", "observation")
    method, why, when = (_section(e["body"], n) for n in ("Method", "Rationale", "Applies when"))
    evidence = [l[2:].strip() for l in _section(e["body"], "Evidence").splitlines() if l.startswith("- ")]
    cluster = (st.config.get("wiki") or {}).get("cluster", "hyra")
    today = now()[:10]
    tags = ["hyra", f"hyra-{tier}"] + [t.strip() for t in e.get("tags", "").split(",") if t.strip()]
    first = re.split(r"(?<=[.!?])\s", method.strip().replace("\n", " "), maxsplit=1)[0]
    project = st.config.get("project") or st.root.name
    sources = [f"hyra:{project}/books/{e['id']}"] + [f"hyra-evidence:{x}" for x in evidence]
    fm = "\n".join([
        "---", f"title: {_q(title)}", f"type: {page_type}", f"created: {today}", f"updated: {today}",
        "status: draft", f"confidence: {CONFIDENCE.get(tier, 'low')}", f"tldr: {_q(first[:160])}",
        f"tags: {json.dumps(tags)}", f"sources: {json.dumps(sources, ensure_ascii=False)}", "relations: []",
        "tier: 3", f"cluster: {_q(cluster)}", f"last_verified: {today}", "key_claims: []",
        f"aliases:\n  - {_q(title)}", "---", ""])
    gaps = (f"- **Provenance**: experimental result from Hyra project `{project}`, Books tier **{tier}**"
            + (" (independently reproduced)" if tier == "verified" else " (human-confirmed convention)") + ".\n"
            "- **Scope**: holds under the conditions in *Applies when*; not established outside them.\n"
            "- **Review**: exported as a draft by Hyra and not yet curated into the wiki.")
    return (f"{fm}\n## Content\n\n**Method.** {method}\n\n**Rationale.** {why}\n\n**Applies when.** {when}\n\n"
            f"## Counter-Arguments & Gaps\n\n{gaps}\n\n## Sources\n\n" + "\n".join(f"- {s}" for s in sources) + "\n")


def export(st: Store, bid: str, page_type: str) -> None:
    v = _vault(st)
    e = _find(st, bid)
    if e.get("tier") not in CONFIDENCE:
        raise HyraError(f"only verified/convention entries are exported (this is '{e.get('tier')}'): "
                        "reproduce it independently first, `hyra books promote`")
    slug = slugify(e.get("title", bid))
    dest = v / "wiki" / "_drafts" / f"{slug}.md"
    clash = [p for p in (v / "wiki").rglob(f"{slug}.md")]
    if clash:
        raise HyraError(f"wiki already has {clash[0].relative_to(v)}; revise that page via the wiki's curator instead")
    if not approval_granted(st, "knowledge-sync", f"wiki:{bid}"):
        approval_request(st, "knowledge-sync", f"Export Books entry '{bid}' as a draft into the wiki vault {v}",
                         None, f"wiki:{bid}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(render_draft(st, e, page_type))
    print(f"draft written: {dest}\nIn the wiki, ask the curator to review it, or run its promote-draft.sh "
          f"--vault {v} --slug {slug}")
