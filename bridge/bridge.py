#!/usr/bin/env python3
"""
Exotel Voicebot (bidirectional WebSocket)  <->  Ultravox Agent (serverWebSocket @ 8 kHz)

Exotel WS events (JSON text):
  - {"event":"start", ...}
  - {"event":"media","media":{"payload":"<base64 s16le mono 8kHz>"}}
  - {"event":"dtmf", ...}          # optional
  - {"event":"stop"}

We:
  - Create an Ultravox Agent call via: POST /api/agents/{agent_id}/calls (returns 201 + joinUrl).
  - Connect to joinUrl (WebSocket, raw PCM bytes).
  - Forward caller PCM Exotel -> Ultravox.
  - Forward Ultravox PCM -> Exotel as base64 "media" frames (~100 ms).
"""

import asyncio
import os
import json
import base64
import time
import traceback

import aiohttp
import websockets

# -------- Config via env --------
def get_ulx_base() -> str:
    # Fallback to official base if unset
    return os.environ.get("ULTRAVOX_API_BASE") or "https://api.ultravox.ai/api"

ULX_KEY   = os.environ.get("ULTRAVOX_API_KEY")
AGENT_ID  = os.environ.get("ULTRAVOX_AGENT_ID")

# PSTN sample rates for Exotel Voicebot (keep them 8000 unless you know what you're doing)
IN_SR     = int(os.environ.get("IN_SAMPLE_RATE",  "8000"))  # caller -> us
OUT_SR    = int(os.environ.get("OUT_SAMPLE_RATE", "8000"))  # us -> caller

