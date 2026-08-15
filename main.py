import csv
import io
import os
import re
import smtplib
import threading
import time
import uuid
from email.message import EmailMessage

import httpx
import openpyxl
import pdfplumber
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODELS = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

MAX_RECIPIENTS = 100
MAX_RESUME_BYTES = 5 * 1024 * 1024
MAX_RECIPIENTS_FILE_BYTES = 2 * 1024 * 1024
SEND_DELAY_SECONDS = 6
JOB_TTL_SECONDS = 2 * 60 * 60

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465

JOBS = {}
JOBS_LOCK = threading.Lock()
ACTIVE_SENDERS = set()
ACTIVE_LOCK = threading.Lock()


def purge_old_jobs():
    cutoff = time.time() - JOB_TTL_SECONDS
    with JOBS_LOCK:
        stale = [jid for jid, j in JOBS.items() if j.get("created", 0) < cutoff]
        for jid in stale:
            JOBS.pop(jid, None)


def extract_resume_text(pdf_bytes: bytes) -> str:
    parts = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            parts.append(page.extract_text() or "")
    text = "\n".join(parts).strip()
    if not text:
        raise ValueError("Could not extract text from this PDF (it may be a scanned image, not real text).")
    return text[:8000]


def parse_recipients(filename: str, content: bytes):
    if filename.lower().endswith(".csv"):
        reader = csv.reader(io.StringIO(content.decode("utf-8", errors="ignore")))
        rows = list(reader)
    else:
        wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
        rows = list(wb.active.iter_rows(values_only=True))

    numeric_re = re.compile(r"^\d+(\.\d+)?$")

    contacts = []
    seen = set()
    for row in rows:
        if not row:
            continue
        cells = [str(c).strip() for c in row if c is not None]
        email = next((c for c in cells if EMAIL_RE.match(c)), None)
        if not email or email.lower() in seen:
            continue
        others = [c for c in cells if c and c != email and not numeric_re.match(c)]
        company = max(others, key=len) if others else email.split("@")[1].split(".")[0].title()
        contacts.append((company, email))
        seen.add(email.lower())
    return contacts


def _parse_draft(content: str, company: str):
    subject_match = re.search(r"SUBJECT:\s*(.+)", content)
    body_match = re.search(r"BODY:\s*(.*)", content, re.DOTALL)
    subject = subject_match.group(1).strip() if subject_match else f"Application for Opportunities at {company}"
    body = body_match.group(1).strip() if body_match else content.strip()
    return subject, body


def draft_email(resume_text: str, company: str, sender_name: str):
    prompt = f"""You are writing a short, professional cold outreach email from a job seeker to a company's HR team, applying for general opportunities (no specific role was given).

Ground every claim strictly in the resume text below. Do not invent skills, employers, numbers, or achievements that are not present in it.

Resume text:
---
{resume_text}
---

Company being contacted: {company}
Sender's name: {sender_name}

Write:
1. A short subject line (under 12 words, no surrounding quotes).
2. A 3-paragraph email body: (1) brief intro and why you're reaching out, (2) 2-3 sentences highlighting the most relevant experience/skills actually present in the resume, (3) a polite closing asking to connect, signed with just the sender's first name (contact details are appended separately, don't repeat them).

Respond in exactly this format:
SUBJECT: <subject line>
BODY:
<body text>
"""
    last_err = None
    for model in GROQ_MODELS:
        for attempt in range(3):
            try:
                resp = httpx.post(
                    GROQ_URL,
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.6,
                        "max_tokens": 500,
                    },
                    timeout=30,
                )
                if resp.status_code == 429:
                    retry_after = float(resp.headers.get("retry-after", 2))
                    time.sleep(min(retry_after, 15))
                    continue
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
                return _parse_draft(content, company)
            except httpx.HTTPStatusError as e:
                last_err = e
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(2)
    raise RuntimeError(f"LLM drafting failed for {company}: {last_err}")


