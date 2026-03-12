import pytest

from src.rag.generate import MiaGenerator


class _FakeResponse:
    def __init__(self, text: str):
        self.text = text


class _FlakyModels:
    def __init__(self):
        self.calls = 0

    def generate_content(self, model, contents, config):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary upstream failure")
        return _FakeResponse("Generated summary answer")


class _FakeClient:
    def __init__(self, models):
        self.models = models


@pytest.mark.asyncio
async def test_mia_generator_retries_and_returns_text(monkeypatch):
    models = _FlakyModels()

    gen = MiaGenerator.__new__(MiaGenerator)
    gen.client = _FakeClient(models)
    gen.temperature = 0.2
    gen.max_context_chars = 12000
    gen.min_score = 0.55
    gen.max_sources = 5

    monkeypatch.setattr(gen, "_build_context", lambda hits: ("ctx", 1, 0.9))
    monkeypatch.setattr(gen, "_build_history_summary", lambda history: "")

    async def _no_sleep(_):
        return None

    monkeypatch.setattr("src.rag.generate.asyncio.sleep", _no_sleep)
    monkeypatch.setattr("src.rag.generate.random.uniform", lambda _a, _b: 0.0)

    out = await gen.generate("what is travel insurance", hits=[{"id": "1"}], conversation_history=[])

    assert out == "Generated summary answer"
    assert models.calls == 2


@pytest.mark.asyncio
async def test_mia_generator_empty_text_returns_user_safe_fallback(monkeypatch):
    class _EmptyModels:
        def generate_content(self, model, contents, config):
            return _FakeResponse("")

    gen = MiaGenerator.__new__(MiaGenerator)
    gen.client = _FakeClient(_EmptyModels())
    gen.temperature = 0.2
    gen.max_context_chars = 12000
    gen.min_score = 0.55
    gen.max_sources = 5

    monkeypatch.setattr(gen, "_build_context", lambda hits: ("ctx", 1, 0.9))
    monkeypatch.setattr(gen, "_build_history_summary", lambda history: "")

    out = await gen.generate("question", hits=[{"id": "1"}], conversation_history=[])

    assert "trouble retrieving" in out.lower()


@pytest.mark.asyncio
async def test_mia_generator_recovers_truncated_output_with_continuation(monkeypatch):
    class _FakeCandidate:
        """Simulates a Gemini candidate whose finish_reason == MAX_TOKENS (value 2)."""
        finish_reason = 2

    class _FakeResponseMaxTokens:
        """First call: incomplete text, finish_reason = MAX_TOKENS."""
        def __init__(self, text: str, max_tokens: bool = False):
            self.text = text
            self.candidates = [_FakeCandidate()] if max_tokens else []

    class _ContinuationModels:
        def __init__(self):
            self.calls = 0

        def generate_content(self, model, contents, config):
            self.calls += 1
            if self.calls == 1:
                return _FakeResponseMaxTokens(
                    "Old Mutual offers insurance, investment, asset management, and banking services across East Africa. As a",
                    max_tokens=True,
                )
            return _FakeResponseMaxTokens(
                " result, customers can access a broad range of financial solutions under one group.",
            )

    models = _ContinuationModels()
    gen = MiaGenerator.__new__(MiaGenerator)
    gen.client = _FakeClient(models)
    gen.temperature = 0.2
    gen.max_context_chars = 12000
    gen.min_score = 0.55
    gen.max_sources = 5

    monkeypatch.setattr(gen, "_build_context", lambda hits: ("ctx", 1, 0.9))
    monkeypatch.setattr(gen, "_build_history_summary", lambda history: "")

    out = await gen.generate("what does old mutual offer", hits=[{"id": "1"}], conversation_history=[])

    assert "as a result" in out.lower()
    assert models.calls == 2


def test_merge_continuation_removes_overlap():
    base = "Old Mutual offers insurance and investment services"
    cont = "investment services across East Africa."
    merged = MiaGenerator._merge_continuation(base, cont)
    assert merged == "Old Mutual offers insurance and investment services across East Africa."


def test_looks_truncated_returns_false_for_complete_sentence():
    assert MiaGenerator._looks_truncated("Old Mutual is a leading insurer in Uganda.") is False


def test_looks_truncated_returns_true_for_dangling_word():
    assert MiaGenerator._looks_truncated(
        "Old Mutual offers a comprehensive range of financial services across East Africa, including insurance and"
    ) is True


@pytest.mark.asyncio
async def test_mia_generator_complete_answer_does_not_trigger_continuation(monkeypatch):
    """A STOP-finish answer (no MAX_TOKENS) must be returned as-is with only 1 API call."""
    class _SingleModels:
        def __init__(self):
            self.calls = 0

        def generate_content(self, model, contents, config):
            self.calls += 1
            return _FakeResponse("Old Mutual is a leading financial services provider in East Africa.")

    models = _SingleModels()
    gen = MiaGenerator.__new__(MiaGenerator)
    gen.client = _FakeClient(models)
    gen.temperature = 0.2
    gen.max_context_chars = 12000
    gen.min_score = 0.55
    gen.max_sources = 5

    monkeypatch.setattr(gen, "_build_context", lambda hits: ("ctx", 1, 0.9))
    monkeypatch.setattr(gen, "_build_history_summary", lambda history: "")

    out = await gen.generate("who is old mutual", hits=[{"id": "1"}], conversation_history=[])

    assert "old mutual" in out.lower()
    assert models.calls == 1, "Should not make a second call for a complete answer"
