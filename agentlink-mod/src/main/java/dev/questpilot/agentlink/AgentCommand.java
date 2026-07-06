package dev.questpilot.agentlink;

import com.goodbird.player2npc.companion.AutomatoneEntity;
import com.mojang.brigadier.CommandDispatcher;
import com.mojang.brigadier.arguments.StringArgumentType;
import com.mojang.brigadier.exceptions.CommandSyntaxException;
import com.player2.playerengine.player2api.Character;
import com.player2.playerengine.tasks.resources.MineAndCollectTask;
import com.player2.playerengine.util.MiningRequirement;
import net.minecraft.commands.CommandSourceStack;
import net.minecraft.commands.Commands;
import net.minecraft.core.BlockPos;
import net.minecraft.core.NonNullList;
import net.minecraft.network.chat.Component;
import net.minecraft.resources.ResourceLocation;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.world.item.Item;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.crafting.CraftingRecipe;
import net.minecraft.world.item.crafting.Ingredient;
import net.minecraft.world.item.crafting.RecipeType;
import net.minecraft.world.level.block.Block;
import net.minecraft.world.level.block.Blocks;
import net.minecraft.world.level.block.entity.BlockEntity;
import net.minecraftforge.common.capabilities.ForgeCapabilities;
import net.minecraftforge.items.IItemHandler;
import net.minecraftforge.items.ItemHandlerHelper;
import net.minecraftforge.registries.ForgeRegistries;

import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * /agent spawn [name]        — summon a PlayerEngine agent next to the caller
 * /agent run <cmdline>       — feed an AltoClef command to the nearest agent
 *                              (get/goto/follow/attack/farm/deposit/equip/...)
 * /agent mine <block> [n]    — MineAndCollectTask on ANY registered block,
 *                              including modded ids (kubejs:dead_log 16)
 * /agent state               — one-line JSON status (pos/health/inventory),
 *                              echoed to chat for the daemon's chat_log reader
 * /agent stop                — shortcut for run stop
 *
 * The external planner daemon issues these through the Minescript chat bridge,
 * so the LLM brain stays outside the game while PlayerEngine provides the body.
 */
public final class AgentCommand {

    private AgentCommand() {}

    public static void register(CommandDispatcher<CommandSourceStack> dispatcher) {
        dispatcher.register(Commands.literal("agent")
            .requires(src -> src.hasPermission(2))
            .then(Commands.literal("spawn")
                .executes(ctx -> spawn(ctx.getSource(), "Agent"))
                .then(Commands.argument("name", StringArgumentType.word())
                    .executes(ctx -> spawn(ctx.getSource(),
                            StringArgumentType.getString(ctx, "name")))))
            .then(Commands.literal("run")
                .then(Commands.argument("cmd", StringArgumentType.greedyString())
                    .executes(ctx -> run(ctx.getSource(),
                            StringArgumentType.getString(ctx, "cmd")))))
            .then(Commands.literal("mine")
                .then(Commands.argument("spec", StringArgumentType.greedyString())
                    .executes(ctx -> mine(ctx.getSource(),
                            StringArgumentType.getString(ctx, "spec")))))
            .then(Commands.literal("give")
                .then(Commands.argument("spec", StringArgumentType.greedyString())
                    .executes(ctx -> give(ctx.getSource(),
                            StringArgumentType.getString(ctx, "spec")))))
            .then(Commands.literal("craft")
                .then(Commands.argument("spec", StringArgumentType.greedyString())
                    .executes(ctx -> craft(ctx.getSource(),
                            StringArgumentType.getString(ctx, "spec")))))
            .then(Commands.literal("take")
                .then(Commands.argument("spec", StringArgumentType.greedyString())
                    .executes(ctx -> container(ctx.getSource(),
                            StringArgumentType.getString(ctx, "spec"), true))))
            .then(Commands.literal("store")
                .then(Commands.argument("spec", StringArgumentType.greedyString())
                    .executes(ctx -> container(ctx.getSource(),
                            StringArgumentType.getString(ctx, "spec"), false))))
            .then(Commands.literal("state")
                .executes(ctx -> state(ctx.getSource())))
            .then(Commands.literal("stop")
                .executes(ctx -> run(ctx.getSource(), "stop"))));
    }

    private static AutomatoneEntity nearest(ServerPlayer player) {
        List<AutomatoneEntity> agents = player.serverLevel().getEntitiesOfClass(
                AutomatoneEntity.class, player.getBoundingBox().inflate(96.0));
        return agents.stream()
                .min(Comparator.comparingDouble(e -> e.distanceToSqr(player)))
                .orElse(null);
    }

