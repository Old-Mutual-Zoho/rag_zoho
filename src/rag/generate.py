import os
import logging
import asyncio
import random
from typing import Any, Dict, List, Tuple


# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Default text generation model for Google Gemini via google-genai.
# As of the current SDK, gemini-2.5-flash is a fast, general-purpose model.
MODEL_NAME = "gemini-2.5-flash"

SYSTEM_INSTRUCTION = """
You are MIA, the Senior Virtual Assistant for Old Mutual Uganda.
CRITICAL RULES:
1. **Only answer from the Retrieved Data**. Do not use external knowledge.
2. **SYNTHESIZE information** from the Retrieved Data - DO NOT copy text verbatim.
3. **NEVER repeat section headings** from sources (e.g., "What is X?", "Q:", "A:", "How I do apply?")
4. **Reformulate in your own words** - provide a natural conversational answer.
5. **Combine information** from multiple sources into a coherent response.
6. If the Retrieved Data is empty or not relevant, say you do not have enough information and ask if the user wants to talk to a human agent.

FORMAT:
- Use bullet points for lists of features/benefits
- Use **bold** for key terms and product names
- Keep responses under 12 lines when possible
- Write in paragraphs for explanations, bullets for lists

TONE: Professional, friendly, helpful, and conversational. Avoid robotic or scripted language.

EXAMPLE OF GOOD RESPONSE:
"Serenicare is Old Mutual's comprehensive health insurance plan that covers dental, optical, outpatient, and inpatient care across East Africa.
It includes coverage for chronic conditions like diabetes and HIV/AIDS, plus maternity benefits and emergency evacuation services within Uganda."

EXAMPLE OF BAD RESPONSE (never do this):
"What is Serenicare?
Serenicare provides benefits like...
Q: Who can get the cover?
A: This product offers..."
""".strip()


