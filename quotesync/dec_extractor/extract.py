"""
Claude-powered dec page extraction.

Sends a PDF or image to Claude's API and gets back structured insurance data.
Supports: PDF, JPEG, PNG, WebP, GIF.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

import anthropic

# ---------------------------------------------------------------------------
# Extraction prompt
# ---------------------------------------------------------------------------

_SYSTEM = """You are an expert insurance analyst. You will be given a declaration page (dec page) from an insurance policy.

Extract all available information and return it as a single JSON object. Use null for any field not found on the document. Do not guess or infer values that are not explicitly stated.

Return ONLY valid JSON — no markdown, no explanation, just the JSON object."""

_USER_PROMPT = """Extract all information from this declaration page and return it as JSON matching this schema exactly:

{
  "named_insured": "string or null",
  "dba": "string or null",
  "fein": "string or null",
  "entity_type": "string or null — e.g. LLC, Corporation, Individual, Partnership",
  "mailing_address": {
    "street": "string",
    "city": "string",
    "state": "2-letter abbreviation",
    "zip_code": "string"
  },
  "policy_number": "string or null",
  "carrier_name": "string or null",
  "agency_name": "string or null",
  "producer_name": "string or null",
  "effective_date": "YYYY-MM-DD or null",
  "expiration_date": "YYYY-MM-DD or null",
  "lines_of_coverage": [
    {
      "line": "BOP | GL | Property | WC | Auto | Umbrella | E&O | Other",
      "description": "string",
      "premium": "number or null"
    }
  ],
  "total_premium": "number or null",
  "sic_code": "string or null",
  "naics_code": "string or null",
  "operations_description": "string or null",
  "gl_limits": {
    "each_occurrence": "number or null",
    "general_aggregate": "number or null",
    "products_completed_ops_aggregate": "number or null",
    "personal_advertising_injury": "number or null",
    "medical_expense": "number or null",
    "fire_damage": "number or null"
  },
  "property_limits": {
    "building": "number or null",
    "bpp": "number or null — Business Personal Property",
    "business_income": "number or null",
    "deductible": "string or null"
  },
  "wc_limits": {
    "employers_liability_each_accident": "number or null",
    "employers_liability_disease_policy_limit": "number or null",
    "employers_liability_disease_each_employee": "number or null"
  },
  "locations": [
    {
      "street": "string",
      "city": "string",
      "state": "string",
      "zip_code": "string"
    }
  ],
  "annual_payroll": "number or null",
  "gross_receipts": "number or null",
  "num_employees": "number or null",
  "year_established": "number or null",
  "claims_history": [
    {
      "date": "YYYY-MM-DD or null",
      "description": "string",
      "amount": "number or null"
    }
  ],
  "raw_notes": "any additional details from the dec page that don't fit the schema above"
}"""


# ---------------------------------------------------------------------------
# Media type helpers
# ---------------------------------------------------------------------------

_IMAGE_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

_PDF_TYPE = "application/pdf"


def _read_as_base64(path: Path) -> tuple[str, str]:
    """Return (base64_data, media_type) for a PDF or image file."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        media_type = _PDF_TYPE
    elif suffix in _IMAGE_TYPES:
        media_type = _IMAGE_TYPES[suffix]
    else:
        raise ValueError(
            f"Unsupported file type '{suffix}'. Supported: PDF, JPEG, PNG, GIF, WebP"
        )
    data = base64.standard_b64encode(path.read_bytes()).decode("utf-8")
    return data, media_type


# ---------------------------------------------------------------------------
# Main extraction function
# ---------------------------------------------------------------------------

def extract_dec_page(
    file_path: str | Path,
    *,
    model: str = "claude-sonnet-4-6",
    api_key: str | None = None,
) -> dict[str, Any]:
    """
    Send a dec page (PDF or image) to Claude and return extracted data as a dict.

    Args:
        file_path: Path to the dec page file.
        model: Claude model to use. Defaults to claude-sonnet-4-6.
        api_key: Anthropic API key. Falls back to ANTHROPIC_API_KEY env var.

    Returns:
        Dict matching the extraction schema defined in _USER_PROMPT.

    Raises:
        ValueError: If the file type is unsupported.
        anthropic.APIError: On API failures.
        json.JSONDecodeError: If Claude returns malformed JSON (shouldn't happen).
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise EnvironmentError(
            "No Anthropic API key found. Set ANTHROPIC_API_KEY in your .env file."
        )

    client = anthropic.Anthropic(api_key=key)
    b64_data, media_type = _read_as_base64(path)

    # Build the content block — PDF and images use different block types
    if media_type == _PDF_TYPE:
        file_block: dict[str, Any] = {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": b64_data,
            },
        }
    else:
        file_block = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": b64_data,
            },
        }

    print(f"[DecExtractor] Sending {path.name} ({media_type}) to {model}...")

    response = client.messages.create(
        model=model,
        max_tokens=4096,
        system=_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": [
                    file_block,
                    {"type": "text", "text": _USER_PROMPT},
                ],
            }
        ],
    )

    raw_text = response.content[0].text.strip()

    # Strip markdown code fences if Claude wrapped the JSON anyway
    if raw_text.startswith("```"):
        lines = raw_text.splitlines()
        raw_text = "\n".join(
            line for line in lines if not line.startswith("```")
        ).strip()

    result = json.loads(raw_text)
    print(f"[DecExtractor] Extraction complete. Named insured: {result.get('named_insured')}")
    return result
