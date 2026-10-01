import json
import os
import re
import sys
import traceback
from datetime import datetime
from math import ceil
from pathlib import Path
from urllib.parse import urlencode, urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.planningportal.nsw.gov.au"
PROJECTS_URL = f"{BASE_URL}/major-projects/projects"

PLANNING_SLACK_WEBHOOK = os.environ["PLANNING_SLACK_WEBHOOK"]

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "planning_state.json"

TARGET_STATUSES = [
    "SEARs",
    "Exhibition",
    "Recommendation",
    "Determination",
]

TARGET_DEVELOPMENT_TYPES = [
    "Coal Mining",
    "Minerals Mining",
    "Extractive industries",
    "Gas supply",
    "Data Storage",
    "Electricity Generation - Other",
    "Waste collection, treatment and disposal",
]

ALLOWED_ASSESSMENT_TYPES = {
    "State Significant Development",
    "State Significant Infrastructure",
    "SSD Modifications",
    "SSI Modifications",
}

WASTE_TO_ENERGY_TERMS = [
    "energy from waste",
    "energy-from-waste",
    "waste to energy",
    "waste-to-energy",
    "energy recovery",
    "thermal treatment",
    "incinerator",
    "incineration",
    "efw",
    "wte",
]

REQUEST_HEADERS = {
    "User-Agent": "NSW-State-Significant-Planning-Monitor/1.0"
}

RESULTS_PER_PAGE = 12
MAX_ACTIVE_PAGES = 8


def log(message):
    print(f"{datetime.now().isoformat(timespec='seconds')} - {message}")


def clean_text(value):
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def load_state():
    if not STATE_FILE.exists():
        return {"initialised": False, "projects": {}}

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)

        data.setdefault("initialised", False)
        data.setdefault("projects", {})
        return data

    except (json.JSONDecodeError, OSError):
        return {"initialised": False, "projects": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=2, sort_keys=True)


def is_waste_to_energy(project):
    haystack = " ".join(
        [
            project.get("title", ""),
            project.get("address", ""),
            project.get("development_type", ""),
        ]
    ).lower()

    return any(term in haystack for term in WASTE_TO_ENERGY_TERMS)


def parse_listing_page(html, development_type):
    soup = BeautifulSoup(html, "html.parser")
    page_text = clean_text(soup.get_text(" ", strip=True))

    total_match = re.search(r"Showing:\s*([\d,]+)\s+results", page_text, re.I)
    total_results = (
        int(total_match.group(1).replace(",", ""))
        if total_match
        else None
    )

    projects = []

    for link in soup.find_all("a", href=True):
        href = link.get("href", "")
        title = clean_text(link.get_text(" ", strip=True))

        if not title or "/major-projects/projects/" not in href:
            continue

        container = link
        for _ in range(7):
            if container.parent is None:
                break
            container = container.parent
            container_text = clean_text(container.get_text(" ", strip=True))

            if (
                re.search(r"\b(SSD|SSI|DA|MP)[A-Za-z0-9\-_/]*\b", container_text, re.I)
                and len(container_text) > len(title)
            ):
                break

        lines = [
            clean_text(x)
            for x in container.stripped_strings
            if clean_text(x)
        ]

        project_id = ""
        status = ""
        assessment_type = ""
        lga = ""
        address = ""

        for line in lines:
            if not project_id:
                match = re.search(
                    r"\b(SSD|SSI|DA|MP)[A-Za-z0-9\-_/]*\b",
                    line,
                    re.I,
                )
                if match:
                    project_id = match.group(0)

            if not status and line in TARGET_STATUSES:
                status = line

            if not assessment_type and line in ALLOWED_ASSESSMENT_TYPES:
                assessment_type = line

        if assessment_type and assessment_type in lines:
            idx = lines.index(assessment_type)
            if idx + 1 < len(lines):
                lga = lines[idx + 1]

        if title in lines:
            idx = lines.index(title)
            if idx + 1 < len(lines):
                candidate = lines[idx + 1]
                if candidate != project_id:
                    address = candidate

        if not all([project_id, status, assessment_type]):
            continue

        projects.append(
            {
                "id": project_id,
                "status": status,
                "assessment_type": assessment_type,
                "development_type": development_type,
                "lga": lga or "Not listed",
                "title": title,
                "address": address,
                "url": urljoin(BASE_URL, href),
            }
        )

    unique = {project["id"]: project for project in projects}
    return list(unique.values()), total_results


