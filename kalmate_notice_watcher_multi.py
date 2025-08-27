#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KALMATE Notice Watcher
- 상위 카테고리(1, 11) → 하위 카테고리(ListView/xx) 1단계 순회
- ✅ <a class="noticedetail" name="SEQ">제목</a> 우선 파싱
- NoticeView 직접 링크/onclick/정규식 후보 → 상세(NoticeView) 접속해 제목/날짜 보강
- ✅ 스냅샷은 최초 1회만, 최신순 상위 10개만 전송
- 이후에는 새 글이 있는 경우에만 새 글들만 전송

환경변수:
  TG_BOT_TOKEN, TG_CHAT_ID  (필수)
  FIRST_RUN_NOTIFY  = 1/true (기본: true)
  FIRST_RUN_TOP_N   = 정수 (기본: 10, 스냅샷 표시 개수)
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
BASELINE_FLAG_FILE = Path(__file__).with_name(".kalmate_baseline_done")  # ✅ 최초 1회만 스냅샷

def _env_true(v: Optional[str]) -> bool:
    return str(v).lower() in {"1", "true", "yes", "y", "on"}

FIRST_RUN_NOTIFY = _env_true(os.getenv("FIRST_RUN_NOTIFY", "1"))
FIRST_RUN_TOP_N = int(os.getenv("FIRST_RUN_TOP_N", "10"))  # ✅ 기본 10개

SUBLIST_MAX = 40
DETAIL_FETCH_MAX = 40

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
    date: Optional[str] = None  # YYYY-MM-DD

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

def derive_listcategory_from_list_url(url: str) -> Optional[str]:
    # /Notice/ListView/11J → 11 / /Notice/ListView/25 → 25
    m = re.search(r"/Notice/ListView/([0-9]+)", url)
    return m.group(1) if m else None

DATE_PATS = [
    re.compile(r"\b(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})\b"),
    re.compile(r"\b(20\d{2})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일\b"),
]

def date_to_int(d: Optional[str]) -> int:
    # "YYYY-MM-DD" → 20250131 (정수). 없음이면 -1
    if not d:
        return -1
    try:
        y, m, dd = d.split("-")
        return int(f"{int(y):04d}{int(m):02d}{int(dd):02d}")
    except Exception:
        return -1

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

def sort_key_post(p: Post):
    # 최신 날짜 우선 → 같은 날짜면 seq_no 큰 순
    return (date_to_int(p.date), safe_int(p.id))

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
    for m in re.finditer(r"Notice/NoticeView\?[^\"'>]*?seq_no=(\d+)", html, flags=re.I):
        seqs.add(m.group(1))
    for pat in SEQ_FROM_ONCLICK_PATS:
        for m in pat.finditer(html):
            seqs.add(m.group(1))
    for m in re.finditer(r'name=["\'](\d{6,})["\']', html, flags=re.I):
        seqs.add(m.group(1))
    seqs = {s for s in seqs if len(s) >= 6}
    return sorted(seqs, key=lambda x: safe_int(x), reverse=True)

def parse_notice_detail(html: str) -> Tuple[Optional[str], Optional[str]]:
    soup = BeautifulSoup(html, "html.parser")
    for sel in ["h1", "h2", ".title", ".subject", ".board_tit", ".board-title", ".notice-title"]:
        el = soup.select_one(sel)
        if el and el.get_text(strip=True):
            title = el.get_text(strip=True)
            date_text = first_date_in_text(el.parent.get_text(" ", strip=True)) if el.parent else None
            return title, date_text
    for th in soup.find_all("th"):
        key = th.get_text(strip=True)
        if "제목" in key or "Title" in key:
            td = th.find_next("td")
            if td:
                title = td.get_text(strip=True)
                date_td = th.find_parent("tr").find_next_sibling("tr")
                date_text = first_date_in_text((date_td.get_text(" ", strip=True) if date_td else "") or "")
                return title, date_text
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        title = og["content"].strip()
        date_text = first_date_in_text(soup.get_text(" ", strip=True))
        return title, date_text
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
        date_text = first_date_in_text(soup.get_text(" ", strip=True))
        return title, date_text
    body_text = soup.get_text("\n", strip=True)
    first_line = (body_text.splitlines() or [""])[0].strip()
    return (first_line or None), first_date_in_text(body_text)

