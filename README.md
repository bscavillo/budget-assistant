# Budget Assistant

A local, privacy-friendly personal-finance app powered by [Ollama](https://ollama.com).
Track income and expenses, set per-category monthly budgets, and get AI-generated
insights and answers about your spending — all running on your own machine. No
financial data leaves your computer.

## Features

- Add income/expense transactions with categories and descriptions
- Per-category monthly budgets with progress bars and overspend warnings
- Monthly summary: income, expenses, balance, spending breakdown
- **AI assistant** (local Ollama model):
  - Monthly insights and saving suggestions
  - Free-form Q&A about your finances
  - Automatic category suggestions for transactions

## Tech stack

- **Backend:** Python + [FastAPI](https://fastapi.tiangolo.com), SQLite, the
  official [`ollama`](https://github.com/ollama/ollama-python) library
- **Frontend:** vanilla HTML/CSS/JS (served by the backend, no build step)

## Prerequisites

- Python 3.11+
- [Ollama](https://ollama.com) installed and running with a model pulled, e.g.:
  ```sh
  ollama pull gemma4
  ```

## Setup

```sh
cd backend
python -m venv .venv
# Windows:  .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```

Then open <http://127.0.0.1:8000> in your browser.

### Choosing the AI model

The backend defaults to `gemma4:latest`. Override it with an environment variable:

```sh
# Windows (PowerShell)
$env:BUDGET_OLLAMA_MODEL = "llama3.2"
# macOS/Linux
export BUDGET_OLLAMA_MODEL=llama3.2
```

## Data & privacy

All data is stored locally in `backend/budget.db` (a SQLite file, git-ignored).
The AI runs locally via Ollama, so your financial data is never sent to any
external service.

## Importing from Postbank

Export your transactions from Postbank online banking as a CSV file, then use
the **Import from Postbank** form in the app. The parser is tolerant of format
variations: it auto-detects the encoding, delimiter, and columns by their
German header names (`Buchungstag`, `Betrag`, `Verwendungszweck`, ...), parses
German number/date formats, and skips rows that were already imported so
overlapping exports don't create duplicates.

If your export isn't recognized, the exact column names may differ from what the
parser expects -- adjust the keyword lists in `backend/postbank_import.py`.

## Roadmap

- [ ] Automatic bank sync via a PSD2 aggregator (e.g. GoCardless/Nordigen) for
      Postbank and other German banks
- [ ] Recurring transactions
- [ ] Charts / trends over multiple months
