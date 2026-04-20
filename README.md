# QuoteSync

AI-powered automation tool that fills commercial insurance carrier portals from a single prospect profile — built for independent agents.

## What It Does

Instead of manually entering the same prospect data across dozens of carrier portals, QuoteSync lets you:

1. **Fill out one form** — a master prospect profile covering GL, Commercial Property, and class-specific details
2. **Save prospect profiles** — stored locally as JSON, reusable across quotes
3. **Run carrier automations** — Playwright-based browser automation logs into each carrier portal and fills their forms using your saved profile

## Quick Start

### 1. Install Python dependencies

```bash
pip install -r requirements.txt
playwright install
```

### 2. Run the web app

```bash
python -m quotesync.app
```

Then open your browser to **http://localhost:5000**

### 3. Create a prospect profile

Click **"+ New Prospect"**, fill in the form, and save. The data is stored in the `data/` folder as JSON.

### 4. Run a carrier automation (coming soon)

Once carrier adapters are built for your specific portals, you'll run them against saved profiles. See `quotesync/carriers/example_carrier.py` for the template.

## Project Structure

```
QuoteSync/
├── quotesync/
│   ├── app.py                  # Flask web app (the form UI)
│   ├── engine.py               # Playwright automation runner
│   ├── models/
│   │   └── prospect.py         # Master prospect data model (Pydantic)
│   ├── carriers/
│   │   ├── base.py             # Base adapter class
│   │   └── example_carrier.py  # Template for building carrier adapters
│   ├── templates/              # HTML templates
│   └── static/css/             # Styles
├── data/                       # Saved prospect profiles (JSON)
├── tests/
└── requirements.txt
```

## Adding a New Carrier

1. Copy `quotesync/carriers/example_carrier.py`
2. Rename it (e.g., `hartford.py`)
3. Fill in the `login_url`, CSS selectors, and form-filling logic
4. Test with `headed=True` so you can watch the browser work

## Lines of Business

Currently covers:
- General Liability
- Commercial Property

With class-specific sections for:
- Contractors/Trades
- Retail/Habitational
- Professional Services
- Manufacturers/Wholesalers
