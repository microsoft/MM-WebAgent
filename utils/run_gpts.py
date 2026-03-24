import requests
import json
import base64
import mimetypes
import os
import random
import threading
from datetime import datetime, timezone
from io import BytesIO
from PIL import Image
import time
import traceback
from typing import Optional, Tuple, Dict, Any

_HTTP = requests.Session()
_HTTP.trust_env = False

openai_api_key = os.getenv("OPENAI_API_KEY", "none")
openai_image_api_key = os.getenv("OPENAI_IMAGE_API_KEY", openai_api_key)
openai_image_edit_api_key = os.getenv("OPENAI_IMAGE_EDIT_API_KEY", openai_image_api_key)
openai_video_api_key = os.getenv("OPENAI_VIDEO_API_KEY", openai_api_key)

_ERROR_LOG_LOCK = threading.Lock()


def _append_error_log(log_path: str | None, record: Dict[str, Any]) -> None:
    """
    Append a single JSON-line record to log_path.
    - Never raises.
    - Thread-safe to reduce line interleaving from concurrent tasks.
    """
    if not log_path:
        return
    try:
        os.makedirs(os.path.dirname(str(log_path)), exist_ok=True)
    except Exception:
        pass
    try:
        rec = dict(record or {})
        rec.setdefault("ts", datetime.now(timezone.utc).isoformat())
        line = json.dumps(rec, ensure_ascii=False)
        with _ERROR_LOG_LOCK:
            with open(str(log_path), "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        return

def _normalize_mime_type(mime_type: str | None, default: str = "image/png") -> str:
    """
    Normalize MIME type for data URL usage.
    - Drops parameters like "; charset=utf-8"
    - Strips whitespace
    - Lower-cases
    """
    if not mime_type:
        return default
    mt = str(mime_type).split(";", 1)[0].strip().lower()
    return mt or default


def _try_convert_raster_to_png_bytes(image_data: bytes) -> bytes | None:
    """
    Best-effort: convert arbitrary raster bytes (e.g. webp/avif) into PNG bytes via PIL.
    Returns PNG bytes on success; None on failure.
    """
    try:
        im = Image.open(BytesIO(image_data))
        im.load()
        # Avoid palette/LA/etc compatibility surprises
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA")
        buf = BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None


def _normalize_openai_base_url(base_url: str | None) -> str:
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        base = "https://api.openai.com/v1"
    if not base.endswith("/v1"):
        base = base + "/v1"
    return base


def _join_openai_url(base_url: str, path: str) -> str:
    base_url = _normalize_openai_base_url(base_url)
    if not path.startswith("/"):
        path = "/" + path
    return base_url + path


def _openai_headers(api_key: str, *, multipart: bool = False) -> Dict[str, str]:
    headers = {"Authorization": f"Bearer {api_key}"}
    if not multipart:
        headers["Content-Type"] = "application/json"
    return headers


def _resolve_token_field_name(model: Optional[str]) -> str:
    lower_model = str(model or "").lower()
    if lower_model.startswith("gpt-5") or lower_model.startswith("o1"):
        return "max_completion_tokens"
    return "max_tokens"


def _apply_token_limit(payload: Dict[str, Any], model: Optional[str], max_tokens: int) -> None:
    payload[_resolve_token_field_name(model)] = int(max_tokens)


def _infer_model_from_url(url: Optional[str], model: Optional[str]) -> Optional[str]:
    if model:
        return model
    normalized_url = str(url or "").lower()
    if normalized_url.endswith("/chat/completions"):
        return os.getenv("OPENAI_DEFAULT_CHAT_MODEL", os.getenv("OPENAI_MODEL_GPT52", "gpt-5.2"))
    if normalized_url.endswith("/images/generations"):
        return os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1")
    if normalized_url.endswith("/images/edits"):
        return os.getenv("OPENAI_IMAGE_EDIT_MODEL", os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1"))
    if normalized_url.endswith("/videos"):
        return os.getenv("OPENAI_VIDEO_MODEL", "sora-2")
    return model


def get_openai_request_config(model: str = "gpt-5.2") -> Dict[str, Any]:
    model = str(model or "").strip()
    if not model:
        return {"url": None, "headers": None, "model": None}

    api_base = os.getenv("OPENAI_BASE_URL", os.getenv("OPENAI_API_BASE_URL", "https://api.openai.com/v1"))
    video_base = os.getenv("OPENAI_VIDEO_BASE_URL", api_base)

    chat_models = {
        "4o": os.getenv("OPENAI_MODEL_GPT4O", "gpt-4o"),
        "gpt-4o": os.getenv("OPENAI_MODEL_GPT4O", "gpt-4o"),
        "4.1": os.getenv("OPENAI_MODEL_GPT41", "gpt-4.1"),
        "gpt-4.1": os.getenv("OPENAI_MODEL_GPT41", "gpt-4.1"),
        "5.1": os.getenv("OPENAI_MODEL_GPT51", "gpt-5.1"),
        "gpt-5.1": os.getenv("OPENAI_MODEL_GPT51", "gpt-5.1"),
        "5.2": os.getenv("OPENAI_MODEL_GPT52", "gpt-5.2"),
        "gpt-5.2": os.getenv("OPENAI_MODEL_GPT52", "gpt-5.2"),
    }
    if model in chat_models:
        resolved_model = chat_models[model]
        print(f"Using OpenAI chat model: model={resolved_model}")
        return {
            "url": _join_openai_url(api_base, "/chat/completions"),
            "headers": _openai_headers(openai_api_key),
            "model": resolved_model,
        }

    if model in {"gpt_image", "gpt_image2"}:
        resolved_model = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1")
        print(f"Using OpenAI image generation model: model={resolved_model}")
        return {
            "url": _join_openai_url(api_base, "/images/generations"),
            "headers": _openai_headers(openai_image_api_key),
            "model": resolved_model,
        }

    if model in {"gpt_image_edit", "gpt_image_edit2"}:
        resolved_model = os.getenv("OPENAI_IMAGE_EDIT_MODEL", os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1"))
        print(f"Using OpenAI image edit model: model={resolved_model}")
        return {
            "url": _join_openai_url(api_base, "/images/edits"),
            "headers": _openai_headers(openai_image_edit_api_key, multipart=True),
            "model": resolved_model,
        }

    if model in {"sora-2", "sora-2-pro"}:
        print(f"Using OpenAI video model: model={model}")
        return {
            "url": _join_openai_url(video_base, "/videos"),
            "headers": _openai_headers(openai_video_api_key, multipart=True),
            "model": model,
        }

    raise ValueError(
        f"Unsupported model alias: {model}. "
        "Supported aliases: 4o, gpt-4o, 4.1, gpt-4.1, 5.1, gpt-5.1, 5.2, gpt-5.2, "
        "gpt_image, gpt_image2, gpt_image_edit, gpt_image_edit2, sora-2, sora-2-pro."
    )


def get_openai_request_url(model: str = "gpt-5.2") -> Tuple[Optional[str], Optional[Dict[str, str]]]:
    cfg = get_openai_request_config(model)
    return cfg["url"], cfg["headers"]


def _is_gemini_request(url: Optional[str], headers: Optional[Dict[str, Any]]) -> bool:
    if not url:
        return False
    if "generativelanguage.googleapis.com" in str(url):
        return True
    if isinstance(headers, dict) and ("x-goog-api-key" in headers or "X-Goog-Api-Key" in headers):
        return True
    return False


def _parse_gemini_response(response_json: Dict[str, Any]) -> Tuple[Optional[str], bool]:
    """
    Extract the generated text from Gemini generateContent response.
    Returns (text, success).
    """
    try:
        candidates = response_json.get("candidates") or []
        if not candidates:
            return None, False
        content = candidates[0].get("content") or {}
        parts = content.get("parts") or []
        text_chunks = []
        for p in parts:
            t = p.get("text")
            if t:
                text_chunks.append(t)
        text = "".join(text_chunks).strip()
        return (text if text else None), bool(text)
    except Exception:
        return None, False


def request_gemini_t2t(
    user_prompt: str,
    system_prompt: str,
    url: str,
    api_key: dict,
    debug: bool = False,
    timeout: int = 120,
):
    """
    Gemini generateContent wrapper. Uses systemInstruction when provided.
    """
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": user_prompt}],
            }
        ],
    }

    if system_prompt:
        payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}

    try:
        response = _HTTP.post(
            url,
            headers=api_key,
            json=payload,
            timeout=timeout,
        )
        if debug and response is not None and response.status_code != 200:
            try:
                print(f"[HTTP] status={response.status_code}")
                print(f"[HTTP] body={response.text[:2000]}")
            except Exception:
                pass
        response.raise_for_status()
        text, success = _parse_gemini_response(response.json())
    except Exception as e:
        if debug:
            print(f"⚠️ Unexpected error: {e}")
            try:
                resp = getattr(e, "response", None)
                if resp is not None:
                    print(f"[HTTPError] status={getattr(resp, 'status_code', None)}")
                    print(f"[HTTPError] body={getattr(resp, 'text', '')[:2000]}")
            except Exception:
                pass
            traceback.print_exc()
        return None, False

    if debug:
        print("Final response:", text)
    return text, success