# Frame sizing: 100 ms at 8kHz, 16-bit mono => 0.1 * 8000 * 2 = 1600 bytes
CHUNK_MS    = int(os.environ.get("CHUNK_MS", "100"))
CHUNK_BYTES = int(IN_SR * 2 * (CHUNK_MS / 1000.0))  # must be a multiple of 320 bytes at 8kHz
if CHUNK_BYTES % 320 != 0:
    # snap to nearest lower multiple of 320 to keep Exotel happy
    CHUNK_BYTES = (CHUNK_BYTES // 320) * 320 or 320

# How long to send silence while Ultravox spins up
PRIMER_SECONDS = float(os.environ.get("PRIMER_SECONDS", "1.5"))

# -------- Helpers --------
def log(*args):
    print(time.strftime("[%H:%M:%S]"), *args, flush=True)

def b64_of_pcm(pcm_bytes: bytes) -> str:
    return base64.b64encode(pcm_bytes).decode("ascii")

SILENCE_100MS = b"\x00\x00" * (IN_SR // 10)  # 100 ms of s16le @ IN_SR
SILENCE_100MS_B64 = b64_of_pcm(SILENCE_100MS)

async def prime_exotel_with_silence(ws, seconds: float = PRIMER_SECONDS):
    """Send small silence frames to keep the Exotel stream alive while the bot warms up."""
    if seconds <= 0:
        return
    chunks = max(1, int(seconds / (CHUNK_MS / 1000.0)))
    for _ in range(chunks):
        try:
            await ws.send(json.dumps({"event": "media",
                                      "media": {"payload": SILENCE_100MS_B64}}))
        except Exception as e:
            log("[primer] failed to send silence:", repr(e))
            return
        await asyncio.sleep(CHUNK_MS / 1000.0)

def pop_chunks(buf: bytearray, size: int):
    """Copy-out fixed-size chunks, then shrink buffer. Avoid memoryviews to prevent BufferError."""
    while len(buf) >= size:
        chunk = bytes(buf[:size])
        del buf[:size]
        yield chunk

# -------- Ultravox --------
async def start_ulx_agent_call(session: aiohttp.ClientSession) -> str:
    base = get_ulx_base()
    if not ULX_KEY:
        raise RuntimeError("ULTRAVOX_API_KEY missing")
    if not AGENT_ID:
        raise RuntimeError("ULTRAVOX_AGENT_ID missing")

    url = f"{base}/agents/{AGENT_ID}/calls"
    log("[ulx] POST", url)

    payload = {
        "medium": {
            "serverWebSocket": {
                "inputSampleRate":  IN_SR,
                "outputSampleRate": OUT_SR
            }
        }
    }

    async with session.post(
        url,
        headers={"X-API-Key": ULX_KEY, "Content-Type": "application/json"},
        json=payload,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as r:
        text = await r.text()
        if r.status not in (200, 201):
            raise RuntimeError(f"Ultravox agent call failed {r.status}: {text[:400]}")
        data = json.loads(text)
        join = data.get("joinUrl")
        if not join:
            raise RuntimeError(f"Agent call OK but missing joinUrl: {text[:400]}")
        log("[ulx] agent call", r.status, "| joinUrl acquired")
        return join

# -------- Bridge handler --------
async def handle_exotel_call(ws: websockets.WebSocketServerProtocol, path: str):
    log("[exotel] connected:", path)
    start_ts = time.time()

    try:
        async with aiohttp.ClientSession() as session:
            # Start primer ASAP so Exotel hears something while we dial Ultravox
            primer_task = asyncio.create_task(prime_exotel_with_silence(ws, PRIMER_SECONDS))

            join_url = await start_ulx_agent_call(session)

            async with websockets.connect(
                join_url, max_size=None, ping_interval=20, ping_timeout=20
            ) as ulx:
                log("[ulx] websocket connected")

                ulx_out_buf = bytearray()
                exo_in_frames = 0
                exo_out_frames = 0
                ulx_in_bytes = 0
                ulx_first_audio_ts = None

                async def exotel_to_ulx():
                    nonlocal exo_in_frames
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        et = msg.get("event")
                        if et == "start":
                            # Helpful for correlating in logs
                            sid = msg.get("start", {}).get("stream_sid")
                            log("[exotel] start; stream_sid:", sid)
                        elif et == "media":
                            try:
                                b64 = msg["media"]["payload"]
                                pcm = base64.b64decode(b64)
                                await ulx.send(pcm)  # raw s16le
                                exo_in_frames += 1
                            except Exception as e:
                                log("[err] exotel media decode/send:", repr(e))
                        elif et == "dtmf":
                            # optional: handle or ignore
                            pass
                        elif et == "stop":
                            log("[exotel] stop event from Exotel")
                            break

                async def ulx_to_exotel():
                    nonlocal ulx_out_buf, exo_out_frames, ulx_in_bytes, ulx_first_audio_ts
                    async for m in ulx:
                        if isinstance(m, (bytes, bytearray)):
                            if ulx_first_audio_ts is None:
                                ulx_first_audio_ts = time.time()
                                log(f"[ulx] first audio after {ulx_first_audio_ts - start_ts:.3f}s")

                            ulx_out_buf.extend(m)
                            ulx_in_bytes += len(m)

                            for chunk in pop_chunks(ulx_out_buf, CHUNK_BYTES):
                                frame = {"event": "media",
                                         "media": {"payload": b64_of_pcm(chunk)}}
                                await ws.send(json.dumps(frame))
                                exo_out_frames += 1
                        else:
                            # If Ultravox ever sends JSON side-data (e.g., transcripts), you can log it here
                            # log("[ulx] data:", m)
                            pass

                await asyncio.gather(exotel_to_ulx(), ulx_to_exotel())

                # flush any small tail so caller hears the last syllable
                if ulx_out_buf:
                    await ws.send(json.dumps({
                        "event": "media",
                        "media": {"payload": b64_of_pcm(bytes(ulx_out_buf))}
                    }))
                    exo_out_frames += 1
                    ulx_out_buf.clear()

                log(f"[stats] exotel->ulx frames: {exo_in_frames} | "
                    f"ulx bytes in: {ulx_in_bytes} | exotel<-ulx frames: {exo_out_frames}")

            # ulx WS closed
            if not primer_task.done():
                primer_task.cancel()

    except websockets.ConnectionClosedOK:
        log("[bridge] connection closed cleanly (1000)")
    except websockets.ConnectionClosedError as e:
        log(f"[bridge] ws closed with error: {e}")
    except Exception as e:
        log("[bridge] error:", repr(e))
        traceback.print_exc()
        try:
            await ws.close(code=1011, reason=str(e)[:120])
        except Exception:
            pass

# -------- Entrypoint --------
async def main():
    # Caddy terminates TLS and reverse-proxies /exotel -> ws://bridge:8080
    async with websockets.serve(
        handle_exotel_call,
        host="0.0.0.0",
        port=8080,
        max_size=None,
        ping_interval=20,
        ping_timeout=20,
    ):
        log("Bridge listening on :8080")
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
