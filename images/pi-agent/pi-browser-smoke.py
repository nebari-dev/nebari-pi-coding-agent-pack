#!/usr/bin/env python3
import argparse
import json
import os
import time
from urllib.parse import quote, urlencode, urljoin

import requests
from playwright.sync_api import sync_playwright


def _has_login_form(page):
    try:
        return (
            page.locator('input[name="username"]').count() > 0
            and page.locator('input[name="password"]').count() > 0
        )
    except Exception:
        return False


def _maybe_login(page, username, password, strict=False):
    if not _has_login_form(page):
        return False
    if not username or not password:
        if strict:
            raise RuntimeError("login_required_but_missing_credentials")
        return False

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


def _create_ephemeral_user_token(hub_api_url, hub_api_token, username):
    url = f"{hub_api_url.rstrip('/')}/users/{quote(username)}/tokens"
    resp = requests.post(
        url,
        headers={"Authorization": f"token {hub_api_token}", "Content-Type": "application/json"},
        json={"expires_in": 300, "note": "pi-browser-smoke-bootstrap"},
        timeout=20,
    )
    if resp.status_code not in (200, 201):
        return None, None, f"token_create_failed_{resp.status_code}"

    payload = resp.json() if resp.content else {}
    token_value = payload.get("token")
    token_id = payload.get("id")
    if not token_value:
        return None, None, "token_create_missing_token"
    return token_value, token_id, None


def _delete_token(hub_api_url, hub_api_token, username, token_id):
    if not token_id:
        return
    try:
        requests.delete(
            f"{hub_api_url.rstrip('/')}/users/{quote(username)}/tokens/{quote(str(token_id))}",
            headers={"Authorization": f"token {hub_api_token}"},
            timeout=20,
        )
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description="Lightweight browser smoke-check for JupyterHub app URLs")
    parser.add_argument("--base-url", required=True, help="Base URL, e.g. http://proxy-public.data-science.svc.cluster.local")
    parser.add_argument("--app-path", required=True, help="App path, e.g. /user/admin/my-app/")
    parser.add_argument("--username", default=os.getenv("PI_BROWSER_SMOKE_USERNAME") or os.getenv("JUPYTERHUB_USER") or os.getenv("NB_USER"), help="Hub username")
    parser.add_argument("--password", default=os.getenv("PI_BROWSER_SMOKE_PASSWORD"), help="Hub password")
    parser.add_argument("--hub-api-url", default=os.getenv("NEBARI_HUB_API_URL") or os.getenv("JUPYTERHUB_API_URL"), help="Hub API URL for token bootstrap")
    parser.add_argument("--hub-api-token", default=os.getenv("NEBARI_HUB_API_TOKEN") or os.getenv("JUPYTERHUB_API_TOKEN"), help="Hub API token for token bootstrap")
    parser.add_argument("--disable-token-bootstrap", action="store_true", help="Disable token bootstrap and use only form login")
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
        "bootstrap_mode": "none",
        "screenshot": args.screenshot,
        "error": None,
    }

    pending_markers = ["/hub/spawn-pending/", "/_temp/jhub-app-proxy/"]

    bootstrap_token = None
    bootstrap_token_id = None

    if not args.disable_token_bootstrap and args.hub_api_token and args.username:
        if args.hub_api_url:
            token, token_id, err = _create_ephemeral_user_token(args.hub_api_url, args.hub_api_token, args.username)
            if token:
                bootstrap_token = token
                bootstrap_token_id = token_id
                result["bootstrap_mode"] = "ephemeral_user_token"
            else:
                result["bootstrap_mode"] = f"fallback_hub_token:{err}"
                bootstrap_token = args.hub_api_token
        else:
            bootstrap_token = args.hub_api_token
            result["bootstrap_mode"] = "provided_hub_token"

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            context = browser.new_context(ignore_https_errors=True)
            page = context.new_page()

            if bootstrap_token and args.username:
                bootstrap_query = urlencode(
                    {
                        "token": bootstrap_token,
                        "user": args.username,
                        "next": "/services/japps/jhub-login",
                    }
                )
                bootstrap_url = urljoin(base_url, f"pi-token-login?{bootstrap_query}")
                page.goto(bootstrap_url, wait_until="domcontentloaded", timeout=30000)
                _maybe_login(page, args.username, args.password, strict=False)

            # Prime japps cookie/auth path
            page.goto(login_shim_url, wait_until="domcontentloaded", timeout=30000)
            _maybe_login(page, args.username, args.password, strict=not bool(bootstrap_token))

            # Hit login shim once more after login/bootstrap to ensure japps token cookie
            page.goto(login_shim_url, wait_until="domcontentloaded", timeout=30000)
            _maybe_login(page, args.username, args.password, strict=not bool(bootstrap_token))

            deadline = time.time() + args.timeout_seconds
            while True:
                response = page.goto(app_url, wait_until="domcontentloaded", timeout=30000)
                _maybe_login(page, args.username, args.password, strict=not bool(bootstrap_token))

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

                if (
                    "/hub/login" in final_url
                    and bootstrap_token
                    and not (args.username and args.password)
                    and time.time() + 5 < deadline
                ):
                    result.update(
                        {
                            "status": "failed",
                            "final_url": final_url,
                            "http_status": http_status,
                            "title": title,
                            "body_text_length": body_text_len,
                            "visible_dom": bool(visible_dom),
                            "error": "token_bootstrap_failed_login_required",
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
    finally:
        if bootstrap_token_id and args.hub_api_url and args.hub_api_token and args.username:
            _delete_token(args.hub_api_url, args.hub_api_token, args.username, bootstrap_token_id)

    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