def generate_video(prompt, save_path="output.mp4", size="1792x1024", seconds="4", debug=False):
    seconds = str(seconds)
    os.makedirs(os.path.dirname(os.path.abspath(save_path)), exist_ok=True)
    assert size in ['720x1280', '1280x720', '1024x1792', '1792x1024'], "Size must be one of '720x1280', '1280x720', '1024x1792', or '1792x1024'"
    assert seconds in ['4', '8', '12'], "Seconds must be one of '4', '8', or '12'"

    request_cfg = get_openai_request_config(os.getenv("OPENAI_VIDEO_MODEL", "sora-2"))
    url = request_cfg["url"]
    headers = request_cfg["headers"]
    model = request_cfg["model"]

    if not url:
        raise ValueError("OpenAI video endpoint is not configured.")
    if not headers or "Authorization" not in headers:
        raise ValueError("OPENAI_VIDEO_API_KEY / OPENAI_API_KEY environment variable not set.")

    success = False
    try:
        multipart_fields = {
            "prompt": (None, prompt),
            "seconds": (None, seconds),
            "size": (None, size),
            "model": (None, model),
        }
        job_response = _HTTP.post(url, headers=headers, files=multipart_fields)
        if not job_response.ok and debug:
            print("❌ Video generation failed.")
            print(json.dumps(job_response.json(), sort_keys=True, indent=4, separators=(',', ': ')))
        else:
            if debug:
                print(json.dumps(job_response.json(), sort_keys=True, indent=4, separators=(',', ': ')))
            job_response = job_response.json()
            job_id = job_response.get("id")
            status = job_response.get("status")
            status_url = f"{url}/{job_id}"

            if debug:
                print(f"⏳ Polling job status for ID: {job_id}")
            while status not in ["completed", "failed"]:
                time.sleep(5)
                job_response = _HTTP.get(status_url, headers=headers).json()
                status = job_response.get("status")
                if debug:
                    print(f"Status: {status}")

            if status == "completed":
                if debug:
                    print(job_response)
                generations = job_response.get("id", "")
                if generations:
                    print(f"✅ Video generation succeeded.")

                    generation_id = generations
                    video_url = f"{url}/{generation_id}/content"
                    
                    video_response = _HTTP.get(video_url, headers=headers)
                    if video_response.ok:
                        with open(save_path, "wb") as file:
                            file.write(video_response.content)
                        print(f'Generated video saved as "{save_path}"')
                        success = True
                else:
                    print("⚠️ Status is succeeded, but no generations were returned.")
            elif status == "failed":
                print("❌ Video generation failed.")
                if debug:
                    print(json.dumps(job_response, sort_keys=True, indent=4, separators=(',', ': ')))
    except Exception as e:
        print(f"⚠️ Exception during video generation: {e}")
        save_path, success = "", False

    return save_path, success


