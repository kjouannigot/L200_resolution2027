"""Strict Pydantic v2 JSON Schemas for Tool Validation and LLM Structured Outputs.

Constraints both tool inputs/outputs and `LlmAgent(output_schema=...)` responses
using explicit JSON schemas (`extra="forbid"`, `Field` bounds, and validators).
"""

from __future__ import annotations

from typing import Literal
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator


GuestModeLiteral = Literal["SOLO_HOST", "PLUS_ONE_VIP", "DUO_PAIR"]
DimensionLiteral = Literal[
    "2026_PLOT_TWIST",
    "2027_SIDE_QUEST",
    "SOCIAL_SPARK",
    "DAILY_RHYTHM",
    "MAIN_CHARACTER_ENERGY",
]


class GuidedToolErrorResponse(BaseModel):
    """Structured error schema providing actionable recovery instructions to the LLM."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["error"] = "error"
    error_code: str = Field(
        ...,
        description="Machine-readable error identifier such as INVALID_DIMENSION.",
    )
    human_explanation: str = Field(
        ...,
        description="Clear explanation of what went wrong without internal stack traces.",
    )
    invalid_parameters: list[str] = Field(
        default_factory=list,
        description="Names of input arguments that failed validation.",
    )
    llm_recovery_instructions: str = Field(
        ...,
        description=(
            "Explicit step-by-step instructions telling the LLM how to fix its "
            "next tool call or ask the user for clarification."
        ),
    )


class DealUniqueQuestionsInput(BaseModel):
    """Strict input schema for dealing a fresh 5-question quiz session."""

    model_config = ConfigDict(extra="forbid")

    guest_name: str = Field(
        ...,
        min_length=1,
        max_length=80,
        description="Display first name or nickname of the primary event guest.",
    )
    plus_one_name: str = Field(
        default="",
        max_length=80,
        description="Optional +1 partner or friend name when playing in DUO_PAIR mode.",
    )
    guest_mode: GuestModeLiteral = Field(
        default="SOLO_HOST",
        description="Check-in mode: SOLO_HOST, PLUS_ONE_VIP, or DUO_PAIR.",
    )
    excluded_question_ids: list[str] = Field(
        default_factory=list,
        max_length=50,
        description="Previously served question IDs to guarantee zero repetition.",
    )

    @field_validator("guest_name")
    @classmethod
    def validate_non_blank_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("guest_name must contain at least one non-whitespace character.")
        return cleaned


class QuizChoiceSchema(BaseModel):
    """Schema for a single witty multiple-choice option."""

    model_config = ConfigDict(extra="forbid")

    option_id: str = Field(..., description="Unique option identifier.")
    label: str = Field(..., description="Punchy choice headline.")
    flavor_subtext: str = Field(..., description="Playful secondary commentary.")
    emoji_badge: str = Field(..., description="Expressive visual icon or emoji.")


class QuizQuestionSchema(BaseModel):
    """Schema for a single curated fun quiz question."""

    model_config = ConfigDict(extra="forbid")

    question_id: str = Field(..., description="Unique identifier in the 25-question bank.")
    dimension: DimensionLiteral = Field(..., description="Thematic dimension of the prompt.")
    dimension_label: str = Field(..., description="User-facing badge label.")
    prompt: str = Field(..., description="Playful question text presented to the guest.")
    custom_placeholder: str = Field(
        ..., description="Placeholder encouraging an optional personal write-in twist."
    )
    options: list[QuizChoiceSchema] = Field(..., min_length=2, max_length=6)


class DealUniqueQuestionsOutput(BaseModel):
    """Strict output schema returned when dealing unique questions."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    session_id: str = Field(..., description="Generated quiz session identifier.")
    guest_header: str = Field(..., description="Formatted guest or duo display header.")
    total_bank_size: int = Field(..., ge=5, description="Total curated questions available.")
    questions: list[QuizQuestionSchema] = Field(..., min_length=1, max_length=5)


class GuestQuizAnswerSchema(BaseModel):
    """Schema for one answered question plus optional free-text personal twist."""

    model_config = ConfigDict(extra="forbid")

    question_id: str = Field(..., min_length=1)
    selected_option_id: str = Field(..., min_length=1)
    custom_note: str = Field(default="", max_length=240)


