#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Korean Air Agent bulletin watcher (Playwright)
- 대상: https://agent.koreanair.com/service/usage/bulletin
- 동작:
  1) 페이지 접속(필요시 로그인 시도 가능)
  2) 게시글 링크(경로에 /service/usage/bulletin/ 포함)만 위에서부터 수집
  3) 최초 1회: 스냅샷(최신 10건)만 전송 → 상태파일 커밋
  4) 이후: 새 글이 있을 때만 그 글들만 전송

환경변수(Secrets 권장):
  TG_BOT_TOKEN, TG_CHAT_ID   : 텔레그램
  START_URL                  : 시작 URL(기본 https://agent.koreanair.com/service/usage/bulletin)
  MAX_ITEMS                  : 긁을 최대 개수(기본 60)
  SNAPSHOT_TOP_N             : 스냅샷 표시 개수(기본 10)
  KAL_USER, KAL_PASS         : (선택) 포털 로그인 필요 시
"""

import os
import re
import json
import time
import hashlib
import logging
from pathlib import Path
from typing import List, Dict, Optional
from urllib.parse import urljoin, urlparse

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# 상태 파일(레포 루트에 생성 → 커밋되어야 함)
STATE_FILE = Path(__file__).with_name("seen_posts.json")
BASELINE_FLAG = Path(__file__).with_name(".kal_baseline_done")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

START_URL = os.getenv("START_URL", "https://agent.koreanair.com/service/usage/bulletin")
MAX_ITEMS = int(os.getenv("MAX_ITEMS", "60"))
SNAPSHOT_TOP_N = int(os.getenv("SNAPSHOT_TOP_N", "10"))

# ---------------- 유틸 ----------------

class Post:
    def __init__(self, title: str, url: str, date: Optional[str] = None):
        self.title = title.strip()
        self.url = url.strip()
        # URL이 가장 신뢰되는 ID. 없으면 title+url 해시 백업
        self.id = self.url if self.url else hashlib.sha1(f"{self.title}|{self.url}".encode("utf-8")).hexdigest()
        self.date = date

def absolutize(base: str, href: str) -> str:
    try:
        return urljoin(base, href)
    except Exception:
        return href

def notify_telegram(text: str):
    token = os.getenv("TG_BOT_TOKEN")
    chat_id = os.getenv("TG_CHAT_ID")
    if not (token and chat_id):
        logging.warning("텔레그램 토큰/챗ID 미설정 → 알림 생략")
        return
    api = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(api, data={"chat_id": chat_id, "text": text})
    if r.status_code != 200:
        logging.warning("텔레그램 전송 실패: %s %s", r.status_code, r.text)

def load_seen() -> Dict[str, Dict]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            logging.warning("seen_posts.json 읽기 실패 → 새로 시작")
    return {}

def save_seen(seen: Dict[str, Dict]):
    STATE_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=2), encoding="utf-8")

# ---------------- 로그인(필요시) ----------------

def try_login(page) -> bool:
    user = os.getenv("KAL_USER")
    pwd = os.getenv("KAL_PASS")
    if not (user and pwd):
        logging.info("로그인 정보 미제공 → 비로그인으로 시도")
        return False

    candidates = [
        {"user": 'input[name="username"]', "pass": 'input[name="password"]', "submit": 'button[type="submit"]'},
        {"user": '#username', "pass": '#password', "submit": 'button[type="submit"]'},
        {"user": 'input[type="email"]', "pass": 'input[type="password"]', "submit": 'button[type="submit"]'},
        {"user": 'input[name="userId"]', "pass": 'input[name="userPwd"]', "submit": 'button, input[type="submit"]'},
    ]
    for c in candidates:
        try:
            page.wait_for_selector(c["user"], timeout=2000)
            page.fill(c["user"], user)
            page.fill(c["pass"], pwd)
            page.click(c["submit"])
            page.wait_for_load_state("networkidle", timeout=8000)
            logging.info("로그인 시도(성공 추정)")
            return True
        except PWTimeout:
            continue
        except Exception:
            continue
    logging.info("로그인 시도 실패 또는 불필요")
    return False

# ---------------- 목록 추출 ----------------

DATE_PATS = [
    re.compile(r"\b(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})\b"),
    re.compile(r"\b(20\d{2})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일\b"),
    re.compile(r"\b(20\d{2})\.(\d{1,2})\.(\d{1,2})\b"),
]

def extract_date_near(text: str) -> Optional[str]:
    if not text:
        return None
    for pat in DATE_PATS:
        m = pat.search(text)
        if m:
            try:
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                return f"{y:04d}-{mo:02d}-{d:02d}"
            except Exception:
                pass
    return None

def extract_posts(page) -> List[Post]:
    """
    /service/usage/bulletin/xxxx 형식의 상세 링크만 수집.
    최신이 상단이라고 가정 → 보이는 순서 그대로 가져옴.
    """
    base = page.url
    posts: List[Post] = []

    # 1) 게시판 컨테이너(클래스명에 bulletin/board/list 등) 우선 검색
    containers = page.query_selector_all(
        'section[class*="bulletin"], div[class*="bulletin"], main[class*="bulletin"], '
        'section[class*="board"], div[class*="board"], ul[class*="list"], ol[class*="list"]'
    )
    if not containers:
        containers = page.query_selector_all("main, section, article, div")

    def add_from_anchors(anchors):
        for a in anchors:
            try:
                href = (a.get_attribute("href") or "").strip()
                title = (a.inner_text() or "").strip()
            except Exception:
                continue
            if not href or len(title) < 3:
                continue
            # 상세 글 링크만: /service/usage/bulletin/... 포함
            if "/service/usage/bulletin" not in href:
                continue
            full = absolutize(base, href)
            # 같은 페이지 내 앵커/자바스크립트 링크 제외
            if full.lower().startswith("javascript:"):
                continue

            # 날짜 힌트(부모/근처 텍스트에서)
            date_hint = None
            try:
                parent_txt = a.evaluate("el => el.closest('li, tr, article, div')?.innerText || ''")
                date_hint = extract_date_near(parent_txt)
            except Exception:
                pass

            posts.append(Post(title=title, url=full, date=date_hint))

    for c in containers:
        anchors = c.query_selector_all("a")
        add_from_anchors(anchors)
        if len(posts) >= MAX_ITEMS:
            break

    # 중복 제거: url 기준
    seen = set()
    deduped: List[Post] = []
    for p in posts:
        if p.id in seen:
            continue
        seen.add(p.id)
        deduped.append(p)
        if len(deduped) >= MAX_ITEMS:
            break

    return deduped

def format_posts(posts: List[Post]) -> str:
    lines = []
    for p in posts:
        date = f" ({p.date})" if p.date else ""
        lines.append(f"- {p.title}{date}\n  {p.url}")
    return "\n".join(lines)

# ---------------- 메인 ----------------

def main():
    seen = load_seen()
    want_snapshot = (not BASELINE_FLAG.exists())

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        page.goto(START_URL, wait_until="domcontentloaded", timeout=30000)
        try_login(page)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except PWTimeout:
            pass

        posts = extract_posts(page)
        browser.close()

    if not posts:
        logging.info("게시글을 찾지 못했습니다.")
        return

    # 최초 1회 스냅샷(최신 10개)
    if want_snapshot:
        topn = posts[:SNAPSHOT_TOP_N]
        text = "KAL Agent 스냅샷 (최신 10건)\n\n" + format_posts(topn)
        notify_telegram(text)

        now = int(time.time())
        for p in posts:
            seen[p.id] = {"title": p.title, "url": p.url, "date": p.date, "ts": now}
        save_seen(seen)
        BASELINE_FLAG.write_text("done", encoding="utf-8")
        logging.info("스냅샷 전송 및 상태 파일 생성 완료")
        return

    # 이후: 새 글만
    new_posts = [p for p in posts if p.id not in seen]
    if new_posts:
        text = f"KAL Agent 새 글 알림 ({len(new_posts)}건)\n\n" + format_posts(new_posts)
        notify_telegram(text)
        now = int(time.time())
        for p in new_posts:
            seen[p.id] = {"title": p.title, "url": p.url, "date": p.date, "ts": now}
        save_seen(seen)
        logging.info("새 글 %d건 전송/저장 완료", len(new_posts))
    else:
        logging.info("새 글 없음")

if __name__ == "__main__":
    main()
