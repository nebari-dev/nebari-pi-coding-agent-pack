#!/usr/bin/env python3
import argparse
import json
import os
import sys
import time
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright


def _has_login_form(page):
    try:
        if page.locator('input[name="username"]').count() > 0 and page.locator('input[name="password"]').count() > 0:
            return True
    except Exception:
        return False
    return False


def _try_login(page, username, password):
    if not _has_login_form(page):
        return False
    if not username or not password:
        raise RuntimeError("login_required_but_missing_credentials")

    page.fill('input[name="username"]', username)
    page.fill('input[name="password"]', password)

    if page.locator('button[type="submit"]').count() > 0:
        page.click('button[type="submit"]')
    elif page.locator('input[type="submit"]').count() > 0:
        page.click('input[type="submit"]')
    else:
        page.keyboard.press("Enter")

    page.wait_for_load_state("domcontentloaded", timeout=30000)
    return True


def main():
    parser = argparse.ArgumentParser(description="Lightweight browser smoke-check for JupyterHub app URLs")
    parser.add_argument("--base-url", required=True, help="Base URL, e.g. http://proxy-public.data-science.svc.cluster.local")
    parser.add_argument("--app-path", required=True, help="App path, e.g. /user/admin/my-app/")
    parser.add_argument("--username", default=os.getenv("PI_BROWSER_SMOKE_USERNAME"), help="Hub username")
    parser.add_argument("--password", default=os.getenv("PI_BROWSER_SMOKE_PASSWORD"), help="Hub password")
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--screenshot", default="/tmp/pi-browser-smoke.png")
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/") + "/"
    app_url = urljoin(base_url, args.app_path.lstrip("/"))
    login_shim_url = urljoin(base_url, "services/japps/jhub-login")

    result = {
        "status": "failed",
        "base_url": base_url,
        "app_url": app_url,
        "final_url": None,
        "http_status": None,
        "title": None,
        "body_text_length": 0,
        "visible_dom": False,
        "screenshot": args.screenshot,
        "error": None,
    }

    pending_markers = ["/hub/spawn-pending/", "/_temp/jhub-app-proxy/"]

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            context = browser.new_context(ignore_https_errors=True)
            page = context.new_page()

            # Prime japps cookie/auth path
            page.goto(login_shim_url, wait_until="domcontentloaded", timeout=30000)
            _try_login(page, args.username, args.password)

            # Hit login shim once more after login to ensure japps token cookie
            page.goto(login_shim_url, wait_until="domcontentloaded", timeout=30000)
            _try_login(page, args.username, args.password)

            deadline = time.time() + args.timeout_seconds
            while True:
                response = page.goto(app_url, wait_until="domcontentloaded", timeout=30000)
                _try_login(page, args.username, args.password)

                final_url = page.url
                title = page.title().strip()
                body_text_len = page.evaluate("() => (document.body && document.body.innerText ? document.body.innerText.trim().length : 0)")
                visible_dom = page.evaluate(
                    """() => {
                      if (!document.body) return false;
                      const candidates = [document.body, ...document.body.querySelectorAll('*')];
                      for (const el of candidates) {
                        const style = window.getComputedStyle(el);
                        if (!style || style.display === 'none' || style.visibility === 'hidden') continue;
                        const rect = el.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0) return true;
                      }
                      return false;
                    }"""
                )
                http_status = response.status if response is not None else None

                blocked = any(marker in final_url for marker in pending_markers)
                expected_path = args.app_path.rstrip("/")
                has_expected_path = expected_path in final_url
                looks_loaded = (http_status is None or (200 <= http_status < 400)) and bool(visible_dom)

                if has_expected_path and not blocked and looks_loaded:
                    result.update(
                        {
                            "status": "passed",
                            "final_url": final_url,
                            "http_status": http_status,
                            "title": title,
                            "body_text_length": body_text_len,
                            "visible_dom": bool(visible_dom),
                        }
                    )
                    page.screenshot(path=args.screenshot, full_page=True)
                    break

                if time.time() >= deadline:
                    result.update(
                        {
                            "status": "failed",
                            "final_url": final_url,
                            "http_status": http_status,
                            "title": title,
                            "body_text_length": body_text_len,
                            "visible_dom": bool(visible_dom),
                            "error": "timeout_waiting_for_user_visible_page",
                        }
                    )
                    page.screenshot(path=args.screenshot, full_page=True)
                    break

                time.sleep(3)

            browser.close()

    except Exception as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    if result["status"] == "passed":
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
