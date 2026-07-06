"""LLM planner: quest graph + game state -> ordered action plan (JSON).

Usage:
  python agent/planner.py                            # fresh state, auto frontier
  python agent/planner.py --state data/state.json --out data/plan.json

Backend: Groq free tier (GROQ_API_KEY) or Anthropic (ANTHROPIC_API_KEY) — see agent/llm.py.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import llm
import state as state_mod
from prompts import PLANNER_SYSTEM, build_planner_prompt

ROOT = Path(__file__).resolve().parent.parent


def chapter_summary(graph):
    lines = []
    for c in graph["chapters"]:
        done_hint = f"{len(c['quests'])} quests"
        lines.append(f"- [{c['order_index']}] {c['title']} ({c['filename']}): {done_hint}")
    return "\n".join(lines)


def extract_json(text):
    """Pull the first JSON object out of a model response."""
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object in response")
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("unbalanced JSON in response")


def plan(graph_path, state_path=None, out_path=None, max_quests=12):
    graph = state_mod.load_graph(graph_path)
    game_state = (json.loads(Path(state_path).read_text(encoding="utf-8"))
                  if state_path else state_mod.fresh_state())
    frontier = state_mod.frontier(graph, game_state)
    if not frontier:
        print("frontier empty — all reachable quests complete")
        return None

    # stable order: chapter order, then title, keeps prompts cacheable
    order = {c["filename"]: c["order_index"] for c in graph["chapters"]}
    frontier.sort(key=lambda q: (order.get(q["chapter"], 99), q["title"]))

    prompt = build_planner_prompt(chapter_summary(graph), frontier, game_state, max_quests)

    print(f"planning via {llm.provider()}...")
    result = extract_json(llm.complete(PLANNER_SYSTEM, prompt))

    if out_path:
        Path(out_path).write_text(json.dumps(result, indent=1, ensure_ascii=False),
                                  encoding="utf-8")
        print(f"wrote {out_path}")
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--graph", default=str(ROOT / "data/quest_graph.json"))
    ap.add_argument("--state", default=None, help="game state JSON (default: fresh spawn)")
    ap.add_argument("--out", default=str(ROOT / "data/plan.json"))
    ap.add_argument("--max-quests", type=int, default=12)
    args = ap.parse_args()
    result = plan(args.graph, args.state, args.out, args.max_quests)
    if result:
        print(json.dumps(result, indent=1, ensure_ascii=False)[:2000])


if __name__ == "__main__":
    main()
