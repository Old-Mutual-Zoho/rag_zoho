"""
Lightweight in-memory PostgresDB replacement for local development.

This provides a minimal subset of the interface expected by the API and
chatbot flows so the system can run without a real database. It is NOT
intended for production use.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
import uuid


@dataclass
class User:
    id: str
    phone_number: str
    kyc_completed: bool = False


@dataclass
class Conversation:
    id: str
    user_id: str
    mode: str
    created_at: datetime = field(default_factory=datetime.utcnow)
    ended_at: Optional[datetime] = None


@dataclass
class Message:
    id: str
    conversation_id: str
    role: str
    content: str
    metadata: Dict[str, Any]
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class RAGMetric:
    id: str
    metric_type: str
    value: float
    conversation_id: Optional[str]
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Quote:
    id: str
    user_id: str
    product_id: str
    product_name: str
    premium_amount: float
    sum_assured: Optional[float]
    underwriting_data: Dict[str, Any]
    pricing_breakdown: Optional[Dict[str, Any]] = None
    status: str = "pending"
    generated_at: datetime = field(default_factory=datetime.utcnow)
    valid_until: datetime = field(default_factory=lambda: datetime.utcnow() + timedelta(days=30))


@dataclass
class PersonalAccidentApplication:
    id: str
    user_id: str
    status: str = "in_progress"
    personal_details: Dict[str, Any] = field(default_factory=dict)
    next_of_kin: Dict[str, Any] = field(default_factory=dict)
    previous_pa_policy: Dict[str, Any] = field(default_factory=dict)
    physical_disability: Dict[str, Any] = field(default_factory=dict)
    risky_activities: Dict[str, Any] = field(default_factory=dict)
    coverage_plan: Dict[str, Any] = field(default_factory=dict)
    national_id_upload: Dict[str, Any] = field(default_factory=dict)
    quote_id: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class TravelInsuranceApplication:
    id: str
    user_id: str
    status: str = "in_progress"
    selected_product: Dict[str, Any] = field(default_factory=dict)
    about_you: Dict[str, Any] = field(default_factory=dict)
    travel_party_and_trip: Dict[str, Any] = field(default_factory=dict)
    data_consent: Dict[str, Any] = field(default_factory=dict)
    travellers: List[Dict[str, Any]] = field(default_factory=list)
    emergency_contact: Dict[str, Any] = field(default_factory=dict)
    bank_details: Dict[str, Any] = field(default_factory=dict)
    passport_upload: Dict[str, Any] = field(default_factory=dict)
    quote_id: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class SerenicareApplication:
    id: str
    user_id: str
    status: str = "in_progress"
    cover_personalization: Dict[str, Any] = field(default_factory=dict)
    optional_benefits: List[str] = field(default_factory=list)
    medical_conditions: Dict[str, Any] = field(default_factory=dict)
    plan_option: Dict[str, Any] = field(default_factory=dict)
    about_you: Dict[str, Any] = field(default_factory=dict)
    quote_id: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class EscalationSession:
    id: str
    session_id: str
    conversation_id: Optional[str] = None
    user_id: Optional[str] = None
    escalated: bool = False
    agent_id: Optional[str] = None
    escalation_reason: Optional[str] = None
    escalation_metadata: Dict[str, Any] = field(default_factory=dict)
    escalated_at: Optional[datetime] = None
    agent_joined_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class PaymentTransaction:
    reference: str
    provider: str
    provider_reference: str
    phone_number: str
    amount: float
    currency: str
    status: str = "PENDING"
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class PaymentAuditEvent:
    id: str
    payment_reference: str
    event_type: str
    status_from: Optional[str] = None
    status_to: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)


class PostgresDB:
    """
    In-memory stand‑in for a Postgres-backed data access layer.

    Methods are intentionally simple and only support what the current
    API and chatbot flows require.
    """

    def __init__(self) -> None:
        self._users: Dict[str, User] = {}
        self._users_by_phone: Dict[str, str] = {}
        self._conversations: Dict[str, Conversation] = {}
        self._messages: List[Message] = []
        self._quotes: Dict[str, Quote] = {}
        # Personal Accident applications
        self._pa_applications: Dict[str, PersonalAccidentApplication] = {}
        # Travel Insurance applications
        self._travel_applications: Dict[str, TravelInsuranceApplication] = {}
        # Serenicare applications
        self._serenicare_applications: Dict[str, SerenicareApplication] = {}
        # Escalation state by session_id
        self._escalation_sessions: Dict[str, EscalationSession] = {}
        # Payment persistence
        self._payment_transactions: Dict[str, PaymentTransaction] = {}
        self._payment_audit_events: List[PaymentAuditEvent] = []
        # RAG metrics
        self._rag_metrics: List[RAGMetric] = []

    # ------------------------------------------------------------------ #
    # Schema / lifecycle
    # ------------------------------------------------------------------ #
    def create_tables(self) -> None:
        """
        No-op for the in-memory implementation. Kept for compatibility
        with the startup hook in `src/api/main.py`.
        """
        return None

    # ------------------------------------------------------------------ #
    # Users
    # ------------------------------------------------------------------ #
    def get_or_create_user(self, phone_number: str) -> User:
        if phone_number in self._users_by_phone:
            return self._users[self._users_by_phone[phone_number]]

        user_id = str(uuid.uuid4())
        user = User(id=user_id, phone_number=phone_number, kyc_completed=False)
        self._users[user_id] = user
        self._users_by_phone[phone_number] = user_id
        return user

    def get_user_by_phone(self, phone_number: str) -> Optional[User]:
        user_id = self._users_by_phone.get(phone_number)
        if not user_id:
            return None
        return self._users.get(user_id)

    def get_user_by_id(self, user_id: str) -> Optional[User]:
        return self._users.get(user_id)

    # ------------------------------------------------------------------ #
    # Conversations & messages
    # ------------------------------------------------------------------ #
    def create_conversation(self, user_id: str, mode: str) -> Conversation:
        conv_id = str(uuid.uuid4())
        conv = Conversation(id=conv_id, user_id=user_id, mode=mode)
        self._conversations[conv_id] = conv
        return conv

    def add_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Message:
        msg = Message(
            id=str(uuid.uuid4()),
            conversation_id=conversation_id,
            role=role,
            content=content,
            metadata=metadata or {},
        )
        self._messages.append(msg)
        return msg

    def get_conversation_history(
        self,
        conversation_id: str,
        limit: int = 50,
    ) -> List[Message]:
        msgs = [m for m in self._messages if m.conversation_id == conversation_id]
        # Return newest first, API reverses again where needed
        msgs.sort(key=lambda m: m.timestamp, reverse=True)
        return msgs[:limit]

    # ------------------------------------------------------------------ #
    # RAG metrics
    # ------------------------------------------------------------------ #
    def add_rag_metric(
        self,
        *,
        metric_type: str,
        value: float,
        conversation_id: Optional[str] = None,
        created_at: Optional[datetime] = None,
    ) -> RAGMetric:
        metric = RAGMetric(
            id=str(uuid.uuid4()),
            metric_type=metric_type,
            value=float(value),
            conversation_id=conversation_id,
            created_at=created_at or datetime.utcnow(),
        )
        self._rag_metrics.append(metric)
        return metric

    def add_rag_metrics(self, metrics: List[Dict[str, Any]]) -> List[RAGMetric]:
        created: List[RAGMetric] = []
        for metric_data in metrics:
            metric = self.add_rag_metric(
                metric_type=metric_data["metric_type"],
                value=metric_data["value"],
                conversation_id=metric_data.get("conversation_id"),
                created_at=metric_data.get("created_at"),
            )
            created.append(metric)
        return created

    # ------------------------------------------------------------------ #
    # Quotes
    # ------------------------------------------------------------------ #
    def create_quote(
        self,
        *,
        user_id: str,
        product_id: str,
        premium_amount: Any,
        sum_assured: Any = None,
        underwriting_data: Optional[Dict[str, Any]] = None,
        pricing_breakdown: Optional[Dict[str, Any]] = None,
        product_name: Optional[str] = None,
        status: str = "pending",
    ) -> Quote:
        quote_id = str(uuid.uuid4())
        quote = Quote(
            id=quote_id,
            user_id=user_id,
            product_id=product_id,
            product_name=product_name or product_id,
            premium_amount=float(premium_amount or 0.0),
            sum_assured=float(sum_assured) if sum_assured is not None else None,
            underwriting_data=underwriting_data or {},
            pricing_breakdown=pricing_breakdown,
            status=status,
        )
        self._quotes[quote_id] = quote
        return quote

    def get_quote(self, quote_id: str) -> Optional[Quote]:
        return self._quotes.get(str(quote_id))

    # ------------------------------------------------------------------ #
    # Payments
    # ------------------------------------------------------------------ #
    def create_payment_transaction(
        self,
        *,
        reference: str,
        provider: str,
        provider_reference: str,
        phone_number: str,
        amount: float,
        currency: str,
        status: str = "PENDING",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> PaymentTransaction:
        now = datetime.utcnow()
        txn = PaymentTransaction(
            reference=str(reference),
            provider=str(provider),
            provider_reference=str(provider_reference),
            phone_number=str(phone_number),
            amount=float(amount),
            currency=str(currency),
            status=str(status),
            metadata=dict(metadata or {}),
            created_at=now,
            updated_at=now,
        )
        self._payment_transactions[txn.reference] = txn
        return txn

    def get_payment_transaction_by_reference(self, reference: str) -> Optional[PaymentTransaction]:
        return self._payment_transactions.get(str(reference))

    def update_payment_transaction_status(
        self,
        reference: str,
        status: str,
        *,
        provider_reference: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[PaymentTransaction]:
        txn = self._payment_transactions.get(str(reference))
        if not txn:
            return None
        txn.status = str(status)
        if provider_reference:
            txn.provider_reference = str(provider_reference)
        if metadata:
            txn.metadata = {**txn.metadata, **metadata}
        txn.updated_at = datetime.utcnow()
        self._payment_transactions[txn.reference] = txn
        return txn

    def add_payment_audit_event(
        self,
        *,
        payment_reference: str,
        event_type: str,
        payload: Optional[Dict[str, Any]] = None,
        status_from: Optional[str] = None,
        status_to: Optional[str] = None,
    ) -> PaymentAuditEvent:
        event = PaymentAuditEvent(
            id=str(uuid.uuid4()),
            payment_reference=str(payment_reference),
            event_type=str(event_type),
            status_from=status_from,
            status_to=status_to,
            payload=dict(payload or {}),
        )
        self._payment_audit_events.append(event)
        return event

    def list_payment_audit_events(self, payment_reference: str) -> List[PaymentAuditEvent]:
        events = [e for e in self._payment_audit_events if e.payment_reference == str(payment_reference)]
        events.sort(key=lambda item: item.created_at)
        return events

    # ------------------------------------------------------------------ #
    # Personal Accident application persistence
    # ------------------------------------------------------------------ #
    def create_pa_application(self, user_id: str, initial_data: Optional[Dict[str, Any]] = None) -> PersonalAccidentApplication:
        app_id = str(uuid.uuid4())
        data = initial_data or {}
        app = PersonalAccidentApplication(
            id=app_id,
            user_id=user_id,
            personal_details=data.get("personal_details", {}),
            next_of_kin=data.get("next_of_kin", {}),
            previous_pa_policy=data.get("previous_pa_policy", {}),
            physical_disability=data.get("physical_disability", {}),
            risky_activities=data.get("risky_activities", {}),
            coverage_plan=data.get("coverage_plan", {}),
            national_id_upload=data.get("national_id_upload", {}),
            quote_id=data.get("quote_id"),
        )
        self._pa_applications[app_id] = app
        return app

    def get_pa_application(self, app_id: str) -> Optional[PersonalAccidentApplication]:
        return self._pa_applications.get(str(app_id))

    def update_pa_application(self, app_id: str, updates: Dict[str, Any]) -> Optional[PersonalAccidentApplication]:
        app = self.get_pa_application(app_id)
        if not app:
            return None
        # Merge updates into the dataclass fields where appropriate
        for k, v in updates.items():
            if hasattr(app, k):
                setattr(app, k, v)
        app.updated_at = datetime.utcnow()
        self._pa_applications[app_id] = app
        return app

    def delete_pa_application(self, app_id: str) -> bool:
        if app_id in self._pa_applications:
            del self._pa_applications[app_id]
            return True
        return False

    def list_pa_applications(
        self,
        user_id: Optional[str] = None,
        order_by: str = "created_at",
        descending: bool = True,
    ) -> List[PersonalAccidentApplication]:
        apps = list(self._pa_applications.values())
        if user_id:
            apps = [a for a in apps if a.user_id == user_id]
        orderable = {
            "id": lambda a: a.id,
            "user_id": lambda a: a.user_id,
            "status": lambda a: a.status,
            "created_at": lambda a: a.created_at,
            "updated_at": lambda a: a.updated_at,
        }
        key_fn = orderable.get(order_by) or orderable["created_at"]
        apps.sort(key=key_fn, reverse=descending)
        return apps

    # ------------------------------------------------------------------ #
    # Travel Insurance application persistence
    # ------------------------------------------------------------------ #
    def create_travel_application(self, user_id: str, initial_data: Optional[Dict[str, Any]] = None) -> TravelInsuranceApplication:
        app_id = str(uuid.uuid4())
        data = initial_data or {}
        app = TravelInsuranceApplication(
            id=app_id,
            user_id=user_id,
            selected_product=data.get("selected_product", {}),
            about_you=data.get("about_you", {}),
            travel_party_and_trip=data.get("travel_party_and_trip", {}),
            data_consent=data.get("data_consent", {}),
            travellers=data.get("travellers", []),
            emergency_contact=data.get("emergency_contact", {}),
            bank_details=data.get("bank_details", {}),
            passport_upload=data.get("passport_upload", {}),
            quote_id=data.get("quote_id"),
        )
        self._travel_applications[app_id] = app
        return app

    def get_travel_application(self, app_id: str) -> Optional[TravelInsuranceApplication]:
        return self._travel_applications.get(str(app_id))

    def update_travel_application(self, app_id: str, updates: Dict[str, Any]) -> Optional[TravelInsuranceApplication]:
        app = self.get_travel_application(app_id)
        if not app:
            return None
        for k, v in updates.items():
            if hasattr(app, k):
                setattr(app, k, v)
        app.updated_at = datetime.utcnow()
        self._travel_applications[app_id] = app
        return app

    def delete_travel_application(self, app_id: str) -> bool:
        if app_id in self._travel_applications:
            del self._travel_applications[app_id]
            return True
        return False

    def list_travel_applications(
        self,
        user_id: Optional[str] = None,
        order_by: str = "created_at",
        descending: bool = True,
    ) -> List[TravelInsuranceApplication]:
        apps = list(self._travel_applications.values())
        if user_id:
            apps = [a for a in apps if a.user_id == user_id]
        orderable = {
            "id": lambda a: a.id,
            "user_id": lambda a: a.user_id,
            "status": lambda a: a.status,
            "created_at": lambda a: a.created_at,
            "updated_at": lambda a: a.updated_at,
        }
        key_fn = orderable.get(order_by) or orderable["created_at"]
        apps.sort(key=key_fn, reverse=descending)
        return apps

    # ------------------------------------------------------------------ #
    # Serenicare application persistence
    # ------------------------------------------------------------------ #
    def create_serenicare_application(self, user_id: str, initial_data: Optional[Dict[str, Any]] = None) -> SerenicareApplication:
        app_id = str(uuid.uuid4())
        data = initial_data or {}
        app = SerenicareApplication(
            id=app_id,
            user_id=user_id,
            cover_personalization=data.get("cover_personalization", {}),
            optional_benefits=data.get("optional_benefits", []),
            medical_conditions=data.get("medical_conditions", {}),
            plan_option=data.get("plan_option", {}),
            about_you=data.get("about_you", {}),
            quote_id=data.get("quote_id"),
        )
        self._serenicare_applications[app_id] = app
        return app

    def get_serenicare_application(self, app_id: str) -> Optional[SerenicareApplication]:
        return self._serenicare_applications.get(str(app_id))

    def update_serenicare_application(self, app_id: str, updates: Dict[str, Any]) -> Optional[SerenicareApplication]:
        app = self.get_serenicare_application(app_id)
        if not app:
            return None
        for k, v in updates.items():
            if hasattr(app, k):
                setattr(app, k, v)
        app.updated_at = datetime.utcnow()
        self._serenicare_applications[app_id] = app
        return app

    def delete_serenicare_application(self, app_id: str) -> bool:
        if app_id in self._serenicare_applications:
            del self._serenicare_applications[app_id]
            return True
        return False

    def list_serenicare_applications(
        self,
        user_id: Optional[str] = None,
        order_by: str = "created_at",
        descending: bool = True,
    ) -> List[SerenicareApplication]:
        apps = list(self._serenicare_applications.values())
        if user_id:
            apps = [a for a in apps if a.user_id == user_id]
        orderable = {
            "id": lambda a: a.id,
            "user_id": lambda a: a.user_id,
            "status": lambda a: a.status,
            "created_at": lambda a: a.created_at,
            "updated_at": lambda a: a.updated_at,
        }
        key_fn = orderable.get(order_by) or orderable["created_at"]
        apps.sort(key=key_fn, reverse=descending)
        return apps

    # ------------------------------------------------------------------ #
    # Escalation persistence
    # ------------------------------------------------------------------ #
    def get_escalation_state(self, session_id: str) -> Optional[Dict[str, Any]]:
        rec = self._escalation_sessions.get(str(session_id))
        if not rec:
            return None
        return {
            "session_id": rec.session_id,
            "conversation_id": rec.conversation_id,
            "user_id": rec.user_id,
            "escalated": rec.escalated,
            "agent_id": rec.agent_id,
            "escalation_reason": rec.escalation_reason,
            "escalation_metadata": rec.escalation_metadata or {},
            "escalated_at": rec.escalated_at.isoformat() if rec.escalated_at else None,
            "agent_joined_at": rec.agent_joined_at.isoformat() if rec.agent_joined_at else None,
            "ended_at": rec.ended_at.isoformat() if rec.ended_at else None,
            "updated_at": rec.updated_at.isoformat() if rec.updated_at else None,
        }

    def mark_escalated(
        self,
        session_id: str,
        *,
        conversation_id: Optional[str] = None,
        user_id: Optional[str] = None,
        reason: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        now = datetime.utcnow()
        rec = self._escalation_sessions.get(str(session_id))
        if not rec:
            rec = EscalationSession(id=str(uuid.uuid4()), session_id=str(session_id))
            self._escalation_sessions[str(session_id)] = rec

        rec.conversation_id = conversation_id or rec.conversation_id
        rec.user_id = user_id or rec.user_id
        rec.escalated = True
        rec.agent_id = None
        rec.escalation_reason = reason or rec.escalation_reason
        rec.escalation_metadata = dict(metadata or rec.escalation_metadata or {})
        rec.escalated_at = now
        rec.ended_at = None
        rec.updated_at = now
        return self.get_escalation_state(session_id) or {}

    def mark_agent_joined(self, session_id: str, agent_id: str) -> Dict[str, Any]:
        now = datetime.utcnow()
        rec = self._escalation_sessions.get(str(session_id))
        if not rec:
            rec = EscalationSession(id=str(uuid.uuid4()), session_id=str(session_id))
            self._escalation_sessions[str(session_id)] = rec
        rec.escalated = True
        rec.agent_id = str(agent_id)
        rec.agent_joined_at = now
        rec.updated_at = now
        return self.get_escalation_state(session_id) or {}

    def end_escalation(self, session_id: str) -> Dict[str, Any]:
        now = datetime.utcnow()
        rec = self._escalation_sessions.get(str(session_id))
        if not rec:
            rec = EscalationSession(id=str(uuid.uuid4()), session_id=str(session_id))
            self._escalation_sessions[str(session_id)] = rec
        rec.escalated = False
        rec.agent_id = None
        rec.escalation_reason = None
        rec.ended_at = now
        rec.updated_at = now
        return self.get_escalation_state(session_id) or {}
