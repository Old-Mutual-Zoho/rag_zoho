from src.api.escalation import router as escalation_router
from src.api.endpoints.payments import payments_api
from src.api.endpoints.policies import policies_api
from src.api.endpoints.premiums import premiums_api
from src.api.endpoints.quotes_underwriting import api as quotes_underwriting_api
from src.api.endpoints.agent_webhook import router as agent_webhook_router, slack_service
import src.api.escalation as escalation_module
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from fastapi import Request
api_router = APIRouter()
"""
FastAPI application - Main entry point
"""

from dotenv import load_dotenv

load_dotenv()

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from typing import Any, Dict, List, Optional
from collections import defaultdict, Counter

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, ValidationError
from src.chatbot.dependencies import api_key_protection

from src.chatbot.modes.conversational import ConversationalMode
from src.chatbot.modes.guided import GuidedMode
from src.chatbot.product_cards import ProductCardGenerator
from src.chatbot.router import ChatRouter
from src.chatbot.flows.registry import get_flow_steps
from src.chatbot.state_manager import StateManager
from src.chatbot.validation import FormValidationError
from src.rag.generate import MiaGenerator
from src.rag.query import retrieve_context
from src.utils.product_matcher import ProductMatcher
from src.utils.rag_config_loader import load_rag_config
from src.api.endpoints.mock_underwriting import router as mock_router
from src.api.endpoints.mock_premiums import router as mock_premiums_router
from src.api.validate_flow import router as validate_flow_router

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize FastAPI app
app = FastAPI(
    title="Old Mutual Chatbot API",
    description="AI-powered insurance chatbot with conversational and guided modes",
    version="1.0.0",
    dependencies=[Depends(api_key_protection)],  # protect everything by default
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================================
# DEPENDENCY INJECTION
# ============================================================================

# Initialize databases: use real Postgres/Redis when env is set, else in-memory stubs
if os.getenv("DATABASE_URL") and os.getenv("USE_POSTGRES_CONVERSATIONS", "").lower() in ("1", "true", "yes"):
    from src.database.postgres_real import PostgresDB

    postgres_db = PostgresDB(connection_string=os.environ["DATABASE_URL"])
else:
    from src.database.postgres import PostgresDB

    postgres_db = PostgresDB()

if os.getenv("REDIS_URL"):
    from src.database.redis_real import RedisCache

    redis_cache = RedisCache(url=os.environ["REDIS_URL"])
else:
    from src.database.redis import RedisCache

    redis_cache = RedisCache()

state_manager = StateManager(redis_cache, postgres_db)

escalation_module.state_manager = state_manager
# Register escalation router
app.include_router(escalation_router, prefix="/api/v1")

# Register payments API router
app.include_router(payments_api, prefix="/api/v1/payments", tags=["Payments"])
# Register policies API router
app.include_router(policies_api, prefix="/api/v1/policies", tags=["Policies"])

# Register premiums API router
app.include_router(premiums_api, prefix="/api/v1/premiums", tags=["Premiums"])

# Register quotes and underwriting API router
app.include_router(quotes_underwriting_api, prefix="/api")

# Register agent webhook router
app.include_router(agent_webhook_router, prefix="/api/v1")

# Register flow validation endpoints (per-field and per-step)
app.include_router(validate_flow_router, prefix="/api/v1", tags=["Flow Validation"])

product_matcher = ProductMatcher()

# Load RAG configuration once per process
rag_cfg = load_rag_config()


class APIRAGAdapter:
    """
    Thin async-compatible wrapper around the existing RAG query pipeline.
    """

    def __init__(self):
        self.cfg = rag_cfg
        # For pgvector, ensure the vector table exists once at startup using the
        # configured embedding dimensionality, instead of running DDL on every
        # request inside the hot retrieval path.
        try:
            from src.rag.query import _vector_store_from_config as _vs_from_cfg  # type: ignore

            store = _vs_from_cfg(self.cfg)
            store_class = type(store).__name__
            if store_class == "PgVectorStore" and hasattr(store, "ensure_table"):
                # Use the configured output dimensionality when available; fall
                # back to a sane default if not set.
                dim = getattr(self.cfg.embeddings, "output_dimensionality", None) or 1536
                store.ensure_table(int(dim))
        except Exception as e:  # pragma: no cover - best effort safeguard
            logger.warning("Failed to pre-initialize vector table: %s", e)

    async def retrieve(self, query: str, filters: Optional[Dict] = None, top_k: Optional[int] = None):
        k = self.cfg.retrieval.top_k if top_k is None else top_k
        return retrieve_context(question=query, cfg=self.cfg, top_k=k, filters=filters)

    async def generate(
        self,
        query: str,
        context_docs: List[Dict],
        conversation_history: List[Dict],
        original_question: Optional[str] = None,
    ):
        """
        Use the configured generation backend (Gemini by default) to
        produce an answer grounded in the retrieved context.
        """
        def _retrieval_stats() -> Dict[str, float]:
            if not context_docs:
                return {"avg_score": 0.0, "coverage": 0.0}
            scores = [float(h.get("score") or 0.0) for h in context_docs]
            avg_score = sum(scores) / len(scores) if scores else 0.0
            coverage = min(len(context_docs) / 5.0, 1.0)
            return {"avg_score": avg_score, "coverage": coverage}

        def _compute_confidence() -> float:
            stats = _retrieval_stats()
            avg_score = stats["avg_score"]
            coverage = stats["coverage"]
            min_score = 0.55
            # Normalize avg_score into 0..1 confidence band.
            if avg_score <= 0:
                score_norm = 0.0
            else:
                score_norm = (avg_score - min_score) / max(1.0 - min_score, 0.01)
                score_norm = max(0.0, min(1.0, score_norm))
            conf = (0.7 * score_norm) + (0.3 * coverage)
            return float(max(0.0, min(1.0, round(conf, 3))))

        def _extractive_answer() -> Dict[str, Any]:
            """
            Fallback: build an answer directly from known product chunks when
            the generator is unavailable or fails.

            Priority:
            - Use payload["text"] from retrieved hits when present.
            - If missing, but doc_id is known, load sections from website_chunks.jsonl
              and stitch together overview + benefits for that product.
            - As a last resort, resolve individual chunk IDs from website_chunks.jsonl.
            """
            confidence = _compute_confidence()
            snippets: List[str] = []

            # 1) Use any text already present on the hits.
            for h in context_docs:
                payload = h.get("payload") or {}
                text = (payload.get("text") or "").strip()
                if text:
                    snippets.append(text)

            # 2) If still empty, try structured product sections file by doc_id.
            if len(snippets) < 1:
                from typing import Set

                doc_ids: Set[str] = set()
                for h in context_docs:
                    p = h.get("payload") or {}
                    doc_id = p.get("doc_id")
                    if not doc_id or doc_id in doc_ids:
                        continue
                    doc_ids.add(doc_id)
                    try:
                        sections = _load_product_sections(doc_id)
                    except Exception as e:  # pragma: no cover - best-effort only
                        logger.warning("Failed to load sections for %s: %s", doc_id, e)
                        continue

                    overview = sections.get("overview") or []
                    benefits = sections.get("benefits") or []

                    if overview:
                        # Take the first overview chunk as the core "what is X" answer.
                        ov_text = (overview[0].get("text") or "").strip()
                        if ov_text:
                            snippets.append(ov_text)

                    if benefits:
                        # Add a concise benefits line if available.
                        b0 = (benefits[0].get("text") or "").strip()
                        if b0:
                            snippets.append(b0)

            # 3) As a last resort, try to resolve individual chunk IDs directly
            #    from website_chunks.jsonl when doc_id-based lookup fails or when
            #    the stored vector payloads are missing "text".
            if len(snippets) < 1:
                try:
                    chunks_path = Path(__file__).parent.parent.parent / "data" / "processed" / "website_chunks.jsonl"
                    if chunks_path.exists():
                        wanted_ids = set()
                        for h in context_docs:
                            cid = h.get("id") or (h.get("payload") or {}).get("id")
                            if cid:
                                wanted_ids.add(cid)

                        if wanted_ids:
                            with open(chunks_path, "r", encoding="utf-8") as f:
                                for line in f:
                                    if not line.strip():
                                        continue
                                    try:
                                        data = json.loads(line)
                                    except Exception:
                                        continue
                                    if data.get("id") not in wanted_ids:
                                        continue
                                    heading = data.get("section_heading") or ""
                                    raw_text = data.get("text") or ""
                                    text = _strip_heading_from_text(raw_text, heading)
                                    text = (text or "").strip()
                                    if text:
                                        snippets.append(text)
                except Exception as e:  # pragma: no cover - defensive
                    logger.warning("Chunk-id extractive fallback failed: %s", e)

            answer_text = "\n\n".join(snippets).strip() or "I'm not sure based on the available information."
            return {"answer": answer_text, "confidence": confidence, "sources": context_docs}

        # If generation is globally disabled, always fall back to extractive mode.
        if not self.cfg.generation.enabled:
            return _extractive_answer()

        stats = _retrieval_stats()
        # If retrieval is weak or empty, avoid LLM and fall back to extractive mode.
        # Keep a low threshold so generation still runs when chunks are reasonably relevant.
        if not context_docs or stats["avg_score"] < 0.2:
            return _extractive_answer()

        if self.cfg.generation.backend == "gemini":
            # Use the new async Gemini generator (MiaGenerator). If the model call
            # fails or returns our generic phone number fallback, degrade to a
            # context-only extractive answer instead of surfacing the error text.
            try:
                mia = MiaGenerator()
                question_for_generation = (original_question or query or "").strip() or query
                answer = await mia.generate(question_for_generation, context_docs, conversation_history)
            except Exception as e:  # pragma: no cover - defensive; MiaGenerator already logs
                logger.error("Gemini generation unavailable, falling back to extractive answer: %s", e, exc_info=True)
                return _extractive_answer()

            lowered_answer = (answer or "").strip().lower()
            if (not lowered_answer) or ("i'm having trouble retrieving those details" in lowered_answer):
                # LLM unavailable / failed -> use extractive context instead.
                return _extractive_answer()

            return {"answer": answer, "confidence": _compute_confidence(), "sources": context_docs}

        # Unsupported backend -> degrade gracefully to extractive answer.
        return _extractive_answer()


rag_adapter = APIRAGAdapter()

conversational_mode = ConversationalMode(rag_adapter, product_matcher, state_manager)
guided_mode = GuidedMode(state_manager, product_matcher, postgres_db)
chat_router = ChatRouter(conversational_mode, guided_mode, state_manager, product_matcher)
product_card_gen = ProductCardGenerator(product_matcher, rag_adapter)


def get_db():
    """Dependency for database sessions"""
    return postgres_db


def get_redis():
    """Dependency for Redis cache"""
    return redis_cache


def get_router():
    """Dependency for chat router"""
    return chat_router


GENERAL_INFO_ALIASES: Dict[str, str] = {
    "motor_private": "motor-insurance",
    "motor_vehicle": "motor-insurance",
    "motor": "motor-insurance",
    "travel": "travel-sure-plus-insurance",
    "travel_insurance": "travel-sure-plus-insurance",
}


def _normalize_general_info_key(value: str) -> str:
    key = (value or "").strip().lower()
    if not key:
        return ""

    key = key.replace("\\", "/")
    key = key.split("?", 1)[0].split("#", 1)[0]

    prefix = "website:product:"
    if key.startswith(prefix):
        key = key[len(prefix):]

    key = key.strip("/")
    if "/" in key:
        key = key.split("/")[-1]

    return key.strip()


def _general_info_candidate_paths(product: str, product_dir: Path) -> List[Path]:
    normalized = _normalize_general_info_key(product)

    candidate_ids: List[str] = []

    alias_target = GENERAL_INFO_ALIASES.get(normalized)
    if alias_target:
        candidate_ids.append(alias_target)

    candidate_ids.extend(
        [
            normalized,
            normalized.replace("_", "-"),
            normalized.replace("-", "_"),
        ]
    )

    deduped_ids: List[str] = []
    seen: set[str] = set()
    for candidate in candidate_ids:
        clean_candidate = (candidate or "").strip()
        if not clean_candidate or clean_candidate in seen:
            continue
        seen.add(clean_candidate)
        deduped_ids.append(clean_candidate)

    return [product_dir / f"{candidate_id}.json" for candidate_id in deduped_ids]


# ============================================================================
# REQUEST/RESPONSE MODELS
# ============================================================================

from src.chatbot.controllers.motor_private_controller import MOTOR_PRIVATE_VEHICLE_MAKE_OPTIONS


@app.get("/api/v1/motor-private/vehicle-makes", tags=["Motor Private"])
async def get_motor_private_vehicle_makes():
    """
    Get the list of vehicle make options for Motor Private.
    """
    return {"options": MOTOR_PRIVATE_VEHICLE_MAKE_OPTIONS}


class ChatMessage(BaseModel):
    """Chat request. Use form_data when the frontend submits a step form (e.g. Personal Accident)."""

    message: str = ""
    session_id: Optional[str] = None
    user_id: str
    metadata: Optional[Dict] = None
    form_data: Optional[Dict] = None  # Step form payload; when set, used as user_input in guided flows


class ChatResponse(BaseModel):
    response: Dict
    session_id: str
    mode: str
    timestamp: str


class QuoteRequest(BaseModel):
    product_id: str
    user_id: str
    underwriting_data: Dict


class QuoteResponse(BaseModel):
    quote_id: str
    product_name: str
    monthly_premium: float
    sum_assured: float
    valid_until: str


class PersonalAccidentFullFormRequest(BaseModel):
    """Submit the full Personal Accident form in a single payload (no guided steps)."""

    user_id: str = Field(..., description="External user identifier (e.g. phone number)")
    data: Dict[str, Any] = Field(..., description="Flattened form fields for Personal Accident application")


class PersonalAccidentFullFormResponse(BaseModel):
    quote_id: str
    product_name: str
    monthly_premium: float
    annual_premium: float
    sum_assured: float
    breakdown: Dict[str, Any]


class MotorPrivateFullFormRequest(BaseModel):
    """Submit the full Motor Private form in a single payload (no guided steps)."""

    user_id: str = Field(..., description="External user identifier (e.g. phone number)")
    data: Dict[str, Any] = Field(..., description="Flattened form fields for Motor Private quote")


class MotorPrivateFullFormResponse(BaseModel):
    quote_id: str
    product_name: str
    total_premium: float
    breakdown: Dict[str, Any]


class TravelInsuranceFullFormRequest(BaseModel):
    """Submit the full Travel Insurance form in a single payload (no guided steps)."""

    user_id: str = Field(..., description="External user identifier (e.g. phone number)")
    data: Dict[str, Any] = Field(..., description="Flattened form fields for Travel Insurance application")


class TravelInsuranceFullFormResponse(BaseModel):
    quote_id: str
    product_name: str
    total_premium_ugx: float
    total_premium_usd: float
    breakdown: Dict[str, Any]


class SerenicareFullFormRequest(BaseModel):
    """Submit the full Serenicare form in a single payload (no guided steps)."""

    user_id: str = Field(..., description="External user identifier (e.g. phone number)")
    data: Dict[str, Any] = Field(..., description="Flattened form fields for Serenicare application")


class SerenicareFullFormResponse(BaseModel):
    quote_id: str
    product_name: str
    monthly_premium: float
    annual_premium: float
    breakdown: Dict[str, Any]


class CreateSessionRequest(BaseModel):
    """Create a new chatbot session (e.g. when user opens the app)."""

    user_id: str = Field(..., description="User identifier (e.g. phone number or auth id)")


class CreateSessionResponse(BaseModel):
    session_id: str
    user_id: str


class StartGuidedRequest(BaseModel):
    """Start a guided flow (e.g. Personal Accident). Session is created if session_id is omitted."""

    flow_name: str = Field(..., description="Flow id, e.g. 'personal_accident'")
    user_id: str
    session_id: Optional[str] = None
    initial_data: Optional[Dict] = Field(default_factory=dict, description="Optional pre-filled data for the flow")


class CSATFeedbackRequest(BaseModel):
    """Post-conversation CSAT feedback."""

    rating: int = Field(..., ge=1, le=5, description="CSAT rating from 1 to 5")
    feedback: Optional[str] = Field(default="", description="Optional user feedback text")
    session_id: Optional[str] = Field(default=None, description="Chat session id")
    user_id: Optional[str] = Field(default=None, description="External user id (optional)")
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict)