def generate_video_until_success(prompt, save_path="output.mp4", size="1792x1024", seconds="4",
                          debug=False, max_retries=10, sleep_time=30):
    for attempt in range(max_retries):
        data, success = generate_video(prompt, save_path=save_path, size=size, seconds=seconds, debug=debug)
        if success:
            return data, success
        else:
            time.sleep(sleep_time)
            print(f"Attempt {attempt + 1} failed. Retrying after {sleep_time}s ...")
    return None, False

def parse_response(response, debug=False):
    success = (response.status_code == 200)
    if success:
        data = response.json()
        data = data["choices"][0]["message"]["content"]
    else:
        data = f"Error {response.status_code}: {response.text}"

    if debug:
        print(data)

    return data, success



def load_image(path_url_or_pil, gemini_format: bool = False):
    image_data = None
    mime_type = "image/png"

    if isinstance(path_url_or_pil, Image.Image):
        buf = BytesIO()
        path_url_or_pil.save(buf, format="PNG")
        image_data = buf.getvalue()

    elif isinstance(path_url_or_pil, BytesIO):
        image_data = path_url_or_pil.getvalue()

    elif isinstance(path_url_or_pil, bytes):
        image_data = path_url_or_pil

    elif isinstance(path_url_or_pil, str):
        if os.path.exists(path_url_or_pil):
            with open(path_url_or_pil, "rb") as f:
                image_data = f.read()
            mime_type, _ = mimetypes.guess_type(path_url_or_pil)
            mime_type = _normalize_mime_type(mime_type, default="image/png")
        elif path_url_or_pil.startswith("http"):
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/115.0.0.0 Safari/537.36"
            }
            resp = _HTTP.get(path_url_or_pil, headers=headers)
            if resp.status_code != 200:
                raise ValueError(f"Failed to download image: {resp.status_code}")
            image_data = resp.content
            mime_type = _normalize_mime_type(resp.headers.get("Content-Type", None), default="image/png")
            if not mime_type.startswith("image/"):
                raise ValueError(f"URL did not return an image (Content-Type={resp.headers.get('Content-Type', None)})")
        else:
            raise ValueError("Unsupported string format: must be file path or URL")

    else:
        raise TypeError("Unsupported input type for load_image")

    # The OpenAI vision endpoint is picky about some formats (notably webp/avif).
    # Convert to PNG to reduce "Invalid image data" 400s.
    if mime_type in {"image/webp", "image/avif"}:
        converted = _try_convert_raster_to_png_bytes(image_data)
        if converted is not None:
            image_data = converted
            mime_type = "image/png"

    img_b64 = base64.b64encode(image_data).decode()
    if gemini_format:
        gemini_data ={
            "inlineData": {
                "mimeType": mime_type,
                "data": img_b64
            }
        }
        return gemini_data
    return f"data:{mime_type};base64,{img_b64}"


