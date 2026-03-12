"""
Conversational mode - RAG-powered free-form chat
"""

from typing import Any, Dict, List, Optional
import logging
import re
import time

logger = logging.getLogger(__name__)


def _is_greeting(message: str) -> bool:
    m = (message or "").strip().lower()
    if not m:
        return False
    # Keep it strict so we don't mis-classify real questions.
    return m in {"hi", "hello", "hey", "hey!", "hello!", "hi!", "good morning", "good afternoon", "good evening"}


def _detect_section_intent(message: str) -> str | None:
    m = (message or "").lower()
    # Benefits
    if any(k in m for k in ["benefit", "benefits", "advantages", "what do i get", "what do you cover"]):
        return "show_benefits"
    # Coverage
    if any(k in m for k in ["coverage", "covered", "what is covered", "what's covered", "what is included", "included"]):
        return "show_coverage"
    # Exclusions
    if any(k in m for k in ["exclusion", "exclusions", "not covered", "what is not covered", "what isn't covered", "limitations"]):
        return "show_exclusions"
    # Eligibility
    if any(k in m for k in ["eligibility", "eligible", "qualify", "requirements", "who can apply", "who is it for"]):
        return "show_eligibility"
    # Pricing
    if any(k in m for k in ["premium", "price", "pricing", "cost", "how much"]):
        return "show_pricing"
    return None


def _detect_digital_flow(message: str) -> str | None:
    m = (message or "").lower()
    if any(k in m for k in ["personal accident", "pa cover", "accident insurance", "accident cover", "pa insurance"]):
        return "personal_accident"
    if any(k in m for k in ["serenicare"]):
        return "serenicare"
    if any(k in m for k in ["motor private", "car insurance", "vehicle insurance", "motor insurance"]):
        return "motor_private"
    if any(k in m for k in ["travel insurance", "travel sure", "travel cover", "travel policy"]):
        return "travel_insurance"
    return None


def _digital_flow_search_hint(digital_flow: str | None) -> str | None:
    if digital_flow == "motor_private":
        return "Motor Insurance"
    if digital_flow == "travel_insurance":
        return "Travel Insurance"
    if digital_flow == "personal_accident":
        return "Personal Accident"
    if digital_flow == "serenicare":
        return "Serenicare"
    return None


def _resolve_doc_ids_for_digital_flow(product_matcher: Any, digital_flow: str | None, *, max_results: int = 2) -> List[str]:
    """Resolve likely product doc_ids for a detected guided flow.

    This is a fallback path when lexical product matching misses short queries
    like "car insurance" but we can still detect the intended flow.
    """
    if not digital_flow or product_matcher is None:
        return []

    direct_aliases = [
        digital_flow,
        digital_flow.replace("_", "-"),
        digital_flow.replace("_", " "),
    ]
    resolved: List[str] = []
    for alias in direct_aliases:
        try:
            if hasattr(product_matcher, "resolve_doc_id"):
                doc_id = product_matcher.resolve_doc_id(alias)
                if doc_id and doc_id not in resolved:
                    resolved.append(doc_id)
        except Exception:
            # Best effort only; continue with index-scoring fallback.
            continue
    if resolved:
        return resolved[:max_results]

    index = getattr(product_matcher, "product_index", None)
    if not isinstance(index, dict) or not index:
        return []

    alias_map: Dict[str, List[str]] = {
        "motor_private": ["motor private", "motor insurance", "car insurance", "vehicle insurance", "motor-insurance"],
        "travel_insurance": ["travel insurance", "travel sure", "travel policy", "travel cover"],
        "personal_accident": ["personal accident", "accident insurance", "accident cover", "pa cover"],
        "serenicare": ["serenicare", "health insurance", "medical cover"],
    }
    penalties: Dict[str, List[str]] = {
        # Prevent "car insurance" from drifting to business/commercial products.
        "motor_private": ["commercial", "business"],
    }

    candidates: List[tuple[int, str]] = []
    aliases = alias_map.get(digital_flow, [digital_flow.replace("_", " ")])
    negative_terms = penalties.get(digital_flow, [])

    for item in index.values():
        if not isinstance(item, dict):
            continue
        doc_id = item.get("doc_id") or item.get("product_id")
        if not doc_id:
            continue

        haystack = " ".join(
            [
                str(item.get("name") or ""),
                str(item.get("slug") or ""),
                str(item.get("product_key") or ""),
                str(item.get("doc_id") or ""),
            ]
        ).lower()
        score = 0
        for alias in aliases:
            alias_l = alias.lower()
            if alias_l and alias_l in haystack:
                score += 4 if " " in alias_l else 2
        for bad in negative_terms:
            if bad in haystack:
                score -= 3
        if score > 0:
            candidates.append((score, str(doc_id)))

    candidates.sort(key=lambda pair: pair[0], reverse=True)
    for _, doc_id in candidates:
        if doc_id not in resolved:
            resolved.append(doc_id)
        if len(resolved) >= max_results:
            break
    return resolved


def _is_broad_product_query(message: str) -> bool:
    m = (message or "").lower()
    if not m:
        return False
    broad_markers = [
        "policies",
        "policy",
        "options",
        "products",
        "plans",
        "covers",
        "types of",
        "available",
    ]
    if any(k in m for k in broad_markers):
        return True
    if "can i get" in m and any(k in m for k in ["insurance", "cover", "policy"]):
        return True
    return False


