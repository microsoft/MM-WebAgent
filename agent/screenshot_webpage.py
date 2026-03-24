import sys
sys.path.append(".")

import os
import re
import json
import shutil
import asyncio
import hashlib
import shlex
import argparse
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Union, List, Dict, Optional, Tuple
from io import BytesIO
from pathlib import Path

from PIL import Image

from utils import (
    parse_score,
    get_openai_request_url,
    request_chatgpt_i2t_until_success,
    request_chatgpt_i2i_until_success,
)
from utils.mm_utils import (
    get_user_prompt,
    parse_html_file,
    read_html_file,
)


def make_safe_id(s: str, max_len: int = 80) -> str:
    base = re.sub(r"[^a-zA-Z0-9]+", "_", s).strip("_")
    digest = hashlib.sha1(s.encode("utf-8")).hexdigest()[:8]
    if not base:
        return digest
    keep = max(1, max_len - 9)
    base = base[:keep]
    return f"{base}_{digest}"


# ============================================================
# Playwright: Take screenshot of webpage and embedded images
# ============================================================

JS_FIND_TARGET_ELEMENT = r"""(imageRef) => {
  function cleanRef(s) {
    return String(s || "").split("#")[0].split("?")[0];
  }
  const ref0 = cleanRef(imageRef);
  const base0 = ref0 ? ref0.split("/").pop() : "";
  const needles = Array.from(new Set([
    String(imageRef || "").toLowerCase(),
    String(ref0 || "").toLowerCase(),
    String(base0 || "").toLowerCase(),
  ])).filter(Boolean);

  function matches(str) {
    if (!str) return false;
    const s = String(str).toLowerCase();
    return needles.some(n => s.includes(n));
  }
  function isVisible(el) {
    if (!el) return false;
    const cs = getComputedStyle(el);
    if (cs.display === "none" || cs.visibility === "hidden") return false;
    const op = parseFloat(cs.opacity || "1");
    if (op < 0.05) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    return true;
  }
  function area(el) {
    const r = el.getBoundingClientRect();
    return Math.max(0, r.width) * Math.max(0, r.height);
  }

  // clear previous marks
  document.querySelectorAll("[data-reflection-target]").forEach(el => {
    el.removeAttribute("data-reflection-target");
  });

  const candidates = [];

  // 1) Prefer <img> first
  document.querySelectorAll("img").forEach(img => {
    const src = img.currentSrc || img.getAttribute("src") || img.src;
    if (matches(src) && isVisible(img)) {
      candidates.push({ el: img, pri: 2, a: area(img) });
    }
  });

  // 2) background-image / mask-image (including ::before/::after)
  const all = document.querySelectorAll("*");
  for (const el of all) {
    if (!isVisible(el)) continue;
    const cs = getComputedStyle(el);
    const bg = cs.backgroundImage;
    const b1 = getComputedStyle(el, "::before").backgroundImage;
    const b2 = getComputedStyle(el, "::after").backgroundImage;
    const mi = cs.maskImage || cs.webkitMaskImage;
    const m1 = getComputedStyle(el, "::before").maskImage || getComputedStyle(el, "::before").webkitMaskImage;
    const m2 = getComputedStyle(el, "::after").maskImage || getComputedStyle(el, "::after").webkitMaskImage;
    if (matches(bg) || matches(b1) || matches(b2) || matches(mi) || matches(m1) || matches(m2)) {
      candidates.push({ el, pri: 1, a: area(el) });
    }
  }

  candidates.sort((x, y) => (y.pri - x.pri) || (y.a - x.a));
  const chosen = candidates.find(c => c.el && !["HTML", "BODY"].includes(c.el.tagName) && c.a >= 16 * 16) || candidates[0];
  if (!chosen || !chosen.el) return { found: false };

  chosen.el.setAttribute("data-reflection-target", "1");
  return {
    found: true,
    tag: chosen.el.tagName,
    isImg: chosen.el.tagName === "IMG",
    area: chosen.a,
  };
}"""


