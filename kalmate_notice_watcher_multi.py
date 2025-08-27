#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KALMATE Notice Watcher (Pro / with verification flags)
- Monitors:
    - https://www.kalmate.com/Notice/ListView/1
    - https://www.kalmate.com/Notice/ListView/11
- Sends Telegram notifications on NEW posts.
- Includes verification options to confirm it's working without waiting for new posts.

Usage examples:
  # 1) Send a TEST message to Telegram (credentials check)
  python kalmate_notice_watcher_pro.py --send-test

  # 2) Show top 5 latest items from each list (parsing check)
  python kalmate_notice_watcher_pro.py --show-latest 5

  # 3) Do a dry-run (detect new posts but DON'T notify or write state)
  python kalmate_notice_watcher_pro.py --dry-run

  # 4) Force a heartbeat notification with the current latest items
  python kalmate_notice_watcher_pro.py --force-notify

  # 5) Reset seen state (start fresh)
  python kalmate_notice_watcher_pro.py --reset-seen
"""

import os
import re
import json
import time
import logging
import argparse
from dataclasses import dataclass
from typing import List, Dict, Optional
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------------------- Config ----------------------------

BASE_URL = "https://www.kalmate.com"
LIST_URLS = [
    f"{BASE_URL}/Notice/ListView/1",
    f"{BASE_URL}/Notice/ListView/11",
]
STATE_FILE = Path(__file__).with_name("seen_posts.json")

# Telegram (use env vars; do NOT hardcode in public repos)
# Set these in your environment or GitHub Actions Secrets:
#   TG_BOT_TOKEN, TG_CHAT_ID
# If either is missing, notifications are skipped with a log warning.

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

# ---------------------------- Models ----------------------------

@dataclass(frozen=True)
class Post:
    id: str
    title: str
    url: str
    date: Optional[str] = None  # YYYY-MM-DD

# ---------------------------- HTTP ----------------------------

def make_session(timeout: int = 15) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(HEADERS)
    session.request = _with_timeout(session.request, timeout=timeout)  # type: ignore
    return session

def _with_timeout(fn, *, timeout: int):
    def wrapper(method, url, **kwargs):
        kwargs.setdefault("timeout", timeout)
        return fn(method, url, **kwargs)
    return wrapper

# ---------------------------- Parsing ----------------------------

HREF_ID_PAT = re.compile(r"/Notice/(?:View|Detail|Read|ListView)/(?P<id>\d+)", re.I)

def parse_posts(html: str) -> List[Post]:
    soup = BeautifulSoup(html, "html.parser")
    posts: List[Post] = []

    # 1) Table layout
    table = soup.find("table")
    if table:
        tbody = table.find("tbody") or table
        for tr in tbody.find_all("tr"):
            a = tr.find("a", href=True)
            if not a:
                continue
            href = a["href"]
            pid = extract_id(href) or href
            title = a.get_text(strip=True)
            url = absolutize(href)
            date_text = guess_date_from_tr(tr)
            posts.append(Post(id=pid, title=title, url=url, date=date_text))

    # 2) <li> list layout
    if not posts:
        for li in soup.find_all("li"):
            a = li.find("a", href=True)
            if not a:
                continue
            title = a.get_text(strip=True)
            if len(title) < 2:
                continue
            href = a["href"]
            if "/Notice/" not in href:
                continue
            pid = extract_id(href) or href
            url = absolutize(href)
            date_text = guess_date_near(li)
            posts.append(Post(id=pid, title=title, url=url, date=date_text))

    # 3) Fallback: scan all anchors
    if not posts:
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/Notice/" not in href:
                continue
            title = a.get_text(strip=True) or href
            if len(title) < 2:
                continue
            pid = extract_id(href) or href
            url = absolutize(href)
            date_text = guess_date_near(a)
            posts.append(Post(id=pid, title=title, url=url, date=date_text))

    # dedupe and sort
    unique: Dict[str, Post] = {}
    for p in posts:
        unique[p.id] = p
    result = list(unique.values())
    result.sort(key=lambda p: safe_int(p.id), reverse=True)
    return result

def extract_id(href: str) -> Optional[str]:
    m = HREF_ID_PAT.search(href)
    return m.group("id") if m else None

def absolutize(href: str) -> str:
    if href.startswith(("http://", "https://")):
        return href
    if not href.startswith("/"):
        href = "/" + href
    return f"{BASE_URL}{href}"

def guess_date_from_tr(tr) -> Optional[str]:
    tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
    return first_date_in_list(tds)

def guess_date_near(node) -> Optional[str]:
    texts = []
    parent = node.parent
    for _ in range(3):
        if not parent:
            break
        texts.append(parent.get_text(" ", strip=True))
        parent = parent.parent
    return first_date_in_list(texts)

DATE_PATS = [
    re.compile(r"\b(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})\b"),
    re.compile(r"\b(20\d{2})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일\b"),
]

def first_date_in_list(candidates: List[str]) -> Optional[str]:
    for text in candidates:
        for pat in DATE_PATS:
            m = pat.search(text)
            if m:
                try:
                    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                    return f"{y:04d}-{mo:02d}-{d:02d}"
                except Exception:
                    pass
    return None

def safe_int(s: str) -> int:
    try:
        return int(re.sub(r"\D", "", s))
    except Exception:
        return -1

# ---------------------------- State ----------------------------

def load_seen() -> Dict[str, Dict]:
    if STATE_FILE.exists():
        try:
            with STATE_FILE.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            logging.warning("Failed to read state file, starting fresh.")
    return {}

def save_seen(data: Dict[str, Dict]):
    with STATE_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ---------------------------- Notify ----------------------------

def notify_telegram(text: str):
    token = os.getenv("TG_BOT_TOKEN")
    chat_id = os.getenv("TG_CHAT_ID")
    if not token or not chat_id:
        logging.warning("Telegram token/chat_id not set. Skipping notification.")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, data={"chat_id": chat_id, "text": text})
    if resp.status_code == 200:
        logging.info("Telegram notification sent.")
    else:
        logging.warning("Telegram notification failed: %s %s", resp.status_code, resp.text)

def format_post_lines(posts: List[Post], source_url: str) -> str:
    lines = [f"[List] {source_url}"]
    for p in posts:
        date = f" ({p.date})" if p.date else ""
        lines.append(f"- {p.title}{date}\n  {p.url}")
    return "\n".join(lines)

# ---------------------------- Core ----------------------------

def fetch_list_html(session: requests.Session, url: str) -> str:
    r = session.get(url)
    r.raise_for_status()
    return r.text

def find_new_posts(current: List[Post], seen_map: Dict[str, Dict]) -> List[Post]:
    return [p for p in current if p.id not in seen_map]

def update_seen(seen_map: Dict[str, Dict], posts: List[Post]):
    now = int(time.time())
    for p in posts:
        seen_map[p.id] = {"title": p.title, "url": p.url, "date": p.date, "ts": now}

# ---------------------------- CLI ----------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--send-test", action="store_true", help="Send a test message to Telegram.")
    parser.add_argument("--show-latest", type=int, default=0, help="Show top N latest items from each list.")
    parser.add_argument("--dry-run", action="store_true", help="Detect but do not notify or write state.")
    parser.add_argument("--force-notify", action="store_true", help="Send a heartbeat with current latest items.")
    parser.add_argument("--reset-seen", action="store_true", help="Delete seen_posts.json and start fresh.")
    args = parser.parse_args()

    if args.reset_seen and STATE_FILE.exists():
        STATE_FILE.unlink()
        print("Seen state reset.")

    session = make_session()

    # 1) Test message
    if args.send_test:
        notify_telegram("TEST: KALMATE watcher is working ✅")
        return

    # 2) Show latest N
    if args.show_latest > 0:
        for url in LIST_URLS:
            html = fetch_list_html(session, url)
            posts = parse_posts(html)
            print(f"\n== {url} ==")
            for p in posts[:args.show_latest]:
                print(f"- {p.title} ({p.date})")
                print(f"  {p.url}")
        return

    # Normal / dry-run / force-notify
    seen = load_seen()
    total_new = 0
    all_new_lines: List[str] = []
    latest_blocks: List[str] = []

    for list_url in LIST_URLS:
        try:
            html = fetch_list_html(session, list_url)
            posts = parse_posts(html)
            logging.info("Parsed %d posts from %s", len(posts), list_url)
            if not posts:
                continue

            # For heartbeat
            latest_blocks.append(format_post_lines(posts[:1], list_url))

            # New detection
            new_posts = find_new_posts(posts, seen)
            if new_posts:
                new_posts.sort(key=lambda p: safe_int(p.id), reverse=True)
                all_new_lines.append(format_post_lines(new_posts, list_url))
                total_new += len(new_posts)
                if not args.dry_run:
                    update_seen(seen, new_posts)
        except Exception:
            logging.exception("Error processing %s", list_url)

    if args.force_notify and latest_blocks:
        text = "HEARTBEAT: KALMATE watcher is alive 💓\n\n" + "\n\n".join(latest_blocks)
        if not args.dry_run:
            notify_telegram(text)
            save_seen(seen)
        print(text)
        return

    if total_new > 0:
        text = f"KALMATE Notice New Posts ({total_new})\n\n" + "\n".join(all_new_lines)
        if args.dry_run:
            print("[DRY-RUN] Would send:\n", text)
        else:
            notify_telegram(text)
            save_seen(seen)
        logging.info("Processed %d new posts", total_new)
    else:
        logging.info("No new posts")
        if args.dry_run:
            print("[DRY-RUN] No new posts detected.")

if __name__ == "__main__":
    main()
