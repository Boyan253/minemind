"""Action executors — translate planner steps into bridge ops and verify results.

M2 implements: quest_checkmark, goto, goto_block, mine, collect (as mine-adjacent
pickup wait), wait_for, chat, manual. Crafting/smelting/GUI actions are M3+ and
report back as unsupported so the daemon escalates instead of pretending.

Every executor returns (ok: bool, note: str). Verification is state-based
(inventory deltas, position, quest progress), not trust-based.
"""
import json
import time

# verified against FTB docs + live run: change_progress is ONE word (underscore)
CHECKMARK_CMD = "/ftbquests change_progress @s complete {quest_id}"
CMD_ERRORS = ("permission", "unknown command", "no such", "incorrect argument",
              "usage:", "expected", "<--[here]")


class Actions:
    def __init__(self, bridge, get_completed_quests, log, mp=False, mark_complete=None,
                 body="companion"):
        self.bridge = bridge          # BridgeClient (daemon.py)
        self.get_completed = get_completed_quests
        self.log = log
        self.mp = mp                  # multiplayer: no save files, maybe no OP
        self.body = body              # "companion" (PlayerEngine entity) | "player" (own body)
        self.mark_complete = mark_complete or (lambda qid: None)

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _bare(item_id):
        # Minescript reports item ids without namespace ("cobblestone", not
        # "minecraft:cobblestone") — compare on the bare name everywhere
        return str(item_id).split(":")[-1]

    def _inventory(self):
        raw = (self.bridge.call("state") or {}).get("inventory", {})
        out = {}
        for k, v in raw.items():
            out[self._bare(k)] = out.get(self._bare(k), 0) + v
        return out

    def _position(self):
        return (self.bridge.call("state") or {}).get("position")

    def _wait(self, predicate, timeout, poll=2.0, desc=""):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(poll)
        self.log(f"timeout waiting for {desc} ({timeout}s)")
        return False

    def _wait_for_player_signal(self, timeout=900):
        """Player confirms a manual step by typing '.done' in Minecraft chat."""
        first = self.bridge.call("chat_log", since=0)
        since = first["latest"] if first else 0

        def check():
            nonlocal since
            logs = self.bridge.call("chat_log", since=since)
            if not logs:
                return False
            since = logs["latest"]
            # ignore our own "[agent]" prompts — they mention '.done' and the chat
            # listener captures echoes, so without this the wait confirms itself
            return any(".done" in e["message"].lower() and "[agent]" not in e["message"]
                       for e in logs["entries"])
        return self._wait(check, timeout, poll=2, desc="player to type .done in chat")

    def _baritone_idle(self, timeout):
        """Baritone reports completion/failure in chat; watch for its messages."""
        marker = {"since": 0}
        first = self.bridge.call("chat_log", since=0)
        marker["since"] = first["latest"] if first else 0

        def check():
            logs = self.bridge.call("chat_log", since=marker["since"])
            if not logs:
                return False
            marker["since"] = logs["latest"]
            for entry in logs["entries"]:
                msg = entry["message"]
                if "[Baritone]" in msg and any(w in msg for w in
                        ("Done", "Finished", "canceled", "Cancelled", "No path", "Unable")):
                    self.log(f"baritone: {msg.strip()[:120]}")
                    return True
            return False
        return self._wait(check, timeout, desc="baritone completion")

    # -- executors ----------------------------------------------------------

    def quest_checkmark(self, step):
        """/save-all is unavailable in singleplayer and save files only flush on
        autosave, so verification is chat-response based in BOTH modes; the
        session/ledger record keeps the frontier moving until files catch up."""
        qid = step["quest_id"]
        before = self.bridge.call("chat_log", since=0)
        since = before["latest"] if before else 0
        self.bridge.call("chat", text=CHECKMARK_CMD.format(quest_id=qid))
        time.sleep(2)
        logs = self.bridge.call("chat_log", since=since) or {"entries": []}
        chat = " | ".join(e["message"] for e in logs["entries"])
        if any(w in chat.lower() for w in CMD_ERRORS):
            if self.mp:
                self.bridge.call("echo", text=f"[agent] no OP for /ftbquests — click quest "
                                              f"{qid} in the book, then type .done in chat")
                if not self._wait_for_player_signal():
                    return False, f"no OP and player never confirmed quest {qid}"
                self.mark_complete(qid)
                return True, "completed manually (no OP on server)"
            return False, f"/ftbquests rejected: {chat[:150]}"
        self.mark_complete(qid)
        self.bridge.call("screenshot")  # capture the moment for the session gallery
        return True, "command accepted (recorded; save file confirms on next autosave)"

    def goto(self, step):
        x, y, z = step["x"], step["y"], step["z"]
        self.bridge.call("chat", text=f"#goto {int(x)} {int(y)} {int(z)}")
        ok = self._baritone_idle(timeout=step.get("timeout", 300))
        pos = self._position()
        if pos and abs(pos["x"] - x) <= 4 and abs(pos["z"] - z) <= 4:
            return True, f"arrived at {pos}"
        return ok, f"ended at {pos}"

    def goto_block(self, step):
        block = step["block"]  # full namespaced id — Baritone needs it for modded blocks
        self.bridge.call("chat", text=f"#goto {block}")
        ok = self._baritone_idle(timeout=step.get("timeout", 420))
        return ok, f"pathed toward nearest {block}"

    def mine(self, step):
        block = step["block"]
        count = int(step.get("count", 8))
        # count the item form of the block; modded drops may differ — daemon replans if so
        want_item = self._bare(step.get("collect_item", step["block"]))
        before = self._inventory().get(want_item, 0)
        self.bridge.call("chat", text=f"#mine {count} {block}")
        target = before + count

        def enough():
            return self._inventory().get(want_item, 0) >= target
        ok = self._wait(enough, timeout=step.get("timeout", 600), poll=5, desc=f"mine {block}")
        self.bridge.call("chat", text="#stop")
        have = self._inventory().get(want_item, 0)
        return have > before, f"{want_item}: {before} -> {have} (wanted {target})"

    def collect(self, step):
        item, count = self._bare(step["item"]), int(step.get("count", 1))

        def enough():
            return self._inventory().get(item, 0) >= count
        if enough():
            return True, "already in inventory"
        self.bridge.call("echo", text=f"[agent] waiting for {count}x {item} in YOUR inventory — "
                                      f"if it's a quest reward, claim it in the book")
        ok = self._wait(enough, timeout=step.get("timeout", 180), poll=3, desc=f"collect {item}")
        return ok, f"{item} x{self._inventory().get(item, 0)}"

    # -- M3: hands ------------------------------------------------------------

    def _tap(self, control, seconds=0.25):
        self.bridge.call("press", control=control, pressed=True)
        time.sleep(seconds)
        self.bridge.call("press", control=control, pressed=False)

    def _select_item(self, item_id):
        """Put the item in hand: pick from hotbar, or move it to the hotbar."""
        want = self._bare(item_id)
        slots = self.bridge.call("inventory_slots") or []
        match = next((s for s in slots if self._bare(s["item"]) == want), None)
        if not match:
            return False
        if match["slot"] <= 8:
            self.bridge.call("select_slot", slot=match["slot"])
        else:
            self.bridge.call("slot_to_hotbar", slot=match["slot"])
        time.sleep(0.4)
        hand = self.bridge.call("hand")
        return bool(hand and self._bare(hand.get("item", "")) == want)

    def place(self, step):
        item = step["item"]
        where = step.get("at") or step.get("where")
        if not isinstance(where, dict):  # descriptive location -> human decides spot
            return self.manual({"reason": f"place {item} at '{where}' (no coordinates given)"})
        if not self._select_item(item):
            return False, f"{item} not in inventory"
        x, y, z = where["x"], where["y"], where["z"]
        # aim at the top face of the block below the target cell
        self.bridge.call("look_at", x=x + 0.5, y=y - 0.05, z=z + 0.5)
        time.sleep(0.3)
        self._tap("use")
        time.sleep(0.5)
        placed = self.bridge.call("getblock", x=int(x), y=int(y), z=int(z)) or ""
        ok = self._bare(item).replace("_block", "") in self._bare(placed.split("[")[0])
        return (True, f"placed at {x},{y},{z}") if ok else \
               (False, f"block at target is {placed!r} — placement failed")

    def use_item_on(self, step):
        if not self._select_item(step["item"]):
            return False, f"{step['item']} not in inventory"
        target = step.get("at")
        if isinstance(target, dict):
            self.bridge.call("look_at", x=target["x"] + 0.5, y=target["y"] + 0.5, z=target["z"] + 0.5)
            time.sleep(0.3)
        else:
            tb = self.bridge.call("targeted_block") or {}
            if self._bare(step.get("target", "")) not in self._bare(tb.get("block", "")):
                return self.manual({"reason": f"use {step['item']} on {step.get('target')} — "
                                              f"can't locate the target block, look at it for me"})
        self._tap("use")
        return True, "used"

    def interact(self, step):
        target = step.get("at")
        if isinstance(target, dict):
            self.bridge.call("look_at", x=target["x"] + 0.5, y=target["y"] + 0.5, z=target["z"] + 0.5)
            time.sleep(0.3)
        else:
            tb = self.bridge.call("targeted_block") or {}
            if self._bare(step.get("block", "")) not in self._bare(tb.get("block", "")):
                return self.manual({"reason": f"interact with {step.get('block')} — "
                                              f"walk me to it or do it yourself"})
        before = (self.bridge.call("state") or {}).get("open_screen")
        self._tap("use")
        time.sleep(0.8)
        after = (self.bridge.call("state") or {}).get("open_screen")
        if step.get("gui"):
            return (after is not None and after != before), f"screen: {after!r}"
        return True, "interacted"

    def kill(self, step):
        want = self._bare(step["entity"])
        need = int(step.get("count", 1))
        killed = 0
        deadline = time.time() + step.get("timeout", 420)
        while killed < need and time.time() < deadline:
            ents = self.bridge.call("entities") or []
            pos = self._position() or {}
            def dist(e):
                p = e.get("position") or [1e9, 1e9, 1e9]
                return ((p[0] - pos.get("x", 0)) ** 2 + (p[2] - pos.get("z", 0)) ** 2) ** 0.5
            targets = [e for e in ents
                       if want in self._bare(e.get("type", "")) or want in e.get("name", "").lower()]
            if not targets:
                self.log(f"no {want} nearby — waiting")
                time.sleep(8)
                continue
            target = min(targets, key=dist)
            tp = target["position"]
            if dist(target) > 3.5:
                self.bridge.call("chat", text=f"#goto {int(tp[0])} {int(tp[1])} {int(tp[2])}")
                time.sleep(4)
                self.bridge.call("chat", text="#stop")
                continue
            self.bridge.call("look_at", x=tp[0], y=tp[1] + 1.2, z=tp[2])
            time.sleep(0.15)
            self._tap("attack", 0.1)
            time.sleep(0.6)
            still = [e for e in (self.bridge.call("entities") or []) if e.get("uuid") == target.get("uuid")]
            if not still or (still[0].get("health") or 1) <= 0:
                killed += 1
                self.log(f"killed {want} ({killed}/{need})")
        return killed >= need, f"killed {killed}/{need} {want}"

    def craft(self, step):
        """Verified collaboration until GUI crafting lands: ask the human, then
        confirm the item actually exists in inventory before moving on."""
        item, count = self._bare(step["item"]), int(step.get("count", 1))
        if self._inventory().get(item, 0) >= count:
            return True, "already in inventory"
        for attempt in (1, 2):
            self.bridge.call("echo", text=f"[agent] please CRAFT {count}x {item}, then type .done")
            self.log(f"CRAFT request: {count}x {item} (attempt {attempt})")
            if not self._wait_for_player_signal():
                return False, f"no confirmation for crafting {item}"
            have = self._inventory().get(item, 0)
            if have >= count:
                return True, f"crafted (verified {have} in inventory)"
            self.bridge.call("echo", text=f"[agent] I only see {have}/{count} {item} — really crafted?")
        return False, f"{item} still missing after 2 confirmations"

    def smelt(self, step):
        item, count = self._bare(step["item"]), int(step.get("count", 1))
        out = self._bare(step.get("output", step["item"]))
        self.bridge.call("echo", text=f"[agent] please SMELT {count}x {item}, then type .done")
        if not self._wait_for_player_signal():
            return False, "no confirmation for smelting"
        return True, f"smelt confirmed ({self._inventory().get(out, 0)} {out} in inventory)"

    # -- PlayerEngine agent companion (agentlink mod) -------------------------

    def agent_state(self, timeout=10):
        """Query the companion via /agent state; parse the [agentstate] JSON line."""
        first = self.bridge.call("chat_log", since=0)
        since = first["latest"] if first else 0
        self.bridge.call("chat", text="/agent state")
        deadline = time.time() + timeout
        while time.time() < deadline:
            logs = self.bridge.call("chat_log", since=since) or {"entries": []}
            if logs.get("latest"):
                since = logs["latest"]
            for entry in logs.get("entries", []):
                msg = entry["message"]
                if "[agentstate]" in msg:
                    try:
                        return json.loads(msg.split("[agentstate]", 1)[1].strip())
                    except (ValueError, IndexError):
                        pass
            time.sleep(1.5)
        return None

    def _agent_inv(self, item):
        state = self.agent_state()
        if not state or not state.get("inventory"):
            return 0
        return state["inventory"].get(self._bare(item), 0)

    def agent_mine(self, step):
        """Companion mines ANY registered block (modded ids included)."""
        block, count = step["block"], int(step.get("count", 8))
        want = self._bare(step.get("collect_item", block))
        before = self._agent_inv(want)
        self.bridge.call("chat", text=f"/agent mine {block} {count}")
        target = before + count

        def enough():
            return self._agent_inv(want) >= target
        ok = self._wait(enough, timeout=step.get("timeout", 600), poll=15,
                        desc=f"agent mining {block}")
        if not ok:
            self.bridge.call("chat", text="/agent stop")
        have = self._agent_inv(want)
        return have > before, f"agent {want}: {before} -> {have} (wanted {target})"

    def agent_get(self, step):
        """Companion runs a full AltoClef gather/craft chain — VANILLA items only."""
        item, count = self._bare(step["item"]), int(step.get("count", 1))
        before = self._agent_inv(item)
        self.bridge.call("chat", text=f"/agent run get {item} {count}")

        def enough():
            return self._agent_inv(item) >= count
        ok = self._wait(enough, timeout=step.get("timeout", 600), poll=15,
                        desc=f"agent getting {item}")
        if not ok:
            self.bridge.call("chat", text="/agent stop")
        return ok, f"agent has {self._agent_inv(item)}x {item}"

    def agent_give(self, step):
        """Direct agent->player transfer via /agent give (FTB quest tasks check
        the PLAYER inventory). Calls the agent over with follow first."""
        item, count = self._bare(step["item"]), int(step.get("count", 1))
        before = self._inventory().get(item, 0)
        self.bridge.call("chat", text="/agent run follow")

        def close_and_given():
            self.bridge.call("chat", text=f"/agent give {item} {count}")
            time.sleep(2)
            return self._inventory().get(item, 0) >= before + count
        ok = self._wait(close_and_given, timeout=step.get("timeout", 120), poll=8,
                        desc=f"agent handing over {item}")
        self.bridge.call("chat", text="/agent stop")
        have = self._inventory().get(item, 0)
        return have > before, f"player {item}: {before} -> {have}"

    def agent_run(self, step):
        """Raw companion command: follow / goto x y z / attack <mob> / deposit / equip / stop."""
        self.bridge.call("chat", text=f"/agent run {step['cmd']}")
        return True, f"sent: {step['cmd']}"

    def agent_craft(self, step):
        """Server-side crafting from the AGENT's inventory — ANY recipe incl. modded."""
        item, count = self._bare(step["item"]), int(step.get("count", 1))
        before = self._agent_inv(item)
        self.bridge.call("chat", text=f"/agent craft {step['item']} {count}")
        time.sleep(2)
        have = self._agent_inv(item)
        return have >= before + count or have >= count, \
            f"agent {item}: {before} -> {have}"

    def agent_take(self, step):
        """Pull items from containers/machine slots within 12 blocks of the agent."""
        item, count = self._bare(step["item"]), int(step.get("count", 1))
        before = self._agent_inv(item)
        self.bridge.call("chat", text=f"/agent take {item} {count}")
        time.sleep(2)
        have = self._agent_inv(item)
        return have > before, f"agent {item}: {before} -> {have}"

    def agent_store(self, step):
        """Store items from the agent into the nearest chest (within 12 blocks)."""
        item, count = self._bare(step["item"]), int(step.get("count", 1))
        before = self._agent_inv(item)
        self.bridge.call("chat", text=f"/agent store {item} {count}")
        time.sleep(2)
        have = self._agent_inv(item)
        return have < before or before == 0, f"agent {item}: {before} -> {have}"

    def wait_for(self, step):
        seconds = step.get("seconds", 30)
        self.log(f"wait_for: {step.get('condition', '')} — sleeping {seconds}s")
        time.sleep(seconds)
        return True, "waited"

    def chat(self, step):
        self.bridge.call("chat", text=step["text"])
        return True, "sent"

    def manual(self, step):
        reason = step.get("reason", "unsupported action")
        self.bridge.call("echo", text=f"[agent] NEED HELP: {reason} — do it, then type .done in chat")
        self.log(f"MANUAL ESCALATION: {reason}")
        if not self._wait_for_player_signal():
            return False, "player never confirmed manual step"
        return True, "human handled it"

    # models drift across body modes despite prompt rules — route structurally.
    # companion mode: resource work goes to the PlayerEngine entity;
    # player mode: agent_* actions fold back onto the player's own body.
    COMPANION_REMAP = {"mine": "agent_mine", "goto_block": "agent_mine"}
    PLAYER_REMAP = {"agent_mine": "mine", "agent_craft": "craft",
                    "agent_get": "craft", "agent_take": "manual", "agent_store": "manual"}

    def _remap_for_body(self, name, action):
        if self.body == "companion":
            if name in self.COMPANION_REMAP and action.get("block"):
                action.setdefault("count", 2 if name == "goto_block" else 8)
                return self.COMPANION_REMAP[name]
            return name
        # player mode
        if name == "agent_give":
            return "_noop_already_player"  # items are already in the player's inventory
        if name == "agent_run":
            cmd = str(action.get("cmd", "")).split()
            if cmd and cmd[0] == "goto" and len(cmd) >= 4:
                action.update(x=float(cmd[1]), y=float(cmd[2]), z=float(cmd[3]))
                return "goto"
            if cmd and cmd[0] == "attack":
                action["entity"] = cmd[1] if len(cmd) > 1 else ""
                return "kill"
            if cmd and cmd[0] == "stop":
                self.bridge.call("chat", text="#stop")
            return "_noop_already_player"  # follow/pickup_drops/etc are meaningless solo
        if name in self.PLAYER_REMAP:
            if self.PLAYER_REMAP[name] == "manual":
                action["reason"] = f"{name} has no player-body equivalent: {action}"
            return self.PLAYER_REMAP[name]
        return name

    def _noop_already_player(self, step):
        return True, "no-op in player-body mode"

    def run(self, step):
        action = dict(step["action"])
        name = action.pop("action")
        new_name = self._remap_for_body(name, action)
        if new_name != name:
            self.log(f"  (remapped {name} -> {new_name} for body={self.body})")
            name = new_name
        handler = getattr(self, name, None)
        if not handler:
            return self.manual({"reason": f"unknown action '{name}': {action}"})
        health_before = (self.bridge.call("state") or {}).get("health")
        ok, note = handler(action)
        health_after = (self.bridge.call("state") or {}).get("health")
        if health_before and health_after and health_after < min(6, health_before - 8):
            self.bridge.call("echo", text="[agent] taking heavy damage — pausing, type .done when safe")
            self.log(f"DANGER: health {health_before} -> {health_after}")
            self._wait_for_player_signal(timeout=300)
        return ok, note
