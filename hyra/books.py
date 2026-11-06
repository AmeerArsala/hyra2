"""Books: a shared knowledge base with evidence-backed, tiered entries; plus distilled skills.

Tiers (from the Hyra 2.0 design):
  observation — a phenomenon seen in a single experiment; recorded for future reference.
  verified    — independently reproduced; only these count as reliable evidence.
  convention  — affects team/agent behaviour; requires human confirmation (approval).

Entries live in the project (.hyra/books) until synced to the global library
(~/.hyra/library/books), which is a shared-knowledge sync and therefore requires approval.
Skills are distilled from entries during retrospectives (.hyra/skills/<name>/SKILL.md) and
published to the library and linked into every installed harness's skills dir, only with approval.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Optional

from .core import approval_granted, approval_request, me
from .store import HyraError, Store, library_root, now

TIERS = ["observation", "verified", "convention"]


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60] or "entry"


def _parse(path: Path) -> dict:
    text = path.read_text()
    meta, body = {}, text
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end > 0:
            for line in text[4:end].splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
            body = text[end + 5:]
    meta["body"] = body
    meta["path"] = str(path)
    return meta


def _write(path: Path, meta: dict, body: str) -> None:
    head = "\n".join(f"{k}: {v}" for k, v in meta.items() if k not in ("body", "path"))
    path.write_text(f"---\n{head}\n---\n{body}")


def _dirs(st: Optional[Store]) -> list[tuple[str, Path]]:
    out = []
    if st:
        out.append(("project", st.dir / "books"))
    out.append(("library", library_root() / "books"))
    return out


def _find(st: Optional[Store], bid: str) -> dict:
    for scope, d in _dirs(st):
        p = d / f"{bid}.md"
        if p.exists():
            e = _parse(p)
            e["scope"] = scope
            return e
    raise HyraError(f"no Books entry {bid!r}")


def books_add(st: Store, title: str, tier: str, method: str, rationale: str, applies_when: str,
              evidence: list[str], tags: str) -> None:
    if tier == "verified":
        raise HyraError("new entries start as 'observation'; use `hyra books promote` with independent "
                        "reproduction evidence to reach 'verified'")
    if not evidence:
        raise HyraError("Books entries must attach the original evidence (--evidence T12, a path, commit:REV, ...)")
    if not (method and rationale and applies_when):
        raise HyraError("entries must explain the method, the rationale, and when it applies")
    bid = f"{slugify(title)}"
    path = st.dir / "books" / f"{bid}.md"
    if path.exists() or (library_root() / "books" / f"{bid}.md").exists():
        raise HyraError(f"entry {bid} exists; revise it with `hyra books revise {bid}`")
    meta = {"id": bid, "title": title, "tier": tier if tier != "convention" else "observation",
            "tags": tags, "author": me(), "created": now(), "updated": now()}
    body = (f"\n# {title}\n\n## Method\n{method}\n\n## Rationale\n{rationale}\n\n## Applies when\n{applies_when}\n\n"
            f"## Evidence\n" + "".join(f"- {e}\n" for e in evidence) + f"\n## History\n- {now()} created by {me()} as observation\n")
    _write(path, meta, body)
    print(f"Books entry {bid} added as observation ({path})")
    if tier == "convention":
        approval_request(st, "convention", f"Adopt Books entry '{bid}' as a team convention: {title}", None, bid)


def books_promote(st: Store, bid: str, to: str, evidence: list[str], note: str) -> None:
    e = _find(st, bid)
    if to == "verified":
        if not evidence:
            raise HyraError("promotion to verified needs independent reproduction evidence (a different run/task "
                            "than the original), e.g. --evidence T31")
        orig = e["body"].split("## Evidence", 1)[-1].split("## History", 1)[0]
        if all(ev in orig for ev in evidence):
            raise HyraError("that evidence is the original evidence; reproduction must be independent")
    elif to == "convention":
        if not approval_granted(st, "convention", bid):
            approval_request(st, "convention", f"Adopt Books entry '{bid}' as a team convention: {e.get('title')}", None, bid)
            return
    e["tier"], e["updated"] = to, now()
    body = e["body"].rstrip() + f"\n- {now()} promoted to {to} by {me()}: {note} " + " ".join(evidence) + "\n"
    if evidence:
        body = body.replace("## History", "".join(f"- {ev} (reproduction)\n" for ev in evidence) + "\n## History", 1)
    _write(Path(e["path"]), e, body)
    print(f"{bid} is now {to}")


def books_revise(st: Store, bid: str, note: str, evidence: list[str], demote: bool) -> None:
    """Existing entries are revised as new evidence emerges; contradicting evidence demotes."""
    e = _find(st, bid)
    if demote:
        e["tier"] = "observation"
    e["updated"] = now()
    body = e["body"].rstrip() + f"\n- {now()} revised by {me()}{' (demoted to observation)' if demote else ''}: {note} " \
        + " ".join(evidence) + "\n"
    _write(Path(e["path"]), e, body)
    print(f"{bid} revised")


def books_search(st: Optional[Store], query: str, tier: Optional[str], limit: int) -> None:
    terms = [t for t in re.split(r"\W+", query.lower()) if t]
    hits = []
    for scope, d in _dirs(st):
        if not d.is_dir():
            continue
        for p in d.glob("*.md"):
            e = _parse(p)
            if tier and e.get("tier") != tier:
                continue
            hay = (e.get("title", "") + " " + e.get("tags", "") + " " + e["body"]).lower()
            score = sum(hay.count(t) for t in terms) + 3 * sum(t in e.get("title", "").lower() for t in terms)
            if score or not terms:
                hits.append((score, scope, e))
    hits.sort(key=lambda h: (-h[0], {"verified": 0, "convention": 1, "observation": 2}.get(h[2].get("tier"), 3)))
    if not hits:
        print("(no matching Books entries)")
    for score, scope, e in hits[:limit]:
        aw = e["body"].split("## Applies when", 1)[-1].split("##", 1)[0].strip() if "## Applies when" in e["body"] else ""
        print(f"[{e.get('tier')}] {e.get('id')} ({scope}): {e.get('title')}\n    applies when: {aw[:160]}")


def books_show(st: Optional[Store], bid: str) -> None:
    e = _find(st, bid)
    print(f"(tier: {e.get('tier')}, scope: {e['scope']}, path: {e['path']})")
    print(e["body"])


def books_sync(st: Store, bid: str) -> None:
    """Publishing project knowledge to the shared library requires human approval."""
    src = st.dir / "books" / f"{bid}.md"
    if not src.exists():
        raise HyraError(f"no project entry {bid}")
    if not approval_granted(st, "knowledge-sync", f"book:{bid}"):
        approval_request(st, "knowledge-sync", f"Sync Books entry '{bid}' to the shared library", None, f"book:{bid}")
        return
    dest = library_root() / "books" / f"{bid}.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    print(f"synced {bid} -> {dest}")


# ---------------------------------------------------------------- skills
def skill_new(st: Store, name: str, description: str, from_books: list[str]) -> None:
    name = slugify(name)
    d = st.dir / "skills" / name
    if d.exists():
        raise HyraError(f"skill {name} exists at {d}")
    d.mkdir(parents=True)
    sources = ""
    for bid in from_books:
        e = _find(st, bid)
        sources += f"\n### From Books: {bid} [{e.get('tier')}]\n{e['body'].split('## History', 1)[0].strip()}\n"
    (d / "SKILL.md").write_text(f"""---