def run_campaign(job_id, resume_bytes, resume_filename, resume_text, sender_name, sender_email, app_password, contacts):
    job = JOBS[job_id]
    try:
        smtp = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20)
        smtp.login(sender_email, app_password)
        for i, (company, email) in enumerate(contacts):
            try:
                subject, body = draft_email(resume_text, company, sender_name)
                msg = EmailMessage()
                msg["From"] = sender_email
                msg["To"] = email
                msg["Subject"] = subject
                msg.set_content(body)
                msg.add_attachment(
                    resume_bytes, maintype="application", subtype="pdf", filename=resume_filename
                )
                try:
                    smtp.send_message(msg)
                except (smtplib.SMTPServerDisconnected, smtplib.SMTPException):
                    smtp = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20)
                    smtp.login(sender_email, app_password)
                    smtp.send_message(msg)
                result = {"company": company, "email": email, "status": "sent", "subject": subject}
            except Exception as e:  # noqa: BLE001
                result = {"company": company, "email": email, "status": "failed", "error": str(e)}
            with JOBS_LOCK:
                job["results"].append(result)
                job["done"] = i + 1
            if i < len(contacts) - 1:
                time.sleep(SEND_DELAY_SECONDS)
        smtp.quit()
    except Exception as e:  # noqa: BLE001
        with JOBS_LOCK:
            job["fatal_error"] = str(e)
    finally:
        with JOBS_LOCK:
            job["finished"] = True
        with ACTIVE_LOCK:
            ACTIVE_SENDERS.discard(sender_email)
        # best-effort scrub of credentials from this frame
        app_password = None  # noqa: F841
        sender_email_local = sender_email  # noqa: F841


@app.get("/")
def index():
    return FileResponse("static/index.html")


@app.post("/api/campaign/start")
async def start_campaign(
    resume: UploadFile = File(...),
    recipients: UploadFile = File(...),
    sender_name: str = Form(...),
    sender_email: str = Form(...),
    app_password: str = Form(...),
):
    purge_old_jobs()

    if not GROQ_API_KEY:
        raise HTTPException(500, "Server misconfigured: GROQ_API_KEY not set")
    if not resume.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Resume must be a PDF")
    if not EMAIL_RE.match(sender_email):
        raise HTTPException(400, "Invalid sender email")

    with ACTIVE_LOCK:
        if sender_email in ACTIVE_SENDERS:
            raise HTTPException(409, "A campaign for this sender email is already running")

    resume_bytes = await resume.read()
    if len(resume_bytes) > MAX_RESUME_BYTES:
        raise HTTPException(400, "Resume too large (max 5MB)")

    recipients_bytes = await recipients.read()
    if len(recipients_bytes) > MAX_RECIPIENTS_FILE_BYTES:
        raise HTTPException(400, "Recipient list too large (max 2MB)")

    try:
        resume_text = extract_resume_text(resume_bytes)
    except ValueError as e:
        raise HTTPException(400, str(e))

    contacts = parse_recipients(recipients.filename, recipients_bytes)
    if not contacts:
        raise HTTPException(400, "No valid (company, email) rows found in the recipient file")
    if len(contacts) > MAX_RECIPIENTS:
        raise HTTPException(400, f"Too many recipients ({len(contacts)}); max {MAX_RECIPIENTS} per campaign")

    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
            smtp.login(sender_email, app_password)
    except smtplib.SMTPAuthenticationError:
        raise HTTPException(
            401,
            "Gmail login failed. Use an App Password (not your normal password) "
            "from https://myaccount.google.com/apppasswords",
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Could not connect to Gmail SMTP: {e}")

    job_id = str(uuid.uuid4())
    with JOBS_LOCK:
        JOBS[job_id] = {
            "total": len(contacts),
            "done": 0,
            "results": [],
            "finished": False,
            "created": time.time(),
        }
    with ACTIVE_LOCK:
        ACTIVE_SENDERS.add(sender_email)

    thread = threading.Thread(
        target=run_campaign,
        args=(job_id, resume_bytes, resume.filename, resume_text, sender_name, sender_email, app_password, contacts),
        daemon=True,
    )
    thread.start()

    return {"job_id": job_id, "total": len(contacts)}


@app.get("/api/campaign/{job_id}/status")
def campaign_status(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found (it may have expired)")
        return dict(job)
