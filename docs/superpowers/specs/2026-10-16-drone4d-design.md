# Drone4D sandbox integration

The user selected the colleague's public isolated sandbox. Clone ANet-Drone4D v0.2.0 locally for its official SDK and native WebGL frontend; the runtime remains remote because this host is macOS arm64 and upstream StateRing requires Linux x86-64.

Use the existing Mission/Qwen observe-decide-execute loop. Add a separate Drone4D port and session service; never route sandbox actions through MAVLink or alter hardware enable switches. Keep the previous hardware APIs compatible. Frontend routes are sandbox simulation, hardware control, and the existing atomic-command workbench under hardware.

Drone4D v0.2.0 exposes gimbal poses and quantified perception, but does not provide RGB snapshots via REST. A small explicit integration module compiled into its native frontend exports a rendered FPV frame after the render phase, with actual camera intrinsics/extrinsics and simulation timestamp. A correlated request broker rejects missing/stale/wrong-vehicle captures. Closing or hiding the renderer must produce an explicit missing-observation error and hold, not substitute synthetic imagery.

A scenario builder places stationary people with visual red/blue hats in the native world. It owns world truth for scene construction/rendering only; the planner receives image, pose, telemetry and tool results, never target coordinates or upstream target IDs. The client executes bounded moves via Drone4D admitted goto/hover/gimbal commands, checks observed completion and converts ENU to the existing NED contract. The same grounding layer computes short approach steps and a conservative observation standoff. Native world path admission remains authoritative for obstacles.

The sandbox UI includes session lifecycle, native world/FPV, telemetry, observations and online decisions. Real Qwen errors remain explicit. Tests use controlled model outputs to verify logic, then actual isolated sandbox calls verify physics and rendered observations; neither substitutes for a successful live VLM run.
