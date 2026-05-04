import os
import re
import json
import logging
import requests
from flask import Flask, request
from twilio.rest import Client as TwilioClient
from twilio.twiml.messaging_response import MessagingResponse
import anthropic

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = app.logger

TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_PHONE_NUMBER = os.environ["TWILIO_PHONE_NUMBER"]

AIRTABLE_TOKEN = os.environ["AIRTABLE_ACCESS_TOKEN"]
AIRTABLE_BASE = os.environ["AIRTABLE_BASE_ID"]
CO_TABLE = os.environ["CHANGE_ORDERS_TABLE_ID"]
SUBS_TABLE = os.environ["SUBCONTRACTORS_TABLE_ID"]
PROJECTS_TABLE = os.environ["PROJECTS_TABLE_ID"]

SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

AIRTABLE_HEADERS = {
    "Authorization": f"Bearer {AIRTABLE_TOKEN}",
    "Content-Type": "application/json",
}

PROJECT_MAP = {
    "vessel": "Vessel Club",
    "vessel club": "Vessel Club",
}

URGENCY_KEYWORDS = {"urgent", "asap", "emergency", "high", "rush", "critical", "now"}

twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None


def airtable_url(table_id):
    return f"https://api.airtable.com/v0/{AIRTABLE_BASE}/{table_id}"


def _normalize_project(raw):
    """Map common project name variations to canonical names."""
    return PROJECT_MAP.get(raw.strip().lower(), raw.strip())


def _detect_urgency(text):
    """Return 'Urgent' if any urgency keyword appears anywhere in the text."""
    words = set(re.findall(r"\w+", text.lower()))
    return "Urgent" if words & URGENCY_KEYWORDS else "Normal"


def _parse_structured(body):
    """
    Try to split the message on common delimiters and extract 3-4 fields.
    Accepts: ' - ', ' / ', ' | ', ',', or plain '-'.
    Returns parsed dict or None if it can't find at least 3 fields.
    """
    for sep in [r"\s+-\s+", r"\s*/\s*", r"\s*\|\s*", r",\s*"]:
        parts = [p.strip() for p in re.split(sep, body) if p.strip()]
        if len(parts) >= 3:
            project = _normalize_project(parts[0])
            scope = parts[1]
            sub_name = parts[2]
            # Urgency can be the 4th field OR anywhere in the full message
            urgency = _detect_urgency(parts[3] if len(parts) >= 4 else body)
            return {"project": project, "scope": scope, "sub_name": sub_name, "urgency": urgency}
    return None


def _parse_with_ai(body):
    """
    Fall back to Claude Haiku to extract fields from free-form text.
    Returns parsed dict or raises ValueError if extraction fails.
    """
    if not anthropic_client:
        raise ValueError("Message format not recognized. Use: Project - Scope - Sub - Urgency")

    known_projects = list(set(PROJECT_MAP.values()))
    prompt = f"""Extract the four fields of a construction change order from this SMS message.

SMS: "{body}"

Known project names: {known_projects}

Return ONLY a JSON object with exactly these keys:
- "project": the job site / project name (match to a known project if possible, otherwise use what was written)
- "scope": description of the extra work or change
- "sub_name": the subcontractor company name
- "urgency": either "Urgent" or "Normal"

If a field is genuinely impossible to determine, set it to null.
Return ONLY valid JSON, no explanation."""

    response = anthropic_client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    # Strip markdown code fences if present
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("Could not understand your message. Use: Project - Scope - Sub - Urgency")

    missing = [k for k in ("project", "scope", "sub_name") if not result.get(k)]
    if missing:
        raise ValueError(f"Could not identify {', '.join(missing)} in your message. Use: Project - Scope - Sub - Urgency")

    result["urgency"] = result.get("urgency") or _detect_urgency(body)
    result["project"] = _normalize_project(result["project"])
    log.info("AI parser extracted: %s", result)
    return result


