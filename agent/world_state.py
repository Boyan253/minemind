"""Read FTB Quests progress from the world save (SNBT).

FTB Quests writes per-team progress under <world>/serverdata/ftbquests/.
The exact file layout varies by FTB Quests version, so this reader walks every
.snbt file in that tree and collects quest ids from any "completed" mapping
(id -> completion timestamp). Run dump() once on a real save to verify the
schema; the daemon calls /save-all before reading so files are fresh.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import snbt

# Point AGENT_MC_DIR at your instance's minecraft/ (or .minecraft/) folder
INSTANCE = Path(os.environ.get("AGENT_MC_DIR", "."))


def find_world(name=None):
    saves = INSTANCE / "saves"
    if name:
        return saves / name
    worlds = sorted([d for d in saves.iterdir() if d.is_dir()],
                    key=lambda d: d.stat().st_mtime, reverse=True) if saves.exists() else []
    if not worlds:
        raise FileNotFoundError(f"no worlds in {saves} — create one in-game first (cheats ON)")
    return worlds[0]


def _walk_completed(node, found):
    if isinstance(node, dict):
        completed = node.get("completed")
        if isinstance(completed, dict):
            found.update(str(k).upper() for k in completed)
        for value in node.values():
            _walk_completed(value, found)
    elif isinstance(node, list):
        for value in node:
            _walk_completed(value, found)


def _quest_data_root(world_dir):
    """FTB Quests progress lives at <world>/ftbquests/ in singleplayer saves
    (verified on Reclamation 2.3.2); older/server layouts use serverdata/."""
    for rel in ("ftbquests", "serverdata/ftbquests"):
        root = Path(world_dir) / rel
        if root.exists():
            return root
    return Path(world_dir) / "ftbquests"


def completed_quests(world_dir):
    """Set of completed quest ids (uppercase hex, matches quest_graph.json)."""
    root = _quest_data_root(world_dir)
    found = set()
    if not root.exists():
        return found
    for path in root.rglob("*.snbt"):
        try:
            _walk_completed(snbt.load(path), found)
        except Exception as exc:
            print(f"[world_state] skipping {path.name}: {exc}")
    return found


def dump(world_dir):
    """Debug: show the ftbquests save-file tree and top-level keys of each file."""
    root = _quest_data_root(world_dir)
    if not root.exists():
        print(f"no ftbquests data at {root}")
        return
    for path in root.rglob("*.snbt"):
        try:
            data = snbt.load(path)
            keys = list(data)[:12] if isinstance(data, dict) else type(data).__name__
            print(f"{path.relative_to(root)}: {keys}")
        except Exception as exc:
            print(f"{path.relative_to(root)}: PARSE ERROR {exc}")


if __name__ == "__main__":
    world = find_world(sys.argv[1] if len(sys.argv) > 1 else None)
    print("world:", world)
    dump(world)
    done = completed_quests(world)
    print(f"{len(done)} completed quests:", sorted(done)[:20])
