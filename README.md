# QuestPilot ⛏️🧠

**An autonomous AI player for _modded_ Minecraft.**

QuestPilot reads your modpack's quest book, plans with an LLM, and sends a
server-side AI companion to do the work — mining, crafting (modded recipes
included), fighting, hauling — while you keep playing. It is, as far as we
know, the first open agent that plays **modpacks**, not just vanilla.

> `/agent spawn Bob` → the planner reads 558 FTB quests → Bob walks off,
> mines 16 dead logs, crafts them through a two-step modded recipe chain,
> comes back and hands you the result. Nobody touched the keyboard.

---

## Why this exists

Every well-known Minecraft AI project is **vanilla-only**:

| Project | Why it can't play your modpack |
|---|---|
| Voyager, Mindcraft, mineflayer bots | Speak the vanilla protocol — can't even join a Forge server |
| Baritone / AltoClef | Brilliant executors, but vanilla item catalogues and Fabric-only builds |
| STEVE-1, JARVIS-VLA, pixel agents | Research models: vanilla training data, need an RTX 3090, ~5 FPS |
| AI-Player, ChatClef companions | Fabric-only, vanilla capabilities, cloud-locked brains |

Modded Minecraft — custom blocks, kubejs recipes, machine GUIs, 500-quest
progression books — is where the actually interesting automation problems
live. QuestPilot targets exactly that gap.

## What it does today

- **Reads the whole quest book offline.** FTB Quests SNBT → dependency DAG
  (chapters, quests, item tasks, rewards). The planner always knows the exact
  frontier of available quests — no screen reading, no guessing.
- **Plans with any LLM.** Claude (API or your Claude Code login), Groq's free
  tier, or anything you wire into one small provider file.
- **Acts through a real body.** A [PlayerEngine](https://www.curseforge.com/minecraft/mc-mods/playerengine)
  companion entity — server-side, pathfinding by Automatone (Baritone fork),
  AltoClef task chains — plus our `agentlink` micro-mod that exposes it to the
  planner:
  - `/agent mine <block> [n]` — mine **any** registered block, modded ids included
  - `/agent craft <item> [n]` — craft **any** recipe from the game's RecipeManager
    (kubejs/datapack recipes work), resolving intermediate ingredients
    **recursively** (log → scrap → planks happens automatically)
  - `/agent take|store <item> [n]` — move items between the agent and nearby
    chests or machine slots (any `IItemHandler`)
  - `/agent give <item> [n]` — hand the loot to the player (quests check *your* inventory)
  - `/agent run <cmd>` — the whole AltoClef command set: `follow`, `goto`,
    `attack`, `farm`, `deposit`, `equip`, ...
- **Verifies everything.** Inventory deltas, quest-save reads, chat-response
  checks. Failed steps trigger replanning; repeatedly failing quests get set
  aside instead of looping.
- **Escalates gracefully.** What it can't do yet (machine GUIs, claiming
  quest rewards), it asks for in chat and waits for your `.done`.

Live-tested on **Reclamation** (Forge 1.20.1, 165 mods, 558 quests) — the
companion autonomously mined modded blocks, crafted through modded recipe
chains, and progressed real quests.

## Architecture

```
┌─ your PC ────────────────────────────────────────────────────────────┐
│                                                                      │
│  daemon.py  ──plans──▶  LLM (Claude / Groq / local)                  │
│    │  ▲                                                              │
│    │  └── quest_graph.json  (built offline from the pack's           │
│    │       FTB Quests SNBT — full dependency DAG)                    │
│    ▼                                                                 │
│  TCP 127.0.0.1:48750  (JSON lines)                                   │
│    ▲                                                                 │
│ ┌──┴───────────────── Minecraft (Forge) ──────────────────────────┐  │
│ │ Minescript bridge  ── chat/state/inventory I/O                  │  │
│ │ agentlink mod      ── /agent commands → PlayerEngine controller │  │
│ │ PlayerEngine       ── companion entity: Automatone pathfinding, │  │
│ │                       AltoClef task chains, own inventory       │  │
│ └──────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────┘
```

The LLM never clicks pixels. Perception is files and registries (quest SNBT,
RecipeManager, capabilities); action is deterministic game logic. The model
only does what models are good at: deciding what to do next.

## Quick start

**Requirements:** a Forge 1.20.1 instance, Python 3.10+, JDK 17+ (only to
build the mod), and one of: `GROQ_API_KEY` (free), `ANTHROPIC_API_KEY`, or a
logged-in Claude Code CLI.

```bash
git clone https://github.com/Boyan253/questpilot && cd questpilot

# 1. mods into your instance's mods/ folder:
#    - PlayerEngine + Player2NPC (CurseForge, Forge 1.20.1)
#    - Baritone (optional, for player-body fallback) + Minescript
#    - agentlink: cd agentlink-mod && ./gradlew build   → build/libs/*.jar

# 2. point the tools at your instance and install the bridge
export AGENT_MC_DIR="/path/to/instance/minecraft"
python tools/install_bridge.py

# 3. build the quest graph from your pack (FTB Quests packs)
python tools/build_quest_graph.py

# 4. run the brain, then in-game chat:  \agent_bridge
export GROQ_API_KEY=...        # or ANTHROPIC_API_KEY, or claude-cli login
python agent/daemon.py
```

The daemon spawns a companion if none exists and starts working the quest
frontier. Type `#stop` / `/agent stop` in chat any time to halt it.

## Status

Early alpha, moving fast. Honest state of the world:

- [x] Quest graph extraction (FTB Quests), frontier planning, LLM planner
- [x] Companion body: mine (modded), craft (modded, recursive), take/store, give
- [x] Multiplayer mode (progress ledger + no-OP fallbacks)
- [x] Death detection, danger pause, verified human-assist steps
- [ ] Machine automation (insert/extract works; machine recipes next)
- [ ] Quest reward auto-claiming
- [ ] Other quest mods (HQM, Better Questing)
- [ ] Pack profiles — fully config-driven multi-pack support

## Credits

QuestPilot stands on excellent shoulders:
[PlayerEngine](https://github.com/Goodbird-git/PlayerEngine) by Goodbird ·
[Automatone](https://github.com/Ladysnake/Automatone) by Ladysnake ·
[AltoClef](https://github.com/gaucho-matrero/altoclef) ·
[Baritone](https://github.com/cabaletta/baritone) ·
[Minescript](https://minescript.net) ·
[FTB Quests](https://github.com/FTBTeam/FTB-Quests)

## License

MIT for everything in this repo. PlayerEngine/Player2NPC (LGPL-3.0) and other
mods are **not** bundled — download them from their own pages.
