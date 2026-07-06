"""Mark quests complete in the multiplayer progress ledger, with dependency closure.

Usage:
  python tools/mark_quests.py "Pure Daisy" "The Nether"          # fuzzy title match
  python tools/mark_quests.py --chapter the_color_green "Grass"  # scope to a chapter
  python tools/mark_quests.py --id 27C68B6622F186D7              # exact ids

Marking a quest also marks everything it depends on (transitively) — you can't
have completed a quest whose prerequisites aren't complete.
"""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GRAPH = ROOT / "data" / "quest_graph.json"
LEDGER = ROOT / "data" / "progress_ledger.json"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("titles", nargs="*")
    ap.add_argument("--chapter", default=None, help="limit title matching to one chapter filename")
    ap.add_argument("--id", action="append", default=[], help="exact quest id (repeatable)")
    args = ap.parse_args()

    graph = json.loads(GRAPH.read_text(encoding="utf-8"))
    quests = {q["id"]: q for c in graph["chapters"] for q in c["quests"]}
    chapter_ids = {c["id"] for c in graph["chapters"]}
    done = set(json.loads(LEDGER.read_text(encoding="utf-8"))) if LEDGER.exists() else set()

    targets = [qid.upper() for qid in args.id]
    for title in args.titles:
        t = title.lower()
        matches = [q for q in quests.values()
                   if (not args.chapter or q["chapter"] == args.chapter)
                   and (t == q["title"].lower() or t in q["title"].lower())]
        if not matches:
            print(f"NO MATCH: {title!r}")
        elif len(matches) > 1:
            print(f"AMBIGUOUS: {title!r} -> " + "; ".join(
                f"{q['title']} ({q['chapter']}, {q['id']})" for q in matches))
        else:
            targets.append(matches[0]["id"])
            print(f"matched {title!r} -> {matches[0]['title']} ({matches[0]['id']})")

    added = set()
    stack = list(targets)
    while stack:
        qid = stack.pop()
        if qid in done or qid in chapter_ids or qid not in quests:
            continue
        done.add(qid)
        added.add(qid)
        stack.extend(quests[qid]["dependencies"])

    LEDGER.write_text(json.dumps(sorted(done), indent=1), encoding="utf-8")
    for qid in sorted(added):
        print(f"  + {quests[qid]['chapter']}: {quests[qid]['title']}")
    print(f"added {len(added)} (incl. dependencies); ledger total {len(done)}")


if __name__ == "__main__":
    main()
