#!/usr/bin/env python3
"""
Optimized multi-agent remote job finder for Tricentis Tosca roles.
Designed for Raspberry Pi, with low cost, stable sources, and better ranking.

Key improvements in this optimized version
-----------------------------------------
1) Cost optimization
   - Uses a cheaper model for filtering/ranking
   - Uses a stronger model only for final email generation
   - Skips LLM calls entirely when there are too few or obviously irrelevant jobs

2) Better source coverage
   - Greenhouse company boards
   - Lever company boards
   - RemoteOK feed
   - Optional SerpAPI job search integration (recommended if you want wider coverage)

3) Better relevance
   - Stronger local prefilter before LLM
   - Better deduplication
   - Remote-first and recent-first ranking
   - New-vs-seen awareness

4) Better maintainability
   - Cleaner configuration
   - Safer JSON parsing
   - More structured ranking pipeline
   - Easy company/source extension

5) Raspberry Pi friendly
   - Minimal dependencies
   - No browser automation by default
   - Good fit for cron

Setup
-----
python3 -m venv .venv
source .venv/bin/activate
pip install -U openai requests python-dotenv

Create .env next to this script:
OPENAI_API_KEY=your_key_here
OPENAI_FILTER_MODEL=gpt-5-mini
OPENAI_EMAIL_MODEL=gpt-5

EMAIL_HOST=smtp.gmail.com
EMAIL_PORT=587
EMAIL_USER=your_email@gmail.com
EMAIL_PASSWORD=your_app_password
EMAIL_TO=your_email@gmail.com
EMAIL_FROM=your_email@gmail.com
TIMEZONE=America/Toronto

MIN_SCORE=72
MAX_EMAIL_JOBS=25
REMOTE_ONLY=true
DAYS_LOOKBACK=21
ONLY_NEW_IN_EMAIL=false
SERPAPI_KEY=

Run once:
python tosca_job_multi_agent.py

Cron example:
0 7 * * * cd /home/pi/tosca-jobs && /home/pi/tosca-jobs/.venv/bin/python /home/pi/tosca-jobs/tosca_job_multi_agent.py >> /home/pi/tosca-jobs/job_agent.log 2>&1
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import html
import json
import logging
import os
import re
import smtplib
import ssl
import sys
import time
import argparse
from urllib.parse import urljoin
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Iterable

import requests
from dotenv import load_dotenv
from openai import OpenAI


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
HISTORY_PATH = DATA_DIR / "seen_jobs.json"
RAW_DUMP_PATH = DATA_DIR / "last_run_raw.json"
FILTERED_DUMP_PATH = DATA_DIR / "last_run_filtered.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("tosca-job-agent")

HTTP_TIMEOUT = 30
USER_AGENT = "tosca-job-agent/2.0 (+raspberry-pi)"

TARGET_KEYWORDS = [
    "tricentis tosca",
    "tosca",
    "tosca automation",
    "qa automation",
    "test automation",
    "automation architect",
    "automation lead",
    "test lead",
    "sap",
    "sap s/4hana",
    "api testing",
    "enterprise testing",
    "regression",
    "quality engineering",
]

HIGH_VALUE_TERMS = [
    "tricentis tosca",
    "tosca",
    "sap",
    "qa automation lead",
    "test automation architect",
    "automation lead",
    "sdet",
    "quality engineer",
]

NEGATIVE_HINTS = [
    "sales",
    "account executive",
    "marketing",
    "recruiter",
    "designer",
    "customer success",
    "product designer",
    "mechanical",
    "civil engineer",
]

CANADA_LOCATION_HINTS = [
    "canada",
    "canadian",
    "ontario",
    "quebec",
    "british columbia",
    "alberta",
    "manitoba",
    "saskatchewan",
    "nova scotia",
    "new brunswick",
    "newfoundland",
    "prince edward island",
    "pei",
    "yukon",
    "northwest territories",
    "nunavut",
    "toronto",
    "vancouver",
    "montreal",
    "calgary",
    "ottawa",
    "edmonton",
    "waterloo",
]

COMPANY_SOURCES: dict[str, list[dict[str, str]]] = {
    "greenhouse": [
        {"company": "GitLab", "board": "gitlab"},
        {"company": "Remote", "board": "remotecom"},
        {"company": "Samsara", "board": "samsara"},
    ],
    "lever": [],
}


@dataclasses.dataclass
class Job:
    title: str
    company: str
    location: str | None
    work_model: str | None
    salary: str | None
    apply_url: str
    source: str
    posted_at: str | None = None
    description: str | None = None
    skills_summary: str | None = None
    score: int | None = None
    relevance_reason: str | None = None
    is_new: bool = True
    age_days: int | None = None

    def stable_id(self) -> str:
        base = "|".join([
            normalize_text(self.title),
            normalize_text(self.company),
            normalize_url(self.apply_url),
        ])
        return hashlib.sha256(base.encode("utf-8")).hexdigest()

    def dedupe_key(self) -> str:
        base = "|".join([
            normalize_text(self.title),
            normalize_text(self.company),
        ])
        return hashlib.sha256(base.encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class Config:
    def __init__(self) -> None:
        self.filter_model = os.getenv("OPENAI_FILTER_MODEL", "gpt-5-mini")
        self.email_model = os.getenv("OPENAI_EMAIL_MODEL", "gpt-5")
        self.min_score = int(os.getenv("MIN_SCORE", "72"))
        self.max_email_jobs = int(os.getenv("MAX_EMAIL_JOBS", "25"))
        self.remote_only = get_env_bool("REMOTE_ONLY", True)
        self.only_new_in_email = get_env_bool("ONLY_NEW_IN_EMAIL", False)
        self.days_lookback = int(os.getenv("DAYS_LOOKBACK", "21"))
        self.serpapi_key = os.getenv("SERPAPI_KEY", "").strip()
        self.linkedin_enabled = get_env_bool("LINKEDIN_ENABLED", True)
        self.linkedin_location = os.getenv("LINKEDIN_LOCATION", "Worldwide")
        raw_queries = os.getenv("LINKEDIN_QUERIES", "").strip()
        self.linkedin_queries = [q.strip() for q in raw_queries.split(";") if q.strip()] if raw_queries else [
            "Tricentis Tosca",
            "Tosca Automation",
            "SAP Test Automation Tosca",
            "QA Automation Lead Tosca",
        ]
        self.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        self.telegram_max_jobs = int(os.getenv("TELEGRAM_MAX_JOBS", "10"))
        self.telegram_poll_seconds = int(os.getenv("TELEGRAM_POLL_SECONDS", "2"))


def get_env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def now_local() -> dt.datetime:
    return dt.datetime.now()


def normalize_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    url = re.sub(r"^http://", "https://", url)
    return re.sub(r"/$", "", url)


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Failed to read %s", path)
        return default


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def clean_text(text: str | None, limit: int = 900) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def infer_work_model(text: str | None) -> str | None:
    t = normalize_text(text)
    if "remote" in t:
        return "Remote"
    if "hybrid" in t:
        return "Hybrid"
    if "onsite" in t or "on-site" in t or "on site" in t:
        return "Onsite"
    return None


def extract_salary(text: str | None) -> str | None:
    if not text:
        return None
    patterns = [
        r"(?:CAD|USD|CA\$|US\$|\$)\s?\d{2,3}(?:[,\.]\d{3})*(?:\s?(?:-|–|to)\s?(?:CAD|USD|CA\$|US\$|\$)?\s?\d{2,3}(?:[,\.]\d{3})*)?(?:\s?(?:per year|yearly|/year))?",
        r"\d{2,3}k\s?(?:-|–|to)\s?\d{2,3}k",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(0).strip()
    return None


def parse_date_to_age_days(value: str | int | None) -> int | None:
    if value is None:
        return None
    now = now_local()
    try:
        if isinstance(value, int):
            ts = dt.datetime.fromtimestamp(value)
            return max(0, (now - ts).days)
        s = str(value).strip()
        if s.isdigit():
            ts_int = int(s)
            if ts_int > 10_000_000_000:
                ts = dt.datetime.fromtimestamp(ts_int / 1000)
            else:
                ts = dt.datetime.fromtimestamp(ts_int)
            return max(0, (now - ts).days)
        s = s.replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(s)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return max(0, (now - parsed).days)
    except Exception:
        return None


def local_keyword_score(job: Job) -> int:
    corpus = " ".join(filter(None, [
        job.title,
        job.company,
        job.location,
        job.work_model,
        job.description,
        job.skills_summary,
    ])).lower()

    score = 0
    for term in TARGET_KEYWORDS:
        if term in corpus:
            score += 8
    for term in HIGH_VALUE_TERMS:
        if term in corpus:
            score += 12
    for bad in NEGATIVE_HINTS:
        if bad in corpus:
            score -= 25

    if "tricentis tosca" in corpus:
        score += 18
    elif re.search(r"\btosca\b", corpus):
        score += 8

    if "sap" in corpus:
        score += 8
    if job.work_model and job.work_model.lower() == "remote":
        score += 8
    if job.age_days is not None:
        if job.age_days <= 7:
            score += 10
        elif job.age_days <= 14:
            score += 6
        elif job.age_days <= 21:
            score += 2
        else:
            score -= 8

    return max(0, min(100, score))


class Fetcher:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    def fetch(self) -> list[Job]:
        raise NotImplementedError


class RemoteOKFetcher(Fetcher):
    url = "https://remoteok.com/api"

    def fetch(self) -> list[Job]:
        logger.info("Fetching RemoteOK jobs")
        jobs: list[Job] = []
        try:
            response = self.session.get(self.url, timeout=HTTP_TIMEOUT)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                return jobs
            for item in payload:
                if not isinstance(item, dict):
                    continue
                title = item.get("position") or item.get("title") or ""
                tags = item.get("tags", []) if isinstance(item.get("tags"), list) else []
                description = clean_text(item.get("description") or "")
                text_blob = " ".join([title, description, " ".join(tags)])
                if not any(term in text_blob.lower() for term in ["tosca", "qa", "test automation", "sap", "quality engineer"]):
                    continue
                job = Job(
                    title=title,
                    company=item.get("company") or "Unknown",
                    location=item.get("location") or "Remote",
                    work_model="Remote",
                    salary=(item.get("salary_min") and item.get("salary_max") and f"{item.get('salary_min')}-{item.get('salary_max')}") or item.get("salary") or None,
                    apply_url=item.get("apply_url") or item.get("url") or "",
                    source="RemoteOK",
                    posted_at=item.get("date") or item.get("epoch"),
                    description=description,
                    skills_summary=", ".join(tags[:8]) if tags else None,
                )
                job.age_days = parse_date_to_age_days(job.posted_at)
                if job.apply_url:
                    jobs.append(job)
        except Exception:
            logger.exception("RemoteOK fetch failed")
        return jobs


class GreenhouseFetcher(Fetcher):
    def fetch(self) -> list[Job]:
        jobs: list[Job] = []
        for source in COMPANY_SOURCES["greenhouse"]:
            company = source["company"]
            board = source["board"]
            url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs"
            logger.info("Fetching Greenhouse board %s", company)
            try:
                response = self.session.get(url, timeout=HTTP_TIMEOUT)
                response.raise_for_status()
                payload = response.json()
                for item in payload.get("jobs", []):
                    title = item.get("title") or ""
                    location = (item.get("location") or {}).get("name") if isinstance(item.get("location"), dict) else None
                    metadata_parts: list[str] = []
                    for md in item.get("metadata", []) or []:
                        if isinstance(md, dict) and md.get("value"):
                            metadata_parts.append(str(md["value"]))
                    text_blob = " ".join([title, location or "", " ".join(metadata_parts)])
                    if not any(term in text_blob.lower() for term in ["tosca", "qa", "test", "sap", "quality"]):
                        continue
                    job = Job(
                        title=title,
                        company=company,
                        location=location,
                        work_model=infer_work_model(text_blob),
                        salary=extract_salary(text_blob),
                        apply_url=item.get("absolute_url") or "",
                        source=f"Greenhouse:{company}",
                        posted_at=item.get("updated_at") or item.get("first_published"),
                        description=clean_text(text_blob),
                    )
                    job.age_days = parse_date_to_age_days(job.posted_at)
                    if job.apply_url:
                        jobs.append(job)
            except Exception:
                logger.exception("Greenhouse fetch failed for %s", company)
        return jobs


class LeverFetcher(Fetcher):
    def fetch(self) -> list[Job]:
        jobs: list[Job] = []
        for source in COMPANY_SOURCES["lever"]:
            company = source["company"]
            company_id = source["company_id"]
            url = f"https://api.lever.co/v0/postings/{company_id}?mode=json"
            logger.info("Fetching Lever postings %s", company)
            try:
                response = self.session.get(url, timeout=HTTP_TIMEOUT)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, list):
                    continue
                for item in payload:
                    title = item.get("text") or ""
                    categories = item.get("categories") or {}
                    location = categories.get("location") if isinstance(categories, dict) else None
                    team = categories.get("team") if isinstance(categories, dict) else None
                    commitment = categories.get("commitment") if isinstance(categories, dict) else None
                    workplace = categories.get("workplaceType") if isinstance(categories, dict) else None
                    description = clean_text(item.get("descriptionPlain") or item.get("description") or "")
                    text_blob = " ".join([title, location or "", team or "", commitment or "", workplace or "", description])
                    if not any(term in text_blob.lower() for term in ["tosca", "qa", "test", "sap", "quality"]):
                        continue
                    job = Job(
                        title=title,
                        company=company,
                        location=location,
                        work_model=infer_work_model(workplace or text_blob),
                        salary=extract_salary(description),
                        apply_url=item.get("hostedUrl") or item.get("applyUrl") or "",
                        source=f"Lever:{company}",
                        posted_at=item.get("createdAt"),
                        description=description,
                        skills_summary=", ".join(filter(None, [team, commitment, workplace])) or None,
                    )
                    job.age_days = parse_date_to_age_days(job.posted_at)
                    if job.apply_url:
                        jobs.append(job)
            except Exception:
                logger.exception("Lever fetch failed for %s", company)
        return jobs


class SerpAPIFetcher(Fetcher):
    def __init__(self, serpapi_key: str) -> None:
        super().__init__()
        self.serpapi_key = serpapi_key

    def fetch(self) -> list[Job]:
        if not self.serpapi_key:
            return []

        logger.info("Fetching SerpAPI Google Jobs results")
        jobs: list[Job] = []
        queries = [
            '"Tricentis Tosca" remote jobs',
            '"Tosca Automation" remote qa jobs',
            '"SAP Test Automation" Tosca remote',
            '"Test Automation Architect" Tosca remote',
        ]

        for query in queries:
            params = {
                "engine": "google_jobs",
                "q": query,
                "api_key": self.serpapi_key,
                "hl": "en",
            }
            try:
                response = self.session.get("https://serpapi.com/search.json", params=params, timeout=HTTP_TIMEOUT)
                response.raise_for_status()
                payload = response.json()
                for item in payload.get("jobs_results", []) or []:
                    title = item.get("title") or ""
                    company = item.get("company_name") or "Unknown"
                    location = item.get("location")
                    description = clean_text(item.get("description") or "")
                    apply_options = item.get("apply_options") or []
                    apply_url = ""
                    if apply_options and isinstance(apply_options, list) and isinstance(apply_options[0], dict):
                        apply_url = apply_options[0].get("link") or ""
                    text_blob = " ".join([title, company, location or "", description])
                    if not any(term in text_blob.lower() for term in ["tosca", "qa", "test", "sap", "quality"]):
                        continue
                    job = Job(
                        title=title,
                        company=company,
                        location=location,
                        work_model=infer_work_model(text_blob),
                        salary=extract_salary(description),
                        apply_url=apply_url,
                        source="SerpAPI",
                        posted_at=item.get("detected_extensions", {}).get("posted_at") if isinstance(item.get("detected_extensions"), dict) else None,
                        description=description,
                    )
                    if job.apply_url:
                        jobs.append(job)
            except Exception:
                logger.exception("SerpAPI fetch failed for query: %s", query)
        return jobs


class LinkedInGuestFetcher(Fetcher):
    """
    Lightweight LinkedIn guest jobs fetcher.
    Uses LinkedIn's public guest endpoint (no browser automation).
    """
    search_url = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
    li_pattern = re.compile(r"<li[^>]*>.*?</li>", re.IGNORECASE | re.DOTALL)
    url_pattern = re.compile(r'href="([^"]+)"', re.IGNORECASE | re.DOTALL)
    title_pattern = re.compile(r'base-search-card__title[^>]*>\s*(.*?)\s*</', re.IGNORECASE | re.DOTALL)
    company_pattern = re.compile(r'base-search-card__subtitle[^>]*>\s*(.*?)\s*</', re.IGNORECASE | re.DOTALL)
    location_pattern = re.compile(r'job-search-card__location[^>]*>\s*(.*?)\s*</', re.IGNORECASE | re.DOTALL)
    posted_pattern = re.compile(r"datetime=['\"]([^'\"]+)['\"]", re.IGNORECASE | re.DOTALL)

    def __init__(self, queries: list[str], location: str) -> None:
        super().__init__()
        self.queries = queries
        self.location = location

    @staticmethod
    def _extract_clean(pattern: re.Pattern[str], text: str, limit: int = 180) -> str | None:
        match = pattern.search(text)
        if not match:
            return None
        return clean_text(html.unescape(match.group(1)), limit=limit)

    @staticmethod
    def _extract_raw(pattern: re.Pattern[str], text: str) -> str | None:
        match = pattern.search(text)
        if not match:
            return None
        return html.unescape(match.group(1)).strip()

    def _parse_cards(self, html_chunk: str) -> list[Job]:
        jobs: list[Job] = []
        for block in self.li_pattern.findall(html_chunk):
            href = self._extract_raw(self.url_pattern, block) or ""
            apply_url = normalize_url(urljoin("https://www.linkedin.com", href))
            title = self._extract_clean(self.title_pattern, block) or ""
            company = self._extract_clean(self.company_pattern, block) or "Unknown"
            location = self._extract_clean(self.location_pattern, block)
            posted_at = self._extract_raw(self.posted_pattern, block)

            if not apply_url or "linkedin.com/jobs/view/" not in apply_url or not title:
                continue

            text_blob = " ".join([title, company, location or ""])
            if not any(term in text_blob.lower() for term in ["tosca", "qa", "test", "sap", "quality"]):
                continue

            job = Job(
                title=title,
                company=company,
                location=location,
                work_model=infer_work_model(text_blob),
                salary=None,
                apply_url=apply_url,
                source="LinkedIn",
                posted_at=posted_at,
                description=None,
            )
            job.age_days = parse_date_to_age_days(job.posted_at)
            jobs.append(job)
        return jobs

    def fetch(self) -> list[Job]:
        logger.info("Fetching LinkedIn guest jobs")
        all_jobs: list[Job] = []
        for query in self.queries:
            for start in (0, 25, 50):
                params = {
                    "keywords": query,
                    "location": self.location,
                    "f_WT": "2",      # Remote
                    "f_TPR": "r604800",  # Last 7 days
                    "start": start,
                }
                try:
                    response = self.session.get(self.search_url, params=params, timeout=HTTP_TIMEOUT)
                    if response.status_code in {429, 999}:
                        logger.warning("LinkedIn blocked/rate-limited request for query=%s start=%s status=%s", query, start, response.status_code)
                        break
                    response.raise_for_status()
                    parsed = self._parse_cards(response.text or "")
                    if not parsed:
                        break
                    all_jobs.extend(parsed)
                except Exception:
                    logger.exception("LinkedIn guest fetch failed for query=%s start=%s", query, start)
                    break
        return all_jobs


class AgentBase:
    def __init__(self, client: OpenAI, model: str) -> None:
        self.client = client
        self.model = model

    def ask_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        response = self.client.responses.create(
            model=self.model,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        text = getattr(response, "output_text", "") or "{}"
        try:
            return json.loads(extract_json_object(text))
        except Exception:
            logger.warning("Model JSON parse failed. Raw excerpt: %s", text[:1000])
            return {}


class RelevanceAgent(AgentBase):
    def evaluate(self, jobs: list[Job], min_score: int) -> list[Job]:
        if not jobs:
            return []

        strong_prefilter = []
        for job in jobs:
            local_score = local_keyword_score(job)
            if local_score >= max(35, min_score - 30):
                strong_prefilter.append(job)

        if not strong_prefilter:
            return []

        if len(strong_prefilter) <= 8:
            for job in strong_prefilter:
                job.score = local_keyword_score(job)
                job.relevance_reason = "Matched local Tosca/SAP/automation relevance rules."
            return [j for j in strong_prefilter if (j.score or 0) >= min_score]

        kept: list[Job] = []
        batch_size = 10
        system_prompt = (
            "You are a recruiting analyst for senior QA automation candidates. "
            "Prefer roles that explicitly mention Tricentis Tosca, Tosca, SAP test automation, enterprise QA automation, API testing, regression leadership, automation lead, automation architect, or similar. "
            "Reject irrelevant jobs. Return strict JSON only."
        )

        for i in range(0, len(strong_prefilter), batch_size):
            batch = strong_prefilter[i:i + batch_size]
            lightweight = []
            for idx, job in enumerate(batch):
                lightweight.append({
                    "idx": idx,
                    "title": job.title,
                    "company": job.company,
                    "location": job.location,
                    "work_model": job.work_model,
                    "salary": job.salary,
                    "age_days": job.age_days,
                    "source": job.source,
                    "description": clean_text(job.description, 450),
                    "skills_summary": job.skills_summary,
                    "local_score": local_keyword_score(job),
                })

            user_prompt = (
                "Evaluate the following jobs for a senior Tricentis Tosca / QA Automation Lead profile. "
                "Return JSON with key 'results'. Each item must include: idx, keep, score, reason, work_model, location, salary.\n\n"
                f"Jobs:\n{json.dumps(lightweight, ensure_ascii=False)}"
            )
            result = self.ask_json(system_prompt, user_prompt)

            for item in result.get("results", []):
                try:
                    idx = int(item["idx"])
                    if idx < 0 or idx >= len(batch):
                        continue
                    job = batch[idx]
                    llm_score = int(item.get("score", 0))
                    local_score = local_keyword_score(job)
                    final_score = round((llm_score * 0.7) + (local_score * 0.3))
                    job.score = max(0, min(100, final_score))
                    job.relevance_reason = str(item.get("reason") or "")[:240]
                    job.work_model = item.get("work_model") or job.work_model
                    job.location = item.get("location") or job.location
                    job.salary = item.get("salary") or job.salary
                    if bool(item.get("keep")) and (job.score or 0) >= min_score:
                        kept.append(job)
                except Exception:
                    logger.exception("Failed to process relevance result")
        return kept


class EmailAgent(AgentBase):
    def build(self, jobs: list[Job], max_jobs: int) -> tuple[str, str]:
        jobs = jobs[:max_jobs]
        today = now_local().strftime("%Y-%m-%d")
        payload = []
        for job in jobs:
            payload.append({
                "title": job.title,
                "company": job.company,
                "salary": job.salary,
                "work_model": job.work_model,
                "location": job.location,
                "apply_url": job.apply_url,
                "score": job.score,
                "reason": job.relevance_reason,
                "source": job.source,
                "posted_at": job.posted_at,
                "age_days": job.age_days,
                "is_new": job.is_new,
            })

        system_prompt = (
            "You write concise professional HTML digest emails for job alerts. "
            "Keep it scan-friendly. Use a table. Highlight the top 3 jobs. Never invent missing data. "
            "Return strict JSON with keys: subject, html."
        )
        user_prompt = (
            f"Create an HTML email for a daily Tricentis Tosca job digest dated {today}. "
            "Include a short intro, one compact table with columns Job, Company, Salary, Mode, Location, Score, Source, Apply, then a Top 3 section, then 2-4 short observations. "
            "Use clean minimal HTML only.\n\n"
            f"Jobs:\n{json.dumps(payload, ensure_ascii=False)}"
        )
        result = self.ask_json(system_prompt, user_prompt)
        subject = result.get("subject") or f"Daily Tosca Jobs - {today}"
        html_body = result.get("html")
        if isinstance(html_body, str) and "<" in html_body:
            return subject, html_body
        return subject, fallback_email_html(jobs, today)


def extract_json_object(text: str) -> str:
    text = text.strip()
    if text.startswith("{") and text.endswith("}"):
        return text
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start:end + 1]
    raise ValueError("No JSON object found")


def fallback_email_html(jobs: list[Job], today: str) -> str:
    def esc(v: Any) -> str:
        return html.escape(str(v or ""))

    rows = []
    for job in jobs:
        new_badge = "🆕 " if job.is_new else ""
        rows.append(
            f"<tr>"
            f"<td>{new_badge}{esc(job.title)}</td>"
            f"<td>{esc(job.company)}</td>"
            f"<td>{esc(job.salary or 'N/A')}</td>"
            f"<td>{esc(job.work_model or 'N/A')}</td>"
            f"<td>{esc(job.location or 'N/A')}</td>"
            f"<td>{esc(job.score or 'N/A')}</td>"
            f"<td>{esc(job.source)}</td>"
            f"<td><a href='{esc(job.apply_url)}'>Apply</a></td>"
            f"</tr>"
        )

    top3 = "".join(
        f"<li><strong>{esc(j.title)}</strong> — {esc(j.company)} ({esc(j.score)}/100). {esc(j.relevance_reason or '')}</li>"
        for j in jobs[:3]
    )

    return f"""
    <html>
      <body style="font-family: Arial, sans-serif; line-height: 1.45; color: #222;">
        <h2>Daily Tricentis Tosca Jobs — {esc(today)}</h2>
        <p>Here is your latest ranked Tosca job digest. Jobs are prioritized for a senior QA Automation / Tosca profile.</p>
        <table border="1" cellpadding="6" cellspacing="0" style="border-collapse: collapse; width: 100%; font-size: 13px;">
          <thead>
            <tr>
              <th>Job</th><th>Company</th><th>Salary</th><th>Mode</th><th>Location</th><th>Score</th><th>Source</th><th>Apply</th>
            </tr>
          </thead>
          <tbody>
            {''.join(rows)}
          </tbody>
        </table>
        <h3>Top 3</h3>
        <ol>{top3}</ol>
        <p><strong>Legend:</strong> 🆕 means new since previous runs.</p>
      </body>
    </html>
    """


def send_email(subject: str, html_body: str) -> None:
    email_host = os.getenv("EMAIL_HOST")
    email_port = int(os.getenv("EMAIL_PORT", "587"))
    email_user = os.getenv("EMAIL_USER")
    email_password = os.getenv("EMAIL_PASSWORD")
    email_to = os.getenv("EMAIL_TO")
    email_from = os.getenv("EMAIL_FROM", email_user)

    missing = [
        key for key, value in {
            "EMAIL_HOST": email_host,
            "EMAIL_USER": email_user,
            "EMAIL_PASSWORD": email_password,
            "EMAIL_TO": email_to,
            "EMAIL_FROM": email_from,
        }.items() if not value
    ]
    if missing:
        raise RuntimeError(f"Missing email config: {', '.join(missing)}")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = email_from
    msg["To"] = email_to
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    context = ssl.create_default_context()
    with smtplib.SMTP(email_host, email_port) as server:
        server.starttls(context=context)
        server.login(email_user, email_password)
        server.sendmail(email_from, [email_to], msg.as_string())
    logger.info("Email sent to %s", email_to)


def telegram_api_request(bot_token: str, method: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{bot_token}/{method}"
    response = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    body = response.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram API error for {method}: {body}")
    return body


def send_telegram_message(bot_token: str, chat_id: str, text: str) -> None:
    chunks = [text[i:i + 3500] for i in range(0, len(text), 3500)] or [text]
    for chunk in chunks:
        telegram_api_request(
            bot_token,
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
            },
        )


def build_telegram_digest(jobs: list[Job], max_jobs: int) -> str:
    today = now_local().strftime("%Y-%m-%d")
    if not jobs:
        return (
            f"📭 Tosca Jobs Digest ({today})\n"
            "No matching Tricentis Tosca remote jobs were found in this run."
        )

    lines = [f"📌 Tosca Jobs Digest ({today})", f"Total matches: {len(jobs)}", ""]
    for idx, job in enumerate(jobs[:max_jobs], start=1):
        new_tag = "🆕 " if job.is_new else ""
        score = job.score if job.score is not None else local_keyword_score(job)
        location = job.location or "Location not listed"
        posted = f"{job.age_days}d ago" if job.age_days is not None else "date n/a"
        lines.append(
            f"{idx}. {new_tag}{job.title} @ {job.company}\n"
            f"   Score: {score} | {location} | {posted}\n"
            f"   {job.apply_url}"
        )
    if len(jobs) > max_jobs:
        lines.append("")
        lines.append(f"...and {len(jobs) - max_jobs} more jobs in the filtered results.")
    return "\n".join(lines)


def send_telegram_digest(bot_token: str, chat_id: str, jobs: list[Job], max_jobs: int) -> None:
    digest = build_telegram_digest(jobs, max_jobs)
    send_telegram_message(bot_token, chat_id, digest)
    logger.info("Telegram digest sent to chat %s", chat_id)


def load_seen_ids() -> set[str]:
    payload = read_json(HISTORY_PATH, {"seen_ids": []})
    return set(payload.get("seen_ids", []))


def save_seen_ids(ids: set[str]) -> None:
    write_json(HISTORY_PATH, {"seen_ids": sorted(ids)})


def dedupe_jobs(jobs: Iterable[Job]) -> list[Job]:
    best: dict[str, Job] = {}
    for job in jobs:
        key = job.dedupe_key()
        current_score = local_keyword_score(job)
        if key not in best:
            best[key] = job
            continue
        previous = best[key]
        previous_score = local_keyword_score(previous)
        if current_score > previous_score:
            best[key] = job
        elif current_score == previous_score:
            prev_age = previous.age_days if previous.age_days is not None else 9999
            curr_age = job.age_days if job.age_days is not None else 9999
            if curr_age < prev_age:
                best[key] = job
    return list(best.values())


def collect_jobs(config: Config) -> list[Job]:
    fetchers: list[Fetcher] = [
        RemoteOKFetcher(),
        GreenhouseFetcher(),
        LeverFetcher(),
    ]
    if config.linkedin_enabled:
        fetchers.append(LinkedInGuestFetcher(config.linkedin_queries, config.linkedin_location))
    if config.serpapi_key:
        fetchers.append(SerpAPIFetcher(config.serpapi_key))

    all_jobs: list[Job] = []
    for fetcher in fetchers:
        fetched = fetcher.fetch()
        logger.info("Fetched %s jobs from %s", len(fetched), fetcher.__class__.__name__)
        all_jobs.extend(fetched)

    write_json(RAW_DUMP_PATH, [job.as_dict() for job in all_jobs])
    return all_jobs


def apply_basic_filters(jobs: list[Job], config: Config) -> list[Job]:
    filtered: list[Job] = []
    for job in jobs:
        haystack = " ".join(filter(None, [job.title, job.description, job.skills_summary, job.work_model, job.location])).lower()
        if not any(term in haystack for term in ["tosca", "tricentis", "qa", "test automation", "sap", "quality"]):
            continue
        if not is_canada_based(job):
            continue
        if config.remote_only:
            wm = (job.work_model or "").lower()
            loc = (job.location or "").lower()
            desc = (job.description or "").lower()
            if "remote" not in wm and "remote" not in loc and "remote" not in desc:
                continue
        if job.age_days is not None and job.age_days > config.days_lookback:
            continue
        if local_keyword_score(job) < 20:
            continue
        filtered.append(job)
    return filtered


def is_canada_based(job: Job) -> bool:
    location_blob = " ".join(filter(None, [job.location, job.description])).lower()
    return any(hint in location_blob for hint in CANADA_LOCATION_HINTS)


def finalize_ranking(jobs: list[Job]) -> list[Job]:
    for job in jobs:
        if job.score is None:
            job.score = local_keyword_score(job)
    jobs.sort(
        key=lambda j: (
            j.score or 0,
            1 if j.is_new else 0,
            -(j.age_days or 9999),
        ),
        reverse=True,
    )
    return jobs


def run(send_email_enabled: bool = True, telegram_chat_id: str | None = None) -> list[Job]:
    load_dotenv(BASE_DIR / ".env")
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY not found in environment or .env")

    config = Config()
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    logger.info("Starting optimized Tosca multi-agent job search")
    jobs = collect_jobs(config)
    jobs = apply_basic_filters(jobs, config)
    jobs = dedupe_jobs(jobs)
    logger.info("After basic filters and dedupe: %s jobs", len(jobs))

    relevance_agent = RelevanceAgent(client, config.filter_model)
    jobs = relevance_agent.evaluate(jobs, config.min_score)
    jobs = dedupe_jobs(jobs)

    seen_ids = load_seen_ids()
    updated_seen = set(seen_ids)
    for job in jobs:
        sid = job.stable_id()
        job.is_new = sid not in seen_ids
        updated_seen.add(sid)

    jobs = finalize_ranking(jobs)
    write_json(FILTERED_DUMP_PATH, [job.as_dict() for job in jobs])

    email_jobs = jobs
    if config.only_new_in_email:
        email_jobs = [j for j in jobs if j.is_new]

    if send_email_enabled:
        if not email_jobs:
            subject = f"Daily Tosca Jobs - {now_local().strftime('%Y-%m-%d')}"
            html_body = (
                "<html><body><p>No matching Tricentis Tosca remote jobs were found today from the configured sources.</p>"
                "<p>You can improve coverage by adding more Greenhouse and Lever company boards or enabling SerpAPI.</p></body></html>"
            )
            send_email(subject, html_body)
            logger.info("No jobs to email")
        else:
            email_agent = EmailAgent(client, config.email_model)
            subject, html_body = email_agent.build(email_jobs, config.max_email_jobs)
            send_email(subject, html_body)

    if telegram_chat_id:
        if not config.telegram_bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required when telegram_chat_id is provided")
        send_telegram_digest(config.telegram_bot_token, telegram_chat_id, jobs, config.telegram_max_jobs)

    save_seen_ids(updated_seen)
    logger.info("Done. %s matching jobs, %s email candidates.", len(jobs), len(email_jobs[:config.max_email_jobs]))
    return jobs


def poll_telegram_updates(bot_token: str, offset: int | None = None, timeout_seconds: int = 45) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"timeout": timeout_seconds}
    if offset is not None:
        params["offset"] = offset
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    response = requests.get(url, params=params, timeout=timeout_seconds + 10)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram polling failed: {payload}")
    return payload.get("result", [])


def run_telegram_bot() -> None:
    load_dotenv(BASE_DIR / ".env")
    config = Config()
    if not config.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required for --telegram-bot mode")

    logger.info("Telegram bot polling started. Use /run to trigger job search.")
    offset: int | None = None

    while True:
        updates = poll_telegram_updates(config.telegram_bot_token, offset=offset, timeout_seconds=45)
        for update in updates:
            offset = int(update.get("update_id", 0)) + 1
            message = update.get("message") or {}
            chat = message.get("chat") or {}
            text = (message.get("text") or "").strip()
            chat_id = str(chat.get("id") or "").strip()
            if not text or not chat_id:
                continue

            if config.telegram_chat_id and chat_id != config.telegram_chat_id:
                logger.info("Ignoring Telegram command from unauthorized chat %s", chat_id)
                continue

            if text.lower() in {"/start", "/help"}:
                help_text = (
                    "🤖 Tosca Job Agent Bot\n"
                    "Available commands:\n"
                    "/run - fetch jobs now and send digest here\n"
                    "/help - show this help message"
                )
                send_telegram_message(config.telegram_bot_token, chat_id, help_text)
                continue

            if text.lower() == "/run":
                send_telegram_message(config.telegram_bot_token, chat_id, "⏳ Running job search, please wait...")
                try:
                    run(send_email_enabled=False, telegram_chat_id=chat_id)
                except Exception as exc:
                    logger.exception("Telegram-triggered run failed")
                    send_telegram_message(config.telegram_bot_token, chat_id, f"❌ Run failed: {exc}")
                continue

            send_telegram_message(
                config.telegram_bot_token,
                chat_id,
                "Unknown command. Send /run to fetch jobs or /help for instructions.",
            )
        time.sleep(max(config.telegram_poll_seconds, 1))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Tosca job finder automation runner")
    parser.add_argument(
        "--telegram-bot",
        action="store_true",
        help="Run a Telegram bot polling loop. Use /run in Telegram to trigger job collection and digest delivery.",
    )
    parser.add_argument(
        "--telegram-chat-id",
        type=str,
        default="",
        help="Send a Telegram digest for this run to a specific chat ID.",
    )
    parser.add_argument(
        "--no-email",
        action="store_true",
        help="Skip email sending for this run.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.telegram_bot:
        run_telegram_bot()
    else:
        target_chat = args.telegram_chat_id.strip() if args.telegram_chat_id else None
        run(send_email_enabled=not args.no_email, telegram_chat_id=target_chat)
