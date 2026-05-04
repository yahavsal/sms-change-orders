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
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL", "#construction")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NOTION_TODOS_DB = "292fbbfc-7b7a-80ce-ba50-eb32c0d71607"

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
    """Two-stage parser: try structured split first, fall back to AI."""
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
    """Post a plain-text alert via webhook (used for errors)."""
    if not SLACK_WEBHOOK:
        return
    try:
        requests.post(SLACK_WEBHOOK, json={"text": text}, timeout=5)
    except Exception as e:
        log.error("Slack alert failed: %s", e)


def post_co_to_slack(co_id, project, scope, sub_name, urgency, sender_phone):
    """Post an interactive CO notification to #construction with Approve/Deny/Review buttons."""
    if not SLACK_BOT_TOKEN:
        alert_slack(f"New CO logged: {project} - {scope} ({sub_name}) [{urgency}]")
        return

    urgency_emoji = "🚨" if urgency == "Urgent" else "📋"
    value = f"{co_id}|{sender_phone}"

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"{urgency_emoji} *New Change Order*\n"
                    f"*Project:* {project}\n"
                    f"*Scope:* {scope}\n"
                    f"*Sub:* {sub_name}\n"
                    f"*Urgency:* {urgency}"
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ Approve"},
                    "action_id": "co_approve",
                    "value": value,
                    "style": "primary",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "❌ Deny"},
                    "action_id": "co_deny",
                    "value": value,
                    "style": "danger",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🔍 Needs Review"},
                    "action_id": "co_review",
                    "value": value,
                },
            ],
        },
    ]

    try:
        resp = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={
                "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
                "Content-Type": "application/json",
            },
            json={
                "channel": SLACK_CHANNEL,
                "blocks": blocks,
                "text": f"New CO: {project} — {scope}",
            },
            timeout=5,
        )
        data = resp.json()
        if not data.get("ok"):
            log.error("Slack bot post failed: %s", data.get("error"))
            alert_slack(f"New CO logged: {project} - {scope} ({sub_name}) [{urgency}]")
    except Exception as e:
        log.error("Slack bot error: %s", e)
        alert_slack(f"New CO logged: {project} - {scope} ({sub_name}) [{urgency}]")


def update_co_status(co_id, status):
    """Update CO status in Airtable."""
    resp = requests.patch(
        f"{airtable_url(CO_TABLE)}/{co_id}",
        headers=AIRTABLE_HEADERS,
        json={"fields": {"Status": status}},
    )
    resp.raise_for_status()
    return resp.json()


def create_notion_task(co_id, project, scope, sub_name):
    """Create a Triage task in Notion ToDos DB."""
    if not NOTION_TOKEN:
        log.warning("NOTION_TOKEN not set, skipping Notion task creation")
        return None

    try:
        resp = requests.post(
            "https://api.notion.com/v1/pages",
            headers={
                "Authorization": f"Bearer {NOTION_TOKEN}",
                "Content-Type": "application/json",
                "Notion-Version": "2022-06-28",
            },
            json={
                "parent": {"database_id": NOTION_TODOS_DB},
                "properties": {
                    "Task name": {
                        "title": [{"text": {"content": f"Review CO: {scope} — {sub_name}"}}]
                    },
                    "Status": {"status": {"name": "Triage"}},
                    "Description": {
                        "rich_text": [
                            {
                                "text": {
                                    "content": (
                                        f"Project: {project}\n"
                                        f"Scope: {scope}\n"
                                        f"Sub: {sub_name}\n"
                                        f"Airtable CO ID: {co_id}"
                                    )
                                }
                            }
                        ]
                    },
                },
            },
            timeout=10,
        )
        resp.raise_for_status()
        notion_id = resp.json()["id"]
        log.info("Created Notion task: %s", notion_id)
        return notion_id
    except Exception as e:
        log.error("Notion task creation failed: %s", e)
        return None


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
        post_co_to_slack(
            co_id,
            parsed["project"],
            parsed["scope"],
            parsed["sub_name"],
            parsed["urgency"],
            sender,
        )
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


@app.route("/slack/actions", methods=["POST"])
def slack_actions():
    """Handle Slack interactive button clicks (Approve / Deny / Needs Review)."""
    try:
        raw_payload = request.form.get("payload", "")
        if not raw_payload:
            return "Bad request", 400

        payload = json.loads(raw_payload)
        action = payload["actions"][0]
        action_id = action["action_id"]
        value = action["value"]
        response_url = payload.get("response_url", "")
        user_name = payload.get("user", {}).get("name", "someone")

        # Value format: co_id|sender_phone
        parts = value.split("|", 1)
        co_id = parts[0]
        sender_phone = parts[1] if len(parts) > 1 else ""

        # Fetch CO details from Airtable
        co_resp = requests.get(f"{airtable_url(CO_TABLE)}/{co_id}", headers=AIRTABLE_HEADERS)
        co_resp.raise_for_status()
        fields = co_resp.json().get("fields", {})
        project = fields.get("Project", "Unknown project")
        scope = fields.get("Scope Description", "")
        sub_ids = fields.get("Subcontractor", [])

        if action_id == "co_approve":
            update_co_status(co_id, "Done")
            if sender_phone:
                send_sms(
                    sender_phone,
                    f"✅ Your change order has been approved. Project: {project} — {scope}. We'll follow up on next steps.",
                )
            reply_text = f"✅ *Approved* by @{user_name}"

        elif action_id == "co_deny":
            update_co_status(co_id, "Denied")
            if sender_phone:
                send_sms(
                    sender_phone,
                    f"❌ Your change order has been denied. Project: {project} — {scope}. Contact your project manager for details.",
                )
            reply_text = f"❌ *Denied* by @{user_name}"

        elif action_id == "co_review":
            update_co_status(co_id, "In progress")
            sub_name = "Unknown Sub"
            if sub_ids:
                sub_resp = requests.get(
                    f"{airtable_url(SUBS_TABLE)}/{sub_ids[0]}", headers=AIRTABLE_HEADERS
                )
                if sub_resp.ok:
                    sub_name = sub_resp.json().get("fields", {}).get("Sub Name", "Unknown Sub")
            create_notion_task(co_id, project, scope, sub_name)
            reply_text = f"🔍 *Flagged for review* by @{user_name} — Notion task created"

        else:
            return "Unknown action", 400

        # Replace the Slack message with a summary (removes the buttons)
        if response_url:
            requests.post(
                response_url,
                json={
                    "replace_original": True,
                    "text": f"*CO:* {project} — {scope}\n{reply_text}",
                },
                timeout=5,
            )

        return "", 200

    except Exception as e:
        log.exception("Slack actions error")
        return "Error", 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
