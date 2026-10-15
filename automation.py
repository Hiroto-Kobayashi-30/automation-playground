"""
GitHub Badge Automation - Policy-Respecting Learning Project
Checks existing achievements before attempting any action.
Runs ONE action per cycle, then waits. Exits when targets reached.
"""
import time
import json
import base64
import sys
import os
from datetime import datetime, timezone

import requests

from config import load_config, DEFAULT_CONFIG

API = "https://api.github.com"
STATE_FILE = "state.json"

# ---------- Achievement Check ----------

def fetch_earned_badges(cfg):
    """Fetch earned achievements from GitHub profile JSON."""
    url = f"{API}/users/{cfg['username']}"
    headers = {
        "Authorization": f"Bearer {cfg['token']}",
        "Accept": "application/vnd.github+json"
    }

    try:
        resp = requests.get(url, headers=headers, timeout=(5, 15))
        if resp.status_code != 200:
            print(f"[Badge Check] Failed to fetch profile (HTTP {resp.status_code})")
            return set()

        data = resp.json()
        achievements = data.get("achievements", [])

        if not achievements:
            print("[Badge Check] No achievements found in profile JSON.")
            return set()

        earned = {ach.get("name", "").lower() for ach in achievements if ach.get("name")}
        print(f"[Badge Check] Found {len(earned)} earned badge(s): {earned}")
        return earned

    except Exception as e:
        print(f"[Badge Check] Error: {e}")
        return set()


def is_badge_earned(cfg, badge_name, earned_badges):
    """Check if a specific badge is already earned."""
    name_map = {
        "quickdraw": "quickdraw",
        "pull_shark": "pull shark",
        "yolo": "yolo",
        "pair_extraordinaire": "pair extraordinaire",
        "public_sponsor": "public sponsor",
    }
    key = name_map.get(badge_name, badge_name.lower())
    return any(key in b.lower() for b in earned_badges)


# ---------- State Management ----------

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "quickdraw_done": False,
        "pull_shark_count": 0,
        "yolo_done": False,
        "pair_extra_done": False,
        "last_action_time": 0,
        "daily_actions": {},
        "cycle_count": 0,
        "badges_checked": False
    }


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ---------- HTTP with Professional Error Handling ----------

