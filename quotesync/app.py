"""
QuoteSync Web Application.

A local web app where you fill out a prospect profile and kick off
carrier portal automation.
"""

import asyncio
import json
import logging
import os
import threading
from pathlib import Path

from flask import Flask, flash, redirect, render_template, request, url_for

from quotesync.config import get_carrier_list, load_adapter
from quotesync.engine import run_carrier, load_profile
from quotesync.models.prospect import ProspectProfile

logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.urandom(24)

DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)


@app.route("/")
def index():
    """Home page — list saved prospects."""
    prospects = []
    for f in sorted(DATA_DIR.glob("*.json")):
        data = json.loads(f.read_text())
        prospects.append({
            "filename": f.stem,
            "name": data.get("legal_business_name", "Unnamed"),
            "risk_class": data.get("risk_class", ""),
        })
    return render_template("index.html", prospects=prospects)


@app.route("/prospect/new")
def new_prospect():
    """Blank prospect form."""
    profile = ProspectProfile()
    return render_template("prospect_form.html", profile=profile, is_new=True)


@app.route("/prospect/<filename>")
def edit_prospect(filename):
    """Edit an existing prospect."""
    filepath = DATA_DIR / f"{filename}.json"
    if not filepath.exists():
        flash("Prospect not found.", "error")
        return redirect(url_for("index"))
    data = json.loads(filepath.read_text())
    profile = ProspectProfile(**data)
    return render_template("prospect_form.html", profile=profile, is_new=False, filename=filename)


@app.route("/prospect/save", methods=["POST"])
def save_prospect():
    """Save prospect profile from form submission."""
    form = request.form.to_dict(flat=True)

    # Build nested structures from flat form fields
    profile_data = _parse_form_to_profile(form)
    profile = ProspectProfile(**profile_data)

    # Use business name as filename (sanitized)
    name = profile.legal_business_name or "unnamed"
    filename = "".join(c if c.isalnum() or c in " -_" else "" for c in name).strip().replace(" ", "_").lower()
    if not filename:
        filename = "unnamed"

    filepath = DATA_DIR / f"{filename}.json"
    filepath.write_text(profile.model_dump_json(indent=2))

    flash(f"Prospect '{profile.legal_business_name}' saved.", "success")
    return redirect(url_for("edit_prospect", filename=filename))


@app.route("/prospect/<filename>/delete", methods=["POST"])
def delete_prospect(filename):
    """Delete a saved prospect."""
    filepath = DATA_DIR / f"{filename}.json"
    if filepath.exists():
        filepath.unlink()
        flash("Prospect deleted.", "success")
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Run Quote
# ---------------------------------------------------------------------------

# In-memory store for quote run status (simple dict — no DB needed for local use)
_quote_runs: dict[str, dict] = {}


@app.route("/run")
def run_quote_select():
    """Select a prospect and carrier to run a quote."""
    prospects = []
    for f in sorted(DATA_DIR.glob("*.json")):
        data = json.loads(f.read_text())
        prospects.append({
            "filename": f.stem,
            "name": data.get("legal_business_name", "Unnamed"),
        })
    carriers = get_carrier_list()
    return render_template("run_quote.html", prospects=prospects, carriers=carriers, runs=_quote_runs)


@app.route("/run/start", methods=["POST"])
def run_quote_start():
    """Launch a carrier adapter in a background thread."""
    prospect_file = request.form.get("prospect")
    carrier_id = request.form.get("carrier")

    if not prospect_file or not carrier_id:
        flash("Please select both a prospect and a carrier.", "error")
        return redirect(url_for("run_quote_select"))

    filepath = DATA_DIR / f"{prospect_file}.json"
    if not filepath.exists():
        flash("Prospect file not found.", "error")
        return redirect(url_for("run_quote_select"))

    # Load adapter
    try:
        adapter = load_adapter(carrier_id)
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("run_quote_select"))

    # Load profile
    profile = load_profile(filepath)

    # Create a run ID
    run_id = f"{prospect_file}__{carrier_id}"
    _quote_runs[run_id] = {
        "prospect": prospect_file,
        "carrier": carrier_id,
        "carrier_name": adapter.name,
        "status": "running",
        "error": None,
    }

    # Launch in background thread
    def _run():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(run_carrier(adapter, profile, headed=True))
            _quote_runs[run_id]["status"] = "completed"
            logger.info("Quote run completed: %s", run_id)
        except Exception as e:
            _quote_runs[run_id]["status"] = "error"
            _quote_runs[run_id]["error"] = str(e)
            logger.error("Quote run failed: %s — %s", run_id, e)

    thread = threading.Thread(target=_run, daemon=True, name=f"quote-{run_id}")
    thread.start()

    flash(f"Started {adapter.name} quote for '{prospect_file}'. Browser will open shortly.", "success")
    return redirect(url_for("run_quote_select"))


@app.route("/run/<run_id>/clear", methods=["POST"])
def run_quote_clear(run_id):
    """Clear a completed/errored run from the status list."""
    _quote_runs.pop(run_id, None)
    return redirect(url_for("run_quote_select"))


def _parse_form_to_profile(form: dict) -> dict:
    """Convert flat form fields (dot-notation) into nested dict for Pydantic."""
    result = {}
    for key, value in form.items():
        if not value:
            continue
        parts = key.split(".")
        current = result
        for part in parts[:-1]:
            current = current.setdefault(part, {})
        # Convert types where needed
        final_key = parts[-1]
        current[final_key] = _coerce_value(final_key, value)
    return result


def _coerce_value(key: str, value: str):
    """Attempt to convert form string values to appropriate Python types."""
    if value.lower() in ("true", "yes"):
        return True
    if value.lower() in ("false", "no"):
        return False
    # Known integer fields
    int_fields = {
        "year_established", "pct_work_subcontracted", "num_employees_ft",
        "num_employees_pt", "num_owners_officers", "square_footage",
        "year_built", "number_of_stories", "roof_year_updated",
        "electrical_year_updated", "plumbing_year_updated", "hvac_year_updated",
        "number_of_units",
    }
    float_fields = {
        "gross_revenue_current", "gross_revenue_prior", "total_annual_payroll",
        "building_value", "bpp_value", "business_income_limit",
        "annual_gross_receipts", "largest_single_job_value",
        "amount_paid", "amount_reserved", "annual_premium",
    }
    if key in int_fields:
        try:
            return int(value)
        except ValueError:
            return value
    if key in float_fields:
        try:
            return float(value.replace(",", "").replace("$", ""))
        except ValueError:
            return value
    return value


def run():
    """Entry point to run the development server."""
    app.run(debug=True, port=5000)


if __name__ == "__main__":
    run()
