import asyncio
import concurrent.futures
import json
import os
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from pydantic import BaseModel

load_dotenv()

from backend.scraper import scrape_profile, connect_profile  # noqa: E402
from backend.improver import improve_profile  # noqa: E402

app = FastAPI(title="LinkedIn Profile Improver")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
TOKEN_FILE = Path(__file__).parent.parent / "google_token.json"
CREDS_FILE = Path(__file__).parent.parent / "google_credentials.json"

SCOPES = ["https://www.googleapis.com/auth/drive.file", "https://www.googleapis.com/auth/documents"]
REDIRECT_URI = "http://localhost:8000/auth/callback"

_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)


class AnalyzeRequest(BaseModel):
    url: str
    extra_context: str = ""


class ConnectRequest(BaseModel):
    url: str
    note: str = ""


class ExperienceItem(BaseModel):
    title: str
    company: str
    date_from: str = ""
    date_to: str = ""
    location: str = ""
    improved_bullets: Optional[List[str]] = []


class EducationItem(BaseModel):
    school: str
    degree: str = ""
    field: str = ""
    years: str = ""


class CertificationItem(BaseModel):
    name: str
    issuer: str = ""
    date: str = ""


class LanguageItem(BaseModel):
    language: str
    proficiency: str = ""


class ResumeRequest(BaseModel):
    name: str
    headline: str
    location: str = ""
    about: str
    experience: List[ExperienceItem]
    education: List[EducationItem] = []
    certifications: List[CertificationItem] = []
    skills: List[str]
    languages: List[LanguageItem] = []


_pending_flow: Optional[Flow] = None


def _get_credentials() -> Optional[Credentials]:
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
        if creds and creds.valid:
            return creds
        if creds and creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request
            creds.refresh(Request())
            TOKEN_FILE.write_text(creds.to_json())
            return creds
    return None


@app.get("/")
async def serve_frontend():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/auth/status")
async def auth_status():
    creds = _get_credentials()
    return {"authenticated": creds is not None}


@app.get("/auth/login")
async def auth_login():
    global _pending_flow
    if not CREDS_FILE.exists():
        raise HTTPException(
            status_code=501,
            detail="google_credentials.json not found. See setup instructions."
        )
    _pending_flow = Flow.from_client_secrets_file(str(CREDS_FILE), scopes=SCOPES, redirect_uri=REDIRECT_URI)
    auth_url, _ = _pending_flow.authorization_url(prompt="consent", access_type="offline")
    return RedirectResponse(auth_url)


@app.get("/auth/callback")
async def auth_callback(code: str, state: str):
    global _pending_flow
    if _pending_flow is None:
        raise HTTPException(status_code=400, detail="No pending auth flow. Please visit /auth/login first.")
    _pending_flow.fetch_token(code=code, state=state)
    TOKEN_FILE.write_text(_pending_flow.credentials.to_json())
    _pending_flow = None
    return RedirectResponse("/?auth=success")