JS_HIDE_DESCENDANTS = r"""() => {
  const el = document.querySelector('[data-reflection-target="1"]');
  if (!el) return 0;
  const nodes = Array.from(el.querySelectorAll("*"));
  for (const n of nodes) {
    if (n.dataset.reflOldCssText !== undefined) continue;
    n.dataset.reflOldCssText = n.style.cssText;
    n.style.visibility = "hidden";
  }
  return nodes.length;
}"""


JS_RESTORE_DESCENDANTS = r"""() => {
  const el = document.querySelector('[data-reflection-target="1"]');
  if (!el) return 0;
  const nodes = Array.from(el.querySelectorAll("*"));
  for (const n of nodes) {
    if (n.dataset.reflOldCssText === undefined) continue;
    n.style.cssText = n.dataset.reflOldCssText;
    delete n.dataset.reflOldCssText;
  }
  return nodes.length;
}"""


JS_CLEAR_MARK = r"""() => {
  document.querySelectorAll("[data-reflection-target]").forEach(el => {
    el.removeAttribute("data-reflection-target");
  });
}"""


JS_GET_TARGET_INFO = r"""() => {
  const el = document.querySelector('[data-reflection-target="1"]');
  if (!el) return { found: false };
  const cs = getComputedStyle(el);
  const r = el.getBoundingClientRect();
  const parent = el.parentElement;
  const pr = parent ? parent.getBoundingClientRect() : null;

  const out = {
    found: true,
    tag: el.tagName,
    rect: { x: r.x, y: r.y, width: r.width, height: r.height },
    parentTag: parent ? parent.tagName : null,
    parentRect: pr ? { width: pr.width, height: pr.height } : null,
    computed: {
      objectFit: cs.objectFit,
      objectPosition: cs.objectPosition,
      backgroundImage: cs.backgroundImage,
      backgroundSize: cs.backgroundSize,
      backgroundPosition: cs.backgroundPosition,
      backgroundRepeat: cs.backgroundRepeat,
      overflow: cs.overflow,
      display: cs.display,
      position: cs.position,
    },
  };

  if (el.tagName === "IMG") {
    out.img = {
      currentSrc: el.currentSrc || el.getAttribute("src") || el.src,
      naturalWidth: el.naturalWidth,
      naturalHeight: el.naturalHeight,
    };
  }
  return out;
}"""

JS_MARK_INLINE_CANVASES = r"""() => {
  const canvases = Array.from(document.querySelectorAll("canvas"));
  canvases.forEach((c, i) => {
    c.setAttribute("data-inline-chart-idx", String(i));
  });
  return canvases.length;
}"""

JS_MARK_INLINE_SVGS = r"""() => {
  try {
    const svgs = Array.from(document.querySelectorAll("svg"));
    svgs.forEach((el, i) => {
      el.setAttribute("data-inline-svg-idx", String(i));
    });
    return svgs.length;
  } catch (e) {
    return 0;
  }
}"""

JS_GET_INLINE_SVG_INFO = r"""(idx) => {
  const sel = `[data-inline-svg-idx="${String(idx)}"]`;
  const el = document.querySelector(sel);
  if (!el) return { found: false };
  const cs = getComputedStyle(el);
  const r = el.getBoundingClientRect();
  return {
    found: true,
    tag: el.tagName,
    id: el.id || null,
    className: el.className ? String(el.className) : null,
    rect: { x: r.x, y: r.y, width: r.width, height: r.height },
    computed: { display: cs.display, visibility: cs.visibility, opacity: cs.opacity, position: cs.position },
  };
}"""

JS_MARK_ECHARTS_INSTANCES = r"""() => {
  try {
    const nodes = Array.from(document.querySelectorAll("[_echarts_instance_]"));
    nodes.forEach((el, i) => {
      el.setAttribute("data-inline-echarts-idx", String(i));
    });
    return nodes.length;
  } catch (e) {
    return 0;
  }
}"""

