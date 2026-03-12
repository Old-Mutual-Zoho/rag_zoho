import pytest

from src.chatbot.flows.router import ChatRouter
from src.chatbot.modes.conversational import ConversationalMode
from src.chatbot.state_manager import StateManager
from src.database.postgres import PostgresDB
from src.database.redis import RedisCache


class DummyRAG:
    def __init__(self):
        self.retrieve_calls = []

    async def retrieve(self, query: str, filters=None, top_k=None):
        self.retrieve_calls.append({"query": query, "filters": filters, "top_k": top_k})
        return [{"payload": {"text": "stub"}}]

    async def generate(self, query: str, context_docs, conversation_history):
        return {"answer": f"ANSWER: {query}", "confidence": 0.5, "sources": []}


class DummyGuided:
    async def process(self, *args, **kwargs):
        raise AssertionError("guided.process should not be called")

    async def start_flow(self, *args, **kwargs):
        raise AssertionError("guided.start_flow should not be called for learn intent")


class DummyGuidedReturnsForm:
    async def process(self, *args, **kwargs):
        raise AssertionError("guided.process should not be called in this test")

    async def start_flow(self, flow_name: str, session_id: str, user_id: str, initial_data=None):
        assert flow_name == "journey"
        assert (initial_data or {}).get("product_flow") == "travel_insurance"
        return {
            "mode": "guided",
            "flow": flow_name,
            "step": 0,
            "response": {"type": "form", "message": "FORM"},
            "data": None,
        }


class DummyGuidedTrackStarts:
    def __init__(self):
        self.calls = []

    async def process(self, *args, **kwargs):
        raise AssertionError("guided.process should not be called in this test")

    async def start_flow(self, flow_name: str, session_id: str, user_id: str, initial_data=None):
        self.calls.append(
            {
                "flow_name": flow_name,
                "session_id": session_id,
                "user_id": user_id,
                "initial_data": initial_data,
            }
        )
        return {
            "mode": "guided",
            "flow": flow_name,
            "step": 0,
            "response": {"type": "form", "message": "FORM"},
            "data": None,
        }


class DummyMatcher:
    def match_products(self, query: str, top_k: int = 3):
        # Return a travel insurance-like product match with URL
        return [
            (
                1.0,
                0,
                {
                    "product_id": "website:product:travel/travel-insurance",
                    "name": "Travel Insurance",
                    "category_name": "Travel",
                    "sub_category_name": "Travel",
                    "url": "https://www.oldmutual.co.ug/",
                },
            )
        ][:top_k]


class FollowUpMatcher:
    def match_products(self, query: str, top_k: int = 3):
        if "travel insurance" not in (query or "").lower():
            return []
        return DummyMatcher().match_products(query, top_k=top_k)


class NoMatchMatcher:
    def match_products(self, query: str, top_k: int = 3):
        return []


class DigitalFlowFallbackMatcher:
    def __init__(self):
        self.product_index = {
            "website:product:other/general/motor-insurance": {
                "product_id": "website:product:other/general/motor-insurance",
                "doc_id": "website:product:other/general/motor-insurance",
                "name": "Motor Insurance",
                "slug": "motor-insurance",
                "product_key": "other/general/motor-insurance",
            },
            "website:product:business/general/motor-commercial": {
                "product_id": "website:product:business/general/motor-commercial",
                "doc_id": "website:product:business/general/motor-commercial",
                "name": "Motor Commercial",
                "slug": "motor-commercial",
                "product_key": "business/general/motor-commercial",
            },
        }

    def match_products(self, query: str, top_k: int = 3):
        # Simulate lexical matching miss for short queries like "Car Insurance".
        return []


@pytest.mark.asyncio
async def test_tell_me_about_travel_insurance_stays_conversational_and_suggests_sections():
    db = PostgresDB()
    redis = RedisCache()
    sm = StateManager(redis, db)

    # Create a session to hold context
    user = db.get_or_create_user(phone_number="256700000000")
    session_id = sm.create_session(str(user.id))

    conv = ConversationalMode(DummyRAG(), DummyMatcher(), sm)
    router = ChatRouter(conv, DummyGuided(), sm, DummyMatcher())

    out = await router.route("hi, tell me about travel insurance", session_id, str(user.id))

    assert out["mode"] == "conversational"
    assert "should i share the benefits" in out["response"].lower()
    assert out.get("suggested_action") is None


