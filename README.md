# Pro-ballers

One-file NFL Telegram Bot (2025-only, legal sources, no fake data).

## Usage

```
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN=...
# Optional: override default SQLite DB with PostgreSQL
# export DATABASE_URL=postgresql+psycopg://user:pass@host:5432/dbname
python app.py migrate        # apply SQL migrations
python app.py bot            # run Telegram bot
python app.py refresh        # run collectors → features → model → picks once
```

By default the application stores data in a local SQLite file named
`proballers.db`.  Setting `DATABASE_URL` allows pointing at a PostgreSQL
database instead.

The collectors use [cloudscraper](https://pypi.org/project/cloudscraper/)
so scraping protected sites continues to work without manual cookies.

### Commands
- `/player First Last` – show recent projections
- `/player First Last , rec , 7+` – probability of hitting a threshold