def fetch_filtered_projects(development_type, status):
    found = {}
    page = 0

    while True:
        params = {
            "case_type": "All",
            "development_type": development_type,
            "industry_type": "All",
            "lga": "All",
            "page": page,
            "status": status,
        }

        response = requests.get(
            PROJECTS_URL,
            params=params,
            headers=REQUEST_HEADERS,
            timeout=30,
        )
        response.raise_for_status()

        projects, total_results = parse_listing_page(
            response.text,
            development_type,
        )

        for project in projects:
            if project["status"] != status:
                continue

            if project["assessment_type"] not in ALLOWED_ASSESSMENT_TYPES:
                continue

            if (
                development_type == "Waste collection, treatment and disposal"
                and not is_waste_to_energy(project)
            ):
                continue

            found[project["id"]] = project

        if total_results is None:
            break

        total_pages = max(1, ceil(total_results / RESULTS_PER_PAGE))
        page += 1

        if page >= min(total_pages, MAX_ACTIVE_PAGES):
            break

    return list(found.values())


def send_to_slack(project):
    payload = {
        "text": f"NSW Planning Portal - {project['status']}: {project['title']}",
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"NSW PLANNING PORTAL - {project['status'].upper()}",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{project['title']}*\n`{project['id']}`",
                },
            },
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*Development type:*\n{project['development_type']}",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Assessment type:*\n{project['assessment_type']}",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*LGA:*\n{project['lga']}",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Milestone:*\n{project['status']}",
                    },
                ],
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "Open project",
                            "emoji": True,
                        },
                        "url": project["url"],
                        "action_id": "open_planning_project",
                    }
                ],
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "NSW State Significant Planning Monitor",
                    }
                ],
            },
        ],
    }

    response = requests.post(
        PLANNING_SLACK_WEBHOOK,
        json=payload,
        timeout=20,
    )
    response.raise_for_status()


def main():
    log("Planning monitor started.")
    state = load_state()

    current_projects = {}

    for development_type in TARGET_DEVELOPMENT_TYPES:
        for status in TARGET_STATUSES:
            log(f"Checking {development_type} / {status}")

            try:
                projects = fetch_filtered_projects(
                    development_type,
                    status,
                )
            except Exception as error:
                log(
                    f"ERROR checking {development_type} / {status}: {error}"
                )
                continue

            for project in projects:
                key = f"{project['id']}|{project['status']}"
                current_projects[key] = project

    log(
        f"Found {len(current_projects)} watched "
        "project/milestone matches."
    )

    if not state.get("initialised"):
        for project in current_projects.values():
            record = state["projects"].setdefault(
                project["id"],
                {
                    "title": project["title"],
                    "url": project["url"],
                    "development_type": project["development_type"],
                    "assessment_type": project["assessment_type"],
                    "lga": project["lga"],
                    "notified_milestones": [],
                },
            )

            milestone = project["status"]

            if milestone not in record["notified_milestones"]:
                record["notified_milestones"].append(milestone)

        state["initialised"] = True
        save_state(state)
        log("Initial baseline saved. No Slack alerts sent.")
        return

    for project in current_projects.values():
        record = state["projects"].setdefault(
            project["id"],
            {
                "title": project["title"],
                "url": project["url"],
                "development_type": project["development_type"],
                "assessment_type": project["assessment_type"],
                "lga": project["lga"],
                "notified_milestones": [],
            },
        )

        record["title"] = project["title"]
        record["url"] = project["url"]
        record["development_type"] = project["development_type"]
        record["assessment_type"] = project["assessment_type"]
        record["lga"] = project["lga"]

        milestone = project["status"]

        if milestone in record["notified_milestones"]:
            continue

        try:
            log(
                f"Sending {project['id']} / "
                f"{milestone} to Slack."
            )

            send_to_slack(project)

            record["notified_milestones"].append(milestone)
            save_state(state)

        except Exception as error:
            log(
                f"ERROR sending {project['id']} / "
                f"{milestone}: {error}"
            )
            log(traceback.format_exc())

    save_state(state)
    log("Planning monitor completed.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log(traceback.format_exc())
        sys.exit(1)