@app.post("/analyze")
async def analyze_profile(request: AnalyzeRequest):
    url = request.url
    if url and not url.startswith("http"):
        url = "https://" + url
    request = AnalyzeRequest(url=url, extra_context=request.extra_context)

    if "linkedin.com/in/" not in request.url:
        raise HTTPException(status_code=400, detail="Please provide a valid LinkedIn profile URL (linkedin.com/in/...)")

    try:
        loop = asyncio.get_event_loop()
        profile_data = await loop.run_in_executor(_executor, scrape_profile, request.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to scrape profile: {type(e).__name__}: {e}")

    if not profile_data.get("name") and not profile_data.get("headline"):
        raise HTTPException(
            status_code=422,
            detail="Could not extract profile data. The profile may be private, or LinkedIn blocked the request.",
        )

    try:
        improvements = improve_profile(profile_data, request.extra_context)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate improvements: {e}")

    return {"original": profile_data, "improved": improvements}


@app.post("/connect")
async def connect_to_profile(request: ConnectRequest):
    url = request.url
    if url and not url.startswith("http"):
        url = "https://" + url
    if "linkedin.com/in/" not in url:
        raise HTTPException(status_code=400, detail="Please provide a valid LinkedIn profile URL (linkedin.com/in/...)")

    try:
        loop = asyncio.get_event_loop()
        status = await loop.run_in_executor(
            _executor,
            lambda: connect_profile(url, request.note),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to send connection request: {type(e).__name__}: {e}")

    return {"status": status}



def _utf16_len(s: str) -> int:
    """Google Docs API uses UTF-16 code unit offsets."""
    return len(s.encode("utf-16-le")) // 2


def _build_resume_doc(data: ResumeRequest) -> tuple:
    """
    Returns (full_text, formatting_requests).
    We insert all text in one shot, then apply formatting in a second batchUpdate.
    This avoids index drift caused by mixing insertText + style requests.
    """
    # Each segment: (text, text_style, para_style)
    segments = []

    ORANGE = {"color": {"rgbColor": {"red": 0.91, "green": 0.47, "blue": 0.13}}}
    GREY   = {"color": {"rgbColor": {"red": 0.5,  "green": 0.5,  "blue": 0.5}}}

    def seg(text, ts=None, ps=None):
        segments.append((text + "\n", ts or {}, ps or {}))

    def heading(title):
        seg(title.upper(),
            ts={"bold": True, "fontSize": {"magnitude": 11, "unit": "PT"}, "foregroundColor": ORANGE},
            ps={"spaceAbove": {"magnitude": 12, "unit": "PT"}, "spaceBelow": {"magnitude": 4, "unit": "PT"}})

    # Name
    seg(data.name or "Resume",
        ts={"bold": True, "fontSize": {"magnitude": 20, "unit": "PT"}},
        ps={"alignment": "CENTER"})
    # Headline
    if data.headline:
        seg(data.headline,
            ts={"fontSize": {"magnitude": 11, "unit": "PT"}, "foregroundColor": GREY},
            ps={"alignment": "CENTER"})
    # Location
    if data.location:
        seg(data.location,
            ts={"fontSize": {"magnitude": 10, "unit": "PT"}, "foregroundColor": GREY},
            ps={"alignment": "CENTER"})
    # About
    if data.about:
        heading("Profile")
        seg(data.about, ts={"fontSize": {"magnitude": 10, "unit": "PT"}})
    # Work Experience
    if data.experience:
        heading("Work Experience")
        for exp in data.experience:
            date_str = " – ".join(p for p in [exp.date_from, exp.date_to] if p)
            seg(exp.title,
                ts={"bold": True, "fontSize": {"magnitude": 11, "unit": "PT"}},
                ps={"spaceAbove": {"magnitude": 8, "unit": "PT"}})
            subtitle = " · ".join(p for p in [exp.company, date_str] if p)
            if subtitle:
                seg(subtitle,
                    ts={"fontSize": {"magnitude": 10, "unit": "PT"}, "foregroundColor": ORANGE})
            for bullet in (exp.improved_bullets or []):
                if bullet:
                    seg("• " + bullet,
                        ts={"fontSize": {"magnitude": 10, "unit": "PT"}},
                        ps={"indentStart": {"magnitude": 14, "unit": "PT"}})
    # Education
    if data.education:
        heading("Education")
        for edu in data.education:
            seg(edu.school,
                ts={"bold": True, "fontSize": {"magnitude": 11, "unit": "PT"}},
                ps={"spaceAbove": {"magnitude": 4, "unit": "PT"}})
            degree = ", ".join(p for p in [edu.degree, edu.field] if p)
            sub = " · ".join(p for p in [degree, edu.years] if p)
            if sub:
                seg(sub, ts={"fontSize": {"magnitude": 10, "unit": "PT"}})
    # Certifications
    if data.certifications:
        heading("Certifications")
        for cert in data.certifications:
            seg(cert.name,
                ts={"bold": True, "fontSize": {"magnitude": 11, "unit": "PT"}},
                ps={"spaceAbove": {"magnitude": 4, "unit": "PT"}})
            sub = " · ".join(p for p in [cert.issuer, cert.date] if p)
            if sub:
                seg(sub, ts={"fontSize": {"magnitude": 10, "unit": "PT"}})
    # Skills
    if data.skills:
        heading("Skills")
        seg(", ".join(data.skills), ts={"fontSize": {"magnitude": 10, "unit": "PT"}})
    # Languages
    if data.languages:
        heading("Languages")
        for lang in data.languages:
            text = lang.language + (f" ({lang.proficiency})" if lang.proficiency else "")
            seg("• " + text, ts={"fontSize": {"magnitude": 10, "unit": "PT"}})

    # ── Build full text and formatting requests ───────────────────────────────
    full_text = "".join(t for t, _, _ in segments)

    fmt_requests = []
    idx = 1  # Google Docs body starts at index 1
    for (text, ts, ps) in segments:
        end = idx + _utf16_len(text)
        if ts:
            fmt_requests.append({"updateTextStyle": {
                "range": {"startIndex": idx, "endIndex": end},
                "textStyle": ts,
                "fields": ",".join(ts.keys()),
            }})
        if ps:
            fmt_requests.append({"updateParagraphStyle": {
                "range": {"startIndex": idx, "endIndex": end},
                "paragraphStyle": ps,
                "fields": ",".join(ps.keys()),
            }})
        idx = end

    return full_text, fmt_requests


@app.post("/resume")
async def create_resume(request: ResumeRequest):
    creds = _get_credentials()
    if not creds:
        return JSONResponse(status_code=401, content={"auth_required": True, "auth_url": "/auth/login"})

    filename = f"{request.name or 'Resume'} — Resume"

    # Create empty Google Doc
    docs = build("docs", "v1", credentials=creds)
    doc = docs.documents().create(body={"title": filename}).execute()
    doc_id = doc["documentId"]

    # Pass 1: insert all text at once
    full_text, fmt_requests = _build_resume_doc(request)
    docs.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": [{"insertText": {"location": {"index": 1}, "text": full_text}}]},
    ).execute()

    # Pass 2: apply all formatting (indices are now stable)
    if fmt_requests:
        docs.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": fmt_requests},
        ).execute()

    doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"
    return {"doc_url": doc_url}


@app.get("/health")
async def health():
    return {"status": "ok"}
