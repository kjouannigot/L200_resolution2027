"""Multi-Agent ADK Architecture, Model Routing, Compaction & App Entrypoint.

Defines the 2027 Resolutions Atelier multi-agent system in Google ADK:
1. `unique_quiz_curator_agent` (`gemini-2.5-flash`): Low-latency dealing & swapping
   of unique, non-repeating fun questions per guest.
2. `manifesto_generation_pipeline` (`SequentialAgent`):
   - `resolution_composer_agent` (`gemini-2.5-pro`): Deep personalized synthesis
     across guest quiz answers, memories, and custom write-ins.
   - `manifesto_self_eval_agent` (`gemini-2.5-pro`, `output_schema=ManifestoSelfEvalVerdict`):
     Automated quality, humor, personalization, and safety self-evaluation gate.
3. `social_wall_concierge_agent` (`gemini-2.5-flash`): Live room highlights and
   Human-in-the-Loop (HITL) gated public wall publication.
4. `root_agent` (`resolutions_2027_coordinator`): Top-level coordinator routing
   across sub-agents and wrapped in an ADK `App` with `EventsCompactionConfig`,
   `ContextCacheConfig`, `ResumabilityConfig`, and AgentOps plugins.
"""

from __future__ import annotations

from google.adk.agents import LlmAgent
from google.adk.agents import SequentialAgent
from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.apps.app import App
from google.adk.apps.app import EventsCompactionConfig
from google.adk.apps.app import ResumabilityConfig
from google.adk.plugins.context_filter_plugin import ContextFilterPlugin
from google.adk.plugins.reflect_retry_tool_plugin import ReflectAndRetryToolPlugin

from .guardrails import FAST_MODEL
from .guardrails import PRO_MODEL
from .guardrails import SafetyAndPolicyGuardrailPlugin
from .observability import IntentOutcomeObservabilityPlugin
from .prompts import COORDINATOR_CONSTITUTION
from .prompts import MANIFESTO_COMPOSER_CONSTITUTION
from .prompts import QUIZ_CURATOR_CONSTITUTION
from .prompts import SELF_CRITIQUE_GUARDRAIL_CONSTITUTION
from .prompts import SOCIAL_WALL_CONCIERGE_CONSTITUTION
from .schemas import ManifestoSelfEvalVerdict
from .secrets import default_secret_provider
from .tools import deal_unique_social_quiz_questions
from .tools import fetch_live_celebration_wall_highlights
from .tools import publish_manifesto_wall_hitl_tool
from .tools import retrieve_guest_preference_memories
from .tools import swap_single_dimension_question
from .tools import synthesize_personalized_2027_manifesto

# Initialize secure Secret Manager / ADC configuration before agent runtime
default_secret_provider.configure_genai_environment()

# 1. Fast Interactive Sub-Agent (Gemini Flash) for Unique Question Dealing
unique_quiz_curator_agent = LlmAgent(
    name="unique_quiz_curator_agent",
    model=FAST_MODEL,
    description=(
        "Deals 5 unique, non-repeating fun questions for a new guest or +1 duo "
        "and swaps individual questions upon request."
    ),
    instruction=QUIZ_CURATOR_CONSTITUTION,
    tools=[
        deal_unique_social_quiz_questions,
        swap_single_dimension_question,
        retrieve_guest_preference_memories,
    ],
)

# 2A. Deep Creative Synthesis Sub-Agent (Gemini Pro)
resolution_composer_agent = LlmAgent(
    name="resolution_composer_agent",
    model=PRO_MODEL,
    description=(
        "Synthesizes a rich, personalized, hilarious 2027 Resolution Manifesto "
        "from a guest's quiz choices, memories, and custom notes."
    ),
    instruction=MANIFESTO_COMPOSER_CONSTITUTION,
    output_key="draft_manifesto_summary",
    tools=[
        synthesize_personalized_2027_manifesto,
        retrieve_guest_preference_memories,
    ],
)

# 2B. Automated Rubric & Safety Self-Evaluator Sub-Agent (Gemini Pro + Strict Schema)
manifesto_self_eval_agent = LlmAgent(
    name="manifesto_self_eval_agent",
    model=PRO_MODEL,
    description=(
        "Audits the drafted 2027 Resolution Manifesto for personalization fidelity, "
        "party humor, actionable January steps, and safety compliance."
    ),
    instruction=SELF_CRITIQUE_GUARDRAIL_CONSTITUTION,
    output_schema=ManifestoSelfEvalVerdict,
    output_key="verified_manifesto_verdict",
)

# 2. Sequential Pipeline combining Composition -> Self-Evaluation
manifesto_generation_pipeline = SequentialAgent(
    name="manifesto_generation_pipeline",
    description=(
        "End-to-end sequential pipeline that composes a personalized 2027 Resolution "
        "Manifesto and verifies it through a structured self-evaluation gate."
    ),
    sub_agents=[
        resolution_composer_agent,
        manifesto_self_eval_agent,
    ],
)

# 3. Social Celebration Wall Concierge Sub-Agent (Gemini Flash + HITL Confirmation)
social_wall_concierge_agent = LlmAgent(
    name="social_wall_concierge_agent",
    model=FAST_MODEL,
    description=(
        "Browses live Celebration Wall highlights and publishes a guest's manifesto "
        "only after explicit Human-in-the-Loop confirmation."
    ),
    instruction=SOCIAL_WALL_CONCIERGE_CONSTITUTION,
    tools=[
        fetch_live_celebration_wall_highlights,
        publish_manifesto_wall_hitl_tool,
    ],
)

# 4. Root Coordinator Agent
root_agent = LlmAgent(
    name="resolutions_2027_coordinator",
    model=FAST_MODEL,
    description=(
        "Coordinator agent for the December 2027 Resolutions Atelier—orchestrating "
        "unique fun quiz curation, personalized manifesto synthesis, self-eval, "
        "and HITL-gated Celebration Wall sharing."
    ),
    instruction=COORDINATOR_CONSTITUTION,
    tools=[
        deal_unique_social_quiz_questions,
        swap_single_dimension_question,
        retrieve_guest_preference_memories,
        synthesize_personalized_2027_manifesto,
        fetch_live_celebration_wall_highlights,
        publish_manifesto_wall_hitl_tool,
    ],
    sub_agents=[
        unique_quiz_curator_agent,
        manifesto_generation_pipeline,
        social_wall_concierge_agent,
    ],
)

# 5. Production ADK App with History Compaction, Context Caching, Resumability & Plugins
app = App(
    name="resolutions_2027_atelier",
    root_agent=root_agent,
    events_compaction_config=EventsCompactionConfig(
        compaction_interval=4,
        overlap_size=1,
        token_threshold=12000,
        event_retention_size=4,
    ),
    context_cache_config=ContextCacheConfig(
        cache_intervals=10,
        ttl_seconds=1800,
        min_tokens=2048,
    ),
    resumability_config=ResumabilityConfig(is_resumable=True),
    plugins=[
        ContextFilterPlugin(num_invocations_to_keep=6),
        ReflectAndRetryToolPlugin(max_retries=2),
        SafetyAndPolicyGuardrailPlugin(),
        IntentOutcomeObservabilityPlugin(),
    ],
)