def _is_affirmative(message: str) -> bool:
    m = (message or "").strip().lower()
    return m in {"yes", "y", "yeah", "yep", "sure", "ok", "okay", "please", "go ahead", "go on"}


def _is_negative(message: str) -> bool:
    m = (message or "").strip().lower()
    return m in {"no", "n", "nope", "not now", "later", "maybe later"}


def _is_explicit_guided_intent(message: str) -> bool:
    m = (message or "").strip().lower()
    if not m:
        return False
    explicit_triggers = [
        "get a quote",
        "get a quotation",
        "get quotation",
        "can i get a quote",
        "can i get a quotation",
        "can i get quotation",
        "give me a quote",
        "provide a quote",
        "i want to apply",
        "i want to buy",
        "i want to purchase",
        "help me apply",
        "help me buy",
    ]
    if any(trigger in m for trigger in explicit_triggers):
        return True

    wants_quote = any(word in m for word in ["want", "need", "get"]) and any(word in m for word in ["quote", "quotation"])
    wants_purchase = any(word in m for word in ["want", "need", "help me", "can i"]) and any(word in m for word in ["apply", "buy", "purchase"])
    return wants_quote or wants_purchase


def _should_reuse_product_topic(message: str, topic: Dict[str, Any]) -> bool:
    if not topic or not topic.get("doc_id"):
        return False

    m = (message or "").strip().lower()
    if not m or _detect_digital_flow(m):
        return False

    if _detect_section_intent(m):
        return True

    contextual_phrases = [
        "what about",
        "how about",
        "what if",
        "tell me more",
        "more about",
        "is it",
        "does it",
        "can it",
        "would it",
        "that one",
        "this one",
        "what else",
        "how much is it",
        "is it expensive",
        "waiting period",
    ]
    if any(phrase in m for phrase in contextual_phrases):
        return True

    if re.search(r"\b(it|this|that|they|them|those|these)\b", m):
        return True

    follow_up_keywords = [
        "benefits",
        "coverage",
        "covered",
        "exclusions",
        "eligibility",
        "premium",
        "pricing",
        "price",
        "cost",
        "claim",
        "claims",
        "limit",
        "limits",
    ]
    tokens = re.findall(r"\b[\w']+\b", m)
    return len(tokens) <= 8 and any(keyword in m for keyword in follow_up_keywords)


def _augment_query_with_topic(message: str, topic_name: Optional[str], *, use_topic: bool) -> str:
    if not use_topic or not topic_name:
        return message

    topic_lower = topic_name.lower()
    message_lower = (message or "").lower()
    if topic_lower in message_lower:
        return message
    return f"{topic_name} {message}".strip()


def _is_followup_message(message: str) -> bool:
    m = (message or "").strip().lower()
    if not m:
        return False
    if _is_greeting(m):
        return False

    followup_starts = (
        "and ",
        "also ",
        "what about",
        "how about",
        "what if",
        "then ",
    )
    if m.startswith(followup_starts):
        return True

    if re.search(r"\b(it|this|that|they|them|those|these)\b", m):
        return True

    tokens = re.findall(r"\b[\w']+\b", m)
    if len(tokens) <= 7 and any(k in m for k in ["waiting period", "limit", "limits", "eligible", "price", "cost", "premium"]):
        return True

    return False


def _last_user_turn(conversation_history: List[Dict[str, Any]]) -> Optional[str]:
    for msg in reversed(conversation_history or []):
        role = (msg.get("role") or "").strip().lower()
        content = (msg.get("content") or "").strip()
        if role == "user" and content:
            return content
    return None


def _augment_query_with_history(message: str, conversation_history: List[Dict[str, Any]], *, use_history: bool) -> str:
    if not use_history:
        return message

    previous_user_turn = _last_user_turn(conversation_history)
    if not previous_user_turn:
        return message

    lowered = message.lower()
    if previous_user_turn.lower() in lowered:
        return message

    return f"Context from previous question: {previous_user_turn}. Follow-up question: {message}"


def _is_fallback_like_answer(answer: str) -> bool:
    lowered = (answer or "").strip().lower()
    if not lowered:
        return True
    fallback_markers = [
        "i'm having trouble retrieving",
        "i am having trouble retrieving",
        "i'm not sure based on the available information",
        "please try again in a moment",
        "please rephrase",
    ]
    return any(marker in lowered for marker in fallback_markers)


def _estimate_response_confidence(
    response: Dict[str, Any],
    retrieval_results: List[Dict[str, Any]],
    products: List[Any],
    filters: Dict[str, Any],
) -> float:
    answer = (response.get("answer") or "").strip()
    response_conf = response.get("confidence")

    if isinstance(response_conf, (int, float)):
        confidence = float(response_conf)
    else:
        if retrieval_results:
            scores = [float(h.get("score") or 0.0) for h in retrieval_results]
            avg_score = sum(scores) / len(scores) if scores else 0.0
            coverage = min(len(retrieval_results) / 5.0, 1.0)
        else:
            avg_score = 0.0
            coverage = 0.0

        min_score = 0.55
        if avg_score <= 0:
            score_norm = 0.0
        else:
            score_norm = (avg_score - min_score) / max(1.0 - min_score, 0.01)
            score_norm = max(0.0, min(1.0, score_norm))

        confidence = (0.7 * score_norm) + (0.3 * coverage)

    if _is_fallback_like_answer(answer):
        confidence = min(confidence, 0.25)
    elif not retrieval_results:
        confidence = min(confidence, 0.35)

    return round(max(0.05, min(confidence, 0.95)), 2)


