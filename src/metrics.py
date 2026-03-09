import time
import uuid
from datetime import datetime
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import insert
from src.database.models import RAGMetric


async def record_metric(
    db: AsyncSession,
    metric_type: str,
    value: float,
    conversation_id: Optional[str] = None,
):
    metric = RAGMetric(
        id=str(uuid.uuid4()),
        metric_type=metric_type,
        value=value,
        conversation_id=conversation_id,
        created_at=datetime.utcnow(),
    )
    db.add(metric)
    await db.commit()


async def record_retrieval_accuracy(db: AsyncSession, score: float, conversation_id: Optional[str] = None):
    await record_metric(db, "retrieval_accuracy", score, conversation_id)


async def record_confidence(db: AsyncSession, conf: float, conversation_id: Optional[str] = None):
    await record_metric(db, "confidence_score", conf, conversation_id)


async def record_latency(db: AsyncSession, start_time: float, conversation_id: Optional[str] = None):
    latency = time.time() - start_time
    await record_metric(db, "response_latency", latency, conversation_id)


async def record_fallback(db: AsyncSession, conversation_id: Optional[str] = None):
    await record_metric(db, "fallbacks", 1.0, conversation_id)