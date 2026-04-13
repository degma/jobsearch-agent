# 🚀 Tosca Job Automation Agent

Automated job search + auto-apply system for QA / Test Automation roles (Tricentis Tosca focused).

## 🔧 Features

- Multi-source job scraping (RemoteOK, Greenhouse, Lever)
- AI-powered filtering using OpenAI
- Daily email alerts
- Auto-fill job applications (Playwright)
- Multi-CV selection

## ⚙️ Setup

```bash
git clone https://github.com/YOUR_USERNAME/tosca-job-agent.git
cd tosca-job-agent

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
python -m playwright install chromium
