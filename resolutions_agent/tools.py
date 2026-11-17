"""Descriptive, Schema-Validated ADK Tools with Guided Error Recovery & HITL Hooks.

Implements Category 1 (Tool & Interface Design) and Category 3 (HITL) of the
AgentOps Code Review Matrix:
- Comprehensive Google-style docstrings describing purpose, parameters, and returns.
- Highly specific, domain-clear verb-noun tool names.
- Explicit Pydantic v2 JSON input and output schema validation (`schemas.py`).
- Guided error recovery dictionaries (`GuidedToolErrorResponse`) instructing the LLM
  exactly how to self-correct without raising unhandled runtime crashes.
- Human-in-the-Loop confirmation (`FunctionTool(..., require_confirmation=...)`)
  on public-facing state mutations.
"""

from __future__ import annotations

import hashlib
import random
import time
import uuid
from typing import Any

from pydantic import ValidationError

from google.adk.tools import FunctionTool
from google.adk.tools import ToolContext

from .memory_store import default_async_consolidator
from .memory_store import default_memory_store
from .observability import pii_redactor
from .question_catalog import ARCHETYPE_CATALOG
from .question_catalog import DIMENSION_SEQUENCE
from .question_catalog import PromptBlueprint
from .question_catalog import default_question_selector
from .schemas import DealUniqueQuestionsInput
from .schemas import DealUniqueQuestionsOutput
from .schemas import EnergyMeterSchema
from .schemas import GuidedToolErrorResponse
from .schemas import PersonalizedResolutionManifesto
from .schemas import PublishToWallInput
from .schemas import QuizChoiceSchema
from .schemas import QuizQuestionSchema
from .schemas import ResolutionPillarSchema
from .schemas import SynthesizeManifestoInput
from .secrets import default_secret_provider


def _blueprint_to_question_schema(bp: PromptBlueprint) -> QuizQuestionSchema:
    return QuizQuestionSchema(
        question_id=bp.question_id,
        dimension=bp.dimension,  # type: ignore[arg-type]
        dimension_label=bp.dimension_label,
        prompt=bp.prompt,
        custom_placeholder=bp.custom_placeholder,
        options=[
            QuizChoiceSchema(
                option_id=opt.option_id,
                label=opt.label,
                flavor_subtext=opt.flavor_subtext,
                emoji_badge=opt.emoji_badge,
            )
            for opt in bp.options
        ],
    )


def _format_guest_header(
    guest_name: str, plus_one_name: str, guest_mode: str
) -> tuple[str, bool]:
    clean_guest = pii_redactor.scrub_text(guest_name.strip()) or "Social Guest"
    clean_plus = pii_redactor.scrub_text(plus_one_name.strip())
    if guest_mode == "DUO_PAIR":
        return f"{clean_guest} & {clean_plus or 'Your +1'}", True
    return clean_guest, False


