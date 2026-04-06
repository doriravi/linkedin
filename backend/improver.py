import json
import os

import anthropic


def improve_profile(profile_data: dict, extra_context: str = "") -> dict:
    """Use Claude to generate improvements for a LinkedIn profile."""
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    profile_text = json.dumps(profile_data, indent=2)

    extra_section = ""
    if extra_context and extra_context.strip():
        extra_section = f"""
Additional profile information provided by the user (use this as the authoritative source — it is more detailed and accurate than the scraped data above):
{extra_context.strip()}
"""

    prompt = f"""You are an expert LinkedIn profile coach and career strategist. Analyze this LinkedIn profile and provide specific, high-impact improvements.

Scraped LinkedIn Data:
{profile_text}
{extra_section}

Return your improvements as valid JSON with this exact structure:
{{
  "headline": "A compelling, keyword-rich headline (max 220 chars). Format: Role | Value Proposition | Niche or Industry.",
  "about": "A rewritten About/Summary section in first person (3-5 paragraphs). Open with a hook, highlight top achievements with numbers, include relevant keywords for ATS, end with a call to action.",
  "experience": [
    {{
      "title": "exact job title from original",
      "company": "exact company name from original",
      "improved_bullets": [
        "Action verb + what you did + qualitative impact (no invented numbers)",
        "Action verb + what you did + qualitative impact (no invented numbers)",
        "Action verb + what you did + qualitative impact (no invented numbers)"
      ]
    }}
  ],
  "skills": ["skill1", "skill2", "...up to 20 high-value skills relevant to their field, including missing ATS keywords"]
}}

Guidelines:
- Headline: punchy, specific, keyword-rich. Avoid generic phrases like "Passionate professional".
- About: first-person, achievement-focused, include industry keywords naturally.
- Experience bullets: start with strong action verbs (Led, Built, Grew, Reduced, Drove). Describe what was done and the impact — but do NOT invent any numbers, percentages, dollar amounts, or metrics that are not stated in the original profile. Keep all claims qualitative and grounded in what is actually known.
- Skills: keep strong existing skills, add high-demand skills they are likely missing based on their role/industry.

Return ONLY the JSON object, no markdown fences, no commentary."""

    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=4096,
        thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": prompt}],
    )

    # Extract text block (thinking blocks are skipped automatically)
    text = next(block.text for block in response.content if block.type == "text")

    # Strip accidental markdown fences
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
    if text.endswith("```"):
        text = text.rsplit("```", 1)[0]
    text = text.strip()

    return json.loads(text)
