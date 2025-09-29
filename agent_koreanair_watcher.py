#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Korean Air Agent bulletin watcher (강화판)
- 대상: https://agent.koreanair.com/service/usage/bulletin
- 동작:
  1) Playwright로 진입(ko-KR, Asia/Seoul, 커스텀 UA) → 배너/팝업 자동 닫기
  2) 상세글 링크만 수집: a[href*="/service/usage/bulletin/"] 이면서
     정확히 .../bulletin 으로 끝나는 목록 루트 링크는 제외
  3) 최초 1회 스냅샷(최신 10건) → 상태 파일 커밋 전제
  4) 이후엔 새 글만 알림
  5) 실패 시 HTML 정규식 Fallback (page.content() → requests.get())

필요 ENV(Secrets 권장):
  TG_BOT_TOKEN, TG_CHAT_ID
  START_URL (기본: https://agent.koreanair.com/service/usage/bulletin)
  SNAPSHOT_TOP_N (기본 10), MAX_ITEMS(기본 60)
  KAL_USER, KAL_PASS  # 로그인 필요할 때만(보통 불필요)
"""
import os
import re
import json
import time
import hashlib
import logging
from pathlib import Path
from typing import List, Dict, Optional
from urllib.parse import urljoin

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ---------- 설정/상태 ----------
STATE_FILE = Path(__file__).with_name("seen_posts.json")
BASELINE_FLAG = Path(__file__).with_name(".kal_baseline_done")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

START_URL = os.getenv("START_URL", "https://agent.koreanair.com/service/usage/bulletin")
MAX_ITEMS = int(os.getenv("MAX_ITEMS", "60"))
SNAPSHOT_TOP_N = int(os.getenv("SNAPSHOT_TOP_N", "10"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

DETAIL_PATH_PAT = re.compile(r"/service/usage/bulletin/(?!$)[^?#/][^?#]*", re.I)

# ---------- 유틸 ----------
class Post:
    def __init__(self, title: str, url: str, date: Optional[str] = None):
        self.title = (title or "").strip()
        self.url = (url or "").strip()
        self.id = self.url if self.url else hashlib.sha1(f"{self.title}|{self.url}".encode()).hexdigest()
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

# ---------- 로그인 & 배너 제거 ----------
def try_login(page) -> bool:
    user = os.getenv("KAL_USER")
    pwd = os.getenv("KAL_PASS")
    if not (user and pwd):
        logging.info("로그인 정보 미제공 → 비로그인으로 시도")
        return False
    cands = [
        {"user": 'input[name="username"]', "pass": 'input[name="password"]', "submit": 'button[type="submit"]'},
        {"user": '#username', "pass": '#password', "submit": 'button[type="submit"]'},
        {"user": 'input[name="userId"]', "pass": 'input[name="userPwd"]', "submit": 'button, input[type="submit"]'},
    ]
    for c in cands:
        try:
            page.wait_for_selector(c["user"], timeout=2000)
            page.fill(c["user"], user)
            page.fill(c["pass"], pwd)
            page.click(c["submit"])
            page.wait_for_load_state("networkidle", timeout=8000)
            logging.info("로그인 시도(성공 추정)")
            return True
        except Exception:
            continue
    logging.info("로그인 시도 실패/불필요")
    return False

def dismiss_banners(page):
    labels = ["동의", "확인", "닫기", "Accept", "Agree", "OK"]
    for txt in labels:
        try:
            btn = page.locator(f'button:has-text("{txt}")')
            if btn.count() > 0:
                btn.first.click(timeout=1000)
        except Exception:
            pass

# ---------- 날짜 추출(옵션) ----------
DATE_PATS = [
    re.compile(r"\b(20\d{2})[./-](\d{1,2})[./-](\d{1,2})\b"),
    re.compile(r"\b(20\d{2})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일\b"),
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

# ---------- 목록 추출 ----------
def extract_posts_via_dom(page) -> List[Post]:
    """
    DOM에서 상세글 링크만 수집:
    a[href*="/service/usage/bulletin/"] 이면서, 정확히 .../bulletin 루트는 제외
    """
    base = page.url
    posts: List[Post] = []

    sel = 'a[href*="/service/usage/bulletin/"]'
    try:
        page.wait_for_selector(sel, timeout=15000)
    except PWTimeout:
        pass

    try:
        page.mouse.wheel(0, 2000)
        page.wait_for_timeout(500)
    except Exception:
        pass

    anchors = page.locator(sel)
    count = anchors.count()
    for i in range(min(count, MAX_ITEMS)):
        a = anchors.nth(i)
        try:
            href = (a.get_attribute("href") or "").strip()
            title = (a.inner_text() or "").strip()
        except Exception:
            continue
        if not href or len(title) < 3:
            continue
        full = absolutize(base, href)
        if full.rstrip("/").endswith("/service/usage/bulletin"):
            continue
        if not DETAIL_PATH_PAT.search(href):
            continue
        date_hint = None
        try:
            parent_txt = a.evaluate("el => el.closest('li, tr, article, div')?.innerText || ''")
            date_hint = extract_date_near(parent_txt)
        except Exception:
            pass
        posts.append(Post(title=title, url=full, date=date_hint))

    seen = set()
    out: List[Post] = []
    for p in posts:
        if p.id in seen:
            continue
        seen.add(p.id)
        out.append(p)
        if len(out) >= MAX_ITEMS:
            break
    return out

ANCHOR_RE = re.compile(
    r'<a[^>]+href=["\'](?P<href>[^"\']*/service/usage/bulletin/[^"\']+)["\'][^>]*>(?P<text>.*?)</a>',
    re.I | re.S
)
TAG_STRIP_RE = re.compile(r"<[^>]+>")

def extract_posts_via_html(html: str, base: str) -> List[Post]:
    posts: List[Post] = []
    for m in ANCHOR_RE.finditer(html or ""):
        href = m.group("href")
        text = TAG_STRIP_RE.sub("", m.group("text")).strip()
        if not href or not text:
            continue
        full = absolutize(base, href)
        if full.rstrip("/").endswith("/service/usage/bulletin"):
            continue
        if not DETAIL_PATH_PAT.search(href):
            continue
        posts.append(Post(text, full))
        if len(posts) >= MAX_ITEMS:
            break
    seen = set()
    out: List[Post] = []
    for p in posts:
        if p.id in seen:
            continue
        seen.add(p.id)
        out.append(p)
    return out

def format_posts(posts: List[Post]) -> str:
    lines = []
    for p in posts:
        date = f" ({p.date})" if p.date else ""
        lines.append(f"- {p.title}{date}\n  {p.url}")
    return "\n".join(lines)

# ---------- 메인 ----------
def main():
    seen = load_seen()
    want_snapshot = (not BASELINE_FLAG.exists())

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(
            user_agent=UA,
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            java_script_enabled=True,
            viewport={"width": 1366, "height": 900},
        )
        context.set_extra_http_headers({"Accept-Language": "ko,en;q=0.9"})
        page = context.new_page()

        page.goto(START_URL, wait_until="domcontentloaded", timeout=30000)
        try_login(page)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except PWTimeout:
            pass
        dismiss_banners(page)

        posts = extract_posts_via_dom(page)

        if not posts:
            try:
                html = page.content()
                posts = extract_posts_via_html(html, page.url)
            except Exception:
                posts = []

        if not posts:
            try:
                resp = requests.get(START_URL, headers={"User-Agent": UA, "Accept-Language": "ko"}, timeout=15)
                if resp.ok:
                    posts = extract_posts_via_html(resp.text, START_URL)
            except Exception:
                pass

        browser.close()

    if not posts:
        logging.info("게시글을 찾지 못했습니다.")
        return

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

    new_posts = [p for p in posts if p.id not in seen]
    if new_posts:
        msg = f"KAL Agent 새 글 알림 ({len(new_posts)}건)\n\n" + format_posts(new_posts)
        notify_telegram(msg)
        now = int(time.time())
        for p in new_posts:
            seen[p.id] = {"title": p.title, "url": p.url, "date": p.date, "ts": now}
        save_seen(seen)
        logging.info("새 글 %d건 전송/저장 완료", len(new_posts))
    else:
        logging.info("새 글 없음")

if __name__ == "__main__":
    main()
