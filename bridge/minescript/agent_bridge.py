r"""agent_bridge — runs INSIDE Minecraft via Minescript 4.x.

Start from in-game chat:  \agent_bridge
Connects to the agent daemon on localhost:48750 (start daemon first) and
serves JSON-lines requests: {"id": N, "op": "...", ...} -> {"id": N, "ok": true, "result": ...}

Ops: probe, state, chat, echo, chat_log, getblock, stop
Defensive about the Minescript API surface: anything missing is reported by
"probe" and returns null in "state" instead of crashing the bridge.
"""
import json
import os
import socket
import threading
import time
from collections import deque

import minescript  # provided by the Minescript mod's embedded interpreter

HOST, PORT = "127.0.0.1", 48750

CHAT_LOG = deque(maxlen=500)
CHAT_SEQ = {"n": 0}


def _fn(name):
    return getattr(minescript, name, None)


def _item_entry(stack):
    get = (lambda k: stack.get(k)) if isinstance(stack, dict) else (lambda k: getattr(stack, k, None))
    return get("item"), get("count") or 1


def op_probe(_req):
    wanted = ["player_position", "player_orientation", "player_inventory",
              "player_health", "player_hand_items", "player_name", "world_info",
              "chat", "echo", "execute", "getblock", "player_look_at",
              "player_press_forward", "player_press_use", "player_press_attack",
              "player_press_sneak", "player_press_jump",
              "player_inventory_select_slot", "player_inventory_slot_to_hotbar",
              "entities", "player_get_targeted_block", "container_click_slot",
              "container_get_items", "screen_name", "EventQueue"]
    return {name: bool(_fn(name)) for name in wanted}


def op_state(_req):
    out = {}
    name = _fn("player_name")
    if name:
        try:
            out["name"] = name()
        except Exception:
            pass
    pos = _fn("player_position")
    if pos:
        x, y, z = pos()
        out["position"] = {"x": round(x, 1), "y": round(y, 1), "z": round(z, 1)}
    health = _fn("player_health")
    if health:
        try:
            out["health"] = health()
        except Exception:
            out["health"] = None
    inv = _fn("player_inventory")
    if inv:
        items = {}
        for stack in inv() or []:
            item_id, count = _item_entry(stack)
            if item_id:
                items[item_id] = items.get(item_id, 0) + int(count)
        out["inventory"] = items
    world_info = _fn("world_info")
    if world_info:
        try:
            info = world_info()
            for key in ("day_ticks", "game_ticks", "raining", "hardcore"):
                val = getattr(info, key, None) if not isinstance(info, dict) else info.get(key)
                if val is not None:
                    out[key] = val
        except Exception:
            pass
    screen = _fn("screen_name")
    if screen:
        try:
            out["open_screen"] = screen()
        except Exception:
            pass
    return out


def op_chat(req):
    text = req["text"]
    # Minescript: chat() sends chat/commands; execute() is command-only on some versions
    if text.startswith("/") and _fn("execute"):
        _fn("execute")(text)
    else:
        _fn("chat")(text)
    return True


def op_echo(req):
    fn = _fn("echo") or _fn("chat")
    fn(str(req["text"]))
    return True


def op_chat_log(req):
    since = req.get("since", 0)
    entries = [e for e in CHAT_LOG if e["n"] > since]
    return {"entries": entries, "latest": CHAT_SEQ["n"]}


def op_getblock(req):
    gb = _fn("getblock")
    return gb(req["x"], req["y"], req["z"]) if gb else None


def op_container(_req):
    """Items in the currently open container GUI (None if nothing open)."""
    fn = _fn("container_get_items")
    if not fn:
        return None
    items = fn()
    if items is None:
        return None
    out = []
    for stack in items:
        item_id, count = _item_entry(stack)
        if item_id:
            out.append({"item": item_id, "count": int(count)})
    return out


def op_look_at(req):
    _fn("player_look_at")(req["x"], req["y"], req["z"])
    return True


def op_press(req):
    """Hold/release a movement or click control: use, attack, forward, jump, sneak..."""
    fn = _fn("player_press_" + req["control"])
    if not fn:
        return None
    fn(bool(req["pressed"]))
    return True


def op_inventory_slots(_req):
    """Player inventory with slot numbers (0-8 hotbar, 9-35 main)."""
    inv = _fn("player_inventory")
    out = []
    for stack in inv() or []:
        get = (lambda k: stack.get(k)) if isinstance(stack, dict) else (lambda k: getattr(stack, k, None))
        item_id, count = _item_entry(stack)
        if item_id:
            out.append({"slot": get("slot"), "item": item_id, "count": int(count or 1),
                        "selected": bool(get("selected"))})
    return out


