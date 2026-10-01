import json
import os
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.ipcn.nsw.gov.au"
CASES_URL = f"{BASE_URL}/cases"

PLANNING_SLACK_WEBHOOK = os.environ["PLANNING_SLACK_WEBHOOK"]

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "ipc_state.json"

REQUEST_HEADERS = {
    "User-Agent": "NSW-IPC-Planning-Monitor/1.0"
}

# All "Mining and resources" cases are in scope.
# Other IPC categories are included only where title/description indicates
# one of our chosen project classes.
INCLUDE_TERMS = [
    "coal",
    "mine",
    "mining",
    "mineral",
    "quarry",
    "extractive",
    "gas-fired",
    "gas fired",
    "gas pipeline",
    "gas supply",
    "natural gas",
    "data centre",
    "data center",
    "battery energy storage",
    "battery storage",
    "bess",
    "pumped hydro",
    "energy from waste",
    "energy-from-waste",
    "waste to energy",
    "waste-to-energy",
    "thermal treatment",
    "incinerator",
    "incineration",
]

# Avoid capturing wind/solar projects simply because their descriptions
# mention an associated battery.
EXCLUDED_TITLE_TERMS = [
    "wind farm",
    "solar farm",
    "solar project",
]

MILESTONE_LABELS = {
    "hearing_request": "PUBLIC HEARING REQUEST",
    "referral": "REFERRAL",
    "consultation": "CONSULTATION / HEARING",
    "decision": "DECISION",
}


def log(message):
    print(f"{datetime.now().isoformat(timespec='seconds')} - {message}")


def clean_text(value):
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def load_state():
    if not STATE_FILE.exists():
        return {"initialised": False, "cases": {}}

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
        data.setdefault("initialised", False)
        data.setdefault("cases", {})
        return data
    except (json.JSONDecodeError, OSError):
        return {"initialised": False, "cases": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=2, sort_keys=True)


def is_in_scope(case):
    category = case.get("category", "").lower()
    title = case.get("title", "").lower()
    description = case.get("description", "").lower()
    haystack = f"{title} {description}"

    if category == "mining and resources":
        return True

    if any(term in title for term in EXCLUDED_TITLE_TERMS):
        return False

    return any(term in haystack for term in INCLUDE_TERMS)


def parse_cases_page(html):
    soup = BeautifulSoup(html, "html.parser")
    cases = []

    # Case cards contain links to /cases/<slug>.
    for link in soup.find_all("a", href=True):
        href = link["href"]

        if not re.match(r"^/cases/[^/?#]+/?$", href):
            continue

        title = clean_text(link.get_text(" ", strip=True))
        if not title:
            continue

        container = link
        for _ in range(7):
            if container.parent is None:
                break
            container = container.parent
            text = clean_text(container.get_text(" ", strip=True))

            if re.search(r"\b(SSD|SSI|DA|MP|PP)[A-Za-z0-9\-_/& ]*\b", text):
                break

        lines = [
            clean_text(x)
            for x in container.stripped_strings
            if clean_text(x)
        ]

        text = clean_text(container.get_text(" ", strip=True))

        project_id = ""
        id_match = re.search(
            r"\b(?:SSD|SSI|DA|MP|PP)[A-Za-z0-9\-_/]*(?:\s*&\s*(?:SSD|SSI|DA|MP|PP)[A-Za-z0-9\-_/]*)?",
            text,
            re.I,
        )
        if id_match:
            project_id = id_match.group(0)

        category = ""
        # IPC category labels are short and normally precede the title.
        if title in lines:
            idx = lines.index(title)
            if idx > 0:
                category = lines[idx - 1]

        status = ""
        status_terms = [
            "Public Hearing request received",
            "In progress",
            "Determined – approved",
            "Determined - approved",
            "Determined – refused",
            "Determined - refused",
            "Advice provided",
        ]
        for candidate in status_terms:
            if candidate in text:
                status = candidate
                break

        description = ""
        if project_id and project_id in lines:
            idx = lines.index(project_id)
            # Usually status then description follow.
            for candidate in lines[idx + 1:]:
                if candidate == status:
                    continue
                if candidate.lower().startswith("key dates"):
                    continue
                if candidate.lower().startswith("submissions end"):
                    continue
                if candidate.lower().startswith("public hearing"):
                    continue
                if len(candidate) >= 40:
                    description = candidate
                    break

        url = urljoin(BASE_URL, href)

        case = {
            "title": title,
            "project_id": project_id or "Not listed",
            "category": category or "Not listed",
            "status": status or "Not listed",
            "description": description,
            "url": url,
        }

        if is_in_scope(case):
            cases.append(case)

    # Deduplicate by case URL.
    unique = {case["url"]: case for case in cases}
    return list(unique.values())


def fetch_all_current_cases():
    all_cases = {}
    page = 0

    while page < 8:
        response = requests.get(
            CASES_URL,
            params={"page": page},
            headers=REQUEST_HEADERS,
            timeout=30,
        )
        response.raise_for_status()

        page_cases = parse_cases_page(response.text)

        if not page_cases and page > 0:
            break

        for case in page_cases:
            all_cases[case["url"]] = case

        # The most relevant active cases are on the early pages; eight pages
        # also gives enough historical context to baseline recent decisions.
        page += 1

    return list(all_cases.values())


