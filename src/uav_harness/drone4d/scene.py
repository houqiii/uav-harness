"""Scenario construction is separate from perception/planning; truth stays in the renderer."""

import json
import random


def search_scene(source_dir, seed=17):
    root = json.loads(
        (source_dir / "scenarios/sandbox/synthcity/t6_manual.json").read_text()
    )
    # Independently selected scenario placement; never sent to the VLM.
    roads = [
        [-230.0, -300.0, None],
        [0.0, -140.0, None],
        [160.0, -300.0, None],
        [180.0, -300.0, None],
        [170.0, -300.0, None],
    ]
    spawn = random.Random(seed).choice(roads)
    return {
        "schema": "awr.sandbox.snapshot.v1",
        "contracts": "1.2.0",
        "world_id": "synthcity",
        "template_id": "t6_manual",
        "calendar": {"start_utc": "2026-10-16T02:00:00Z", "tz": "Asia/Shanghai"},
        "seed": seed,
        "acoustic_bg": "rural",
        "env": {"preset": "clear", "overrides": {}},
        "models": [],
        "nests": root["nests"],
        "vehicles": [
            {
                "id": "harness-01",
                "model_id": "p600_mid360",
                "nest_id": "nest-a",
                "label": "意图任务机",
            }
        ],
        "tasks": [],
        # Candidate road positions roughly 200 m from the launch nest.
        "targets": [
            {
                "id": "subject-a",
                "label": "场景人物 A",
                "class": "person",
                "pos": spawn,
                "motion": {"mode": "static"},
                "sensitivity": [],
            },
        ],
    }


def visual_styles():
    # Appearance is supplied only to the scene renderer, never to the model observation.
    return {"subject-a": {"hat": "red"}}
