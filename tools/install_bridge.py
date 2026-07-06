"""Install/refresh the in-game bridge script into your Minecraft instance.

Copies bridge/minescript/agent_bridge.py into the instance's minescript/
folder (where Minescript looks for \\commands). Creates a minimal config.txt
with the system Python path only if Minescript hasn't generated one yet —
if Minescript complains about config on first launch, delete config.txt and
let the mod regenerate it, then re-run this script.

Set AGENT_MC_DIR to your instance's minecraft/ (or .minecraft/) folder.
"""
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_mc = os.environ.get("AGENT_MC_DIR")
if not _mc:
    sys.exit("set AGENT_MC_DIR to your Minecraft instance's minecraft/ folder")
INSTANCE_MC = Path(_mc)
SRC = ROOT / "bridge" / "minescript" / "agent_bridge.py"


def main():
    ms_dir = INSTANCE_MC / "minescript"
    ms_dir.mkdir(exist_ok=True)
    dest = ms_dir / "agent_bridge.py"
    shutil.copy2(SRC, dest)
    print(f"installed {dest}")

    config = ms_dir / "config.txt"
    if not config.exists():
        python = shutil.which("python") or sys.executable
        config.write_text(f'python="{python}"\n', encoding="utf-8")
        print(f"wrote {config} (python={python})")
    else:
        print(f"config exists, not touching: {config}")


if __name__ == "__main__":
    main()
