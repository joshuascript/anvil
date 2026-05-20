# Animation NaN Crash Fix (libanimationsystem.so)

## Problem

The game crashed ~1 minute into loading a gamemode with a SIGSEGV in `libanimationsystem.so` at offset `0x7ff6854287cd`:

```asm
=> 0x7ff6854287cd: movslq -0x40(%rbp,%r10,4),%r10
```

Registers at crash:
```
r10 = 0xfffffffe00000000   ← bone index overflow
rax = 0xffffffff80000001
```

The stack was littered with `0xffc00000ffc00000` — two packed IEEE 754 NaN floats (float32 `0xFFC00000` = -NaN).

Native assertion failures (from `assertion_failures.md`) preceded the crash:

1. `tier0_stdlib.cpp:562` — `V_acosf()`: `x >= -1.0f && x <= 1.0f` — x was `-nan`
2. `mathlib.cpp:4100` — `CriticallyDampedSpring()`: `IsFinite(flVelocity)` — not finite
3. `mathlib.cpp:4101` — `CriticallyDampedSpring()`: `IsFinite(flPosition)` — not finite

## Root Cause

Both `V_acosf` and `CriticallyDampedSpring` are used internally by `libanimationsystem.so`
to smooth bone orientations (hip dip, whip, tilt plane, IK ground springs). They are **not**
the managed `PhysicsSpring` type.

The NaN cascade:

1. Managed code passes a non-unit or NaN direction vector to `SetLookDirection`, or passes
   NaN velocity floats to `renderer.Set("move_speed", ...)` etc.
2. Native animation system computes `acos(dot(bad_vector, bone_forward))` → NaN
3. NaN enters a bone's `CriticallyDampedSpring` position/velocity → assertions fire
4. NaN bone weight hits `cvttss2si(NaN)` → `0x80000000` bone index
5. `movslq -0x40(%rbp,%r10,4),%r10` scales `0x80000000` → ~8 GB offset → SIGSEGV

The NaN source was upstream in the managed player animation update path:

- `Controller.Velocity` / `Controller.WishVelocity` could carry NaN (uninitialized or
  bad physics state on first frames)
- `Vector3.SmoothDamped` accumulates NaN permanently once a NaN is fed in — subsequent
  frames remain NaN even after the source is fixed
- `SetLookDirection` passed the direction vector to native without normalizing, so any
  sub-unit or zero vector produced NaN from native's dot product

## Fix

### `SkinnedModelRenderer.Parameters.cs` — `SetLookDirection`

Both overloads now guard against zero/NaN/non-finite direction vectors:

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

Key changes:
- Early return if direction is zero or non-finite — keeps last good spring state
- `.Normal` ensures the vector is unit-length before native receives it

### `MoveMode.Animation.cs` — `OnUpdateAnimatorVelocity`

Added NaN sanitization at the top of the method:

```csharp
if ( vel.IsNaN ) vel = Vector3.Zero;
if ( wishVel.IsNaN ) wishVel = Vector3.Zero;

if ( smoothedMove.Current.IsNaN || smoothedMove.Velocity.IsNaN ) smoothedMove = new Vector3.SmoothDamped( 0, 0, 0.5f );
if ( smoothedWish.Current.IsNaN || smoothedWish.Velocity.IsNaN ) smoothedWish = new Vector3.SmoothDamped( 0, 0, 0.5f );
if ( smoothedSkid.Current.IsNaN || smoothedSkid.Velocity.IsNaN ) smoothedSkid = new Vector3.SmoothDamped( 0, 0, 0.5f );
```

Also added NaN guard in `GetAngle`:

```csharp
private static float GetAngle( Vector3 localVelocity )
{
    if ( localVelocity.IsNaN ) return 0f;
    return MathF.Atan2( localVelocity.y, localVelocity.x ).RadianToDegree().NormalizeDegrees();
}
```

## Verification

Session `20260519_234226`: 6 crashes — #1-4 CLR null-ref, **#5-6 `libanimationsystem.so`**

Session `20260520_000336` (after fix): 4 crashes — all CLR null-ref only.
`libanimationsystem.so` crashes absent. Fix confirmed.

## Notes

- The "Springs range issue" popup the user observed was these exact assertions
  surfacing as a native error dialog before the crash
- `CriticallyDampedSpring` in `libanimationsystem.so` is animation-internal; the managed
  `PhysicsSpring` / `SpringJoint` types are unrelated
- `Vector3.SmoothDamped` has no NaN protection internally — once poisoned, it stays NaN
  indefinitely until explicitly reset
