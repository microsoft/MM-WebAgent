#!/usr/bin/env python3
"""
Playwright screenshot helper used by the generation pipeline.

Automatically starts a local `http-server` instance and uses Playwright
(sync API) to screenshot HTML pages, with support for iframe / ECharts /
lazy-load content.

Example:
python agent/screenshot_html.py \
  -r outputs/000000024/1_20251217_182707_7311 \
  -o outputs/000000024/1_20251217_182707_7311/save.png \
  --fullpage
"""

import os
import time
import socket
import shutil
import sys
import subprocess
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

# ------------------------
# Configuration
# ------------------------
MAX_SCROLL_STEPS = int(os.getenv("SCREENSHOT_MAX_SCROLL_STEPS", "40"))
SCROLL_STEP_DELAY_MS = int(os.getenv("SCREENSHOT_SCROLL_STEP_DELAY_MS", "350"))
EXECUTABLE_PATH = shutil.which("google-chrome-stable")

# ------------------------
# Scroll helpers
# ------------------------
def _evaluate_scroll_height(page) -> int:
    height = page.evaluate(
        """
        Math.max(
            document.body.scrollHeight || 0,
            document.documentElement.scrollHeight || 0,
            window.innerHeight || 0,
        )
        """
    )
    return int(height or 0)


def _scroll_to_bottom_and_top(page) -> None:
    page.evaluate("window.scrollTo(0, 0)")
    last_height = _evaluate_scroll_height(page)

    for _ in range(MAX_SCROLL_STEPS):
        page.evaluate("window.scrollBy(0, window.innerHeight)")
        page.wait_for_timeout(SCROLL_STEP_DELAY_MS)

        new_height = _evaluate_scroll_height(page)
        reached_bottom = page.evaluate(
            """
            window.scrollY + window.innerHeight >=
            Math.max(
                document.body.scrollHeight,
                document.documentElement.scrollHeight,
            ) - 2
            """
        )
        if reached_bottom and abs(new_height - last_height) < 2:
            break

        last_height = new_height

    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(1000)


# ------------------------
# Main screenshot function (sync)
# ------------------------
def screenshot_main_html_dirs_http(
    root: Path,
    out_file: Path,
    fname: str = None,
    width=1280,
    height=720,
    full_page=True,
    wait_ms=2500,
    selector=None,
    port=None,
    timeout_ms=300000,
    executable_path=EXECUTABLE_PATH,
    debug=False,
):
    if not isinstance(root, Path):
        root = Path(root)
    if out_file is not None and not isinstance(out_file, Path):
        out_file = Path(out_file)

    if out_file is not None:
        out_file.parent.mkdir(parents=True, exist_ok=True)

    # Locate the HTML file
    if fname is None:
        html_files = list(root.glob("*.html"))
        if not html_files:
            if debug:
                print("HTML file not found.")
            return None, None
        html_path = html_files[0]
    else:
        html_path = root / fname

    # Pick a free port
    if port is None:
        s = socket.socket()
        s.bind(("", 0))
        port = s.getsockname()[1]
        s.close()

    def wait_until_port_open(port, timeout=5):
        start = time.time()
        while time.time() - start < timeout:
            try:
                sock = socket.create_connection(("127.0.0.1", port), timeout=0.5)
                sock.close()
                return True
            except OSError:
                time.sleep(0.1)
        return False

    if debug:
        print(f"[HTTP] Serving {root} at http://localhost:{port}/")
    server = subprocess.Popen(
        ["npx", "http-server", "-p", str(port), "."],
        cwd=str(root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    if not wait_until_port_open(port):
        server.terminate()
        raise RuntimeError("Failed to start http-server.")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox"],
                executable_path=executable_path,
            )

            context = browser.new_context(
                viewport={"width": width, "height": height},
                device_scale_factor=1,
            )

            page = context.new_page()
            url = f"http://localhost:{port}/{html_path.name}"
            if debug:
                print(f"[+] Opening {url}")

            page.goto(url, wait_until="load", timeout=timeout_ms)

            if wait_ms:
                page.wait_for_timeout(wait_ms)

            # Scroll to trigger lazy loading
            _scroll_to_bottom_and_top(page)

            # Take the screenshot
            if selector:
                locator = page.locator(selector)
                if locator.count() == 0:
                    if debug:
                        print(f"Selector {selector} not found; falling back to full-page screenshot.")
                    image_bytes = page.screenshot(
                        path=out_file, full_page=full_page
                    )
                else:
                    image_bytes = locator.first.screenshot(path=out_file)
            else:
                image_bytes = page.screenshot(
                    path=out_file, full_page=full_page
                )

            if debug:
                print(f"Saved screenshot to {out_file}")

            page.close()
            context.close()
            browser.close()

            return html_path, image_bytes

    finally:
        if debug:
            print("Stopping HTTP server")
        server.terminate()


# ------------------------
# CLI
# ------------------------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Screenshot HTML using http-server + Playwright (sync)"
    )
    parser.add_argument("--root", "-r", required=True, type=Path)
    parser.add_argument("--out", "-o", default=None, type=Path)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fullpage", action="store_true")
    parser.add_argument("--wait-ms", type=int, default=2500)
    parser.add_argument("--selector", type=str, default=None)
    parser.add_argument("--timeout-ms", type=int, default=300000)
    args = parser.parse_args()

    if not args.root.exists():
        print("Root directory does not exist.")
        sys.exit(1)

    html_path, img_bytes = screenshot_main_html_dirs_http(
        root=args.root,
        out_file=args.out,
        width=args.width,
        height=args.height,
        full_page=args.fullpage,
        wait_ms=args.wait_ms,
        selector=args.selector,
        timeout_ms=args.timeout_ms,
        port=args.port,
        executable_path=EXECUTABLE_PATH,
        debug=True,
    )

    if img_bytes:
        save_path = args.out or "shots/test_savefig.png"
        print(f"Screenshot completed: {save_path}")
