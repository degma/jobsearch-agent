#!/usr/bin/env python3
"""
Auto-apply assistant for job applications.

Design goals
------------
- Prefer direct ATS/company forms over brittle job-board automation.
- Never submit blindly: generate a reviewed application package first.
- Support a safe mode where the script fills forms and pauses before final submit.
- Reuse the job search output and recommend the best resume variant per job.

Dependencies
------------
pip install playwright python-dotenv openai requests
python -m playwright install chromium

Environment (.env)
------------------
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-5-mini
APPLY_MODE=review   # review | autofill | autosubmit
FULL_NAME=Martin Degraf
EMAIL=martin.degraf@gmail.com
PHONE=+12898854333
LOCATION=Oakville, Ontario, Canada
LINKEDIN_URL=
GITHUB_URL=https://github.com/degma
WORK_AUTH=Authorized to work in Canada
SPONSORSHIP=No
DEFAULT_SALARY_EXPECTATION=120000 CAD
DEFAULT_NOTICE_PERIOD=2 weeks
DATA_DIR=./data

How it works
------------
1. Reads jobs from data/last_run_filtered.json produced by the search agent.
2. Chooses jobs with supported apply links.
3. Uses an LLM to build a tailored application package:
   - best resume variant
   - short professional summary
   - optional custom cover note
   - answers for common screening questions
4. Uses Playwright to open the apply page, detect common form fields, and fill them.
5. In review mode, pauses before final submit so you can inspect and click submit yourself.
6. Stores a local application log to avoid duplicate applications.

Important note
--------------
Many job boards add CAPTCHAs, anti-bot checks, login walls, and terms that make full automation unreliable.
This assistant is intentionally strongest on direct company ATS flows and review-first automation.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI
from playwright.async_api import async_playwright, Page


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
JOBS_PATH = DATA_DIR / "last_run_filtered.json"
APPLICATION_LOG = DATA_DIR / "application_log.json"
MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")

load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

RESUME_VARIANTS = {
    "qe_lead": BASE_DIR / "Martin Degraf_QE Lead_2026.pdf",
    "general": BASE_DIR / "Resume Martin Degraf 2025.pdf",
    "coordinator": BASE_DIR / "Martin_Degraf_Resume_Senior_Test_Coordinator.pdf",
}

PROFILE = {
    "full_name": os.getenv("FULL_NAME", "Martin Degraf"),
    "email": os.getenv("EMAIL", "martin.degraf@gmail.com"),
    "phone": os.getenv("PHONE", "+12898854333"),
    "location": os.getenv("LOCATION", "Oakville, Ontario, Canada"),
    "linkedin_url": os.getenv("LINKEDIN_URL", ""),
    "github_url": os.getenv("GITHUB_URL", "https://github.com/degma"),
    "work_auth": os.getenv("WORK_AUTH", "Authorized to work in Canada"),
    "sponsorship": os.getenv("SPONSORSHIP", "No"),
    "salary_expectation": os.getenv("DEFAULT_SALARY_EXPECTATION", "120000 CAD"),
    "notice_period": os.getenv("DEFAULT_NOTICE_PERIOD", "2 weeks"),
}

SUPPORTED_DOMAINS = [
    "greenhouse.io",
    "ashbyhq.com",
    "lever.co",
    "workdayjobs.com",
    "myworkdayjobs.com",
]


@dataclass
class Job:
    title: str
    company: str
    location: str | None = None
    apply_url: str | None = None
    description: str | None = None
    salary: str | None = None
    work_model: str | None = None
    source: str | None = None
    score: int | None = None
    recommended_resume: str | None = None


@dataclass
class ApplicationPackage:
    resume_key: str
    professional_summary: str
    cover_note: str
    screening_answers: dict[str, str]


@dataclass
class ApplicationRecord:
    title: str
    company: str
    apply_url: str
    status: str
    resume_used: str


def load_jobs() -> list[Job]:
    if not JOBS_PATH.exists():
        return []
    payload = json.loads(JOBS_PATH.read_text(encoding="utf-8"))
    jobs: list[Job] = []
    for item in payload:
        jobs.append(Job(**{k: item.get(k) for k in Job.__dataclass_fields__.keys()}))
    return jobs


def load_application_log() -> list[dict[str, Any]]:
    if not APPLICATION_LOG.exists():
        return []
    return json.loads(APPLICATION_LOG.read_text(encoding="utf-8"))


def save_application_log(records: list[dict[str, Any]]) -> None:
    APPLICATION_LOG.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")


def already_applied(job: Job, records: list[dict[str, Any]]) -> bool:
    url = (job.apply_url or "").strip().lower()
    for rec in records:
        if (rec.get("apply_url") or "").strip().lower() == url:
            return True
    return False


def is_supported_apply_url(url: str | None) -> bool:
    if not url:
        return False
    u = url.lower()
    return any(domain in u for domain in SUPPORTED_DOMAINS)


def recommend_resume_key(job: Job) -> str:
    corpus = " ".join(filter(None, [job.title, job.description, job.work_model, job.location])).lower()
    qe = sum(term in corpus for term in ["tosca", "sap", "s/4hana", "automation lead", "architect", "quality engineering"])
    coord = sum(term in corpus for term in ["uat", "e2e", "coordinator", "banking", "crm", "integration"])
    gen = sum(term in corpus for term in ["qa", "regression", "agile", "api", "quality assurance"])
    scores = {"qe_lead": qe, "coordinator": coord, "general": gen}
    return max(scores, key=scores.get)


def build_application_package(job: Job) -> ApplicationPackage:
    resume_key = recommend_resume_key(job)
    prompt = f"""
