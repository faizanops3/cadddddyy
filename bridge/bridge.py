#!/usr/bin/env python3
"""
Exotel Voicebot (bidirectional WebSocket)  <->  Ultravox Agent (serverWebSocket @ 8 kHz)

Exotel WS (JSON text) -> events: start, media {payload: base64 s16le 8kHz}, dtmf, stop.
We:
  - POST /api/agents/{agent_id}/calls  -> 201 with { joinUrl }
  - Connect to joinUrl (WS, raw PCM in/out)
  - Pump audio both ways
  - On ANY end, close the other side and DELETE /api/calls/{callId}
"""

import asyncio, os, json, base64, time, traceback
from urllib.parse import urlparse
import aiohttp
import websockets

# -------- Config via env --------
def get_ulx_base() -> str:
    return os.environ.get("ULTRAVOX_API_BASE") or "https://api.ultravox.ai/api"

ULX_KEY   = os.environ.get("ULTRAVOX_API_KEY")
AGENT_ID  = os.environ.get("ULTRAVOX_AGENT_ID")
IN_SR     = int(os.environ.get("IN_SAMPLE_RATE",  "8000"))  # caller -> us
OUT_SR    = int(os.environ.get("OUT_SAMPLE_RATE", "8000"))  # us -> caller
PRIMER_SECONDS = float(os.environ.get("PRIMER_SECONDS", "1.2"))
MAX_DURATION = os.environ.get("ULTRAVOX_MAX_DURATION", "600s")  # cap the session

# Frame sizing: 100 ms @ 8kHz s16le -> 1600B; keep multiple of 320B
CHUNK_MS    = int(os.environ.get("CHUNK_MS", "100"))
CHUNK_BYTES = int(IN_SR * 2 * (CHUNK_MS / 1000.0))
if CHUNK_BYTES % 320 != 0:
    CHUNK_BYTES = (CHUNK_BYTES // 320) * 320 or 320

def log(*args): print(time.strftime("[%H:%M:%S]"), *args, flush=True)
def b64_of_pcm(b: bytes) -> str: return base64.b64encode(b).decode("ascii")

SILENCE_100MS = b"\x00\x00" * (IN_SR // 10)
SILENCE_100MS_B64 = b64_of_pcm(SILENCE_100MS)

async def prime_exotel_with_silence(ws, seconds: float = PRIMER_SECONDS):
    if seconds <= 0: return
    chunks = max(1, int(seconds / (CHUNK_MS / 1000.0)))
    for _ in range(chunks):
        try:
            await ws.send(json.dumps({"event": "media",
                                      "media": {"payload": SILENCE_100MS_B64}}))
        except Exception as e:
            log("[primer] failed:", repr(e))
            return
        await asyncio.sleep(CHUNK_MS / 1000.0)

def pop_chunks(buf: bytearray, size: int):
    while len(buf) >= size:
        chunk = bytes(buf[:size]); del buf[:size]
        yield chunk

def extract_call_id(join_url: str) -> str | None:
    # joinUrl looks like: wss://voice.ultravox.ai/calls/<CALL_ID>/server_web_socket
    try:
        p = urlparse(join_url)
        parts = [x for x in p.path.split("/") if x]
        i = parts.index("calls")
        return parts[i+1] if i+1 < len(parts) else None
    except Exception:
        return None

# -------- Ultravox API --------
async def start_ulx_agent_call(session: aiohttp.ClientSession) -> tuple[str, str]:
    base = get_ulx_base()
    if not ULX_KEY:  raise RuntimeError("ULTRAVOX_API_KEY missing")
    if not AGENT_ID: raise RuntimeError("ULTRAVOX_AGENT_ID missing")

    url = f"{base}/agents/{AGENT_ID}/calls"
    log("[ulx] POST", url)

    payload = {
        "maxDuration": MAX_DURATION,  # defensive cap
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
        call_id = extract_call_id(join) or ""
        log("[ulx] agent call", r.status, "| callId:", call_id or "?", "| joinUrl acquired")
        return join, call_id

async def end_ulx_call(session: aiohttp.ClientSession, call_id: str):
    if not call_id: return
    base = get_ulx_base()
    url = f"{base}/calls/{call_id}"
    try:
        async with session.delete(url, headers={"X-API-Key": ULX_KEY}, timeout=10) as r:
            log(f"[ulx] DELETE /calls/{call_id} -> {r.status}")
    except Exception as e:
        log("[ulx] delete failed:", repr(e))

# -------- Bridge handler --------
async def handle_exotel_call(ws: websockets.WebSocketServerProtocol, path: str):
    log("[exotel] connected:", path)
    start_ts = time.time()
    call_id = ""
    session = aiohttp.ClientSession()

    try:
        # keep Exotel alive while Ultravox spins up
        primer_task = asyncio.create_task(prime_exotel_with_silence(ws, PRIMER_SECONDS))

        join_url, call_id = await start_ulx_agent_call(session)

        async with websockets.connect(
            join_url, max_size=None, ping_interval=20, ping_timeout=20
        ) as ulx:
            log("[ulx] websocket connected")
            ulx_out_buf = bytearray()
            exo_in_frames = exo_out_frames = ulx_in_bytes = 0
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
                        sid = msg.get("start", {}).get("stream_sid")
                        log("[exotel] start; stream_sid:", sid)
                    elif et == "media":
                        try:
                            pcm = base64.b64decode(msg["media"]["payload"])
                            await ulx.send(pcm)  # raw s16le
                            exo_in_frames += 1
                        except Exception as e:
                            log("[err] exotel media decode/send:", repr(e))
                    elif et == "dtmf":
                        pass
                    elif et == "stop":
                        log("[exotel] stop event")
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
                            frame = {"event":"media","media":{"payload": b64_of_pcm(chunk)}}
                            await ws.send(json.dumps(frame))
                            exo_out_frames += 1
                    else:
                        # optional: log JSON side-messages
                        # log("[ulx] data:", m)
                        pass

            # Run both pumps; as soon as one ends, close the other so we don’t hang
            t1 = asyncio.create_task(exotel_to_ulx())
            t2 = asyncio.create_task(ulx_to_exotel())

            done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)

            # actively close both directions to stop billing immediately
            try:
                await ulx.close()
            except Exception:
                pass
            try:
                await ws.close()
            except Exception:
                pass

            # cancel the leftover task if still running
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

            # flush any tail to reduce truncation
            if ulx_out_buf:
                await ws.send(json.dumps({"event":"media","media":{"payload": b64_of_pcm(bytes(ulx_out_buf))}}))
                exo_out_frames += 1
                ulx_out_buf.clear()

            log(f"[stats] exotel->ulx frames: {exo_in_frames} | ulx bytes in: {ulx_in_bytes} | exotel<-ulx frames: {exo_out_frames}")

        # cancel primer if still ticking
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
    finally:
        # always attempt to end the Ultravox call
        try:
            await end_ulx_call(session, call_id)
        finally:
            await session.close()

# -------- Entrypoint --------
async def main():
    # Caddy terminates TLS and proxies /exotel -> ws://bridge:8080
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
