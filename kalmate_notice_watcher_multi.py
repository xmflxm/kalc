#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KALMATE Notice Watcher
- 상위 카테고리(1, 11) → 하위 카테고리(ListView/xx) 1단계 순회
- NoticeView 링크 직접 탐지 + 실패 시 HTML에서 seq_no 후보 추출 → 상세페이지(NoticeView) 접속해 제목/날짜 파싱
- 첫 실행(BASELINE) 알림 지원

환경변수:
  TG_BOT_TOKEN, TG_CHAT_ID  (필수)
  FIRST_RUN_NOTIFY  = 1/true (기본: true)
  FIRST_RUN_TOP_N   = 정수 (기본: 1, 첫 실행에 목록당 몇 개 보낼지)
"""

import os
import re
import json
import time
import logging
from dataclasses import dataclass
from typing import List, Dict, Optional, Iterable, Set, Tuple
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

def _env_true(v: Optional[str]) -> bool:
    return str(v).lower() in {"1", "true", "yes", "y", "on"}

FIRST_RUN_NOTIFY = _env_true(os.getenv("FIRST_RUN_NOTIFY", "1"))
FIRST_RUN_TOP_N = int(os.getenv("FIRST_RUN_TOP_N", "1"))

SUBLIST_MAX = 40            # 하위 카테고리 최대 탐색(안전장치)
DETAIL_FETCH_MAX = 40       # 상세페이지 최대 조회(안전장치)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

# ---------------------------- 모델 ----------------------------

@dataclass(frozen=True)
class Post:
    id: str            # seq_no
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

def get_html(session: requests.Session, url: str, referer: Optional[str] = None) -> str:
    headers = {}
    if referer:
        headers["Referer"] = referer
    r = session.get(url, headers=headers)
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

def first_date_in_text(text: str) -> Optional[str]:
    for pat in DATE_PATS:
        m = pat.search(text or "")
        if m:
            try:
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                return f"{y:04d}-{mo:02d}-{d:02d}"
            except Exception:
                pass
    return None

def first_date_in_list(candidates: Iterable[str]) -> Optional[str]:
    for t in candidates:
        d = first_date_in_text(t)
        if d:
            return d
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

SEQ_FROM_ONCLICK_PATS = [
    re.compile(r"seq_no\s*=\s*['\"]?(\d+)", re.I),
    re.compile(r"NoticeView\?seq_no=(\d+)", re.I),
    re.compile(r"\(\s*(\d{6,})\s*\)"),
    re.compile(r"fn(?:View|Detail|Go)\s*\(\s*['\"]?(\d+)", re.I),
    re.compile(r"__doPostBack\([^,]+,\s*'?(?:seq_no\s*=\s*)?(\d+)'?\)", re.I),
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

def extract_seqs_from_html(html: str) -> List[str]:
    seqs: Set[str] = set()
    # 1) href 안의 NoticeView
    for m in re.finditer(r"Notice/NoticeView\?[^\"'>]*?seq_no=(\d+)", html, flags=re.I):
        seqs.add(m.group(1))
    # 2) onclick 등 다양한 패턴
    for pat in SEQ_FROM_ONCLICK_PATS:
        for m in pat.finditer(html):
            seqs.add(m.group(1))
    # 길이 필터(보통 6자리 이상)
    seqs = {s for s in seqs if len(s) >= 6}
    # 내림차순(신규가 보통 큼)
    return sorted(seqs, key=lambda x: safe_int(x), reverse=True)

def parse_notice_posts_from_html(session: requests.Session, html: str, referer: Optional[str] = None) -> List[Post]:
    soup = BeautifulSoup(html, "html.parser")
    posts: List[Post] = []

    # 1) href로 NoticeView 직접 연결된 경우
    direct_found = False
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
        direct_found = True

    # 2) 실패 시: HTML에서 seq_no 후보 찾아서 상세페이지 열어 제목/날짜 파싱
    if not direct_found:
        seqs = extract_seqs_from_html(html)
        if seqs:
            detail_count = 0
            for seq in seqs:
                if detail_count >= DETAIL_FETCH_MAX:
                    break
                detail_url = f"{BASE_URL}/Notice/NoticeView?seq_no={seq}"
                try:
                    detail_html = get_html(session, detail_url, referer=referer)
                    title, date_text = parse_notice_detail(detail_html)
                    title = title or detail_url
                    posts.append(Post(id=seq, title=title, url=detail_url, date=date_text))
                    detail_count += 1
                except Exception:
                    # 상세페이지가 막히면 넘어감
                    continue

    # dedupe + 정렬
    uniq: Dict[str, Post] = {}
    for p in posts:
        uniq[p.id] = p
    result = list(uniq.values())
    result.sort(key=lambda p: safe_int(p.id), reverse=True)
    return result

def parse_notice_detail(html: str) -> Tuple[Optional[str], Optional[str]]:
    """상세페이지에서 제목/날짜 추출(여러 후보 시도)"""
    soup = BeautifulSoup(html, "html.parser")

    # 1) 흔한 제목 셀렉터
    for sel in ["h1", "h2", ".title", ".subject", ".board_tit", ".board-title", ".notice-title"]:
        el = soup.select_one(sel)
        if el and el.get_text(strip=True):
            title = el.get_text(strip=True)
            # 날짜도 근처에서 시도
            date_text = first_date_in_text(el.parent.get_text(" ", strip=True)) if el.parent else None
            return title, date_text

    # 2) 테이블형 (제목/작성일 등)
    for th in soup.find_all("th"):
        key = th.get_text(strip=True)
        if "제목" in key or "Title" in key:
            td = th.find_next("td")
            if td:
                title = td.get_text(strip=True)
                # 근처에서 날짜 후보
                date_td = th.find_parent("tr").find_next_sibling("tr")
                date_text = first_date_in_text((date_td.get_text(" ", strip=True) if date_td else "") or "")
                return title, date_text

    # 3) og:title 메타
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        title = og["content"].strip()
        date_text = first_date_in_text(soup.get_text(" ", strip=True))
        return title, date_text

    # 4) <title> 태그
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
        date_text = first_date_in_text(soup.get_text(" ", strip=True))
        return title, date_text

    # 5) 최후: 본문에서 첫 줄
    body_text = soup.get_text("\n", strip=True)
    first_line = (body_text.splitlines() or [""])[0].strip()
    return (first_line or None), first_date_in_text(body_text)

# ---------------------------- 파싱: 하위 카테고리 ----------------------------

LISTVIEW_LINK_PAT = re.compile(r"^/Notice/ListView/[\w\-]+", re.I)

def extract_sublist_urls(html: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    urls: Set[str] = set()

    # a[href]에서 /Notice/ListView/... 수집
    for a in soup.find_all("a", href=True):
        href = (a["href"] or "").strip()
        if "/Notice/NoticeView" in href:
            continue
        if "/Notice/ListView/" not in href:
            continue
        m = LISTVIEW_LINK_PAT.search(href if href.startswith("/") else "/" + href)
        if not m:
            continue
        urls.add(absolutize(href))

    # onclick 내 ListView 링크
    for a in soup.find_all("a"):
        onclick = a.get("onclick", "") or ""
        m = re.search(r"ListView/([\w\-]+)", onclick, flags=re.I)
        if m:
            urls.add(f"{BASE_URL}/Notice/ListView/{m.group(1)}")

    out = sorted(urls)
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

    # 1) 상위 카테고리에서 하위 카테고리 수집
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
        logging.info("하위 카테고리를 찾지 못했습니다.")
        return

    # 2) 각 하위 카테고리 첫 페이지에서 게시글 수집
    for sub in sorted(sublists):
        try:
            sub_html = get_html(session, sub, referer=sub)  # referer 세팅
            posts = parse_notice_posts_from_html(session, sub_html, referer=sub)
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
