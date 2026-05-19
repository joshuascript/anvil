# The Anvil Project

### What is Anvil?
Anvil is a patch toolkit for the s&box native engine on Linux. It compiles
and applies the patches needed to run the engine without Proton.

### What's inside?
- **patch/**   — C source patches and compiled binaries. Preloaded at launch to fix
                    case-sensitive filesystem access and native engine crashes
- **launch/**  — Managed launch scripts. Always use these instead of the sbox binary directly.
- **debug/**   — Python utilities for crash analysis and engine probing, and crash logs

### Getting started
1. Run `bash anvil/launch/patch_engine.sh` to compile patches
2. Run `bash anvil/launch/launch-sbox.sh` to start the game