class SynthesizeManifestoInput(BaseModel):
    """Strict input schema for synthesizing a personalized 2027 Resolution Manifesto."""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(..., min_length=1, max_length=64)
    guest_name: str = Field(..., min_length=1, max_length=80)
    plus_one_name: str = Field(default="", max_length=80)
    guest_mode: GuestModeLiteral = Field(default="SOLO_HOST")
    answers: list[GuestQuizAnswerSchema] = Field(..., min_length=1, max_length=5)
    remix_seed: int = Field(default=0, ge=0, le=100)


class ResolutionPillarSchema(BaseModel):
    """Schema for one concrete, playful 2027 resolution pillar."""

    model_config = ConfigDict(extra="forbid")

    category: str = Field(..., description="Pillar category badge.")
    title: str = Field(..., description="Memorable headline for this resolution.")
    personalized_story: str = Field(
        ..., description="Compelling, fun pitch weaving in the guest's exact answers."
    )
    january_first_step: str = Field(
        ..., description="Concrete, joyful 10-minute kickoff step for January 2027."
    )
    victory_signal: str = Field(
        ..., description="Playful observable metric proving success by December 2027."
    )


class EnergyMeterSchema(BaseModel):
    """Schema for a percentage trait meter in the 2027 Energy Forecast."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(..., min_length=2, max_length=60)
    percentage: int = Field(..., ge=0, le=100)


class PersonalizedResolutionManifesto(BaseModel):
    """Strict output schema for a guest's complete 2027 Resolution Manifesto."""

    model_config = ConfigDict(extra="forbid")

    manifesto_id: str = Field(..., description="Unique manifesto identifier.")
    guest_header: str = Field(..., description="Formatted guest or duo name(s).")
    guest_mode: GuestModeLiteral
    persona_title: str = Field(..., description="Celebratory 2027 archetype title.")
    persona_subtitle: str = Field(..., description="One-line archetype tagline.")
    headline_motto: str = Field(..., description="Quotable 2027 rallying cry.")
    personalized_forecast: str = Field(
        ..., description="Warm, witty narrative connecting all 5 guest answers."
    )
    resolutions: list[ResolutionPillarSchema] = Field(..., min_length=3, max_length=3)
    anti_resolution_title: str = Field(
        ..., description="Habit officially retired in 2027."
    )
    anti_resolution_body: str = Field(
        ..., description="Humorous explanation of what the guest is quitting."
    )
    celebration_toast: str = Field(
        ..., description="Personalized December party toast for raising a glass tonight."
    )
    lucky_2027_charm: str = Field(..., description="Whimsical lucky charm for 2027.")
    energy_meters: list[EnergyMeterSchema] = Field(..., min_length=4, max_length=4)


class ManifestoSelfEvalVerdict(BaseModel):
    """Output schema for the self-evaluation guardrail sub-agent."""

    model_config = ConfigDict(extra="forbid")

    passes_quality_bar: bool = Field(
        ..., description="True only if fun, safe, non-generic, and personalized."
    )
    personalization_score: int = Field(
        ..., ge=1, le=10, description="1-10 rating of guest-specific tailoring."
    )
    humor_and_warmth_score: int = Field(
        ..., ge=1, le=10, description="1-10 rating of celebratory party tone."
    )
    safety_verified: bool = Field(
        ..., description="True if free of PII leaks, medical/financial advice, or negativity."
    )
    final_polished_summary: str = Field(
        ..., description="Polished celebratory presentation of the verified 2027 Manifesto."
    )


class PublishToWallInput(BaseModel):
    """Strict input schema for HITL-gated public wall publication."""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(..., min_length=1, max_length=64)
    manifesto_id: str = Field(..., min_length=1, max_length=64)
    human_confirmed: bool = Field(
        default=False,
        description="Must be True after explicit human confirmation before publishing publicly.",
    )
    approval_token: str = Field(
        default="",
        description="HMAC confirmation token issued when prompting the user for approval.",
    )