def request_chatgpt_i2t_until_success(path_url_or_pil, user_prompt, system_prompt, url, api_key,
                          max_tokens=15536, debug=False, timeout=120, max_retries=100, sleep_time=30, reasoning_effort=None, log_path=None, model=None):
    last_err = None
    for attempt in range(max_retries):
        data, success = request_chatgpt_i2t(
            path_url_or_pil,
            user_prompt,
            system_prompt,
            url,
            api_key,
            max_tokens,
            debug,
            timeout,
            reasoning_effort,
            log_path=log_path,
            model=model,
        )
        if success:
            return data, success
        else:
            last_err = data
            time.sleep(sleep_time)
            print(f"Attempt {attempt + 1} failed. Retrying after {sleep_time}s ...")
    exhausted = {"error_type": "max_retries", "attempts": int(max_retries), "last_error": last_err}
    _append_error_log(log_path, {"fn": "request_chatgpt_i2t_until_success", "event": "exhausted", "error": exhausted})
    return exhausted, False


def request_gemini_i2t(paths_or_urls, user_prompt, system_prompt, url, api_key,
                        max_tokens=15536, debug=False, timeout=120, reasoning_effort=None, log_path=None):
    """
    Send text + one or more images to a ChatGPT-like vision model API.
    
    Args:
        paths_or_urls: str | list[str] | list[PIL.Image.Image]
            Single path/url/PIL image or a list of them.
        user_prompt: str
            Text prompt for the user message.
        system_prompt: str
            System instruction for model behavior.
        url: str
            API endpoint.
        api_key: dict
            Request headers (e.g., {"Authorization": "Bearer <key>"}).
        max_tokens: int
            Maximum number of output tokens.
        debug: bool
            Print debug logs.
        timeout: int
            Timeout in seconds.
    """
    # ensure list
    if not isinstance(paths_or_urls, (list, tuple)):
        paths_or_urls = [paths_or_urls]

    # encode each image
    image_contents = [load_image(p, gemini_format=True) for p in paths_or_urls]

    # construct message
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": user_prompt}] + image_contents,
            }
        ],
    }

    if system_prompt:
        payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}

    try:
        response = _HTTP.post(
            url,
            headers=api_key,
            json=payload,
            timeout=timeout
        )
        if response is not None and response.status_code != 200:
            err = {
                "error_type": "http",
                "status_code": int(getattr(response, "status_code", 0) or 0),
                "retry_after": response.headers.get("Retry-After") or response.headers.get("retry-after"),
                "body": (getattr(response, "text", "") or "")[:2000],
            }
            _append_error_log(log_path, {"fn": "request_chatgpt_i2t", "url": url, "error": err})
            if debug:
                try:
                    print(f"[HTTP] status={response.status_code}")
                    print(f"[HTTP] body={response.text[:2000]}")
                except Exception:
                    pass
        response.raise_for_status()
        data, success = _parse_gemini_response(response.json())
        if not success:
            _append_error_log(log_path, {"fn": "request_chatgpt_i2t", "url": url, "error": {"error_type": "parse_response_failed"}})
        return data, success
    except requests.exceptions.Timeout as e:
        err = {"error_type": "timeout", "exception": type(e).__name__, "message": str(e)[:2000]}
        _append_error_log(log_path, {"fn": "request_chatgpt_i2t", "url": url, "error": err})
        if debug:
            print("⚠️ Request timed out")
        return err, False
    except requests.exceptions.HTTPError as e:
        resp = getattr(e, "response", None)
        err = {
            "error_type": "http",
            "status_code": getattr(resp, "status_code", None),
            "retry_after": (getattr(resp, "headers", {}) or {}).get("Retry-After") if resp is not None else None,
            "body": (getattr(resp, "text", "") or "")[:2000] if resp is not None else "",
        }
        _append_error_log(log_path, {"fn": "request_chatgpt_i2t", "url": url, "error": err})
        if debug:
            print(f"⚠️ Unexpected error: {e}")
            try:
                if resp is not None:
                    print(f"[HTTPError] status={getattr(resp, 'status_code', None)}")
                    print(f"[HTTPError] body={getattr(resp, 'text', '')[:2000]}")
            except Exception:
                pass
            traceback.print_exc()
        return err, False
    except requests.exceptions.RequestException as e:
        err = {"error_type": "request_exception", "exception": type(e).__name__, "message": str(e)[:2000]}
        _append_error_log(log_path, {"fn": "request_chatgpt_i2t", "url": url, "error": err})
        if debug:
            print(f"⚠️ Unexpected error: {e}")
            traceback.print_exc()
        return err, False
    except Exception as e:
        err = {"error_type": "exception", "exception": type(e).__name__, "message": str(e)[:2000]}
        _append_error_log(log_path, {"fn": "request_chatgpt_i2t", "url": url, "error": err})
        if debug:
            print(f"⚠️ Unexpected error: {e}")
            traceback.print_exc()
        return err, False
    if debug:
        print("Final response:", data)

    return data, success

