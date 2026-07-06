"""Agent daemon — runs OUTSIDE the game. The in-game bridge connects to us.

Usage:
  python agent/daemon.py                     # LLM planning loop (needs GROQ_API_KEY or ANTHROPIC_API_KEY)
  python agent/daemon.py --plan data/sample_plan_session1.json   # run a fixed plan, no LLM
  python agent/daemon.py --probe             # connect, print bridge capabilities + state, exit

Start this first, then in Minecraft chat run:  \\agent_bridge
"""
import argparse
import json
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import state as state_mod
import world_state
from actions import Actions

ROOT = Path(__file__).resolve().parent.parent
HOST, PORT = "127.0.0.1", 48750
LOG_PATH = ROOT / "data" / "session_log.jsonl"
LEDGER_PATH = ROOT / "data" / "progress_ledger.json"


class Ledger:
    """Local completed-quest record for multiplayer, where the server's save
    files aren't readable. Hand-editable JSON — seed quests you finished before
    the agent existed by adding their ids."""

    def __init__(self):
        self.done = set()
        if LEDGER_PATH.exists():
            self.done = set(json.loads(LEDGER_PATH.read_text(encoding="utf-8")))

    def mark(self, qid):
        self.done.add(qid)
        LEDGER_PATH.write_text(json.dumps(sorted(self.done), indent=1), encoding="utf-8")


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": time.time(), "msg": msg}) + "\n")


class BridgeClient:
    """JSON-lines request/response to the in-game bridge, with auto-reconnect:
    if the game quits/switches worlds, we wait for the player to run
    \\agent_bridge again and retry the in-flight request."""

    def __init__(self):
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind((HOST, PORT))
        self.srv.listen(1)
        self.conn = None
        self.buf = b""
        self.next_id = 1
        self._accept()

    def _accept(self):
        log(f"waiting for in-game bridge on {HOST}:{PORT} — in Minecraft chat run: \\agent_bridge")
        self.conn, _ = self.srv.accept()
        self.buf = b""
        log("bridge connected")

    def call(self, op, **kwargs):
        req = {"id": self.next_id, "op": op, **kwargs}
        self.next_id += 1
        payload = json.dumps(req).encode() + b"\n"
        while True:  # survive any number of game restarts / world switches
            try:
                self.conn.sendall(payload)
                while b"\n" not in self.buf:
                    chunk = self.conn.recv(65536)
                    if not chunk:
                        raise ConnectionError("bridge closed")
                    self.buf += chunk
                line, self.buf = self.buf.split(b"\n", 1)
                resp = json.loads(line)
                if not resp.get("ok"):
                    log(f"bridge error on {op}: {resp.get('error')}")
                    return None
                return resp.get("result")
            except (ConnectionError, OSError):
                log("bridge disconnected (game closed / world switch) — reconnect with \\agent_bridge")
                self._accept()


def wait_for_bridge():
    return BridgeClient()


# set in main(): base_mp.json for the friend's server, base_sp.json for singleplayer
BASE_PATH = ROOT / "data" / "base_sp.json"


def load_base_summary():
    if not BASE_PATH.exists():
        return None
    base = json.loads(BASE_PATH.read_text(encoding="utf-8"))
    totals = {}
    for c in (base.get("containers") or {}).values():
        for item, count in c.get("items", {}).items():
            totals[item] = totals.get(item, 0) + count
    top = dict(sorted(totals.items(), key=lambda kv: -kv[1])[:80])
    return {"notes": base.get("notes", []),
            "storage_totals": top,
            "container_count": len(base.get("containers", {}))}


def build_game_state(bridge, world, completed):
    live = bridge.call("state") or {}
    state = {
        "completed_quests": sorted(completed),
        "inventory": live.get("inventory", {}),
        "position": live.get("position", {}),
        "health": live.get("health"),
        "day_ticks": live.get("day_ticks"),
        "world": world.name if world else "multiplayer",
    }
    base = load_base_summary()
    if base:
        state["base"] = base
    return state


