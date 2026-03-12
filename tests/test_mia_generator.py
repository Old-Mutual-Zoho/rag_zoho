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
