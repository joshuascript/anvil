# The Anvil Project

### Overview
Anvil is a patch toolkit for the s&box native engine on Linux. It compiles
and applies the patches needed to run the engine without Proton.

### Contents
- **patches/**         — C source patches. Preloaded at launch to fix case-sensitive
                         filesystem access and native engine crashes
- **patch_engine.sh**  — Compiles all patches in `patches/` and drops `.so` files into `patches/bin/`
- **launch-sbox.sh**   — Preloads compiled patches and launches the game

### Getting started
1. Run `bash anvil/patch_engine.sh` to compile patches
2. Run `bash anvil/launch-sbox.sh` to start the game
