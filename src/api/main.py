from src.api.escalation import router as escalation_router
from src.api.endpoints.payments import payments_api
from src.api.endpoints.policies import policies_api
from src.api.endpoints.premiums import premiums_api
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
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, ValidationError
from src.chatbot.dependencies import api_key_protection

from src.chatbot.modes.conversational import ConversationalMode
from src.chatbot.modes.guided import GuidedMode
from src.chatbot.product_cards import ProductCardGenerator
from src.chatbot.router import ChatRouter
from src.chatbot.state_manager import StateManager
from src.chatbot.validation import FormValidationError
from src.rag.generate import MiaGenerator
from src.rag.query import retrieve_context
from src.utils.product_matcher import ProductMatcher
from src.utils.rag_config_loader import load_rag_config
from src.api.endpoints.mock_underwriting import router as mock_router
from src.api.endpoints.mock_premiums import router as mock_premiums_router

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

# Register agent webhook router
app.include_router(agent_webhook_router, prefix="/api/v1")

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

    async def generate(self, query: str, context_docs: List[Dict], conversation_history: List[Dict]):
        """
        Use the configured generation backend (Gemini by default) to
        produce an answer grounded in the retrieved context.
        """
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
            return {"answer": answer_text, "confidence": 0.5, "sources": context_docs}

        # If generation is globally disabled, always fall back to extractive mode.
        if not self.cfg.generation.enabled:
            return _extractive_answer()

        if self.cfg.generation.backend == "gemini":
            # Use the new async Gemini generator (MiaGenerator). If the model call
            # fails or returns our generic phone number fallback, degrade to a
            # context-only extractive answer instead of surfacing the error text.
            mia = MiaGenerator()
            try:
                answer = await mia.generate(query, context_docs, conversation_history)
            except Exception as e:  # pragma: no cover - defensive; MiaGenerator already logs
                logger.error("MiaGenerator.generate raised unexpectedly: %s", e, exc_info=True)
                return _extractive_answer()

            fallback_phrase = "I'm having trouble retrieving those details. Please call 0800-100-900 for immediate help."
            if not answer or fallback_phrase in answer:
                # LLM unavailable / failed -> use extractive context instead.
                return _extractive_answer()

            return {"answer": answer, "confidence": 0.7, "sources": context_docs}

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

    # Save message to database
    session = state_manager.get_session(session_id)
    if session:
        user_content = json.dumps(request.form_data) if request.form_data else request.message
        db.add_message(
            conversation_id=session["conversation_id"],
            role="user",
            content=user_content,
            metadata=request.metadata or {},
        )
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
        product_file = PRODUCT_DIR / f"{product}.json"

        logger.info(f"Resolved product file path: {product_file}")

        if not product_file.exists():
            logger.error(f"Product file not found: {product_file}")
            raise HTTPException(status_code=404, detail="Product information not found")

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

        if current_flow == "personal_accident":
            from src.chatbot.flows.personal_accident import PersonalAccidentFlow

            step_names = PersonalAccidentFlow.STEPS
            step_name = step_names[step] if step < len(step_names) else None
            steps_total = len(step_names)
        elif current_flow == "travel_insurance":
            from src.chatbot.flows.travel_insurance import TravelInsuranceFlow

            step_names = TravelInsuranceFlow.STEPS
            step_name = step_names[step] if step < len(step_names) else None
            steps_total = len(step_names)
        elif current_flow == "motor_private":
            from src.chatbot.flows.motor_private import MotorPrivateFlow

            step_names = MotorPrivateFlow.STEPS
            step_name = step_names[step] if step < len(step_names) else None
            steps_total = len(step_names)
        elif current_flow == "serenicare":
            from src.chatbot.flows.serenicare import SerenicareFlow

            step_names = SerenicareFlow.STEPS
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


