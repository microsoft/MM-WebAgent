import os
import re
import json
import hashlib
import requests
from urllib.parse import urlparse
import traceback
from glob import glob
from pathlib import Path
from bs4 import BeautifulSoup
from io import BytesIO
from PIL import Image
try:
    from decord import VideoReader, cpu  # type: ignore
except Exception:
    VideoReader = None  # type: ignore
    cpu = None  # type: ignore
import numpy as np
from typing import List, Optional
from typing import Optional
WEBPAGE_FIX_STYLE_ID = "reflection-image-v3-fixes"
WEBPAGE_FIX_BLOCK_PREFIX = "reflection-image-v3"

def make_safe_id(s: str, max_len: int = 80) -> str:
    base = re.sub(r"[^a-zA-Z0-9]+", "_", s).strip("_")
    digest = hashlib.sha1(s.encode("utf-8")).hexdigest()[:8]
    if not base:
        return digest
    # Reserve space for "_{digest}" to reduce collisions after sanitizing refs.
    keep = max(1, max_len - 9)
    base = base[:keep]
    return f"{base}_{digest}"


def apply_webpage_css_fixes(
    *,
    webpage_html_path: str,
    image_ref: str,
    image_id: str,
    webpage_solutions: List[str],
) -> bool:
    """
    Append model-generated webpage-side fixes (CSS rules) into the main HTML.

    Constraint: only append/replace the CSS block for the current image_id,
    without rewriting the overall HTML structure.
    """
    if not webpage_solutions:
        return False
    if not webpage_html_path or not os.path.exists(webpage_html_path):
        return False

    rules: List[str] = []
    for s in webpage_solutions:
        if not isinstance(s, str) or not s.strip():
            continue
        s2 = _strip_markdown_fences(s)
        # Require a CSS rule containing braces; otherwise skip it to avoid
        # injecting natural-language text directly into the style block.
        if "{" not in s2 or "}" not in s2:
            continue
        rules.append(s2.strip())

    if not rules:
        return False

    css_block = (
        f"/* BEGIN {WEBPAGE_FIX_BLOCK_PREFIX}:{image_id} ref:{image_ref} */\n"
        + "\n\n".join(rules)
        + f"\n/* END {WEBPAGE_FIX_BLOCK_PREFIX}:{image_id} */"
    )

    old_html = read_html_file(webpage_html_path)
    new_html = _remove_existing_fix_block(old_html, image_id)
    new_html = _upsert_style_block_and_append(new_html, style_id=WEBPAGE_FIX_STYLE_ID, css_block=css_block)

    if new_html != old_html:
        Path(webpage_html_path).write_text(new_html, encoding="utf-8")
        return True

    return False


def _strip_markdown_fences(text: str) -> str:
    if not isinstance(text, str):
        return ""
    t = text.strip()
    # Support both ```css ... ``` and generic ``` ... ``` fences.
    t = re.sub(r"^\s*```[a-zA-Z0-9_-]*\s*\n", "", t)
    t = re.sub(r"\n\s*```\s*$", "", t)
    return t.strip()



def _remove_existing_fix_block(html_text: str, image_id: str) -> str:
    if not html_text or not image_id:
        return html_text
    pat = re.compile(
        rf"/\*\s*BEGIN\s+{re.escape(WEBPAGE_FIX_BLOCK_PREFIX)}:{re.escape(image_id)}\s*\*/.*?/\*\s*END\s+{re.escape(WEBPAGE_FIX_BLOCK_PREFIX)}:{re.escape(image_id)}\s*\*/\s*",
        flags=re.IGNORECASE | re.DOTALL,
    )
    return re.sub(pat, "", html_text)


