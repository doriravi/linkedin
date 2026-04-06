import asyncio
import concurrent.futures
import io
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
from googleapiclient.http import MediaIoBaseUpload
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


def _build_resume_html(data: ResumeRequest) -> str:
    """Build a two-column styled resume (dark sidebar + content) as HTML."""
    DARK   = "#1C1C1E"
    ORANGE = "#E87722"
    WHITE  = "#FFFFFF"
    LIGHT  = "#F5F5F5"
    GREY   = "#666666"

    def esc(s: str) -> str:
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # ── Sidebar helpers ────────────────────────────────────────────────────────
    def sb_section(title: str) -> str:
        return (f"<tr><td style='padding:14px 20px 4px'>"
                f"<div style='color:{ORANGE};font-size:11px;font-weight:bold;"
                f"text-transform:uppercase;letter-spacing:1.5px;border-bottom:1px solid {ORANGE};"
                f"padding-bottom:4px;margin-bottom:8px'>{title}</div></td></tr>")

    def sb_item(text: str, sub: str = "") -> str:
        sub_html = f"<div style='color:#aaa;font-size:9px;margin-top:1px'>{esc(sub)}</div>" if sub else ""
        return (f"<tr><td style='padding:2px 20px'>"
                f"<div style='color:{WHITE};font-size:10px;line-height:1.6'>"
                f"&#9654; {esc(text)}{sub_html}</div></td></tr>")

    # ── Main content helpers ───────────────────────────────────────────────────
    def mc_section(title: str) -> str:
        return (f"<tr><td style='padding:14px 24px 4px'>"
                f"<div style='font-size:14px;font-weight:bold;color:{DARK};"
                f"text-transform:uppercase;letter-spacing:1px;"
                f"border-bottom:2px solid {ORANGE};padding-bottom:4px'>{esc(title)}</div>"
                f"</td></tr>")

    def mc_entry(title: str, subtitle: str, bullets: list) -> str:
        bhtml = ""
        if bullets:
            items = "".join(f"<li style='font-size:10px;color:#333;line-height:1.6;margin-bottom:2px'>{esc(b)}</li>" for b in bullets)
            bhtml = f"<ul style='margin:4px 0 0 0;padding-left:16px'>{items}</ul>"
        return (f"<tr><td style='padding:6px 24px 8px'>"
                f"<div style='font-size:11px;font-weight:bold;color:{DARK}'>{esc(title)}</div>"
                f"<div style='font-size:10px;color:{ORANGE};margin:1px 0'>{esc(subtitle)}</div>"
                f"{bhtml}</td></tr>")

    # ── Build sidebar ─────────────────────────────────────────────────────────
    name_parts = data.name.split(" ", 1) if data.name else ["", ""]
    first = name_parts[0]
    last  = name_parts[1] if len(name_parts) > 1 else ""

    sidebar_rows = [
        # Avatar placeholder
        f"<tr><td style='padding:30px 20px 10px;text-align:center'>"
        f"<div style='width:80px;height:80px;border-radius:50%;background:{ORANGE};"
        f"margin:0 auto;display:inline-block;line-height:80px;color:{WHITE};"
        f"font-size:28px;font-weight:bold;text-align:center'>"
        f"{esc(first[:1])}{esc(last[:1])}</div></td></tr>",
        # Name
        f"<tr><td style='padding:8px 20px 2px;text-align:center'>"
        f"<div style='color:{WHITE};font-size:20px;font-weight:bold'>"
        f"<span style='color:{WHITE}'>{esc(first)} </span>"
        f"<span style='color:{ORANGE}'>{esc(last)}</span></div></td></tr>",
        # Headline
        f"<tr><td style='padding:2px 20px 16px;text-align:center'>"
        f"<div style='color:#bbb;font-size:10px;text-transform:uppercase;letter-spacing:1px'>"
        f"{esc(data.headline)}</div></td></tr>",
    ]

    # Contact
    contact_items = []
    if data.location:
        contact_items.append((data.location, ""))
    sidebar_rows.append(sb_section("Contact"))
    for item, sub in contact_items:
        sidebar_rows.append(sb_item(item, sub))

    # Skills
    if data.skills:
        sidebar_rows.append(sb_section("Skills"))
        for skill in data.skills[:15]:
            sidebar_rows.append(sb_item(skill))

    # Languages
    if data.languages:
        sidebar_rows.append(sb_section("Languages"))
        for lang in data.languages:
            sidebar_rows.append(sb_item(lang.language, lang.proficiency))

    # ── Build main content ────────────────────────────────────────────────────
    main_rows = []

    if data.about:
        main_rows.append(mc_section("Profile"))
        for para in (data.about.split("\n\n") or [data.about])[:2]:
            if para.strip():
                main_rows.append(
                    f"<tr><td style='padding:4px 24px 8px'>"
                    f"<p style='font-size:10px;color:#333;line-height:1.7;margin:0'>{esc(para.strip())}</p>"
                    f"</td></tr>"
                )

    if data.education:
        main_rows.append(mc_section("Education"))
        for edu in data.education:
            degree = ", ".join(p for p in [edu.degree, edu.field] if p)
            subtitle = " · ".join(p for p in [edu.years] if p)
            main_rows.append(mc_entry(edu.school, degree or subtitle, [subtitle] if degree and subtitle else []))

    if data.experience:
        main_rows.append(mc_section("Work Experience"))
        for exp in data.experience:
            date_str = " – ".join(p for p in [exp.date_from, exp.date_to] if p)
            subtitle = " · ".join(p for p in [exp.company, date_str] if p)
            bullets = [b for b in (exp.improved_bullets or []) if b]
            main_rows.append(mc_entry(exp.title, subtitle, bullets))

    if data.certifications:
        main_rows.append(mc_section("Certifications"))
        for cert in data.certifications:
            subtitle = " · ".join(p for p in [cert.issuer, cert.date] if p)
            main_rows.append(mc_entry(cert.name, subtitle, []))

    # ── Assemble two-column table ─────────────────────────────────────────────
    sidebar_html  = "\n".join(sidebar_rows)
    main_html     = "\n".join(main_rows)

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;font-family:Arial,sans-serif;background:#e0e0e0">
<table width="780" cellpadding="0" cellspacing="0"
       style="margin:20px auto;border-collapse:collapse;box-shadow:0 4px 20px rgba(0,0,0,.3)">
  <tr>
    <!-- Sidebar -->
    <td width="240" valign="top" style="background:{DARK};vertical-align:top">
      <table width="100%" cellpadding="0" cellspacing="0">
        {sidebar_html}
        <tr><td style="padding:20px">&nbsp;</td></tr>
      </table>
    </td>
    <!-- Main -->
    <td width="540" valign="top" style="background:{WHITE};vertical-align:top">
      <table width="100%" cellpadding="0" cellspacing="0">
        {main_html}
        <tr><td style="padding:20px">&nbsp;</td></tr>
      </table>
    </td>
  </tr>
