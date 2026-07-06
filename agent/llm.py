"""LLM backend for the planner — Groq (free tier), Anthropic API, or the
local Claude Code CLI (uses the user's Claude subscription login as a proxy;
no API key needed).

Selection order:
  RECLAMATION_LLM=groq|anthropic|claude-cli    explicit override
  else: groq if GROQ_API_KEY is set, else anthropic if ANTHROPIC_API_KEY is
  set, else claude-cli if the `claude` CLI is on PATH.

Groq: GROQ_MODEL, default llama-3.3-70b-versatile.
Anthropic API: RECLAMATION_PLANNER_MODEL, default claude-opus-4-8.
claude-cli: RECLAMATION_CLAUDE_MODEL to override the CLI's default model.
"""
import json
import os
import shutil
import subprocess
import urllib.request

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_DEFAULT_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
ANTHROPIC_DEFAULT_MODEL = os.environ.get("RECLAMATION_PLANNER_MODEL", "claude-opus-4-8")


def provider():
    p = os.environ.get("RECLAMATION_LLM", "").lower()
    if p in ("groq", "anthropic", "claude-cli"):
        return p
    if os.environ.get("GROQ_API_KEY"):
        return "groq"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if shutil.which("claude"):
        return "claude-cli"
    raise RuntimeError("no LLM configured — set GROQ_API_KEY (free: console.groq.com), "
                       "ANTHROPIC_API_KEY, or install/login the Claude Code CLI")


def complete(system, prompt, max_tokens=4000):
    """Return the model's text response for a system+user prompt pair."""
    p = provider()
    if p == "groq":
        return _groq(system, prompt, max_tokens)
    if p == "claude-cli":
        return _claude_cli(system, prompt)
    return _anthropic(system, prompt, max_tokens)


def _groq(system, prompt, max_tokens):
    body = json.dumps({
        "model": GROQ_DEFAULT_MODEL,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }).encode()
    req = urllib.request.Request(
        GROQ_URL, data=body,
        headers={
            "Authorization": f"Bearer {os.environ['GROQ_API_KEY']}",
            "Content-Type": "application/json",
            # bare urllib UA gets Cloudflare-blocked (error 1010)
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.load(resp)
    return data["choices"][0]["message"]["content"]


def _claude_cli(system, prompt):
    """Plan via the locally logged-in Claude Code CLI — the user's subscription
    acts as the API proxy. One-shot print mode, no tools, JSON-only output."""
    exe = shutil.which("claude")
    if not exe:
        raise RuntimeError("claude CLI not found on PATH")
    cmd = [exe, "-p", "--output-format", "text", "--max-turns", "1"]
    model = os.environ.get("RECLAMATION_CLAUDE_MODEL")
    if model:
        cmd += ["--model", model]
    full_prompt = f"{system}\n\n---\n\n{prompt}\n\nRespond with the JSON object only."
    result = subprocess.run(cmd, input=full_prompt, capture_output=True,
                            text=True, encoding="utf-8", timeout=600)
    if result.returncode != 0:
        detail = (result.stderr or "").strip() or (result.stdout or "").strip() or "(no output)"
        raise RuntimeError(f"claude CLI failed ({result.returncode}): {detail[:400]}")
    return result.stdout


def _anthropic(system, prompt, max_tokens):
    import anthropic
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=ANTHROPIC_DEFAULT_MODEL,
        max_tokens=max_tokens,
        thinking={"type": "adaptive"},
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    return next(b.text for b in resp.content if b.type == "text")
