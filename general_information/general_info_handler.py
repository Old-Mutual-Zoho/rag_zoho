"""Generate per-product general information JSON files from processed scraped data."""

from __future__ import annotations

import argparse
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("general_info_handler")

ELIGIBILITY_FALLBACK = "Please contact Old Mutual Uganda for detailed eligibility and application requirements."


def _normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip()


def _slugify(value: str) -> str:
    cleaned = _normalize_whitespace(value).lower()
    cleaned = re.sub(r"[^a-z0-9]+", "-", cleaned)
    return cleaned.strip("-") or "unknown-product"


def _contains_any(text: str, keywords: Sequence[str]) -> bool:
    lowered = (text or "").lower()
    return any(keyword in lowered for keyword in keywords)


def _iter_product_documents(input_path: Path) -> Iterable[Dict[str, Any]]:
    with input_path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                doc = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Skipping malformed JSON line %s in %s", line_number, input_path)
                continue
            if not isinstance(doc, dict):
                continue
            if doc.get("type") != "product":
                continue
            yield doc


def _extract_sections(doc: Dict[str, Any]) -> List[Dict[str, str]]:
    sections = doc.get("sections") or []
    if not isinstance(sections, list):
        return []
    result: List[Dict[str, str]] = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        heading = _normalize_whitespace(str(section.get("heading") or ""))
        content = _normalize_whitespace(str(section.get("content") or ""))
        if heading or content:
            result.append({"heading": heading, "content": content})
    return result


def _extract_faqs(doc: Dict[str, Any]) -> List[Dict[str, str]]:
    faqs = doc.get("faqs") or []
    if not isinstance(faqs, list):
        return []
    result: List[Dict[str, str]] = []
    for faq in faqs:
        if not isinstance(faq, dict):
            continue
        question = _normalize_whitespace(str(faq.get("question") or ""))
        answer = _normalize_whitespace(str(faq.get("answer") or ""))
        if question or answer:
            result.append({"question": question, "answer": answer})
    return result


def _pick_title(doc: Dict[str, Any]) -> str:
    return _normalize_whitespace(str(doc.get("title") or doc.get("product_name") or ""))


def _extract_definition(doc: Dict[str, Any], title: str) -> str:
    sections = _extract_sections(doc)
    if sections:
        preferred_terms = ("what is", "overview", "about")
        avoid_terms = (
            "how do i apply",
            "first time customers",
            "flexipay",
            "non flexipay",
            "banking options",
            "how does it work",
            "requirements",
        )

        scored_sections: List[tuple[int, int, str]] = []
        for index, section in enumerate(sections):
            heading = section["heading"].lower()
            content = section["content"]
            if not content:
                continue
            score = 0
            if _contains_any(heading, preferred_terms):
                score += 5
            if _contains_any(heading, avoid_terms):
                score -= 2
            score += min(len(content) // 100, 4)
            scored_sections.append((score, -index, content))

        if scored_sections:
            scored_sections.sort(reverse=True)
            return _normalize_whitespace(scored_sections[0][2])

    faqs = _extract_faqs(doc)
    for faq in faqs:
        if faq["answer"]:
            return faq["answer"]

    if title:
        return f"{title} is an Old Mutual product."
    return "Old Mutual product information."


def _extract_benefits(doc: Dict[str, Any], definition: str) -> List[str]:
    benefits: List[str] = []
    sections = _extract_sections(doc)
    faqs = _extract_faqs(doc)

    section_terms = ("benefit", "what's in it", "whats in it", "value", "feature", "included")
    for section in sections:
        if _contains_any(section["heading"], section_terms) and section["content"]:
            benefits.append(section["content"])

    faq_terms = ("benefit", "included", "value")
    for faq in faqs:
        if _contains_any(faq["question"], faq_terms) and faq["answer"]:
            benefits.append(faq["answer"])

    deduped: List[str] = []
    seen: set[str] = set()
    for benefit in benefits:
        normalized = _normalize_whitespace(benefit)
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(normalized)

    if deduped:
        return deduped
    return [_normalize_whitespace(definition)]


def _extract_eligibility(doc: Dict[str, Any]) -> str:
    sections = _extract_sections(doc)
    faqs = _extract_faqs(doc)

    section_terms = ("who is this for", "who can", "eligible", "requirements")
    for section in sections:
        if _contains_any(section["heading"], section_terms) and section["content"]:
            return section["content"]

    faq_terms = ("who can", "eligible", "requirements", "get the cover")
    for faq in faqs:
        if _contains_any(faq["question"], faq_terms) and faq["answer"]:
            return faq["answer"]

    return ELIGIBILITY_FALLBACK


def _build_general_info(doc: Dict[str, Any]) -> Dict[str, Any]:
    title = _pick_title(doc)
    product_id = _normalize_whitespace(str(doc.get("product_id") or "")) or _slugify(title)
    definition = _extract_definition(doc, title)
    benefits = _extract_benefits(doc, definition)
    eligibility = _extract_eligibility(doc)
    source_url = _normalize_whitespace(str(doc.get("url") or ""))

    return {
        "product_id": product_id,
        "title": title or product_id,
        "definition": definition,
        "benefits": benefits,
        "eligibility": eligibility,
        "source_url": source_url,
    }


def generate_general_info_files(
    input_path: Path,
    output_dir: Path,
    overwrite: bool = False,
) -> Dict[str, int]:
    output_dir.mkdir(parents=True, exist_ok=True)
    created = 0
    skipped_existing = 0
    skipped_duplicates = 0
    seen_product_ids: set[str] = set()

    for doc in _iter_product_documents(input_path):
        info = _build_general_info(doc)
        product_id = str(info["product_id"])
        if product_id in seen_product_ids:
            skipped_duplicates += 1
            continue
        seen_product_ids.add(product_id)

        output_path = output_dir / f"{product_id}.json"
        if output_path.exists() and not overwrite:
            skipped_existing += 1
            continue

        with output_path.open("w", encoding="utf-8") as handle:
            json.dump(info, handle, indent=2, ensure_ascii=False)
        created += 1

    return {
        "created": created,
        "skipped_existing": skipped_existing,
        "skipped_duplicates": skipped_duplicates,
        "seen_products": len(seen_product_ids),
    }


def _default_paths() -> tuple[Path, Path]:
    project_root = Path(__file__).resolve().parents[1]
    input_path = project_root / "data" / "processed" / "website_documents.jsonl"
    output_dir = project_root / "general_information" / "product_json"
    return input_path, output_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate product general information JSON files.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing files in product_json.")
    args = parser.parse_args()

    input_path, output_dir = _default_paths()
    if not input_path.exists():
        raise FileNotFoundError(f"Processed dataset not found: {input_path}")

    summary = generate_general_info_files(input_path=input_path, output_dir=output_dir, overwrite=args.overwrite)
    logger.info(
        "General info generation completed. created=%s skipped_existing=%s skipped_duplicates=%s seen_products=%s",
        summary["created"],
        summary["skipped_existing"],
        summary["skipped_duplicates"],
        summary["seen_products"],
    )


if __name__ == "__main__":
    main()