@pytest.mark.asyncio
async def test_product_guide_button_action_returns_section_answer():
    db = PostgresDB()
    redis = RedisCache()
    sm = StateManager(redis, db)

    user = db.get_or_create_user(phone_number="256700000000")
    session_id = sm.create_session(str(user.id))

    conv = ConversationalMode(DummyRAG(), DummyMatcher(), sm)

    # Prime context by asking about travel insurance
    await conv.process("tell me about travel insurance", session_id, str(user.id))

    out = await conv.process("yes", session_id, str(user.id))

    assert out["mode"] == "conversational"
    assert out["response"].startswith("ANSWER:")
    assert "benefits" in out["response"].lower()


@pytest.mark.asyncio
async def test_product_guide_yes_chain_offers_next_section_and_handles_second_yes():
    db = PostgresDB()
    redis = RedisCache()
    sm = StateManager(redis, db)

    user = db.get_or_create_user(phone_number="256700000000")
    session_id = sm.create_session(str(user.id))

    conv = ConversationalMode(DummyRAG(), DummyMatcher(), sm)

    # Start with product explanation (sets pending offer to benefits)
    first = await conv.process("tell me about travel insurance", session_id, str(user.id))
    assert "share the benefits" in first["response"].lower()

    # Yes -> benefits (should now offer eligibility)
    benefits = await conv.process("yes", session_id, str(user.id))
    assert "benefits" in benefits["response"].lower()
    assert "share the eligibility" in benefits["response"].lower()

    # Yes again -> eligibility
    eligibility = await conv.process("yes", session_id, str(user.id))
    assert "eligibility" in eligibility["response"].lower()


@pytest.mark.asyncio
async def test_get_quotation_button_starts_guided_and_returns_form_immediately():
    db = PostgresDB()
    redis = RedisCache()
    sm = StateManager(redis, db)

    user = db.get_or_create_user(phone_number="256700000000")
    session_id = sm.create_session(str(user.id))

    conv = ConversationalMode(DummyRAG(), DummyMatcher(), sm)
    router = ChatRouter(conv, DummyGuidedReturnsForm(), sm, DummyMatcher())

    # Prime topic
    await router.route("tell me about travel insurance", session_id, str(user.id))

    out = await router.route("", session_id, str(user.id), form_data={"action": "get_quotation"})

    assert out["mode"] == "guided"
    assert out["response"]["type"] == "form"


@pytest.mark.asyncio
async def test_quote_message_prompts_confirmation_before_starting_guided_flow():
    db = PostgresDB()
    redis = RedisCache()
    sm = StateManager(redis, db)

    user = db.get_or_create_user(phone_number="256700000001")
    session_id = sm.create_session(str(user.id))

    conv = ConversationalMode(DummyRAG(), DummyMatcher(), sm)
    guided = DummyGuidedTrackStarts()
    router = ChatRouter(conv, guided, sm, DummyMatcher())

    out = await router.route("I want a travel insurance quote", session_id, str(user.id))

    assert out["mode"] == "conversational"
    assert "i can guide you through" in out["response"].lower()
    assert "would you like me to proceed" in out["response"].lower()
    assert out["suggested_action"]["type"] == "switch_to_guided"
    assert out["suggested_action"]["buttons"][0]["action"] == "confirm_guided_switch"
    assert out["suggested_action"]["buttons"][1]["action"] == "cancel_guided_switch"
    assert guided.calls == []


@pytest.mark.asyncio
async def test_quote_confirmation_yes_starts_guided_journey_with_detected_product_flow():
    db = PostgresDB()
    redis = RedisCache()
    sm = StateManager(redis, db)

    user = db.get_or_create_user(phone_number="256700000002")
    session_id = sm.create_session(str(user.id))

    conv = ConversationalMode(DummyRAG(), DummyMatcher(), sm)
    guided = DummyGuidedTrackStarts()
    router = ChatRouter(conv, guided, sm, DummyMatcher())

    await router.route("I want a travel insurance quote", session_id, str(user.id))
    out = await router.route("yes", session_id, str(user.id))

    assert out["mode"] == "guided"
    assert out["flow"] == "journey"
    assert len(guided.calls) == 1
    assert guided.calls[0]["initial_data"] == {"product_flow": "travel_insurance"}


