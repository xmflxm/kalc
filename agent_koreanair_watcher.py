#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Korean Air Agent bulletin watcher (클릭 네비게이션 방식)
- 최신순 페이지에서 각 항목을 실제 클릭하여 상세 URL을 확보
- 최초 1회: 스냅샷 10건 전송
- 이후: 새 글만 전송
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

# ---- 상태 파일 ----
STATE_FILE = Path(__file__).with_name("seen_posts.json")
BASELINE_FLAG = Path(__file__).with_name(".kal_baseline_done")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# 최신순 페이지(사용자가 확인한 URL)
DEFAULT_START = "https://agent.koreanair.com/service/usage/bulletin?currentPage=1&sortByNewest=true"
START_URL = os.getenv("START_URL", DEFAULT_START)

MAX_LIST = int(os.getenv("MAX_LIST", "40"))       # 한 번에 시도할 목록 수(안정성 위해 과하지 않게)
SNAPSHOT_TOP_N = int(os.getenv("SNAPSHOT_TOP_N", "10"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

DETAIL_OK = re.compile(r"/service/usage/bulletin/[^/?#]+", re.I)

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

def collect_posts_by_click(page) -> List[Post]:
    """목록에서 제목을 '클릭'하여 상세 URL을 얻는다."""
    posts: List[Post] = []

    # 목록 UL/OL 로드 대기
    list_selector = "ol, ul"
    try:
        page.wait_for_selector(list_selector, timeout=10000)
    except PWTimeout:
        logging.info("목록 컨테이너 대기 실패")
        return posts

    # 각 항목(li) 찾기 (핀/카테고리 등 클래스 다양 → li 전체에서 제목 영역 찾기)
    items = page.locator("li").all()
    if not items:
        logging.info("li 항목 없음")
        return posts

    count = min(len(items), MAX_LIST)
    logging.info("목록 항목 %d개 중 %d개 시도", len(items), count)

    for idx in range(count):
        li = items[idx]
        try:
            # 제목 텍스트가 들어간 영역(스팬) 찾기
            title_span = li.locator("span").first
            title_txt = title_span.inner_text().strip()
            if not title_txt or len(title_txt) < 2:
                continue

            # 날짜도 같이 추출(있으면)
            date_txt = ""
            try:
                date_txt = li.locator("p:has-text('-')").last.inner_text().strip()
                # YYYY-MM-DD 형태만 간단 필터
                if not re.search(r"\d{4}-\d{2}-\d{2}", date_txt):
                    date_txt = ""
            except Exception:
                pass

            # 제목을 감싼 a/div 클릭 시 상세로 이동하는 구조 → 클릭
            # a 태그에 href가 없어도 click 가능
            clickable = li.locator("a, div, span").filter(has_text=title_txt).first
            # 현재 URL 기억
            before = page.url
            # 새 탭이 아니라 same-tab 라우팅 가정
            with page.expect_navigation(wait_until="domcontentloaded", timeout=10000):
                clickable.click()

            # 이동 후 URL 검사
            cur = page.url
            if DETAIL_OK.search(cur):
                posts.append(Post(title=title_txt, url=cur, date=date_txt))
            else:
                logging.info("상세 패턴 불일치: %s", cur)

            # 뒤로 가서 목록 복귀
            page.go_back(wait_until="domcontentloaded")
            # 목록 재등장 대기
            page.wait_for_selector(list_selector, timeout=8000)

            # 너무 빠른 클릭 방지
            time.sleep(0.2)

        except Exception as e:
            logging.info("항목 %d 처리 실패: %s", idx, e)
            # 목록 복구 시도
            try:
                if not page.locator(list_selector).is_visible():
                    page.goto(START_URL, wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass
            continue

    # 중복 제거(제목/URL 기준)
    uniq = {}
    for p in posts:
        if p.id not in uniq:
            uniq[p.id] = p
    return list(uniq.values())

def main():
    seen = load_seen()
    want_snapshot = not BASELINE_FLAG.exists()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
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

    # 최신순 페이지이므로, 날짜 텍스트가 있으면 최신순 정렬 비슷하게 보정
    def key_func(p: Post):
        # YYYY-MM-DD → 정렬핵
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