def _upsert_style_block_and_append(html_text: str, *, style_id: str, css_block: str) -> str:
    if not css_block.strip():
        return html_text

    # 1) If style#id already exists, insert before </style>.
    style_open_re = re.compile(
        rf"<style\b[^>]*\bid=['\"]{re.escape(style_id)}['\"][^>]*>",
        flags=re.IGNORECASE,
    )
    m = style_open_re.search(html_text or "")
    if m:
        close_m = re.search(r"</style\s*>", html_text[m.end():], flags=re.IGNORECASE)
        if close_m:
            insert_at = m.end() + close_m.start()
            return html_text[:insert_at] + "\n" + css_block.strip() + "\n" + html_text[insert_at:]

    # 2) Otherwise create a new style block and insert it before </head>
    #    (or at the file start if no </head> exists).
    new_style = (
        f"\n<style id=\"{style_id}\">\n"
        f"/* {WEBPAGE_FIX_BLOCK_PREFIX}: auto-generated fixes */\n"
        f"{css_block.strip()}\n"
        f"</style>\n"
    )
    head_close = re.search(r"</head\s*>", html_text or "", flags=re.IGNORECASE)
    if head_close:
        idx = head_close.start()
        return (html_text or "")[:idx] + new_style + (html_text or "")[idx:]
    return new_style + (html_text or "")