def _build_section_query(product_name: str, section: str) -> str:
    base = product_name or "this insurance product"
    if section == "show_benefits":
        return f"List the key benefits of {base}. Keep it clear and structured."
    if section == "show_eligibility":
        return f"Explain eligibility requirements for {base}. Include who it is for and common requirements."
    if section == "show_coverage":
        return f"Explain what is covered under {base}. Provide a clear coverage summary."
    if section == "show_exclusions":
        return f"Explain common exclusions and what is not covered for {base}."
    if section == "show_pricing":
        return f"Explain how pricing/premiums work for {base}. If exact prices are not available, explain the factors that affect cost."
    return f"Explain {base} insurance product, its benefits, coverage, and eligibility."


def _build_overview_query(product_name: str) -> str:
    base = product_name or "this insurance product"
    return f"Explain {base} insurance product, its benefits, coverage, and eligibility."


def _infer_recommendation_hint(message: str) -> str | None:
    m = (message or "").lower()
    if "accident" in m:
        return "personal accident"
    if any(k in m for k in ["travel", "trip"]):
        return "travel insurance"
    if any(k in m for k in ["motor", "car", "vehicle", "auto"]):
        return "motor private"
    if any(k in m for k in ["medical", "health", "hospital"]):
        return "serenicare"
    return None


def _next_section_offer(action: str, *, is_digital: bool) -> tuple[str | None, str | None]:
    order = {
        "show_benefits": ("show_eligibility", "eligibility"),
        "show_eligibility": ("show_coverage", "coverage"),
        "show_coverage": ("show_exclusions", "exclusions"),
        "show_exclusions": ("show_pricing", "pricing"),
        "show_pricing": ("get_quote", "a quick quote") if is_digital else ("how_to_access", "how to access it"),
    }
    return order.get(action, (None, None))


# metrics functions
def _emit_metrics(db, metrics: list[Dict[str, Any]]) -> None:
    if db is None:
        return
    if not metrics:
        return
    try:
        from datetime import datetime
        if hasattr(db, "add_rag_metrics"):
            now = datetime.utcnow()
            for metric in metrics:
                metric.setdefault("created_at", now)
            db.add_rag_metrics(metrics)
        elif hasattr(db, "add_rag_metric"):
            now = datetime.utcnow()
            for metric in metrics:
                metric.setdefault("created_at", now)
                db.add_rag_metric(**metric)
        else:
            logger.warning("[metrics] DB adapter missing add_rag_metrics; count=%s", len(metrics))
    except Exception as exc:
        logger.warning("[metrics] Failed to record metrics: %s", exc)


def _metric_payload(metric_type: str, value: float, conversation_id: Optional[str]) -> Dict[str, Any]:
    return {
        "metric_type": metric_type,
        "value": float(value),
        "conversation_id": conversation_id,
    }