class MiaGenerator:
    def __init__(
        self,
        max_context_chars: int = 12000,
        min_score: float = 0.55,
        max_sources: int = 5,
        temperature: float = 0.2  # Lowered for financial accuracy
    ):
        from google import genai
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("CRITICAL: GEMINI_API_KEY is missing.")

        self.client = genai.Client(api_key=api_key)
        self.max_context_chars = max_context_chars
        self.min_score = min_score
        self.max_sources = max_sources
        self.temperature = temperature

    def _build_history_summary(self, conversation_history: List[Dict]) -> str:
        if not conversation_history:
            return ""

        last_user = ""
        last_assistant = ""
        for msg in reversed(conversation_history):
            role = msg.get("role")
            content = (msg.get("content") or "").strip()
            if role == "assistant" and not last_assistant and content:
                last_assistant = content
            if role == "user" and not last_user and content:
                last_user = content
            if last_user and last_assistant:
                break

        if not last_user and not last_assistant:
            return ""

        def _shorten(text: str, max_len: int = 240) -> str:
            cleaned = " ".join(text.split())
            if len(cleaned) <= max_len:
                return cleaned
            return cleaned[: max_len - 3].rstrip() + "..."

        parts = []
        if last_user:
            parts.append(f"User asked about: {_shorten(last_user)}")
        if last_assistant:
            parts.append(f"Assistant replied: {_shorten(last_assistant)}")
        return " | ".join(parts)

    def _build_context(self, hits: List[Dict[str, Any]]) -> Tuple[str, int, float]:
        if not hits:
            return "", 0, 0.0

        filtered_hits = [h for h in hits if h.get("score", 0) >= self.min_score]
        if not filtered_hits:
            # If filtering by score removes all hits, use all hits anyway (better than nothing)
            logger.warning(f"All hits below min_score {self.min_score}, using all {len(hits)} hits anyway")
            filtered_hits = hits

        filtered_hits.sort(key=lambda x: x.get("score", 0), reverse=True)
        avg_score = sum(h.get("score", 0) for h in filtered_hits) / len(filtered_hits)

        # Load chunk texts from file if payload doesn't contain text field
        chunk_texts = self._load_chunk_texts_if_needed(filtered_hits)

        context_parts = []
        current_length = 0
        sources_used = 0

        for idx, h in enumerate(filtered_hits[:self.max_sources], 1):
            p = h.get("payload") or h
            chunk_id = h.get("id") or p.get("id")

            # Try to get text from payload first, then from loaded chunks
            text = p.get("text", "").strip()
            if not text and chunk_id in chunk_texts:
                text = chunk_texts[chunk_id]

            if not text:
                logger.warning(f"No text found for chunk {chunk_id}, skipping")
                continue

            chunk = f"[Source {idx}] **{p.get('title', 'Unknown')}**: {text}\n"
            if current_length + len(chunk) > self.max_context_chars:
                break
            context_parts.append(chunk)
            current_length += len(chunk)
            sources_used += 1

        return "\n".join(context_parts), sources_used, avg_score

    def _load_chunk_texts_if_needed(self, hits: List[Dict[str, Any]]) -> Dict[str, str]:
        """Load chunk texts from website_chunks.jsonl when payload doesn't contain text."""
        import json
        from pathlib import Path

        # Check if any hit is missing text in payload
        needs_loading = False
        for h in hits:
            p = h.get("payload") or h
            if not p.get("text"):
                needs_loading = True
                break

        if not needs_loading:
            return {}

        # Collect IDs that need text
        needed_ids = set()
        for h in hits:
            p = h.get("payload") or h
            if not p.get("text"):
                chunk_id = h.get("id") or p.get("id")
                if chunk_id:
                    needed_ids.add(chunk_id)

        if not needed_ids:
            return {}

        # Load from chunks file
        chunks_path = Path(__file__).parent.parent.parent / "data" / "processed" / "website_chunks.jsonl"
        chunk_texts = {}

        if not chunks_path.exists():
            logger.warning(f"Chunks file not found: {chunks_path}")
            return {}

        try:
            with open(chunks_path, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                        chunk_id = chunk.get("id")
                        if chunk_id in needed_ids:
                            chunk_texts[chunk_id] = chunk.get("text", "")
                            if len(chunk_texts) >= len(needed_ids):
                                break  # Found all needed chunks
                    except json.JSONDecodeError:
                        continue

            logger.info(f"Loaded {len(chunk_texts)} chunk texts from file")
            return chunk_texts
        except Exception as e:
            logger.error(f"Error loading chunk texts: {e}")
            return {}

    async def generate(self, question: str, hits: List[Dict[str, Any]], conversation_history: List[Dict] = None) -> str:
        context, num_sources, _ = self._build_context(hits)

        context_note = (
            f"**Instructions:** Using the {num_sources} source(s) below, synthesize a natural conversational answer. "
            "Do NOT copy headings or Q&A format from sources - reformulate in your own words. "
            "Do not add facts not present in the sources."
            if num_sources > 0
            else "No relevant documents found. Say you don't have enough information and ask if the user wants to talk to a human agent."
        )

        # Keep history compact and avoid duplicating the same context as both
        # free-form summary and transcript.
        history_text = ""
        if conversation_history:
            history_lines = []
            for msg in conversation_history[-6:]:
                role = msg.get("role", "user")
                content = " ".join((msg.get("content") or "").split())
                if not content:
                    continue
                if len(content) > 280:
                    content = content[:277].rstrip() + "..."
                if role == "user":
                    history_lines.append(f"User: {content}")
                elif role == "assistant":
                    history_lines.append(f"Assistant: {content}")

            if history_lines:
                history_text = "\n\n**Recent Conversation:**\n" + "\n".join(history_lines)
            else:
                summary = self._build_history_summary(conversation_history)
                if summary:
                    history_text = f"\n\n**Conversation Summary:** {summary}"

        full_prompt = (
            f"{context_note}{history_text}\n\n"
            f"**User Question:** {question}\n\n"
            f"**Retrieved Data:**\n{context or 'None'}"
        )

        logger.info(f"Generating response for question: {question[:100]}... with {num_sources} sources")

        def _sync_generate(prompt: str, max_output_tokens: int = 800):
            from google.genai import types
            response = self.client.models.generate_content(
                model=MODEL_NAME,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION,
                    temperature=self.temperature,
                    max_output_tokens=max_output_tokens,
                ),
            )
            return response

        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                response = await asyncio.to_thread(_sync_generate, full_prompt, 800)
                text = (getattr(response, "text", "") or "").strip()
                if not text:
                    logger.warning("GenAI returned empty text response.")
                    return "I'm having trouble retrieving those details right now. Please try again in a moment."

                # Some provider-side stops can return partial text. If we detect an
                # abrupt ending, ask for a short continuation and stitch it in.
                if self._looks_truncated(text):
                    try:
                        continuation_prompt = (
                            "Continue the answer from where it stopped. "
                            "Do not repeat the text already provided. "
                            "Finish the incomplete thought in 1-3 short sentences.\n\n"
                            f"Current partial answer:\n{text}"
                        )
                        continuation = await asyncio.to_thread(_sync_generate, continuation_prompt, 240)
                        continuation_text = (getattr(continuation, "text", "") or "").strip()
                        if continuation_text:
                            text = self._merge_continuation(text, continuation_text)
                    except Exception as continuation_error:
                        logger.warning("Continuation attempt failed: %s", continuation_error)

                logger.info("Successfully generated response from Gemini API")
                return text
            except Exception as e:
                if attempt >= max_attempts:
                    logger.error(f"GenAI error when generating response: {type(e).__name__}: {e}", exc_info=True)
                    break
                backoff = (2 ** (attempt - 1)) + random.uniform(0, 0.5)
                logger.warning(
                    "GenAI request failed on attempt %s/%s (%s). Retrying in %.2fs...",
                    attempt,
                    max_attempts,
                    type(e).__name__,
                    backoff,
                )
                await asyncio.sleep(backoff)

        return "I'm having trouble retrieving those details right now. Please try again in a moment."

    @staticmethod
    def _looks_truncated(text: str) -> bool:
        s = (text or "").strip()
        if not s:
            return True

        # Very short replies can naturally end without punctuation.
        if len(s) < 80:
            return False

        if s.endswith((".", "!", "?", '"', "'", "*", ")", "]")):
            return False

        last_word = s.split()[-1].strip(".,!?;:'\")]").lower()
        dangling_words = {
            "a", "an", "and", "as", "at", "because", "but", "for",
            "from", "in", "into", "of", "on", "or", "that", "the",
            "to", "with", "which",
        }
        if last_word in dangling_words:
            return True

        # Unbalanced markdown bold is a strong signal of a cut-off answer.
        if s.count("**") % 2 == 1:
            return True

        return True

    @staticmethod
    def _merge_continuation(base_text: str, continuation_text: str) -> str:
        base = (base_text or "").rstrip()
        cont = (continuation_text or "").strip()
        if not cont:
            return base

        if cont.lower() in base.lower():
            return base

        # Remove simple overlap when continuation starts by repeating the tail.
        max_overlap = min(80, len(base), len(cont))
        overlap = 0
        for size in range(max_overlap, 11, -1):
            if base[-size:].lower() == cont[:size].lower():
                overlap = size
                break

        if overlap > 0:
            cont = cont[overlap:].lstrip()

        if not cont:
            return base

        separator = " " if base and base[-1].isalnum() and cont[0].isalnum() else ""
        return f"{base}{separator}{cont}"


def generate_with_gemini(
    *,
    question: str,
    hits: List[Dict[str, Any]],
    model: str | None = None,
    api_key_env: str = "GEMINI_API_KEY",
) -> str:
    """Sync helper used by scripts/run_rag.py."""
    import asyncio

    # Allow alternate env var names while keeping GEMINI_API_KEY as the canonical key.
    if api_key_env and api_key_env != "GEMINI_API_KEY" and not os.environ.get("GEMINI_API_KEY"):
        alt_value = os.environ.get(api_key_env)
        if alt_value:
            os.environ["GEMINI_API_KEY"] = alt_value

    global MODEL_NAME
    previous_model = MODEL_NAME
    if model:
        MODEL_NAME = model

    try:
        generator = MiaGenerator()
        return asyncio.run(generator.generate(question, hits, conversation_history=None))
    finally:
        MODEL_NAME = previous_model
