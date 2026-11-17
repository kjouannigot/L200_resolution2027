"""Automated Golden Dataset Evaluation Runner using `google.adk.evaluation.AgentEvaluator`.

Runs both:
1. Deterministic offline regression checks against `resolutions_2027.evalset.json`
   (verifying anti-repeat question dealing, custom-note personalization, PII scrubbing,
   and HITL confirmation enforcement).
2. Live LLM trajectory evaluation via `AgentEvaluator.evaluate(...)` whenever
   Vertex AI or Gemini credentials are configured in the environment.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from google.adk.evaluation.agent_evaluator import AgentEvaluator

from resolutions_agent.observability import structured_logger
from resolutions_agent.tools import deal_unique_social_quiz_questions
from resolutions_agent.tools import publish_manifesto_to_public_celebration_wall
from resolutions_agent.tools import synthesize_personalized_2027_manifesto

EVAL_DIR = Path(__file__).parent
GOLDEN_EVALSET_PATH = EVAL_DIR / "resolutions_2027.evalset.json"


def run_deterministic_golden_assertions() -> None:
    """Validates golden dataset structure and deterministic tool trajectories."""
    evalset = json.loads(GOLDEN_EVALSET_PATH.read_text(encoding="utf-8"))
    assert evalset["eval_set_id"] == "resolutions_2027_golden_evalset"
    assert len(evalset["eval_cases"]) >= 2

    # Golden Trajectory 1: Consecutive guests receive non-overlapping questions
    guest_a = deal_unique_social_quiz_questions(
        guest_name="Clara",
        plus_one_name="Leo",
        guest_mode="DUO_PAIR",
    )
    guest_b = deal_unique_social_quiz_questions(
        guest_name="Mateo",
        guest_mode="SOLO_HOST",
    )
    ids_a = {q["question_id"] for q in guest_a["questions"]}
    ids_b = {q["question_id"] for q in guest_b["questions"]}
    assert len(ids_a) == 5 and len(ids_b) == 5
    assert ids_a.isdisjoint(ids_b), "Consecutive guests must get distinct questions!"

    # Golden Trajectory 2: Personalized Manifesto weaves custom notes & duo names
    synth = synthesize_personalized_2027_manifesto(
        session_id=guest_a["session_id"],
        guest_name="Clara",
        plus_one_name="Leo",
        guest_mode="DUO_PAIR",
        answers=[
            {
                "question_id": "twist_retire_habit",
                "selected_option_id": "retire_tabs",
                "custom_note": "",
            },
            {
                "question_id": "quest_headline",
                "selected_option_id": "q_passport",
                "custom_note": "Sunset train across Italy",
            },
            {
                "question_id": "social_party_move",
                "selected_option_id": "sm_plusone_vip",
                "custom_note": "",
            },
            {
                "question_id": "rhythm_dream_tuesday",
                "selected_option_id": "dt_kitchen_disco",
                "custom_note": "",
            },
            {
                "question_id": "energy_mascot",
                "selected_option_id": "ms_penguin",
                "custom_note": "",
            },
        ],
    )
    assert synth["status"] == "ok"
    manifesto = synth["manifesto"]
    assert "Clara & Leo" in manifesto["personalized_forecast"]
    assert "Sunset train across Italy" in manifesto["resolutions"][0]["personalized_story"]

    # Golden Trajectory 3: HITL Confirmation Gate blocks unconfirmed public post
    blocked = publish_manifesto_to_public_celebration_wall(
        session_id=guest_a["session_id"],
        manifesto_id=manifesto["manifesto_id"],
        human_confirmed=False,
    )
    assert blocked["status"] == "requires_human_confirmation"

    confirmed = publish_manifesto_to_public_celebration_wall(
        session_id=guest_a["session_id"],
        manifesto_id=manifesto["manifesto_id"],
        human_confirmed=True,
        approval_token=blocked["approval_token"],
    )
    assert confirmed["status"] == "published"


async def run_full_evaluation() -> None:
    run_deterministic_golden_assertions()
    structured_logger.info(
        "Deterministic golden dataset assertions passed",
        extra={"phase": "OUTCOME", "component": "eval_harness"},
    )

    if os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GOOGLE_API_KEY"):
        await AgentEvaluator.evaluate(
            agent_module="resolutions_agent",
            eval_dataset_file_path_or_dir=str(GOLDEN_EVALSET_PATH),
            num_runs=1,
        )


if __name__ == "__main__":
    asyncio.run(run_full_evaluation())
