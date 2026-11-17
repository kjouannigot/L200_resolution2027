"""Unit tests for Category 1: Tool & Interface Design (20 pts)."""

from __future__ import annotations

import inspect

from resolutions_agent.tools import deal_unique_social_quiz_questions
from resolutions_agent.tools import fetch_live_celebration_wall_highlights
from resolutions_agent.tools import publish_manifesto_to_public_celebration_wall
from resolutions_agent.tools import retrieve_guest_preference_memories
from resolutions_agent.tools import swap_single_dimension_question
from resolutions_agent.tools import synthesize_personalized_2027_manifesto


def test_all_tools_have_comprehensive_docstrings_and_specific_names() -> None:
    tools = [
        deal_unique_social_quiz_questions,
        swap_single_dimension_question,
        retrieve_guest_preference_memories,
        synthesize_personalized_2027_manifesto,
        publish_manifesto_to_public_celebration_wall,
        fetch_live_celebration_wall_highlights,
    ]
    for fn in tools:
        doc = inspect.getdoc(fn) or ""
        assert len(fn.__name__) >= 20
        assert "Args:" in doc
        assert "Returns:" in doc


def test_consecutive_guests_receive_distinct_questions() -> None:
    session_one = deal_unique_social_quiz_questions(
        guest_name="Camille",
        guest_mode="SOLO_HOST",
    )
    session_two = deal_unique_social_quiz_questions(
        guest_name="Devon",
        guest_mode="PLUS_ONE_VIP",
    )
    assert session_one["status"] == "ok"
    assert session_two["status"] == "ok"
    qids_one = {q["question_id"] for q in session_one["questions"]}
    qids_two = {q["question_id"] for q in session_two["questions"]}
    assert qids_one.isdisjoint(qids_two)


def test_guided_error_handling_returns_llm_recovery_instructions() -> None:
    bad_checkin = deal_unique_social_quiz_questions(
        guest_name="   ",
        guest_mode="INVALID_MODE",
    )
    assert bad_checkin["status"] == "error"
    assert "llm_recovery_instructions" in bad_checkin
    assert "SOLO_HOST" in bad_checkin["llm_recovery_instructions"]

    bad_swap = swap_single_dimension_question(dimension="NON_EXISTENT_DIM")
    assert bad_swap["status"] == "error"
    assert "llm_recovery_instructions" in bad_swap

    bad_limit = fetch_live_celebration_wall_highlights(limit=999)
    assert bad_limit["status"] == "error"
    assert "1 and 30" in bad_limit["llm_recovery_instructions"]
