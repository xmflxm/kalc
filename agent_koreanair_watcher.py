name: KAL Agent Watcher

on:
  schedule:
    - cron: "*/30 * * * *"   # 30분마다 실행(UTC)
  workflow_dispatch: {}

permissions:
  contents: write

concurrency:
  group: kal-agent-watcher
  cancel-in-progress: false

jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.x"

      - name: Install Playwright
        run: |
          python -m pip install --upgrade pip
          pip install playwright requests
          python -m playwright install chromium

      - name: Run watcher
        env:
          TG_BOT_TOKEN: ${{ secrets.TG_BOT_TOKEN }}
          TG_CHAT_ID: ${{ secrets.TG_CHAT_ID }}
          START_URL: "https://agent.koreanair.com/service/usage/bulletin"
          # 디버그(임시): 실행 상태 핑 + HTML/스크린샷 저장
          STARTUP_PING: "1"
          DEBUG_HTML: "1"
          # 필요시 로그인(보통 불필요)
          # KAL_USER: ${{ secrets.KAL_USER }}
          # KAL_PASS: ${{ secrets.KAL_PASS }}
          # 강제 스냅샷 1회(필요 시만 잠깐 켜기)
          # FORCE_SNAPSHOT: "1"
        run: |
          python agent_koreanair_watcher.py

      - name: Upload captured artifacts (debug)
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: kal-artifacts
          path: |
            /tmp/kal_page.html
            /tmp/kal_page.png
            /tmp/kal_frame_*.html
            /tmp/kal_frame_*.png
          if-no-files-found: ignore

      - name: Commit & push state if changed
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add -A
          if git diff --cached --quiet --exit-code; then
            echo "No changes to commit."
          else
            git commit -m "chore: update state files [skip ci]"
            git push
          fi
