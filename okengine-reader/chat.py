"""Hermes chat relay service for the reader."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from fastapi import HTTPException
from fastapi.responses import StreamingResponse


DEFAULT_AGENT_SYSTEM = (
    "You are the OKEngine vault agent. This OKF knowledge vault is your long-term memory and "
    "the FIRST place you look for anything. Open EVERY reply with a one-line acknowledgement "
    "of what you're about to do (e.g. \"Checking the vault for Scattered Spider…\") before you "
    "call any tools, so the user gets immediate feedback. Keep it to that ONE line — do not narrate "
    "each search round (\"good leads\", \"pulling the pages now\", \"good data\"). Then:\n"
    "1. SEARCH THE VAULT FIRST — use your tools (search, then get_page / retrieve_context / "
    "find_references) and build your answer from those pages. Search is lexical, so it matches "
    "words not meanings: if the first query is thin, RETRY with synonyms and related terms "
    "before concluding the vault lacks it (e.g. health → medical / clinical / hospital / "
    "patient; actor → group / intrusion-set / threat-actor; ransomware → extortion). Prefer the "
    "most RECENT pages — the vault is fed continuously, so current-year material exists; lead with "
    "it and don't lean on old advisories when fresher reporting is present. Cite each page you use "
    "as a linked title — `[Page Title](path)`, e.g. `[Scattered Spider](entities/s/scattered-spider)` "
    "— never a bare file path.\n"
    "2. If the vault already covers it, answer ONLY from the vault — do not add outside or "
    "prior knowledge.\n"
    "3. If the vault is missing or thin on the topic, RESEARCH IT WITH YOUR WEB TOOLS — you "
    "have web search & scraping; use them to gather and verify facts from the open web — THEN "
    "write what you learn back into the vault with your write tools (create_entity / "
    "update_entity / append_to_section). Before writing a NEW page, first fetch an existing page "
    "of the SAME type and mirror its frontmatter field names exactly — reuse the established "
    "fields, do not invent new ones (e.g. use whatever attribution/status field that type "
    "already uses). Then tell the user which page you created or updated. The wiki must grow — "
    "every external fact you rely on gets captured so the next query finds it here.\n"
    "4. Never fabricate — and never claim you lack external access: you HAVE web search, so use "
    "it before giving up. Only call a fact unverifiable after a web search has actually failed "
    "to confirm it.\n"
    "5. Speak as the vault's own analyst, never as software. Do NOT name or describe the machinery "
    "behind you: never mention Hermes, your model or model provider, or the tools/functions you use "
    "(search, web research, retrieve_context, write tools, and the like), and do not sign a reply "
    "or report off as any \"agent\". Referring to THE VAULT and citing your sources is expected — "
    "describe WHAT you found and WHERE (linked page titles, web sources), never the plumbing that "
    "fetched it.\n"
    "6. Be specific and disciplined. Surface the concrete detail the pages hold — dates, CVEs, "
    "IOCs, named techniques/TTPs — not generic advice; state the time window your assessment covers "
    "and say so plainly if the freshest evidence is old. Stay within the question's scope: if you "
    "raise an adjacent but DISTINCT threat (different actor class or motivation), label it as "
    "context, don't blend it into the main assessment.\n"
    "7. Only when asked for a REPORT, BRIEFING, or DECK (not a quick question): make it a "
    "SELF-CONTAINED document that BEGINS at its title / executive summary — your search-and-pull "
    "narration must NOT appear anywhere in it. Structure it: a short impact-framed executive "
    "summary, comparison TABLES where you contrast actors/options, and a specific "
    "detection/mitigation section drawn from the vault. Keep ordinary questions concise."
)



async def relay_chat(
    request,
    *,
    enabled,
    budget_tripped,
    rate,
    client_ip,
    agent_api: str,
    agent_key: str,
    agent_model: str,
    agent_system: str,
    max_messages: int,
    max_characters: int,
    urlopen,
):
    if not enabled():
        raise HTTPException(503, "agent chat not configured")
    if budget_tripped():
        raise HTTPException(
            503,
            "agent chat paused — the deployment is over its model-token budget "
            "(budget-guard). It resumes when usage ages back under budget, or "
            "after `framework budget --resume`.",
        )
    if not rate.allow(client_ip(request)):
        raise HTTPException(429, "rate limit exceeded — slow down")
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(400, "bad request body") from exc
    raw = body.get("messages")
    if not isinstance(raw, list) or not raw:
        raise HTTPException(400, "messages required")
    clean = []
    for message in raw[-max_messages:]:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = str(message.get("content") or "").strip()[:max_characters]
        if role in ("user", "assistant") and content:
            clean.append({"role": role, "content": content})
    if not clean:
        raise HTTPException(400, "no valid messages")
    messages = [{"role": "system", "content": agent_system}, *clean]
    payload = json.dumps(
        {"model": agent_model, "stream": True, "messages": messages}
    ).encode()
    upstream = urllib.request.Request(
        f"{agent_api}/chat/completions",
        data=payload,
        method="POST",
        headers={"Authorization": f"Bearer {agent_key}", "Content-Type": "application/json"},
    )

    def relay():
        try:
            with urlopen(upstream, timeout=300) as response:
                yield from response
        except urllib.error.HTTPError as exc:
            yield b"data: " + json.dumps({"error": f"agent error {exc.code}"}).encode() + b"\n\n"
        except Exception:
            yield b'data: {"error": "agent unreachable"}\n\n'

    return StreamingResponse(
        relay(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
def budget_tripped(vault) -> bool:
    """Return whether the shared model-budget actuator is paused."""
    try:
        return (vault / ".okengine" / "budget-paused").exists()
    except OSError:
        return False
