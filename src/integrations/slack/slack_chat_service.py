import json
import os

try:
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
except ModuleNotFoundError:  # pragma: no cover - handled in __init__
    WebClient = None  # type: ignore[assignment]
    SlackApiError = Exception  # type: ignore[assignment]


class SlackChatService:
    def __init__(self, token: str, channel: str, client: WebClient = None):
        if client is None and WebClient is None:
            raise ImportError("slack_sdk is required to use SlackChatService without a custom client")
        self.client = client or WebClient(token=token)  # type: ignore[misc]
        self.channel = channel
        self._thread_cache = {}
        self._agent_map = self._load_agent_map()

    @staticmethod
    def _load_agent_map() -> dict:
        raw = os.getenv("SLACK_AGENT_MAP", "").strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return {str(k): str(v) for k, v in parsed.items()}
        except Exception:
            pass
        return {}

    def _chat_tag(self, chat_id: str) -> str:
        return f"[chat_id:{chat_id}]"

    def _message_prefix(self, sender: str, chat_id: str) -> str:
        return f"[{sender}]{self._chat_tag(chat_id)}"

    def _find_thread_ts(self, chat_id: str):
        cached = self._thread_cache.get(chat_id)
        if cached:
            return cached
        history = self.client.conversations_history(channel=self.channel, limit=200)
        tag = self._chat_tag(chat_id)
        for msg in history.data.get("messages", []):
            text = msg.get("text") or ""
            # Root message has ts and no parent thread_ts.
            if tag in text and not msg.get("thread_ts"):
                ts = msg.get("ts")
                if ts:
                    self._thread_cache[chat_id] = ts
                    return ts
        return None

    def _ensure_thread(self, chat_id: str) -> str:
        existing = self._find_thread_ts(chat_id)
        if existing:
            return existing
        root = self.client.chat_postMessage(
            channel=self.channel,
            text=f"[system]{self._chat_tag(chat_id)} Session opened",
        )
        ts = root.data.get("ts")
        if not ts:
            raise Exception("Slack API error: missing_thread_ts")
        self._thread_cache[chat_id] = ts
        return ts

    def thread_exists(self, chat_id: str) -> bool:
        return bool(self._find_thread_ts(chat_id))

    def send_history_message(self, chat_id: str, message: str):
        """
        Send a context/history note to the Slack thread.
        These entries are for agent context and are filtered out from client polling.
        """
        return self.send_message(chat_id=chat_id, message=message, sender="history")

    def send_message(self, chat_id: str, message: str, sender: str = "agent", agent_id: str = None):
        try:
            thread_ts = self._ensure_thread(chat_id)
            text = f"{self._message_prefix(sender, chat_id)} {message}"
            if sender == "agent" and agent_id:
                text = f"[agent_id:{agent_id}] {text}"
            response = self.client.chat_postMessage(
                channel=self.channel,
                thread_ts=thread_ts,
                text=text,
            )
            data = response.data
            data["thread_ts"] = thread_ts
            data["chat_id"] = chat_id
            if agent_id:
                data["agent_id"] = agent_id
            return data
        except SlackApiError as e:
            raise Exception(f"Slack API error: {self._extract_slack_error(e)}")

    def receive_messages(self, chat_id: str):
        try:
            thread_ts = self._find_thread_ts(chat_id)
            if not thread_ts:
                return []
            response = self.client.conversations_replies(channel=self.channel, ts=thread_ts, limit=200)
            out = []
            for msg in response.data.get("messages", []):
                if msg.get("ts") == thread_ts:
                    continue
                text = msg.get("text") or ""
                sender = self._extract_sender(text, msg.get("user"), msg.get("bot_id"))
                if sender in {"history", "system"}:
                    continue
                agent_id = self._extract_agent_id(text, msg.get("user"))
                clean_text = self._clean_message(text, chat_id)
                out.append(
                    {
                        "chat_id": chat_id,
                        "thread_ts": thread_ts,
                        "ts": msg.get("ts"),
                        "text": clean_text,
                        "message": clean_text,
                        "raw_text": text,
                        "sender": sender,
                        "agent_id": agent_id,
                        "user": msg.get("user"),
                        "bot_id": msg.get("bot_id"),
                    }
                )
            return out
        except SlackApiError as e:
            raise Exception(f"Slack API error: {self._extract_slack_error(e)}")

    @staticmethod
    def _extract_sender(text: str, user: str = None, bot_id: str = None) -> str:
        working = text or ""
        if working.startswith("[agent_id:") and "]" in working:
            working = working[working.find("]") + 1 :].lstrip()
        if working.startswith("[") and "]" in working:
            return working[1 : working.find("]")]
        # Human-typed thread replies in Slack usually have `user` and no `bot_id`.
        if user and not bot_id:
            return "agent"
        return "unknown"

    def resolve_agent_id(self, slack_user_id: str) -> str:
        if not slack_user_id:
            return ""
        return self._agent_map.get(str(slack_user_id), "")

    def _extract_agent_id(self, text: str, user: str = None) -> str:
        marker = "[agent_id:"
        if marker in text and "]" in text[text.find(marker) :]:
            start = text.find(marker) + len(marker)
            end = text.find("]", start)
            if end > start:
                return text[start:end].strip()
        return self.resolve_agent_id(user or "")

    def _clean_message(self, text: str, chat_id: str) -> str:
        cleaned = text or ""
        if cleaned.startswith("[agent_id:") and "]" in cleaned:
            cleaned = cleaned[cleaned.find("]") + 1 :].lstrip()
        if cleaned.startswith("[") and "]" in cleaned:
            cleaned = cleaned[cleaned.find("]") + 1 :].lstrip()
        tag = self._chat_tag(chat_id)
        if cleaned.startswith(tag):
            cleaned = cleaned[len(tag) :].lstrip()
        return cleaned

    @staticmethod
    def _extract_slack_error(exc: Exception) -> str:
        response = getattr(exc, "response", None)
        if isinstance(response, dict):
            return str(response.get("error", "unknown_error"))
        try:
            return str(response["error"])  # type: ignore[index]
        except Exception:
            return str(exc)
