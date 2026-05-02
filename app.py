import os
import logging
import requests
from flask import Flask, request
from twilio.rest import Client as TwilioClient
from twilio.twiml.messaging_response import MessagingResponse

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

AIRTABLE_HEADERS = {
    "Authorization": f"Bearer {AIRTABLE_TOKEN}",
    "Content-Type": "application/json",
}

PROJECT_MAP = {
    "vessel": "Vessel Club",
    "vessel club": "Vessel Club",
}

twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


def airtable_url(table_id):
    return f"https://api.airtable.com/v0/{AIRTABLE_BASE}/{table_id}"


def parse_sms(body):
    parts = [p.strip() for p in body.split(" - ")]
    if len(parts) < 3:
        raise ValueError("Invalid format. Use: Project - Scope - Sub - Urgency")

    project_key = parts[0].lower()
    project = PROJECT_MAP.get(project_key, parts[0])
    scope = parts[1]
    sub_name = parts[2]
    urgency_raw = parts[3].lower() if len(parts) >= 4 else "normal"
    urgency = "Urgent" if urgency_raw in ("urgent", "high", "asap") else "Normal"

    return {"project": project, "scope": scope, "sub_name": sub_name, "urgency": urgency}


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