def request_chatgpt_i2t(paths_or_urls, user_prompt, system_prompt, url, api_key,
                        max_tokens=15536, debug=False, timeout=120, reasoning_effort=None, log_path=None, model=None):
    """
    Send text + one or more images to a ChatGPT-like vision model API.
    
    Args:
        paths_or_urls: str | list[str] | list[PIL.Image.Image]
            Single path/url/PIL image or a list of them.
        user_prompt: str
            Text prompt for the user message.
        system_prompt: str
            System instruction for model behavior.
        url: str
            API endpoint.
        api_key: dict
            Request headers (e.g., {"Authorization": "Bearer <key>"}).
        max_tokens: int
            Maximum number of output tokens.
        debug: bool
            Print debug logs.
        timeout: int
            Timeout in seconds.
    """
    if _is_gemini_request(url, api_key):
        print("Detected Gemini request")
        return request_gemini_i2t(
            paths_or_urls,
            user_prompt=user_prompt,
            system_prompt=system_prompt,
            url=url,
            api_key=api_key,
            debug=debug,
            timeout=timeout,
            log_path=log_path
        )

    model = _infer_model_from_url(url, model)

    # ensure list
    if not isinstance(paths_or_urls, (list, tuple)):
        paths_or_urls = [paths_or_urls]

    # encode each image
    image_contents = []
    for p in paths_or_urls:
        img_encoded_str = load_image(p)
        image_contents.append({"type": "image_url", "image_url": {"url": img_encoded_str}})

    # construct message
    message = [
        {"role": "system", "content": [{"type": "text", "text": system_prompt}]},
        {
            "role": "user",
            "content": [{"type": "text", "text": user_prompt}] + image_contents
        }
    ]

    payload = {"model": model, "messages": message}
    _apply_token_limit(payload, model, max_tokens)

    if reasoning_effort is not None:
        assert reasoning_effort in ["low", "medium", "high"], "Invalid reasoning_effort"
        payload["reasoning_effort"] = reasoning_effort

    try:
        response = _HTTP.post(
            url,
            headers=api_key,
            json=payload,
            timeout=timeout
        )
        if response is not None and response.status_code != 200:
            err = {
                "error_type": "http",
                "status_code": int(getattr(response, "status_code", 0) or 0),
                "retry_after": response.headers.get("Retry-After") or response.headers.get("retry-after"),
                "body": (getattr(response, "text", "") or "")[:2000],
            }
            _append_error_log(log_path, {"fn": "request_chatgpt_i2t", "url": url, "error": err})
            if debug:
                try:
                    print(f"[HTTP] status={response.status_code}")
                    print(f"[HTTP] body={response.text[:2000]}")
                except Exception:
                    pass
        response.raise_for_status()
        data, success = parse_response(response, debug)
        if not success:
            _append_error_log(log_path, {"fn": "request_chatgpt_i2t", "url": url, "error": {"error_type": "parse_response_failed"}})
        return data, success
    except requests.exceptions.Timeout as e:
        err = {"error_type": "timeout", "exception": type(e).__name__, "message": str(e)[:2000]}
        _append_error_log(log_path, {"fn": "request_chatgpt_i2t", "url": url, "error": err})
        if debug:
            print("⚠️ Request timed out")
        return err, False
    except requests.exceptions.HTTPError as e:
        resp = getattr(e, "response", None)
        err = {
            "error_type": "http",
            "status_code": getattr(resp, "status_code", None),
            "retry_after": (getattr(resp, "headers", {}) or {}).get("Retry-After") if resp is not None else None,
            "body": (getattr(resp, "text", "") or "")[:2000] if resp is not None else "",
        }
        _append_error_log(log_path, {"fn": "request_chatgpt_i2t", "url": url, "error": err})
        if debug:
            print(f"⚠️ Unexpected error: {e}")
            try:
                if resp is not None:
                    print(f"[HTTPError] status={getattr(resp, 'status_code', None)}")
                    print(f"[HTTPError] body={getattr(resp, 'text', '')[:2000]}")
            except Exception:
                pass
            traceback.print_exc()
        return err, False
    except requests.exceptions.RequestException as e:
        err = {"error_type": "request_exception", "exception": type(e).__name__, "message": str(e)[:2000]}
        _append_error_log(log_path, {"fn": "request_chatgpt_i2t", "url": url, "error": err})
        if debug:
            print(f"⚠️ Unexpected error: {e}")
            traceback.print_exc()
        return err, False
    except Exception as e:
        err = {"error_type": "exception", "exception": type(e).__name__, "message": str(e)[:2000]}
        _append_error_log(log_path, {"fn": "request_chatgpt_i2t", "url": url, "error": err})
        if debug:
            print(f"⚠️ Unexpected error: {e}")
            traceback.print_exc()
        return err, False
    if debug:
        print("Final response:", data)

    return data, success