JS_GET_INLINE_ECHARTS_INFO = r"""(idx) => {
  const sel = `[data-inline-echarts-idx="${String(idx)}"]`;
  const el = document.querySelector(sel);
  if (!el) return { found: false };
  const cs = getComputedStyle(el);
  const r = el.getBoundingClientRect();
  return {
    found: true,
    tag: el.tagName,
    id: el.id || null,
    className: el.className ? String(el.className) : null,
    rect: { x: r.x, y: r.y, width: r.width, height: r.height },
    computed: { display: cs.display, visibility: cs.visibility, opacity: cs.opacity, position: cs.position },
  };
}"""
JS_GET_INLINE_CANVAS_INFO = r"""(idx) => {
  const sel = `[data-inline-chart-idx="${String(idx)}"]`;
  const el = document.querySelector(sel);
  if (!el) return { found: false };
  const cs = getComputedStyle(el);
  const r = el.getBoundingClientRect();
  return {
    found: true,
    tag: el.tagName,
    id: el.id || null,
    className: el.className ? String(el.className) : null,
    rect: { x: r.x, y: r.y, width: r.width, height: r.height },
    computed: {
      display: cs.display,
      visibility: cs.visibility,
      opacity: cs.opacity,
      position: cs.position,
    },
  };
}"""