def parse_case_progress(case):
    response = requests.get(
        case["url"],
        headers=REQUEST_HEADERS,
        timeout=30,
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    page_text = clean_text(soup.get_text(" ", strip=True))
    lower = page_text.lower()

    milestones = []

    if (
        "public hearing request received" in lower
        or "ministerial request for a public hearing" in lower
        or "ministerial request to hold a public hearing" in lower
    ):
        milestones.append("hearing_request")

    if "referral received" in lower:
        milestones.append("referral")

    # One combined consultation/hearing milestone. It fires when submissions
    # open or when a public hearing/meeting is actually announced.
    if (
        "submissions open" in lower
        or "notice - public hearing" in lower
        or re.search(r"\bpublic hearing day one\b", lower)
        or re.search(r"\bpublic hearing\s+\d", lower)
        or "public meeting" in lower
    ):
        milestones.append("consultation")

    decision = ""
    if "determined – approved" in lower or "determined - approved" in lower:
        milestones.append("decision")
        decision = "Approved"
    elif "determined – refused" in lower or "determined - refused" in lower:
        milestones.append("decision")
        decision = "Refused"
    elif "case outcome" in lower and case.get("status", "").lower().startswith("determined"):
        milestones.append("decision")
        decision = case["status"].replace("Determined", "").strip(" –-") or "Determined"

    # Extract key dates if visibly present.
    dates = []
    for pattern in [
        r"Submissions end\s+([0-9]{1,2}/[0-9]{1,2}/[0-9]{4}[^A-Za-z]{0,10}[0-9:.apm ]*)",
        r"Public hearing\s+([0-9]{1,2}/[0-9]{1,2}/[0-9]{4}[^A-Za-z]{0,10}[0-9:.apm ]*)",
    ]:
        for match in re.findall(pattern, page_text, re.I):
            value = clean_text(match)
            if value and value not in dates:
                dates.append(value)

    case["milestones"] = milestones
    case["decision"] = decision
    case["key_dates"] = dates[:4]
    return case


def send_to_slack(case, milestone):
    label = MILESTONE_LABELS[milestone]

    fields = [
        {
            "type": "mrkdwn",
            "text": f"*IPC category:*\n{case['category']}",
        },
        {
            "type": "mrkdwn",
            "text": f"*Project ID:*\n{case['project_id']}",
        },
    ]

    if milestone == "decision" and case.get("decision"):
        fields.append(
            {
                "type": "mrkdwn",
                "text": f"*Outcome:*\n{case['decision']}",
            }
        )

    if case.get("key_dates"):
        fields.append(
            {
                "type": "mrkdwn",
                "text": "*Key date(s):*\n" + "\n".join(case["key_dates"]),
            }
        )

    payload = {
        "text": f"Independent Planning Commission - {label}: {case['title']}",
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"IPC - {label}",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{case['title']}*",
                },
            },
            {
                "type": "section",
                "fields": fields,
            },
        ],
    }

    if case.get("description"):
        payload["blocks"].append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": case["description"][:500],
                },
            }
        )

    payload["blocks"].extend(
        [
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "Open IPC case",
                            "emoji": True,
                        },
                        "url": case["url"],
                        "action_id": "open_ipc_case",
                    }
                ],
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "NSW Independent Planning Commission Monitor",
                    }
                ],
            },
        ]
    )

    response = requests.post(
        PLANNING_SLACK_WEBHOOK,
        json=payload,
        timeout=20,
    )
    response.raise_for_status()


def main():
    log("IPC monitor started.")
    state = load_state()

    cases = fetch_all_current_cases()
    log(f"Found {len(cases)} in-scope IPC cases.")

    detailed_cases = []

    for case in cases:
        try:
            detailed = parse_case_progress(case)
            detailed_cases.append(detailed)
            log(
                f"{detailed['title']} - "
                f"milestones: {', '.join(detailed['milestones']) or 'none'}"
            )
        except Exception as error:
            log(f"ERROR reading {case['url']}: {error}")

    # First run establishes a baseline without sending historical alerts.
    if not state.get("initialised"):
        for case in detailed_cases:
            record = state["cases"].setdefault(
                case["url"],
                {
                    "title": case["title"],
                    "project_id": case["project_id"],
                    "category": case["category"],
                    "notified_milestones": [],
                },
            )

            record["notified_milestones"] = sorted(
                set(record["notified_milestones"] + case["milestones"])
            )

        state["initialised"] = True
        save_state(state)
        log("Initial IPC baseline saved. No Slack alerts sent.")
        return

    for case in detailed_cases:
        record = state["cases"].setdefault(
            case["url"],
            {
                "title": case["title"],
                "project_id": case["project_id"],
                "category": case["category"],
                "notified_milestones": [],
            },
        )

        record["title"] = case["title"]
        record["project_id"] = case["project_id"]
        record["category"] = case["category"]

        for milestone in case["milestones"]:
            if milestone in record["notified_milestones"]:
                continue

            try:
                log(f"Sending {case['title']} / {milestone} to Slack.")
                send_to_slack(case, milestone)

                record["notified_milestones"].append(milestone)
                record["notified_milestones"] = sorted(
                    set(record["notified_milestones"])
                )
                save_state(state)

            except Exception as error:
                log(
                    f"ERROR sending {case['title']} / {milestone}: {error}"
                )
                log(traceback.format_exc())

    save_state(state)
    log("IPC monitor completed.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log(traceback.format_exc())
        sys.exit(1)
