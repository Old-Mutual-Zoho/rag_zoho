"""
Quote PDF generation service.

Generates downloadable PDF quotes for customers using reportlab.
Supports both preview (indicative) and final (binding) quotes.
"""

from __future__ import annotations

import io
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        SimpleDocTemplate,
        Paragraph,
        Spacer,
        Table,
        TableStyle,
    )
    from reportlab.lib.enums import TA_CENTER
    REPORTLAB_AVAILABLE = True
except ImportError:
    REPORTLAB_AVAILABLE = False
    logger.warning("reportlab not installed. PDF generation will not be available.")


class QuotePDFGenerator:
    """Generates PDF documents for insurance quotes."""

    def __init__(self):
        if not REPORTLAB_AVAILABLE:
            raise ImportError("reportlab is required for PDF generation. Install with: pip install reportlab")

    def generate_quote_pdf(
        self,
        quote_data: Dict[str, Any],
        output_path: Optional[Path] = None,
    ) -> bytes:
        """
        Generate a PDF for an insurance quote.

        Args:
            quote_data: Quote information (matches QuotePreviewResponse or FinalQuoteResponse)
            output_path: Optional file path to save PDF. If None, returns bytes only.

        Returns:
            PDF content as bytes
        """
        buffer = io.BytesIO()

        # Create the PDF document
        doc = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            rightMargin=0.75*inch,
            leftMargin=0.75*inch,
            topMargin=1*inch,
            bottomMargin=0.75*inch,
        )

        # Build the document content
        story = []
        styles = getSampleStyleSheet()

        # Custom styles
        title_style = ParagraphStyle(
            'CustomTitle',
            parent=styles['Heading1'],
            fontSize=24,
            textColor=colors.HexColor('#1a237e'),
            spaceAfter=30,
            alignment=TA_CENTER,
        )

        heading_style = ParagraphStyle(
            'CustomHeading',
            parent=styles['Heading2'],
            fontSize=14,
            textColor=colors.HexColor('#1a237e'),
            spaceAfter=12,
        )

        # Header
        company_name = Paragraph("Old Mutual Insurance", title_style)
        story.append(company_name)

        quote_type = quote_data.get("status", "preview").upper()
        if quote_type == "PREVIEW":
            quote_title = "INDICATIVE QUOTE"
        else:
            quote_title = "FINAL QUOTE"

        title = Paragraph(quote_title, heading_style)
        story.append(title)
        story.append(Spacer(1, 20))

        # Quote details table
        quote_details = [
            ["Quote ID:", quote_data.get("quote_id", "N/A")],
            ["Product:", quote_data.get("product_name", "N/A")],
            ["Date:", datetime.fromisoformat(quote_data.get("created_at", datetime.utcnow().isoformat())).strftime("%d %B %Y")],
        ]

        if quote_data.get("valid_until"):
            valid_until = datetime.fromisoformat(quote_data["valid_until"]).strftime("%d %B %Y")
            quote_details.append(["Valid Until:", valid_until])

        details_table = Table(quote_details, colWidths=[2*inch, 4*inch])
        details_table.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
            ('FONTNAME', (1, 0), (1, -1), 'Helvetica'),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ]))
        story.append(details_table)
        story.append(Spacer(1, 30))

        # Premium Summary
        story.append(Paragraph("Premium Summary", heading_style))

        premium = quote_data.get("premium", 0)
        currency = quote_data.get("currency", "UGX")
        frequency = quote_data.get("payment_frequency", "monthly")

        premium_data = [
            ["Coverage Amount:", f"{currency} {quote_data.get('sum_assured', 0):,.0f}"],
            [f"Premium ({frequency.title()}):", f"{currency} {premium:,.2f}"],
        ]

        breakdown = quote_data.get("breakdown", {})
        if isinstance(breakdown, dict):
            annual = breakdown.get("annual_equivalent") or breakdown.get("annual_total")
            if annual:
                premium_data.append(["Annual Premium:", f"{currency} {annual:,.2f}"])

        premium_table = Table(premium_data, colWidths=[3*inch, 3*inch])
        premium_table.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
            ('FONTNAME', (1, 0), (1, -1), 'Helvetica'),
            ('FONTSIZE', (0, 0), (-1, -1), 11),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f5f5f5')),
            ('BOX', (0, 0), (-1, -1), 1, colors.grey),
        ]))
        story.append(premium_table)
        story.append(Spacer(1, 30))

        # Benefits
        benefits = quote_data.get("benefits", [])
        if benefits:
            story.append(Paragraph("Benefits Included", heading_style))

            for benefit in benefits:
                if isinstance(benefit, dict):
                    desc = benefit.get("description", "")
                    amount = benefit.get("amount")
                    unit = benefit.get("unit", "")

                    if amount:
                        benefit_text = f"{desc}: {currency} {amount:,.0f}"
                        if unit:
                            benefit_text += f" {unit}"
                    else:
                        benefit_text = desc
                else:
                    benefit_text = str(benefit)

                story.append(Paragraph(f"• {benefit_text}", styles['BodyText']))

            story.append(Spacer(1, 20))

        # Exclusions
        exclusions = quote_data.get("exclusions", [])
        if exclusions:
            story.append(Paragraph("Standard Exclusions", heading_style))

            for exclusion in exclusions:
                story.append(Paragraph(f"• {exclusion}", styles['BodyText']))

            story.append(Spacer(1, 20))

        # Important notices
        assumptions = quote_data.get("assumptions", []) or quote_data.get("important_notes", [])
        if assumptions:
            story.append(Paragraph("Important Information", heading_style))

            for note in assumptions:
                story.append(Paragraph(f"• {note}", styles['BodyText']))

            story.append(Spacer(1, 20))

        # Footer
        if quote_type == "PREVIEW":
            footer_text = """
            <para align=center>
            <b>This is an indicative quote only and is not binding.</b><br/>
            Final premium may change after full underwriting assessment.<br/>
            Please provide complete and accurate information for final quotation.
            </para>
            """
        else:
            footer_text = """
            <para align=center>
            <b>This is a binding quote subject to payment and policy issuance.</b><br/>
            Terms and conditions apply. Please read the policy document carefully.
            </para>
            """

        story.append(Spacer(1, 30))
        story.append(Paragraph(footer_text, styles['BodyText']))

        # Build PDF
        doc.build(story)

        # Get PDF bytes
        pdf_bytes = buffer.getvalue()
        buffer.close()

        # Save to file if path provided
        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, 'wb') as f:
                f.write(pdf_bytes)
            logger.info(f"Saved quote PDF to {output_path}")

        return pdf_bytes


# Global instance (only if reportlab is available)
quote_pdf_generator = QuotePDFGenerator() if REPORTLAB_AVAILABLE else None


__all__ = ["QuotePDFGenerator", "quote_pdf_generator"]
