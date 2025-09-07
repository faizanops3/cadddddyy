#!/usr/bin/env python3
"""
Exotel Voicebot (bidirectional WebSocket) <-> Ultravox Agent (serverWebSocket @ 8 kHz)

Exotel WS events (JSON text):
  - {"event":"start", ...}
  - {"event":"media","media":{"payload":"<base64 s16le mono 8kHz>"}}
  - {"event":"dtmf", ...}          # optional
  - {"event":"stop"}

We:
  - POST /api/agents/{agent_id}/calls  ->  201 { joinUrl, callId }
  - Connect to joinUrl (WebSocket, raw PCM bytes)
  - Forward caller PCM Exotel -> Ultravox
  - Forward Ultravox PCM -> Exotel as base64 "media" frames (~100 ms)
"""

import asyncio
import os
import json
import base64
import time
import traceback

import aiohttp
import websockets

# =======================
# Config via environment
# =======================

def get_ulx_base() -> str:
    # Fallback to official base if unset
    return os.environ.get("ULTRAVOX_API_BASE") or "https://api.ultravox.ai/api"

ULX_KEY   = os.environ.get("ULTRAVOX_API_KEY")
AGENT_ID  = os.environ.get("ULTRAVOX_AGENT_ID")  # required

# PSTN sample rates for Exotel Voicebot (keep them 8000 unless you know what you're doing)
IN_SR     = int(os.environ.get("IN_SAMPLE_RATE",  "8000"))  # caller -> us
OUT_SR    = int(os.environ.get("OUT_SAMPLE_RATE", "8000"))  # us -> caller