def parse_sms(body):
    """
    Two-stage parser: try structured split first, fall back to AI.
    """
    result = _parse_structured(body)
    if result:
        log.info("Structured parser matched: %s", result)
        return result

    log.info("Structured parse failed, trying AI fallback for: %s", body)
    return _parse_with_ai(body)


def find_or_create_sub(name):
    params = {"filterByFormula": f"LOWER({{Sub Name}})=LOWER(\"{name}\")"}
    resp = requests.get(airtable_url(SUBS_TABLE), headers=AIRTABLE_HEADERS, params=params)
    resp.raise_for_status()
    records = resp.json().get("records", [])
    if records:
        return records[0]["id"]

    resp = requests.post(
        airtable_url(SUBS_TABLE),
        headers=AIRTABLE_HEADERS,
        json={"records": [{"fields": {"Sub Name": name, "Trade": "General"}}]},
    )
    resp.raise_for_status()
    new_id = resp.json()["records"][0]["id"]
    log.info("Created new subcontractor: %s (%s)", name, new_id)
    return new_id


def find_project(name):
    params = {"filterByFormula": f"LOWER({{Project Name}})=LOWER(\"{name}\")"}
    resp = requests.get(airtable_url(PROJECTS_TABLE), headers=AIRTABLE_HEADERS, params=params)
    resp.raise_for_status()
    records = resp.json().get("records", [])
    if not records:
        raise ValueError(f"Project '{name}' not found in Airtable")
    return records[0]["id"]


def create_change_order(project_name, scope, sub_id, urgency, raw_sms):
    fields = {
        "Project": project_name,
        "Scope Description": scope,
        "Subcontractor": [sub_id],
        "Urgency": urgency,
        "Raw SMS": raw_sms,
    }
    resp = requests.post(
        airtable_url(CO_TABLE),
        headers=AIRTABLE_HEADERS,
        json={"records": [{"fields": fields}]},
    )
    resp.raise_for_status()
    return resp.json()["records"][0]["id"]


def send_sms(to, body):
    twilio_client.messages.create(body=body, from_=TWILIO_PHONE_NUMBER, to=to)


def alert_slack(text):
    if not SLACK_WEBHOOK:
        return
    try:
        requests.post(SLACK_WEBHOOK, json={"text": text}, timeout=5)
    except Exception as e:
        log.error("Slack alert failed: %s", e)


@app.route("/")
def health():
    return "OK"


@app.route("/sms", methods=["POST"])
def sms_webhook():
    sender = request.form.get("From", "")
    body = request.form.get("Body", "").strip()
    msg_sid = request.form.get("MessageSid", "")
    log.info("SMS from %s [%s]: %s", sender, msg_sid, body)

    try:
        parsed = parse_sms(body)
        sub_id = find_or_create_sub(parsed["sub_name"])
        find_project(parsed["project"])
        co_id = create_change_order(
            parsed["project"], parsed["scope"], sub_id, parsed["urgency"], body
        )
        short_id = co_id[-6:].upper()
        reply = f"✅ CO-{short_id} logged. Quote request sent to {parsed['sub_name']}."
        send_sms(sender, reply)
        log.info("Change order created: %s", co_id)

    except ValueError as e:
        reply = f"⚠️ Error: {e}. Use format: Project - Scope - Sub - Urgency"
        send_sms(sender, reply)
        alert_slack(f"🚨 SMS parse error from {sender}: {body}\nError: {e}")
        log.warning("Parse error: %s", e)

    except Exception as e:
        reply = "⚠️ Something went wrong logging your CO. Try again or check Slack for details."
        send_sms(sender, reply)
        alert_slack(f"🚨 SMS webhook error from {sender}: {body}\nError: {type(e).__name__}: {e}")
        log.exception("Webhook error")

    twiml = MessagingResponse()
    return str(twiml), 200, {"Content-Type": "text/xml"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