def get_iframe_height_from_html_sync(html_path: str) -> int:
    """Extract iframe height settings from an HTML file."""
    
    try:
        with open(html_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        match = re.search(r'iframe\s*\{[^}]*height\s*:\s*(\d+)px', content)
        if match:
            return int(match.group(1))
        
        return 180
    except Exception:
        return 180

def load_standalone_image_as_png_bytes(
    image_path: str,
    *,
    max_side: int = 1024,
) -> Optional[bytes]:
    """
    Load a local image (png/jpg/webp/...) and convert it to PNG bytes for
    vision input.

    Motivation:
    - v3 may pass the original image file as the third image to chat/completions.
    - Official vision endpoints may reject some MIME types or formats,
      especially webp, with 400 "Invalid image data."
    - Normalizing to PNG, with optional downsampling, improves stability and
      reduces payload size.
    """
    try:
        if not image_path or not os.path.exists(image_path):
            return None
        im = Image.open(image_path)
        # Force full load to avoid delayed-decoding edge cases.
        im.load()

        # Optional downsampling.
        if isinstance(max_side, int) and max_side > 0:
            w, h = im.size
            m = max(w, h)
            if m > max_side:
                scale = max_side / float(m)
                new_w = max(1, int(round(w * scale)))
                new_h = max(1, int(round(h * scale)))
                im = im.resize((new_w, new_h), Image.LANCZOS)

        # Normalize to RGBA to avoid palette/LA mode compatibility issues.
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA")

        buf = BytesIO()
        im.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception:
        return None



def strip_query_fragment(s: str) -> str:
    if not isinstance(s, str):
        return s
    s = s.split("#", 1)[0]
    s = s.split("?", 1)[0]
    return s


def extract_webpage_css_excerpt(webpage_html: str, max_length: int = 8000) -> str:
    """
    Extract CSS from webpage HTML, especially rules likely to affect chart rendering.
    """
    
    # Try to extract inline <style> contents.
    style_matches = re.findall(r'<style[^>]*>(.*?)</style>', webpage_html, re.DOTALL | re.IGNORECASE)
    css_content = "\n".join(style_matches)
    
    # If the CSS is too long, keep only likely relevant parts.
    if len(css_content) > max_length:
        # Prefer rules containing specific keywords.
        keywords = ['chart', 'viz', 'iframe', 'opacity', 'visibility', 'display', 'height', 'overflow']
        relevant_lines = []
        for line in css_content.split('\n'):
            if any(kw in line.lower() for kw in keywords):
                relevant_lines.append(line)
        css_content = "\n".join(relevant_lines[:200])  # cap the number of lines
    
    # Also extract iframe-related HTML structure.
    iframe_context = re.findall(r'<[^>]*(?:chart|viz|iframe)[^>]*>.*?</[^>]+>', webpage_html, re.DOTALL | re.IGNORECASE)
    html_excerpt = "\n".join(iframe_context[:10])  # cap the number of snippets
    
    return f"/* Relevant CSS Rules */\n{css_content}\n\n/* Relevant HTML Structure */\n{html_excerpt}"


def extract_html_excerpt(html_text: str, needle: str, max_length: int = 8000, context_chars: int = 900) -> str:
    if not html_text:
        return ""

    if not needle:
        return html_text[:max_length]

    needles = [needle]
    if needle.startswith("./"):
        needles.append(needle[2:])
    basename = os.path.basename(strip_query_fragment(needle))
    if basename and basename not in needles:
        needles.append(basename)

    matches: List[int] = []
    for n in needles:
        try:
            for m in re.finditer(re.escape(n), html_text):
                matches.append(m.start())
        except re.error:
            continue

    if not matches:
        return html_text[:max_length]

    matches = sorted(set(matches))[:3]
    chunks = []
    for idx in matches:
        start = max(0, idx - context_chars)
        end = min(len(html_text), idx + context_chars)
        chunks.append(html_text[start:end])

    excerpt = "\n...\n".join(chunks)
    if len(excerpt) > max_length:
        excerpt = excerpt[:max_length]
    return excerpt


def extract_design_prompt_excerpt(design_prompt: str, image_ref: str, max_length: int = 4500, context_chars: int = 900) -> str:
    """
    Keep only the prompt fragment most relevant to the current image, instead
    of sending the full long prompt into evaluation requests and risking 400
    errors from oversized payloads or token counts.
    """
    if not design_prompt:
        return ""
    # Reuse the HTML-excerpt strategy by locating image_ref / basename matches.
    return extract_html_excerpt(design_prompt, image_ref, max_length=max_length, context_chars=context_chars)



def sample_frames_from_video(video_path_or_bytes, K, max_side=512):
    """
    Sample K frames uniformly from a video path or video bytes.
    Uses decord, so no temporary file is required.
    """
    if video_path_or_bytes is None:
        return None
    if VideoReader is None or cpu is None:
        raise RuntimeError(
            "Video evaluation requires `decord` (missing in current env). "
            "Install it or disable `video_eval`."
        )

    # 1. Build a VideoReader from the input.
    if isinstance(video_path_or_bytes, (bytes, bytearray)):
        vr = VideoReader(BytesIO(video_path_or_bytes), ctx=cpu(0))
    else:
        vr = VideoReader(video_path_or_bytes, ctx=cpu(0))

    num_frames = len(vr)

    if K <= 0:
        raise ValueError("K must be positive")

    # 2. Choose frame indices.
    if K >= num_frames:
        indices = list(range(num_frames))
    else:
        indices = [int(i * num_frames / K) for i in range(K)]

    frames = []
    for idx in indices:
        frame = vr[idx].asnumpy()   # HWC, RGB
        if max_side and max(frame.shape[0], frame.shape[1]) > max_side:
            scale = max_side / float(max(frame.shape[0], frame.shape[1]))
            new_w = max(1, int(round(frame.shape[1] * scale)))
            new_h = max(1, int(round(frame.shape[0] * scale)))
            frame = np.array(Image.fromarray(frame).resize((new_w, new_h), Image.LANCZOS))
        frames.append(Image.fromarray(frame))

    return frames


def get_main_html(project_dir, fname=None):
    if fname is not None:
        html_path = os.path.join(project_dir, fname)
    else:
        html_path = glob(os.path.join(project_dir, "*.html"))[0]
    return html_path

def get_user_prompt(project_dir):
    with open(os.path.join(project_dir, "planner_output.json")) as fp:
        data = json.load(fp)
    user_prompt = data.get("user_prompt", "")
    return user_prompt

def get_generated_html(project_dir, fname="webpage_1.html"):
    with open(os.path.join(project_dir, fname), "r", encoding="utf-8") as f:
        generated_html = f.read()
    return generated_html

def get_evaluation_summary(project_dir, fname="evaluation_scores.json"):
    with open(os.path.join(project_dir, fname), "r", encoding="utf-8") as f:
        eval_summary = json.load(f)
    return eval_summary

def read_html_file(html_file):
    """load html content from file."""
    if html_file is None:
        return ""
        
    with open(html_file, "r", encoding="utf-8") as f:
        data = f.read()
    return data

def parse_html_file(html_file):
    """
    Parse an HTML file and extract all subresources:
    - images: <img>, <picture>, srcset, data-src, lazy refs, CSS background images
    - videos: <video>, <source>, data-src
    - charts: <iframe>, <object>, <embed>

    Returns:
        data: raw HTML text
        subfiles: {"image": [...], "video": [...], "chart": [...]}
    """
    subfiles = {"image": [], "video": [], "chart": [], "inline_chart_targets": []}

    html_file = Path(html_file)

    with open(html_file, "r", encoding="utf-8") as f:
        data = f.read()

    soup = BeautifulSoup(data, "html.parser")

    # ----------------------
    # images: <img>
    # ----------------------
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy")
        if src:
            subfiles["image"].append(src)

    # ----------------------
    # <picture> + <source srcset>
    # ----------------------
    for picture in soup.find_all("picture"):
        for source in picture.find_all("source"):
            srcset = source.get("srcset") or source.get("data-srcset")
            if srcset:
                for s in srcset.split(","):
                    s = s.strip().split(" ")[0]
                    if s:
                        subfiles["image"].append(s)

    # ----------------------
    # videos: <video> + <source>
    # ----------------------
    for vid in soup.find_all("video"):
        # the <video> tag itself
        src = vid.get("src") or vid.get("data-src") or vid.get("data-lazy")
        if src:
            subfiles["video"].append(src)
        # nested <source> tags
        for source in vid.find_all("source"):
            src = source.get("src") or source.get("data-src") or source.get("data-lazy")
            if src:
                subfiles["video"].append(src)

    # ----------------------
    # charts / embedded HTML
    # ----------------------
    for iframe in soup.find_all("iframe"):
        src = iframe.get("src")
        if src:
            subfiles["chart"].append(src)
    for tag in soup.find_all(["object", "embed"]):
        src = tag.get("data") or tag.get("src")
        if src:
            subfiles["chart"].append(src)
    # ----------------------
    # CSS background images
    # 1. inline style
    # 2. <style> blocks
    # 3. external CSS files
    # ----------------------
    url_pattern = re.compile(r"url\(['\"]?(.*?)['\"]?\)")

    # inline style
    for tag in soup.find_all(style=True):
        style = tag["style"]
        for u in url_pattern.findall(style):
            subfiles["image"].append(u)

    # <style> blocks
    for style_tag in soup.find_all("style"):
        css = style_tag.string
        if css:
            for u in url_pattern.findall(css):
                subfiles["image"].append(u)

    # external CSS files
    for link in soup.find_all("link", rel="stylesheet"):
        href = link.get("href")
        if href:
            css_file = href if not href.startswith("http") else None
            if css_file and os.path.exists(css_file):
                with open(css_file, "r", encoding="utf-8") as f:
                    css_text = f.read()
                for u in url_pattern.findall(css_text):
                    subfiles["image"].append(u)

    # Inline charts: best-effort discovery for non-iframe charts.
    # (Actual visibility is decided by Playwright screenshots.)
    try:
        subfiles["inline_chart_targets"] = extract_inline_chart_targets_from_html(data)
    except Exception:
        subfiles["inline_chart_targets"] = []

    # ----------------------
    # Deduplicate normal resource lists.
    # ----------------------
    for k in ("image", "video", "chart"):
        subfiles[k] = list(dict.fromkeys(subfiles[k]))

    return data, subfiles


def extract_inline_chart_targets_from_html(
    html_text: str,
    *,
    max_targets: int = 12,
) -> List[dict]:
    """
    Heuristic: identify likely inline charts:
    - <canvas> (Chart.js, etc.)
    - <svg> (D3/SVG charts; try to avoid tiny icons)
    - ECharts init containers (div + echarts.init)

    Returns a list of dicts:
    - kind: "canvas" | "svg" | "echarts"
    - key: identifier used as chart_ref in evaluation (e.g. "inline:canvas#myChart" or "inline:svg@2")
    - idx: document-order index for kind (0-based) OR omitted when selector-based
    - selector: optional CSS selector (preferred when id is available)
    - hint: short descriptor (id/class) for excerpt extraction
    """
    if not html_text:
        return []

    soup = BeautifulSoup(html_text, "html.parser")
    out: List[dict] = []

    def _append_target(kind: str, *, key: str, idx: int | None = None, selector: str | None = None, hint: str = ""):
        out.append({"kind": kind, "key": key, "idx": idx, "selector": selector, "hint": hint})

    # ---- canvas ----
    for idx, c in enumerate(soup.find_all("canvas")):
        cid = (c.get("id") or "").strip()
        cls = " ".join(c.get("class") or [])
        if cid:
            key = f"inline:canvas#{cid}"
            hint = f"canvas#{cid}"
            selector = f"#{cid}"
        else:
            key = f"inline:canvas@{idx}"
            hint = f"canvas@{idx}"
            selector = None
        if cls:
            hint = f"{hint}.{cls.replace(' ', '.')}"
        _append_target("canvas", key=key, idx=idx, selector=selector, hint=hint)
        if len(out) >= max_targets:
            break

    if len(out) >= max_targets:
        return out[:max_targets]

    # ---- svg ----
    svg_added = 0
    for idx, s in enumerate(soup.find_all("svg")):
        sid = (s.get("id") or "").strip()
        cls = " ".join(s.get("class") or [])

        # avoid tiny icons when explicit size is small
        w = str(s.get("width") or "").strip()
        h = str(s.get("height") or "").strip()
        try:
            wv = float(re.sub(r"[^0-9.]", "", w)) if w else None
            hv = float(re.sub(r"[^0-9.]", "", h)) if h else None
        except Exception:
            wv, hv = None, None
        if (wv is not None and wv < 120) or (hv is not None and hv < 90):
            continue

        if sid:
            key = f"inline:svg#{sid}"
            selector = f"#{sid}"
            hint = f"svg#{sid}"
        else:
            key = f"inline:svg@{idx}"
            selector = None
            hint = f"svg@{idx}"
        if cls:
            hint = f"{hint}.{cls.replace(' ', '.')}"
        _append_target("svg", key=key, idx=idx, selector=selector, hint=hint)
        svg_added += 1
        if len(out) >= max_targets:
            return out[:max_targets]
        if svg_added >= 6:
            break

    # ---- echarts containers ----
    # Prefer parsing obvious getElementById(...) patterns.
    ids = []
    try:
        echarts_id_re = re.compile(
            r"echarts\s*\.\s*init\s*\(\s*document\s*\.\s*getElementById\s*\(\s*['\"]([^'\"]+)['\"]\s*\)",
            flags=re.IGNORECASE | re.DOTALL,
        )
        for m in echarts_id_re.finditer(html_text):
            ids.append(m.group(1))
    except re.error:
        ids = []
    ids = list(dict.fromkeys([i for i in ids if i]))

    for j, cid in enumerate(ids[: max(0, max_targets - len(out))]):
        key = f"inline:echarts#{cid}"
        _append_target("echarts", key=key, selector=f"#{cid}", hint=f"echarts#{cid}")
        if len(out) >= max_targets:
            return out[:max_targets]

    # If echarts appears but ids aren't detectable, request a few runtime-discovered instances.
    if re.search(r"\becharts\b", html_text, flags=re.IGNORECASE):
        for idx in range(min(4, max_targets - len(out))):
            key = f"inline:echarts@{idx}"
            _append_target("echarts", key=key, idx=idx, selector=None, hint=f"echarts@{idx}")
            if len(out) >= max_targets:
                break

    return out



def download_media(url, save_dir=None, filename=None, timeout=10, debug=False):
    """
    Download the image or video pointed to by a URL.

    Returns:
        (media_bytes, save_path)
        media_bytes: bytes | None
        save_path: str | None
    """
    media_bytes = None
    save_path = None

    try:
        with requests.get(url, stream=True, timeout=timeout) as r:
            r.raise_for_status()

            # If no filename is provided, infer one from the URL.
            if filename is None:
                parsed = urlparse(url)
                base = os.path.basename(parsed.path)
                filename = base or "downloaded_file"

            # If there is no extension, try inferring one from Content-Type.
            if "." not in filename:
                ctype = r.headers.get("Content-Type", "")
                if "/" in ctype:
                    ext = ctype.split("/")[-1]
                    filename += "." + ext

            # Read the full response body (iter_content could be used for streaming).
            media_bytes = r.content

    except Exception as e:
        if debug:
            print(f"❌ download failed: {e}")
            traceback.print_exc()
        return None, None

    # If the caller does not want to save the file, return the bytes directly.
    if save_dir is None:
        return media_bytes, None

    # Save the file locally.
    try:
        os.makedirs(save_dir, exist_ok=True)
        save_path = os.path.join(save_dir, filename)
        with open(save_path, "wb") as f:
            f.write(media_bytes)
    except Exception as e:
        if debug:
            print(f"❌ save failed: {e}")
            traceback.print_exc()
        return media_bytes, None

    return media_bytes, save_path
