# Quote and Underwriting API Documentation

## Overview

This document describes the product-agnostic quote and underwriting API endpoints. These endpoints provide a clean separation between quote preview (indicative quotes), underwriting assessment (risk evaluation), and quote finalization (binding quotes).

## Architecture Benefits

✅ **Product-agnostic**: Works for any insurance product via `product_id`  
✅ **Configurable**: Benefits loaded from JSON files (`product_json/`)  
✅ **Testable**: Each endpoint independently testable  
✅ **Swappable**: Mock vs real via environment config  
✅ **Observable**: Structured logging with `trace_id`  
✅ **PDF Download**: Automatic PDF generation for quotes  

## Endpoints

### 1. Quote Preview (Indicative Quote)

**POST** `/v1/products/{product_id}/quotes/preview`

Generate a non-binding indicative quote based on basic information.

**Use when:**
- Customer wants to see instant premium estimate
- Showing benefits before collecting full details
- Allowing quote download before commitment

**Request:**
```json
{
  "product_id": "personal_accident",
  "user_id": "user-123",
  "sum_assured": 10000000,
  "date_of_birth": "1990-01-15",
  "gender": "Male",
  "occupation": "Engineer",
  "policy_start_date": "2026-04-01",
  "payment_frequency": "monthly",
  "currency": "UGX",
  "product_data": {}
}
```

**Response:**
```json
{
  "quote_id": "QT-ABC123",
  "product_id": "personal_accident",
  "product_name": "Personal Accident Insurance",
  "status": "preview",
  "is_binding": false,
  "premium": 6250.00,
  "currency": "UGX",
  "payment_frequency": "monthly",
  "breakdown": {
    "base_premium": 75000.00,
    "age_loading": -7500.00,
    "gender_loading": 11250.00,
    "total": 6250.00,
    "frequency": "monthly",
    "annual_equivalent": 75000.00
  },
  "sum_assured": 10000000,
  "benefits": [
    {
      "code": "ACCIDENTAL_DEATH",
      "description": "Accidental death benefit: UGX 10,000,000 lump sum"
    },
    {
      "code": "PERMANENT_DISABILITY",
      "description": "Permanent disability: UGX 10,000,000 up to"
    }
  ],
  "exclusions": [
    "Self-inflicted injuries",
    "War, invasion, or civil commotion"
  ],
  "assumptions": [
    "This is an indicative quote based on information provided",
    "Final premium may change after full underwriting assessment"
  ],
  "download_url": "/v1/quotes/QT-ABC123/download",
  "valid_until": "2026-04-05T10:30:00Z"
}
```

### 2. Underwriting Assessment

**POST** `/v1/products/{product_id}/underwriting/assess`

Perform comprehensive risk assessment with full disclosures.

**Use when:**
- Customer has completed all underwriting questions
- Ready to evaluate insurability
- Need final premium determination

**Request:**
```json
{
  "product_id": "personal_accident",
  "user_id": "user-123",
  "quote_id": "QT-ABC123",
  "date_of_birth": "1990-01-15",
  "gender": "Male",
  "nationality": "Ugandan",
  "occupation": "Engineer",
  "sum_assured": 10000000,
  "policy_start_date": "2026-04-01",
  "has_pre_existing_conditions": false,
  "pre_existing_conditions": [],
  "risky_activities": ["diving"],
  "smoker": false,
  "declaration_truthful": true,
  "consent_medical_exam": true
}
```

**Response:**
```json
{
  "assessment_id": "UW-XYZ789",
  "product_id": "personal_accident",
  "user_id": "user-123",
  "quote_id": "QT-ABC123",
  "decision": {
    "status": "APPROVED",
    "decision_date": "2026-03-05T10:30:00Z",
    "base_premium": 75000.00,
    "final_premium": 86250.00,
    "premium_adjustment_percent": 15,
    "adjustment_reasons": ["Risky activity: diving"],
    "exclusions_added": [],
    "special_terms": []
  },
  "requirements": [
    {
      "type": "underwriting",
      "field": "riskyActivities",
      "message": "Risky activities declared; application requires manual underwriting review",
      "severity": "warning"
    }
  ],
  "auto_decisioned": true,
  "requires_manual_review": false,
  "valid_until": "2026-04-05T10:30:00Z"
}
```

**Decision Status:**
- `APPROVED`: Customer approved, quote can be finalized
- `DECLINED`: Customer declined, cannot proceed
- `REFERRED`: Requires manual underwriter review
- `PENDING`: Additional information needed

### 3. Quote Finalization

**POST** `/v1/products/{product_id}/quotes/finalize`

Convert indicative quote to binding quote after approved assessment.

**Request:**
```json
{
  "quote_id": "QT-ABC123",
  "user_id": "user-123",
  "underwriting_assessment_id": "UW-XYZ789",
  "updated_premium": 86250.00,
  "additional_exclusions": [],
  "special_terms": []
}
```

