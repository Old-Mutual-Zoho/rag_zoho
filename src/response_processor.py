"""Response processing utilities.

This module integrates follow-up detection, incomplete input checks and fallback triggering.
"""
from typing import Any, Dict, Optional
import re
import logging

from .followup_manager import FollowUpManager
from .fallback_handler import FallbackHandler
from .error_handler import ErrorHandler

logger = logging.getLogger(__name__)


class ResponseProcessor:
    """Process raw responses from the RAG/LLM layer and determine next actions.

    Responsibilities:
    - Detect follow-up questions contained in the model response.
    - Detect incomplete or ambiguous user input and ask clarifying questions.
    - Trigger fallback handling when confidence is low or no useful answer exists.
    - Normalize final output to a consistent dict the rest of the app can consume.

    Supports persisting follow-ups into the provided StateManager (session store).
    """

    DEFAULT_CONFIDENCE_THRESHOLD = 0.2

    # Valid single-word insurance-related queries that should not be flagged as incomplete
    VALID_SINGLE_WORDS = {
        'claims', 'claim', 'investment', 'investments', 'serenicare',
        'travel', 'motor', 'accident', 'health', 'life', 'funeral',
        'benefits', 'coverage', 'eligibility', 'premium', 'quote',
        'exclusions', 'exclusion', 'pricing', 'policy', 'policies',
        'underwriting', 'payout', 'payouts', 'deductible', 'deductibles',
        'copay', 'copayment', 'reimbursement', 'hospitalization',
        'outpatient', 'inpatient', 'maternity', 'dental', 'vision',
        'disability', 'annuity', 'annuities', 'retirement', 'pension',
        'savings', 'endowment', 'medical', 'surgical', 'emergency'
    }

    # Valid two-word phrase patterns
    VALID_TWO_WORD_PHRASES = [
        'travel insurance', 'motor insurance', 'personal accident',
        'life insurance', 'funeral cover', 'get quote', 'buy insurance',
        'claim process', 'motor private', 'health cover', 'medical cover',
        'insurance policy', 'insurance quote', 'insurance premium',
        'travel cover', 'accident cover', 'car insurance', 'vehicle insurance',
        'insurance products', 'insurance plans', 'policy details',
        'premium calculator', 'quote calculator', 'cover options'
    ]

    def __init__(self,
                 followup_manager: Optional[FollowUpManager] = None,
                 fallback_handler: Optional[FallbackHandler] = None,
                 error_handler: Optional[ErrorHandler] = None,
                 state_manager: Optional[Any] = None,
                 confidence_threshold: Optional[float] = None):
        self.followup_manager = followup_manager or FollowUpManager()
        self.fallback_handler = fallback_handler or FallbackHandler()
        self.error_handler = error_handler or ErrorHandler()
        # Optional StateManager (provides get_session/update_session for persistent session storage)
        self.state_manager = state_manager
        # Allow configurable confidence threshold
        self.confidence_threshold = confidence_threshold or self.DEFAULT_CONFIDENCE_THRESHOLD

    def process_response(
        self,
        raw_response: str,
        user_input: str,
        confidence: float,
        conversation_state: Dict[str, Any],
        *,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        products_matched: Optional[list] = None,
    ) -> Dict[str, Any]:
        """Return a normalized dict with keys: message, follow_up (optional), fallback (optional), metadata.

        If state_manager and session_id are provided follow-ups will be queued into the persistent session store.
        When products_matched is provided (e.g. ["Serenicare"]), short queries that match a product name
        are not treated as incomplete, so the RAG answer is returned instead of a clarifying question.
        """
        try:
            logger.debug("Processing response: confidence=%s, user_input=%s", confidence, user_input)

            # Basic sanitation
            message = (raw_response or "").strip()

            # Detect errors or model failure signatures
            if not message or message.lower().startswith("error"):
                logger.warning("Empty or error-like model response detected")
                payload = self.fallback_handler.generate_fallback(
                    user_input,
                    reason="empty_or_error",
                    conversation_state=conversation_state,
                    session_id=session_id,
                    user_id=user_id,
                )
                # Persist fallback into session store if available
                if self.state_manager and session_id:
                    self.state_manager.update_session(session_id, {"fallbacks": conversation_state.get("fallbacks", [])})
                return payload

            # If user input looks incomplete, ask a clarifying question — unless the query
            # matches a product we already resolved (e.g. user typed "serenicare" and we have Serenicare).
            # Also skip this check if products_matched list is populated, since that means we found relevant products.
            has_matched_products = products_matched is not None and len(products_matched) > 0
            query_matches = self._query_matches_product(user_input, products_matched)

            if self._is_incomplete_input(user_input) and not has_matched_products and not query_matches:
                logger.info(
                    "Incomplete input detected: user_input='%s', has_products=%s, query_matches=%s",
                    user_input, has_matched_products, query_matches
                )
                question = self.followup_manager.create_clarifying_question(user_input)
                # Persist followup in session if possible
                if self.state_manager and session_id:
                    self.followup_manager.queue_followup_session(session_id, self.state_manager, question)
                else:
                    self.followup_manager.queue_followup(conversation_state, question)
                return {
                    "message": question,
                    "follow_up": True,
                    "fallback": False,
                    "metadata": {"reason": "incomplete_input"},
                }

            # Low confidence => ask a clarification question instead of fallback.
            if confidence is not None and confidence < self.confidence_threshold:
                logger.info("Low confidence (%.2f) - asking clarification", confidence)
                question = self.followup_manager.create_clarifying_question(user_input)
                if self.state_manager and session_id:
                    self.followup_manager.queue_followup_session(session_id, self.state_manager, question)
                else:
                    self.followup_manager.queue_followup(conversation_state, question)
                return {
                    "message": question,
                    "follow_up": True,
                    "fallback": False,
                    "metadata": {"reason": "low_confidence_clarification", "confidence": confidence},
                }

            # Detect whether model response contains a follow-up question for the user
            if self._contains_follow_up_question(message):
                question_text = self.followup_manager.extract_followup_from_text(message)
                if self.state_manager and session_id:
                    self.followup_manager.queue_followup_session(session_id, self.state_manager, question_text)
                else:
                    self.followup_manager.queue_followup(conversation_state, question_text)
                return {
                    "message": message,
                    "follow_up": True,
                    "fallback": False,
                    "metadata": {"reason": "model_asked_follow_up"},
                }

            # Default: a normal answer
            return {
                "message": message,
                "follow_up": False,
                "fallback": False,
                "metadata": {"confidence": confidence},
            }

        except Exception as e:
            logger.exception("Exception while processing response")
            return self.error_handler.handle_exception(e, context={"raw_response": raw_response, "user_input": user_input})

    # Heuristics
    @staticmethod
    def _contains_follow_up_question(text: str) -> bool:
        # Simple heuristic: presence of a question sentence in response that appears addressed to the user
        # e.g. "Do you want...", "Would you like...", or any trailing question mark
        question_patterns = [r"\bdo you\b", r"\bwould you\b", r"\bcan you\b", r"\bwould you like\b"]
        lowered = text.lower()
        if '?' in text:
            return True
        for p in question_patterns:
            if re.search(p, lowered):
                return True
        return False

    @classmethod
    def _is_incomplete_input(cls, user_input: str) -> bool:
        """Check if user input appears too short or vague to process.

        Note: This should be used in conjunction with product matching checks,
        as product names alone (e.g., "serenicare") are valid complete queries.
        """
        if not user_input or not user_input.strip():
            return True

        stripped = user_input.strip()
        tokens = stripped.split()
        lowered = stripped.lower()

        # Single character or very short gibberish (less than 2 chars)
        if len(stripped) <= 2:
            return True

        # If single word is a known valid insurance term, it's complete
        if len(tokens) == 1 and lowered in cls.VALID_SINGLE_WORDS:
            logger.debug("Single word '%s' is a valid insurance term", stripped)
            return False

        # Common valid 2-word queries
        if len(tokens) == 2:
            if any(phrase in lowered for phrase in cls.VALID_TWO_WORD_PHRASES):
                logger.debug("Two-word query '%s' matches valid phrase pattern", stripped)
                return False

        # If query has 3+ words, it's probably complete enough
        if len(tokens) >= 3:
            return False

        # Check if it contains common insurance keywords even if short
        if cls._contains_insurance_keywords(lowered):
            logger.debug("Query '%s' contains insurance keywords", stripped)
            return False

        # At this point: 1-2 word query that doesn't match known patterns
        # Could be incomplete
        logger.debug("Query '%s' appears incomplete (short and no known patterns)", stripped)
        return True

    @staticmethod
    def _contains_insurance_keywords(text: str) -> bool:
        """Check if text contains common insurance-related keywords."""
        insurance_keywords = [
            'insurance', 'policy', 'cover', 'claim', 'premium',
            'quote', 'benefit', 'payout', 'deductible', 'plan'
        ]
        return any(keyword in text for keyword in insurance_keywords)

    @staticmethod
    def _query_matches_product(user_input: str, products_matched: Optional[list]) -> bool:
        """True if we have matched products and the user query is that product name.

        Examples:
        - 'serenicare' matches ['Serenicare']
        - 'Personal accident' matches ['Personal Accident Insurance']
        - 'motor' matches ['Motor Private Insurance']
        """
        if not user_input or not products_matched:
            return False
        q = user_input.strip().lower()
        if not q:
            return False
        for name in products_matched:
            if not name:
                continue
            n = (name or "").strip().lower()
            if q == n or q in n or n in q:
                logger.debug("Query '%s' matches product '%s'", user_input, name)
                return True
        logger.debug("Query '%s' does not match any of %s", user_input, products_matched)
        return False
