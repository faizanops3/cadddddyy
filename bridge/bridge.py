#!/usr/bin/env python3
"""
Exotel Voicebot (bidirectional WebSocket)  <->  Ultravox Agent (serverWebSocket @ 8 kHz)

Exotel WS events:
  - {"event":"start", ...}
  - {"event":"media","media":{"payload":"<base64 s16le mono 8kHz>"}}
  - {"event":"dtmf", ...}   # optional
  - {"event":"stop"}

We:
  - Create an Ultravox call via: POST /api/agents/{agent_id}/calls  (returns 201 + joinUrl)
  - Connect to joinUrl (WebSocket)
  - Forward caller PCM bytes Exotel -> Ultravox
  - Forward Ultravox PCM bytes back to Exotel as base64 "media" frames (~100 ms chunks)
"""

import asyncio, os, json, base64, aiohttp, websockets, traceback

def get_ulx_base() -> str:
    # Never allow None; default to official base
    return os.environ.get("ULTRAVOX_API_BASE") or "https://api.ultravox.ai/api"

ULX_KEY   = os.environ.get("ULTRAVOX_API_KEY")
AGENT_ID  = os.environ.get("ULTRAVOX_AGENT_ID")
MODEL     = os.environ.get("ULTRAVOX_MODEL", "fixie-ai/ultravox")
VOICE     = os.environ.get("ULTRAVOX_VOICE", "Mark")
PROMPT    = os.environ.get("ULTRAVOX_SYSTEM_PROMPT", "You are a friendly agent. Keep answers short.")
IN_SR     = int(os.environ.get("IN_SAMPLE_RATE", "8000"))   # PSTN sample-rate
OUT_SR    = int(os.environ.get("OUT_SAMPLE_RATE", "8000"))

# Exotel bidirectional best-practice: multiples of 320 bytes (20ms @ 8 kHz s16le).
# ~100ms chunks -> 1600 bytes.
CHUNK_BYTES = 1600  # safe default (~100ms)
# (If you want even snappier barge-in later, you can send smaller multiples like 640B/960B; keep it multiple of 320B.)  # noqa: E501

async def start_ulx_agent_call(session: aiohttp.ClientSession) -> str:
    base = get_ulx_base()
    if not AGENT_ID:
        raise RuntimeError("ULTRAVOX_AGENT_ID missing")
    url = f"{base}/agents/{AGENT_ID}/calls"
    print(f"[boot] ULX_API: {base} | POST {url}")

    payload = {
        # Agent carries model/voice/prompt config; we only specify the medium.
        "medium": {
            "serverWebSocket": {
                "inputSampleRate":  IN_SR,
                "outputSampleRate": OUT_SR
                # "clientBufferSizeMs": 30000  # optional; large buffer + use PlaybackClearBuffer messages
            }
        }
    }

    async with session.post(
        url,
        headers={"X-API-Key": ULX_KEY, "Content-Type": "application/json"},
        json=payload,
        timeout=aiohttp.ClientTimeout(total=30)
    ) as r:
        text = await r.text()
        if r.status not in (200, 201):
            raise RuntimeError(f"Ultravox agent call failed {r.status}: {text[:300]}")
        data = json.loads(text)
        join = data.get("joinUrl")
        if not join:
            raise RuntimeError(f"Agent call OK but missing joinUrl: {text[:300]}")
        print(f"[ulx] agent call {r.status}; joinUrl acquired.")
        return join

def pop_chunks(buf: bytearray, size: int):
    """Copy-out fixed-size chunks, then shrink buffer. No memoryview -> no BufferError."""
    while len(buf) >= size:
        chunk = bytes(buf[:size])
        del buf[:size]
        yield chunk

async def handle_exotel_call(ws: websockets.WebSocketServerProtocol, path: str):
    print(f"[exotel] connected: {path}")
    if not ULX_KEY:
        print("[err] ULTRAVOX_API_KEY missing")
        await ws.close(code=1011, reason="ULTRAVOX_API_KEY missing")
        return
    try:
        async with aiohttp.ClientSession() as session:
            join_url = await start_ulx_agent_call(session)

            # Connect to Ultravox realtime WS (raw PCM bytes in/out)
            async with websockets.connect(
                join_url, max_size=None, ping_interval=20, ping_timeout=20
            ) as ulx:
                print("[ulx] websocket connected.")
                ulx_out_buf = bytearray()

                async def exotel_to_ulx():
                    """Exotel JSON -> raw PCM bytes to Ultravox."""
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            # Ignore any non-JSON test frames
                            continue
                        et = msg.get("event")
                        if et == "media":
                            try:
                                b64 = msg["media"]["payload"]
                                pcm = base64.b64decode(b64)
                                await ulx.send(pcm)  # raw s16le
                            except Exception as e:
                                print("[err] exotel media decode/send:", repr(e))
                        elif et == "stop":
                            print("[exotel] stop")
                            break
                        # 'start', 'dtmf', etc. can be handled as needed

                async def ulx_to_exotel():
                    """Ultravox raw PCM -> Exotel JSON media frames (base64 s16le @ 8kHz)."""
                    nonlocal ulx_out_buf
                    async for m in ulx:
                        if isinstance(m, (bytes, bytearray)):
                            ulx_out_buf.extend(m)
                            for chunk in pop_chunks(ulx_out_buf, CHUNK_BYTES):
                                b64 = base64.b64encode(chunk).decode("ascii")
                                frame = {"event":"media","media":{"payload": b64}}
                                await ws.send(json.dumps(frame))
                        else:
                            # Data messages (transcripts, clear buffer, etc.) if enabled
                            # print("[ulx] data:", m)
                            pass

                await asyncio.gather(exotel_to_ulx(), ulx_to_exotel())

                # optional: flush remainder
                if ulx_out_buf:
                    b64 = base64.b64encode(bytes(ulx_out_buf)).decode("ascii")
                    await ws.send(json.dumps({"event":"media","media":{"payload": b64}}))
                    ulx_out_buf.clear()

    except websockets.ConnectionClosedOK:
        print("[bridge] connection closed cleanly (1000).")
    except websockets.ConnectionClosedError as e:
        print(f"[bridge] ws closed with error: {e}")
    except Exception as e:
        print("[bridge] error:", repr(e))
        traceback.print_exc()
        try:
            await ws.close(code=1011, reason=str(e)[:120])
        except Exception:
            pass

async def main():
    # Caddy terminates TLS and reverse-proxies to us at /exotel → ws://bridge:8080
    async with websockets.serve(
        handle_exotel_call, host="0.0.0.0", port=8080, max_size=None, ping_interval=20, ping_timeout=20
    ):
        print("Bridge listening on :8080")
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