def op_select_slot(req):
    fn = _fn("player_inventory_select_slot")
    return fn(int(req["slot"])) if fn else None


def op_slot_to_hotbar(req):
    fn = _fn("player_inventory_slot_to_hotbar")
    return fn(int(req["slot"])) if fn else None


def op_hand(_req):
    fn = _fn("player_hand_items")
    if not fn:
        return None
    hands = fn()
    main = getattr(hands, "main_hand", None) if not isinstance(hands, (list, tuple)) else (hands[0] if hands else None)
    if main is None:
        return None
    item_id, count = _item_entry(main)
    return {"item": item_id, "count": int(count or 0)}


def op_entities(req):
    fn = _fn("entities")
    if not fn:
        return None
    out = []
    for e in fn() or []:
        get = (lambda k: e.get(k)) if isinstance(e, dict) else (lambda k: getattr(e, k, None))
        pos = get("position")
        out.append({"name": str(get("name") or ""), "type": str(get("type") or ""),
                    "position": list(pos) if pos else None, "health": get("health"),
                    "uuid": str(get("uuid") or "")})
    return out[:100]


def op_targeted_block(req):
    fn = _fn("player_get_targeted_block")
    if not fn:
        return None
    tb = fn(req.get("max_distance", 20))
    if tb is None:
        return None
    get = (lambda k: tb.get(k)) if isinstance(tb, dict) else (lambda k: getattr(tb, k, None))
    pos = get("position")
    return {"position": list(pos) if pos else None, "block": str(get("type") or get("block") or "")}


def op_container_click(req):
    fn = _fn("container_click_slot")
    return fn(int(req["slot"])) if fn else None


def op_screenshot(_req):
    fn = _fn("screenshot")
    if not fn:
        return None
    fn()
    return True


OPS = {"probe": op_probe, "state": op_state, "chat": op_chat, "echo": op_echo,
       "chat_log": op_chat_log, "getblock": op_getblock, "container": op_container,
       "look_at": op_look_at, "press": op_press, "inventory_slots": op_inventory_slots,
       "select_slot": op_select_slot, "slot_to_hotbar": op_slot_to_hotbar,
       "hand": op_hand, "entities": op_entities, "targeted_block": op_targeted_block,
       "container_click": op_container_click, "screenshot": op_screenshot}


def chat_listener():
    """Capture incoming chat (Baritone status, quest toasts that echo to chat)."""
    eq_cls = _fn("EventQueue")
    if not eq_cls:
        return
    try:
        with eq_cls() as q:
            q.register_chat_listener()
            while True:
                event = q.get()
                msg = getattr(event, "message", None) or str(event)
                CHAT_SEQ["n"] += 1
                CHAT_LOG.append({"n": CHAT_SEQ["n"], "t": time.time(), "message": msg})
                # one-word kill switch: player types .quit in chat
                if ".quit" in msg.lower() and "[agent" not in msg and "[bridge" not in msg:
                    minescript.echo("[bridge] .quit received — shutting down")
                    os._exit(0)
    except Exception as exc:
        minescript.echo(f"[bridge] chat listener stopped: {exc}")


def main():
    threading.Thread(target=chat_listener, daemon=True).start()
    minescript.echo(f"[bridge] connecting to daemon at {HOST}:{PORT} ...")
    try:
        sock = socket.create_connection((HOST, PORT), timeout=10)
        # connect timeout must NOT persist: the daemon can be silent for minutes
        # while the LLM plans — a lingering timeout kills recv() (live-run crash)
        sock.settimeout(None)
    except OSError as exc:
        minescript.echo(f"[bridge] FAILED to connect ({exc}) — start the daemon first:")
        minescript.echo("[bridge]   python D:/reclamation-agent/agent/daemon.py")
        return
    minescript.echo("[bridge] connected — agent has control. \\killjob to stop.")
    buf = b""
    try:
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                req = json.loads(line)
                if req.get("op") == "stop":
                    sock.sendall(json.dumps({"id": req.get("id"), "ok": True, "result": "bye"}).encode() + b"\n")
                    return
                try:
                    result = OPS[req["op"]](req)
                    resp = {"id": req.get("id"), "ok": True, "result": result}
                except Exception as exc:
                    resp = {"id": req.get("id"), "ok": False, "error": f"{type(exc).__name__}: {exc}"}
                sock.sendall(json.dumps(resp).encode() + b"\n")
    finally:
        sock.close()
        minescript.echo("[bridge] disconnected.")


main()