def request_chatgpt_t2t(user_prompt, system_prompt, url, api_key,
                          max_tokens=15536, debug=False, timeout=120,  reasoning_effort=None, log_path=None, model=None):
    if _is_gemini_request(url, api_key):
        return request_gemini_t2t(
            user_prompt=user_prompt,
            system_prompt=system_prompt,
            url=url,
            api_key=api_key,
            debug=debug,
            timeout=timeout,
        )

    model = _infer_model_from_url(url, model)

    message = [
        {"role": "system", "content": [{"type": "text", "text": system_prompt}]},
        {"role": "user", "content": [{"type": "text", "text": user_prompt}]},
    ]
    payload = {"model": model, "messages": message}
    _apply_token_limit(payload, model, max_tokens)

    if reasoning_effort is not None:
        assert reasoning_effort in ["low", "medium", "high"], "Invalid reasoning_effort"
        payload["reasoning_effort"] = reasoning_effort


    try:
        response = _HTTP.post(url, headers=api_key, json=payload, timeout=timeout)
        if response is not None and response.status_code != 200:
            err = {
                "error_type": "http",
                "status_code": int(getattr(response, "status_code", 0) or 0),
                "retry_after": response.headers.get("Retry-After") or response.headers.get("retry-after"),
                "body": (getattr(response, "text", "") or "")[:2000],
            }
            _append_error_log(log_path, {"fn": "request_chatgpt_t2t", "url": url, "error": err})
            if debug:
                try:
                    print(f"[HTTP] status={response.status_code}")
                    print(f"[HTTP] body={response.text[:2000]}")
                except Exception:
                    pass
        response.raise_for_status()
        data, success = parse_response(response, debug)
        if not success:
            _append_error_log(log_path, {"fn": "request_chatgpt_t2t", "url": url, "error": {"error_type": "parse_response_failed"}})
        return data, success
    except requests.exceptions.Timeout as e:
        err = {"error_type": "timeout", "exception": type(e).__name__, "message": str(e)[:2000]}
        _append_error_log(log_path, {"fn": "request_chatgpt_t2t", "url": url, "error": err})
        if debug:
            print("⚠️ Request timed out")
        return err, False
    except requests.exceptions.HTTPError as e:
        resp = getattr(e, "response", None)
        err = {
            "error_type": "http",
            "status_code": getattr(resp, "status_code", None),
            "retry_after": (getattr(resp, "headers", {}) or {}).get("Retry-After") if resp is not None else None,
            "body": (getattr(resp, "text", "") or "")[:2000] if resp is not None else "",
        }
        _append_error_log(log_path, {"fn": "request_chatgpt_t2t", "url": url, "error": err})
        if debug:
            print(f"⚠️ Unexpected error: {e}")
            try:
                if resp is not None:
                    print(f"[HTTPError] status={getattr(resp, 'status_code', None)}")
                    print(f"[HTTPError] body={getattr(resp, 'text', '')[:2000]}")
            except Exception:
                pass
            traceback.print_exc()
        return err, False
    except requests.exceptions.RequestException as e:
        err = {"error_type": "request_exception", "exception": type(e).__name__, "message": str(e)[:2000]}
        _append_error_log(log_path, {"fn": "request_chatgpt_t2t", "url": url, "error": err})
        if debug:
            print(f"⚠️ Unexpected error: {e}")
            traceback.print_exc()
        return err, False
    except Exception as e:
        err = {"error_type": "exception", "exception": type(e).__name__, "message": str(e)[:2000]}
        _append_error_log(log_path, {"fn": "request_chatgpt_t2t", "url": url, "error": err})
        if debug:
            print(f"⚠️ Unexpected error: {e}")
            traceback.print_exc()
        return err, False

