# SparkWash

Booking website for car wash services. Flask + SQLite backend, single `index.html` frontend.

## Local development

```bash
pip install -r requirements.txt
python app.py          # runs on http://localhost:5000
```

Data is saved to `./data/sparkwash.db` locally.

---

## Deploy to Fly.io (free tier)

Fly.io gives you persistent volumes on the free tier, so your bookings survive redeploys.

### First-time setup

```bash
# Install the Fly CLI (once)
curl -L https://fly.io/install.sh | sh

# Log in / sign up
fly auth login

# Create the app (run once from your project folder)
fly launch --no-deploy

# Create the persistent volume (1GB is plenty, free tier allows 3GB total)
fly volumes create sparkwash_data --size 1 --region arn

# Set your admin password as a secret (never hardcode in production)
fly secrets set ADMIN_PASSWORD=your-secure-password-here

# Deploy
fly deploy
```

### Subsequent deploys (after editing code)

```bash
fly deploy
```

That's it. Your `/data/sparkwash.db` lives on the volume and is never touched by redeploys.

### Useful commands

```bash
fly logs              # live logs
fly ssh console       # SSH into the running container
fly volumes list      # check your volume
```

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `PORT` | `8080` | HTTP port (set automatically by Fly) |
| `APP_DATA_DIR` | `./data` | Directory for db + CSV. Set to `/data` on Fly. |
| `APP_DB_FILE` | `$APP_DATA_DIR/sparkwash.db` | SQLite database path |
| `APP_CSV_FILE` | `$APP_DATA_DIR/bookings.csv` | CSV export path |
| `ADMIN_PASSWORD` | `admin123` | **Change this. Use `fly secrets set` in production.** |

---

## Migration from old JSON files

If you have existing data in `data/app-data.json` (or the old `slots.json` / `bookings.json`),
the app migrates it automatically on first startup — no action needed.

---

## Roadmap notes (future features)

### Email reminders
Add `Flask-Mail` or use the `resend` library (generous free tier).
Store customer email in the `bookings` table — just add an `email` column via:
```sql
ALTER TABLE bookings ADD COLUMN email TEXT NOT NULL DEFAULT '';
```
Schedule reminders with APScheduler (pure Python, no Redis needed).

### Login / auth
Add `Flask-Login` + a `users` table to the existing SQLite db.
No new infrastructure needed — same db, same volume.

### HTTPS
Fly.io handles TLS automatically — nothing to configure.
