#!/usr/bin/env python3
"""
Exotel Voicebot (bidirectional WebSocket)  <->  Ultravox (serverWebSocket @ 8 kHz)

- Exotel sends JSON frames:
    {"event":"start", ...}
    {"event":"media","media":{"payload":"<base64 s16le mono 8kHz>"}}
    {"event":"stop"}

- We create an Ultravox call via REST (POST /api/calls) with
  medium.serverWebSocket { inputSampleRate: 8000, outputSampleRate: 8000 },
  accept the 201 Created response, then connect to the returned joinUrl (WS).

- We forward caller PCM from Exotel -> Ultravox (raw bytes),
  and chunk Ultravox PCM back to Exotel as base64 "media" frames using
  ~100 ms (1600 bytes) multiples (each 20 ms = 320 bytes @ 8 kHz mono s16le).
"""

import asyncio
import os
import json
import base64
import aiohttp
import websockets
import traceback

# ULX_API = os.environ.get("ULTRAVOX_API_BASE", "https://api.ultravox.ai/api")
ULX_API = os.environ.get("https://api.ultravox.ai/api")
ULX_KEY = os.environ.get("ULTRAVOX_API_KEY")

MODEL = os.environ.get("ULTRAVOX_MODEL", "fixie-ai/ultravox")
VOICE = os.environ.get("ULTRAVOX_VOICE", "Mark")
SYSTEM_PROMPT = os.environ.get(
    "ULTRAVOX_SYSTEM_PROMPT",
    "You are a friendly agent. Keep answers short."
)

# PSTN = 8 kHz mono s16le
IN_SAMPLE_RATE = int(os.environ.get("IN_SAMPLE_RATE", "8000"))
OUT_SAMPLE_RATE = int(os.environ.get("OUT_SAMPLE_RATE", "8000"))

# Exotel best-practice: send multiples of 320 bytes (20 ms @ 8 kHz, s16le)
EXOTEL_CHUNK_BYTES = 1600  # ~100 ms = 8k * 2B * 0.1s

async def start_ulx_call(session: aiohttp.ClientSession) -> str:
    """Create an Ultravox call and return the WebSocket joinUrl."""
    payload = {
        "systemPrompt": SYSTEM_PROMPT,
        "model": MODEL,
        "voice": VOICE,
        "medium": {
            "serverWebSocket": {
                "inputSampleRate": IN_SAMPLE_RATE,
                "outputSampleRate": OUT_SAMPLE_RATE
            }
        }
    }
    async with session.post(
        f"{ULX_API}/calls",
        headers={"X-API-Key": ULX_KEY, "Content-Type": "application/json"},
        json=payload,
        timeout=aiohttp.ClientTimeout(total=30)
    ) as r:
        text = await r.text()
        if r.status not in (200, 201):
            raise RuntimeError(f"Ultravox /calls failed {r.status}: {text}")
        data = json.loads(text)
        join = data.get("joinUrl")
        if not join:
            raise RuntimeError("Ultravox /calls missing joinUrl")
        print(f"[ulx] call created {r.status}, joinUrl obtained.")
        return join

def chunk_bytes(buf: bytearray, chunk_size: int):
    """Yield chunk_size slices from buf and keep remainder in-place."""
    mv = memoryview(buf)
    n = (len(buf) // chunk_size) * chunk_size
    i = 0
    while i < n:
        yield mv[i:i+chunk_size].tobytes()
        i += chunk_size
    # shrink buffer to remainder
    if n:
        del buf[:n]

async def handle_exotel_call(ws: websockets.WebSocketServerProtocol, path: str):
    print(f"[exotel] connected: {path}")
    if not ULX_KEY:
        print("[err] ULTRAVOX_API_KEY missing")
        await ws.close(code=1011, reason="ULTRAVOX_API_KEY missing")
        return

    try:
        async with aiohttp.ClientSession() as session:
            join_url = await start_ulx_call(session)

            # Connect to Ultravox WS
            async with websockets.connect(
                join_url,
                max_size=None,
                ping_interval=20,
                ping_timeout=20
            ) as ulx:
                print("[ulx] websocket connected.")

                # Buffers for output chunking to Exotel
                ulx_out_buf = bytearray()

                async def exotel_to_ulx():
                    """Pump caller audio from Exotel -> Ultravox."""
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            # Ignore any non-JSON from test clients
                            continue

                        et = msg.get("event")
                        if et == "start":
                            print("[exotel] start")
                            # (Optional) could send config to Ultravox here if needed
                        elif et == "media":
                            try:
                                b64 = msg["media"]["payload"]
                                pcm = base64.b64decode(b64)
                                # send raw PCM bytes to Ultravox
                                await ulx.send(pcm)
                            except Exception as e:
                                print("[err] exotel media decode/send:", repr(e))
                        elif et == "stop":
                            print("[exotel] stop")
                            break

                async def ulx_to_exotel():
                    """Pump agent audio from Ultravox -> Exotel (as base64 media frames)."""
                    nonlocal ulx_out_buf
                    async for m in ulx:
                        if isinstance(m, (bytes, bytearray)):
                            ulx_out_buf.extend(m)
                            # send in ~100 ms blocks (multiples of 320 bytes)
                            for chunk in chunk_bytes(ulx_out_buf, EXOTEL_CHUNK_BYTES):
                                b64 = base64.b64encode(chunk).decode("ascii")
                                frame = {"event": "media", "media": {"payload": b64}}
                                await ws.send(json.dumps(frame))
                        else:
                            # JSON data messages (transcripts, state, etc.) — optional
                            # print("[ulx] data:", m)
                            pass

                await asyncio.gather(exotel_to_ulx(), ulx_to_exotel())

                # Flush any remainder to Exotel (optional)
                if ulx_out_buf:
                    b64 = base64.b64encode(bytes(ulx_out_buf)).decode("ascii")
                    await ws.send(json.dumps({"event": "media", "media": {"payload": b64}}))
                    ulx_out_buf.clear()

    except websockets.ConnectionClosedOK:
        print("[bridge] connection closed cleanly (1000).")
    except websockets.ConnectionClosedError as e:
        print(f"[bridge] ws closed with error: {e}")
    except Exception as e:
        print("[bridge] error:", repr(e))
        traceback.print_exc()
        # Best-effort close so Exotel gets a reason
        try:
            await ws.close(code=1011, reason=str(e)[:120])
        except Exception:
            pass

async def main():
    # Caddy terminates TLS and reverse-proxies to us on localhost:8080
    async with websockets.serve(
        handle_exotel_call,
        host="0.0.0.0",
        port=8080,
        max_size=None,
        ping_interval=20,
        ping_timeout=20
    ):
        print("Bridge listening on :8080")
        await asyncio.Future()  # run forever

if __name__ == "__main__":
    asyncio.run(main())