def request_chatgpt_t2t_until_success(user_prompt, system_prompt, url, api_key,
                          max_tokens=15536, debug=False, timeout=120, max_retries=100, sleep_time=30,  reasoning_effort=None, log_path=None, model=None):
    last_err = None
    for attempt in range(max_retries):
        data, success = request_chatgpt_t2t(user_prompt, system_prompt, url, api_key,
                                           max_tokens, debug, timeout, reasoning_effort=reasoning_effort, log_path=log_path, model=model)
        if success:
            return data, success
        else:
            last_err = data
            time.sleep(sleep_time)
            print(f"Attempt {attempt + 1} failed. Retrying after {sleep_time}s ...")
    exhausted = {"error_type": "max_retries", "attempts": int(max_retries), "last_error": last_err}
    _append_error_log(log_path, {"fn": "request_chatgpt_t2t_until_success", "event": "exhausted", "error": exhausted})
    return exhausted, False

def request_chatgpt_t2i(user_prompt,
                        system_prompt,
                        url,
                        api_key,
                        model=None,
                        size="1024x1024",
                        n=1,
                        debug=False,
                        timeout=120,
                        save_path=None):
    """
    Send a text prompt to the OpenAI image generation API (text-to-image).
    
    Args:
        user_prompt: str
            The text prompt describing the image.
        system_prompt: str
            Optional system-level instruction (you can prepend it to user_prompt).
        url: str
            API endpoint (e.g., OpenAI /images/generations URL).
        api_key: str
            Your API key string.
        size: str
            Image size (e.g., "1024x1024", "640x640").
        n: int
            Number of images to generate.
        debug: bool
            Print debug logs.
        timeout: int
            Timeout in seconds.
        save_path: str or Path
            Directory path to save generated images (optional).
            
    Returns:
        List of image file paths or base64 strings.
    """
    model = _infer_model_from_url(url, model)
    prompt = f"{system_prompt.strip()}\n{user_prompt.strip()}" if system_prompt else user_prompt
    
    payload = {
        "model": model,
        "prompt": prompt,
        "size": size,
        "n": n,
        "response_format": "b64_json",
    }
    
    try:
        response = _HTTP.post(url, headers=api_key, json=payload, timeout=timeout)
        response.raise_for_status()
        if debug:
            print(json.dumps(response.json(), sort_keys=True, indent=4, separators=(',', ': ')))
    except requests.exceptions.Timeout:
        if debug:
            print("⚠️ Request timed out")
        return None, False
    except requests.exceptions.RequestException as e:
        if debug:
            print(f"⚠️ Request failed: {e}")
        return None, False
    
    data = response.json()
    if debug:
        print("Response JSON:", data)
    
    results = []
    for i, item in enumerate(data.get("data", [])):
        if "b64_json" in item:
            img_bytes = base64.b64decode(item["b64_json"])
            if save_path:
                os.makedirs(os.path.dirname(save_path), exist_ok=True)
                with open(save_path, "wb") as f:
                    f.write(img_bytes)
                results.append(str(save_path))
            else:
                results.append(item["b64_json"])
        elif "url" in item:
            results.append(item["url"])
    
    return results, True


def request_chatgpt_t2i_until_success(user_prompt, system_prompt, url, api_key, size="1024x1024",
                          debug=False, timeout=120, save_path=None, max_retries=100, sleep_time=30, model=None):
    for attempt in range(max_retries):
        data, success = request_chatgpt_t2i( user_prompt, system_prompt, url, api_key, model, size,
                                           debug=debug, timeout=timeout, save_path=save_path)
        if success:
            return data, success
        else:
            time.sleep(sleep_time)
            print(f"Attempt {attempt + 1} failed. Retrying after {sleep_time}s ...")
    return None, False

