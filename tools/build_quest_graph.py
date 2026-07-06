"""Build quest_graph.json from the Reclamation pack's FTB Quests SNBT files.

Reads chapters/*.snbt + the ftbquestlocalizer lang file, resolves titles and
descriptions, flattens tasks/rewards/dependencies, and emits a single JSON
DAG the planner can consume. Also runs sanity checks (dangling deps, cycles).

Usage: python tools/build_quest_graph.py
"""
import json
import re
import sys
from collections import deque
from pathlib import Path

import snbt

ROOT = Path(__file__).resolve().parent.parent
QUESTS = ROOT / "pack/extracted/overrides/config/ftbquests/quests"
LANG = ROOT / "pack/extracted/overrides/kubejs/assets/ftbquestlocalizer/lang/en_us.json"
OUT = ROOT / "data/quest_graph.json"

FORMAT_CODES = re.compile(r"&[0-9a-fk-or]")
LANG_KEY = re.compile(r"^\{(.+)\}$")


def resolve_text(value, lang):
    """Resolve '{ftbquests...}' lang keys; strip &-format codes."""
    if not isinstance(value, str):
        return value
    m = LANG_KEY.match(value)
    if m:
        value = lang.get(m.group(1), value)
    return FORMAT_CODES.sub("", value)


def parse_task(task, lang):
    t = {"id": task.get("id"), "type": task.get("type", "item")}
    if "title" in task:
        t["title"] = resolve_text(task["title"], lang)
    item = task.get("item")
    if isinstance(item, dict):  # {id: ..., count: ..., tag: {...}}
        t["item"] = item.get("id")
        if item.get("tag"):
            t["item_nbt"] = item["tag"]
    elif item is not None:
        t["item"] = item
    for key in ("count", "advancement", "criterion", "dimension", "structure",
                "biome", "entity", "stat", "value", "fluid", "amount",
                "to_kill", "block", "force", "consume_items"):
        if key in task:
            t[key] = task[key]
    return t


def parse_reward(reward, lang):
    r = {"type": reward.get("type", "item")}
    item = reward.get("item")
    if isinstance(item, dict):
        r["item"] = item.get("id")
    elif item is not None:
        r["item"] = item
    for key in ("count", "xp", "xp_levels", "table_id", "command"):
        if key in reward:
            r[key] = reward[key]
    if "title" in reward:
        r["title"] = resolve_text(reward["title"], lang)
    return r


def quest_fallback_title(quest_parsed):
    """FTB Quests shows the first task's item name when a quest has no title."""
    tasks = quest_parsed.get("tasks", [])
    if tasks and tasks[0].get("item"):
        return tasks[0]["item"].split(":")[-1].replace("_", " ").title()
    if tasks and tasks[0].get("title"):
        return tasks[0]["title"]
    return "(untitled)"


def main():
    lang = json.loads(LANG.read_text(encoding="utf-8"))
    chapters_out = []
    all_ids = {}
    dep_edges = []

    chapter_files = sorted(QUESTS.glob("chapters/*.snbt"))
    if not chapter_files:
        sys.exit(f"no chapter files under {QUESTS}")

    for path in chapter_files:
        data = snbt.load(path)
        chapter = {
            "id": data.get("id"),
            "filename": data.get("filename", path.stem),
            "title": resolve_text(data.get("title", ""), lang)
                     or data.get("filename", path.stem).replace("_", " ").title(),
            "order_index": data.get("order_index", 0),
            "quests": [],
        }
        for q in data.get("quests", []):
            quest = {
                "id": q.get("id"),
                "chapter": chapter["filename"],
                "title": resolve_text(q.get("title", ""), lang),
                "description": [resolve_text(line, lang)
                                for line in q.get("description", []) if line],
                "dependencies": list(q.get("dependencies", [])),
                "tasks": [parse_task(t, lang) for t in q.get("tasks", [])],
                "rewards": [parse_reward(r, lang) for r in q.get("rewards", [])],
                "optional": bool(q.get("optional", False)),
            }
            if not quest["title"]:
                quest["title"] = quest_fallback_title(quest)
            chapter["quests"].append(quest)
            all_ids[quest["id"]] = quest
            for dep in quest["dependencies"]:
                dep_edges.append((dep, quest["id"]))
        chapters_out.append(chapter)

    chapters_out.sort(key=lambda c: c["order_index"])

    # sanity: dangling deps (deps may point at quests OR chapter ids)
    chapter_ids = {c["id"] for c in chapters_out}
    dangling = sorted({d for d, _ in dep_edges} - set(all_ids) - chapter_ids)

    # sanity: cycle check via Kahn's algorithm over quest->quest edges
    indeg = {qid: 0 for qid in all_ids}
    adj = {qid: [] for qid in all_ids}
    for dep, qid in dep_edges:
        if dep in all_ids:
            adj[dep].append(qid)
            indeg[qid] += 1
    queue = deque([q for q, d in indeg.items() if d == 0])
    seen = 0
    while queue:
        node = queue.popleft()
        seen += 1
        for nxt in adj[node]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
    has_cycle = seen != len(all_ids)

    graph = {
        "pack": "Reclamation 2.3.2",
        "minecraft": "1.20.1",
        "modloader": "forge-47.4.0",
        "chapters": chapters_out,
        "stats": {
            "chapters": len(chapters_out),
            "quests": len(all_ids),
            "dependency_edges": len(dep_edges),
            "root_quests": sum(1 for q in all_ids.values() if not q["dependencies"]),
            "dangling_dependencies": dangling,
            "has_cycle": has_cycle,
        },
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(graph, indent=1, ensure_ascii=False), encoding="utf-8")

    s = graph["stats"]
    print(f"wrote {OUT}")
    print(f"chapters={s['chapters']} quests={s['quests']} edges={s['dependency_edges']} "
          f"roots={s['root_quests']} cycle={s['has_cycle']}")
    if dangling:
        print(f"WARNING: {len(dangling)} dangling dependency ids: {dangling[:10]}")
    for c in chapters_out:
        print(f"  [{c['order_index']}] {c['title']} ({c['filename']}): {len(c['quests'])} quests")


if __name__ == "__main__":
    main()