You are preparing a professional job application package for this role.
Return strict JSON with keys:
- professional_summary
- cover_note
- screening_answers (object with concise answers for: work_auth, sponsorship, salary_expectation, notice_period, location, remote_preference)

Candidate profile:
{json.dumps(PROFILE, ensure_ascii=False)}

Job:
{json.dumps(asdict(job), ensure_ascii=False)}

Rules:
- Keep cover_note under 140 words.
- Keep professional_summary under 90 words.
- Be factual and conservative.
- Emphasize Tricentis Tosca, QA automation leadership, SAP, API/integration testing when relevant.
"""
    response = client.responses.create(model=MODEL, input=prompt)
    text = response.output_text
    try:
        data = json.loads(_extract_json(text))
    except Exception:
        data = {
            "professional_summary": "Senior QA automation leader with strong Tricentis Tosca, SAP, API, and enterprise integration testing experience.",
            "cover_note": "I am interested in this role because it aligns closely with my experience leading QA automation initiatives with Tricentis Tosca in enterprise environments.",
            "screening_answers": {
                "work_auth": PROFILE["work_auth"],
                "sponsorship": PROFILE["sponsorship"],
                "salary_expectation": PROFILE["salary_expectation"],
                "notice_period": PROFILE["notice_period"],
                "location": PROFILE["location"],
                "remote_preference": "Remote",
            },
        }
    return ApplicationPackage(
        resume_key=resume_key,
        professional_summary=data.get("professional_summary", ""),
        cover_note=data.get("cover_note", ""),
        screening_answers=data.get("screening_answers", {}),
    )


async def fill_common_fields(page: Page, job: Job, pkg: ApplicationPackage) -> None:
    fields = {
        r"first.*name": PROFILE["full_name"].split()[0],
        r"last.*name": PROFILE["full_name"].split()[-1],
        r"full.*name|name": PROFILE["full_name"],
        r"email": PROFILE["email"],
        r"phone|mobile": PROFILE["phone"],
        r"location|city": PROFILE["location"],
        r"linkedin": PROFILE["linkedin_url"],
        r"github": PROFILE["github_url"],
        r"summary|about|professional summary": pkg.professional_summary,
        r"cover letter|why are you interested|additional information": pkg.cover_note,
        r"salary": pkg.screening_answers.get("salary_expectation", PROFILE["salary_expectation"]),
        r"notice": pkg.screening_answers.get("notice_period", PROFILE["notice_period"]),
        r"work authorization|authorized": pkg.screening_answers.get("work_auth", PROFILE["work_auth"]),
        r"sponsorship": pkg.screening_answers.get("sponsorship", PROFILE["sponsorship"]),
    }

    inputs = await page.locator("input, textarea, select").all()
    for element in inputs:
        try:
            name = ((await element.get_attribute("name")) or "") + " " + ((await element.get_attribute("aria-label")) or "") + " " + ((await element.get_attribute("placeholder")) or "")
            lname = name.lower()
            for pattern, value in fields.items():
                if re.search(pattern, lname):
                    tag = await element.evaluate("el => el.tagName.toLowerCase()")
                    if tag == "select":
                        try:
                            await element.select_option(label=value)
                        except Exception:
                            pass
                    else:
                        await element.fill(str(value))
                    break
        except Exception:
            continue


async def upload_resume_if_present(page: Page, pkg: ApplicationPackage) -> str:
    resume_path = RESUME_VARIANTS[pkg.resume_key]
    file_inputs = await page.locator('input[type="file"]').all()
    for fi in file_inputs:
        try:
            await fi.set_input_files(str(resume_path))
            return str(resume_path)
        except Exception:
            continue
    return str(resume_path)


async def apply_to_job(page: Page, job: Job, mode: str) -> ApplicationRecord:
    await page.goto(job.apply_url, wait_until="domcontentloaded", timeout=60000)
    pkg = build_application_package(job)
    resume_used = await upload_resume_if_present(page, pkg)
    await fill_common_fields(page, job, pkg)

    status = "review_ready"
    if mode == "autosubmit":
        buttons = page.locator("button, input[type='submit']")
        count = await buttons.count()
        for i in range(count):
            b = buttons.nth(i)
            try:
                txt = ((await b.text_content()) or "").lower()
                if any(x in txt for x in ["submit", "apply", "send application"]):
                    await b.click()
                    status = "submitted"
                    break
            except Exception:
                continue
    elif mode == "autofill":
        status = "autofilled"

    return ApplicationRecord(
        title=job.title,
        company=job.company,
        apply_url=job.apply_url or "",
        status=status,
        resume_used=resume_used,
    )


async def run() -> None:
    mode = os.getenv("APPLY_MODE", "review").strip().lower()
    jobs = load_jobs()
    records = load_application_log()

    targets = [
        j for j in jobs
        if (j.apply_url and is_supported_apply_url(j.apply_url) and not already_applied(j, records))
    ]

    if not targets:
        print("No supported unapplied jobs found.")
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        new_records = list(records)
        for job in targets[:5]:
            try:
                result = await apply_to_job(page, job, mode)
                new_records.append(asdict(result))
                print(f"{result.status}: {result.company} - {result.title}")
                if mode == "review":
                    print("Review mode active. Inspect the page and submit manually if everything looks correct.")
                    await page.wait_for_timeout(15000)
            except Exception as exc:
                new_records.append(asdict(ApplicationRecord(
                    title=job.title,
                    company=job.company,
                    apply_url=job.apply_url or "",
                    status=f"error: {exc}",
                    resume_used="",
                )))

        save_application_log(new_records)
        await browser.close()


def _extract_json(text: str) -> str:
    text = text.strip()
    if text.startswith("{") and text.endswith("}"):
        return text
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start:end + 1]
    raise ValueError("No JSON object found")


if __name__ == "__main__":
    asyncio.run(run())
