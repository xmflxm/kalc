#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KALMATE Notice Watcher (multi-page)
- Targets:
    - https://www.kalmate.com/Notice/ListView/1
    - https://www.kalmate.com/Notice/ListView/11
- Detects new posts and sends notifications (Telegram by default).
"""

import os
import re
import json
import time
import logging
from dataclasses import dataclass
from typing import List, Dict, Optional
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_URL = "https://www.kalmate.com"
LIST_URLS = [
    f"{BASE_URL}/Notice/ListView/1",
    f"{BASE_URL}/Notice/ListView/11",
]
STATE_FILE = Path(__file__).with_name("seen_posts.json")

# Default Telegram values (better to override via env vars)
DEFAULT_TG_BOT_TOKEN = "8299349928:AAExoxkAyRL_1qS2qSRIGN0E2OKffcX71ds"
DEFAULT_TG_CHAT_ID = "6164865591"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

@dataclass(frozen=True)
class Post:
    id: str
    title: str
    url: str
    date: Optional[str] = None

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

HREF_ID_PAT = re.compile(r"/Notice/(?:View|Detail|Read|ListView)/(?P<id>\d+)", re.I)

def parse_posts(html: str) -> List[Post]:
    soup = BeautifulSoup(html, "html.parser")
    posts: List[Post] = []

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

def notify_telegram(text: str):
    token = os.getenv("TG_BOT_TOKEN", DEFAULT_TG_BOT_TOKEN)
    chat_id = os.getenv("TG_CHAT_ID", DEFAULT_TG_CHAT_ID)
    if not (token and chat_id):
        logging.info("Telegram notification disabled (no token/chat id).")
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

def fetch_list_html(session: requests.Session, url: str) -> str:
    r = session.get(url)
    r.raise_for_status()
    return r.text

def find_new_posts(current: List[Post], seen_map: Dict[str, Dict]) -> List[Post]:
    new_items = []
    for p in current:
        if p.id not in seen_map:
            new_items.append(p)
    return new_items

def update_seen(seen_map: Dict[str, Dict], posts: List[Post]):
    now = int(time.time())
    for p in posts:
        seen_map[p.id] = {"title": p.title, "url": p.url, "date": p.date, "ts": now}

def main():
    session = make_session()
    seen = load_seen()
    total_new = 0
    all_new_lines: List[str] = []

    for list_url in LIST_URLS:
        try:
            html = fetch_list_html(session, list_url)
            posts = parse_posts(html)
            if not posts:
                logging.warning("No posts parsed: %s", list_url)
                continue
            new_posts = find_new_posts(posts, seen)
            if new_posts:
                new_posts.sort(key=lambda p: safe_int(p.id), reverse=True)
                all_new_lines.append(format_post_lines(new_posts, list_url))
                update_seen(seen, new_posts)
                total_new += len(new_posts)
        except Exception as e:
            logging.exception("Error processing %s", list_url)

    if total_new > 0:
        text = f"KALMATE Notice New Posts ({total_new})\n\n" + "\n\n".join(all_new_lines)
        notify_telegram(text)
        save_seen(seen)
        logging.info("Processed %d new posts", total_new)
    else:
        logging.info("No new posts")

if __name__ == "__main__":
    main()