def survey(bridge):
    """Learn the base: record every container the player opens until '.done'."""
    base = json.loads(BASE_PATH.read_text(encoding="utf-8")) if BASE_PATH.exists() \
        else {"notes": [], "containers": {}}
    containers = base.setdefault("containers", {})
    log("SURVEY: walk your base and open every chest/machine; type .done in chat to finish")
    bridge.call("echo", text="[agent] survey mode — open chests/machines one by one, type .done when finished")
    first = bridge.call("chat_log", since=0)
    since = first["latest"] if first else 0
    last_screen = None
    while True:
        live = bridge.call("state") or {}
        screen = live.get("open_screen")
        if screen != last_screen:
            log(f"screen changed: {last_screen!r} -> {screen!r}")
        if screen and "chat" not in screen.lower():
            items = None
            for _ in range(3):  # container contents sync from server with a delay
                items = bridge.call("container")
                if items:
                    break
                time.sleep(0.7)
            if items is None and screen != last_screen:
                log(f"  no container items readable on screen {screen!r}")
            if items:
                pos = live.get("position", {})
                # container GUIs include the player's own inventory — subtract it
                agg = {}
                for it in items:
                    agg[it["item"]] = agg.get(it["item"], 0) + it["count"]
                for item, count in (live.get("inventory") or {}).items():
                    bare = item.split(":")[-1]
                    if bare in agg:
                        agg[bare] = max(0, agg[bare] - count)
                agg = {k: v for k, v in agg.items() if v > 0}
                key = f"{round(pos.get('x', 0))},{round(pos.get('y', 0))},{round(pos.get('z', 0))}"
                entry = {"screen": screen, "position": pos, "items": agg}
                existing = containers.get(key)
                # a too-early read can see only the (subtracted) player inv -> {};
                # never replace a known-good entry with an emptier one
                if existing and len(agg) < len(existing.get("items", {})):
                    entry = existing
                if containers.get(key) != entry:
                    containers[key] = entry
                    log(f"recorded '{screen}' @ {key}: {len(agg)} item types")
                    bridge.call("echo", text=f"[agent] recorded ({len(agg)} item types) — next one")
        last_screen = screen
        logs = bridge.call("chat_log", since=since)
        if logs:
            since = logs["latest"]
            if any(".done" in e["message"].lower() and "[agent]" not in e["message"]
                   for e in logs["entries"]):
                break
        time.sleep(1)
    BASE_PATH.write_text(json.dumps(base, indent=1), encoding="utf-8")
    total_types = len({i for c in containers.values() for i in c["items"]})
    log(f"survey saved: {len(containers)} containers, {total_types} distinct item types -> {BASE_PATH}")
    bridge.call("echo", text=f"[agent] survey done: {len(containers)} containers recorded")