# ============================================================================
# ENDPOINTS
# ============================================================================


@app.get("/", tags=["Health"])
async def root():
    """Health check endpoint."""
    return {"service": "Old Mutual Chatbot API", "status": "healthy", "version": "1.0.0", "timestamp": datetime.now().isoformat()}


@app.get("/health", tags=["Health"])
async def health_check():
    """Detailed health check (Postgres, Redis)."""
    return {"status": "healthy", "database": {"postgres": "connected", "redis": redis_cache.ping()}, "timestamp": datetime.now().isoformat()}


@api_router.get("/metrics/rag", tags=["Metrics"])
async def get_rag_metrics(
    limit: int = Query(50, ge=1, le=500),
    conversation_id: Optional[str] = None,
    db: PostgresDB = Depends(get_db),
):
    metrics = db.get_recent_rag_metrics(limit=limit, conversation_id=conversation_id)
    return {
        "count": len(metrics),
        "metrics": [
            {
                "id": m.id,
                "conversation_id": m.conversation_id,
                "metric_type": m.metric_type,
                "value": m.value,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in metrics
        ],
    }


@api_router.get("/metrics/system-performance", tags=["Metrics"])
async def get_system_performance_metrics(
    days: int = Query(7, ge=1, le=90),
    db: PostgresDB = Depends(get_db),
):
    """
    System performance KPIs for the admin dashboard.

    Computes escalation rate, AI resolution rate, and payment success rate
    for the last `days` days, with change vs the previous period.
    """
    now = datetime.utcnow()
    current_start = now - timedelta(days=days)
    previous_start = current_start - timedelta(days=days)

    def _rate(numerator: int, denominator: int) -> float:
        return round((numerator / denominator) * 100, 2) if denominator > 0 else 0.0

    def _delta(current: float, previous: float) -> float:
        return round(current - previous, 2)

    # Escalation rate (user-confirmed)
    current_conversations = db.count_conversations(current_start, now)
    previous_conversations = db.count_conversations(previous_start, current_start)
    current_escalations = len(
        db.list_conversation_events(
            start=current_start,
            end=now,
            event_type="escalation_confirmed",
            limit=50000,
        )
    )
    previous_escalations = len(
        db.list_conversation_events(
            start=previous_start,
            end=current_start,
            event_type="escalation_confirmed",
            limit=50000,
        )
    )

    escalation_rate = _rate(current_escalations, current_conversations)
    escalation_rate_prev = _rate(previous_escalations, previous_conversations)
    escalation_change = _delta(escalation_rate, escalation_rate_prev)

    # AI resolution rate: proxy as non-escalated share of conversations
    resolution_rate = _rate(max(current_conversations - current_escalations, 0), current_conversations)
    resolution_rate_prev = _rate(max(previous_conversations - previous_escalations, 0), previous_conversations)
    resolution_change = _delta(resolution_rate, resolution_rate_prev)

    # Payment success rate: success / (success + failed) within window
    current_success = db.count_payment_transactions(current_start, now, ["SUCCESS", "COMPLETED"])
    current_failed = db.count_payment_transactions(current_start, now, ["FAILED", "ERROR"])
    previous_success = db.count_payment_transactions(previous_start, current_start, ["SUCCESS", "COMPLETED"])
    previous_failed = db.count_payment_transactions(previous_start, current_start, ["FAILED", "ERROR"])

    payment_rate = _rate(current_success, current_success + current_failed)
    payment_rate_prev = _rate(previous_success, previous_success + previous_failed)
    payment_change = _delta(payment_rate, payment_rate_prev)

    label_suffix = f"vs previous {days} days"
    return {
        "kpis": [
            {
                "label": "Escalation Rate",
                "value": f"{escalation_rate:.2f}%",
                "change": escalation_change,
                "invertTrend": True,
                "changeLabel": label_suffix,
            },
            {
                "label": "AI Resolution Rate",
                "value": f"{resolution_rate:.2f}%",
                "change": resolution_change,
                "changeLabel": label_suffix,
            },
            {
                "label": "Payment Success",
                "value": f"{payment_rate:.2f}%",
                "change": payment_change,
                "changeLabel": label_suffix,
            },
        ]
    }


@api_router.get("/metrics/ai-performance", tags=["Metrics"])
async def get_ai_performance_metrics(
    days: int = Query(30, ge=1, le=180),
    db: PostgresDB = Depends(get_db),
):
    """
    Aggregated AI performance metrics for the admin dashboard.
    """
    now = datetime.utcnow()
    current_start = now - timedelta(days=days)
    previous_start = current_start - timedelta(days=days)

    def _avg(values: List[float]) -> float:
        return round(sum(values) / len(values), 4) if values else 0.0

    def _rate(num: int, denom: int) -> float:
        return round((num / denom) * 100, 2) if denom > 0 else 0.0

    def _delta(curr: float, prev: float) -> float:
        return round(curr - prev, 2)

    def _fmt_pct(value: float, digits: int = 1) -> str:
        return f"{value:.{digits}f}%"

    def _fmt_delta(value: float, digits: int = 1) -> str:
        sign = "+" if value > 0 else ""
        return f"{sign}{value:.{digits}f}%"

    def _fmt_seconds(value: float) -> str:
        return f"{value:.1f}s"

    def _fmt_duration(seconds: float) -> str:
        if seconds <= 0:
            return "0s"
        mins = int(seconds // 60)
        secs = int(seconds % 60)
        if mins <= 0:
            return f"{secs}s"
        return f"{mins}m {secs}s"

    def _pct_change(curr: float, prev: float) -> float:
        return round(((curr - prev) / prev) * 100, 2) if prev > 0 else 0.0

    def _format_count(value: int) -> str:
        return f"{value:,}"

    def _window_metrics(start: datetime, end: datetime) -> Dict[str, Any]:
        rag = db.list_rag_metrics(
            start=start,
            end=end,
            metric_types=["retrieval_accuracy", "confidence_score", "response_latency", "fallbacks"],
            limit=50000,
        )
        by_type: Dict[str, List[float]] = defaultdict(list)
        for m in rag:
            by_type[m.metric_type].append(float(m.value))

        accuracy = _avg(by_type["retrieval_accuracy"]) * 100
        confidence = _avg(by_type["confidence_score"]) * 100
        latency = _avg(by_type["response_latency"])
        fallbacks = len(by_type["fallbacks"])

        conversations = db.count_conversations(start, end)
        escalations = len(
            db.list_conversation_events(
                start=start,
                end=end,
                event_type="escalation_confirmed",
                limit=50000,
            )
        )
        agent_joins = len(
            db.list_conversation_events(
                start=start,
                end=end,
                event_type="agent_joined",
                limit=50000,
            )
        )

        escalation_rate = _rate(escalations, conversations)

        # Resolution rate: proxy as non-escalated share of conversations.
        resolution_rate = _rate(max(conversations - escalations, 0), conversations)
        fallback_rate = _rate(fallbacks, conversations)

        return {
            "accuracy": accuracy,
            "confidence": confidence,
            "latency": latency,
            "fallback_rate": fallback_rate,
            "escalation_rate": escalation_rate,
            "resolution_rate": resolution_rate,
            "agent_join_rate": _rate(agent_joins, conversations),
            "conversations": conversations,
        }

    current = _window_metrics(current_start, now)
    previous = _window_metrics(previous_start, current_start)

    csat_events_current = db.list_conversation_events(
        start=current_start,
        end=now,
        event_type="csat",
        limit=5000,
    )
    csat_events_previous = db.list_conversation_events(
        start=previous_start,
        end=current_start,
        event_type="csat",
        limit=5000,
    )
    csat_current_vals = [float((e.payload or {}).get("rating", 0)) for e in csat_events_current]
    csat_prev_vals = [float((e.payload or {}).get("rating", 0)) for e in csat_events_previous]
    csat_current = _avg([v for v in csat_current_vals if v > 0])
    csat_previous = _avg([v for v in csat_prev_vals if v > 0])
    csat_delta = round(csat_current - csat_previous, 2)

    # Rated accuracy (proxy): ratings 4-5 are treated as "accurate"
    rated_threshold = 4
    rated_current = [v for v in csat_current_vals if v > 0]
    rated_prev = [v for v in csat_prev_vals if v > 0]
    rated_current_correct = len([v for v in rated_current if v >= rated_threshold])
    rated_prev_correct = len([v for v in rated_prev if v >= rated_threshold])
    rated_accuracy_current = _rate(rated_current_correct, len(rated_current))
    rated_accuracy_prev = _rate(rated_prev_correct, len(rated_prev))
    rated_accuracy_delta = _delta(rated_accuracy_current, rated_accuracy_prev)
    rated_coverage = _rate(len(rated_current), current["conversations"])

    top_metrics = [
        {
            "label": "AI Accuracy (Rated)",
            "value": _fmt_pct(rated_accuracy_current),
            "delta": _fmt_delta(rated_accuracy_delta),
            "tone": "positive" if rated_accuracy_current >= rated_accuracy_prev else "negative",
        },
        {
            "label": "AI Resolution Rate",
            "value": _fmt_pct(current["resolution_rate"]),
            "delta": _fmt_delta(_delta(current["resolution_rate"], previous["resolution_rate"])),
            "tone": "positive" if current["resolution_rate"] >= previous["resolution_rate"] else "negative",
        },
        {
            "label": "Fallback Rate",
            "value": _fmt_pct(current["fallback_rate"]),
            "delta": _fmt_delta(_delta(current["fallback_rate"], previous["fallback_rate"])),
            "tone": "positive" if current["fallback_rate"] <= previous["fallback_rate"] else "negative",
        },
        {
            "label": "Agent Pickup Rate",
            "value": _fmt_pct(current["agent_join_rate"]),
            "delta": _fmt_delta(_delta(current["agent_join_rate"], previous["agent_join_rate"])),
            "tone": "positive" if current["agent_join_rate"] >= previous["agent_join_rate"] else "negative",
        },
        {
            "label": "Avg Response Time",
            "value": _fmt_seconds(current["latency"]),
            "delta": f"{_delta(current['latency'], previous['latency']):+.1f}s",
            "tone": "positive" if current["latency"] <= previous["latency"] else "negative",
        },
    ]

    # Trend data (accuracy + fallback) for last 7 days
    trend_days = 7
    trend_start = now - timedelta(days=trend_days)
    trend_metrics = db.list_rag_metrics(
        start=trend_start,
        end=now,
        metric_types=["retrieval_accuracy", "fallbacks", "confidence_score"],
        limit=50000,
    )
    trend_by_day: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
    for m in trend_metrics:
        label = m.created_at.strftime("%a")
        trend_by_day[label][m.metric_type].append(float(m.value))

    trend_data = []
    for i in range(trend_days):
        day = (trend_start + timedelta(days=i)).strftime("%a")
        acc = _avg(trend_by_day[day]["retrieval_accuracy"]) * 100
        responses = len(trend_by_day[day]["confidence_score"])
        fallbacks = len(trend_by_day[day]["fallbacks"])
        fallback_rate = _rate(fallbacks, responses)
        trend_data.append(
            {"day": day, "accuracy": round(acc, 1), "fallback": round(fallback_rate, 1)}
        )

    # Conversation quality metrics
    msg_stats = db.list_conversation_message_stats(current_start, now)
    durations = []
    message_counts = []
    for row in msg_stats:
        if row["min_ts"] and row["max_ts"]:
            durations.append((row["max_ts"] - row["min_ts"]).total_seconds())
        message_counts.append(row.get("message_count", 0))

    avg_duration = _avg(durations) if durations else 0.0
    avg_messages = _avg(message_counts) if message_counts else 0.0
    completion_rate = current["resolution_rate"]
    drop_off_rate = current["fallback_rate"]

    quality_metrics = [
        {
            "label": "Total Handled",
            "value": _format_count(current["conversations"]),
            "change": _fmt_delta(_pct_change(current["conversations"], previous["conversations"]), 0),
        },
        {"label": "AI Resolution Rate", "value": _fmt_pct(completion_rate), "change": _fmt_delta(_delta(completion_rate, previous["resolution_rate"]))},
        {"label": "Fallback Rate", "value": _fmt_pct(drop_off_rate), "change": _fmt_delta(_delta(drop_off_rate, previous["fallback_rate"]))},
        {"label": "Avg Length", "value": _fmt_duration(avg_duration), "change": _fmt_delta(0.0, 0)},
        {"label": "Avg Messages", "value": f"{avg_messages:.1f}", "change": _fmt_delta(0.0, 0)},
    ]

    quality_metrics.extend(
        [
            {
                "label": "User CSAT",
                "value": f"{csat_current:.1f}/5" if csat_current > 0 else "N/A",
                "change": f"{csat_delta:+.1f}" if csat_current > 0 else "0",
            },
            {
                "label": "Agent Pickup Rate",
                "value": _fmt_pct(current["agent_join_rate"]),
                "change": _fmt_delta(_delta(current["agent_join_rate"], previous.get("agent_join_rate", 0.0))),
            },
            {
                "label": "Accuracy Coverage",
                "value": _fmt_pct(rated_coverage),
                "change": _fmt_delta(0.0, 0),
            },
            {
                "label": "Rated Samples",
                "value": _format_count(len(rated_current)),
                "change": _fmt_delta(0.0, 0),
            },
        ]
    )

    # Intent recognition performance
    intent_events = db.list_conversation_events(
        start=current_start,
        end=now,
        event_type="intent",
        limit=20000,
    )
    intent_groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for ev in intent_events:
        payload = ev.payload or {}
        key = str(payload.get("intent") or "unknown")
        intent_groups[key].append({"payload": payload, "created_at": ev.created_at})

    def _trend_series(events: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        series = {}

        # Today (4-hour buckets)
        start_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        buckets = []
        for i in range(0, 24, 4):
            label = f"{i:02d}:00"
            end_bucket = start_today + timedelta(hours=i + 4)
            start_bucket = start_today + timedelta(hours=i)
            count = sum(1 for e in events if start_bucket <= e["created_at"] < end_bucket)
            buckets.append({"label": label, "inquiries": count})
        series["today"] = buckets

        # Weekly (last 7 days)
        weekly = []
        week_start = now - timedelta(days=6)
        for i in range(7):
            day = (week_start + timedelta(days=i))
            label = day.strftime("%a")
            day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
            day_end = day_start + timedelta(days=1)
            count = sum(1 for e in events if day_start <= e["created_at"] < day_end)
            weekly.append({"label": label, "inquiries": count})
        series["weekly"] = weekly

        # Monthly (last 4 weeks)
        monthly = []
        for i in range(4):
            start_week = now - timedelta(days=(27 - i * 7))
            end_week = start_week + timedelta(days=7)
            count = sum(1 for e in events if start_week <= e["created_at"] < end_week)
            monthly.append({"label": f"Week {i + 1}", "inquiries": count})
        series["monthly"] = monthly

        # Yearly (last 12 months)
        yearly = []
        for i in range(12):
            month_start = (now.replace(day=1) - timedelta(days=30 * (11 - i))).replace(day=1)
            month_end = (month_start + timedelta(days=32)).replace(day=1)
            count = sum(1 for e in events if month_start <= e["created_at"] < month_end)
            yearly.append({"label": month_start.strftime("%b"), "inquiries": count})
        series["yearly"] = yearly

        return series

    intent_rows = []
    for intent_key, events in sorted(intent_groups.items(), key=lambda item: len(item[1]), reverse=True)[:5]:
        confidences = [float(e["payload"].get("confidence", 0.0)) for e in events]
        response_latencies = [float(e["payload"].get("response_latency", 0.0)) for e in events if e["payload"].get("response_latency") is not None]
        top_regions = [str(e["payload"].get("region", "Unknown")) for e in events if e["payload"].get("region")]
        products = [e["payload"].get("product_name") for e in events if e["payload"].get("product_name")]
        product_counts = Counter([p for p in products if p])
        product_breakdown = [
            {"product": name, "inquiries": count, "share": f"{round((count / len(events)) * 100)}%"}
            for name, count in product_counts.most_common(4)
        ]

        hours = [e["created_at"].hour for e in events]
        peak_hour = max(hours, key=hours.count) if hours else 0
        peak_window = f"{peak_hour:02d}:00 - {min(peak_hour + 2, 23):02d}:00 UTC"

        intent_rows.append(
            {
                "category": intent_key.replace("_", " ").title(),
                "volume": _format_count(len(events)),
                "accuracy": round(_avg(confidences) * 100, 1),
                "details": {
                    "timeRange": f"Last {days} days",
                    "peakWindow": peak_window,
                    "avgHandleTime": _fmt_seconds(_avg(response_latencies)),
                    "firstResponse": _fmt_seconds(_avg(response_latencies)),
                    "confidenceBand": f"{round(min(confidences) * 100, 0) if confidences else 0}% - {round(max(confidences) * 100, 0) if confidences else 0}%",
                    "topRegions": top_regions[:3] or ["Unknown"],
                    "productBreakdown": product_breakdown,
                    "trendSeries": _trend_series(events),
                },
            }
        )

    # RAG retrieval performance
    rag_metrics = db.list_rag_metrics(
        start=current_start,
        end=now,
        metric_types=["retrieval_accuracy", "confidence_score", "response_latency", "fallbacks"],
        limit=50000,
    )
    rag_by_type: Dict[str, List[float]] = defaultdict(list)
    for m in rag_metrics:
        rag_by_type[m.metric_type].append(float(m.value))
    retrieval_success = _avg(rag_by_type["retrieval_accuracy"]) * 100
    avg_latency_ms = _avg(rag_by_type["response_latency"]) * 1000
    doc_relevance = _avg(rag_by_type["confidence_score"]) * 10

    rag_context_rows = [
        {"doc": "Retrieval Accuracy", "accuracy": _fmt_pct(retrieval_success, 1)},
        {"doc": "Confidence Score", "accuracy": f"{doc_relevance:.1f}/10"},
        {"doc": "Fallback Rate", "accuracy": _fmt_pct(current["fallback_rate"], 1)},
    ]

    weakness_rows = []
    low_conf_events = [e for e in intent_events if float(e.payload.get("confidence", 1.0)) < 0.4]
    for e in low_conf_events[:6]:
        conf = float(e.payload.get("confidence", 0.0))
        if conf < 0.3:
            severity = "high"
        elif conf < 0.4:
            severity = "medium"
        else:
            severity = "low"
        weakness_rows.append(
            {
                "category": (e.payload.get("intent") or "Unknown").replace("_", " ").title(),
                "query": (e.payload.get("user_message") or "")[:60],
                "severity": severity,
                "frequency": 1,
            }
        )

    # Learning opportunities
    learning_ops = [
        {"title": "Unanswered Gaps", "note": f"{len(low_conf_events)} low-confidence topics detected."},
        {"title": "Suggested Intents", "note": "Review high-volume intents for consolidation."},
        {"title": "Training Needs", "note": "Check confidence band drops in recent intents."},
    ]

    # Escalation analysis
    escalations = db.list_escalations(current_start, now)
    reason_counts = Counter([(e.escalation_reason or "Unspecified") for e in escalations])
    total_escalations = sum(reason_counts.values()) or 1
    escalation_reasons = [
        {
            "reason": reason,
            "cases": count,
            "progress": round((count / total_escalations) * 100),
            "tone": "good" if count / total_escalations < 0.5 else "bad",
        }
        for reason, count in reason_counts.most_common(4)
    ]

    # Model health
    last_hour_metrics = db.list_rag_metrics(
        start=now - timedelta(hours=1),
        end=now,
        metric_types=["response_latency", "fallbacks", "confidence_score"],
        limit=5000,
    )
    last_hour_requests = len([m for m in last_hour_metrics if m.metric_type == "confidence_score"])
    model_health = [
        {
            "label": "AI Service Uptime",
            "value": "Online" if last_hour_metrics else "N/A",
            "note": "Last hour status",
            "status": "Online" if last_hour_metrics else "Unknown",
        },
        {"label": "Inference Latency", "value": f"{avg_latency_ms:.0f}ms", "note": "Avg over period", "status": "Online"},
        {"label": "API Request Volume", "value": f"{last_hour_requests}/hr", "note": "Recent demand", "status": "Online"},
        {"label": "AI Error Rate", "value": _fmt_pct(current["fallback_rate"], 1), "note": "Fallbacks as proxy", "status": "Online"},
    ]

    return {
        "topMetrics": top_metrics,
        "accuracyRatedMeta": {
            "coverage": _fmt_pct(rated_coverage),
            "samples": _format_count(len(rated_current)),
        },
        "trendData": trend_data,
        "qualityMetrics": quality_metrics,
        "intentRows": intent_rows,
        "ragContextRows": rag_context_rows,
        "ragWeaknessRows": weakness_rows,
        "learningOps": learning_ops,
        "escalationReasons": escalation_reasons,
        "modelHealth": model_health,
    }


@api_router.post("/metrics/csat", tags=["Metrics"])
async def post_csat_feedback(
    body: CSATFeedbackRequest,
    db: PostgresDB = Depends(get_db),
):
    session_id = (body.session_id or "").strip() or None
    user_id = (body.user_id or "").strip() or None
    conversation_id: Optional[str] = None

    if session_id:
        session = state_manager.get_session(session_id) or {}
        conversation_id = session.get("conversation_id") or session_id

    if not conversation_id and user_id:
        # No active session; store against user id for analytics
        conversation_id = user_id

    if not conversation_id:
        raise HTTPException(status_code=400, detail="session_id or user_id is required")

    payload = {
        "rating": int(body.rating),
        "feedback": (body.feedback or "").strip(),
        "session_id": session_id,
        "user_id": user_id,
        **(body.metadata or {}),
    }

    try:
        db.add_conversation_event(
            conversation_id=conversation_id,
            event_type="csat",
            payload=payload,
        )
    except Exception as exc:
        logger.error("Failed to store CSAT feedback: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to store feedback")

    return {"success": True}


async def _handle_chat_message(request: ChatMessage, router: ChatRouter, db: PostgresDB) -> ChatResponse:
    """Shared logic for chat message. In conversational mode uses same RAG retrieval as run_rag (config top_k, synonyms, re-ranking)."""
    # Resolve external identifier (e.g. phone) to internal user UUID so session/conversation creation never hits FK violation
    user = db.get_or_create_user(phone_number=request.user_id)
    internal_user_id = str(user.id)

    session_id = request.session_id
    if not session_id:
        session_id = state_manager.create_session(internal_user_id)
    else:
        # If a provided session_id no longer exists in Redis (e.g., TTL expired),
        # create a fresh session and return that id so the client can continue consistently.
        existing_session = state_manager.get_session(session_id)
        if not existing_session:
            session_id = state_manager.create_session(internal_user_id)

    # Route message (form_data from frontend is used as user_input in guided flows)
    # Conversational path uses APIRAGAdapter.retrieve() with cfg.retrieval.top_k, synonym expansion, re-ranking
    response = await router.route(
        message=request.message or "",
        session_id=session_id,
        user_id=internal_user_id,
        form_data=request.form_data,
        db=db,
    )

    # In escalated mode, hand over to human agent: mirror every user text message to
    # the session thread in Slack and seed recent bot/user context once for the agent.
    if response.get("mode") == "escalated" and (request.message or "").strip():
        try:
            session_for_thread = state_manager.get_session(session_id) or {}
            already_seeded = bool(session_for_thread.get("agent_thread_seeded"))

            if not already_seeded:
                conv_id = session_for_thread.get("conversation_id")
                if conv_id:
                    history = db.get_conversation_history(conv_id, limit=12)
                    if history:
                        if not slack_service.thread_exists(session_id):
                            slack_service.send_history_message(session_id, "Context before human-agent handoff:")
                        for msg in reversed(history):
                            role = (getattr(msg, "role", "") or "").lower()
                            content = (getattr(msg, "content", "") or "").strip()
                            if not content:
                                continue
                            if role == "assistant" and content.startswith("Message sent to human agent"):
                                continue
                            who = "User" if role == "user" else "Bot"
                            slack_service.send_history_message(session_id, f"{who}: {content}")
                state_manager.update_session(session_id, {"agent_thread_seeded": True})

            slack_service.send_message(chat_id=session_id, message=(request.message or "").strip(), sender="client")
            response["forwarded_to_agent"] = True
        except Exception as e:
            logger.warning("Failed to mirror client message to Slack for session %s: %s", session_id, e)
            response["forwarded_to_agent"] = False
            response["forward_error"] = str(e)

    # Save message to database and update Redis message cache
    session = state_manager.get_session(session_id)
    if session:
        user_content = json.dumps(request.form_data) if request.form_data else request.message
        db.add_message(
            conversation_id=session["conversation_id"],
            role="user",
            content=user_content,
            metadata=request.metadata or {},
        )
        # Build the rolling Redis message buffer
        cached_messages = list(session.get("recent_messages") or [])
        cached_messages.append({"role": "user", "content": user_content})

        # In escalated mode, avoid storing repeated bot acknowledgements as assistant replies.
        if response.get("mode") != "escalated":
            resp_val = response.get("response")
            if isinstance(resp_val, dict):
                assistant_content = resp_val.get("response") or resp_val.get("message") or str(resp_val)
            else:
                assistant_content = str(resp_val)
            db.add_message(
                conversation_id=session["conversation_id"],
                role="assistant",
                content=assistant_content,
                metadata={"mode": response.get("mode")},
            )
            cached_messages.append({"role": "assistant", "content": assistant_content})

        # Persist last 10 messages (5 turns) back to Redis so follow-up turns
        # can read history without hitting PostgreSQL.
        state_manager.update_session(session_id, {"recent_messages": cached_messages[-10:]})

    return ChatResponse(response=response, session_id=session_id, mode=response.get("mode", "conversational"), timestamp=datetime.now().isoformat())


@api_router.get("/general-information", tags=["General Information"])
async def get_general_information(
    request: Request,
    product: str,
    redis=Depends(get_redis)
):
    """
    Serve general information for a product.
    - product: product key (e.g. serenicare, motor_private, personal_accident, travel)
    Returns: definition, benefits, eligibility for the product.
    """

    logger = logging.getLogger("general_information")
    logger.info(f"General info request: product={product}")

    try:
        # --- Resolve product JSON path safely ---
        BASE_DIR = Path(__file__).resolve().parents[2]  # D:\ZOHO\rag
        PRODUCT_DIR = BASE_DIR / "general_information" / "product_json"

        product_file: Optional[Path] = None
        for candidate in _general_info_candidate_paths(product, PRODUCT_DIR):
            if candidate.exists():
                product_file = candidate
                break

        if product_file is None:
            logger.error(f"Product file not found: {product_file}")
            raise HTTPException(status_code=404, detail="Product information not found")

        logger.info(f"Resolved product file path: {product_file}")

        # --- Load JSON ---
        try:
            with open(product_file, "r", encoding="utf-8") as f:
                info = json.load(f)
        except Exception as e:
            logger.error(f"Failed to load product file {product_file}: {e}")
            raise HTTPException(status_code=500, detail="Failed to load product information")

        logger.info(f"General info served for product={product}")
        return JSONResponse(content=info)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)} (product={product})")
        raise HTTPException(status_code=500, detail="Internal server error")

# ---------- API router (prefix /api) ----------
# api_router = APIRouter()  # app-level dependency covers these too now


@api_router.post("/session", response_model=CreateSessionResponse, tags=["Sessions"])
async def create_session(
    body: CreateSessionRequest,
    db: PostgresDB = Depends(get_db),
):
    """Create a new chat session. Returns session_id for later requests."""
    try:
        user = db.get_or_create_user(phone_number=body.user_id)
        session_id = state_manager.create_session(str(user.id))
        return CreateSessionResponse(session_id=session_id, user_id=body.user_id)
    except Exception as e:
        logger.error(f"Error creating session: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@api_router.get("/session/{session_id}")
async def get_session_state(session_id: str):
    """Return current session state for the frontend (mode, flow, step, step name)."""
    try:
        session = state_manager.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        step = session.get("current_step", 0)
        step_name = None
        steps_total = None
        current_flow = session.get("current_flow")

        step_names = get_flow_steps(current_flow)
        if step_names is not None:
            step_name = step_names[step] if step < len(step_names) else None
            steps_total = len(step_names)
        elif current_flow == "journey":
            # Dynamic flow: steps are determined by engine state, not a static list.
            step_name = "dynamic"
            steps_total = None

        return {
            "session_id": session_id,
            "mode": session.get("mode", "conversational"),
            "current_flow": current_flow,
            "current_step": step,
            "step_name": step_name,
            "steps_total": steps_total,
            "collected_keys": list((session.get("collected_data") or {}).keys()),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting session: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@api_router.get("/forms/draft/{session_id}/{flow_name}", tags=["Forms"])
async def get_form_draft(session_id: str, flow_name: str):
    """Fetch the cached draft for a multi-step form flow."""
    try:
        draft = state_manager.get_form_draft(session_id, flow_name)
        if not draft:
            raise HTTPException(status_code=404, detail="Draft not found")
        return draft
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting form draft: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@api_router.delete("/forms/draft/{session_id}/{flow_name}", tags=["Forms"])
async def delete_form_draft(session_id: str, flow_name: str):
    """Clear a cached draft for a multi-step form flow."""
    try:
        state_manager.clear_form_draft(session_id, flow_name)
        return {"status": "deleted", "session_id": session_id, "flow": flow_name}
    except Exception as e:
        logger.error(f"Error deleting form draft: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@api_router.post("/chat/start-guided", tags=["Chat"])
async def start_guided_body(
    body: StartGuidedRequest,
    router: ChatRouter = Depends(get_router),
    db: PostgresDB = Depends(get_db),
):
    """Start a guided flow. If session_id is omitted, a new session is created."""
    try:
        session_id = body.session_id
        user = db.get_or_create_user(phone_number=body.user_id)
        internal_user_id = str(user.id)
        if not session_id:
            session_id = state_manager.create_session(internal_user_id)
        response = await router.guided.start_flow(
            flow_name=body.flow_name,
            session_id=session_id,
            user_id=internal_user_id,
            initial_data=body.initial_data or {},
        )
        return {"session_id": session_id, **response}
    except Exception as e:
        logger.error(f"Error starting guided flow: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@api_router.post("/chat/message", response_model=ChatResponse)
async def api_send_message(
    request: ChatMessage,
    router: ChatRouter = Depends(get_router),
    db: PostgresDB = Depends(get_db),
):
    """
    Send a message or form_data (frontend). Uses same RAG retrieval as run_rag:
    config-driven top_k, synonym expansion, re-ranking. Routes to conversational or guided mode.
    """
    try:
        return await _handle_chat_message(request, router, db)
    except FormValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "validation_error",
                "message": e.message,
                "field_errors": e.field_errors,
            },
        )
    except Exception as e:
        logger.error(f"Error processing message: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.websocket("/ws/chat")
async def websocket_chat(
    websocket: WebSocket,
):
    """
    WebSocket endpoint for chat conversations.

    - Expects JSON payloads shaped like ChatMessage:
      { "message": "...", "session_id": "...", "user_id": "...", "metadata": {...}, "form_data": {...} }
    - Returns ChatResponse-shaped JSON for each incoming message.
    - Uses the same ChatRouter/RAG pipeline as the HTTP /api/chat/message endpoint.
    - Protected by the same API key mechanism using the X-API-KEY header.
    """
    from src.chatbot.dependencies import get_api_keys
    import hmac

    # Authenticate using the same API key as HTTP routes.
    api_key = websocket.headers.get("x-api-key") or websocket.query_params.get("api_key")
    valid_keys = get_api_keys()
    candidate = (api_key or "").strip()
    ok = bool(candidate) and any(hmac.compare_digest(candidate, k) for k in valid_keys)
    if not ok:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()

    while True:
        try:
            data = await websocket.receive_json()
        except WebSocketDisconnect:
            break
        except Exception:
            # Malformed frame; close with a generic error.
            await websocket.close(code=status.WS_1003_UNSUPPORTED_DATA)
            break

        try:
            msg = ChatMessage(**data)
        except ValidationError as e:
            await websocket.send_json(
                {
                    "error": "invalid_payload",
                    "details": e.errors(),
                }
            )
            continue

        try:
            resp = await _handle_chat_message(msg, chat_router, postgres_db)
        except FormValidationError as e:
            await websocket.send_json(
                {
                    "error": "validation_error",
                    "message": e.message,
                    "field_errors": e.field_errors,
                }
            )
            continue
        except Exception as e:
            logger.error("Error processing websocket message: %s", e, exc_info=True)
            await websocket.send_json(
                {
                    "error": "server_error",
                    "detail": "An error occurred while processing your message.",
                }
            )
            continue

        await websocket.send_json(resp.dict())


# ---------- Product routes ----------
def _public_product(matcher: ProductMatcher, item: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert internal product representation (doc_id-based) to a frontend-friendly shape.

    - product_id: short stable key like "personal/insure/serenicare"
    - doc_id: internal id like "website:product:personal/insure/serenicare" (kept for debugging/back-compat)
    """
    doc_id = item.get("doc_id") or item.get("product_id") or ""
    public_id = (item.get("product_key") or matcher.get_public_id(doc_id) or doc_id) if doc_id else (item.get("product_key") or "")
    return {
        "product_id": public_id,
        "doc_id": doc_id,
        "slug": item.get("slug") or "",
        "name": item.get("name") or "",
        "category": item.get("category_name") or "",
        "subcategory": item.get("sub_category_name") or "",
        "url": item.get("url"),
    }


def _resolve_product_doc_id(matcher: ProductMatcher, product_id: str) -> str:
    """
    Accept either:
    - full doc_id: "website:product:..."
    - short product key: "category/subcategory/slug"
    - unique slug: "serenicare" (only when globally unique)
    """
    doc_id = matcher.resolve_doc_id(product_id)
    if not doc_id:
        raise HTTPException(
            status_code=404,
            detail="Product not found. Use full doc_id (website:product:...) or short key (category/subcategory/slug).",
        )
    return doc_id


@api_router.get("/products/list", tags=["Products"])
async def api_list_products(
    category: Optional[str] = Query(None, description="Filter by category (e.g. personal, business)."),
    subcategory: Optional[str] = Query(None, description="Filter by subcategory (e.g. save-and-invest). Use with category."),
    search: Optional[str] = Query(None, description="Search in product name and category; returns matching products."),
    matcher: ProductMatcher = Depends(lambda: product_matcher),
):
    """
    List products with optional filters.

    - **category**: list products where category equals this (e.g. `?category=personal`).
    - **subcategory**: narrow by subcategory (e.g. `?category=personal&subcategory=save-and-invest`).
    - **search**: text search in name/category (e.g. `?search=savings`).
    """
    try:
        if search and search.strip():
            # Text search: use match_products, return product dicts
            scored = matcher.match_products(search.strip(), top_k=50)
            products = [p[2] for p in scored]
        elif category:
            products = matcher.get_products_by_category(category)
            if subcategory and subcategory.strip():
                sub_lower = subcategory.strip().lower()
                products = [p for p in products if (p.get("sub_category_name") or "").lower() == sub_lower]
        else:
            products = list(matcher.product_index.values())
            if subcategory and subcategory.strip():
                sub_lower = subcategory.strip().lower()
                products = [p for p in products if (p.get("sub_category_name") or "").lower() == sub_lower]
        public = [_public_product(matcher, p) for p in products]
        return {"products": public, "count": len(public)}
    except Exception as e:
        logger.error(f"Error listing products: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@api_router.get("/products/card/{product_id:path}", tags=["Products"])
async def api_get_product_card(
    product_id: str,
    include_details: bool = False,
    matcher: ProductMatcher = Depends(lambda: product_matcher),
):
    """Product card (RAG summary). Use by-id when product_id contains slashes."""
    try:
        doc_id = _resolve_product_doc_id(matcher, product_id)
        card = product_card_gen.generate_card(doc_id, False)
        if not card:
            raise HTTPException(status_code=404, detail="Product not found")
        if include_details:
            card["details"] = await product_card_gen.get_product_details(doc_id)

        # Present friendly ids outward
        public_id = matcher.get_public_id(doc_id) or doc_id
        card["product_id"] = public_id
        card["doc_id"] = doc_id
        if isinstance(card.get("details"), dict):
            card["details"]["product_id"] = public_id
            card["details"]["doc_id"] = doc_id
            # Rewrite related_products ids if present
            rel = card["details"].get("related_products")
            if isinstance(rel, list):
                for r in rel:
                    if isinstance(r, dict) and r.get("product_id"):
                        r_doc = r["product_id"]
                        r["product_id"] = matcher.get_public_id(r_doc) or r_doc
                        r["doc_id"] = r_doc
        return card
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting product: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@api_router.get("/products/card/{product_id:path}/details", tags=["Products"])
async def api_get_product_card_details(
    product_id: str,
    matcher: ProductMatcher = Depends(lambda: product_matcher),
):
    """Detailed product information (Learn More) via RAG/LLM."""
    try:
        doc_id = _resolve_product_doc_id(matcher, product_id)
        details = await product_card_gen.get_product_details(doc_id)
        public_id = matcher.get_public_id(doc_id) or doc_id
        if isinstance(details, dict):
            details["product_id"] = public_id
            details["doc_id"] = doc_id
            rel = details.get("related_products")
            if isinstance(rel, list):
                for r in rel:
                    if isinstance(r, dict) and r.get("product_id"):
                        r_doc = r["product_id"]
                        r["product_id"] = matcher.get_public_id(r_doc) or r_doc
                        r["doc_id"] = r_doc
        return details
    except Exception as e:
        logger.error(f"Error getting product details: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@api_router.post("/forms/personal-accident/full", response_model=PersonalAccidentFullFormResponse, tags=["Forms"])
async def submit_personal_accident_full_form(
    body: PersonalAccidentFullFormRequest,
    db: PostgresDB = Depends(get_db),
):
    """
    Accept the entire Personal Accident application in one payload and create a quote.

    This bypasses the guided chat step-by-step flow, so the frontend can collect all
    details in a single (possibly multi-section) form and submit once.
    """
    from src.chatbot.flows.personal_accident import PersonalAccidentFlow

    try:
        # Resolve external identifier (e.g. phone) to internal user UUID
        user = db.get_or_create_user(phone_number=body.user_id)
        internal_user_id = str(user.id)

        # Reuse the existing PersonalAccidentFlow validations by running the
        # step handlers sequentially against a shared data dict, but without
        # touching the guided session state machine.
        flow = PersonalAccidentFlow(product_matcher, db)

        payload: Dict[str, Any] = dict(body.data or {})
        data: Dict[str, Any] = {
            "user_id": internal_user_id,
            "product_id": "personal_accident",
        }

        # Normalize the flattened full-form payload into the same shape expected
        # by the guided-flow validators, without creating a draft quote first.
        quick_quote: Dict[str, Any] = {
            "first_name": payload.get("first_name") or payload.get("firstName", ""),
            "last_name": payload.get("surname") or payload.get("lastName", ""),
            "middle_name": payload.get("middle_name") or payload.get("middleName", ""),
            "email": payload.get("email", ""),
            "mobile": payload.get("mobile") or payload.get("mobile_number", ""),
            "dob": payload.get("dob"),
            "policy_start_date": payload.get("policyStartDate") or payload.get("policy_start_date"),
            "cover_limit_ugx": int(payload.get("coverLimitAmountUgx") or payload.get("cover_limit_amount_ugx") or 10_000_000),
        }
        data["quick_quote"] = quick_quote

        # Run each logical step's validation + data shaping.
        # Full-form payloads may intentionally omit quick-quote-only fields like DOB,
        # so we keep normalized quick_quote data above and validate the remaining steps.
        await flow._step_personal_details(payload, data, internal_user_id)
        await flow._step_next_of_kin(payload, data, internal_user_id)
        await flow._step_previous_pa_policy(payload, data, internal_user_id)
        await flow._step_physical_disability(payload, data, internal_user_id)
        await flow._step_risky_activities(payload, data, internal_user_id)
        await flow._step_upload_national_id(payload, data, internal_user_id)

        # Calculate premium using the same helper as the guided flow.
        sum_assured = int((data.get("quick_quote") or {}).get("cover_limit_ugx", 10_000_000))
        premium = flow._calculate_pa_premium(data, sum_assured)

        # Persist a quote once, with all underwriting data collected at once.
        quote = db.create_quote(
            user_id=internal_user_id,
            product_id=data.get("product_id", "personal_accident"),
            premium_amount=premium["monthly"],
            sum_assured=sum_assured,
            underwriting_data=data,
            pricing_breakdown=premium.get("breakdown"),
            product_name="Personal Accident",
        )

        return PersonalAccidentFullFormResponse(
            quote_id=str(quote.id),
            product_name="Personal Accident",
            monthly_premium=premium["monthly"],
            annual_premium=premium["annual"],
            sum_assured=sum_assured,
            breakdown=premium.get("breakdown", {}),
        )
    except FormValidationError as e:
        # Mirror the chat/message validation error shape
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "validation_error",
                "message": e.message,
                "field_errors": e.field_errors,
            },
        )
    except Exception as e:
        logger.error(f"Error submitting Personal Accident full form: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@api_router.post("/forms/motor-private/full", response_model=MotorPrivateFullFormResponse, tags=["Forms"])
async def submit_motor_private_full_form(
    body: MotorPrivateFullFormRequest,
    db: PostgresDB = Depends(get_db),
):
    """Accept the full Motor Private quote form in one payload and create a quote.

    This reuses MotorPrivateFlow.complete_flow so all motor-specific validations run
    server-side and a quote is persisted once.
    """
    from src.chatbot.controllers.motor_private_controller import MotorPrivateController

    try:
        controller = MotorPrivateController(db)
        result = await controller.submit_full_form(body.user_id, body.data or {})

        return MotorPrivateFullFormResponse(
            quote_id=result["quote_id"],
            product_name=result["product_name"],
            total_premium=result["total_premium"],
            breakdown=result["breakdown"],
        )
    except FormValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "validation_error",
                "message": e.message,
                "field_errors": e.field_errors,
            },
        )
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.error(f"Error submitting Motor Private full form: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@api_router.post("/forms/travel-insurance/full", response_model=TravelInsuranceFullFormResponse, tags=["Forms"])
async def submit_travel_insurance_full_form(
    body: TravelInsuranceFullFormRequest,
    db: PostgresDB = Depends(get_db),
):
    """
    Accept the entire Travel Insurance application in one payload and create a quote.

    Runs all server-side field validations via TravelInsuranceController then
    calculates the premium and persists a quote record.
    """
    from src.chatbot.controllers.travel_insurance_controller import TravelInsuranceController
    from src.integrations.policy.premium import premium_service

    try:
        user = db.get_or_create_user(phone_number=body.user_id)
        internal_user_id = str(user.id)

        controller = TravelInsuranceController(db)
        payload: Dict[str, Any] = dict(body.data or {})

        app = controller.create_application(internal_user_id, {})
        app_id = app["id"]

        # Validate each section via the controller (raises FormValidationError on bad input)
        controller.update_about_you(app_id, payload)
        controller.update_travel_party_and_trip(app_id, payload)
        controller.update_data_consent(app_id, payload)
        controller.update_traveller_details(app_id, payload)

        # Optional sections — only validate if the client included relevant fields
        if any(k in payload for k in ("ec_surname", "ec_relationship", "ec_phone_number", "ec_email")):
            controller.update_emergency_contact(app_id, payload)
        if any(k in payload for k in ("bank_name", "account_holder_name", "account_number")):
            controller.update_bank_details(app_id, payload)

        # Build the data dict that the premium calculator expects
        data: Dict[str, Any] = {
            "selected_product": payload.get("selected_product") or {"id": payload.get("product_id", "worldwide_essential")},
            "travel_party_and_trip": {
                "travel_party": payload.get("travel_party"),
                "num_travellers_18_69": payload.get("num_travellers_18_69", 1),
                "num_travellers_0_17": payload.get("num_travellers_0_17", 0),
                "num_travellers_70_75": payload.get("num_travellers_70_75", 0),
                "num_travellers_76_80": payload.get("num_travellers_76_80", 0),
                "num_travellers_81_85": payload.get("num_travellers_81_85", 0),
                "departure_date": payload.get("departure_date"),
                "return_date": payload.get("return_date"),
                "departure_country": payload.get("departure_country"),
                "destination_country": payload.get("destination_country"),
            },
        }
        pricing = premium_service.calculate_sync("travel_insurance", {"data": data})

        result = controller.finalize_and_create_quote(app_id, internal_user_id, pricing)
        quote_id = (result or {}).get("quote_id") or ""

        return TravelInsuranceFullFormResponse(
            quote_id=quote_id,
            product_name=(data["selected_product"] or {}).get("label", "Travel Insurance"),
            total_premium_ugx=pricing["total_ugx"],
            total_premium_usd=pricing["total_usd"],
            breakdown=pricing.get("breakdown", {}),
        )
    except FormValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "validation_error",
                "message": e.message,
                "field_errors": e.field_errors,
            },
        )
    except Exception as e:
        logger.error(f"Error submitting Travel Insurance full form: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@api_router.post("/forms/serenicare/full", response_model=SerenicareFullFormResponse, tags=["Forms"])
async def submit_serenicare_full_form(
    body: SerenicareFullFormRequest,
    db: PostgresDB = Depends(get_db),
):
    """
    Accept the entire Serenicare application in one payload and create a quote.

    Runs all server-side field validations via SerenicareController then
    calculates the premium and persists a quote record.
    """
    from src.chatbot.controllers.serenicare_controller import SerenicareController
    from src.integrations.policy.premium import premium_service

    try:
        user = db.get_or_create_user(phone_number=body.user_id)
        internal_user_id = str(user.id)

        controller = SerenicareController(db)
        payload: Dict[str, Any] = dict(body.data or {})

        app = controller.create_application(internal_user_id, {})
        app_id = app["id"]

        # Validate each section (raises FormValidationError on bad input)
        controller.update_about_you(app_id, payload)
        controller.update_plan_selection(app_id, payload)
        controller.update_optional_benefits(app_id, payload)
        controller.update_medical_conditions(app_id, payload)
        controller.update_cover_personalization(app_id, payload)

        # Determine selected plan for premium calculation
        plan = {"id": payload.get("plan_option", "essential")}
        pricing = premium_service.calculate_sync("serenicare", {"data": payload, "plan": plan})

        result = controller.finalize_and_create_quote(app_id, internal_user_id, pricing)
        quote_id = (result or {}).get("quote_id") or ""

        return SerenicareFullFormResponse(
            quote_id=quote_id,
            product_name="Serenicare",
            monthly_premium=pricing["monthly"],
            annual_premium=pricing["annual"],
            breakdown=pricing.get("breakdown", {}),
        )
    except FormValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "validation_error",
                "message": e.message,
                "field_errors": e.field_errors,
            },
        )
    except Exception as e:
        logger.error(f"Error submitting Serenicare full form: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@api_router.post("/quotes/generate", response_model=QuoteResponse, tags=["Quotes"])
async def generate_quote(request: QuoteRequest, db: PostgresDB = Depends(get_db)):
    """Generate an insurance quote from underwriting data."""
    try:
        # This would use the quotation flow
        from src.chatbot.flows.quotation import QuotationFlow

        quotation_flow = QuotationFlow(product_matcher, db)
        quote_data = await quotation_flow._calculate_premium(request.underwriting_data)

        # Save quote to database
        quote = db.create_quote(
            user_id=request.user_id,
            product_id=request.product_id,
            premium_amount=quote_data["monthly_premium"],
            sum_assured=quote_data["sum_assured"],
            underwriting_data=request.underwriting_data,
            pricing_breakdown=quote_data["breakdown"],
        )

        return QuoteResponse(
            quote_id=str(quote.id),
            product_name=quote_data["product_name"],
            monthly_premium=quote_data["monthly_premium"],
            sum_assured=quote_data["sum_assured"],
            valid_until=quote.valid_until.isoformat(),
        )

    except Exception as e:
        logger.error(f"Error generating quote: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@api_router.get("/quotes/{quote_id}", tags=["Quotes"])
async def get_quote(quote_id: str, db: PostgresDB = Depends(get_db)):
    """Get a quote by ID."""
    try:
        quote = db.get_quote(quote_id)

        if not quote:
            raise HTTPException(status_code=404, detail="Quote not found")

        return {
            "quote_id": str(quote.id),
            "product_id": quote.product_id,
            "product_name": quote.product_name,
            "premium_amount": float(quote.premium_amount),
            "sum_assured": float(quote.sum_assured) if quote.sum_assured else None,
            "status": quote.status,
            "valid_until": quote.valid_until.isoformat() if quote.valid_until else None,
            "generated_at": quote.generated_at.isoformat(),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting quote: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@api_router.get("/sessions/{session_id}/history", tags=["Sessions"])
async def get_conversation_history(session_id: str, limit: int = 50):
    """Get conversation history for a session."""
    try:
        session = state_manager.get_session(session_id)

        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        messages = postgres_db.get_conversation_history(session["conversation_id"], limit=limit)

        msg_list = [{"role": msg.role, "content": msg.content, "timestamp": msg.timestamp.isoformat()} for msg in reversed(messages)]
        return {"session_id": session_id, "messages": msg_list}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting history: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@api_router.delete("/sessions/{session_id}", tags=["Sessions"])
async def end_session(session_id: str, ended_by: str = "user"):
    """End a chatbot session."""
    try:
        state_manager.end_session(session_id, ended_by=ended_by)
        return {"message": "Session ended successfully"}

    except Exception as e:
        logger.error(f"Error ending session: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# API versioning: expose routes under /api/v1
app.include_router(api_router, prefix="/api/v1")


def _strip_heading_from_text(text: str, heading: str) -> str:
    """
    Remove duplicated heading from the start of text so the API returns
    content-only in "text" when "heading" is already present.
    Handles "Heading\\ncontent", "Q: Heading\\nA: answer" (FAQ), and similar.
    """
    if not text or not heading:
        return text
    t, h = text.strip(), heading.strip()
    if not h:
        return text
    # "Heading\ncontent" or "Heading content"
    if t.lower().startswith(h.lower()):
        rest = t[len(h) :].lstrip("\n\t ")
        if rest.upper().startswith("A:") and "Q:" in t[:4]:
            rest = rest[2:].lstrip()
        return rest if rest else t
    # FAQ: "Q: Heading\nA: answer" -> return just the answer
    q_prefix = "Q: " + h
    if t.lower().startswith(q_prefix.lower()):
        after = t[len(q_prefix) :].lstrip()
        if after.upper().startswith("A:"):
            return after[2:].lstrip()
        return after
    return text


def _load_product_sections(product_id: str) -> Dict[str, List[Dict[str, str]]]:
    """
    Load typed sections for a product from website_chunks.jsonl.
    Each entry's "text" is trimmed so it does not repeat the "heading".
    """
    chunks_path = Path(__file__).parent.parent.parent / "data" / "processed" / "website_chunks.jsonl"
    if not chunks_path.exists():
        raise HTTPException(status_code=500, detail="Product chunks file not found")

    sections: Dict[str, List[Dict[str, str]]] = {}
    with open(chunks_path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            data = json.loads(line)
            if data.get("type") != "product":
                continue
            if data.get("doc_id") != product_id:
                continue
            ctype = data.get("chunk_type") or "general"
            heading = data.get("section_heading") or ""
            raw_text = data.get("text") or ""
            text = _strip_heading_from_text(raw_text, heading)
            entry = {"heading": heading, "text": text}
            sections.setdefault(ctype, []).append(entry)
    return sections


# ============================================================================
# STARTUP/SHUTDOWN EVENTS
# ============================================================================
@app.on_event("startup")
async def startup_event():
    """Initialize on startup"""
    logger.info("Starting Old Mutual Chatbot API...")

    # Log sanitized DB target details (no credentials) for connectivity debugging.
    db_url = os.getenv("DATABASE_URL", "")
    if db_url:
        try:
            parsed = urlparse(db_url)
            query = parse_qs(parsed.query or "")
            logger.info(
                "DATABASE_URL target: scheme=%s host=%s port=%s db=%s sslmode=%s channel_binding=%s use_postgres=%s",
                parsed.scheme,
                parsed.hostname,
                parsed.port or 5432,
                (parsed.path or "").lstrip("/"),
                (query.get("sslmode") or [""])[0],
                (query.get("channel_binding") or [""])[0],
                os.getenv("USE_POSTGRES_CONVERSATIONS", ""),
            )
        except Exception as e:
            logger.warning("Could not parse DATABASE_URL for startup logging: %s", e)
    else:
        logger.info("DATABASE_URL not set; using in-memory PostgresDB stub")

    logger.info("Database schema initialization is managed by Alembic migrations")

    # Test Redis connection
    if redis_cache.ping():
        logger.info("Redis connection successful")
    else:
        logger.warning("Redis connection failed")


@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on shutdown"""
    logger.info("Shutting down Old Mutual Chatbot API...")


app.include_router(mock_router)
app.include_router(mock_premiums_router)
