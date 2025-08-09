# Pro-ballers

One-file NFL Telegram Bot (2025-only, legal sources, no fake data).

## Usage

```
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN=...
export DATABASE_URL=postgresql+psycopg://user:pass@host:5432/dbname
python app.py migrate        # apply SQL migrations
python app.py bot            # run Telegram bot
python app.py refresh        # run collectors → features → model → picks once
```

### Commands
- `/player First Last` – show recent projections
- `/player First Last , rec , 7+` – probability of hitting a threshold
