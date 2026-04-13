"""
Carrier credential and configuration management.

Loads credentials from a .env file in the project root.
Each carrier needs a USERNAME and PASSWORD env var.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root
_env_path = Path(__file__).parent.parent / ".env"
load_dotenv(_env_path)


@dataclass
class CarrierCredentials:
    username: str
    password: str

    @property
    def is_configured(self) -> bool:
        return bool(self.username and self.password)


def get_credentials(carrier_key: str) -> CarrierCredentials:
    """Get credentials for a carrier by its key (e.g. 'MMG', 'ACUITY')."""
    key = carrier_key.upper()
    return CarrierCredentials(
        username=os.getenv(f"{key}_USERNAME", ""),
        password=os.getenv(f"{key}_PASSWORD", ""),
    )


# Registry of all available carriers and their config keys.
# Add new carriers here as adapters are built.
CARRIER_REGISTRY: dict[str, dict] = {
    "mmg_bop": {
        "name": "MMG Insurance — BOP",
        "env_key": "MMG",
        "adapter_class": "quotesync.carriers.mmg_bop.MMGBopAdapter",
    },
    "acuity_bop": {
        "name": "Acuity Insurance — Bis-Pak (BOP)",
        "env_key": "ACUITY",
        "adapter_class": "quotesync.carriers.acuity_bop.AcuityBopAdapter",
    },
}


def get_carrier_list() -> list[dict]:
    """Return list of carriers with their configuration status."""
    from quotesync.engine import _session_path, SESSIONS_DIR
    import re

    carriers = []
    for carrier_id, info in CARRIER_REGISTRY.items():
        creds = get_credentials(info["env_key"])
        # Check if a saved session exists for this carrier
        slug = re.sub(r"[^a-z0-9]+", "_", info["name"].lower()).strip("_")
        session_file = SESSIONS_DIR / f"{slug}.json"
        carriers.append({
            "id": carrier_id,
            "name": info["name"],
            "configured": creds.is_configured,
            "has_session": session_file.exists(),
        })
    return carriers


def load_adapter(carrier_id: str):
    """Instantiate a carrier adapter with its credentials."""
    import importlib

    info = CARRIER_REGISTRY.get(carrier_id)
    if not info:
        raise ValueError(f"Unknown carrier: {carrier_id}")

    creds = get_credentials(info["env_key"])
    if not creds.is_configured:
        raise ValueError(
            f"Credentials not configured for {info['name']}. "
            f"Set {info['env_key']}_USERNAME and {info['env_key']}_PASSWORD in .env"
        )

    # Dynamic import: "quotesync.carriers.mmg_bop.MMGBopAdapter"
    module_path, class_name = info["adapter_class"].rsplit(".", 1)
    module = importlib.import_module(module_path)
    adapter_class = getattr(module, class_name)
    return adapter_class(username=creds.username, password=creds.password)
