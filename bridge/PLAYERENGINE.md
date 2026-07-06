# PlayerEngine integration — design (from decompiled forge 1.3.0 jars)

## Verdict recap
PlayerEngine forge-1.20.1-1.3.0 = Automatone (server-side Baritone fork) +
AltoClef task chains, packaged as `com.player2.playerengine.*`. Player2NPC is
the showcase entity mod. Both installed in the instance. The LLM "brain" is
Player2's CLOUD api (api.player2.game + OAuth) — no localhost impersonation,
no direct chat-command bypass. => we ship a micro-mod ("agentlink") that
exposes the controller to our daemon.

## Wiring facts (decompiled player2npc-forge-1.3.0)
- Entity: `com.goodbird.player2npc.companion.AutomatoneEntity extends LivingEntity`
  - type registered as `player2npc:aicompanion` (`Player2NPC.AUTOMATONE`)
  - `init(Player owner)`: creates LivingEntityInteractionManager/Inventory/Hunger;
    controller ONLY created when `character != null`:
    `new PlayerEngineController(IBaritone.KEY.get(this), character, PLAYER2_GAME_ID)`
  - ctor `AutomatoneEntity(Level, Character, Player owner)` — Character is a
    simple record (name, shortName, greetingInfo, description, skinURL, ...)
    → construct a dummy one locally, NO Player2 account needed
  - `ConversationManager.sendGreeting(...)` hits the cloud — skip/ignore failure
- Command execution: `controller.getCommandExecutor().execute(String line)`
  (CommandExecutor in playerengine jar; prefix "@", commands: get, goto, follow,
  attack, farm, fish, deposit, equip, food, inventory, locate_structure, stash,
  status, stop, idle...)
- `get` resolves via hardcoded vanilla TaskCatalogue (159 items) → modded items
  NOT supported by `get`; modded chains stay in OUR planner, decomposed to
  primitives (mine/goto/attack/deposit + vanilla get).
- Components: Player2NPCComponents registers IBaritone/ISelectionManager/etc.
  for AutomatoneEntity.class via Cardinal Components — reusing THEIR entity
  type avoids re-registering anything.

## agentlink mod (v1 = thin reflection shim, no new entity)
Forge MDK 1.20.1, depends on both jars (libs/ fileTree). Registers:
- `/agent spawn <name>`  → build dummy Character via reflection, spawn
  AutomatoneEntity at caller, setOwner(caller)
- `/agent run <line...>` → nearest owned AutomatoneEntity →
  controller (reflection field) → getCommandExecutor().execute(line)
- `/agent status` / `/agent stop` → task list / executor stop
Daemon integration: existing Minescript bridge sends these as chat commands —
zero new protocol. Planner emits pe_* actions mapped to `/agent run ...`.

## Open risks (smoke test gates)
1. PlayerEngine+Player2NPC load alongside Reclamation's 165 mods (mixin/CC clash?)
2. PLAYER_JOIN auth check (`AuthenticationManager.checkAuth`) behaviour without
   a Player2 account — expected: async fail, harmless log spam
3. Automatone pathing performance server-side in a heavy pack
4. AltoClef chains touching vanilla-only assumptions inside Reclamation
   (e.g. `food` command wants vanilla food) — planner works around
