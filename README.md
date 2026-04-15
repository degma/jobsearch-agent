# 🚀 Tosca Job Automation Agent

Automated job search + ranking system for QA / Test Automation roles (Tricentis Tosca focused), with email and Telegram delivery support.

## 🔧 Features

- Multi-source job scraping (RemoteOK, Greenhouse, Lever, LinkedIn guest, optional SerpAPI)
- AI-powered filtering and ranking using OpenAI
- Daily email digest support
- Telegram integration:
  - trigger a run with `/run`
  - receive job digest results directly in Telegram chat

## ⚙️ Setup

```bash
git clone https://github.com/YOUR_USERNAME/tosca-job-agent.git
cd tosca-job-agent

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
python -m playwright install chromium
```

Create `.env` next to `tosca_job_multi_agent.py`:

```dotenv
OPENAI_API_KEY=your_key_here
OPENAI_FILTER_MODEL=gpt-5-mini
OPENAI_EMAIL_MODEL=gpt-5

EMAIL_HOST=smtp.gmail.com
EMAIL_PORT=587
EMAIL_USER=your_email@gmail.com
EMAIL_PASSWORD=your_app_password
EMAIL_TO=your_email@gmail.com
EMAIL_FROM=your_email@gmail.com

MIN_SCORE=72
MAX_EMAIL_JOBS=25
REMOTE_ONLY=true
DAYS_LOOKBACK=21
ONLY_NEW_IN_EMAIL=false
SERPAPI_KEY=

LINKEDIN_ENABLED=true
LINKEDIN_LOCATION=Worldwide
LINKEDIN_QUERIES=Tricentis Tosca;Tosca Automation;SAP Test Automation Tosca

TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_MAX_JOBS=10
TELEGRAM_POLL_SECONDS=2
```

## ▶️ Run

Standard run (email enabled):

```bash
python tosca_job_multi_agent.py
```

Run once and send results to Telegram chat (without email):

```bash
python tosca_job_multi_agent.py --no-email --telegram-chat-id <chat_id>
```

Run Telegram polling bot (send `/run` in chat):

```bash
python tosca_job_multi_agent.py --telegram-bot
```

### Telegram Bot Notes

- Set `TELEGRAM_BOT_TOKEN` in `.env`.
- Optionally set `TELEGRAM_CHAT_ID` to restrict commands to one chat.
- In `--telegram-bot` mode:
  - `/run` starts a fresh job search and returns results in chat.
  - `/help` shows available commands.