    private static int mine(CommandSourceStack src, String spec) throws CommandSyntaxException {
        ServerPlayer player = src.getPlayerOrException();
        AutomatoneEntity agent = nearest(player);
        if (agent == null || agent.controller == null) {
            src.sendFailure(Component.literal("[agentlink] no controllable agent nearby"));
            return 0;
        }
        String[] parts = spec.trim().split("\\s+");
        int count = parts.length > 1 ? Integer.parseInt(parts[1]) : 8;
        ResourceLocation id = ResourceLocation.tryParse(parts[0]);
        Block block = id == null ? null : ForgeRegistries.BLOCKS.getValue(id);
        if (block == null || block == Blocks.AIR) {
            src.sendFailure(Component.literal("[agentlink] unknown block: " + parts[0]));
            return 0;
        }
        agent.controller.runUserTask(new MineAndCollectTask(
                block.asItem(), count, new Block[]{block},
                MiningRequirement.getMinimumRequirementForBlock(block)));
        src.sendSuccess(() -> Component.literal("[agentlink] mining " + count + "x " + id), false);
        return 1;
    }

    /** Direct inventory transfer agent -> caller. Requires the agent within 8
     *  blocks (bring it with `run follow` first) — AltoClef's GiveItemToPlayerTask
     *  throws items by physics and loses most of the stack. */
    private static int give(CommandSourceStack src, String spec) throws CommandSyntaxException {
        ServerPlayer player = src.getPlayerOrException();
        AutomatoneEntity agent = nearest(player);
        if (agent == null || agent.getLivingInventory() == null) {
            src.sendFailure(Component.literal("[agentlink] no agent nearby"));
            return 0;
        }
        if (agent.distanceToSqr(player) > 64.0) {
            src.sendFailure(Component.literal("[agentlink] agent too far to hand over — `/agent run follow` first"));
            return 0;
        }
        String[] parts = spec.trim().split("\\s+");
        String itemName = parts[0].contains(":") ? parts[0].split(":", 2)[1] : parts[0];
        int count = parts.length > 1 ? Integer.parseInt(parts[1]) : 64;
        int moved = 0;
        for (ItemStack stack : agent.getLivingInventory().main) {
            if (moved >= count || stack.isEmpty()) continue;
            ResourceLocation key = ForgeRegistries.ITEMS.getKey(stack.getItem());
            if (key == null || !key.getPath().equals(itemName)) continue;
            int take = Math.min(count - moved, stack.getCount());
            ItemStack transfer = stack.copyWithCount(take);
            player.getInventory().add(transfer); // mutates transfer to the leftover
            int accepted = take - transfer.getCount();
            stack.shrink(accepted);
            moved += accepted;
            if (accepted < take) break; // player inventory full
        }
        final int total = moved;
        src.sendSuccess(() -> Component.literal(
                "[agentlink] handed over " + total + "x " + itemName), false);
        return total > 0 ? 1 : 0;
    }

    private static Item resolveItem(String name) {
        ResourceLocation id = ResourceLocation.tryParse(name);
        if (id != null && ForgeRegistries.ITEMS.containsKey(id)) {
            return ForgeRegistries.ITEMS.getValue(id);
        }
        // bare name: first registry entry with that path (any namespace)
        for (ResourceLocation key : ForgeRegistries.ITEMS.getKeys()) {
            if (key.getPath().equals(name)) {
                return ForgeRegistries.ITEMS.getValue(key);
            }
        }
        return null;
    }

    private static int insertIntoAgent(AutomatoneEntity agent, ItemStack stack) {
        NonNullList<ItemStack> main = agent.getLivingInventory().main;
        int remaining = stack.getCount();
        for (ItemStack slot : main) {
            if (remaining == 0) break;
            if (!slot.isEmpty() && ItemStack.isSameItemSameTags(slot, stack)
                    && slot.getCount() < slot.getMaxStackSize()) {
                int moved = Math.min(remaining, slot.getMaxStackSize() - slot.getCount());
                slot.grow(moved);
                remaining -= moved;
            }
        }
        for (int i = 0; i < main.size() && remaining > 0; i++) {
            if (main.get(i).isEmpty()) {
                int moved = Math.min(remaining, stack.getMaxStackSize());
                main.set(i, stack.copyWithCount(moved));
                remaining -= moved;
            }
        }
        return stack.getCount() - remaining;
    }

    private static int countIn(NonNullList<ItemStack> main, Item item) {
        int total = 0;
        for (ItemStack stack : main) {
            if (!stack.isEmpty() && stack.getItem() == item) total += stack.getCount();
        }
        return total;
    }

