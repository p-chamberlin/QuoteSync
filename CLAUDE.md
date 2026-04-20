# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Install / first-time setup (Windows bash, venv already exists at `venv/`):

```bash
pip install -r requirements.txt
playwright install
```

Run the Flask web app (primary entry point — opens http://localhost:5000):

```bash
python -m quotesync.app
```

Run a carrier adapter from code (bypasses the UI — useful for iteration):

```python
import asyncio
from quotesync.engine import run_carrier, load_profile
from quotesync.config import load_adapter

profile = load_profile("data/<prospect>.json")
adapter = load_adapter("mmg_bop")   # or "acuity_bop"
asyncio.run(run_carrier(adapter, profile, headed=True, resume_from=None))
```

Run the dec-page extractor (Claude-vision PDF/image → ProspectProfile JSON):

```bash
python -m quotesync.dec_extractor path/to/dec_page.pdf --output data/my_prospect.json
python -m quotesync.dec_extractor path/to/dec_page.pdf --raw    # raw extraction, no mapping
```

Tests: `tests/` is present but empty — there is no test runner configured.

## Required environment

`.env` in project root (see `.env.example`). Each carrier needs `<KEY>_USERNAME` / `<KEY>_PASSWORD` where `<KEY>` matches the `env_key` in `CARRIER_REGISTRY`. `ANTHROPIC_API_KEY` is required for `dec_extractor`.

## Architecture

QuoteSync is a **single-user local tool** — no auth, no multi-tenancy, no scaling concerns. Simplicity wins over abstractions.

**Data flow:**

```
Prospect form (Flask) ──► data/<slug>.json  (Pydantic ProspectProfile)
                                    │
                                    ▼
        /run → engine.run_carrier() spawns a background thread
                                    │
                                    ▼
        Playwright persistent Chrome (profiles/<slug>/) ──► CarrierAdapter
                                                             .login()
                                                             .navigate_to_new_quote()
                                                             .fill_quote() → {"premium": "..."}
```

**Key modules:**

- `quotesync/models/prospect.py` — `ProspectProfile` is the **single source of truth**. Every carrier adapter maps from this model to the portal's specific fields. Sub-models cover GL, Property, and four risk classes (Contractor / Retail / Professional / Manufacturer). Changing a field here ripples into every adapter and the HTML form.
- `quotesync/engine.py` — Async Playwright runner. Opens a **persistent Chrome profile per carrier** under `profiles/<slug>/` so cookies/MFA survive between runs. On exception it leaves the browser open for 5 minutes for inspection rather than closing it.
- `quotesync/carriers/base.py` — `CarrierAdapter` ABC. Subclasses must implement `login`, `navigate_to_new_quote`, `fill_quote`. Optionally override `navigate_to_saved_quote` to support `resume_from`.
- `quotesync/config.py` — `CARRIER_REGISTRY` maps `carrier_id` → `adapter_class`. **Adding a new carrier requires registering it here** plus adding `<KEY>_USERNAME` / `<KEY>_PASSWORD` to `.env`.
- `quotesync/app.py` — Flask routes. `/run/start` launches `run_carrier` on a background thread with its own asyncio loop and stores progress in the in-memory `_quote_runs` dict.
- `quotesync/dec_extractor/` — standalone `python -m` CLI. Sends a PDF/image to Claude via the Anthropic SDK, parses JSON, maps via `prospect_mapper.to_prospect_profile` into a `ProspectProfile`.

**Login detection in the engine:** after `page.goto(login_url)` the engine waits for the OIDC redirect chain (`/auth/realms/`, `/signin-oidc`) to settle before checking whether the final URL contains `login`/`signin`/`sso`. Do not shorten these waits — a premature check during redirect will falsely trigger `adapter.login()` on an already-valid session.

**Session persistence:** `profiles/<slug>/` holds each carrier's Chrome user-data-dir and is gitignored. The "Clear session" button (`/session/<carrier_id>/clear`) deletes the directory to force a fresh login.

## Resume-from (development iteration pattern)

When iterating on a late step (e.g. Acuity's premium page), rerunning from step 1 wastes minutes. The adapter accepts `resume_from=<step_name>`:

- `engine.run_carrier` detects `resume_from` via `inspect.signature` and passes it only if the adapter's `fill_quote` accepts it.
- When `resume_from` is set, the engine calls `adapter.navigate_to_saved_quote(page)` instead of `navigate_to_new_quote`.
- Adapters that support resume keep a step registry (e.g. `acuity_bop.STEPS`) and persist the in-progress quote name to `data/.acuity_state.json` so the next run can navigate back to it.

## Carrier adapter notes

- Both live adapters (`mmg_bop.py`, `acuity_bop.py`) are long files dominated by portal-specific selector logic. Prefer small, localized edits over refactors — the selectors are paid for with manual portal inspection.
- MMG uses Angular custom dropdowns; Acuity iRating uses legacy Dojo/Dijit widgets. They need completely different interaction patterns. See the feedback memory `feedback_acuity_debugging.md` for the Dijit-specific pitfalls before touching Acuity.
- `example_carrier.py` is a template — selectors are placeholders, not a working adapter.
- Default to `headed=True` during development. The user watches the browser to spot regressions and intervene for MFA.

## Git workflow

- **Commit after each carrier step advances successfully.** Adapter steps (e.g. Acuity's `class_questions`, MMG's Step 4→5) are paid for with manual portal inspection; they're hard-won and easy to regress. Commit the moment a step advances to the next page end-to-end so we always have a working baseline to bisect back to.
- Keep commit messages scoped to one step or one bug fix — don't bundle unrelated changes.
- Still ask before committing and before pushing, per default git safety rules.
