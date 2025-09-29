#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Korean Air Agent bulletin watcher (프레임 지원 + 디버그)
- 대상: https://agent.koreanair.com/service/usage/bulletin
- 동작:
  1) Playwright(Chromium) 접속 → 배너/팝업 닫기
  2) 모든 프레임(iframe 포함)을 순회하며 상세글 링크 수집
     - 허용: /service/usage/bulletin/<slug|id> 또는 ?query
     - 제외: 정확한 목록 루트(/service/usage/bulletin)
  3) 최초 1회 스냅샷(최신 10건) 전송 → 상태파일 커밋
  4) 이후 새 글만 전송
  5) 디버그: 실행 단계 핑(Telegram), 각 프레임 HTML/스크린샷 저장(/tmp)
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
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout, Frame

# ---------- 설정/상태 ----------
STATE_FILE = Path(__file__).with_name("seen_posts.json")
BASELINE_FLAG = Path(__file__).with_name(".kal_baseline_done")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

START_URL = os.getenv("START_URL", "https://agent.koreanair.com/service/usage/bulletin")
MAX_ITEMS = int(os.getenv("MAX_ITEMS", "60"))
SNAPSHOT_TOP_N = int(os.getenv("SNAPSHOT_TOP_N", "10"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# 상세글 허용(슬러그 또는 ?쿼리), 목록 루트 제외
BULLETIN_LINK_OK = re.compile(r"/service/usage/bulletin(?:/[^?#]+|\?[^#]+)", re.I)
BULLETIN_ROOT = re.compile(r"^/service/usage/bulletin/?$", re.I)

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

def tg_ping(text: str):
    try:
        token = os.getenv("TG_BOT_TOKEN")
        chat_id = os.getenv("TG_CHAT_ID")
        if not (token and chat_id):
            return
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": text},
            timeout=10,
        )
    except Exception:
        pass

def load_seen() -> Dict[str, Dict]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            logging.warning("seen_posts.json 읽기 실패 → 새로 시작")
    return {}

def save_seen(seen: Dict[str, Dict]):
    STATE_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=2), encoding="utf-8")

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

# ---------- 프레임 단위 추출 ----------
def extract_posts_in_frame(frame: Frame, idx: int, save_debug: bool) -> List[Post]:
    """
    주어진 frame에서 상세 링크만 수집.
    필요 시 frame HTML/스크린샷을 /tmp 에 저장.
    """
    base = frame.url
    posts: List[Post] = []

    sel = 'a[href*="/service/usage/bulletin"]'
    try:
        frame.wait_for_selector(sel, timeout=5000)
    except PWTimeout:
        pass

    anchors = frame.locator(sel)
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
        path = urlparse(full).path
        if BULLETIN_ROOT.search(path):
            continue
        if not BULLETIN_LINK_OK.search(full):
            continue

        date_hint = None
        try:
            parent_txt = a.evaluate("el => el.closest('li, tr, article, div')?.innerText || ''")
            date_hint = extract_date_near(parent_txt)
        except Exception:
            pass

        posts.append(Post(title=title, url=full, date=date_hint))

    # 디버그 저장
    if save_debug:
        try:
            html = frame.content()
            Path(f"/tmp/kal_frame_{idx}.html").write_text(html, encoding="utf-8")
            # 스크린샷은 프레임 기준으로 바로 찍기 어려워 페이지에서 찍는 게 보통이지만,
            # 여기서는 frame.screenshot()이 가능하면 저장, 실패 시 무시
            try:
                frame.screenshot(path=f"/tmp/kal_frame_{idx}.png")
            except Exception:
                pass
        except Exception:
            pass

    # 중복 제거
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

def format_posts(posts: List[Post]) -> str:
    lines = []
    for p in posts:
        date = f" ({p.date})" if p.date else ""
        lines.append(f"- {p.title}{date}\n  {p.url}")
    return "\n".join(lines)

# ---------- 메인 ----------
def main():
    seen = load_seen()

    # 디버그 플래그
    STARTUP_PING = os.getenv("STARTUP_PING", "0").lower() in {"1", "true", "yes"}
    FORCE_SNAPSHOT = os.getenv("FORCE_SNAPSHOT", "0").lower() in {"1", "true", "yes"}
    DEBUG_HTML = os.getenv("DEBUG_HTML", "0").lower() in {"1", "true", "yes"}

    if STARTUP_PING:
        tg_ping("▶️ KAL Agent watcher 시작: " + os.getenv("START_URL", START_URL))

    want_snapshot = (not BASELINE_FLAG.exists()) or FORCE_SNAPSHOT

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
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except PWTimeout:
            pass
        dismiss_banners(page)

        # 페이지 자체 스크린샷(디버그)
        if DEBUG_HTML:
            try:
                page.screenshot(path="/tmp/kal_page.png", full_page=True)
                Path("/tmp/kal_page.html").write_text(page.content(), encoding="utf-8")
            except Exception:
                pass

        # === 모든 프레임 순회 ===
        posts: List[Post] = []
        frames = page.frames
        logging.info("frames: %d", len(frames))
        for i, fr in enumerate(frames):
            try:
                sub = extract_posts_in_frame(fr, i, DEBUG_HTML)
                posts.extend(sub)
            except Exception:
                continue

        # 중복 제거(최종)
        uniq = {}
        for p in posts:
            if p.id not in uniq:
                uniq[p.id] = p
        posts = list(uniq.values())

        logging.info("ALL frames collected: %d", len(posts))
        if STARTUP_PING:
            tg_ping(f"ℹ️ 프레임 합계: {len(posts)}건")

        browser.close()

    if not posts:
        logging.info("게시글을 찾지 못했습니다.")
        if STARTUP_PING:
            tg_ping("❗ 게시글을 찾지 못했습니다. (로그/아티팩트 확인)")
        return

    # 최초 1회 스냅샷
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