def parse_notice_posts_from_html(session: requests.Session, html: str, current_list_url: Optional[str] = None) -> List[Post]:
    soup = BeautifulSoup(html, "html.parser")
    posts: List[Post] = []
    listcategory = derive_listcategory_from_list_url(current_list_url or "") or ""

    # 1) noticedetail 앵커 우선
    for a in soup.find_all("a", class_=lambda c: c and "noticedetail" in c.lower(), href=True):
        seq = (a.get("name") or "").strip()
        if not seq.isdigit():
            continue
        detail_url = f"{BASE_URL}/Notice/NoticeView?seq_no={seq}" + (f"&listcategory={listcategory}" if listcategory else "")
        title = a.get_text(strip=True) or detail_url
        tr = a.find_parent("tr")
        date_text = guess_date_from_tr(tr) if tr else guess_date_near(a)
        posts.append(Post(id=seq, title=title, url=detail_url, date=date_text))
    if posts:
        uniq: Dict[str, Post] = {p.id: p for p in posts}
        return sorted(uniq.values(), key=sort_key_post, reverse=True)

    # 2) href NoticeView
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
    if posts:
        uniq: Dict[str, Post] = {p.id: p for p in posts}
        return sorted(uniq.values(), key=sort_key_post, reverse=True)

    # 3) 실패 시: seq 후보로 상세 열어 파싱
    seqs = extract_seqs_from_html(html)
    result2: List[Post] = []
    if seqs:
        count = 0
        for seq in seqs:
            if count >= DETAIL_FETCH_MAX:
                break
            detail_url = f"{BASE_URL}/Notice/NoticeView?seq_no={seq}" + (f"&listcategory={listcategory}" if listcategory else "")
            try:
                detail_html = get_html(session, detail_url, referer=current_list_url)
                title, date_text = parse_notice_detail(detail_html)
                title = title or detail_url
                result2.append(Post(id=seq, title=title, url=detail_url, date=date_text))
                count += 1
            except Exception:
                continue
    if result2:
        uniq: Dict[str, Post] = {p.id: p for p in result2}
        return sorted(uniq.values(), key=sort_key_post, reverse=True)

    return []

# ---------------------------- 파싱: 하위 카테고리 ----------------------------

LISTVIEW_LINK_PAT = re.compile(r"^/Notice/ListView/[\w\-]+", re.I)

def extract_sublist_urls(html: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    urls: Set[str] = set()
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

def format_lines(posts: List[Post]) -> str:
    lines = []
    for p in posts:
        date = f" ({p.date})" if p.date else ""
        lines.append(f"- {p.title}{date}\n  {p.url}")
    return "\n".join(lines)

# ---------------------------- 메인 ----------------------------

def main():
    session = make_session()
    seen = load_seen()

    baseline_needed = FIRST_RUN_NOTIFY and (not BASELINE_FLAG_FILE.exists())

    # 1) 하위 카테고리 수집
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

    # 2) 모든 하위 카테고리에서 게시글 수집
    all_posts: List[Post] = []
    for sub in sorted(sublists):
        try:
            sub_html = get_html(session, sub, referer=sub)
            posts = parse_notice_posts_from_html(session, sub_html, current_list_url=sub)
            if posts:
                all_posts.extend(posts)
            else:
                logging.info("게시글(NoticeView) 없음: %s", sub)
        except Exception:
            logging.exception("하위 카테고리 처리 오류: %s", sub)

    # 고유화 + 최신순 정렬
    uniq_map: Dict[str, Post] = {}
    for p in all_posts:
        uniq_map[p.id] = p
    all_posts_sorted = sorted(uniq_map.values(), key=sort_key_post, reverse=True)

    # 3) 스냅샷(최초 1회만, 최신 10개 표시 / seen에는 전체 등록)
    if baseline_needed and all_posts_sorted:
        topn = all_posts_sorted[:max(1, FIRST_RUN_TOP_N)]
        text = "KALMATE 공지 BASELINE 스냅샷 (최신 10건)\n\n" + format_lines(topn)
        notify_telegram(text)
        # seen에는 전체를 저장(스냅샷 이후엔 새 글만 알림)
        now = int(time.time())
        for p in all_posts_sorted:
            seen[p.id] = {"title": p.title, "url": p.url, "date": p.date, "ts": now}
        save_seen(seen)
        BASELINE_FLAG_FILE.write_text("done", encoding="utf-8")
        logging.info("스냅샷 %d건 전송(표시 %d건), baseline 플래그 생성 완료", len(all_posts_sorted), len(topn))
        return

    # 4) 평소 모드: 새 글만 추려서 전송
    new_posts = [p for p in all_posts_sorted if p.id not in seen]
    if new_posts:
        new_posts_sorted = sorted(new_posts, key=sort_key_post, reverse=True)
        msg = f"KALMATE 공지 새 글 알림 ({len(new_posts_sorted)}건)\n\n" + format_lines(new_posts_sorted)
        notify_telegram(msg)
        now = int(time.time())
        for p in new_posts_sorted:
            seen[p.id] = {"title": p.title, "url": p.url, "date": p.date, "ts": now}
        save_seen(seen)
        logging.info("새 글 %d건 처리 완료", len(new_posts_sorted))
    else:
        logging.info("새 글 없음")

if __name__ == "__main__":
    main()
