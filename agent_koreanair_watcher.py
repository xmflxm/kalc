#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Korean Air Agent bulletin watcher
- 최신순 페이지에서 카드(목록)를 실제 클릭 → 상세 URL/제목/등록일 수집
- 최초 1회 스냅샷(10건), 이후 새 글만 텔레그램 알림
"""

import os
import re
import json
import time
import hashlib
import logging
from pathlib import Path
from typing import List, Dict, Optional

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ----- 상태 파일 -----
STATE_FILE = Path(__file__).with_name("seen_posts.json")
BASELINE_FLAG = Path(__file__).with_name(".kal_baseline_done")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# 최신순 페이지(확인된 링크)
DEFAULT_START = "https://agent.koreanair.com/service/usage/bulletin?currentPage=1&sortByNewest=true"
START_URL = os.getenv("START_URL", DEFAULT_START)

MAX_LIST = int(os.getenv("MAX_LIST", "80"))        # 클릭 후보 개수
SNAPSHOT_TOP_N = int(os.getenv("SNAPSHOT_TOP_N", "10"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# 상세 URL 패턴
DETAIL_OK = re.compile(r"/service/usage/bulletin/[^/?#]+", re.I)

# ===== 유틸 =====
class Post:
    def __init__(self, title: str, url: str, date: Optional[str] = None):
        self.title = (title or "").strip()
        self.url = (url or "").strip()
        self.id = self.url if self.url else hashlib.sha1(f"{self.title}|{self.url}".encode()).hexdigest()
        self.date = (date or "").strip() or None

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

def format_posts(posts: List[Post]) -> str:
    lines = []
    for p in posts:
        date = f" ({p.date})" if p.date else ""
        lines.append(f"- {p.title}{date}\n  {p.url}")
    return "\n".join(lines)

def dismiss_banners(page):
    labels = ["동의", "확인", "닫기", "Accept", "Agree", "OK", "확인하기"]
    for txt in labels:
        try:
            btn = page.locator(f'button:has-text("{txt}")')
            if btn.count() > 0:
                btn.first.click(timeout=800)
        except Exception:
            pass

def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def _to_ymd(s: str) -> Optional[str]:
    if not s:
        return None
    # 2025-10-10 / 2025.10.10 / 2025년 10월 10일 → YYYY-MM-DD
    m = re.search(r"(20\d{2})[.\-년\s]*(\d{1,2})[.\-월\s]*(\d{1,2})", s)
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    # ✅ 유효 범위 검증 (0일, 13월 같은 값 거르기)
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None
    return f"{y:04d}-{mo:02d}-{d:02d}"

# ===== 1-a 수정: 상세 페이지에서 '제목' 정확 추출 =====
def get_detail_title(page) -> Optional[str]:
    """상세 페이지의 '제목' 필드 우선 추출(사이트 전용 휴리스틱)."""
    # 1) dt:has('제목') + dd
    try:
        loc = page.locator("dt:has-text('제목') + dd").first
        if loc.count() > 0:
            v = _clean(loc.inner_text())
            if len(v) > 3:
                return v
    except Exception:
        pass
    # 2) 카드/본문 헤더 후보
    for sel in ["article h3", "article h2", "section h3", "section h2"]:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                v = _clean(loc.inner_text())
                if len(v) > 3 and not re.search(r"공지사항\s*상세|상세보기", v):
                    return v
        except Exception:
            pass
    # 3) og:title
    try:
        v = page.locator('meta[property="og:title"]').first.get_attribute("content")
        v = _clean(v)
        if v and len(v) > 3 and not re.search(r"공지사항\s*상세|상세보기", v):
            return v
    except Exception:
        pass
    # 4) h1/h2/role=heading
    for sel in ["h1", "h2", "[role='heading']"]:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                v = _clean(loc.inner_text())
                if len(v) > 3 and not re.search(r"공지사항\s*상세|상세보기", v):
                    return v
        except Exception:
            pass
    # 5) <title> (사이트명 꼬리 제거)
    try:
        v = _clean(page.title())
        if len(v) > 3:
            v = re.sub(r"\s*[-|–]\s*KAL.*$", "", v)
            if not re.search(r"공지사항\s*상세|상세보기", v):
                return v
    except Exception:
        pass
    return None

def get_detail_date(page) -> Optional[str]:
    """상세 페이지의 '등록일'을 YYYY-MM-DD로 추출."""
    # 1) dt:has('등록일') + dd
    try:
        loc = page.locator("dt:has-text('등록일') + dd").first
        if loc.count() > 0:
            return _to_ymd(loc.inner_text())
    except Exception:
        pass
    # 2) <time> 태그
    try:
        loc = page.locator("time").first
        if loc.count() > 0:
            v = _to_ymd(loc.inner_text() or loc.get_attribute("datetime") or "")
            if v:
                return v
    except Exception:
        pass
    # 3) 본문에서 최후 보정
    try:
        return _to_ymd(page.inner_text("body"))
    except Exception:
        return None

# ===== 목록 클릭 수집 =====
def collect_posts_by_click(page) -> List[Post]:
    posts: List[Post] = []

    # 네트워크 안정화 + 스크롤로 항목 더 로딩
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    try:
        for _ in range(2):
            page.mouse.wheel(0, 1800)
            page.wait_for_timeout(350)
    except Exception:
        pass

    # 클릭 후보(넓게)
    CAND_SELECTOR = (
        "a, [role='link'], button, [onclick], "
        "li, article, div[class*='list'], div[class*='item']"
    )
    elems = page.locator(CAND_SELECTOR)
    total = elems.count()
    n = min(total, MAX_LIST)
    logging.info("클릭 후보 %d개 중 %d개 시도", total, n)

    for i in range(n):
        el = elems.nth(i)
        try:
            txt = _clean(el.inner_text())
            if len(txt) < 4:
                continue

            before = page.url
            with page.expect_navigation(wait_until="domcontentloaded", timeout=10000):
                el.click()

            cur = page.url
            if DETAIL_OK.search(cur):
                # 상세에서 제목/등록일 정확 추출 (★ 1-a 반영)
                title = get_detail_title(page) or txt
                date_txt = get_detail_date(page) or ""
                posts.append(Post(title=title, url=cur, date=date_txt))
            else:
                logging.info("상세 패턴 불일치: %s", cur)

            # 목록 복귀
            page.go_back(wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            page.wait_for_timeout(200)

        except Exception as e:
            logging.info("클릭 실패(%d): %s", i, e)
            try:
                page.goto(START_URL, wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass
            continue

    # 중복 제거
    uniq, out = set(), []
    for p in posts:
        if p.id in uniq:
            continue
        uniq.add(p.id)
        out.append(p)
    return out

# ===== 메인 =====
def main():
    seen = load_seen()
    want_snapshot = not BASELINE_FLAG.exists()
    FORCE_SNAPSHOT = os.getenv("FORCE_SNAPSHOT", "0").lower() in {"1", "true", "yes"}
    if FORCE_SNAPSHOT:
        want_snapshot = True

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
            viewport={"width": 1366, "height": 950},
        )
        page = context.new_page()
        page.goto(START_URL, wait_until="domcontentloaded", timeout=30000)
        try:
            dismiss_banners(page)
            page.wait_for_load_state("networkidle", timeout=8000)
        except PWTimeout:
            pass

        posts = collect_posts_by_click(page)
        browser.close()

    if not posts:
        logging.info("게시글을 찾지 못했습니다.")
        return

    # 날짜 기준 정렬(있으면)
    def key_func(p: Post):
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", p.date or "")
        if m:
            return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return (0, 0, 0)

    posts.sort(key=key_func, reverse=True)

    if want_snapshot:
        topn = posts[:SNAPSHOT_TOP_N]
        text = "KAL Agent 스냅샷 (최신 10건)\n\n" + format_posts(topn)
        notify_telegram(text)
        now = int(time.time())
        for p in posts:
            seen[p.id] = {"title": p.title, "url": p.url, "date": p.date, "ts": now}
        save_seen(seen)
        BASELINE_FLAG.write_text("done", encoding="utf-8")
        logging.info("스냅샷 전송 및 상태 저장 완료")
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