async def screenshot_webpage_and_embedded_images(
    *,
    root: Path,
    webpage_fname: str,
    image_refs: List[str],
    chart_refs: List[str],
    inline_chart_targets: Optional[List[Dict]] = None,
    fullpage_out_file: Path,
    embedded_out_dir: Path,
    width: int = 1280,
    height: int = 720,
    wait_ms: int = 2500,
    port: Optional[int] = None,
    timeout_ms: int = 300000,
) -> Tuple[bytes, Dict[str, Optional[bytes]], Dict[str, Optional[Dict]]]:
    """Open the webpage once and return the full-page screenshot, per-image embedded screenshots, and embedding diagnostics."""
    from subprocess import Popen
    import subprocess
    import socket
    import time
    import sys
    try:
        from playwright.async_api import async_playwright  # type: ignore
    except Exception as e:
        raise RuntimeError(
            "Screenshot/evaluation requires Playwright. Install it and the Chromium browser:\n"
            "  pip install playwright\n"
            "  python -m playwright install chromium\n"
            f"Original import error: {type(e).__name__}: {e}"
        )

    EXECUTABLE_PATH = shutil.which("google-chrome-stable")

    if not isinstance(root, Path):
        root = Path(root)
    if not isinstance(fullpage_out_file, Path):
        fullpage_out_file = Path(fullpage_out_file)
    if not isinstance(embedded_out_dir, Path):
        embedded_out_dir = Path(embedded_out_dir)

    fullpage_out_file.parent.mkdir(parents=True, exist_ok=True)
    embedded_out_dir.mkdir(parents=True, exist_ok=True)

    def _decode_stream(data: object) -> str:
        if data is None:
            return ""
        if isinstance(data, (bytes, bytearray)):
            try:
                return data.decode("utf-8", errors="replace")
            except Exception:
                return repr(data)
        return str(data)

    def wait_until_port_open(p: int, *, proc: "Popen", timeout: int = 15) -> tuple[bool, str]:
        """Wait until port is open, or the server process exits."""
        start = time.time()
        last_err = ""
        while time.time() - start < timeout:
            # If the process already exited, surface stdout/stderr for debugging.
            rc = proc.poll()
            if rc is not None:
                try:
                    out, err = proc.communicate(timeout=0.2)
                except Exception:
                    out, err = b"", b""
                out_s = _decode_stream(out).strip()
                err_s = _decode_stream(err).strip()
                msg = f"server process exited early (rc={rc})"
                if out_s:
                    msg += f"\n[stdout]\n{out_s}"
                if err_s:
                    msg += f"\n[stderr]\n{err_s}"
                return False, msg

            try:
                sock = socket.create_connection(("127.0.0.1", p), timeout=0.5)
                sock.close()
                return True, ""
            except OSError as e:
                last_err = str(e)
                time.sleep(0.1)
        return False, f"timeout waiting for port {p} to open (last_err={last_err})"

    port_was_none = port is None
    if port is None:
        s = socket.socket()
        # Bind localhost to avoid picking an IPv6-only address on some systems.
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()

    # Prefer a dependency-free server by default. You can force npx via WEBAGENT_HTTP_SERVER=npx.
    # - python: sys.executable -m http.server
    # - npx: npx --yes http-server (avoid interactive "Ok to proceed?")
    server_mode = (os.getenv("WEBAGENT_HTTP_SERVER", "python") or "python").strip().lower()
    if server_mode not in ("python", "npx"):
        server_mode = "python"

    if server_mode == "npx":
        server_cmd = ["npx", "--yes", "http-server", "-p", str(port), "."]
    else:
        server_cmd = [
            sys.executable,
            "-m",
            "http.server",
            str(port),
            "--bind",
            "127.0.0.1",
            "--directory",
            ".",
        ]

    startup_timeout = int(os.getenv("WEBAGENT_HTTP_SERVER_STARTUP_TIMEOUT_SEC", "20"))
    max_port_retries = int(os.getenv("WEBAGENT_HTTP_SERVER_PORT_RETRIES", "3"))
    attempt = 0
    server = None
    last_detail = ""
    while True:
        attempt += 1
        server = Popen(
            server_cmd,
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        ok, detail = wait_until_port_open(port, proc=server, timeout=startup_timeout)
        if ok:
            break

        last_detail = detail
        try:
            server.terminate()
        except Exception:
            pass

        # If we auto-picked the port, retry with a new ephemeral port (race with other processes).
        if port_was_none and attempt <= max_port_retries:
            try:
                s = socket.socket()
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
                s.close()
            except Exception:
                pass

            # Update server command with the new port.
            if server_mode == "npx":
                server_cmd = ["npx", "--yes", "http-server", "-p", str(port), "."]
            else:
                server_cmd = [
                    sys.executable,
                    "-m",
                    "http.server",
                    str(port),
                    "--bind",
                    "127.0.0.1",
                    "--directory",
                    ".",
                ]
            continue

        raise RuntimeError(f"Static server failed to start on port {port} (mode={server_mode}).\n{detail}")

    print(f"[HTTP] Serving {root} at http://localhost:{port}/ (mode={server_mode})")

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox"],
                executable_path=EXECUTABLE_PATH,
            )
            context = await browser.new_context(
                viewport={"width": width, "height": height},
                device_scale_factor=1,
            )

            page = await context.new_page()
            url = f"http://localhost:{port}/{webpage_fname}"
            print(f"[+] Opening {url}")

            await page.goto(url, wait_until="load", timeout=timeout_ms)
            if wait_ms:
                await page.wait_for_timeout(wait_ms)

            # Try to trigger lazy loading and let animations settle
            max_steps = int(os.getenv("SCREENSHOT_MAX_SCROLL_STEPS", "40"))
            step_delay = int(os.getenv("SCREENSHOT_SCROLL_STEP_DELAY_MS", "350"))
            await page.evaluate("window.scrollTo(0, 0)")
            for _ in range(max_steps):
                await page.evaluate("window.scrollBy(0, window.innerHeight)")
                await page.wait_for_timeout(step_delay)
                reached_bottom = await page.evaluate(
                    """
                    window.scrollY + window.innerHeight >= Math.max(
                        document.body.scrollHeight || 0,
                        document.documentElement.scrollHeight || 0
                    ) - 2
                    """
                )
                if reached_bottom:
                    break
            await page.evaluate("window.scrollTo(0, 0)")
            await page.wait_for_timeout(800)

            def _blank_png_bytes() -> bytes:
                try:
                    img = Image.new("RGB", (2, 2), (0, 0, 0))
                    buf = BytesIO()
                    img.save(buf, format="PNG")
                    return buf.getvalue()
                except Exception:
                    # Minimal valid PNG header + IHDR for 1x1; fallback if PIL breaks.
                    return b"\x89PNG\r\n\x1a\n"

            # Full-page screenshots can fail on very tall / heavy pages (Chromium protocol error).
            # Fall back to viewport screenshot to avoid aborting the whole evaluation run.
            try:
                fullpage_bytes = await page.screenshot(path=fullpage_out_file, full_page=True)
            except Exception:
                try:
                    fullpage_bytes = await page.screenshot(path=fullpage_out_file, full_page=False)
                except Exception:
                    fullpage_bytes = _blank_png_bytes()

            embedded_map: Dict[str, Optional[bytes]] = {}
            embedded_info_map: Dict[str, Optional[Dict]] = {}
            for image_ref in image_refs:
                image_id = make_safe_id(image_ref)
                out_file = embedded_out_dir / f"{image_id}_embedded.png"

                try:
                    info = await page.evaluate(JS_FIND_TARGET_ELEMENT, image_ref)
                except Exception:
                    info = {"found": False}

                if not info or not info.get("found"):
                    embedded_map[image_ref] = None
                    embedded_info_map[image_ref] = None
                    continue

                locator = page.locator('[data-reflection-target="1"]')
                is_img = bool(info.get("isImg", False))
                hidden = False
                try:
                    embedded_info_map[image_ref] = await page.evaluate(JS_GET_TARGET_INFO)
                except Exception:
                    embedded_info_map[image_ref] = None

                try:
                    if await locator.count() == 0:
                        embedded_map[image_ref] = None
                        embedded_info_map[image_ref] = embedded_info_map.get(image_ref)
                        continue

                    await locator.first.scroll_into_view_if_needed()
                    await page.wait_for_timeout(250)

                    # For background-image cases, hide descendants to avoid treating overlay text as part of the image.
                    if not is_img:
                        await page.evaluate(JS_HIDE_DESCENDANTS)
                        hidden = True
                        await page.wait_for_timeout(50)

                    embedded_bytes = await locator.first.screenshot(path=out_file)
                    embedded_map[image_ref] = embedded_bytes
                except Exception:
                    embedded_map[image_ref] = None
                finally:
                    try:
                        if hidden:
                            await page.evaluate(JS_RESTORE_DESCENDANTS)
                        await page.evaluate(JS_CLEAR_MARK)
                    except Exception:
                        pass

            for chart_ref in chart_refs:
                chart_id = make_safe_id(chart_ref)
                out_file = embedded_out_dir / f"{chart_id}_embedded.png"

                iframe_selector = f'iframe[src*="{chart_ref}"]'
                parent_selector = f'.viz:has(iframe[src*="{chart_ref}"])'
                
                async def _pick_first_visible(loc):
                    try:
                        n = await loc.count()
                    except Exception:
                        return None
                    for i in range(n):
                        cand = loc.nth(i)
                        try:
                            if await cand.is_visible():
                                return cand
                        except Exception:
                            continue
                    return None
                
                try:
                    locator = page.locator(parent_selector)
                    target = await _pick_first_visible(locator)
                    if target is not None:
                        await target.scroll_into_view_if_needed(timeout=timeout_ms)
                        await page.wait_for_timeout(500)
                        image_bytes = await target.screenshot(path=out_file)
                    else:
                        iframe_locator = page.locator(iframe_selector)
                        iframe_target = await _pick_first_visible(iframe_locator)
                        if iframe_target is not None:
                            await iframe_target.scroll_into_view_if_needed(timeout=timeout_ms)
                            await page.wait_for_timeout(500)
                            image_bytes = await iframe_target.screenshot(path=out_file)
                        else:
                            image_bytes = await page.screenshot(path=out_file, full_page=True)
                except Exception as e:
                    image_bytes = fullpage_bytes
                
                embedded_map[chart_ref] = image_bytes

            # Inline charts - screenshot by target kind (canvas/svg/echarts) using DOM order or selector.
            if inline_chart_targets:
                try:
                    await page.evaluate(JS_MARK_INLINE_CANVASES)
                except Exception:
                    pass
                try:
                    await page.evaluate(JS_MARK_INLINE_SVGS)
                except Exception:
                    pass
                try:
                    await page.evaluate(JS_MARK_ECHARTS_INSTANCES)
                except Exception:
                    pass

                for t in inline_chart_targets:
                    key = str(t.get("key") or "").strip()
                    kind = str(t.get("kind") or "").strip().lower() or "canvas"
                    selector = str(t.get("selector") or "").strip() or None

                    idx = None
                    try:
                        if t.get("idx") is not None:
                            idx = int(t.get("idx"))
                    except Exception:
                        idx = None

                    if not key:
                        if idx is not None:
                            key = f"inline:{kind}@{idx}"
                        else:
                            continue

                    if selector:
                        locator = page.locator(selector)
                        out_file = embedded_out_dir / f"{make_safe_id(key)}_embedded.png"
                        try:
                            if await locator.count() == 0:
                                embedded_map[key] = None
                                embedded_info_map[key] = None
                                continue
                        except Exception:
                            embedded_map[key] = None
                            embedded_info_map[key] = None
                            continue
                        try:
                            await locator.first.scroll_into_view_if_needed()
                            await page.wait_for_timeout(250)
                        except Exception:
                            pass
                        try:
                            info = await page.evaluate(
                                r"""(sel) => {
                                  const el = document.querySelector(sel);
                                  if (!el) return { found: false };
                                  const cs = getComputedStyle(el);
                                  const r = el.getBoundingClientRect();
                                  return {
                                    found: true,
                                    tag: el.tagName,
                                    id: el.id || null,
                                    className: el.className ? String(el.className) : null,
                                    rect: { x: r.x, y: r.y, width: r.width, height: r.height },
                                    computed: { display: cs.display, visibility: cs.visibility, opacity: cs.opacity, position: cs.position },
                                  };
                                }""",
                                selector,
                            )
                            embedded_info_map[key] = info
                        except Exception:
                            embedded_info_map[key] = None
                        try:
                            embedded_map[key] = await locator.first.screenshot(path=out_file)
                        except Exception:
                            embedded_map[key] = None
                        continue

                    if idx is None:
                        embedded_map[key] = None
                        embedded_info_map[key] = None
                        continue

                    if kind == "svg":
                        out_file = embedded_out_dir / f"inline_svg_{idx}_embedded.png"
                        locator = page.locator(f'[data-inline-svg-idx=\"{idx}\"]')
                        info_fn = JS_GET_INLINE_SVG_INFO
                    elif kind == "echarts":
                        out_file = embedded_out_dir / f"inline_echarts_{idx}_embedded.png"
                        locator = page.locator(f'[data-inline-echarts-idx=\"{idx}\"]')
                        info_fn = JS_GET_INLINE_ECHARTS_INFO
                    else:
                        out_file = embedded_out_dir / f"inline_canvas_{idx}_embedded.png"
                        locator = page.locator(f'[data-inline-chart-idx=\"{idx}\"]')
                        info_fn = JS_GET_INLINE_CANVAS_INFO

                    try:
                        if await locator.count() == 0:
                            embedded_map[key] = None
                            embedded_info_map[key] = None
                            continue
                    except Exception:
                        embedded_map[key] = None
                        embedded_info_map[key] = None
                        continue

                    try:
                        embedded_info_map[key] = await page.evaluate(info_fn, idx)
                    except Exception:
                        embedded_info_map[key] = None

                    try:
                        await locator.first.scroll_into_view_if_needed()
                        await page.wait_for_timeout(250)
                    except Exception:
                        pass

                    try:
                        image_bytes = await locator.first.screenshot(path=out_file)
                        embedded_map[key] = image_bytes
                    except Exception:
                        embedded_map[key] = None
                        embedded_info_map[key] = embedded_info_map.get(key)
            

            await page.close()
            await context.close()
            await browser.close()

            return fullpage_bytes, embedded_map, embedded_info_map
    finally:
        print("Stopping HTTP server")
        if server is not None:
            try:
                server.terminate()
                server.wait(timeout=2)
            except Exception:
                try:
                    server.kill()
                except Exception:
                    pass
