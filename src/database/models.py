"""
SQLAlchemy models for users, conversations, messages, quotes.
Used by postgres_real when USE_POSTGRES_CONVERSATIONS and DATABASE_URL are set.
"""
from __future__ import annotations
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from uuid import uuid4
from sqlalchemy import JSON, Boolean, DateTime, Float, String, Text, ForeignKey
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    phone_number: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    kyc_completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    conversations: Mapped[list["Conversation"]] = relationship("Conversation", back_populates="user")


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(32), default="conversational")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped["User"] = relationship("User", back_populates="conversations")
    messages: Mapped[list["Message"]] = relationship("Message", back_populates="conversation", order_by="Message.timestamp")
    metrics: Mapped[list["RAGMetric"]] = relationship("RAGMetric", back_populates="conversation")


class EscalationSession(Base):
    __tablename__ = "escalation_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    session_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    conversation_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    user_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)

    escalated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    agent_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    escalation_reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    escalation_metadata: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    escalated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    agent_joined_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    conversation_id: Mapped[str] = mapped_column(String(36), ForeignKey("conversations.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    message_metadata: Mapped[Dict[str, Any]] = mapped_column("metadata", JSON, default=dict, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    conversation: Mapped["Conversation"] = relationship("Conversation", back_populates="messages")


class Quote(Base):
    __tablename__ = "quotes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    product_id: Mapped[str] = mapped_column(String(256), nullable=False)
    product_name: Mapped[str] = mapped_column(String(256), nullable=False)
    premium_amount: Mapped[float] = mapped_column(Float, nullable=False)
    sum_assured: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    underwriting_data: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    pricing_breakdown: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.utcnow() + timedelta(days=30))


class PaymentTransaction(Base):
    __tablename__ = "payment_transactions"

    reference: Mapped[str] = mapped_column(String(128), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    provider_reference: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    phone_number: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True, default="PENDING")
    transaction_metadata: Mapped[Dict[str, Any]] = mapped_column("metadata", JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    audit_events: Mapped[List["PaymentAuditEvent"]] = relationship(
        "PaymentAuditEvent",
        back_populates="transaction",
        cascade="all, delete-orphan",
        order_by="PaymentAuditEvent.created_at",
    )


class PaymentAuditEvent(Base):
    __tablename__ = "payment_audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    payment_reference: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("payment_transactions.reference"),
        nullable=False,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status_from: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    status_to: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    payload: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    transaction: Mapped["PaymentTransaction"] = relationship("PaymentTransaction", back_populates="audit_events")

class RAGMetric(Base):
    __tablename__ = "rag_metrics"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    conversation_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("conversations.id"), nullable=True, index=True)
    metric_type: Mapped[str] = mapped_column(String(50), nullable=False)  # e.g., retrieval_accuracy, confidence_score
    value: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    conversation: Mapped[Optional["Conversation"]] = relationship("Conversation", back_populates="metrics")


# ======================================================================
# NEW: Application persistence tables (PA, Travel, Serenicare)
# ======================================================================

class PersonalAccidentApplication(Base):
    __tablename__ = "personal_accident_applications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)

    status: Mapped[str] = mapped_column(String(32), default="in_progress", nullable=False)

    personal_details: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    next_of_kin: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    previous_pa_policy: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    physical_disability: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    risky_activities: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    coverage_plan: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    national_id_upload: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    quote_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class TravelInsuranceApplication(Base):
    __tablename__ = "travel_insurance_applications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)

    status: Mapped[str] = mapped_column(String(32), default="in_progress", nullable=False)

    selected_product: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    about_you: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    travel_party_and_trip: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    data_consent: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    travellers: Mapped[list] = mapped_column(JSON, default=list, nullable=False)  # list[dict]
    emergency_contact: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    bank_details: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    passport_upload: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    quote_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class SerenicareApplication(Base):
    __tablename__ = "serenicare_applications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)

    status: Mapped[str] = mapped_column(String(32), default="in_progress", nullable=False)

    # Main applicant and dependents (mainMembers)
    main_members: Mapped[list] = mapped_column(JSON, default=list, nullable=False)  # list[dict]

    # Serenicare flow fields
    cover_personalization: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    optional_benefits: Mapped[list] = mapped_column(JSON, default=list, nullable=False)  # list[str]
    medical_conditions: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    plan_option: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    about_you: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    # Legacy/flat fields (if still needed)
    first_name: Mapped[str] = mapped_column(String(50), nullable=True)
    last_name: Mapped[str] = mapped_column(String(50), nullable=True)
    middle_name: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    mobile: Mapped[str] = mapped_column(String(20), nullable=True)
    email: Mapped[str] = mapped_column(String(100), nullable=True)
    plan_type: Mapped[str] = mapped_column(String(32), nullable=True)
    serious_conditions: Mapped[str] = mapped_column(String(8), nullable=True)

    quote_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


# MotorCare (Motor Private) Application schema matching frontend validations

class MotorPrivateApplication(Base):
    __tablename__ = "motor_private_applications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="in_progress", nullable=False)

    # Step 1: Get A Quote
    cover_type: Mapped[str] = mapped_column(String(32), nullable=False)  # "comprehensive" or "third_party"

    # Step 2: Personal Details
    first_name: Mapped[str] = mapped_column(String(50), nullable=False)
    middle_name: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    surname: Mapped[str] = mapped_column(String(50), nullable=False)
    mobile: Mapped[str] = mapped_column(String(20), nullable=False)
    email: Mapped[str] = mapped_column(String(100), nullable=False)

    # Step 3: Premium Calculation
    vehicle_make: Mapped[str] = mapped_column(String(50), nullable=False)
    year_of_manufacture: Mapped[int] = mapped_column(String(4), nullable=False)
    cover_start_date: Mapped[str] = mapped_column(String(20), nullable=False)  # ISO date string
    is_rare_model: Mapped[str] = mapped_column(String(8), nullable=False)  # "yes" or "no"
    has_undergone_valuation: Mapped[str] = mapped_column(String(8), nullable=False)  # "yes" or "no"
    vehicle_value_ugx: Mapped[float] = mapped_column(Float, nullable=False)

    quote_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
