"""Pull {company, round} out of a messy subject line.

This is the one genuinely fuzzy job in the pipeline, so it is the one place
an LLM earns its keep.  Everything about it is best-effort: classification is
metadata for the timeline, it never gates the alert.  If NIM is cold, slow,
or down, we fall back to regex and the notification still goes out on time.
"""
import json
import re

import requests

from . import config

_NOISE = re.compile(r"^\s*(?:(?:fwd?|re|fw)\s*:\s*)+", re.I)

_ROUND_PATTERNS = [
    (re.compile(r"\bround\s*[-–—]?\s*(\d+)", re.I), lambda m: "Round " + m.group(1)),
    (re.compile(r"\bR(\d)\b"), lambda m: "Round " + m.group(1)),
    (re.compile(r"\bphase\s*[-–—]?\s*(\d+)", re.I), lambda m: "Phase " + m.group(1)),
]
_ROUND_KEYWORDS = [
    (r"\bonline\s+assessment\b|\bOA\b", "Online Assessment"),
    (r"\bpre[- ]?placement\s+talk\b|\bPPT\b", "Pre-Placement Talk"),
    (r"\bgroup\s+discussion\b|\bGD\b", "Group Discussion"),
    (r"\btechnical\s+interview\b|\bTR\b", "Technical Interview"),
    (r"\bHR\s+(?:round|interview)\b", "HR Interview"),
    (r"\bfinal\s+(?:list|select)", "Final Selection"),
    (r"\bshortlist|\bqualified|\bselected\b", "Shortlist"),
]

_PROMPT = """Extract the recruiting company and the selection round from this \
university placement email subject line.

Subject: {subject}

Reply with ONLY a JSON object, no prose, no markdown fence:
{{"company": "<company name, or null if absent>", "round": "<round label, or null>"}}

The round should be a short human label like "Round 2", "Online Assessment", \
"Technical Interview", or "Shortlist"."""


def heuristic(subject):
    """Regex-only pass. Reliable for the round, useless for the company."""
    clean = _NOISE.sub("", subject or "").strip()
    round_label = None
    for pattern, fmt in _ROUND_PATTERNS:
        m = pattern.search(clean)
        if m:
            round_label = fmt(m)
            break
    if not round_label:
        for pattern, label in _ROUND_KEYWORDS:
            if re.search(pattern, clean, re.I):
                round_label = label
                break
    return {"company": None, "round": round_label, "source": "heuristic"}


def _ask_nim(subject, timeout):
    resp = requests.post(
        config.NIM_BASE_URL.rstrip("/") + "/chat/completions",
        headers={"Authorization": "Bearer " + config.NIM_API_KEY},
        json={
            "model": config.NIM_MODEL,
            "messages": [{"role": "user", "content": _PROMPT.format(subject=subject)}],
            "temperature": 0,
            "max_tokens": 120,
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"].strip()
    # Models fence JSON even when told not to.
    content = re.sub(r"^```(?:json)?|```$", "", content, flags=re.M).strip()
    data = json.loads(content)
    return {
        "company": (data.get("company") or None),
        "round": (data.get("round") or None),
        "source": "nim",
    }


def classify(subject, timeout=25):
    """Best-effort {company, round, source}. Never raises."""
    fallback = heuristic(subject)
    if not config.NIM_API_KEY or not subject:
        return fallback
    try:
        result = _ask_nim(subject, timeout)
    except Exception as exc:
        fallback["error"] = str(exc)
        return fallback
    # Trust the regex over the model for the round -- it is exact when it hits.
    if fallback["round"] and not result["round"]:
        result["round"] = fallback["round"]
    return result