**Response:**
```json
{
  "quote_id": "FQ-FINAL123",
  "product_id": "personal_accident",
  "status": "final",
  "is_binding": true,
  "premium": 86250.00,
  "policy_start_date": "2026-04-01",
  "policy_end_date": "2027-04-01",
  "payment_required": true,
  "payment_amount": 86250.00,
  "download_url": "/v1/quotes/FQ-FINAL123/download"
}
```

### 4. Retrieve Quote

**GET** `/v1/quotes/{quote_id}`

Retrieve existing quote by ID.

### 5. Download Quote PDF

**GET** `/v1/quotes/{quote_id}/download`

Download quote as PDF file.

### 6. Retrieve Assessment

**GET** `/v1/underwriting/{assessment_id}`

Retrieve existing underwriting assessment by ID.

## Flow Integration

### Typical Flow Sequence

```
1. Customer enters basic info
   ↓
2. POST /v1/products/personal_accident/quotes/preview
   → Returns indicative quote + benefits + PDF download
   ↓
3. Customer reviews quote, decides to proceed
   ↓
4. Customer completes full underwriting questions
   ↓
5. POST /v1/products/personal_accident/underwriting/assess
   → Returns APPROVED/DECLINED/REFERRED decision
   ↓
6. If APPROVED:
   POST /v1/products/personal_accident/quotes/finalize
   → Returns binding quote
   ↓
7. Proceed to payment with binding quote
```

### Integration with Personal Accident Flow

The PA flow now uses these endpoints:
- **Step 0 (Quick Quote)** → Calls quote preview internally
- **Step 1 (Premium Summary)** → Shows preview quote with benefits from JSON
- **Steps 2-7** → Collect full underwriting data
- **Step 8** → Call underwriting assess
- **Step 9** → Finalize quote and proceed to payment

## Configuration

### Product Benefits Configuration

Benefits are loaded from `/product_json/{product_id}_config.json`:

```json
{
  "product_id": "personal_accident",
  "name": "Personal Accident Insurance",
  "coverage_tiers": [
    {
      "tier_id": "basic",
      "sum_assured": 5000000,
      "benefits": [
        {
          "code": "ACCIDENTAL_DEATH",
          "description": "Accidental death benefit",
          "amount": 5000000,
          "unit": "lump sum"
        }
      ],
      "exclusions": [...],
      "premium_factors": {
        "base_rate_pct": 0.15,
        "age_bands": [...],
        "gender_modifiers": {...}
      }
    }
  ]
}
```

### Environment Variables

```bash
# Use mock underwriting (default for development)
INTEGRATIONS_MODE=mock

# Use real underwriting service
INTEGRATIONS_MODE=real
PARTNER_UNDERWRITING_API_URL=https://api.partner.com/underwriting
PARTNER_UNDERWRITING_API_KEY=your-key

# Per-product override
INTEGRATIONS_MODE_personal_accident=mock
```

## Testing

### Test Quote Preview

```bash
curl -X POST http://localhost:8000/v1/products/personal_accident/quotes/preview \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "test-user-1",
    "sum_assured": 10000000,
    "date_of_birth": "1990-01-15",
    "gender": "Male",
    "policy_start_date": "2026-04-01"
  }'
```

### Test with Trace ID

```bash
curl -X POST http://localhost:8000/v1/products/personal_accident/quotes/preview \
  -H "Content-Type: application/json" \
  -H "X-Trace-ID: test-trace-123" \
  -d '{...}'
```

### Download Quote PDF

```bash
curl http://localhost:8000/v1/quotes/QT-ABC123/download \
  -o quote.pdf
```

## Migration from Old Endpoints

### Before (hardcoded benefits in flow)
```python
from src.chatbot.flows.personal_accident import PA_BENEFITS_BY_LEVEL
benefits = PA_BENEFITS_BY_LEVEL["10000000"]
```

### After (dynamic from config)
```python
from src.integrations.product_benefits import product_benefits_loader
benefits = product_benefits_loader.get_formatted_benefits("personal_accident", 10000000)
```

## Best Practices

1. **Idempotency**: Use `X-Idempotency-Key` header for POST requests (future enhancement)
2. **Trace IDs**: Always include `X-Trace-ID` for request tracking
3. **Validation**: Request schemas enforce validation, handle 400 errors
4. **Caching**: Quote/assessment results cached in-memory (replace with DB for production)
5. **PDF Storage**: PDFs stored in-memory (replace with S3/blob storage for production)

## Future Enhancements

- [ ] Persist quotes/assessments to database
- [ ] Add idempotency key support
- [ ] Store PDFs in S3/cloud storage
- [ ] Add webhook notifications for async underwriting
- [ ] Support quote versioning
- [ ] Add quote expiry background job
- [ ] Metrics and monitoring (Prometheus/Grafana)
