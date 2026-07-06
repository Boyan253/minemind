"""Game-state model + quest-frontier computation.

State is a plain JSON dict (produced later by the in-game bridge):
{
  "completed_quests": ["73F3DDAFC46A9CD5", ...],
  "inventory": {"minecraft:dirt": 12, ...},
  "position": {"x": 0, "y": 64, "z": 0, "dimension": "minecraft:overworld"},
  "health": 20, "hunger": 20, "time_of_day": "day",
  "placed_infrastructure": ["minecraft:crafting_table", ...]
}
"""
import json
from pathlib import Path


def load_graph(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def quest_index(graph):
    return {q["id"]: q for c in graph["chapters"] for q in c["quests"]}


def frontier(graph, state, include_optional=False):
    """Quests whose dependencies are all completed but are not completed themselves."""
    done = set(state.get("completed_quests", []))
    chapter_ids = {c["id"] for c in graph["chapters"]}
    out = []
    for quest in quest_index(graph).values():
        if quest["id"] in done:
            continue
        if quest["optional"] and not include_optional:
            continue
        deps = [d for d in quest["dependencies"] if d not in chapter_ids]
        if all(d in done for d in deps):
            out.append(quest)
    return out


def fresh_state():
    return {
        "completed_quests": [],
        "inventory": {},
        "position": {"x": 0, "y": 64, "z": 0, "dimension": "minecraft:overworld"},
        "health": 20,
        "hunger": 20,
        "time_of_day": "day",
        "placed_infrastructure": [],
    }