def _build_flow_schema(flow_id: str) -> Dict:
    """Build step and form schema for a guided flow. Raises KeyError for unknown flow_id."""
    if flow_id == "personal_accident":
        from src.chatbot.flows.personal_accident import (
            PA_BENEFITS_BY_LEVEL,
            PERSONAL_ACCIDENT_RISKY_ACTIVITIES,
            PersonalAccidentFlow,
        )

        steps = []
        for i, name in enumerate(PersonalAccidentFlow.STEPS):
            entry = {"index": i, "name": name}
            if name == "quick_quote":
                entry["form"] = {
                    "type": "form",
                    "message": "Get your Personal Accident quote in seconds",
                    "fields": [
                        {"name": "firstName", "label": "First Name", "type": "text", "required": True, "minLength": 2, "maxLength": 50},
                        {"name": "lastName", "label": "Last Name", "type": "text", "required": True, "minLength": 2, "maxLength": 50},
                        {"name": "middleName", "label": "Middle Name", "type": "text", "required": False, "maxLength": 50},
                        {"name": "mobile", "label": "Mobile Number", "type": "tel", "required": True, "placeholder": "07XX XXX XXX"},
                        {"name": "email", "label": "Email Address", "type": "email", "required": True, "maxLength": 100},
                        {"name": "dob", "label": "Date of Birth", "type": "date", "required": True, "help": "Must be 18-65 years old"},
                        {"name": "policyStartDate", "label": "Policy Start Date", "type": "date", "required": True, "help": "Must be after today"},
                        {"name": "coverLimitAmountUgx", "label": "Cover Limit", "type": "select", "required": True, "options": [
                            {"value": "5000000", "label": "UGX 5,000,000"},
                            {"value": "10000000", "label": "UGX 10,000,000"},
                            {"value": "20000000", "label": "UGX 20,000,000"},
                        ]},
                    ],
                }
            elif name == "premium_summary":
                entry["form"] = {
                    "type": "premium_summary",
                    "message": "Your Personal Accident Premium",
                    "benefits": PA_BENEFITS_BY_LEVEL,
                    "actions": ["edit", "proceed_to_details"],
                }
            elif name == "personal_details":
                entry["form"] = {
                    "type": "form",
                    "message": "👤 Personal Details",
                    "fields": [
                        {"name": "surname", "label": "Surname", "type": "text", "required": True},
                        {"name": "first_name", "label": "First Name", "type": "text", "required": True},
                        {"name": "middle_name", "label": "Middle Name", "type": "text", "required": False},
                        {"name": "email", "label": "Email Address", "type": "email", "required": True},
                        {"name": "mobile_number", "label": "Mobile Number", "type": "tel", "required": True},
                        {"name": "national_id_number", "label": "National ID Number", "type": "text", "required": True, "help": "11-digit NIN"},
                        {"name": "nationality", "label": "Nationality", "type": "text", "required": True},
                        {"name": "tax_identification_number", "label": "TIN (Optional)", "type": "text", "required": False},
                        {"name": "occupation", "label": "Occupation", "type": "text", "required": True},
                        {"name": "gender", "label": "Gender", "type": "select", "required": True, "options": [
                            {"value": "Male", "label": "Male"},
                            {"value": "Female", "label": "Female"},
                            {"value": "Other", "label": "Other"},
                        ]},
                        {"name": "country_of_residence", "label": "Country of Residence", "type": "text", "required": True},
                        {"name": "physical_address", "label": "Physical Address", "type": "text", "required": True},
                    ],
                }
            elif name == "next_of_kin":
                entry["form"] = {
                    "type": "form",
                    "message": "👥 Next of Kin",
                    "fields": [
                        {"name": "nok_first_name", "label": "First Name", "type": "text", "required": True},
                        {"name": "nok_last_name", "label": "Last Name", "type": "text", "required": True},
                        {"name": "nok_middle_name", "label": "Middle Name", "type": "text", "required": False},
                        {"name": "nok_phone_number", "label": "Phone Number", "type": "tel", "required": True},
                        {"name": "nok_relationship", "label": "Relationship", "type": "text", "required": True},
                        {"name": "nok_address", "label": "Address", "type": "text", "required": True},
                        {"name": "nok_id_number", "label": "ID Number", "type": "text", "required": False},
                    ],
                }
            elif name == "previous_pa_policy":
                entry["form"] = {
                    "type": "yes_no_details",
                    "question_id": "previous_pa_policy",
                    "details_field": {"name": "previous_insurer_name", "show_when": "yes"},
                }
            elif name == "physical_disability":
                entry["form"] = {
                    "type": "yes_no_details",
                    "question_id": "physical_disability",
                    "details_field": {"name": "disability_details", "show_when": "no"},
                }
            elif name == "risky_activities":
                entry["form"] = {
                    "type": "checkbox",
                    "options": PERSONAL_ACCIDENT_RISKY_ACTIVITIES,
                    "other_field": {"name": "risky_activity_other"},
                }
            elif name == "upload_national_id":
                entry["form"] = {"type": "file_upload", "field_name": "national_id_file_ref", "accept": "application/pdf"}
            elif name == "final_confirmation":
                entry["form"] = {
                    "type": "confirmation",
                    "message": "Please review your details",
                    "actions": ["edit", "confirm"],
                }
            elif name == "choose_plan_and_pay":
                entry["form"] = {"type": "proceed_to_payment", "actions": ["confirm"]}
            steps.append(entry)
        return {"flow_id": "personal_accident", "steps": steps}

    # Motor private flow (incoming changes merged)
    if flow_id == "motor_private":
        from src.chatbot.flows.motor_private import (
            MOTOR_PRIVATE_ADDITIONAL_BENEFITS,
            MOTOR_PRIVATE_EXCESS_PARAMETERS,
            MotorPrivateFlow,
        )

        steps = []
        for i, name in enumerate(MotorPrivateFlow.STEPS):
            entry = {"index": i, "name": name}
            if name == "vehicle_details":
                entry["form"] = {
                    "type": "form",
                    "fields": [
                        {"name": "vehicle_make", "label": "Choose vehicle make", "type": "select", "required": True},
                        {"name": "year_of_manufacture", "label": "Year of manufacture", "type": "text", "required": True},
                        {"name": "cover_start_date", "label": "Cover start date", "type": "date", "required": True},
                        {"name": "rare_model", "label": "Is the car a rare model?", "type": "radio", "options": ["Yes", "No"], "required": True},
                        {"name": "valuation_done", "label": "Has the vehicle undergone valuation?", "type": "radio",
                         "options": ["Yes", "No"], "required": True},
                        {"name": "vehicle_value", "label": "Value of Vehicle (UGX)", "type": "number", "required": True},
                        {"name": "first_time_registration", "label": "First time registration for this type?", "type": "radio",
                         "options": ["Yes", "No"], "required": True},
                        {"name": "car_alarm_installed", "label": "Car alarm installed?", "type": "radio", "options": ["Yes", "No"], "required": True},
                        {"name": "tracking_system_installed", "label": "Tracking system installed?", "type": "radio",
                         "options": ["Yes", "No"], "required": True},
                        {"name": "car_usage_region", "label": "Car usage region", "type": "radio",
                         "options": ["Within Uganda", "Within East Africa", "Outside East Africa"], "required": True},
                    ],
                }
            elif name == "excess_parameters":
                entry["form"] = {"type": "checkbox", "options": MOTOR_PRIVATE_EXCESS_PARAMETERS}
            elif name == "additional_benefits":
                entry["form"] = {"type": "checkbox", "options": MOTOR_PRIVATE_ADDITIONAL_BENEFITS}
            elif name == "benefits_summary":
                entry["form"] = {"type": "benefits_summary"}
            elif name == "premium_calculation":
                entry["form"] = {"type": "premium_summary", "actions": ["edit", "download_quote"]}
            elif name == "about_you":
                entry["form"] = {
                    "type": "form",
                    "fields": [
                        {"name": "first_name", "label": "First Name", "type": "text", "required": True},
                        {"name": "middle_name", "label": "Middle Name (Optional)", "type": "text", "required": False},
                        {"name": "surname", "label": "Surname", "type": "text", "required": True},
                        {"name": "phone_number", "label": "Phone Number", "type": "text", "required": True},
                        {"name": "email", "label": "Email", "type": "email", "required": True},
                    ],
                }
            elif name in ("premium_and_download", "choose_plan_and_pay"):
                entry["form"] = {"type": "premium_summary", "actions": ["edit", "download_quote", "proceed_to_pay"]}
            steps.append(entry)
        return {"flow_id": "motor_private", "steps": steps}

    # Serenicare flow (incoming changes merged)
    if flow_id == "serenicare":
        from src.chatbot.flows.serenicare import SERENICARE_OPTIONAL_BENEFITS, SERENICARE_PLANS, SerenicareFlow

        steps = []
        for i, name in enumerate(SerenicareFlow.STEPS):
            entry = {"index": i, "name": name}
            if name == "cover_personalization":
                entry["form"] = {
                    "type": "form",
                    "fields": [
                        {"name": "date_of_birth", "label": "Date of Birth", "type": "date", "required": True},
                        {"name": "include_spouse", "label": "Include Spouse/Partner", "type": "checkbox", "required": False},
                        {"name": "include_children", "label": "Include Child/Children", "type": "checkbox", "required": False},
                        {"name": "add_another_main_member", "label": "Add another main member", "type": "checkbox", "required": False},
                    ],
                }
            elif name == "optional_benefits":
                entry["form"] = {"type": "checkbox", "options": SERENICARE_OPTIONAL_BENEFITS}
            elif name == "medical_conditions":
                entry["form"] = {
                    "type": "radio",
                    "question_id": "medical_conditions",
                    "options": [{"id": "yes", "label": "Yes"}, {"id": "no", "label": "No"}],
                    "required": True,
                }
            elif name == "plan_selection":
                entry["form"] = {
                    "type": "options",
                    "options": [
                        {"id": p["id"], "label": p["label"], "description": p["description"], "benefits": p["benefits"]}
                        for p in SERENICARE_PLANS
                    ],
                }
            elif name == "about_you":
                entry["form"] = {
                    "type": "form",
                    "fields": [
                        {"name": "first_name", "label": "First Name", "type": "text", "required": True},
                        {"name": "middle_name", "label": "Middle Name (Optional)", "type": "text", "required": False},
                        {"name": "surname", "label": "Surname", "type": "text", "required": True},
                        {"name": "phone_number", "label": "Phone Number", "type": "text", "required": True},
                        {"name": "email", "label": "Email", "type": "email", "required": True},
                    ],
                }
            elif name == "premium_and_download":
                entry["form"] = {"type": "premium_summary", "actions": ["view_all_plans", "proceed_to_pay"]}
            elif name == "choose_plan_and_pay":
                entry["form"] = {"type": "proceed_to_payment", "actions": ["confirm"]}
            steps.append(entry)
        return {"flow_id": "serenicare", "steps": steps}

    raise KeyError(flow_id)


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