class ConversationalMode:
    def __init__(self, rag_system, product_matcher, state_manager):
        self.rag = rag_system
        self.product_matcher = product_matcher
        self.state_manager = state_manager

        # Optional LLM-based small-talk responder.
        try:
            from src.chatbot.intent_classifier import SmallTalkResponder

            self.small_talk_responder = SmallTalkResponder()
        except Exception:
            self.small_talk_responder = None

        # Lazily import response processor to avoid circular imports at module load time
        try:
            from src.response_processor import ResponseProcessor

            self.response_processor = ResponseProcessor(state_manager=self.state_manager)
        except Exception:
            # Fallback: no response processor available
            self.response_processor = None

    async def process(self, message: str, session_id: str, user_id: str, form_data: Optional[Dict[str, Any]] = None, db=None) -> Dict:
        """Process message in conversational mode"""
        start_time = time.time()

        session_for_id = self.state_manager.get_session(session_id) or {}
        conversation_id: Optional[str] = session_for_id.get("conversation_id") or session_id

        # Backward-compatible: if the frontend still sends a product-guide action via form_data,

        # Backward-compatible: if the frontend still sends a product-guide action via form_data,
        # handle it, but we no longer *emit* buttons/actions as the primary UX.
        if form_data and isinstance(form_data, dict) and form_data.get("action"):
            return await self._process_product_guide_action(form_data, session_id)

        # Handle pending agent handoff confirmation (ask -> wait for yes/no).
        pending_ctx = dict((session_for_id.get("context") or {}))
        if pending_ctx.get("pending_agent_offer"):
            if _is_affirmative(message):
                pending_ctx.pop("pending_agent_offer", None)
                self.state_manager.update_session(session_id, {"context": pending_ctx})
                try:
                    from src.integrations.policy.escalation_service import EscalationService

                    EscalationService(state_manager=self.state_manager).escalate_to_human(
                        session_id=session_id,
                        reason="user_requested_agent",
                        user_id=user_id,
                        metadata={"conversation_id": conversation_id},
                    )
                except Exception:
                    self.state_manager.mark_escalated(
                        session_id,
                        reason="user_requested_agent",
                        metadata={"conversation_id": conversation_id},
                    )
                return {
                    "mode": "escalated",
                    "response": "Message sent to human agent.",
                    "escalated": True,
                    "agent_id": None,
                }
            if _is_negative(message):
                pending_ctx.pop("pending_agent_offer", None)
                self.state_manager.update_session(session_id, {"context": pending_ctx})
                return {
                    "mode": "conversational",
                    "response": "No problem. Any other question you would like me to help you with?",
                    "confidence": 1.0,
                }
            # If user says something else, clear the pending offer and continue normally.
            if (message or "").strip():
                pending_ctx.pop("pending_agent_offer", None)
                self.state_manager.update_session(session_id, {"context": pending_ctx})

        escalation_state = self.state_manager.get_escalation_state(session_id)
        if escalation_state.get("escalated"):
            logger.info(f"Routing message to human agent for session {session_id}")
            agent_id = escalation_state.get("agent_id")
            status_msg = "Message sent to human agent."
            if agent_id:
                status_msg = f"Message sent to human agent ({agent_id})."
            return {
                "mode": "escalated",
                "response": status_msg,
                "escalated": True,
                "agent_id": agent_id,
            }

        # If we previously offered to share a section (e.g., benefits) and the user replies "yes",
        # convert that into the corresponding section answer.
        session = self.state_manager.get_session(session_id) or {}
        ctx = dict(session.get("context") or {})
        pending_offer = ctx.get("pending_section_offer")
        if pending_offer:
            if _is_affirmative(message):
                ctx.pop("pending_section_offer", None)
                self.state_manager.update_session(session_id, {"context": ctx})
                return await self._process_product_guide_action({"action": str(pending_offer)}, session_id)
            if _is_negative(message):
                ctx.pop("pending_section_offer", None)
                self.state_manager.update_session(session_id, {"context": ctx})
            elif (message or "").strip():
                # User asked something else; clear the pending offer to avoid accidental triggers.
                ctx.pop("pending_section_offer", None)
                self.state_manager.update_session(session_id, {"context": ctx})

        # If the user is explicitly asking for a product section (benefits/coverage/etc),
        # resolve the product and answer via the product-guide path (filters by doc_id).
        if form_data is None:
            section_action = _detect_section_intent(message)
            if section_action:
                products = self.product_matcher.match_products(message, top_k=1)

                # Prefer explicit mention in message, else fall back to last product topic.
                session = self.state_manager.get_session(session_id) or {}
                ctx = dict(session.get("context") or {})

                picked = products[0][2] if products else None
                if picked:
                    ctx["product_topic"] = {
                        "digital_flow": _detect_digital_flow(message),
                        "name": picked.get("name"),
                        "doc_id": picked.get("product_id"),
                        "url": picked.get("url"),
                    }
                    self.state_manager.update_session(session_id, {"context": ctx})

                # If we still don't know which product, ask a single clarifying question.
                topic = (ctx.get("product_topic") or {}) if isinstance(ctx, dict) else {}
                if not topic.get("doc_id"):
                    return {
                        "mode": "conversational",
                        "response": (
                            "Sure 🙂 Which product do you mean?\n"
                            "Examples: ✈️ Travel Sure Plus, 🩹 Personal Accident, 🏥 Serenicare, 🚗 Motor Private."
                        ),
                        "intent": "clarify_product",
                        "confidence": 0.9,
                    }

                return await self._process_product_guide_action({"action": section_action}, session_id)

        # NO_RETRIEVAL intents (greetings, small talk, thanks, goodbyes).
        if form_data is None:
            no_ret_kind = self._detect_no_retrieval_intent(message)
            if no_ret_kind:
                # Small-talk/greeting/thanks/goodbye: skip RAG.
                if self.small_talk_responder is not None:
                    try:
                        answer_text = await self.small_talk_responder.respond(message, no_ret_kind)
                    except Exception:
                        answer_text = self._build_no_retrieval_reply(no_ret_kind)
                else:
                    answer_text = self._build_no_retrieval_reply(no_ret_kind)

                _emit_metrics(
                    db,
                    [
                        _metric_payload(
                            "response_latency",
                            time.time() - start_time,
                            conversation_id,
                        )
                    ],
                )

                if hasattr(db, "add_conversation_event"):
                    try:
                        db.add_conversation_event(
                            conversation_id=conversation_id or session_id,
                            event_type="intent",
                            payload={
                                "intent": no_ret_kind.lower(),
                                "intent_type": "NO_RETRIEVAL",
                                "confidence": 1.0,
                                "user_message": message,
                                "response_latency": time.time() - start_time,
                            },
                        )
                    except Exception as exc:
                        logger.warning("[metrics] Failed to record conversation event: %s", exc)

                return {
                    "mode": "conversational",
                    "response": answer_text,
                    "sources": [],
                    "products_matched": [],
                    "intent": no_ret_kind.lower(),
                    "intent_type": "NO_RETRIEVAL",
                    "suggested_action": None,
                    "confidence": 1.0,
                }

        # Detect coarse intent (quote/buy/learn/etc.)
        broad_query = _is_broad_product_query(message)
        intent = self._detect_intent(message)
        explicit_guided_intent = _is_explicit_guided_intent(message)
        detected_product = _detect_digital_flow(message)
        if broad_query and intent in ("learn", "general"):
            intent = "discover"

        # Match relevant products
        products = self.product_matcher.match_products(message, top_k=3)

        session = self.state_manager.get_session(session_id) or {}
        ctx = dict(session.get("context") or {})
        topic = (ctx.get("product_topic") or {}) if isinstance(ctx, dict) else {}
        should_reuse_topic = _should_reuse_product_topic(message, topic)
        recent_history = self._get_recent_history(session_id)
        query_with_topic = _augment_query_with_topic(
            message,
            topic.get("name"),
            use_topic=should_reuse_topic,
        )
        if detected_product:
            query_with_topic = _augment_query_with_topic(
                query_with_topic,
                _digital_flow_search_hint(detected_product),
                use_topic=True,
            )
        should_use_history = _is_followup_message(message) and bool(recent_history) and not _detect_digital_flow(message)
        retrieval_query = _augment_query_with_history(
            query_with_topic,
            recent_history,
            use_history=should_use_history,
        )

        # Build filters for RAG retrieval.
        filters: Dict[str, Any] = {}
        if products:
            top_score = float(products[0][0] or 0.0)
            second_score = float(products[1][0] or 0.0) if len(products) > 1 else 0.0
            is_confident = (top_score >= 1.2) and (top_score >= second_score + 0.5)

            logger.info(
                "[RAG] Product match: top_score=%s, is_confident=%s, detected=%s, products=%s",
                top_score, is_confident, detected_product, [p[2]["name"] for p in products[:1]]
            )

            if intent == "compare":
                # Comparing products: allow multiple doc_ids.
                filters["products"] = [p[2]["product_id"] for p in products[:3]]
            elif should_reuse_topic and topic.get("doc_id"):
                filters["products"] = [topic["doc_id"]]
                logger.info("[RAG] Reusing session product topic filter: %s", topic["doc_id"])
            elif detected_product:
                # User explicitly asked about a specific product - filter to that product only
                # Find matching product in the list
                for p in products:
                    if p[2].get("product_id") and detected_product in p[2].get("product_id", ""):
                        filters["products"] = [p[2]["product_id"]]
                        logger.info("[RAG] Applying explicit product filter: %s", p[2]["product_id"])
                        break
            elif is_confident and not broad_query:
                # Single-product intent with high confidence: restrict to the best match.
                filters["products"] = [products[0][2]["product_id"]]
                logger.info("[RAG] Applying confident product filter: %s", products[0][2]["product_id"])
        elif detected_product:
            detected_doc_ids = _resolve_doc_ids_for_digital_flow(self.product_matcher, detected_product)
            if detected_doc_ids:
                filters["products"] = detected_doc_ids
                logger.info("[RAG] Applying digital flow fallback filter: flow=%s, doc_ids=%s", detected_product, detected_doc_ids)
        elif should_reuse_topic and topic.get("doc_id"):
            filters["products"] = [topic["doc_id"]]
            logger.info("[RAG] Reusing session product topic filter without fresh product match: %s", topic["doc_id"])

        # Retrieve relevant documents (hybrid BM25 + vector via APIRAGAdapter).
        retrieval_results = await self.rag.retrieve(query=retrieval_query, filters=filters or None, top_k=None)

        # Generate response
        response = await self.rag.generate(query=retrieval_query, context_docs=retrieval_results, conversation_history=recent_history)

        # ---- Record RAG metrics ----
        confidence = _estimate_response_confidence(response, retrieval_results, products, filters)
        sources = response.get("sources", [])
        metrics_to_emit = [
            _metric_payload("confidence_score", confidence, conversation_id),
            _metric_payload("retrieval_accuracy", min(len(sources) / 5.0, 1.0), conversation_id),
        ]
        if not sources:
            metrics_to_emit.append(_metric_payload("fallbacks", 1.0, conversation_id))
        # ---- End metrics ----

        # --- Escalation/handover logic ---
        session = self.state_manager.get_session(session_id) or {}

        # If confidence is low, suggest handover button
        show_handover_button = False
        if confidence < 0.4:
            show_handover_button = True

        products_matched_names = [p[2]["name"] for p in products] if products else []
        if not products_matched_names and topic.get("name") and should_reuse_topic:
            products_matched_names = [topic["name"]]
        if self.response_processor:
            processed = self.response_processor.process_response(
                raw_response=response.get("answer"),
                user_input=message,
                confidence=confidence,
                conversation_state=session,
                session_id=session_id,
                # Low confidence triggers a human-offer prompt; escalation waits for user confirmation.
                user_id=user_id,
                products_matched=products_matched_names,
            )
            answer_text = processed.get("message")
            follow_up_flag = processed.get("follow_up", False)
            processed_reason = (processed.get("metadata") or {}).get("reason")
            if processed.get("offer_human"):
                sess = self.state_manager.get_session(session_id) or {}
                ctx = dict(sess.get("context") or {})
                ctx["pending_agent_offer"] = True
                self.state_manager.update_session(session_id, {"context": ctx})
        else:
            answer_text = response["answer"]
            follow_up_flag = False
            processed_reason = None

        if processed_reason == "incomplete_input" and not products:
            recommendation = await self._build_recommendation_response(message, session_id)
            if recommendation:
                answer_text = recommendation
                follow_up_flag = True

        # Determine product topic for follow-up guidance.
        digital_flow = _detect_digital_flow(message) or topic.get("digital_flow")
        top_product = products[0][2] if products else (topic if topic.get("doc_id") else None)

        if digital_flow or top_product:
            topic_name = None
            topic_url = None
            topic_doc_id = None

            if top_product:
                topic_name = top_product.get("name")
                topic_url = top_product.get("url")
                topic_doc_id = top_product.get("product_id") or top_product.get("doc_id")

            # Persist topic in session context (so buttons can work).
            session = self.state_manager.get_session(session_id) or {}
            ctx = dict(session.get("context") or {})
            ctx["product_topic"] = {
                "digital_flow": digital_flow,
                "name": topic_name,
                "doc_id": topic_doc_id,
                "url": topic_url,
            }
            self.state_manager.update_session(session_id, {"context": ctx})

        # Append a natural follow-up prompt when the user is learning about a product.
        follow_up_prompt = None
        related_products_block = None
        if broad_query and products:
            related_names = [p[2].get("name") for p in products if p[2].get("name")]
            if related_names:
                related_list = "\n".join([f"- {name}" for name in related_names[:4]])
                related_products_block = f"Related products you can consider:\n{related_list}"
        if broad_query and "accident" in (message or "").lower():
            follow_up_prompt = (
                "Is this about Personal Accident cover for an individual, or Group Personal Accident for employees?"
            )
        elif intent in ("learn", "general", "compare", "discover") and (digital_flow or top_product):
            topic_label = topic_name or "this product"
            answer_lower = (answer_text or "").lower()
            mentions_benefits = "benefit" in answer_lower

            # Offer benefits only if we didn't already include them.
            if mentions_benefits:
                follow_up_prompt = f"Would you like anything else about {topic_label}, such as pricing or eligibility?"
            else:
                follow_up_prompt = f"Should I share the benefits of {topic_label}?"

            # Store what a simple "yes" should do next.
            session = self.state_manager.get_session(session_id) or {}
            ctx = dict(session.get("context") or {})
            ctx["pending_section_offer"] = "show_benefits"
            self.state_manager.update_session(session_id, {"context": ctx})

        # Sources removed from conversation response per user request
        # sources_block = self._format_sources(response.get("sources", []))
        # if sources_block:
        #     answer_text = f"{answer_text}\n\n{sources_block}" if answer_text else sources_block

        # If response processor already queued a follow-up, prefer that text over our generic follow_up_prompt
        if follow_up_flag:
            # If the processor flagged a follow-up, we assume it already queued it.
            # Keep the model-provided message as-is.
            pass
        elif follow_up_prompt:
            answer_parts = [p for p in [answer_text, related_products_block, follow_up_prompt] if p]
            answer_text = "\n\n".join(answer_parts)
        elif related_products_block:
            answer_text = f"{answer_text}\n\n{related_products_block}" if answer_text else related_products_block

        # Determine if we should suggest guided mode
        suggested_action = None
        if explicit_guided_intent:
            digital_flow = _detect_digital_flow(message)

            if digital_flow:
                suggested_action = {
                    "type": "switch_to_guided",
                    "message": "Ready to get started? I can guide you through a few questions to provide a quote.",
                    "flow": "journey",
                    "initial_data": {"product_flow": digital_flow},
                    "buttons": [
                        {"label": "Get quotation", "action": "get_quotation"},
                        {"label": "Not now", "action": "continue_chat"},
                    ],
                }
            elif products:
                top = products[0][2]
                suggested_action = {
                    "type": "switch_to_guided",
                    "message": f"{top.get('name', 'This product')} requires agent assistance. Please share your contact details.",
                    "flow": "agent_handoff",
                    "initial_data": {"product_name": top.get("name"), "product_url": top.get("url")},
                    "buttons": [
                        {"label": "Share details", "action": "start_guided"},
                        {"label": "Not now", "action": "continue_chat"},
                    ],
                }
            else:
                suggested_action = {
                    "type": "switch_to_guided",
                    "message": "Let me help you find the right solution. Please share your details.",
                    "flow": "agent_handoff",
                    "buttons": [
                        {"label": "Share details", "action": "start_guided"},
                        {"label": "Not now", "action": "continue_chat"},
                    ],
                }
        elif intent == "discover" and products:
            suggested_action = {
                "type": "show_product_cards",
                "message": "Here are some products that might interest you:",
                "products": [self._generate_product_card(p[2]) for p in products],
            }

        # No product-guide buttons by default; users can reply in free text.

        response_latency = time.time() - start_time
        metrics_to_emit.append(
            _metric_payload("response_latency", response_latency, conversation_id)
        )
        _emit_metrics(db, metrics_to_emit)

        if hasattr(db, "add_conversation_event"):
            try:
                top_product = products[0][2] if products else {}
                db.add_conversation_event(
                    conversation_id=conversation_id or session_id,
                    event_type="intent",
                    payload={
                        "intent": intent,
                        "intent_type": "INFORMATIONAL",
                        "confidence": response.get("confidence", 0.5),
                        "user_message": message,
                        "response_latency": response_latency,
                        "product_name": top_product.get("name"),
                        "product_id": top_product.get("product_id"),
                        "category": top_product.get("category_name"),
                        "subcategory": top_product.get("sub_category_name"),
                    },
                )
            except Exception as exc:
                logger.warning("[metrics] Failed to record conversation event: %s", exc)

        return {
            "mode": "conversational",
            "response": answer_text,
            "sources": response.get("sources", []),
            "products_matched": [p[2]["name"] for p in products],
            "intent": intent,
            "intent_type": "INFORMATIONAL",
            "suggested_action": suggested_action,
            "confidence": confidence,
            "show_handover_button": show_handover_button,
        }

    async def _process_product_guide_action(self, form_data: Dict[str, Any], session_id: str) -> Dict:
        action = str(form_data.get("action") or "").strip()

        session = self.state_manager.get_session(session_id) or {}
        ctx = session.get("context") or {}
        topic = (ctx.get("product_topic") or {}) if isinstance(ctx, dict) else {}

        digital_flow = topic.get("digital_flow")
        product_name = topic.get("name") or (digital_flow.replace("_", " ").title() if digital_flow else None)
        doc_id = topic.get("doc_id")
        url = topic.get("url")

        # Quote button: frontend should start guided journey (digital only).
        # The router handles action=get_quotation and will immediately return the first product form/cards.
        if action == "get_quote" and digital_flow:
            return {
                "mode": "conversational",
                "response": "Sure — click 'Get quotation' to begin.",
                "suggested_action": {
                    "type": "switch_to_guided",
                    "flow": "journey",
                    "initial_data": {"product_flow": digital_flow},
                    "buttons": [{"label": "Get quotation", "action": "get_quotation"}],
                },
            }

        if action == "how_to_access":
            msg = "This product is not available as a digital buy/quote journey in this chatbot. "
            msg += "To access it, please visit an Old Mutual branch/agent or contact customer support."
            if url:
                msg += f"\n\nMore details: {url}"
            return {
                "mode": "conversational",
                "response": msg,
            }

        query = _build_section_query(product_name or "", action)
        filters = {"products": [doc_id]} if doc_id else None
        hits = await self.rag.retrieve(query=query, filters=filters)
        gen = await self.rag.generate(query=query, context_docs=hits, conversation_history=self._get_recent_history(session_id))

        # Process generation through ResponseProcessor if available so follow-ups/fallbacks are handled consistently
        session = self.state_manager.get_session(session_id) or {}
        if self.response_processor:
            processed = self.response_processor.process_response(
                raw_response=gen.get("answer"),
                user_input=query,
                confidence=gen.get("confidence", 0.0),
                conversation_state=session,
                session_id=session_id,
            )
            gen_text = processed.get("message")
            follow_up_flag = processed.get("follow_up", False)
        else:
            gen_text = gen.get("answer")
            follow_up_flag = False

        next_action, next_label = _next_section_offer(action, is_digital=bool(digital_flow))

        follow_up = "Do you have any more questions?"
        if next_action and next_label:
            follow_up = (
                f"Do you have any more questions, or should I share the {next_label}? "
                f"Reply 'yes' for {next_label}, or type your next question."
            )

            # Store what a simple "yes" should do next.
            session = self.state_manager.get_session(session_id) or {}
            ctx = dict(session.get("context") or {})
            ctx["pending_section_offer"] = next_action
            self.state_manager.update_session(session_id, {"context": ctx})

        response_text = gen_text
        if not follow_up_flag and follow_up:
            response_text = f"{gen_text}\n\n{follow_up}" if gen_text else follow_up

        return {
            "mode": "conversational",
            "response": response_text,
        }

    async def _build_recommendation_response(self, message: str, session_id: str) -> Optional[str]:
        hint = _infer_recommendation_hint(message)
        if not hint:
            return None

        rec_products = self.product_matcher.match_products(hint, top_k=1)
        if not rec_products:
            return None

        top_score, _, product = rec_products[0]
        if float(top_score or 0.0) < 1.0:
            return None

        hint_tokens = set(re.findall(r"\b[\w']+\b", hint.lower()))
        hint_tokens -= {"insurance", "cover", "policy", "plan", "personal", "business"}
        name_tokens = set(re.findall(r"\b[\w']+\b", (product.get("name") or "").lower()))
        slug_tokens = set(re.findall(r"\b[\w']+\b", (product.get("slug") or "").lower()))
        if hint_tokens and not (hint_tokens & name_tokens or hint_tokens & slug_tokens):
            return None

        product_name = product.get("name") or hint.title()
        product_id = product.get("product_id")

        query = _build_overview_query(product_name)
        filters = {"products": [product_id]} if product_id else None
        hits = await self.rag.retrieve(query=query, filters=filters)
        gen = await self.rag.generate(query=query, context_docs=hits, conversation_history=self._get_recent_history(session_id))

        explanation = (gen.get("answer") or "").strip()
        if "accident" in hint.lower():
            question = (
                "Is this about Personal Accident cover for an individual, or Group Personal Accident for employees?"
            )
        else:
            question = f"Is {product_name} the cover you meant, or should I suggest something else?"

        parts = [p for p in [explanation, question] if p]

        session = self.state_manager.get_session(session_id) or {}
        ctx = dict(session.get("context") or {})
        ctx["product_topic"] = {
            "digital_flow": _detect_digital_flow(hint),
            "name": product_name,
            "doc_id": product_id,
            "url": product.get("url"),
        }
        self.state_manager.update_session(session_id, {"context": ctx})

        return "\n\n".join(parts)

    def _detect_intent(self, message: str) -> str:
        """Detect coarse user intent from message (quote/buy/learn/compare/discover/claim/general)."""
        message_lower = message.lower()

        # Quote/Purchase intents
        if any(word in message_lower for word in ["quote", "how much", "price", "cost", "premium"]):
            return "quote"

        if any(word in message_lower for word in ["buy", "purchase", "apply", "get insurance"]):
            return "buy"

        # Discovery / learning intents
        if any(word in message_lower for word in ["what is", "tell me about", "explain", "how does"]):
            return "learn"

        if any(word in message_lower for word in ["compare", "difference", "vs", "versus"]):
            return "compare"

        if any(word in message_lower for word in ["need", "looking for", "want", "recommend"]):
            return "discover"

        # Claims/Support
        if any(word in message_lower for word in ["claim", "file", "submit"]):
            return "claim"

        # Default
        return "general"

    def _detect_no_retrieval_intent(self, message: str) -> Optional[str]:
        """
        Detect intents that should never trigger retrieval (NO_RETRIEVAL):
        GREETING, SMALL_TALK, THANKS, GOODBYE.
        """
        m = (message or "").strip().lower()
        if not m:
            return None

        # Greetings
        if _is_greeting(m):
            return "GREETING"

        # Thanks / appreciation
        thanks_phrases = {
            "thanks",
            "thank you",
            "thank you!",
            "thanks!",
            "thx",
            "thank u",
        }
        if m in thanks_phrases:
            return "THANKS"

        # Goodbyes
        goodbye_phrases = {
            "bye",
            "goodbye",
            "bye!",
            "goodbye!",
            "see you",
            "see you later",
        }
        if m in goodbye_phrases:
            return "GOODBYE"

        # Simple small talk
        small_talk_phrases = {
            "how are you",
            "how are you?",
            "how are u",
            "how are u?",
            "how's it going",
            "how's it going?",
            "hi",
            "whatsapp",
            "hello",
        }
        if m in small_talk_phrases:
            return "SMALL_TALK"

        return None

    def _build_no_retrieval_reply(self, kind: str) -> str:
        """
        Build a conversational reply for NO_RETRIEVAL intents without hitting RAG.
        """
        kind = (kind or "").upper()

        if kind == "GREETING":
            return (
                "Hey! I’m MIA, your Old Mutual assistant.\n"
                "You can ask me about our products, benefits, coverage, or how to get a quote."
            )
        if kind == "THANKS":
            return "You’re welcome! If you have any more questions about Old Mutual products or services, I’m here to help."
        if kind == "GOODBYE":
            return "You’re welcome. Feel free to come back any time you need help with Old Mutual products or services."
        if kind == "SMALL_TALK":
            return "I’m doing well, thank you for asking. How can I help you with Old Mutual products or services today?"

        # Fallback – should rarely be hit.
        return "How can I help you with Old Mutual products or services today?"

    def _get_recent_history(self, session_id: str, limit: int = 5) -> List[Dict]:
        """Get recent conversation history.

        Fast path: reads the rolling ``recent_messages`` buffer stored in the
        Redis session so follow-up turns never need a PostgreSQL round-trip.
        Falls back to PostgreSQL on cold start (e.g. after a server restart
        before the first reply has been saved this session).
        """
        session = self.state_manager.get_session(session_id)
        if not session:
            return []

        # Redis cache (fast path)
        cached = session.get("recent_messages")
        if cached:
            return cached[-limit:]

        # Cold-start fallback: read from PostgreSQL
        messages = self.state_manager.db.get_conversation_history(session["conversation_id"], limit=limit)
        return [{"role": msg.role, "content": msg.content} for msg in reversed(messages)]

    def _generate_product_card(self, product: Dict) -> Dict:
        """Generate product card data"""
        return {
            "product_id": product.get("product_key") or product["product_id"],
            "doc_id": product.get("doc_id") or product.get("product_id"),
            "name": product["name"],
            "category": product.get("category_name", ""),
            "description": product.get("description", ""),
            "min_premium": product.get("min_premium"),
            "actions": [{"type": "learn_more", "label": "Learn More"}, {"type": "get_quote", "label": "Get a Quote"}],
        }

    def _format_sources(self, sources: List[Dict]) -> str:
        if not sources:
            return ""

        items = []
        seen = set()
        for s in sources:
            payload = s.get("payload") or s
            title = (payload.get("title") or "Source").strip()
            url = (payload.get("url") or "").strip()
            if not url:
                continue
            key = (title, url)
            if key in seen:
                continue
            seen.add(key)
            items.append(f"- {title}: {url}")

        if not items:
            return ""
        return "Sources:\n" + "\n".join(items)