def run_plan(bridge, world, plan, actions, mark_complete=None):
    completed_steps = 0
    failed_quests = set()
    for step in plan.get("steps", []):
        desc = f"step {step.get('n')}: {step['action'].get('action')} {json.dumps(step['action'])[:100]}"
        log(desc)
        ok, note = actions.run(step)
        log(f"  -> {'OK' if ok else 'FAIL'}: {note}")
        if not ok:
            log("  retrying once...")
            ok, note = actions.run(step)
            log(f"  -> {'OK' if ok else 'FAIL'}: {note}")
        if not ok:
            failed_quests.add(step.get("for_quest"))
            return completed_steps, step
        completed_steps += 1
    # item quests complete automatically once items are held, but the save file
    # lags (autosave) — record fully-executed targets so the frontier advances
    if mark_complete:
        for target in plan.get("target_quests", []):
            if target["id"] not in failed_quests:
                mark_complete(target["id"])
    return completed_steps, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", help="fixed plan JSON instead of LLM planning")
    ap.add_argument("--probe", action="store_true", help="print capabilities and exit")
    ap.add_argument("--world", default=None, help="world folder name (default: most recent)")
    ap.add_argument("--mp", action="store_true",
                    help="multiplayer server: track progress in a local ledger "
                         "instead of reading save files")
    ap.add_argument("--survey", action="store_true",
                    help="learn the base: record every container the player opens")
    ap.add_argument("--base", default=None, help="base knowledge file (default: "
                    "data/base_mp.json with --mp, data/base_sp.json otherwise)")
    ap.add_argument("--max-cycles", type=int, default=10)
    args = ap.parse_args()

    global BASE_PATH
    BASE_PATH = Path(args.base) if args.base else \
        ROOT / "data" / ("base_mp.json" if args.mp else "base_sp.json")

    bridge = wait_for_bridge()
    caps = bridge.call("probe")
    missing = [k for k, v in (caps or {}).items() if not v]
    log(f"bridge capabilities OK; missing: {missing or 'none'}")

    if args.survey:
        survey(bridge)
        return

    if args.probe:
        print(json.dumps(bridge.call("state"), indent=1))
        if not args.mp:
            world = world_state.find_world(args.world)
            world_state.dump(world)
        return

    if args.mp:
        ledger = Ledger()
        world = None
        get_completed = lambda: set(ledger.done)
        mark_complete = ledger.mark
        log(f"multiplayer mode: local ledger has {len(ledger.done)} completed quests "
            f"(seed {LEDGER_PATH.name} with quests you finished before the agent)")
    else:
        world = world_state.find_world(args.world)
        log(f"world: {world.name}")
        # save files lag behind reality (autosave only) — union them with an
        # in-session record of what the agent completed this run
        session_done = set()
        get_completed = lambda: world_state.completed_quests(world) | session_done
        mark_complete = session_done.add

    completed = get_completed()
    log(f"{len(completed)} quests already complete")

    actions = Actions(bridge, get_completed, log, mp=args.mp, mark_complete=mark_complete)
    bridge.call("echo", text="[agent] online — starting work")

    # make sure a PlayerEngine companion exists in THIS world
    if actions.agent_state(timeout=6) is None:
        log("no companion in this world — spawning Bob")
        bridge.call("chat", text="/agent spawn Bob")
        time.sleep(3)

    if args.plan:
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        done, failed = run_plan(bridge, world, plan, actions, mark_complete)
        log(f"fixed plan finished: {done} steps done, failed at: {failed and failed.get('n')}")
        return

    # LLM planning loop
    import llm
    import planner
    graph = state_mod.load_graph(ROOT / "data" / "quest_graph.json")
    quest_index = state_mod.quest_index(graph)

    def reconcile_item_quests():
        """FTB item-task auto-detection only fires on inventory CHANGE — items
        delivered before a quest's dependencies complete never trigger it.
        Force-complete any unlocked quest whose (non-consume) item tasks are
        all satisfied by the player's current inventory."""
        done = get_completed()
        inv = {k.split(":")[-1]: v for k, v in
               ((bridge.call("state") or {}).get("inventory") or {}).items()}
        chapter_ids = {c["id"] for c in graph["chapters"]}
        for quest in quest_index.values():
            if quest["id"] in done or quest["optional"]:
                continue
            deps = [d for d in quest["dependencies"] if d not in chapter_ids]
            if not all(d in done for d in deps):
                continue
            tasks = quest["tasks"]
            if not tasks or not all(t.get("type") == "item" and t.get("item")
                                    and not t.get("consume_items") for t in tasks):
                continue
            if all(inv.get(str(t["item"]).split(":")[-1], 0) >= int(t.get("count", 1))
                   for t in tasks):
                log(f"reconcile: completing '{quest['title']}' (items already in inventory)")
                actions.quest_checkmark({"quest_id": quest["id"]})
    fail_counts = {}
    blocked = set()
    for cycle in range(args.max_cycles):
        reconcile_item_quests()
        completed = get_completed() & set(quest_index)  # drop task ids from the count
        game_state = build_game_state(bridge, world, completed)
        frontier = [q for q in state_mod.frontier(graph, game_state) if q["id"] not in blocked]
        if not frontier:
            log("frontier empty — all reachable quests complete (or blocked)!")
            break
        log(f"cycle {cycle + 1}: {len(completed)} done, {len(frontier)} on frontier; planning via {llm.provider()}")
        order = {c["filename"]: c["order_index"] for c in graph["chapters"]}
        frontier.sort(key=lambda q: (order.get(q["chapter"], 99), q["title"]))
        prompt = planner.build_planner_prompt(planner.chapter_summary(graph), frontier, game_state)
        # planning can transiently fail (LLM rate limit, malformed JSON) — retry,
        # then skip this cycle rather than crashing the whole session
        plan = None
        for attempt in range(3):
            try:
                plan = planner.extract_json(llm.complete(planner.PLANNER_SYSTEM, prompt))
                break
            except Exception as exc:
                log(f"planning attempt {attempt + 1} failed: {exc}")
                time.sleep(10)
        if plan is None:
            log("planning failed 3x — waiting 60s before next cycle")
            bridge.call("echo", text="[agent] planner unavailable, will retry")
            time.sleep(60)
            continue
        targets = ", ".join(t["title"] for t in plan.get("target_quests", [])[:5])
        log(f"plan: {len(plan.get('steps', []))} steps targeting: {targets}")
        done, failed = run_plan(bridge, world, plan, actions, mark_complete)
        if failed:
            qid = failed.get("for_quest")
            fail_counts[qid] = fail_counts.get(qid, 0) + 1
            if qid and fail_counts[qid] >= 2:
                blocked.add(qid)
                log(f"quest {qid} failed {fail_counts[qid]} plans — blocking it this session")
                bridge.call("echo", text=f"[agent] giving up on quest {qid} for now — "
                                         f"may need your help with it later")
            log(f"replanning after failure at step {failed.get('n')}")
    bridge.call("echo", text="[agent] session over")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("stopped by user")
