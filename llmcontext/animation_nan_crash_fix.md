# Animation NaN Crash (libanimationsystem.so)

## Problem

The game crashes ~1 minute into loading a gamemode with a SIGSEGV in `libanimationsystem.so`:

```asm
=> movslq -0x40(%rbp,%r10,4),%r10
```

Registers at crash:
```
r10 = 0xfffffffe00000000   ← bone index overflow
rax = 0xffffffff80000001
```

The stack is filled with `0x7fc000007fc00000` — packed IEEE 754 quiet NaN floats
(`float32 0x7FC00000`). Native assertion dialogs precede the crash:

1. `tier0_stdlib.cpp:562` — `V_acosf()`: `x >= -1.0f && x <= 1.0f` — x was `-nan`
2. `mathlib.cpp:4100` — `CriticallyDampedSpring()`: `IsFinite(flVelocity)`
3. `mathlib.cpp:4101` — `CriticallyDampedSpring()`: `IsFinite(flPosition)`

See `llmcontext/assertions/assertion_animation_nan.md` for the assertion details.

## Root Cause

The NaN cascade:

1. Managed code passes a NaN or non-unit vector/velocity into the animation parameter system
2. Native animation system computes `acos(dot(bad_vector, bone_forward))` → NaN
3. NaN enters a bone's `CriticallyDampedSpring` position/velocity → assertions fire
4. NaN bone weight hits `cvttss2si(NaN)` → `0x80000000` bone index
5. `movslq -0x40(%rbp,%r10,4),%r10` scales `0x80000000` → ~8 GB offset → SIGSEGV

`Vector3.SmoothDamped` accumulates NaN permanently once poisoned — subsequent frames
remain NaN even after the source is fixed. It must be explicitly reset.

---

## Fix 1 — `SkinnedModelRenderer.Parameters.cs` — `SetLookDirection` ✅ CONFIRMED

Both overloads guard against zero/NaN/non-finite direction vectors:

```csharp
public void SetLookDirection( string name, Vector3 eyeDirectionWorld )
{
    var len = eyeDirectionWorld.Length;
    if ( !float.IsFinite( len ) || len < 1e-6f ) return;
    var delta = eyeDirectionWorld.Normal * WorldRotation.Inverse;
    Set( name, delta );
}

public void SetLookDirection( string name, Vector3 eyeDirectionWorld, float weight )
{
    var len = eyeDirectionWorld.Length;
    if ( !float.IsFinite( len ) || len < 1e-6f ) return;
    var delta = eyeDirectionWorld.Normal * WorldRotation.Inverse;
    Set( name, delta );
    Set( $"{name}_weight", weight );
}
```

## Fix 2 — `MoveMode.Animation.cs` — `OnUpdateAnimatorVelocity` ✅ CONFIRMED

NaN sanitization at the top of the method, and in `GetAngle`:

```csharp
if ( vel.IsNaN ) vel = Vector3.Zero;
if ( wishVel.IsNaN ) wishVel = Vector3.Zero;

if ( smoothedMove.Current.IsNaN || smoothedMove.Velocity.IsNaN ) smoothedMove = new Vector3.SmoothDamped( 0, 0, 0.5f );
if ( smoothedWish.Current.IsNaN || smoothedWish.Velocity.IsNaN ) smoothedWish = new Vector3.SmoothDamped( 0, 0, 0.5f );
if ( smoothedSkid.Current.IsNaN || smoothedSkid.Velocity.IsNaN ) smoothedSkid = new Vector3.SmoothDamped( 0, 0, 0.5f );

private static float GetAngle( Vector3 localVelocity )
{
    if ( localVelocity.IsNaN ) return 0f;
    return MathF.Atan2( localVelocity.y, localVelocity.x ).RadianToDegree().NormalizeDegrees();
}
```

### Verification (Fix 1 + 2)

Session `20260519_234226`: 6 crashes — #1-4 CLR null-ref, **#5-6 `libanimationsystem.so`**

Session `20260520_000336` (after fix): 4 crashes — all CLR null-ref only.
`libanimationsystem.so` crashes absent. **Fix confirmed by user.**

---

## Fix 3 — `CitizenAnimationHelper.cs` — `WithVelocity` / `WithWishVelocity` ⏳ UNCONFIRMED

Server code calls `CitizenAnimationHelper` directly, bypassing the `MoveMode` path entirely.
Identified as the regression source in session `20260520_165312` — same NaN crash signature,
new call site. Applied 2026-05-20, confirmation pending.

```csharp
public void WithVelocity( Vector3 Velocity )
{
    if ( Velocity.IsNaN ) return;
    // ...
}

public void WithWishVelocity( Vector3 Velocity )
{
    if ( Velocity.IsNaN ) return;
    // ...
}
```

---

## Notes

- `CriticallyDampedSpring` in `libanimationsystem.so` is animation-internal; the managed
  `PhysicsSpring` / `SpringJoint` types are unrelated
- `Vector3.SmoothDamped` has no internal NaN protection — once poisoned it stays NaN
  indefinitely until explicitly reset with `new Vector3.SmoothDamped(...)`
- When new server-side animation code is added, check that any path calling
  `renderer.Set("move_speed", ...)` or similar with a velocity guards against NaN upstream