</table>
</body>
</html>"""


def _build_resume_requests(data: ResumeRequest) -> list:
    """Build a list of Google Docs API batchUpdate requests for the resume."""
    requests = []
    idx = 1  # current insert index (1 = start of doc)

    def insert(text: str, style: dict = None) -> None:
        nonlocal idx
        requests.append({"insertText": {"location": {"index": idx}, "text": text}})
        end = idx + len(text)
        if style:
            requests.append({"updateTextStyle": {
                "range": {"startIndex": idx, "endIndex": end},
                "textStyle": style,
                "fields": ",".join(style.keys()),
            }})
        idx = end

    def paragraph(text: str, style: dict = None, para_style: dict = None) -> None:
        nonlocal idx
        insert(text + "\n", style)
        if para_style:
            requests.append({"updateParagraphStyle": {
                "range": {"startIndex": idx - len(text) - 1, "endIndex": idx},
                "paragraphStyle": para_style,
                "fields": ",".join(para_style.keys()),
            }})

    def section_heading(title: str) -> None:
        paragraph(title.upper(), style={
            "bold": True,
            "fontSize": {"magnitude": 11, "unit": "PT"},
            "foregroundColor": {"color": {"rgbColor": {"red": 0.91, "green": 0.47, "blue": 0.13}}},
        }, para_style={"spaceAbove": {"magnitude": 12, "unit": "PT"},
                       "spaceBelow": {"magnitude": 4, "unit": "PT"}})

    # ── Name ─────────────────────────────────────────────────────────────────
    paragraph(data.name or "Resume", style={
        "bold": True,
        "fontSize": {"magnitude": 20, "unit": "PT"},
    }, para_style={"alignment": "CENTER"})

    # ── Headline ─────────────────────────────────────────────────────────────
    if data.headline:
        paragraph(data.headline, style={
            "fontSize": {"magnitude": 11, "unit": "PT"},
            "foregroundColor": {"color": {"rgbColor": {"red": 0.4, "green": 0.4, "blue": 0.4}}},
        }, para_style={"alignment": "CENTER"})

    # ── Location ─────────────────────────────────────────────────────────────
    if data.location:
        paragraph(data.location, style={
            "fontSize": {"magnitude": 10, "unit": "PT"},
            "foregroundColor": {"color": {"rgbColor": {"red": 0.5, "green": 0.5, "blue": 0.5}}},
        }, para_style={"alignment": "CENTER"})

    # ── About ─────────────────────────────────────────────────────────────────
    if data.about:
        section_heading("Profile")
        paragraph(data.about, style={"fontSize": {"magnitude": 10, "unit": "PT"}})

    # ── Work Experience ───────────────────────────────────────────────────────
    if data.experience:
        section_heading("Work Experience")
        for exp in data.experience:
            date_str = " – ".join(p for p in [exp.date_from, exp.date_to] if p)
            paragraph(exp.title, style={"bold": True, "fontSize": {"magnitude": 11, "unit": "PT"}},
                      para_style={"spaceAbove": {"magnitude": 6, "unit": "PT"}})
            subtitle = " · ".join(p for p in [exp.company, date_str] if p)
            if subtitle:
                paragraph(subtitle, style={
                    "fontSize": {"magnitude": 10, "unit": "PT"},
                    "foregroundColor": {"color": {"rgbColor": {"red": 0.91, "green": 0.47, "blue": 0.13}}},
                })
            for bullet in (exp.improved_bullets or []):
                if bullet:
                    paragraph("• " + bullet, style={"fontSize": {"magnitude": 10, "unit": "PT"}},
                              para_style={"indentFirstLine": {"magnitude": 0, "unit": "PT"},
                                          "indentStart": {"magnitude": 14, "unit": "PT"}})

    # ── Education ─────────────────────────────────────────────────────────────
    if data.education:
        section_heading("Education")
        for edu in data.education:
            paragraph(edu.school, style={"bold": True, "fontSize": {"magnitude": 11, "unit": "PT"}},
                      para_style={"spaceAbove": {"magnitude": 4, "unit": "PT"}})
            degree = ", ".join(p for p in [edu.degree, edu.field] if p)
            sub = " · ".join(p for p in [degree, edu.years] if p)
            if sub:
                paragraph(sub, style={"fontSize": {"magnitude": 10, "unit": "PT"}})

    # ── Certifications ────────────────────────────────────────────────────────
    if data.certifications:
        section_heading("Certifications")
        for cert in data.certifications:
            paragraph(cert.name, style={"bold": True, "fontSize": {"magnitude": 11, "unit": "PT"}},
                      para_style={"spaceAbove": {"magnitude": 4, "unit": "PT"}})
            sub = " · ".join(p for p in [cert.issuer, cert.date] if p)
            if sub:
                paragraph(sub, style={"fontSize": {"magnitude": 10, "unit": "PT"}})

    # ── Skills ────────────────────────────────────────────────────────────────
    if data.skills:
        section_heading("Skills")
        paragraph(", ".join(data.skills), style={"fontSize": {"magnitude": 10, "unit": "PT"}})

    # ── Languages ─────────────────────────────────────────────────────────────
    if data.languages:
        section_heading("Languages")
        for lang in data.languages:
            text = lang.language + (f" ({lang.proficiency})" if lang.proficiency else "")
            paragraph("• " + text, style={"fontSize": {"magnitude": 10, "unit": "PT"}})

    return requests


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

    # Write content via batchUpdate
    reqs = _build_resume_requests(request)
    if reqs:
        docs.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": reqs},
        ).execute()

    doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"
    return {"doc_url": doc_url}


@app.get("/health")
async def health():
    return {"status": "ok"}
