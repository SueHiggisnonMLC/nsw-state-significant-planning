import json
import os
import re
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

BASE_URL = "https://www.planningportal.nsw.gov.au"
PROJECTS_URL = f"{BASE_URL}/major-projects/projects"

PLANNING_SLACK_WEBHOOK = os.environ["PLANNING_SLACK_WEBHOOK"]

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "planning_state.json"

# We deliberately stop at Recommendation.
TARGET_STATUSES = [
    "SEARs",
    "Exhibition",
    "Recommendation",
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
    "User-Agent": "NSW-State-Significant-Planning-Monitor/2.0"
}


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


def build_driver():
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    return webdriver.Chrome(options=options)


def is_waste_to_energy(project):
    haystack = " ".join(
        [
            project.get("title", ""),
            project.get("address", ""),
            project.get("development_type", ""),
        ]
    ).lower()

    return any(term in haystack for term in WASTE_TO_ENERGY_TERMS)


def extract_project_from_card(card, development_type, expected_status):
    text = clean_text(card.text)
    lines = [
        clean_text(line)
        for line in card.text.splitlines()
        if clean_text(line)
    ]

    project_id = ""
    assessment_type = ""
    title = ""
    lga = ""
    address = ""

    project_id_match = re.search(
        r"\b(SSD|SSI|DA|MP)[A-Za-z0-9\-_/]*\b",
        text,
        re.I,
    )
    if project_id_match:
        project_id = project_id_match.group(0)

    for assessment in ALLOWED_ASSESSMENT_TYPES:
        if assessment in lines or assessment in text:
            assessment_type = assessment
            break

    # Prefer a heading/link as the title.
    try:
        title_element = card.find_element(By.CSS_SELECTOR, "h2, h3, h4, a")
        title = clean_text(title_element.text)
    except Exception:
        pass

    # If the first clickable text was "Read more", find a better title.
    if not title or title.lower() == "read more":
        for line in lines:
            if (
                line != project_id
                and line != expected_status
                and line not in ALLOWED_ASSESSMENT_TYPES
                and line.lower() != "read more"
                and len(line) > 5
            ):
                title = line
                break

    if assessment_type and assessment_type in lines:
        idx = lines.index(assessment_type)
        if idx + 1 < len(lines):
            lga = lines[idx + 1]

    if title and title in lines:
        idx = lines.index(title)
        if idx + 1 < len(lines):
            candidate = lines[idx + 1]
            if candidate != project_id:
                address = candidate

    url = ""
    try:
        links = card.find_elements(By.CSS_SELECTOR, "a[href*='/major-projects/projects/']")
        for link in links:
            href = link.get_attribute("href")
            if href and href.rstrip("/") != PROJECTS_URL.rstrip("/"):
                url = href
                break
    except Exception:
        pass

    if not all([project_id, title, assessment_type, url]):
        return None

    return {
        "id": project_id,
        "status": expected_status,
        "assessment_type": assessment_type,
        "development_type": development_type,
        "lga": lga or "Not listed",
        "title": title,
        "address": address,
        "url": urljoin(BASE_URL, url),
    }


def fetch_filtered_projects(driver, development_type, status):
    params = (
        f"?case_type=All"
        f"&development_type={requests.utils.quote(development_type)}"
        f"&industry_type=All"
        f"&lga=All"
        f"&page=0"
        f"&status={requests.utils.quote(status)}"
    )

    url = PROJECTS_URL + params
    log(f"Opening {development_type} / {status}")

    driver.get(url)

    # Wait until the results area has finished rendering.
    WebDriverWait(driver, 25).until(
        lambda d: "Loading" not in d.find_element(By.TAG_NAME, "body").text
    )

    time.sleep(2)

    # Locate cards by finding project links, then walking up to a useful parent.
    project_links = driver.find_elements(
        By.CSS_SELECTOR,
        "a[href*='/major-projects/projects/']"
    )

    cards = []
    seen_card_text = set()

    for link in project_links:
        try:
            card = link
            for _ in range(7):
                parent = card.find_element(By.XPATH, "..")
                parent_text = clean_text(parent.text)

                if (
                    re.search(r"\b(SSD|SSI|DA|MP)[A-Za-z0-9\-_/]*\b", parent_text, re.I)
                    and status in parent_text
                ):
                    card = parent
                    break

                card = parent

            signature = clean_text(card.text)
            if signature and signature not in seen_card_text:
                seen_card_text.add(signature)
                cards.append(card)

        except Exception:
            continue

    projects = {}

    for card in cards:
        project = extract_project_from_card(
            card,
            development_type,
            status,
        )

        if not project:
            continue

        if project["assessment_type"] not in ALLOWED_ASSESSMENT_TYPES:
            continue

        if (
            development_type == "Waste collection, treatment and disposal"
            and not is_waste_to_energy(project)
        ):
            continue

        projects[project["id"]] = project

    log(
        f"{development_type} / {status}: "
        f"{len(projects)} matching projects."
    )
    return list(projects.values())


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

    driver = build_driver()
    current_projects = {}

    try:
        counts = {
            "SEARs": 0,
            "Exhibition": 0,
            "Recommendation": 0,
        }

        for development_type in TARGET_DEVELOPMENT_TYPES:
            for status in TARGET_STATUSES:
                try:
                    projects = fetch_filtered_projects(
                        driver,
                        development_type,
                        status,
                    )
                except Exception as error:
                    log(
                        f"ERROR checking {development_type} / "
                        f"{status}: {error}"
                    )
                    continue

                counts[status] += len(projects)

                for project in projects:
                    key = f"{project['id']}|{project['status']}"
                    current_projects[key] = project

        log(
            "Milestone totals - "
            f"SEARs: {counts['SEARs']}, "
            f"Exhibition: {counts['Exhibition']}, "
            f"Recommendation: {counts['Recommendation']}"
        )

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

    finally:
        driver.quit()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log(traceback.format_exc())
        sys.exit(1)
