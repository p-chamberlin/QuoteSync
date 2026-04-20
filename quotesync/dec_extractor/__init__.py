"""
Dec Page Extractor — reads insurance declaration pages and extracts structured data.

Usage:
    python -m quotesync.dec_extractor path/to/dec_page.pdf
    python -m quotesync.dec_extractor path/to/dec_page.jpg --output prospect.json

The extracted data maps to ProspectProfile and can feed directly into QuoteSync.
"""

from .extract import extract_dec_page
from .prospect_mapper import to_prospect_profile

__all__ = ["extract_dec_page", "to_prospect_profile"]