name: {name}
description: {description}
---

# {name}

<!-- Distilled by Hyra from project '{st.config.get('project') or st.root.name}' on {now()[:10]}.
     Rewrite the draft below into a procedure another agent can follow cold. Keep it short.
     Every rule must say WHEN it applies and cite the evidence (Books ids / task ids). -->

## When to use

## Procedure
1.

## Checks / pitfalls

## Evidence & provenance
{sources or '- (cite Books entries / tasks)'}
## Revision log
- {now()[:10]} created by {me()}
""")
    print(f"skill draft at {d/'SKILL.md'} — edit it, then `hyra skill publish {name}` (needs approval)")


def skill_list(st: Optional[Store]) -> None:
    for scope, d in ([("project", st.dir / "skills")] if st else []) + [("library", library_root() / "skills")]:
        if d.is_dir():
            for p in sorted(d.iterdir()):
                if (p / "SKILL.md").exists():
                    desc = _parse(p / "SKILL.md").get("description", "")
                    print(f"{p.name} ({scope}): {desc[:120]}")


def skill_publish(st: Store, name: str) -> None:
    src = st.dir / "skills" / name
    if not (src / "SKILL.md").exists():
        raise HyraError(f"no project skill {name}")
    if "\n1.\n" in (src / "SKILL.md").read_text():
        raise HyraError("skill still contains the empty template procedure; finish writing it first")
    if not approval_granted(st, "knowledge-sync", f"skill:{name}"):
        approval_request(st, "knowledge-sync", f"Publish skill '{name}' to the library and all agent harnesses", None, f"skill:{name}")
        return
    dest = library_root() / "skills" / name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    from .install import link_skill
    print(f"published {name} -> {dest}")
    print("\n".join(link_skill(dest, name)))
