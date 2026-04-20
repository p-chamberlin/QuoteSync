"""
CLI entry point for the dec page extractor.

Usage:
    # Activate your venv first, then:
    python -m quotesync.dec_extractor path/to/dec_page.pdf
    python -m quotesync.dec_extractor path/to/dec_page.pdf --output my_prospect.json
    python -m quotesync.dec_extractor path/to/dec_page.pdf --raw   # print raw extraction only
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Load .env before anything else so ANTHROPIC_API_KEY is available
from dotenv import load_dotenv

load_dotenv()

from .extract import extract_dec_page
from .prospect_mapper import to_prospect_profile


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m quotesync.dec_extractor",
        description="Extract insurance data from a declaration page PDF or image.",
    )
    parser.add_argument(
        "file",
        type=Path,
        help="Path to the dec page (PDF, JPEG, PNG, WebP, GIF)",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="Save the ProspectProfile JSON to this file (optional)",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Print the raw extraction JSON instead of the ProspectProfile",
    )
    parser.add_argument(
        "--model",
        default="claude-sonnet-4-6",
        help="Claude model to use (default: claude-sonnet-4-6)",
    )
    args = parser.parse_args(argv)

    if not args.file.exists():
        print(f"Error: file not found: {args.file}", file=sys.stderr)
        sys.exit(1)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print(
            "Error: ANTHROPIC_API_KEY not set. Add it to your .env file:\n"
            "  ANTHROPIC_API_KEY=sk-ant-...",
            file=sys.stderr,
        )
        sys.exit(1)

    # Run extraction
    extracted = extract_dec_page(args.file, model=args.model, api_key=api_key)

    if args.raw:
        print(json.dumps(extracted, indent=2, default=str))
        return

    # Map to ProspectProfile
    profile = to_prospect_profile(extracted)
    profile_json = profile.model_dump(mode="json")

    print("\n" + "=" * 60)
    print("EXTRACTED PROSPECT PROFILE")
    print("=" * 60)
    print(f"  Named Insured : {profile.legal_business_name}")
    print(f"  Entity Type   : {profile.entity_type}")
    print(f"  Address       : {profile.mailing_address.street}, {profile.mailing_address.city}, {profile.mailing_address.state} {profile.mailing_address.zip_code}")
    print(f"  FEIN          : {profile.fein or '—'}")
    print(f"  Effective Date: {profile.effective_date or '—'}")
    print(f"  Current Carrier: {profile.current_carrier or '—'}")
    print(f"  GL Limits     : {profile.gl.desired_limits}")
    if profile.property.building_value:
        print(f"  Building Value: ${profile.property.building_value:,.0f}")
    if profile.property.bpp_value:
        print(f"  BPP Value     : ${profile.property.bpp_value:,.0f}")
    print(f"  Operations    : {profile.operations_description[:80] + '...' if len(profile.operations_description) > 80 else profile.operations_description or '—'}")
    print("=" * 60)

    if args.output:
        args.output.write_text(json.dumps(profile_json, indent=2, default=str))
        print(f"\nSaved to: {args.output}")
        print("Load into QuoteSync: copy this file to data/<name>.json")
    else:
        print("\nFull JSON:")
        print(json.dumps(profile_json, indent=2, default=str))


if __name__ == "__main__":
    main()