def make_request(cfg, method, url, max_retries=None, **kwargs):
    """
    GitHub API request with:
    - Separate connect/read timeouts (fast-fail on unreachable, patient on slow responses)
    - Rate-limit awareness (retry-after, x-ratelimit-reset)
    - Exponential backoff on network errors
    """
    max_retries = max_retries or cfg["max_retries"]
    headers = {
        "Authorization": f"Bearer {cfg['token']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    for attempt in range(max_retries):
        try:
            # Separate timeouts: connect=5s, read=20s
            resp = requests.request(
                method, url, headers=headers,
                timeout=(5, 20),
                **kwargs
            )
        except requests.exceptions.ConnectTimeout:
            wait = cfg["retry_base_delay"] * (2 ** attempt)
            print(f"[Connect timeout] Attempt {attempt+1}/{max_retries}, waiting {wait}s...")
            time.sleep(wait)
            continue
        except requests.exceptions.ReadTimeout:
            wait = cfg["retry_base_delay"] * (2 ** attempt)
            print(f"[Read timeout] Attempt {attempt+1}/{max_retries}, waiting {wait}s...")
            time.sleep(wait)
            continue
        except requests.exceptions.ConnectionError as e:
            wait = cfg["retry_base_delay"] * (2 ** attempt)
            print(f"[Connection error] Attempt {attempt+1}/{max_retries}, waiting {wait}s: {e}")
            time.sleep(wait)
            continue

        if resp.status_code < 400:
            return resp

        if resp.status_code in (403, 429):
            retry_after = resp.headers.get("retry-after")
            if retry_after:
                wait = int(retry_after)
            elif resp.headers.get("x-ratelimit-remaining") == "0":
                reset = int(resp.headers.get("x-ratelimit-reset", time.time() + 60))
                wait = max(reset - time.time(), 0) + 5
            else:
                wait = max(60 * (2 ** attempt), 60)
            print(f"[Rate limit] Waiting {wait:.0f}s before retry...")
            time.sleep(wait)
            continue

        if resp.status_code == 403:
            print(f"[Permission error] Check token scopes. Response: {resp.text[:200]}")
            return resp

        if 400 <= resp.status_code < 500:
            print(f"[Client error {resp.status_code}] {resp.text[:200]}")
            return resp

        wait = cfg["retry_base_delay"] * (2 ** attempt)
        print(f"[Server error {resp.status_code}] waiting {wait}s...")
        time.sleep(wait)

    return None


# ---------- Core Actions ----------

def get_default_branch_sha(cfg):
    r = make_request(cfg, "GET", f"{API}/repos/{cfg['username']}/{cfg['repo']}/git/ref/heads/main")
    if r and r.status_code == 200:
        return r.json()["object"]["sha"]
    return None


def create_issue(cfg, title, body):
    r = make_request(cfg, "POST", f"{API}/repos/{cfg['username']}/{cfg['repo']}/issues",
                     json={"title": title, "body": body})
    return r.json() if r and r.status_code == 201 else None


def close_issue(cfg, number):
    r = make_request(cfg, "PATCH", f"{API}/repos/{cfg['username']}/{cfg['repo']}/issues/{number}",
                     json={"state": "closed"})
    return r and r.status_code == 200


def create_branch(cfg, base_sha, name):
    r = make_request(cfg, "POST", f"{API}/repos/{cfg['username']}/{cfg['repo']}/git/refs",
                     json={"ref": f"refs/heads/{name}", "sha": base_sha})
    return r and r.status_code == 201


def commit_file(cfg, branch, path, content, message):
    encoded = base64.b64encode(content.encode()).decode()
    r = make_request(cfg, "PUT", f"{API}/repos/{cfg['username']}/{cfg['repo']}/contents/{path}",
                     json={"message": message, "content": encoded, "branch": branch})
    return r and r.status_code == 201


def create_pr(cfg, head, base, title, body):
    r = make_request(cfg, "POST", f"{API}/repos/{cfg['username']}/{cfg['repo']}/pulls",
                     json={"title": title, "body": body, "head": head, "base": base})
    return r.json() if r and r.status_code == 201 else None


def merge_pr(cfg, number):
    r = make_request(cfg, "PUT", f"{API}/repos/{cfg['username']}/{cfg['repo']}/pulls/{number}/merge",
                     json={"merge_method": "squash"})
    return r and r.status_code == 200


# ---------- Badge Workflows ----------

def do_quickdraw(cfg, state):
    """Create + close issue within 5 minutes."""
    print("[Quickdraw] Creating issue...")
    issue = create_issue(cfg, "Automation test issue", "Created by learning script.")
    if not issue:
        return False
    print(f"[Quickdraw] Issue #{issue['number']} created. Waiting 30s...")
    time.sleep(30)
    if close_issue(cfg, issue["number"]):
        print(f"[Quickdraw] Issue #{issue['number']} closed. Badge triggered.")
        state["quickdraw_done"] = True
        return True
    return False


def do_pull_shark(cfg, state):
    """Create branch -> commit -> PR -> merge."""
    print("[Pull Shark] Preparing branch...")
    base_sha = get_default_branch_sha(cfg)
    if not base_sha:
        print("[Pull Shark] Cannot get base SHA. Is repo initialized with 'main'?")
        return False

    branch = f"badge-pr-{int(time.time())}"
    if not create_branch(cfg, base_sha, branch):
        print("[Pull Shark] Branch creation failed.")
        return False

    coauthor = cfg.get("coauthor_username", "").strip()
    msg = f"Add automation log\n\nCo-authored-by: {coauthor} <{cfg['coauthor_email']}>" if coauthor else "Add automation log"

    if not commit_file(cfg, branch, "automation_log.txt", f"Run at {datetime.now(timezone.utc)}", msg):
        print("[Pull Shark] Commit failed.")
        return False

    pr = create_pr(cfg, branch, "main", "Automation PR", "Created by learning script.")
    if not pr:
        print("[Pull Shark] PR creation failed.")
        return False

    print(f"[Pull Shark] PR #{pr['number']} created. Waiting 60s before merge...")
    time.sleep(60)

    if merge_pr(cfg, pr["number"]):
        print(f"[Pull Shark] PR #{pr['number']} merged.")
        state["pull_shark_count"] += 1
        state["yolo_done"] = True
        if coauthor:
            state["pair_extra_done"] = True
            print("[Pair Extraordinaire] Co-authored commit merged.")
        return True
    return False


# ---------- Main Loop ----------

def all_done(cfg, state, earned_badges):
    """Check if all target badges are satisfied (state OR earned)."""
    t = cfg["targets"]

    if t["quickdraw"] and not (state["quickdraw_done"] or is_badge_earned(cfg, "quickdraw", earned_badges)):
        return False
    if t["pull_shark"] and state["pull_shark_count"] < 2:
        return False
    if t["yolo"] and not (state["yolo_done"] or is_badge_earned(cfg, "yolo", earned_badges)):
        return False
    if t["pair_extraordinaire"] and not (state["pair_extra_done"] or is_badge_earned(cfg, "pair_extraordinaire", earned_badges)):
        return False
    return True


def daily_action_count(state):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return state["daily_actions"].get(today, 0)


def record_action(state):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    state["daily_actions"][today] = state["daily_actions"].get(today, 0) + 1
    state["last_action_time"] = time.time()


def main():
    cfg = load_config()

    if not cfg["token"] or not cfg["username"]:
        print("[Error] Set token and username in config.py or config.json")
        sys.exit(1)

    state = load_state()

    # ---------- Achievement Check ----------
    print("\n[Badge Check] Querying existing achievements...")
    earned_badges = fetch_earned_badges(cfg)
    state["badges_checked"] = True

    if earned_badges:
        if is_badge_earned(cfg, "quickdraw", earned_badges):
            state["quickdraw_done"] = True
        if is_badge_earned(cfg, "yolo", earned_badges):
            state["yolo_done"] = True
        if is_badge_earned(cfg, "pull_shark", earned_badges):
            state["pull_shark_count"] = 2
        if is_badge_earned(cfg, "pair_extraordinaire", earned_badges):
            state["pair_extra_done"] = True
        save_state(state)
    # ----------------------------------------

    print("=" * 60)
    print("GitHub Badge Automation (Learning Project)")
    print("=" * 60)
    print(f"Targets: {[k for k, v in cfg['targets'].items() if v]}")
    print(f"Earned badges detected: {earned_badges if earned_badges else 'none'}")
    print(f"Min interval: {cfg['min_action_interval']}s | Max daily: {cfg['max_daily_actions']}")
    print(f"Stop with Ctrl+C. State saved automatically.")
    print("=" * 60)

    try:
        while not all_done(cfg, state, earned_badges):
            if daily_action_count(state) >= cfg["max_daily_actions"]:
                print(f"[Limit] Daily action limit ({cfg['max_daily_actions']}) reached. Waiting...")
                time.sleep(cfg["cycle_wait"])
                continue

            elapsed = time.time() - state["last_action_time"]
            if elapsed < cfg["min_action_interval"]:
                wait = cfg["min_action_interval"] - elapsed
                print(f"[Pacing] Waiting {wait:.0f}s before next action...")
                time.sleep(wait)
                continue

            # Execute exactly ONE pending action per cycle
            if cfg["targets"]["quickdraw"] and not state["quickdraw_done"]:
                do_quickdraw(cfg, state)
            elif cfg["targets"]["pull_shark"] and state["pull_shark_count"] < 2:
                do_pull_shark(cfg, state)
            elif cfg["targets"]["yolo"] and not state["yolo_done"]:
                print("[YOLO] Will be satisfied by the next unreviewed merge.")
            elif cfg["targets"]["pair_extraordinaire"] and not state["pair_extra_done"]:
                print("[Pair Extra] Requires a co-authored PR. Set coauthor_username.")

            record_action(state)
            state["cycle_count"] += 1
            save_state(state)

            print(f"[Cycle {state['cycle_count']}] Action done. Waiting {cfg['cycle_wait']}s...")
            time.sleep(cfg["cycle_wait"])

    except KeyboardInterrupt:
        print("\n[Stop] Saving state...")
        save_state(state)
        print("[Stop] Done. Resume later by running again.")


if __name__ == "__main__":
    main()