def request_chatgpt_i2i(img_path,
                        user_prompt,
                        system_prompt,
                        url,
                        api_key,
                        model=None,
                        mask_path=None,
                        debug=False,
                        timeout=120,
                        save_path=None,
                        log_path=None):
    model = _infer_model_from_url(url, model)
    prompt = f"{system_prompt.strip()}\n{user_prompt.strip()}" if system_prompt else user_prompt

    # This request must use multipart/form-data; requests will handle the boundary automatically.
    data = {"model": model, "prompt": prompt, "response_format": "b64_json"}

    def _print_http_error(resp: requests.Response):
        try:
            print(f"[HTTP] status={resp.status_code}")
            ra = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
            if ra:
                print(f"[HTTP] retry-after={ra}")
            print(f"[HTTP] body={resp.text[:2000]}")
        except Exception:
            pass

    try:
        # Use `with` to close file handles promptly and avoid "Too many open files" after repeated retries.
        with open(img_path, "rb") as f_img:
            files = {
                "image": (os.path.basename(img_path), f_img, "image/png")
            }
            if mask_path:
                with open(mask_path, "rb") as f_mask:
                    files["mask"] = (os.path.basename(mask_path), f_mask, "image/png")
                    response = _HTTP.post(url, headers=api_key, files=files, data=data, timeout=timeout)
            else:
                response = _HTTP.post(url, headers=api_key, files=files, data=data, timeout=timeout)

        if debug and response is not None and response.status_code != 200:
            _print_http_error(response)

        response.raise_for_status()
    except requests.exceptions.HTTPError as e:
        # Let the caller's until_success wrapper decide how to back off / retry.
        # We still return the status code so the caller can distinguish unrecoverable
        # cases such as persistent HTTP 500 errors.
        if debug:
            traceback.print_exc()
            try:
                resp = getattr(e, "response", None)
                if resp is not None:
                    _print_http_error(resp)
            except Exception:
                pass
        try:
            resp = getattr(e, "response", None)
            code = getattr(resp, "status_code", None)
            body = getattr(resp, "text", "")
            ra = None
            try:
                if resp is not None:
                    ra = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
                    if ra is not None:
                        # Retry-After can be int seconds (common) or HTTP-date; we handle seconds only.
                        ra = float(str(ra).strip())
                        if ra < 0:
                            ra = None
            except Exception:
                ra = None
            err = {
                "error_type": "http",
                "status_code": code,
                "retry_after": ra,
                "body": body[:2000],
            }
            _append_error_log(log_path, {"fn": "request_chatgpt_i2i", "url": url, "error": err})
            return err, False
        except Exception:
            err = {"error_type": "http_error", "exception": type(e).__name__, "message": str(e)[:2000]}
            _append_error_log(log_path, {"fn": "request_chatgpt_i2i", "url": url, "error": err})
            return err, False
    except requests.exceptions.Timeout as e:
        err = {"error_type": "timeout", "exception": type(e).__name__, "message": str(e)[:2000]}
        _append_error_log(log_path, {"fn": "request_chatgpt_i2i", "url": url, "error": err})
        if debug:
            traceback.print_exc()
        return err, False
    except requests.exceptions.RequestException as e:
        err = {"error_type": "request_exception", "exception": type(e).__name__, "message": str(e)[:2000]}
        _append_error_log(log_path, {"fn": "request_chatgpt_i2i", "url": url, "error": err})
        if debug:
            traceback.print_exc()
        return err, False
    except Exception as e:
        err = {"error_type": "exception", "exception": type(e).__name__, "message": str(e)[:2000]}
        _append_error_log(log_path, {"fn": "request_chatgpt_i2i", "url": url, "error": err})
        if debug:
            traceback.print_exc()
        return err, False

    data = response.json()
    results = []
    for i, item in enumerate(data.get("data", [])):
        if "b64_json" in item:
            img_bytes = base64.b64decode(item["b64_json"])
            if save_path:
                os.makedirs(os.path.dirname(save_path), exist_ok=True)
                with open(save_path, "wb") as f:
                    f.write(img_bytes)
                results.append(str(save_path))
            else:
                results.append(item["b64_json"])
        elif "url" in item:
            results.append(item["url"])
    print("Generated images:", save_path)
    return results, True

def request_chatgpt_i2i_until_success(img_path, user_prompt, system_prompt, url, api_key, mask_path=None,
                          debug=False, timeout=120, save_path=None, max_retries=100, sleep_time=30, log_path=None, model=None):
    last_err = None
    for attempt in range(max_retries):
        data, success = request_chatgpt_i2i( img_path, user_prompt, system_prompt, url, api_key, model, mask_path,
                                           debug=debug, timeout=timeout, save_path=save_path, log_path=log_path)
        if success:
            return data, success
        else:
            last_err = data
            # HTTP 500 is often a persistent server-side error; avoid wasting time on pointless retries.
            if (
                isinstance(data, dict)
                and data.get("error_type") == "http"
                and int(data.get("status_code") or 0) == 500
            ) or (
                isinstance(data, str) and ("HTTPError 500:" in data or data.strip().startswith("HTTPError 500"))
            ):
                print("Attempt 1 failed with HTTP 500. Skipping further retries for this image edit.")
                _append_error_log(log_path, {"fn": "request_chatgpt_i2i_until_success", "event": "skip_http_500", "error": data})
                return data, False
            # HTTP 429 is common: use exponential backoff with jitter to reduce retry storms.
            wait = min(180, sleep_time * (2 ** min(attempt, 6)))
            # Respect server-side Retry-After first when present to avoid retrying too early.
            if (
                isinstance(data, dict)
                and data.get("error_type") == "http"
                and int(data.get("status_code") or 0) == 429
                and data.get("retry_after") is not None
            ):
                try:
                    wait = max(wait, float(data["retry_after"]))
                except Exception:
                    pass
            wait = wait + random.uniform(0, 0.25 * wait)
            time.sleep(wait)
            print(f"Attempt {attempt + 1} failed. Retrying after {int(wait)}s ...")
    exhausted = {"error_type": "max_retries", "attempts": int(max_retries), "last_error": last_err}
    _append_error_log(log_path, {"fn": "request_chatgpt_i2i_until_success", "event": "exhausted", "error": exhausted})
    return exhausted, False
