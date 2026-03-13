"""
Real Postgres-backed DB for production when USE_POSTGRES_CONVERSATIONS and DATABASE_URL are set.
Implements the same interface as src.database.postgres (in-memory stub).
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session, sessionmaker

from src.database.models import (
    Base,
    Conversation,
    ConversationEvent,
    EscalationSession,
    Message,
    PaymentAuditEvent,
    PaymentTransaction,
    Quote,
    RAGMetric,
    User,
    PersonalAccidentApplication,
    TravelInsuranceApplication,
    SerenicareApplication,
)


def _normalize_connection_string(s: str) -> str:
    """Strip common mistakes: 'psql \'...\'', extra quotes, whitespace."""
    s = s.strip()
    if re.match(r"^psql\s+", s, re.IGNORECASE):
        s = re.sub(r"^psql\s+", "", s, flags=re.IGNORECASE).strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1].strip()
    return s


class PostgresDB:
    """
    Postgres data access using SQLAlchemy. Use when DATABASE_URL is set and
    USE_POSTGRES_CONVERSATIONS=true.
    """

    def __init__(self, connection_string: str) -> None:
        connection_string = _normalize_connection_string(connection_string)
        self.engine = create_engine(connection_string, pool_pre_ping=True, pool_size=5, max_overflow=10)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine, expire_on_commit=False)

    def create_tables(self) -> None:
        Base.metadata.create_all(bind=self.engine)

    @contextmanager
    def _session(self) -> Session:
        s = self.SessionLocal()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    # ------------------------------------------------------------------ #
    # Users
    # ------------------------------------------------------------------ #
    def get_or_create_user(self, phone_number: str) -> User:
        with self._session() as s:
            stmt = select(User).where(User.phone_number == phone_number)
            u = s.execute(stmt).scalar_one_or_none()
            if u:
                return u
            u = User(id=str(uuid4()), phone_number=phone_number, kyc_completed=False)
            s.add(u)
            s.flush()
            s.refresh(u)
            return u

    def get_user_by_phone(self, phone_number: str) -> Optional[User]:
        with self._session() as s:
            stmt = select(User).where(User.phone_number == phone_number)
            return s.execute(stmt).scalar_one_or_none()

    def get_user_by_id(self, user_id: str) -> Optional[User]:
        with self._session() as s:
            stmt = select(User).where(User.id == user_id)
            return s.execute(stmt).scalar_one_or_none()

    # ------------------------------------------------------------------ #
    # Conversations & messages
    # ------------------------------------------------------------------ #
    def create_conversation(self, user_id: str, mode: str) -> Conversation:
        with self._session() as s:
            c = Conversation(id=str(uuid4()), user_id=user_id, mode=mode)
            s.add(c)
            s.flush()
            s.refresh(c)
            return c

    def add_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Message:
        with self._session() as s:
            m = Message(
                id=str(uuid4()),
                conversation_id=conversation_id,
                role=role,
                content=content,
                message_metadata=metadata or {},
            )
            s.add(m)
            s.flush()
            s.refresh(m)
            return m

    def add_rag_metric(
        self,
        *,
        metric_type: str,
        value: float,
        conversation_id: Optional[str] = None,
        created_at: Optional[datetime] = None,
    ) -> RAGMetric:
        with self._session() as s:
            metric = RAGMetric(
                id=str(uuid4()),
                metric_type=metric_type,
                value=value,
                conversation_id=conversation_id,
                created_at=created_at or datetime.utcnow(),
            )
            s.add(metric)
            s.flush()
            s.refresh(metric)
            return metric

    def add_rag_metrics(self, metrics: List[Dict[str, Any]]) -> List[RAGMetric]:
        created: List[RAGMetric] = []
        with self._session() as s:
            for metric_data in metrics:
                metric = RAGMetric(
                    id=str(uuid4()),
                    metric_type=metric_data["metric_type"],
                    value=float(metric_data["value"]),
                    conversation_id=metric_data.get("conversation_id"),
                    created_at=metric_data.get("created_at") or datetime.utcnow(),
                )
                s.add(metric)
                created.append(metric)
            s.flush()
            for metric in created:
                s.refresh(metric)
        return created

    def get_recent_rag_metrics(
        self,
        *,
        limit: int = 50,
        conversation_id: Optional[str] = None,
    ) -> List[RAGMetric]:
        with self._session() as s:
            stmt = select(RAGMetric).order_by(RAGMetric.created_at.desc()).limit(limit)
            if conversation_id:
                stmt = stmt.where(RAGMetric.conversation_id == conversation_id)
            return list(s.execute(stmt).scalars().all())

    def get_conversation_history(self, conversation_id: str, limit: int = 50) -> List[Message]:
        with self._session() as s:
            stmt = (
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.timestamp.desc())
                .limit(limit)
            )
            return list(s.execute(stmt).scalars().all())

    def add_conversation_event(
        self,
        *,
        conversation_id: str,
        event_type: str,
        payload: Optional[Dict[str, Any]] = None,
        created_at: Optional[datetime] = None,
    ) -> ConversationEvent:
        with self._session() as s:
            ev = ConversationEvent(
                id=str(uuid4()),
                conversation_id=str(conversation_id),
                event_type=str(event_type),
                payload=payload or {},
                created_at=created_at or datetime.utcnow(),
            )
            s.add(ev)
            s.flush()
            s.refresh(ev)
            return ev

    def end_conversation(self, conversation_id: str, ended_at: Optional[datetime] = None) -> Optional[Conversation]:
        with self._session() as s:
            stmt = select(Conversation).where(Conversation.id == str(conversation_id))
            rec = s.execute(stmt).scalar_one_or_none()
            if not rec:
                return None
            rec.ended_at = ended_at or datetime.utcnow()
            s.add(rec)
            s.flush()
            return rec

    def list_conversation_events(
        self,
        *,
        start: datetime,
        end: datetime,
        event_type: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[ConversationEvent]:
        with self._session() as s:
            stmt = select(ConversationEvent).where(
                ConversationEvent.created_at >= start,
                ConversationEvent.created_at < end,
            )
            if event_type:
                stmt = stmt.where(ConversationEvent.event_type == str(event_type))
            stmt = stmt.order_by(ConversationEvent.created_at.desc())
            if limit:
                stmt = stmt.limit(int(limit))
            return list(s.execute(stmt).scalars().all())

    # ------------------------------------------------------------------ #
    # Metrics helpers
    # ------------------------------------------------------------------ #
    def count_conversations(self, start: datetime, end: datetime) -> int:
        with self._session() as s:
            stmt = (
                select(func.count())
                .select_from(Conversation)
                .where(Conversation.created_at >= start, Conversation.created_at < end)
            )
            return int(s.execute(stmt).scalar() or 0)

    def count_escalations(self, start: datetime, end: datetime) -> int:
        with self._session() as s:
            ts = func.coalesce(EscalationSession.escalated_at, EscalationSession.created_at)
            stmt = (
                select(func.count())
                .select_from(EscalationSession)
                .where(ts >= start, ts < end)
            )
            return int(s.execute(stmt).scalar() or 0)

    def count_payment_transactions(self, start: datetime, end: datetime, statuses: List[str]) -> int:
        statuses_upper = [s.upper() for s in (statuses or [])]
        with self._session() as s:
            stmt = (
                select(func.count())
                .select_from(PaymentTransaction)
                .where(
                    PaymentTransaction.created_at >= start,
                    PaymentTransaction.created_at < end,
                    func.upper(PaymentTransaction.status).in_(statuses_upper),
                )
            )
            return int(s.execute(stmt).scalar() or 0)

    def count_quotes(
        self,
        start: datetime,
        end: datetime,
        *,
        exclude_statuses: Optional[List[str]] = None,
    ) -> int:
        """
        Count quotes created in [start, end).

        Pass ``exclude_statuses`` to filter out quotes that have progressed
        beyond a certain state.  For the "chatbot leads" metric we exclude
        quotes whose status is 'paid', 'payment_initiated', or 'completed' so
        that only users who got a quote but never proceeded to payment are
        counted.
        """
        with self._session() as s:
            stmt = (
                select(func.count())
                .select_from(Quote)
                .where(
                    Quote.generated_at >= start,
                    Quote.generated_at < end,
                )
            )
            if exclude_statuses:
                exclude_upper = [st.upper() for st in exclude_statuses]
                stmt = stmt.where(
                    func.upper(Quote.status).not_in(exclude_upper)
                )
            return int(s.execute(stmt).scalar() or 0)

    def list_rag_metrics(
        self,
        *,
        start: datetime,
        end: datetime,
        metric_types: Optional[List[str]] = None,
        limit: Optional[int] = None,
    ) -> List[RAGMetric]:
        with self._session() as s:
            stmt = select(RAGMetric).where(
                RAGMetric.created_at >= start,
                RAGMetric.created_at < end,
            )
            if metric_types:
                stmt = stmt.where(RAGMetric.metric_type.in_(metric_types))
            stmt = stmt.order_by(RAGMetric.created_at.desc())
            if limit:
                stmt = stmt.limit(int(limit))
            return list(s.execute(stmt).scalars().all())

    def list_escalations(self, start: datetime, end: datetime) -> List[EscalationSession]:
        with self._session() as s:
            ts = func.coalesce(EscalationSession.escalated_at, EscalationSession.created_at)
            stmt = select(EscalationSession).where(ts >= start, ts < end)
            return list(s.execute(stmt).scalars().all())

    def list_conversation_message_stats(self, start: datetime, end: datetime) -> List[Dict[str, Any]]:
        with self._session() as s:
            stmt = (
                select(
                    Message.conversation_id,
                    func.min(Message.timestamp),
                    func.max(Message.timestamp),
                    func.count(),
                )
                .where(Message.timestamp >= start, Message.timestamp < end)
                .group_by(Message.conversation_id)
            )
            rows = s.execute(stmt).all()
            return [
                {
                    "conversation_id": str(row[0]),
                    "min_ts": row[1],
                    "max_ts": row[2],
                    "message_count": int(row[3] or 0),
                }
                for row in rows
            ]

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
        with self._session() as s:
            q = Quote(
                id=str(uuid4()),
                user_id=user_id,
                product_id=product_id,
                product_name=product_name or product_id,
                premium_amount=float(premium_amount or 0.0),
                sum_assured=float(sum_assured) if sum_assured is not None else None,
                underwriting_data=underwriting_data or {},
                pricing_breakdown=pricing_breakdown,
                status=status,
            )
            s.add(q)
            s.flush()
            s.refresh(q)
            return q

    def get_quote(self, quote_id: str) -> Optional[Quote]:
        with self._session() as s:
            stmt = select(Quote).where(Quote.id == str(quote_id))
            return s.execute(stmt).scalar_one_or_none()

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
        amount: Any,
        currency: str,
        status: str = "PENDING",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> PaymentTransaction:
        with self._session() as s:
            txn = PaymentTransaction(
                reference=str(reference),
                provider=str(provider),
                provider_reference=str(provider_reference),
                phone_number=str(phone_number),
                amount=float(amount or 0.0),
                currency=str(currency),
                status=str(status),
                transaction_metadata=metadata or {},
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            s.add(txn)
            s.flush()
            s.refresh(txn)
            return txn

    def get_payment_transaction_by_reference(self, reference: str) -> Optional[PaymentTransaction]:
        with self._session() as s:
            stmt = select(PaymentTransaction).where(PaymentTransaction.reference == str(reference))
            return s.execute(stmt).scalar_one_or_none()

    def update_payment_transaction_status(
        self,
        reference: str,
        status: str,
        *,
        provider_reference: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[PaymentTransaction]:
        with self._session() as s:
            stmt = select(PaymentTransaction).where(PaymentTransaction.reference == str(reference))
            txn = s.execute(stmt).scalar_one_or_none()
            if not txn:
                return None

            txn.status = str(status)
            if provider_reference:
                txn.provider_reference = str(provider_reference)
            if metadata:
                txn.transaction_metadata = {**(txn.transaction_metadata or {}), **metadata}
            txn.updated_at = datetime.utcnow()

            s.add(txn)
            s.flush()
            s.refresh(txn)
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
        with self._session() as s:
            event = PaymentAuditEvent(
                id=str(uuid4()),
                payment_reference=str(payment_reference),
                event_type=str(event_type),
                status_from=status_from,
                status_to=status_to,
                payload=payload or {},
                created_at=datetime.utcnow(),
            )
            s.add(event)
            s.flush()
            s.refresh(event)
            return event

    def list_payment_audit_events(self, payment_reference: str) -> List[PaymentAuditEvent]:
        with self._session() as s:
            stmt = (
                select(PaymentAuditEvent)
                .where(PaymentAuditEvent.payment_reference == str(payment_reference))
                .order_by(PaymentAuditEvent.created_at.asc())
            )
            return list(s.execute(stmt).scalars().all())

    # ------------------------------------------------------------------ #
    # Personal Accident applications
    # ------------------------------------------------------------------ #
    def create_pa_application(self, user_id: str, initial_data: Optional[Dict[str, Any]] = None) -> PersonalAccidentApplication:
        data = initial_data or {}
        with self._session() as s:
            app = PersonalAccidentApplication(
                id=str(uuid4()),
                user_id=user_id,
                status=data.get("status", "in_progress"),
                personal_details=data.get("personal_details", {}),
                next_of_kin=data.get("next_of_kin", {}),
                previous_pa_policy=data.get("previous_pa_policy", {}),
                physical_disability=data.get("physical_disability", {}),
                risky_activities=data.get("risky_activities", {}),
                coverage_plan=data.get("coverage_plan", {}),
                national_id_upload=data.get("national_id_upload", {}),
                quote_id=data.get("quote_id"),
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            s.add(app)
            s.flush()
            s.refresh(app)
            return app

    def get_pa_application(self, app_id: str) -> Optional[PersonalAccidentApplication]:
        with self._session() as s:
            stmt = select(PersonalAccidentApplication).where(PersonalAccidentApplication.id == str(app_id))
            return s.execute(stmt).scalar_one_or_none()

    def update_pa_application(self, app_id: str, updates: Dict[str, Any]) -> Optional[PersonalAccidentApplication]:
        with self._session() as s:
            stmt = select(PersonalAccidentApplication).where(PersonalAccidentApplication.id == str(app_id))
            app = s.execute(stmt).scalar_one_or_none()
            if not app:
                return None
            for k, v in (updates or {}).items():
                if hasattr(app, k):
                    setattr(app, k, v)
            app.updated_at = datetime.utcnow()
            s.add(app)
            s.flush()
            s.refresh(app)
            return app

    def delete_pa_application(self, app_id: str) -> bool:
        with self._session() as s:
            stmt = select(PersonalAccidentApplication).where(PersonalAccidentApplication.id == str(app_id))
            app = s.execute(stmt).scalar_one_or_none()
            if not app:
                return False
            s.delete(app)
            return True

    def list_pa_applications(
        self,
        user_id: Optional[str] = None,
        order_by: str = "created_at",
        descending: bool = True,
    ) -> List[PersonalAccidentApplication]:
        with self._session() as s:
            stmt = select(PersonalAccidentApplication)
            if user_id:
                stmt = stmt.where(PersonalAccidentApplication.user_id == str(user_id))
            orderable = {
                "id": PersonalAccidentApplication.id,
                "user_id": PersonalAccidentApplication.user_id,
                "status": PersonalAccidentApplication.status,
                "created_at": PersonalAccidentApplication.created_at,
                "updated_at": PersonalAccidentApplication.updated_at,
            }
            col = orderable.get(order_by) or PersonalAccidentApplication.created_at
            stmt = stmt.order_by(col.desc() if descending else col.asc())
            return list(s.execute(stmt).scalars().all())

    # ------------------------------------------------------------------ #
    # Travel Insurance applications
    # ------------------------------------------------------------------ #
    def create_travel_application(self, user_id: str, initial_data: Optional[Dict[str, Any]] = None) -> TravelInsuranceApplication:
        data = initial_data or {}
        with self._session() as s:
            app = TravelInsuranceApplication(
                id=str(uuid4()),
                user_id=user_id,
                status=data.get("status", "in_progress"),
                selected_product=data.get("selected_product", {}),
                about_you=data.get("about_you", {}),
                travel_party_and_trip=data.get("travel_party_and_trip", {}),
                data_consent=data.get("data_consent", {}),
                travellers=data.get("travellers", []),
                emergency_contact=data.get("emergency_contact", {}),
                bank_details=data.get("bank_details", {}),
                passport_upload=data.get("passport_upload", {}),
                quote_id=data.get("quote_id"),
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            s.add(app)
            s.flush()
            s.refresh(app)
            return app

    def get_travel_application(self, app_id: str) -> Optional[TravelInsuranceApplication]:
        with self._session() as s:
            stmt = select(TravelInsuranceApplication).where(TravelInsuranceApplication.id == str(app_id))
            return s.execute(stmt).scalar_one_or_none()

    def update_travel_application(self, app_id: str, updates: Dict[str, Any]) -> Optional[TravelInsuranceApplication]:
        with self._session() as s:
            stmt = select(TravelInsuranceApplication).where(TravelInsuranceApplication.id == str(app_id))
            app = s.execute(stmt).scalar_one_or_none()
            if not app:
                return None
            for k, v in (updates or {}).items():
                if hasattr(app, k):
                    setattr(app, k, v)
            app.updated_at = datetime.utcnow()
            s.add(app)
            s.flush()
            s.refresh(app)
            return app

    def delete_travel_application(self, app_id: str) -> bool:
        with self._session() as s:
            stmt = select(TravelInsuranceApplication).where(TravelInsuranceApplication.id == str(app_id))
            app = s.execute(stmt).scalar_one_or_none()
            if not app:
                return False
            s.delete(app)
            return True

    def list_travel_applications(
        self,
        user_id: Optional[str] = None,
        order_by: str = "created_at",
        descending: bool = True,
    ) -> List[TravelInsuranceApplication]:
        with self._session() as s:
            stmt = select(TravelInsuranceApplication)
            if user_id:
                stmt = stmt.where(TravelInsuranceApplication.user_id == str(user_id))
            orderable = {
                "id": TravelInsuranceApplication.id,
                "user_id": TravelInsuranceApplication.user_id,
                "status": TravelInsuranceApplication.status,
                "created_at": TravelInsuranceApplication.created_at,
                "updated_at": TravelInsuranceApplication.updated_at,
            }
            col = orderable.get(order_by) or TravelInsuranceApplication.created_at
            stmt = stmt.order_by(col.desc() if descending else col.asc())
            return list(s.execute(stmt).scalars().all())

    # ------------------------------------------------------------------ #
    # Serenicare applications
    # ------------------------------------------------------------------ #
    def create_serenicare_application(self, user_id: str, initial_data: Optional[Dict[str, Any]] = None) -> SerenicareApplication:
        data = initial_data or {}
        with self._session() as s:
            app = SerenicareApplication(
                id=str(uuid4()),
                user_id=user_id,
                status=data.get("status", "in_progress"),
                cover_personalization=data.get("cover_personalization", {}),
                optional_benefits=data.get("optional_benefits", []),
                medical_conditions=data.get("medical_conditions", {}),
                plan_option=data.get("plan_option", {}),
                about_you=data.get("about_you", {}),
                quote_id=data.get("quote_id"),
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            s.add(app)
            s.flush()
            s.refresh(app)
            return app

    def get_serenicare_application(self, app_id: str) -> Optional[SerenicareApplication]:
        with self._session() as s:
            stmt = select(SerenicareApplication).where(SerenicareApplication.id == str(app_id))
            return s.execute(stmt).scalar_one_or_none()

    def update_serenicare_application(self, app_id: str, updates: Dict[str, Any]) -> Optional[SerenicareApplication]:
        with self._session() as s:
            stmt = select(SerenicareApplication).where(SerenicareApplication.id == str(app_id))
            app = s.execute(stmt).scalar_one_or_none()
            if not app:
                return None
            for k, v in (updates or {}).items():
                if hasattr(app, k):
                    setattr(app, k, v)
            app.updated_at = datetime.utcnow()
            s.add(app)
            s.flush()
            s.refresh(app)
            return app

    def delete_serenicare_application(self, app_id: str) -> bool:
        with self._session() as s:
            stmt = select(SerenicareApplication).where(SerenicareApplication.id == str(app_id))
            app = s.execute(stmt).scalar_one_or_none()
            if not app:
                return False
            s.delete(app)
            return True

    def list_serenicare_applications(
        self,
        user_id: Optional[str] = None,
        order_by: str = "created_at",
        descending: bool = True,
    ) -> List[SerenicareApplication]:
        with self._session() as s:
            stmt = select(SerenicareApplication)
            if user_id:
                stmt = stmt.where(SerenicareApplication.user_id == str(user_id))
            orderable = {
                "id": SerenicareApplication.id,
                "user_id": SerenicareApplication.user_id,
                "status": SerenicareApplication.status,
                "created_at": SerenicareApplication.created_at,
                "updated_at": SerenicareApplication.updated_at,
            }
            col = orderable.get(order_by) or SerenicareApplication.created_at
            stmt = stmt.order_by(col.desc() if descending else col.asc())
            return list(s.execute(stmt).scalars().all())

    # ------------------------------------------------------------------ #
    # Escalation persistence
    # ------------------------------------------------------------------ #
    def get_escalation_state(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self._session() as s:
            stmt = select(EscalationSession).where(EscalationSession.session_id == str(session_id))
            rec = s.execute(stmt).scalar_one_or_none()
            if not rec:
                return None
            return {
                "session_id": rec.session_id,
                "conversation_id": rec.conversation_id,
                "user_id": rec.user_id,
                "escalated": bool(rec.escalated),
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
        with self._session() as s:
            stmt = select(EscalationSession).where(EscalationSession.session_id == str(session_id))
            rec = s.execute(stmt).scalar_one_or_none()
            now = datetime.utcnow()
            if not rec:
                rec = EscalationSession(
                    id=str(uuid4()),
                    session_id=str(session_id),
                    conversation_id=conversation_id,
                    user_id=user_id,
                    escalated=True,
                    agent_id=None,
                    escalation_reason=reason,
                    escalation_metadata=metadata or {},
                    escalated_at=now,
                    ended_at=None,
                    created_at=now,
                    updated_at=now,
                )
                s.add(rec)
            else:
                rec.conversation_id = conversation_id or rec.conversation_id
                rec.user_id = user_id or rec.user_id
                rec.escalated = True
                rec.agent_id = None
                rec.escalation_reason = reason or rec.escalation_reason
                rec.escalation_metadata = dict(metadata or rec.escalation_metadata or {})
                rec.escalated_at = now
                rec.ended_at = None
                rec.updated_at = now
                s.add(rec)
            s.flush()
            return {
                "session_id": rec.session_id,
                "conversation_id": rec.conversation_id,
                "user_id": rec.user_id,
                "escalated": bool(rec.escalated),
                "agent_id": rec.agent_id,
                "escalation_reason": rec.escalation_reason,
                "escalation_metadata": rec.escalation_metadata or {},
                "escalated_at": rec.escalated_at.isoformat() if rec.escalated_at else None,
                "agent_joined_at": rec.agent_joined_at.isoformat() if rec.agent_joined_at else None,
                "ended_at": rec.ended_at.isoformat() if rec.ended_at else None,
                "updated_at": rec.updated_at.isoformat() if rec.updated_at else None,
            }

    def mark_agent_joined(self, session_id: str, agent_id: str) -> Dict[str, Any]:
        with self._session() as s:
            stmt = select(EscalationSession).where(EscalationSession.session_id == str(session_id))
            rec = s.execute(stmt).scalar_one_or_none()
            now = datetime.utcnow()
            if not rec:
                rec = EscalationSession(
                    id=str(uuid4()),
                    session_id=str(session_id),
                    escalated=True,
                    agent_id=str(agent_id),
                    escalation_metadata={},
                    agent_joined_at=now,
                    created_at=now,
                    updated_at=now,
                )
                s.add(rec)
            else:
                rec.escalated = True
                rec.agent_id = str(agent_id)
                rec.agent_joined_at = now
                rec.updated_at = now
                s.add(rec)
            s.flush()
            return {
                "session_id": rec.session_id,
                "conversation_id": rec.conversation_id,
                "user_id": rec.user_id,
                "escalated": bool(rec.escalated),
                "agent_id": rec.agent_id,
                "escalation_reason": rec.escalation_reason,
                "escalation_metadata": rec.escalation_metadata or {},
                "escalated_at": rec.escalated_at.isoformat() if rec.escalated_at else None,
                "agent_joined_at": rec.agent_joined_at.isoformat() if rec.agent_joined_at else None,
                "ended_at": rec.ended_at.isoformat() if rec.ended_at else None,
                "updated_at": rec.updated_at.isoformat() if rec.updated_at else None,
            }

    def end_escalation(self, session_id: str) -> Dict[str, Any]:
        with self._session() as s:
            stmt = select(EscalationSession).where(EscalationSession.session_id == str(session_id))
            rec = s.execute(stmt).scalar_one_or_none()
            now = datetime.utcnow()
            if not rec:
                rec = EscalationSession(
                    id=str(uuid4()),
                    session_id=str(session_id),
                    escalated=False,
                    escalation_metadata={},
                    ended_at=now,
                    created_at=now,
                    updated_at=now,
                )
                s.add(rec)
            else:
                rec.escalated = False
                rec.agent_id = None
                rec.escalation_reason = None
                rec.ended_at = now
                rec.updated_at = now
                s.add(rec)
            s.flush()
            return {
                "session_id": rec.session_id,
                "conversation_id": rec.conversation_id,
                "user_id": rec.user_id,
                "escalated": bool(rec.escalated),
                "agent_id": rec.agent_id,
                "escalation_reason": rec.escalation_reason,
                "escalation_metadata": rec.escalation_metadata or {},
                "escalated_at": rec.escalated_at.isoformat() if rec.escalated_at else None,
                "agent_joined_at": rec.agent_joined_at.isoformat() if rec.agent_joined_at else None,
                "ended_at": rec.ended_at.isoformat() if rec.ended_at else None,
                "updated_at": rec.updated_at.isoformat() if rec.updated_at else None,
            }
