#!/usr/bin/env python3
import asyncio, os, json, base64, aiohttp, websockets, traceback

# Prefer env on every call; never allow None
def get_ulx_base():
    return os.environ.get("ULTRAVOX_API_BASE") or "https://api.ultravox.ai/api"

ULX_KEY = os.environ.get("ULTRAVOX_API_KEY")
MODEL = os.environ.get("ULTRAVOX_MODEL", "fixie-ai/ultravox")
VOICE = os.environ.get("ULTRAVOX_VOICE", "Mark")
SYSTEM_PROMPT = os.environ.get("ULTRAVOX_SYSTEM_PROMPT", "You are a friendly agent. Keep answers short.")

IN_SAMPLE_RATE = int(os.environ.get("IN_SAMPLE_RATE", "8000"))
OUT_SAMPLE_RATE = int(os.environ.get("OUT_SAMPLE_RATE", "8000"))

EXOTEL_CHUNK_BYTES = 1600  # ~100 ms @ 8 kHz s16le mono (multiples of 320 bytes)

async def start_ulx_call(session: aiohttp.ClientSession) -> str:
    base = get_ulx_base()
    print(f"[boot] ULX_API resolved to: {base}")
    payload = {
        "systemPrompt": SYSTEM_PROMPT,
        "model": MODEL,
        "voice": VOICE,
        "medium": {"serverWebSocket": {"inputSampleRate": IN_SAMPLE_RATE, "outputSampleRate": OUT_SAMPLE_RATE}}
    }
    url = f"{base}/calls"
    async with session.post(
        url,
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
            raise RuntimeError(f"Ultravox /calls OK but missing joinUrl: {text[:200]}")
        print(f"[ulx] call created {r.status}; joinUrl ready.")
        return join

async def handle_exotel_call(ws, path):
    print(f"[exotel] connected: {path}")
    if not ULX_KEY:
        print("[err] ULTRAVOX_API_KEY missing")
        await ws.close(code=1011, reason="ULTRAVOX_API_KEY missing")
        return
    try:
        async with aiohttp.ClientSession() as session:
            join_url = await start_ulx_call(session)
            async with websockets.connect(join_url, max_size=None, ping_interval=20, ping_timeout=20) as ulx:
                print("[ulx] websocket connected.")
                ulx_out_buf = bytearray()

                async def exotel_to_ulx():
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        et = msg.get("event")
                        if et == "media":
                            try:
                                pcm = base64.b64decode(msg["media"]["payload"])
                                await ulx.send(pcm)
                            except Exception as e:
                                print("[err] exotel media decode/send:", repr(e))
                        elif et == "stop":
                            break

                def chunk_bytes(buf: bytearray, size: int):
                    mv = memoryview(buf)
                    n = (len(buf) // size) * size
                    i = 0
                    while i < n:
                        yield mv[i:i+size].tobytes()
                        i += size
                    if n:
                        del buf[:n]

                async def ulx_to_exotel():
                    nonlocal ulx_out_buf
                    async for m in ulx:
                        if isinstance(m, (bytes, bytearray)):
                            ulx_out_buf.extend(m)
                            for chunk in chunk_bytes(ulx_out_buf, EXOTEL_CHUNK_BYTES):
                                b64 = base64.b64encode(chunk).decode("ascii")
                                await ws.send(json.dumps({"event": "media", "media": {"payload": b64}}))
                        else:
                            pass

                await asyncio.gather(exotel_to_ulx(), ulx_to_exotel())

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
        try:
            await ws.close(code=1011, reason=str(e)[:120])
        except Exception:
            pass

async def main():
    async with websockets.serve(
        handle_exotel_call, host="0.0.0.0", port=8080, max_size=None, ping_interval=20, ping_timeout=20
    ):
        print("Bridge listening on :8080")
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
