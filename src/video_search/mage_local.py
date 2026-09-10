from __future__ import annotations

import base64
import importlib.util
import io
import json
import platform
import shutil
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Protocol


DEFAULT_MODEL = "microsoft/Mage-VL"


class MageEngine(Protocol):
    model_id: str

    def generate(self, frames: list[bytes], prompt: str, max_tokens: int) -> str: ...


def environment_report(cache_root: Path | None = None) -> dict[str, object]:
    """Report local inference readiness without importing ML libraries or downloading files."""
    cache_root = cache_root or Path.home() / ".cache" / "huggingface"
    model_directory = cache_root / "hub" / "models--microsoft--Mage-VL"
    alternate_model_directory = cache_root / "models--microsoft--Mage-VL"
    dependencies = {
        name: importlib.util.find_spec(name) is not None
        for name in (
            "torch",
            "torchvision",
            "transformers",
            "accelerate",
            "safetensors",
            "PIL",
            "cv2",
        )
    }
    free_disk_gb = round(shutil.disk_usage(cache_root.parent).free / 1024**3, 1)
    return {
        "model": DEFAULT_MODEL,
        "machine": platform.machine(),
        "free_disk_gb": free_disk_gb,
        "dependencies": dependencies,
        "dependencies_ready": all(dependencies.values()),
        "model_cached": model_directory.exists() or alternate_model_directory.exists(),
        "cache_root": str(cache_root),
        "downloads_performed": False,
    }


def _data_url_bytes(url: str) -> bytes:
    prefix, separator, payload = url.partition(",")
    if not separator or ";base64" not in prefix:
        raise ValueError("image_url must be a base64 data URL")
    return base64.b64decode(payload, validate=True)


def _request_inputs(payload: dict[str, object]) -> tuple[list[bytes], str, int]:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        raise ValueError("messages must be a list")

    content: object = None
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            break
    if not isinstance(content, list):
        raise ValueError("the last user message must contain a content list")

    frames: list[bytes] = []
    prompt_parts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and isinstance(part.get("text"), str):
            prompt_parts.append(part["text"])
        if part.get("type") == "image_url":
            image_url = part.get("image_url")
            if isinstance(image_url, dict) and isinstance(image_url.get("url"), str):
                frames.append(_data_url_bytes(image_url["url"]))

    if not frames:
        raise ValueError("at least one image frame is required")
    prompt = "\n".join(prompt_parts).strip()
    if not prompt:
        raise ValueError("a text prompt is required")
    max_tokens = payload.get("max_tokens", 1_200)
    if not isinstance(max_tokens, int) or max_tokens < 1:
        raise ValueError("max_tokens must be a positive integer")
    return frames, prompt, max_tokens


def create_local_mage_server(
    address: tuple[str, int], engine: MageEngine
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, payload: dict[str, object]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self._send(200, {"status": "ready", "model": engine.model_id})
                return
            self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path.rstrip("/") not in {
                "/v1/chat/completions",
                "/chat/completions",
            }:
                self._send(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("request body must be a JSON object")
                frames, prompt, max_tokens = _request_inputs(payload)
                answer = engine.generate(frames, prompt, max_tokens)
                self._send(
                    200,
                    {
                        "id": f"chatcmpl-{uuid.uuid4().hex}",
                        "object": "chat.completion",
                        "model": engine.model_id,
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": answer},
                                "finish_reason": "stop",
                            }
                        ],
                    },
                )
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                self._send(400, {"error": str(error), "type": type(error).__name__})
            except Exception as error:  # pragma: no cover - depends on model runtime
                self._send(500, {"error": str(error), "type": type(error).__name__})

        def log_message(self, format: str, *args: object) -> None:
            return

    return ThreadingHTTPServer(address, Handler)


class TransformersMageEngine:
    """Experimental Apple-Silicon runner for the official Mage-VL checkpoint."""

    def __init__(self, model_id: str = DEFAULT_MODEL, device: str = "mps") -> None:
        try:
            import torch
            from PIL import Image
            from transformers import AutoModelForCausalLM, AutoProcessor
        except ImportError as error:
            raise RuntimeError(
                "Mage local dependencies are missing; install the mage-local extra first"
            ) from error

        if device == "mps" and not torch.backends.mps.is_available():
            raise RuntimeError("PyTorch MPS is not available on this machine")

        self.model_id = model_id
        self.device = device
        self._torch = torch
        self._image_class = Image
        self._lock = threading.Lock()
        self.processor = AutoProcessor.from_pretrained(
            model_id,
            trust_remote_code=True,
        )
        self.model = AutoModelForCausalLM.from_pretrained(
            model_id,
            trust_remote_code=True,
            torch_dtype=torch.float16,
            low_cpu_mem_usage=True,
            attn_implementation="sdpa",
        ).eval()
        self.model.to(device)

    def generate(self, frames: list[bytes], prompt: str, max_tokens: int) -> str:
        images = [
            self._image_class.open(io.BytesIO(frame)).convert("RGB") for frame in frames
        ]
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "video"},
                    {"type": "text", "text": prompt},
                ],
            }
        ]
        rendered_prompt = self.processor.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
        )
        inputs = self.processor(
            text=[rendered_prompt],
            videos=[images],
            return_tensors="pt",
            padding=True,
        )
        inputs = {
            key: (
                value.to(device=self.device, dtype=self.model.dtype)
                if key == "pixel_values"
                else value.to(self.device)
            )
            for key, value in inputs.items()
        }
        with self._lock, self._torch.inference_mode():
            generated = self.model.generate(
                **inputs,
                max_new_tokens=max_tokens,
                do_sample=False,
            )
        prompt_length = inputs["input_ids"].shape[1]
        return self.processor.batch_decode(
            generated[:, prompt_length:],
            skip_special_tokens=True,
        )[0]


def serve_local_mage(
    *,
    model_id: str = DEFAULT_MODEL,
    host: str = "127.0.0.1",
    port: int = 30_000,
    device: str = "mps",
) -> None:
    engine = TransformersMageEngine(model_id=model_id, device=device)
    server = create_local_mage_server((host, port), engine)
    try:
        server.serve_forever()
    finally:
        server.server_close()
