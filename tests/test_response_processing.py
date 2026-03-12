import pytest

from src.response_processor import ResponseProcessor


class DummyStateManager:
    def __init__(self):
        self.sessions = {}

    def get_session(self, session_id):
        return self.sessions.get(session_id)

    def update_session(self, session_id, updates):
        s = self.sessions.setdefault(session_id, {})
        s.update(updates)


@pytest.fixture
def state_manager():
    return DummyStateManager()


def test_incomplete_input_asks_clarifier(state_manager):
    rp = ResponseProcessor(state_manager=state_manager)
    session = {}
    out = rp.process_response(raw_response="", user_input="Hi", confidence=0.9, conversation_state=session, session_id="s1")
    assert out["follow_up"] is True
    assert "provide more details" in out["message"].lower()


def test_low_confidence_triggers_clarification(state_manager):
    rp = ResponseProcessor(state_manager=state_manager)
    session = {}
    out = rp.process_response(raw_response="I think...", user_input="Tell me about claims", confidence=0.1, conversation_state=session, session_id="s1")
    assert out["fallback"] is False
    assert out["follow_up"] is True
    assert "clarify" in out["message"].lower() or "more details" in out["message"].lower()
    assert out["metadata"]["reason"] == "low_confidence_clarification"


def test_followup_detected_from_model(state_manager):
    rp = ResponseProcessor(state_manager=state_manager)
    session = {}
    raw = "We can help with that. Do you want to know about benefits?"
    out = rp.process_response(raw_response=raw, user_input="benefits", confidence=0.9, conversation_state=session, session_id="s1")
    assert out["follow_up"] is True
    assert "do you want to know about benefits" in out["message"].lower()


def test_error_handler_on_exception(monkeypatch):
    # force an exception deep in the processor
    def bad_create(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr("src.followup_manager.FollowUpManager.create_clarifying_question", bad_create)
    rp = ResponseProcessor()
    session = {}
    out = rp.process_response(raw_response="ok", user_input="x", confidence=0.9, conversation_state=session)
    assert out["fallback"] is True
    assert "internal error" in out["message"].lower() or "error" in out["metadata"]["error"].lower()