@pytest.mark.asyncio
async def test_quote_confirmation_no_keeps_chat_mode_and_does_not_start_guided():
    db = PostgresDB()
    redis = RedisCache()
    sm = StateManager(redis, db)

    user = db.get_or_create_user(phone_number="256700000003")
    session_id = sm.create_session(str(user.id))

    conv = ConversationalMode(DummyRAG(), DummyMatcher(), sm)
    guided = DummyGuidedTrackStarts()
    router = ChatRouter(conv, guided, sm, DummyMatcher())

    await router.route("I want a travel insurance quote", session_id, str(user.id))
    out = await router.route("not now", session_id, str(user.id))

    assert out["mode"] == "conversational"
    assert "stay in chat" in out["response"].lower()
    assert guided.calls == []


@pytest.mark.asyncio
async def test_pricing_question_stays_conversational_without_guided_prompt():
    db = PostgresDB()
    redis = RedisCache()
    sm = StateManager(redis, db)

    user = db.get_or_create_user(phone_number="256700000004")
    session_id = sm.create_session(str(user.id))

    conv = ConversationalMode(DummyRAG(), DummyMatcher(), sm)
    router = ChatRouter(conv, DummyGuided(), sm, DummyMatcher())

    out = await router.route("How much is travel insurance?", session_id, str(user.id))

    assert out["mode"] == "conversational"
    assert out.get("suggested_action") is None


@pytest.mark.asyncio
async def test_followup_reuses_session_product_topic_for_ambiguous_question():
    db = PostgresDB()
    redis = RedisCache()
    sm = StateManager(redis, db)

    user = db.get_or_create_user(phone_number="256700000005")
    session_id = sm.create_session(str(user.id))

    rag = DummyRAG()
    conv = ConversationalMode(rag, FollowUpMatcher(), sm)

    await conv.process("tell me about travel insurance", session_id, str(user.id))
    await conv.process("is it expensive?", session_id, str(user.id))

    second_call = rag.retrieve_calls[-1]
    assert second_call["filters"] == {"products": ["website:product:travel/travel-insurance"]}
    assert "travel insurance" in second_call["query"].lower()


@pytest.mark.asyncio
async def test_followup_uses_previous_user_turn_when_no_product_match():
    db = PostgresDB()
    redis = RedisCache()
    sm = StateManager(redis, db)

    user = db.get_or_create_user(phone_number="256700000006")
    session_id = sm.create_session(str(user.id))
    conv = ConversationalMode(DummyRAG(), NoMatchMatcher(), sm)

    session = sm.get_session(session_id)
    conversation_id = session["conversation_id"]
    db.add_message(conversation_id=conversation_id, role="user", content="Tell me about travel insurance")
    db.add_message(conversation_id=conversation_id, role="assistant", content="Travel insurance protects trips.")

    out = await conv.process("what about waiting period?", session_id, str(user.id))

    assert out["mode"] == "conversational"
    q = conv.rag.retrieve_calls[-1]["query"].lower()
    assert "context from previous question" in q
    assert "tell me about travel insurance" in q
    assert "follow-up question" in q


@pytest.mark.asyncio
async def test_car_insurance_uses_digital_flow_fallback_filter_when_matcher_misses():
    db = PostgresDB()
    redis = RedisCache()
    sm = StateManager(redis, db)

    user = db.get_or_create_user(phone_number="256700000007")
    session_id = sm.create_session(str(user.id))
    rag = DummyRAG()
    conv = ConversationalMode(rag, DigitalFlowFallbackMatcher(), sm)

    out = await conv.process("Car Insurance", session_id, str(user.id))

    assert out["mode"] == "conversational"
    call = rag.retrieve_calls[-1]
    assert call["filters"] == {"products": ["website:product:other/general/motor-insurance"]}