    /** Greedy consume+produce for `crafts` iterations of one recipe. */
    private static boolean tryCraft(AutomatoneEntity agent, CraftingRecipe recipe,
                                    ItemStack result, int crafts) {
        NonNullList<ItemStack> main = agent.getLivingInventory().main;
        int[] counts = new int[main.size()];
        for (int i = 0; i < main.size(); i++) counts[i] = main.get(i).getCount();
        for (int c = 0; c < crafts; c++) {
            for (Ingredient ing : recipe.getIngredients()) {
                if (ing.isEmpty()) continue;
                boolean matched = false;
                for (int i = 0; i < main.size(); i++) {
                    if (counts[i] > 0 && ing.test(main.get(i))) {
                        counts[i]--;
                        matched = true;
                        break;
                    }
                }
                if (!matched) return false;
            }
        }
        for (int i = 0; i < main.size(); i++) {
            int consumed = main.get(i).getCount() - counts[i];
            if (consumed > 0) main.get(i).shrink(consumed);
        }
        for (int c = 0; c < crafts; c++) {
            insertIntoAgent(agent, result.copy());
        }
        return true;
    }

    /** Craft target from the agent's inventory; when an ingredient is missing
     *  but itself craftable, craft it first (modpack chains like
     *  dead_log -> scrap_wood -> flimsy_planks resolve automatically). */
    private static boolean craftRecursive(ServerPlayer player, AutomatoneEntity agent,
                                          Item target, int wanted, int depth) {
        NonNullList<ItemStack> main = agent.getLivingInventory().main;
        if (countIn(main, target) >= wanted) return true;
        if (depth <= 0) return false;
        List<CraftingRecipe> recipes = player.serverLevel().getRecipeManager()
                .getAllRecipesFor(RecipeType.CRAFTING);
        for (CraftingRecipe recipe : recipes) {
            ItemStack result = recipe.getResultItem(player.serverLevel().registryAccess());
            if (result.isEmpty() || result.getItem() != target) continue;
            int missing = wanted - countIn(main, target);
            int crafts = (missing + result.getCount() - 1) / result.getCount();
            if (tryCraft(agent, recipe, result, crafts)) return true;
            // gather per-ingredient needs (representative item per ingredient slot)
            Map<Item, Integer> needs = new HashMap<>();
            for (Ingredient ing : recipe.getIngredients()) {
                if (ing.isEmpty() || ing.getItems().length == 0) continue;
                needs.merge(ing.getItems()[0].getItem(), crafts, Integer::sum);
            }
            boolean progressed = false;
            for (Map.Entry<Item, Integer> need : needs.entrySet()) {
                if (need.getKey() == target) { progressed = false; break; }
                if (countIn(main, need.getKey()) < need.getValue()) {
                    progressed |= craftRecursive(player, agent, need.getKey(),
                            need.getValue(), depth - 1);
                }
            }
            if (progressed && tryCraft(agent, recipe, result, crafts)) return true;
        }
        return false;
    }

    /** Craft ANY registered recipe (kubejs/modded included) from the agent's
     *  own inventory, server-side, resolving intermediate ingredients recursively. */
    private static int craft(CommandSourceStack src, String spec) throws CommandSyntaxException {
        ServerPlayer player = src.getPlayerOrException();
        AutomatoneEntity agent = nearest(player);
        if (agent == null || agent.getLivingInventory() == null || agent.controller == null) {
            src.sendFailure(Component.literal("[agentlink] no controllable agent nearby"));
            return 0;
        }
        String[] parts = spec.trim().split("\\s+");
        Item target = resolveItem(parts[0]);
        int wanted = parts.length > 1 ? Integer.parseInt(parts[1]) : 1;
        if (target == null) {
            src.sendFailure(Component.literal("[agentlink] unknown item: " + parts[0]));
            return 0;
        }
        boolean ok = craftRecursive(player, agent, target, wanted, 4);
        int have = countIn(agent.getLivingInventory().main, target);
        final ResourceLocation rid = ForgeRegistries.ITEMS.getKey(target);
        if (ok) {
            src.sendSuccess(() -> Component.literal(
                    "[agentlink] crafted: agent now has " + have + "x " + rid), false);
            return 1;
        }
        src.sendFailure(Component.literal("[agentlink] cannot craft " + parts[0]
                + " — missing base ingredients (agent has " + have + ")"));
        return 0;
    }

