"""Semantic demo contract, deliberately separate from executable flight plans."""
from typing import Literal
from .contracts import Contract


class SearchTarget(Contract):
    category: Literal['person']
    attribute: Literal['wearing_red_hat']


class SearchIntent(Contract):
    task: Literal['search_and_observe_person']
    target: SearchTarget


def demo_proposal(query, intent):
    return {
        'query': query, 'intent': intent.model_dump(), 'executable': False,
        'profile': {'name': 'red_hat_demo', 'search_altitude_m': 50,
                    'scan_step_deg': 30, 'scan_frames': 12,
                    'initial_horizontal_distance_m': 80,
                    'stand_off_m': 20, 'move_step_m': 10},
        'provenance': {'intent': 'deepseek', 'vision': 'preset', 'telemetry': 'simulated'},
    }
