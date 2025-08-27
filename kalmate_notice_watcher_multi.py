#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KALMATE Notice Watcher (카테고리 1단계 깊이 탐색 + NoticeView 전용 + 첫 실행 알림)
- 시작 URL(상위 카테고리):
    - https://www.kalmate.com/Notice/ListView/1
    - https://www.kalmate.com/Notice/ListView/11
- 동작:
    1) 상위 카테고리 페이지에서 하위 카테고리 /Notice/ListView/... 링크들을 모읍니다.
    2) 각 하위 카테고리 첫 페이지에서 NoticeView?seq_no=... 링크(또는 onclick의 seq_no)만 게시글로 인식합니다.
    3) 첫 실행 시(BASELINE) 최신글 스냅샷을 텔레그램으로 1회 발송하고, 이후엔 새 글만 알림.
환경변수:
  TG_BOT_TOKEN, TG_CHAT_ID
  FIRST_RUN_NOTIFY  ("1"/"true"면 첫 실행 알림, 기본 True)
  FIRST_RUN_TOP_N   (첫 실행 목록당 몇 개 보낼지, 기본 1)
"""

import os
import re
import json
import time
import logging
from dataclasses import dataclass
from typing import List, Dict, Optional, Iterable, Set
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------------------- 설정 ----------------------------

BASE_URL = "https://www.kalmate.com"
SEED_LIST_URLS = [
    f"{BASE_URL}/Notice/ListView/1",
    f"{BASE_URL}/Notice/ListView/11",
]
STATE_FILE = Path(__file__).with_name("seen_posts.json")

# 첫 실행 알림
def _env_true(v: Optional[str]) -> bool:
    return str(v).lower() in {"1", "true", "yes", "y", "on"}
FIRST_RUN_NOTIFY = _env_true(os.getenv("FIRST_RUN_NOTIFY", "1"))
FIRST_RUN_TOP_N = int(os.getenv("FIRST_RUN_TOP_N", "1"))

# 하위 카테고리 최대 탐색 개수(안전장치)
SUBLIST_MAX = 30

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

# ---------------------------- 모델 ----------------------------

@dataclass(frozen=True)
class Post:
    id: str            # seq_no 등
    title: str
    url: str
    date: Optional[str] = None

# ---------------------------- HTTP ----------------------------

def make_session(timeout: int = 15) -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
    )
    s.mount("http://", HTTPAdapter(max_retries=retry))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update(HEADERS)
    s.request = _with_timeout(s.request, timeout=timeout)  # type: ignore
    return s

def _with_timeout(fn, *, timeout: int):
    def wrapper(method, url, **kwargs):
        kwargs.setdefault("timeout", timeout)
        return fn(method, url, **kwargs)
    return wrapper

def get_html(session: requests.Session, url: str) -> str:
    r = session.get(url)
    r.raise_for_status()
    return r.text

# ---------------------------- 유틸 ----------------------------

def absolutize(href: str) -> str:
    if href.startswith(("http://", "https://")):
        return href
    if not href.startswith("/"):
        href = "/" + href
    return f"{BASE_URL}{href}"

DATE_PATS = [
    re.compile(r"\b(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})\b"),
    re.compile(r"\b(20\d{2})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일\b"),
]

def first_date_in_list(candidates: Iterable[str]) -> Optional[str]:
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

def safe_int(s: str) -> int:
    try:
        return int(re.sub(r"\D", "", s))
    except Exception:
        return -1

# ---------------------------- 파싱: 게시글 ----------------------------

# NoticeView 링크만 게시글로 인정
SEQ_FROM_ONCLICK_PATTERNS = [
    re.compile(r"seq_no\s*=\s*['\"]?(\d+)", re.I),
    re.compile(r"NoticeView\?seq_no=(\d+)", re.I),
    re.compile(r"\(\s*(\d{6,})\s*\)"),
]

def extract_seq_from_url(url: str) -> Optional[str]:
    p = urlparse(url)
    if p.path.lower().endswith("/notice/noticeview"):
        q = parse_qs(p.query)
        for key in ("seq_no", "seqNo", "seq"):
            vals = q.get(key)
            if vals and vals[0]:
                return vals[0]
    return None

def extract_seq_from_onclick(onclick: str) -> Optional[str]:
    if not onclick:
        return None
    for pat in SEQ_FROM_ONCLICK_PATTERNS:
        m = pat.search(onclick)
        if m:
            return m.group(1)
    return None

def parse_notice_posts_from_html(html: str) -> List[Post]:
    soup = BeautifulSoup(html, "html.parser")
    posts: List[Post] = []

    # 1) href로 NoticeView 직접 연결된 경우
    for a in soup.find_all("a", href=True):
        href = (a["href"] or "").strip()
        if "Notice/NoticeView" not in href:
            continue
        url = absolutize(href)
        seq = extract_seq_from_url(url)
        if not seq:
            continue
        title = a.get_text(strip=True) or url
        tr = a.find_parent("tr")
        date_text = guess_date_from_tr(tr) if tr else guess_date_near(a)
        posts.append(Post(id=seq, title=title, url=url, date=date_text))

    # 2) onclick에 seq_no가 있는 경우(일부 사이트 형태)
    if not posts:
        for a in soup.find_all("a"):
            seq = extract_seq_from_onclick(a.get("onclick", ""))
            if not seq:
                continue
            url = f"{BASE_URL}/Notice/NoticeView?seq_no={seq}"
            title = a.get_text(strip=True) or url
            tr = a.find_parent("tr")
            date_text = guess_date_from_tr(tr) if tr else guess_date_near(a)
            posts.append(Post(id=seq, title=title, url=url, date=date_text))

    # dedupe + 정렬
    uniq: Dict[str, Post] = {}
    for p in posts:
        uniq[p.id] = p
    result = list(uniq.values())
    result.sort(key=lambda p: safe_int(p.id), reverse=True)
    return result

# ---------------------------- 파싱: 하위 카테고리 ----------------------------

LISTVIEW_LINK_PAT = re.compile(r"^/Notice/ListView/[\w\-]+", re.I)

def extract_sublist_urls(html: str) -> List[str]:
    """상위 카테고리 화면에서 하위 카테고리 ListView 링크들을 모은다."""
    soup = BeautifulSoup(html, "html.parser")
    urls: Set[str] = set()

    # a[href] 에서 /Notice/ListView/... 만 수집 (NoticeView는 제외)
    for a in soup.find_all("a", href=True):
        href = (a["href"] or "").strip()
        if "Notice/NoticeView" in href:
            continue
        if "/Notice/ListView/" not in href:
            continue
        # 예) /Notice/ListView/25, /Notice/ListView/11J 등
        m = LISTVIEW_LINK_PAT.search(href if href.startswith("/") else "/" + href)
        if not m:
            continue
        url = absolutize(href)
        urls.add(url)

    # onclick 안에 ListView 경로가 박혀 있는 경우 대비
    for a in soup.find_all("a"):
        onclick = a.get("onclick", "") or ""
        # ListView/25, ListView/11J 등 추출
        m = re.search(r"ListView/([\w\-]+)", onclick, flags=re.I)
        if m:
            urls.add(f"{BASE_URL}/Notice/ListView/{m.group(1)}")

    # 안전상 상위 N개만
    out = list(urls)
    out.sort()
    return out[:SUBLIST_MAX]

# ---------------------------- 상태/알림 ----------------------------

def load_seen() -> Dict[str, Dict]:
    if STATE_FILE.exists():
        try:
            with STATE_FILE.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            logging.warning("STATE_FILE 읽기 실패, 새로 시작")
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

def main():
    session = make_session()
    seen = load_seen()
    is_first_run = (not STATE_FILE.exists()) or (not seen)

    total_new = 0
    new_blocks: List[str] = []
    baseline_blocks: List[str] = []
    baseline_posts: List[Post] = []

    # 1) 상위 카테고리에서 하위 카테고리 URL 수집
    sublists: Set[str] = set()
    for seed in SEED_LIST_URLS:
        try:
            seed_html = get_html(session, seed)
            urls = extract_sublist_urls(seed_html)
            logging.info("하위 카테고리 %d개 발견 @ %s", len(urls), seed)
            sublists.update(urls)
        except Exception:
            logging.exception("상위 카테고리 처리 오류: %s", seed)

    if not sublists:
        logging.info("하위 카테고리를 찾지 못했습니다. (사이트 구조 변화 가능)")
        return

    # 2) 각 하위 카테고리 첫 페이지에서 게시글(NoticeView) 수집
    for sub in sorted(sublists):
        try:
            sub_html = get_html(session, sub)
            posts = parse_notice_posts_from_html(sub_html)
            if not posts:
                logging.info("게시글(NoticeView) 없음: %s", sub)
                continue

            if is_first_run and FIRST_RUN_NOTIFY:
                topn = posts[:max(1, FIRST_RUN_TOP_N)]
                baseline_blocks.append(format_post_lines(topn, sub))
                baseline_posts.extend(topn)
            else:
                new_posts = [p for p in posts if p.id not in seen]
                if new_posts:
                    new_posts.sort(key=lambda p: safe_int(p.id), reverse=True)
                    new_blocks.append(format_post_lines(new_posts, sub))
                    for p in new_posts:
                        seen[p.id] = {"title": p.title, "url": p.url, "date": p.date, "ts": int(time.time())}
                    total_new += len(new_posts)
        except Exception:
            logging.exception("하위 카테고리 처리 오류: %s", sub)

    # 3) 알림/저장
    if is_first_run and FIRST_RUN_NOTIFY and baseline_blocks:
        msg = "KALMATE 공지 BASELINE 스냅샷\n\n" + "\n\n".join(baseline_blocks)
        notify_telegram(msg)
        # 베이스라인도 저장
        for p in baseline_posts:
            seen[p.id] = {"title": p.title, "url": p.url, "date": p.date, "ts": int(time.time())}
        save_seen(seen)
        logging.info("첫 실행 베이스라인 %d건 전송 완료", len(baseline_posts))
        return

    if total_new > 0:
        msg = f"KALMATE 공지 새 글 알림 ({total_new}건)\n\n" + "\n\n".join(new_blocks)
        notify_telegram(msg)
        save_seen(seen)
        logging.info("새 글 %d건 처리 완료", total_new)
    else:
        logging.info("새 글 없음")

if __name__ == "__main__":
    main()