    /** take=true: pull item from nearby inventories (chests, machine slots) into
     *  the agent; take=false: store from the agent into the nearest chest. */
    private static int container(CommandSourceStack src, String spec, boolean take)
            throws CommandSyntaxException {
        ServerPlayer player = src.getPlayerOrException();
        AutomatoneEntity agent = nearest(player);
        if (agent == null || agent.getLivingInventory() == null) {
            src.sendFailure(Component.literal("[agentlink] no agent nearby"));
            return 0;
        }
        String[] parts = spec.trim().split("\\s+");
        String itemName = parts[0].contains(":") ? parts[0].split(":", 2)[1] : parts[0];
        int count = parts.length > 1 ? Integer.parseInt(parts[1]) : 64;
        int moved = 0;
        BlockPos center = agent.blockPosition();
        for (BlockPos pos : BlockPos.betweenClosed(center.offset(-12, -6, -12),
                                                   center.offset(12, 6, 12))) {
            if (moved >= count) break;
            BlockEntity be = player.serverLevel().getBlockEntity(pos);
            if (be == null) continue;
            IItemHandler handler = be.getCapability(ForgeCapabilities.ITEM_HANDLER)
                    .resolve().orElse(null);
            if (handler == null) continue;
            if (take) {
                for (int slot = 0; slot < handler.getSlots() && moved < count; slot++) {
                    ItemStack in = handler.getStackInSlot(slot);
                    if (in.isEmpty()) continue;
                    ResourceLocation key = ForgeRegistries.ITEMS.getKey(in.getItem());
                    if (key == null || !key.getPath().equals(itemName)) continue;
                    ItemStack pulled = handler.extractItem(slot, count - moved, false);
                    moved += insertIntoAgent(agent, pulled);
                }
            } else {
                NonNullList<ItemStack> main = agent.getLivingInventory().main;
                for (ItemStack stack : main) {
                    if (moved >= count || stack.isEmpty()) continue;
                    ResourceLocation key = ForgeRegistries.ITEMS.getKey(stack.getItem());
                    if (key == null || !key.getPath().equals(itemName)) continue;
                    int offer = Math.min(count - moved, stack.getCount());
                    ItemStack leftover = ItemHandlerHelper.insertItem(
                            handler, stack.copyWithCount(offer), false);
                    int accepted = offer - leftover.getCount();
                    stack.shrink(accepted);
                    moved += accepted;
                }
            }
        }
        final int total = moved;
        final String verb = take ? "took" : "stored";
        src.sendSuccess(() -> Component.literal(
                "[agentlink] " + verb + " " + total + "x " + itemName), false);
        return total > 0 ? 1 : 0;
    }

    private static int state(CommandSourceStack src) throws CommandSyntaxException {
        ServerPlayer player = src.getPlayerOrException();
        AutomatoneEntity agent = nearest(player);
        if (agent == null) {
            src.sendSuccess(() -> Component.literal("[agentstate] {\"agent\": null}"), false);
            return 0;
        }
        Map<String, Integer> inv = new HashMap<>();
        if (agent.getLivingInventory() != null) {
            for (ItemStack stack : agent.getLivingInventory().main) {
                if (!stack.isEmpty()) {
                    ResourceLocation key = ForgeRegistries.ITEMS.getKey(stack.getItem());
                    String name = key == null ? "unknown" : key.getPath();
                    inv.merge(name, stack.getCount(), Integer::sum);
                }
            }
        }
        StringBuilder sb = new StringBuilder("[agentstate] {\"pos\":[")
                .append((int) agent.getX()).append(',')
                .append((int) agent.getY()).append(',')
                .append((int) agent.getZ()).append("],\"health\":")
                .append(agent.getHealth()).append(",\"inventory\":{");
        boolean first = true;
        for (Map.Entry<String, Integer> e : inv.entrySet()) {
            if (!first) sb.append(',');
            sb.append('"').append(e.getKey()).append("\":").append(e.getValue());
            first = false;
        }
        sb.append("}}");
        String line = sb.toString();
        src.sendSuccess(() -> Component.literal(line), false);
        return 1;
    }

    private static int spawn(CommandSourceStack src, String name) throws CommandSyntaxException {
        ServerPlayer player = src.getPlayerOrException();
        // Local dummy character — no Player2 account involved. The cloud
        // greeting inside init() fails silently without auth; harmless.
        Character character = new Character(
                "agentlink-" + name.toLowerCase(), name, name,
                "", "Reclamation quest agent (external planner)", "", new String[0]);
        AutomatoneEntity agent = new AutomatoneEntity(player.level(), character, player);
        agent.setPos(player.getX() + 1.0, player.getY(), player.getZ());
        player.serverLevel().addFreshEntity(agent);
        src.sendSuccess(() -> Component.literal("[agentlink] spawned agent '" + name + "'"), false);
        return 1;
    }

    private static int run(CommandSourceStack src, String cmd) throws CommandSyntaxException {
        ServerPlayer player = src.getPlayerOrException();
        AutomatoneEntity agent = nearest(player);
        if (agent == null) {
            src.sendFailure(Component.literal("[agentlink] no agent within 64 blocks — /agent spawn first"));
            return 0;
        }
        if (agent.controller == null) {
            src.sendFailure(Component.literal("[agentlink] agent has no controller (spawned without character?)"));
            return 0;
        }
        agent.controller.getCommandExecutor().executeWithPrefix(cmd);
        src.sendSuccess(() -> Component.literal("[agentlink] -> " + cmd), false);
        return 1;
    }
}
