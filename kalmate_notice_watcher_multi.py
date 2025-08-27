#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KALMATE Notice Watcher (multi-page, NoticeView 전용 파싱)
- 모니터링:
    - https://www.kalmate.com/Notice/ListView/1
    - https://www.kalmate.com/Notice/ListView/11
- 새 글 감지 시 텔레그램 알림.
- 핵심: NoticeView?seq_no=... 형태만 수집 (ListView 카테고리 링크는 무시)
"""

import os
import re
import json
import time
import logging
from dataclasses import dataclass
from typing import List, Dict, Optional
from pathlib import Path
from urllib.parse import urlparse, parse_qs

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
    id: str            # seq_no 등 고유 식별자
    title: str
    url: str
    date: Optional[str] = None  # YYYY-MM-DD

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

# ---------------------------- 파싱 ----------------------------

# NoticeView 링크의 seq_no를 최우선으로 추출
HREF_NUM_ID_PAT = re.compile(r"/Notice/(?:View|Detail|Read|ListView)/(?P<id>\d+)", re.I)
SEQ_FROM_ONCLICK_PATTERNS = [
    re.compile(r"seq_no\s*=\s*['\"]?(\d+)", re.I),
    re.compile(r"NoticeView\?seq_no=(\d+)", re.I),
    re.compile(r"\(\s*(\d{6,})\s*\)"),  # 함수콜 형식에서 숫자 인자만 있을 때
]

def parse_posts(html: str) -> List[Post]:
    soup = BeautifulSoup(html, "html.parser")
    posts: List[Post] = []

    # 1) href에 NoticeView가 직접 들어있는 앵커만 수집
    for a in soup.find_all("a", href=True):
        href = (a["href"] or "").strip()
        if "Notice/NoticeView" in href:
            p = make_post_from_anchor(a, href)
            if p:
                posts.append(p)

    # 2) (보조) onclick 안에 seq_no 정보가 들어있는 경우 처리
    #    (일부 사이트는 href가 '#'이고 onclick으로 상세로 이동)
    if not posts:
        for a in soup.find_all("a"):
            onclick = a.get("onclick") or ""
            seq = extract_seq_no_from_onclick(onclick)
            if not seq:
                continue
            # 상세 URL 구성
            url = f"{BASE_URL}/Notice/NoticeView?seq_no={seq}"
            title = a.get_text(strip=True) or url
            date_text = guess_date_from_tr(a.find_parent("tr")) or guess_date_near(a)
            posts.append(Post(id=seq, title=title, url=url, date=date_text))

    # ✅ ListView 등 다른 /Notice/ 링크는 더 이상 수집하지 않음 (카테고리 오탐 방지)

    # 중복 제거 및 정렬
    unique: Dict[str, Post] = {}
    for p in posts:
        unique[p.id] = p
    result = list(unique.values())
    result.sort(key=lambda p: safe_int(p.id), reverse=True)
    return result

def make_post_from_anchor(a, href: str) -> Optional[Post]:
    url = absolutize(href)
    pid = extract_post_id(url)
    if not pid:
        return None
    title = a.get_text(strip=True) or url
    tr = a.find_parent("tr")
    date_text = guess_date_from_tr(tr) if tr else guess_date_near(a)
    return Post(id=pid, title=title, url=url, date=date_text)

def extract_post_id(url: str) -> Optional[str]:
    parsed = urlparse(url)
    if parsed.path.lower().endswith("/notice/noticeview"):
        q = parse_qs(parsed.query)
        for key in ("seq_no", "seqNo", "seq"):
            vals = q.get(key)
            if vals and vals[0]:
                return vals[0]
    # (보조) /Notice/.../<숫자> 구조는 더 이상 사용하지 않음 (카테고리 혼입 방지)
    return None

def extract_seq_no_from_onclick(onclick: str) -> Optional[str]:
    if not onclick:
        return None
    for pat in SEQ_FROM_ONCLICK_PATTERNS:
        m = pat.search(onclick)
        if m:
            return m.group(1)
    return None

def absolutize(href: str) -> str:
    if href.startswith(("http://", "https://")):
        return href
    if not href.startswith("/"):
        href = "/" + href
    return f"{BASE_URL}{href}"

def guess_date_from_tr(tr) -> Optional[str]:
    if not tr:
        return None
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

# ---------------------------- 상태/알림 ----------------------------

def load_seen() -> Dict[str, Dict]:
    if STATE_FILE.exists():
        try:
            with STATE_FILE.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            logging.warning("STATE_FILE 읽기 실패, 새로 시작합니다.")
    return {}

def save_seen(data: Dict[str, Dict]):
    with STATE_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def notify_telegram(text: str):
    token = os.getenv("TG_BOT_TOKEN")
    chat_id = os.getenv("TG_CHAT_ID")
    if not (token and chat_id):
        logging.warning("텔레그램 토큰/챗ID 미설정. 알림 건너뜀.")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, data={"chat_id": chat_id, "text": text})
    if resp.status_code == 200:
        logging.info("텔레그램 알림 전송 완료")
    else:
        logging.warning("텔레그램 알림 실패: %s %s", resp.status_code, resp.text)

def format_post_lines(posts: List[Post], source_url: str) -> str:
    lines = [f"[목록] {source_url}"]
    for p in posts:
        date = f" ({p.date})" if p.date else ""
        lines.append(f"- {p.title}{date}\n  {p.url}")
    return "\n".join(lines)

# ---------------------------- 메인 ----------------------------

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
                logging.info("NoticeView 형식 게시글을 찾지 못했습니다: %s", list_url)
                continue
            new_posts = find_new_posts(posts, seen)
            if new_posts:
                new_posts.sort(key=lambda p: safe_int(p.id), reverse=True)
                all_new_lines.append(format_post_lines(new_posts, list_url))
                update_seen(seen, new_posts)
                total_new += len(new_posts)
        except Exception:
            logging.exception("목록 처리 중 오류: %s", list_url)

    if total_new > 0:
        text = f"KALMATE 공지 새 글 알림 ({total_new}건)\n\n" + "\n\n".join(all_new_lines)
        notify_telegram(text)
        save_seen(seen)
        logging.info("새 글 %d건 처리 완료", total_new)
    else:
        logging.info("새 글 없음")

if __name__ == "__main__":
    main()
