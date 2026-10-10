import logging
import os
import uuid
import threading

import httpx
from flask import Flask, request, jsonify
from pydantic import BaseModel, field_validator, ValidationError

from google import genai
from google.genai.errors import ClientError, ServerError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("api-wrapper")

DEMO_MODE = os.getenv("DEMO_MODE", "false").lower() == "true"

# Created lazily, on first real use inside a request — NOT at import time.
# Passenger forks worker processes from a single preloaded copy of this
# module ("smart spawning"). If the Gemini client (and the network
# connections/sockets its httpx pool opens) were created before that fork,
# every forked worker would inherit the same connection state, which isn't
# safe across forks — some workers eventually hang forever on a stale,
# inherited connection. Creating it per-process, after the fork, avoids this.
_client = None


def get_client():
    global _client
    if _client is None:
        _client = genai.Client()
    return _client


app = Flask(__name__)

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address


def get_api_key_or_ip():
    api_key = request.headers.get("x-api-key")
    return api_key if api_key else get_remote_address()


limiter = Limiter(
    key_func=get_api_key_or_ip,
    app=app,
    storage_uri="file:///tmp/flask_limiter_storage"
)


@app.route("/health")
def health_check():
    return jsonify({"status": "ok"})


class GenerateRequest(BaseModel):
    prompt: str

    @field_validator("prompt")
    @classmethod
    def prompt_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("prompt cannot be empty")
        return v


class GenerateAsyncRequest(BaseModel):
    prompt: str
    callback_url: str

    @field_validator("prompt")
    @classmethod
    def prompt_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("prompt cannot be empty")
        return v


class UpstreamError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(message)


def call_gemini(prompt: str) -> str:
    if DEMO_MODE:
        return f"[demo response] You said: {prompt}"

    try:
        response = get_client().models.generate_content(
            model="gemini-3.1-flash-lite",
            contents=prompt,
        )
        return response.text

    except ClientError as e:
        if e.code == 429:
            logger.warning("Rate limited by Gemini")
            raise UpstreamError(
                429, "AI service is rate limited right now — please retry shortly."
            )
        logger.error(f"Gemini client error: code={e.code}, msg={e.message}")
        raise UpstreamError(
            400, "Request was rejected by the AI service — check your input."
        )

    except ServerError:
        logger.error("Gemini server error")
        raise UpstreamError(
            503, "AI service temporarily unavailable — please try again."
        )

    except Exception:
        logger.exception("Unexpected error calling Gemini")
        raise UpstreamError(500, "An unexpected error occurred.")


@app.route("/generate", methods=["POST"])
@limiter.limit("10/minute")
def generate():
    try:
        req = GenerateRequest.model_validate(request.get_json(force=True))
    except ValidationError as e:
        return jsonify({"detail": str(e)}), 400

    try:
        result = call_gemini(req.prompt)
    except UpstreamError as e:
        return jsonify({"detail": e.message}), e.status_code

    return jsonify({"result": result})


def run_job_and_notify(job_id: str, prompt: str, callback_url: str):
    try:
        result = call_gemini(prompt)
        payload = {"job_id": job_id, "status": "completed", "result": result}
    except UpstreamError as e:
        payload = {"job_id": job_id, "status": "failed", "error": e.message}

    try:
        httpx.post(callback_url, json=payload, timeout=10)
        logger.info(f"Webhook delivered for job {job_id}")
    except httpx.RequestError:
        logger.error(f"Failed to deliver webhook for job {job_id}")


@app.route("/generate/async", methods=["POST"])
@limiter.limit("10/minute")
def generate_async():
    try:
        req = GenerateAsyncRequest.model_validate(request.get_json(force=True))
    except ValidationError as e:
        return jsonify({"detail": str(e)}), 400

    job_id = str(uuid.uuid4())

    thread = threading.Thread(
        target=run_job_and_notify,
        args=(job_id, req.prompt, req.callback_url),
        daemon=True,
    )
    thread.start()

    return jsonify({"job_id": job_id, "status": "accepted"})