# Frame sizing: 100 ms at 8kHz, 16-bit mono => 0.1 * 8000 * 2 = 1600 bytes
CHUNK_MS    = int(os.environ.get("CHUNK_MS", "100"))
_chb = int(IN_SR * 2 * (CHUNK_MS / 1000.0))
CHUNK_BYTES = (_chb // 320) * 320 or 320  # snap to multiple of 320B

# How long to send silence while Ultravox spins up
PRIMER_SECONDS = float(os.environ.get("PRIMER_SECONDS", "1.2"))

# Retry DELETE window (sec)
DELETE_RETRY_WINDOW_S = float(os.environ.get("DELETE_RETRY_WINDOW_S", "10"))

# Optional idle watchdog: hang up if no media from Exotel for N seconds (0 disables)
IDLE_TIMEOUT_S = float(os.environ.get("IDLE_TIMEOUT_S", "0"))

# =======================
# Logging helpers
# =======================

def log(*args):
    print(time.strftime("[%H:%M:%S]"), *args, flush=True)

def mask(s: str, keep=6):
    if not s:
        return ""
    return f"{s[:keep]}…{s[-keep:]}" if len(s) > keep * 2 else s

def b64_of_pcm(pcm_bytes: bytes) -> str:
    return base64.b64encode(pcm_bytes).decode("ascii")

SILENCE_100MS = b"\x00\x00" * (IN_SR // 10)  # 100 ms of s16le @ IN_SR
SILENCE_100MS_B64 = b64_of_pcm(SILENCE_100MS)

async def prime_exotel_with_silence(ws, seconds: float = PRIMER_SECONDS, frame_ms: int = 100):
    """Send small silence frames to keep Exotel stream alive while Ultravox warms up."""
    if seconds <= 0:
        return
    frames = max(1, int(seconds / (frame_ms / 1000.0)))
    for _ in range(frames):
        try:
            await ws.send(json.dumps({"event": "media", "media": {"payload": SILENCE_100MS_B64}}))
        except Exception as e:
            log("[primer] failed to send silence:", repr(e))
            return
        await asyncio.sleep(frame_ms / 1000.0)

def pop_chunks(buf: bytearray, size: int):
    """Copy-out fixed-size chunks, then shrink buffer. Avoid memoryviews to prevent BufferError."""
    while len(buf) >= size:
        chunk = bytes(buf[:size])
        del buf[:size]
        yield chunk

# =======================
# Ultravox REST
# =======================

async def start_ulx_agent_call(session: aiohttp.ClientSession):
    """
    Returns (join_url, call_id)
    """
    base = get_ulx_base()
    if not ULX_KEY:
        raise RuntimeError("ULTRAVOX_API_KEY missing")
    if not AGENT_ID:
        raise RuntimeError("ULTRAVOX_AGENT_ID missing")

    url = f"{base}/agents/{AGENT_ID}/calls"
    log("[ulx] POST", url, f"| base={base} agent={mask(AGENT_ID)}")

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
        call_id = data.get("callId") or data.get("id")
        if not join:
            raise RuntimeError(f"Agent call OK but missing joinUrl: {text[:400]}")
        log("[ulx] agent call", r.status, "| callId:", call_id, "| joinUrl acquired")
        return join, call_id

async def end_ulx_call(session: aiohttp.ClientSession, call_id: str):
    """DELETE the Ultravox call with retries to avoid lingering 30-min sessions."""
    if not call_id:
        return
    base = get_ulx_base()
    url = f"{base}/calls/{call_id}"
    backoff = 0.5
    deadline = time.time() + DELETE_RETRY_WINDOW_S
    last_status = None
    while time.time() < deadline:
        try:
            async with session.delete(url, headers={"X-API-Key": ULX_KEY}, timeout=10) as r:
                last_status = r.status
                if r.status in (200, 202, 204, 404):
                    log(f"[ulx] DELETE /calls/{call_id} -> {r.status} (done)")
                    return
                if r.status in (425, 409, 429):  # settling/too early/conflict
                    log(f"[ulx] delete {r.status}, retrying in {backoff:.1f}s")
                else:
                    text = await r.text()
                    log(f"[ulx] delete unexpected {r.status}: {text[:200]}")
        except Exception as e:
            log("[ulx] delete err:", repr(e))
        await asyncio.sleep(backoff)
        backoff = min(backoff * 1.7, 3.0)
    log(f"[ulx] delete gave up after retries; last status={last_status}")

# =======================
# Bridge handler
# =======================

async def handle_exotel_call(ws: websockets.WebSocketServerProtocol, path: str):
    log("[exotel] connected:", path)
    start_ts = time.time()
    last_exo_media_ts = time.time()

    ulx_ws = None
    call_id = None

    try:
        async with aiohttp.ClientSession() as session:
            # Start primer ASAP so Exotel hears something while we dial Ultravox
            primer_task = asyncio.create_task(prime_exotel_with_silence(ws, PRIMER_SECONDS))

            join_url, call_id = await start_ulx_agent_call(session)

            async with websockets.connect(
                join_url, max_size=None, ping_interval=20, ping_timeout=20
            ) as ulx:
                ulx_ws = ulx
                log("[ulx] websocket connected")

                ulx_out_buf = bytearray()
                exo_in_frames = 0
                exo_out_frames = 0
                ulx_in_bytes = 0
                ulx_first_audio_ts = None
                stop_event = asyncio.Event()

                async def exotel_to_ulx():
                    nonlocal exo_in_frames, last_exo_media_ts
                    try:
                        async for raw in ws:
                            try:
                                msg = json.loads(raw)
                            except Exception:
                                # Ignore any non-JSON test frames
                                continue
                            et = msg.get("event")
                            if et == "start":
                                sid = msg.get("start", {}).get("stream_sid")
                                log("[exotel] start; stream_sid:", sid)
                            elif et == "media":
                                try:
                                    b64 = msg["media"]["payload"]
                                    pcm = base64.b64decode(b64)
                                    await ulx.send(pcm)  # raw s16le
                                    exo_in_frames += 1
                                    last_exo_media_ts = time.time()
                                except Exception as e:
                                    log("[err] exotel media decode/send:", repr(e))
                            elif et == "dtmf":
                                # optional: handle or ignore
                                pass
                            elif et == "stop":
                                log("[exotel] stop event")
                                stop_event.set()
                                # close ulx proactively so ulx_to_exotel unblocks
                                try:
                                    await ulx.close(code=1000)
                                except Exception:
                                    pass
                                break
                    except websockets.ConnectionClosed:
                        log("[exotel] ws closed (peer)")
                        stop_event.set()
                        try:
                            await ulx.close(code=1000)
                        except Exception:
                            pass

                async def ulx_to_exotel():
                    nonlocal ulx_out_buf, exo_out_frames, ulx_in_bytes, ulx_first_audio_ts
                    try:
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
                                # If Ultravox ever sends JSON side-data (e.g., transcripts), you can log here
                                # log("[ulx] data:", m)
                                pass
                    except websockets.ConnectionClosed:
                        # likely closed from exotel_to_ulx on stop
                        pass
                    finally:
                        # Flush any tail audio so caller hears the last syllable
                        if ulx_out_buf:
                            try:
                                await ws.send(json.dumps({
                                    "event": "media",
                                    "media": {"payload": b64_of_pcm(bytes(ulx_out_buf))}
                                }))
                                exo_out_frames += 1
                            except Exception:
                                pass
                            ulx_out_buf.clear()

                async def idle_watchdog():
                    if IDLE_TIMEOUT_S <= 0:
                        return
                    while not stop_event.is_set():
                        await asyncio.sleep(1.0)
                        if time.time() - last_exo_media_ts > IDLE_TIMEOUT_S:
                            log(f"[watchdog] idle > {IDLE_TIMEOUT_S:.0f}s; closing")
                            stop_event.set()
                            try:
                                await ws.close(code=1000, reason="idle-timeout")
                            except Exception:
                                pass
                            try:
                                await ulx.close(code=1000)
                            except Exception:
                                pass
                            break

                # Run the pumps (+ optional watchdog)
                tasks = [asyncio.create_task(exotel_to_ulx()),
                         asyncio.create_task(ulx_to_exotel())]
                if IDLE_TIMEOUT_S > 0:
                    tasks.append(asyncio.create_task(idle_watchdog()))
                await asyncio.gather(*tasks, return_exceptions=True)

                # Stats
                wall = time.time() - start_ts
                log(f"[stats] exotel->ulx frames: {exo_in_frames} | "
                    f"ulx bytes in: {ulx_in_bytes} | exotel<-ulx frames: {exo_out_frames}")
                log(f"[stats] wall duration: {wall:.2f}s")

            # Ultravox WS context exited; let backend settle then DELETE
            await asyncio.sleep(0.5)
            await end_ulx_call(session, call_id)

            # Cancel primer if it was still running
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

# =======================
# Entrypoint
# =======================

async def main():
    log("Bridge starting…")
    log(f"ULX base={get_ulx_base()} | agent={mask(AGENT_ID)} "
        f"| IN_SR={IN_SR} OUT_SR={OUT_SR} CHUNK_MS={CHUNK_MS} PRIMER={PRIMER_SECONDS}s")
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