@api_router.get("/products/categories", tags=["Products"])
async def api_list_product_categories(matcher: ProductMatcher = Depends(lambda: product_matcher)):
    """List top-level product categories (e.g. personal, business)."""
    try:
        categories = sorted({p.get("category_name") for p in matcher.product_index.values() if p.get("category_name")})
        return {"categories": categories}
    except Exception as e:
        logger.error(f"Error listing product categories: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@api_router.get("/products/by-id/{product_id:path}", tags=["Products"])
async def api_get_product_by_id(
    product_id: str,
    include_details: bool = False,
    matcher: ProductMatcher = Depends(lambda: product_matcher),
):
    """
    Get product info from chunks: overview, benefits, general. When include_details=true, also returns faq.
    product_id can be either:
    - full doc_id (e.g. website:product:personal/save-and-invest/sure-deal-savings-plan), or
    - short key (e.g. personal/save-and-invest/sure-deal-savings-plan)
    """
    try:
        doc_id = _resolve_product_doc_id(matcher, product_id)
        product = matcher.get_product_by_id(doc_id)
        if not product:
            raise HTTPException(status_code=404, detail="Product not found")
        sections = _load_product_sections(doc_id)
        public_id = product.get("product_key") or matcher.get_public_id(doc_id) or doc_id
        out = {
            "product_id": public_id,
            "doc_id": doc_id,
            "name": product.get("name"),
            "category": product.get("category_name"),
            "subcategory": product.get("sub_category_name"),
            "url": product.get("url"),
            "overview": sections.get("overview", []),
            "benefits": sections.get("benefits", []),
            "general": sections.get("general", []),
        }
        if include_details:
            out["faq"] = sections.get("faq", [])
        return out
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting product: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@api_router.get("/products/{category}", tags=["Products"])
async def api_list_product_subcategories_or_products(category: str, matcher: ProductMatcher = Depends(lambda: product_matcher)):
    """
    List subcategories under a business unit (category), or products if category has none.
    Frontend: if subcategories is non-empty show them; else show products (or 404 if invalid category).
    """
    try:
        cat_lower = category.lower()
        subs = sorted(
            {
                p.get("sub_category_name")
                for p in matcher.product_index.values()
                if p.get("category_name", "").lower() == cat_lower and p.get("sub_category_name")
            }
        )
        products: List[Dict[str, Any]] = []
        if not subs:
            products = [
                _public_product(matcher, p)
                for p in matcher.product_index.values()
                if p.get("category_name", "").lower() == cat_lower
            ]
            if not products:
                raise HTTPException(status_code=404, detail="Category not found or has no products")
        return {"category": category, "subcategories": subs, "products": products}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing category: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@api_router.get("/products/{category}/{subcategory}", tags=["Products"])
async def api_list_products_in_subcategory(category: str, subcategory: str, matcher: ProductMatcher = Depends(lambda: product_matcher)):
    """List products in a category/subcategory. product_id is a short key (category/subcategory/slug)."""
    try:
        cat_lower = category.lower()
        sub_lower = subcategory.lower()
        items = [
            _public_product(matcher, p)
            for p in matcher.product_index.values()
            if p.get("category_name", "").lower() == cat_lower and p.get("sub_category_name", "").lower() == sub_lower
        ]
        if not items:
            raise HTTPException(status_code=404, detail="No products found for this category/subcategory")
        return {"category": category, "subcategory": subcategory, "products": items}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing products in subcategory: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@api_router.get("/products/{category}/{subcategory}/{product_slug}", tags=["Products"])
async def api_get_product_structured(
    category: str,
    subcategory: str,
    product_slug: str,
    matcher: ProductMatcher = Depends(lambda: product_matcher),
):
    """Structured product sections (overview, benefits, faq, etc.) by category/subcategory/slug."""
    doc_id = f"website:product:{category}/{subcategory}/{product_slug}"
    product = matcher.get_product_by_id(doc_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    sections = _load_product_sections(doc_id)
    public_id = product.get("product_key") or f"{category}/{subcategory}/{product_slug}"
    return {
        "product_id": public_id,
        "doc_id": doc_id,
        "name": product.get("name"),
        "category": product.get("category_name"),
        "subcategory": product.get("sub_category_name"),
        "url": product.get("url"),
        "overview": sections.get("overview", []),
        "benefits": sections.get("benefits", []),
        "payment_methods": sections.get("payment_methods", []),
        "general": sections.get("general", []),
        "faq": sections.get("faq", []),
    }


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

        # Run each logical step's validation + data shaping.
        await flow._step_personal_details(payload, data, internal_user_id)
        await flow._step_next_of_kin(payload, data, internal_user_id)
        await flow._step_previous_pa_policy(payload, data, internal_user_id)
        await flow._step_physical_disability(payload, data, internal_user_id)
        await flow._step_risky_activities(payload, data, internal_user_id)
        await flow._step_coverage_selection(payload, data, internal_user_id)
        await flow._step_upload_national_id(payload, data, internal_user_id)

        # Calculate premium using the same helper as the guided flow.
        plan = data.get("coverage_plan") or {}
        sum_assured = plan.get("sum_assured", 10_000_000)
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
        controller = MotorPrivateController(db, product_matcher)
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
async def end_session(session_id: str):
    """End a chatbot session."""
    try:
        state_manager.end_session(session_id)
        return {"message": "Session ended successfully"}

    except Exception as e:
        logger.error(f"Error ending session: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# API versioning: expose routes under /api/v1
app.include_router(api_router, prefix="/api/v1")

# Register applications router
try:
    from src.api.applications_router import api as applications_api

    app.include_router(applications_api, prefix="/api")
except Exception:
    pass

# Register Personal Accident quote forms router
try:
    from src.api.pa_quote_forms_router import api as pa_quote_api

    # Expose under versioned prefix
    app.include_router(pa_quote_api, prefix="/api/v1")
except Exception:
    pass


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
