"""
Product-agnostic quote and underwriting endpoints.

Provides versioned REST APIs for:
- Quote preview (indicative quotes before underwriting)
- Underwriting assessment (risk evaluation)
- Quote finalization (binding quotes after assessment)
- Quote/assessment retrieval

Design principles:
- Product-agnostic: works for any insurance product via product_id
- Contract-first: strict request/response schemas
- Swappable: mock vs real via environment config
- Testable: each endpoint independently testable
- Observable: structured logging with trace_id
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, Optional
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Header, Depends
from fastapi.responses import Response

from src.integrations.contracts.quotes import (
    QuotePreviewRequest,
    QuotePreviewResponse,
    FinalQuoteRequest,
    FinalQuoteResponse,
    QuoteRetrievalResponse,
    BenefitItem,
    PremiumBreakdown,
)
from src.integrations.contracts.underwriting_assessment import (
    UnderwritingAssessmentRequest,
    UnderwritingAssessmentResponse,
    UnderwritingDecision,
    RequirementItem,
    UnderwritingRetrievalResponse,
)
from src.integrations.product_benefits import product_benefits_loader
from src.integrations.underwriting import run_quote_preview
from src.integrations.clients.mocks.underwriting import mock_underwriting_client
from src.integrations.policy.underwriting_service import UnderwritingService

logger = logging.getLogger(__name__)

api = APIRouter(prefix="/v1/products", tags=["Product Quotes & Underwriting"])


# In-memory stores for demo (replace with database in production)
_quotes_store: Dict[str, Dict[str, Any]] = {}
_assessments_store: Dict[str, Dict[str, Any]] = {}
_pdf_store: Dict[str, bytes] = {}


from src.integrations.config import should_use_real_integrations as _should_use_real_integrations


def _get_trace_id(x_trace_id: Optional[str] = Header(None)) -> str:
    """Get or generate trace ID for request tracking."""
    return x_trace_id or f"trace-{uuid4().hex[:16]}"


@api.post("/{product_id}/quotes/preview")
async def preview_quote(
    product_id: str,
    request: QuotePreviewRequest,
    trace_id: str = Depends(_get_trace_id),
) -> QuotePreviewResponse:
    """
    Generate an indicative (non-binding) quote preview.

    This endpoint provides a quick premium estimate based on basic information.
    The quote is NOT binding and may change after full underwriting assessment.

    Use cases:
    - Show customers an instant quote before collecting detailed information
    - Display benefits and exclusions for a coverage tier
    - Allow quote download before commitment

    **Important:** This is an estimate. Final premium determined after underwriting.
    """
    logger.info(f"[{trace_id}] Quote preview requested for {product_id}", extra={
        "trace_id": trace_id,
        "product_id": product_id,
        "user_id": request.user_id,
    })

    try:
        # Normalize sum assured
        sum_assured = request.sum_assured or request.cover_limit_ugx
        if not sum_assured:
            raise HTTPException(status_code=400, detail="sum_assured or cover_limit_ugx is required")

        # Load product benefits and configuration
        benefits_data = product_benefits_loader.get_benefits_for_tier(product_id, sum_assured)
        exclusions = product_benefits_loader.get_exclusions(product_id)
        assumptions = product_benefits_loader.get_important_notes(product_id)

        # Convert benefits to contract format
        benefits = [
            BenefitItem(
                code=b.get("code", ""),
                description=product_benefits_loader.format_benefit_description(b),
                amount=b.get("amount"),
                unit=b.get("unit"),
            )
            for b in benefits_data
        ]

        # Run quote preview (calls underwriting mock or service)
        preview_result = await run_quote_preview(
            user_id=request.user_id,
            product_id=product_id,
            underwriting_data={
                "dob": request.date_of_birth,
                "gender": request.gender,
                "occupation": request.occupation,
                "coverLimitAmountUgx": str(int(sum_assured)),
                "policyStartDate": request.policy_start_date,
                **request.product_data,
            },
            currency=request.currency,
            metadata=request.metadata,
        )

        # Extract underwriting and quotation data
        underwriting = preview_result.get("underwriting", {})
        quotation = preview_result.get("quotation", {})

        quote_id = quotation.get("quote_id") or underwriting.get("quote_id") or f"QT-{uuid4().hex[:12].upper()}"
        premium = quotation.get("amount") or quotation.get("premium") or underwriting.get("premium", 0)

        # Build premium breakdown
        breakdown_data = underwriting.get("breakdown", {})
        breakdown = PremiumBreakdown(
            base_premium=breakdown_data.get("annual_base", 0),
            age_loading=breakdown_data.get("age_modifier_amount", 0),
            risk_loading=breakdown_data.get("risk_loading", 0),
            total=premium,
            frequency=request.payment_frequency,
            annual_equivalent=breakdown_data.get("annual_total"),
            metadata=breakdown_data,
        )

        # Generate PDF
        pdf_url = None
        try:
            from src.integrations.quote_pdf import quote_pdf_generator
            if quote_pdf_generator:
                response_data = {
                    "quote_id": quote_id,
                    "product_id": product_id,
                    "product_name": product_benefits_loader.get_product_config(product_id).get("name", product_id),
                    "status": "preview",
                    "premium": premium,
                    "currency": request.currency,
                    "payment_frequency": request.payment_frequency,
                    "sum_assured": sum_assured,
                    "benefits": [b.dict() for b in benefits],
                    "exclusions": exclusions,
                    "assumptions": assumptions,
                    "breakdown": breakdown.dict(),
                    "created_at": datetime.utcnow().isoformat(),
                }
                pdf_bytes = quote_pdf_generator.generate_quote_pdf(response_data)
                _pdf_store[quote_id] = pdf_bytes
                pdf_url = f"/v1/quotes/{quote_id}/download"
        except Exception as e:
            logger.warning(f"Failed to generate PDF: {e}")

        # Create response
        response = QuotePreviewResponse(
            quote_id=quote_id,
            product_id=product_id,
            product_name=product_benefits_loader.get_product_config(product_id).get("name", product_id),
            status="preview",
            is_binding=False,
            premium=premium,
            currency=request.currency,
            payment_frequency=request.payment_frequency,
            breakdown=breakdown,
            sum_assured=sum_assured,
            benefits=benefits,
            policy_start_date=request.policy_start_date,
            policy_duration_months=12,
            assumptions=assumptions,
            exclusions=exclusions,
            important_notes=[],
            download_url=pdf_url,
            valid_until=(datetime.utcnow() + timedelta(days=30)).isoformat(),
            metadata={"trace_id": trace_id},
        )

        # Store quote
        _quotes_store[quote_id] = response.dict()

        logger.info(f"[{trace_id}] Quote preview generated: {quote_id}")
        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[{trace_id}] Failed to generate quote preview")
        raise HTTPException(status_code=500, detail=f"Failed to generate quote: {str(e)}")


@api.post("/{product_id}/underwriting/assess")
async def assess_underwriting(
    product_id: str,
    request: UnderwritingAssessmentRequest,
    trace_id: str = Depends(_get_trace_id),
) -> UnderwritingAssessmentResponse:
    """
    Perform full underwriting assessment with risk evaluation.

    This endpoint performs comprehensive risk assessment including:
    - Medical/health screening
    - Insurance history check
    - Risk factor analysis
    - Premium adjustment calculation
    - Decision (APPROVED/DECLINED/REFERRED)

    Use after collecting complete customer information and disclosures.

    Returns decision and any requirements/exclusions.
    """
    logger.info(f"[{trace_id}] Underwriting assessment for {product_id}", extra={
        "trace_id": trace_id,
        "product_id": product_id,
        "user_id": request.user_id,
    })

    try:
        # Build underwriting payload
        underwriting_payload = {
            "user_id": request.user_id,
            "product_id": product_id,
            "coverLimitAmountUgx": str(int(request.sum_assured)),
            "dob": request.date_of_birth,
            "gender": request.gender,
            "occupation": request.occupation,
            "riskyActivities": request.risky_activities,
            "policyStartDate": request.policy_start_date,
            "has_pre_existing_conditions": request.has_pre_existing_conditions,
            "pre_existing_conditions": request.pre_existing_conditions,
            "smoker": request.smoker,
            **request.product_specific_data,
        }

        # Run underwriting (mock or real)
        if _should_use_real_integrations() and os.getenv("PARTNER_UNDERWRITING_API_URL"):
            underwriting_raw = await UnderwritingService().submit_underwriting(underwriting_payload)
        else:
            underwriting_raw = await mock_underwriting_client.submit_underwriting(underwriting_payload)

        # Parse results
        assessment_id = f"UW-{uuid4().hex[:12].upper()}"
        decision_status = underwriting_raw.get("decision_status", "APPROVED")
        base_premium = underwriting_raw.get("breakdown", {}).get("annual_base", 0)
        final_premium = underwriting_raw.get("premium", 0)
        requirements = [
            RequirementItem(
                type=req.get("type", "info"),
                field=req.get("field"),
                message=req.get("message", ""),
                severity="blocker" if req.get("type") == "eligibility" else "warning",
            )
            for req in underwriting_raw.get("requirements", [])
        ]

        # Build decision
        decision = UnderwritingDecision(
            status=decision_status,
            base_premium=base_premium,
            final_premium=final_premium,
            premium_adjustment_percent=((final_premium - base_premium) / base_premium * 100) if base_premium > 0 else 0,
            adjustment_reasons=[],
            decline_reasons=[req.message for req in requirements if req.type == "eligibility"],
            referral_reasons=[req.message for req in requirements if req.type == "underwriting"],
        )

        # Create response
        response = UnderwritingAssessmentResponse(
            assessment_id=assessment_id,
            product_id=product_id,
            user_id=request.user_id,
            quote_id=request.quote_id,
            decision=decision,
            requirements=requirements,
            risk_score=None,
            risk_category=None,
            risk_factors=[],
            valid_until=(datetime.utcnow() + timedelta(days=30)).isoformat(),
            auto_decisioned=True,
            requires_manual_review=(decision_status == "REFERRED"),
            metadata={"trace_id": trace_id, "underwriting_raw": underwriting_raw},
        )

        # Store assessment
        _assessments_store[assessment_id] = response.dict()

        logger.info(f"[{trace_id}] Assessment complete: {assessment_id} - {decision_status}")
        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[{trace_id}] Failed to assess underwriting")
        raise HTTPException(status_code=500, detail=f"Assessment failed: {str(e)}")


@api.post("/{product_id}/quotes/finalize")
async def finalize_quote(
    product_id: str,
    request: FinalQuoteRequest,
    trace_id: str = Depends(_get_trace_id),
) -> FinalQuoteResponse:
    """
    Finalize a quote after successful underwriting assessment.

    Converts an indicative quote to a binding quote ready for payment.
    Requires an approved underwriting assessment.

    This quote is binding and can be used for policy issuance after payment.
    """
    logger.info(f"[{trace_id}] Finalizing quote {request.quote_id}", extra={
        "trace_id": trace_id,
        "product_id": product_id,
        "quote_id": request.quote_id,
    })

    try:
        # Retrieve original quote and assessment
        original_quote = _quotes_store.get(request.quote_id)
        if not original_quote:
            raise HTTPException(status_code=404, detail=f"Quote {request.quote_id} not found")

        assessment = _assessments_store.get(request.underwriting_assessment_id)
        if not assessment:
            raise HTTPException(status_code=404, detail=f"Assessment {request.underwriting_assessment_id} not found")

        if assessment["decision"]["status"] != "APPROVED":
            raise HTTPException(status_code=400, detail="Cannot finalize quote with non-approved assessment")

        # Use updated premium or original
        final_premium = request.updated_premium or assessment["decision"]["final_premium"]

        # Build final quote
        final_quote_id = f"FQ-{uuid4().hex[:12].upper()}"

        response = FinalQuoteResponse(
            quote_id=final_quote_id,
            product_id=product_id,
            product_name=original_quote["product_name"],
            status="final",
            is_binding=True,
            premium=final_premium,
            currency=original_quote["currency"],
            payment_frequency=original_quote["payment_frequency"],
            breakdown=PremiumBreakdown(**original_quote["breakdown"]),
            sum_assured=original_quote["sum_assured"],
            benefits=[BenefitItem(**b) for b in original_quote["benefits"]],
            policy_start_date=original_quote["policy_start_date"],
            policy_end_date=(datetime.fromisoformat(original_quote["policy_start_date"]) + timedelta(days=365)).isoformat()[:10],
            policy_duration_months=12,
            exclusions=original_quote["exclusions"] + request.additional_exclusions,
            special_terms=request.special_terms,
            download_url=None,  # TODO: Generate final quote PDF
            underwriting_assessment_id=request.underwriting_assessment_id,
            valid_until=(datetime.utcnow() + timedelta(days=30)).isoformat(),
            payment_required=True,
            payment_amount=final_premium,
            metadata={"trace_id": trace_id, "original_quote_id": request.quote_id},
        )

        # Store final quote
        _quotes_store[final_quote_id] = response.dict()

        logger.info(f"[{trace_id}] Final quote created: {final_quote_id}")
        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[{trace_id}] Failed to finalize quote")
        raise HTTPException(status_code=500, detail=f"Finalization failed: {str(e)}")


@api.get("/quotes/{quote_id}")
async def get_quote(quote_id: str) -> QuoteRetrievalResponse:
    """Retrieve an existing quote by ID."""
    quote = _quotes_store.get(quote_id)
    if not quote:
        raise HTTPException(status_code=404, detail=f"Quote {quote_id} not found")

    return QuoteRetrievalResponse(
        quote_id=quote["quote_id"],
        product_id=quote["product_id"],
        product_name=quote["product_name"],
        status=quote["status"],
        is_binding=quote["is_binding"],
        premium=quote["premium"],
        currency=quote["currency"],
        sum_assured=quote["sum_assured"],
        created_at=quote["created_at"],
        valid_until=quote.get("valid_until"),
        download_url=quote.get("download_url"),
        metadata=quote.get("metadata", {}),
    )


@api.get("/quotes/{quote_id}/download")
async def download_quote_pdf(quote_id: str):
    """Download quote as PDF."""
    pdf_bytes = _pdf_store.get(quote_id)
    if not pdf_bytes:
        raise HTTPException(status_code=404, detail=f"PDF not found for quote {quote_id}")

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"attachment; filename=quote_{quote_id}.pdf"
        }
    )


@api.get("/underwriting/{assessment_id}")
async def get_assessment(assessment_id: str) -> UnderwritingRetrievalResponse:
    """Retrieve an existing underwriting assessment by ID."""
    assessment = _assessments_store.get(assessment_id)
    if not assessment:
        raise HTTPException(status_code=404, detail=f"Assessment {assessment_id} not found")

    return UnderwritingRetrievalResponse(
        assessment_id=assessment["assessment_id"],
        product_id=assessment["product_id"],
        user_id=assessment["user_id"],
        quote_id=assessment.get("quote_id"),
        decision_status=assessment["decision"]["status"],
        final_premium=assessment["decision"]["final_premium"],
        created_at=assessment["created_at"],
        auto_decisioned=assessment["auto_decisioned"],
        metadata=assessment.get("metadata", {}),
    )


__all__ = ["api"]