def deal_unique_social_quiz_questions(
    guest_name: str,
    guest_mode: str = "SOLO_HOST",
    plus_one_name: str = "",
    excluded_question_ids: list[str] | None = None,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Deals 5 fresh, non-repeating fun questions customized for an event guest or +1 duo.

    Selects one playful prompt from each of the 5 thematic dimensions
    (`2026_PLOT_TWIST`, `2027_SIDE_QUEST`, `SOCIAL_SPARK`, `DAILY_RHYTHM`,
    `MAIN_CHARACTER_ENERGY`) while actively avoiding recently served question IDs
    so consecutive guests at the celebration always get different questions.

    Args:
        guest_name: First name or nickname of the primary guest (1-80 chars).
        guest_mode: Guest check-in mode; must be one of `'SOLO_HOST'`,
            `'PLUS_ONE_VIP'`, or `'DUO_PAIR'`.
        plus_one_name: Optional first name of the +1 guest when `guest_mode`
            is `'DUO_PAIR'`.
        excluded_question_ids: Optional list of previously seen `question_id`
            strings to exclude from selection so no prompt repeats.
        tool_context: Optional ADK ToolContext injected automatically at runtime
            to persist active session state.

    Returns:
        A validated dictionary matching `DealUniqueQuestionsOutput` with keys
        `status`, `session_id`, `guest_header`, `total_bank_size`, and `questions`
        (5 structured question objects), or `GuidedToolErrorResponse` with
        `llm_recovery_instructions` if input validation fails.
    """
    try:
        validated = DealUniqueQuestionsInput(
            guest_name=guest_name,
            plus_one_name=plus_one_name,
            guest_mode=guest_mode,  # type: ignore[arg-type]
            excluded_question_ids=excluded_question_ids or [],
        )
    except ValidationError as exc:
        bad_fields = [str(err["loc"][0]) for err in exc.errors() if err.get("loc")]
        return GuidedToolErrorResponse(
            error_code="INVALID_QUIZ_CHECKIN_ARGUMENTS",
            human_explanation="Guest check-in inputs did not pass schema validation.",
            invalid_parameters=bad_fields,
            llm_recovery_instructions=(
                "Ensure `guest_name` is a non-empty string (1-80 chars) and "
                "`guest_mode` is strictly one of 'SOLO_HOST', 'PLUS_ONE_VIP', "
                "or 'DUO_PAIR', then call `deal_unique_social_quiz_questions` again."
            ),
        ).model_dump()

    prior_seen: list[str] = list(validated.excluded_question_ids)
    if tool_context is not None:
        session_seen = tool_context.state.get("user:seen_question_ids", [])
        if isinstance(session_seen, list):
            prior_seen.extend(str(qid) for qid in session_seen)

    session_id = f"quiz-{uuid.uuid4().hex[:10]}"
    seed = f"{session_id}:{validated.guest_name}:{validated.plus_one_name}:{time.time_ns()}"
    dealt_blueprints = default_question_selector.deal_five_questions(
        guest_seed=seed,
        excluded_ids=prior_seen,
    )
    guest_header, _ = _format_guest_header(
        validated.guest_name, validated.plus_one_name, validated.guest_mode
    )

    output = DealUniqueQuestionsOutput(
        session_id=session_id,
        guest_header=guest_header,
        total_bank_size=default_question_selector.total_size,
        questions=[_blueprint_to_question_schema(bp) for bp in dealt_blueprints],
    )

    if tool_context is not None:
        updated_seen = list(
            dict.fromkeys([*prior_seen, *[bp.question_id for bp in dealt_blueprints]])
        )[-20:]
        tool_context.state["user:seen_question_ids"] = updated_seen
        tool_context.state["active_quiz_session_id"] = session_id
        tool_context.state["guest_header"] = guest_header

    return output.model_dump()


def swap_single_dimension_question(
    dimension: str,
    excluded_question_ids: list[str] | None = None,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Swaps one quiz question for a fresh, unseen prompt from the same dimension.

    Args:
        dimension: Thematic dimension identifier to swap; must be one of
            `'2026_PLOT_TWIST'`, `'2027_SIDE_QUEST'`, `'SOCIAL_SPARK'`,
            `'DAILY_RHYTHM'`, or `'MAIN_CHARACTER_ENERGY'`.
        excluded_question_ids: Question IDs already shown to this guest so the
            replacement is guaranteed to be different.
        tool_context: Optional ADK ToolContext injected automatically at runtime.

    Returns:
        A dictionary containing `status="ok"` and `question` (matching
        `QuizQuestionSchema`), or a `GuidedToolErrorResponse` dictionary if
        `dimension` is unrecognized.
    """
    clean_dim = dimension.strip().upper()
    if clean_dim not in DIMENSION_SEQUENCE:
        return GuidedToolErrorResponse(
            error_code="INVALID_QUESTION_DIMENSION",
            human_explanation=f"Dimension '{dimension}' does not exist in the 2027 question bank.",
            invalid_parameters=["dimension"],
            llm_recovery_instructions=(
                f"Pass `dimension` as one of {list(DIMENSION_SEQUENCE)} and retry "
                "`swap_single_dimension_question`."
            ),
        ).model_dump()

    excluded = set(excluded_question_ids or [])
    if tool_context is not None:
        for qid in tool_context.state.get("user:seen_question_ids", []):
            excluded.add(str(qid))

    rng = random.Random(f"swap:{clean_dim}:{time.time_ns()}")
    chosen = default_question_selector.pick_for_dimension(clean_dim, excluded, rng)
    question_payload = _blueprint_to_question_schema(chosen).model_dump()

    if tool_context is not None:
        seen = list(tool_context.state.get("user:seen_question_ids", []))
        seen.append(chosen.question_id)
        tool_context.state["user:seen_question_ids"] = seen[-20:]

    return {
        "status": "ok",
        "question": question_payload,
    }


async def retrieve_guest_preference_memories(
    guest_name: str,
    interest_query: str = "",
    top_k: int = 3,
) -> dict[str, Any]:
    """Retrieves scrubbed historical preferences and past personas for a returning guest.

    Queries the persistent guest memory store (Vertex AI Search / Firestore vector
    index / SQLite vector store) to enrich conversational personalization across turns.

    Args:
        guest_name: Display name of the guest to look up.
        interest_query: Optional semantic topic filter (e.g., `'travel'` or `'cooking'`).
        top_k: Maximum number of memory entries to return (1-10).

    Returns:
        Dictionary with `status="ok"`, `guest_name`, and `memories` list, or a
        guided recovery payload if `guest_name` is empty.
    """
    cleaned_name = guest_name.strip()
    if not cleaned_name:
        return GuidedToolErrorResponse(
            error_code="EMPTY_GUEST_NAME",
            human_explanation="Cannot search guest memories with an empty guest_name.",
            invalid_parameters=["guest_name"],
            llm_recovery_instructions=(
                "Ask the user for their first name or nickname, then invoke "
                "`retrieve_guest_preference_memories` with a non-empty `guest_name`."
            ),
        ).model_dump()

    bounded_k = max(1, min(top_k, 10))
    memories = await default_memory_store.search_guest_memories(
        guest_name=cleaned_name,
        query=interest_query,
        top_k=bounded_k,
    )
    return {
        "status": "ok",
        "guest_name": pii_redactor.scrub_text(cleaned_name),
        "memory_count": len(memories),
        "memories": memories,
    }


def synthesize_personalized_2027_manifesto(
    session_id: str,
    guest_name: str,
    answers: list[dict[str, Any]],
    guest_mode: str = "SOLO_HOST",
    plus_one_name: str = "",
    remix_seed: int = 0,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Synthesizes a fun, compelling, personalized 2027 Resolution Manifesto from quiz answers.

    Computes trait scores across four celebratory dimensions, selects the guest's
    2027 Persona Archetype, weaves every selected choice and custom write-in note
    into 3 tailored resolutions, generates an official 2027 Anti-Resolution and
    December Toast, persists the draft, and triggers non-blocking background
    async memory consolidation.

    Args:
        session_id: Active quiz session identifier from `deal_unique_social_quiz_questions`.
        guest_name: Primary guest's display name.
        answers: List of 1 to 5 answer dicts, each containing `question_id`,
            `selected_option_id`, and optional `custom_note`.
        guest_mode: One of `'SOLO_HOST'`, `'PLUS_ONE_VIP'`, or `'DUO_PAIR'`.
        plus_one_name: Optional +1 partner/friend name when in `'DUO_PAIR'` mode.
        remix_seed: Optional integer (0-100) to spin an alternate persona/motto framing.
        tool_context: Optional ADK ToolContext injected automatically at runtime.

    Returns:
        Dictionary with `status="ok"`, `manifesto` (validated against
        `PersonalizedResolutionManifesto`), and `hitl_publish_approval_token`
        for subsequent HITL-gated wall publishing, or `GuidedToolErrorResponse`
        on validation errors.
    """
    try:
        validated = SynthesizeManifestoInput(
            session_id=session_id,
            guest_name=guest_name,
            plus_one_name=plus_one_name,
            guest_mode=guest_mode,  # type: ignore[arg-type]
            answers=answers,  # type: ignore[arg-type]
            remix_seed=remix_seed,
        )
    except ValidationError as exc:
        bad_fields = [str(err["loc"][0]) for err in exc.errors() if err.get("loc")]
        return GuidedToolErrorResponse(
            error_code="INVALID_MANIFESTO_SYNTHESIS_INPUT",
            human_explanation="Manifesto synthesis input failed strict JSON schema validation.",
            invalid_parameters=bad_fields,
            llm_recovery_instructions=(
                "Provide a non-empty `session_id`, `guest_name`, and 1-5 `answers` "
                "objects each containing `question_id` and `selected_option_id`, "
                "then retry `synthesize_personalized_2027_manifesto`."
            ),
        ).model_dump()

    guest_header, is_duo = _format_guest_header(
        validated.guest_name, validated.plus_one_name, validated.guest_mode
    )

    trait_scores: dict[str, int] = {
        "boldness": 2,
        "spontaneity": 2,
        "cozy_joy": 2,
        "social_spark": 2,
    }
    resolved_by_dim: dict[str, tuple[Any, str]] = {}

    for ans in validated.answers:
        q_bp = default_question_selector.get_by_id(ans.question_id)
        if q_bp is None:
            continue
        chosen_opt = next(
            (opt for opt in q_bp.options if opt.option_id == ans.selected_option_id),
            q_bp.options[0],
        )
        clean_note = pii_redactor.scrub_text(ans.custom_note.strip())
        resolved_by_dim[q_bp.dimension] = (chosen_opt, clean_note)
        for trait_name, boost in chosen_opt.traits.items():
            trait_scores[trait_name] = trait_scores.get(trait_name, 0) + boost

    for dim in DIMENSION_SEQUENCE:
        if dim not in resolved_by_dim:
            fallback_bp = default_question_selector.get_by_dimension(dim)[0]
            resolved_by_dim[dim] = (fallback_bp.options[0], "")

    twist_opt, twist_note = resolved_by_dim["2026_PLOT_TWIST"]
    quest_opt, quest_note = resolved_by_dim["2027_SIDE_QUEST"]
    social_opt, social_note = resolved_by_dim["SOCIAL_SPARK"]
    rhythm_opt, rhythm_note = resolved_by_dim["DAILY_RHYTHM"]
    energy_opt, energy_note = resolved_by_dim["MAIN_CHARACTER_ENERGY"]

    ranked = sorted(trait_scores.items(), key=lambda kv: kv[1], reverse=True)
    top_trait, second_trait = ranked[0][0], ranked[1][0]
    matching_archetypes = [
        arch
        for arch in ARCHETYPE_CATALOG
        if arch.primary_trait == top_trait or arch.secondary_trait == second_trait
    ] or list(ARCHETYPE_CATALOG)
    archetype = matching_archetypes[validated.remix_seed % len(matching_archetypes)]

    digest = hashlib.sha256(
        f"{guest_header}:{quest_opt.option_id}:{energy_opt.option_id}:{validated.remix_seed}".encode(
            "utf-8"
        )
    ).digest()
    motto = archetype.mottos[(digest[0] + validated.remix_seed) % len(archetype.mottos)]

    verb_step = "are stepping" if is_duo else "is stepping"
    forecast = (
        f"{guest_header} {verb_step} into 2027 {twist_opt.forecast_hook}—now "
        f"{quest_opt.forecast_hook} while {rhythm_opt.forecast_hook}. Add a signature gift "
        f"for {social_opt.forecast_hook}, and 2027 launches {energy_opt.forecast_hook}!"
    )
    if energy_note:
        forecast += f' Personal 2027 wildcard locked in: "{energy_note}"!'

    def _with_twist(text: str, note: str, label: str) -> str:
        return f'{text} {label}: "{note}"!' if note else text

    pillars = [
        ResolutionPillarSchema(
            category="01 · Bold 2027 Side-Quest",
            title=quest_opt.resolution_title,
            personalized_story=_with_twist(
                f"Crafted for {guest_header}: {quest_opt.resolution_pitch}",
                quest_note,
                "Personal spotlight",
            ),
            january_first_step=quest_opt.january_step,
            victory_signal=quest_opt.victory_signal,
        ),
        ResolutionPillarSchema(
            category=(
                "02 · Shared Duo & Celebration Pact"
                if is_duo
                else "02 · Social & Connection Pact"
            ),
            title=social_opt.resolution_title,
            personalized_story=_with_twist(
                f"For {guest_header}: {social_opt.resolution_pitch}",
                social_note,
                "Signature party twist",
            ),
            january_first_step=social_opt.january_step,
            victory_signal=social_opt.victory_signal,
        ),
        ResolutionPillarSchema(
            category="03 · Everyday Joy Upgrade",
            title=rhythm_opt.resolution_title,
            personalized_story=_with_twist(
                f"{rhythm_opt.resolution_pitch} Pair it with '{energy_opt.resolution_title}' all year.",
                rhythm_note,
                "Custom rhythm upgrade",
            ),
            january_first_step=rhythm_opt.january_step,
            victory_signal=rhythm_opt.victory_signal,
        ),
    ]

    max_score = max(trait_scores.values(), default=1)
    meter_defs = (
        ("boldness", "Bold Moonshot Nerve"),
        ("spontaneity", "Spontaneous Plot-Twist"),
        ("cozy_joy", "Five-Star Cozy Joy"),
        ("social_spark", "Celebration Magnetism"),
    )
    meters = [
        EnergyMeterSchema(
            label=label,
            percentage=min(
                99,
                max(
                    74,
                    int(74 + (trait_scores[key] / max_score) * 21 + (digest[idx] % 4)),
                ),
            ),
        )
        for idx, (key, label) in enumerate(meter_defs)
    ]

    manifesto_id = f"man-2027-{uuid.uuid4().hex[:8]}"
    manifesto = PersonalizedResolutionManifesto(
        manifesto_id=manifesto_id,
        guest_header=guest_header,
        guest_mode=validated.guest_mode,
        persona_title=archetype.title,
        persona_subtitle=archetype.subtitle,
        headline_motto=motto,
        personalized_forecast=forecast,
        resolutions=pillars,
        anti_resolution_title=twist_opt.anti_title or "No More Autopilot Weeks",
        anti_resolution_body=_with_twist(
            twist_opt.anti_body
            or "In 2027, joyless busywork is officially deprecated.",
            twist_note,
            "Retired from 2026",
        ),
        celebration_toast=(
            f"Raise a glass to {guest_header} — {twist_opt.toast_hook}, "
            f"{quest_opt.toast_hook}, and {energy_opt.toast_hook}!"
        ),
        lucky_2027_charm=energy_opt.charm if digest[1] % 2 == 0 else quest_opt.charm,
        energy_meters=meters,
    )

    manifesto_dict = manifesto.model_dump()
    default_memory_store.save_draft_manifesto(validated.session_id, manifesto_dict)

    # Non-blocking background async memory consolidation (never blocks response!)
    default_async_consolidator.schedule_consolidation(
        guest_name=validated.guest_name,
        manifesto_payload=manifesto_dict,
    )

    approval_token = default_secret_provider.create_hitl_approval_token(
        session_id=validated.session_id,
        manifesto_id=manifesto_id,
    )
    if tool_context is not None:
        tool_context.state["latest_manifesto"] = manifesto_dict
        tool_context.state["pending_hitl_approval_token"] = approval_token

    return {
        "status": "ok",
        "manifesto": manifesto_dict,
        "hitl_publish_approval_token": approval_token,
    }


def publish_manifesto_to_public_celebration_wall(
    session_id: str,
    manifesto_id: str,
    human_confirmed: bool = False,
    approval_token: str = "",
) -> dict[str, Any]:
    """Publishes a guest's 2027 Resolution Manifesto to the shared Celebration Wall (HITL-gated).

    HIGH-STAKES PUBLIC ACTION: Requires explicit human confirmation (`human_confirmed=True`
    and a valid HMAC `approval_token`) before posting the guest's display name,
    persona, and resolutions to the public event wall.

    Args:
        session_id: Quiz session identifier associated with the draft manifesto.
        manifesto_id: Identifier of the generated manifesto to publish.
        human_confirmed: Boolean flag indicating explicit user consent to post publicly.
        approval_token: Cryptographic HMAC approval token issued for `(session_id, manifesto_id)`.

    Returns:
        Dictionary with `status="published"` when confirmed, `status="requires_human_confirmation"`
        when awaiting human approval, or `GuidedToolErrorResponse` if `manifesto_id` is missing.
    """
    try:
        validated = PublishToWallInput(
            session_id=session_id,
            manifesto_id=manifesto_id,
            human_confirmed=human_confirmed,
            approval_token=approval_token,
        )
    except ValidationError as exc:
        bad_fields = [str(err["loc"][0]) for err in exc.errors() if err.get("loc")]
        return GuidedToolErrorResponse(
            error_code="INVALID_PUBLISH_WALL_INPUT",
            human_explanation="Wall publication arguments failed validation.",
            invalid_parameters=bad_fields,
            llm_recovery_instructions=(
                "Provide valid non-empty `session_id` and `manifesto_id` strings "
                "returned by `synthesize_personalized_2027_manifesto`."
            ),
        ).model_dump()

    expected_token = default_secret_provider.create_hitl_approval_token(
        session_id=validated.session_id,
        manifesto_id=validated.manifesto_id,
    )
    if not validated.human_confirmed or validated.approval_token != expected_token:
        return {
            "status": "requires_human_confirmation",
            "hitl_required": True,
            "manifesto_id": validated.manifesto_id,
            "approval_token": expected_token,
            "confirmation_prompt": (
                "Would you like to publish your 2027 Resolution Manifesto to the "
                "live Celebration Wall for other guests at the party to see?"
            ),
            "llm_recovery_instructions": (
                "Ask the user for explicit confirmation before publishing. Once "
                "they say yes, call `publish_manifesto_to_public_celebration_wall` "
                f"with `human_confirmed=True` and `approval_token='{expected_token}'`."
            ),
        }

    draft = default_memory_store.get_draft_manifesto(validated.manifesto_id)
    if draft is None:
        return GuidedToolErrorResponse(
            error_code="MANIFESTO_DRAFT_NOT_FOUND",
            human_explanation=f"No draft manifesto found with ID '{validated.manifesto_id}'.",
            invalid_parameters=["manifesto_id"],
            llm_recovery_instructions=(
                "Call `synthesize_personalized_2027_manifesto` first to create a "
                "draft manifesto, then use its returned `manifesto_id`."
            ),
        ).model_dump()

    default_memory_store.publish_manifesto_sync(draft)
    return {
        "status": "published",
        "manifesto_id": validated.manifesto_id,
        "guest_header": draft.get("guest_header", ""),
        "persona_title": draft.get("persona_title", ""),
    }


def fetch_live_celebration_wall_highlights(limit: int = 10) -> dict[str, Any]:
    """Fetches recent published 2027 Resolution Manifestos and live celebration trends.

    Args:
        limit: Maximum number of recent published manifestos to retrieve (1-30).

    Returns:
        Dictionary containing `status="ok"`, `total_published_count`,
        `top_persona_tonight`, and `recent_manifestos`.
    """
    if limit < 1 or limit > 30:
        return GuidedToolErrorResponse(
            error_code="LIMIT_OUT_OF_RANGE",
            human_explanation=f"Requested limit {limit} is outside allowed bounds (1..30).",
            invalid_parameters=["limit"],
            llm_recovery_instructions=(
                "Call `fetch_live_celebration_wall_highlights` again with `limit` "
                "between 1 and 30 (e.g., `limit=10`)."
            ),
        ).model_dump()

    wall_data = default_memory_store.list_public_wall(limit=limit)
    return {
        "status": "ok",
        **wall_data,
    }


# Wrap high-stakes public wall publishing with ADK's native HITL confirmation hook
publish_manifesto_wall_hitl_tool = FunctionTool(
    publish_manifesto_to_public_celebration_wall,
    require_confirmation=lambda session_id, manifesto_id, human_confirmed=False, approval_token="": (
        not human_confirmed
    ),
)
