#!/usr/bin/env python3
import asyncio, os, json, base64, aiohttp, websockets, traceback

ULX_API = os.environ.get("ULTRAVOX_API_BASE", "https://api.ultravox.ai/api")
ULX_KEY = os.environ.get("ULTRAVOX_API_KEY")

async def start_ulx_call(session):
    payload = {
        "systemPrompt": "You are a friendly agent. Keep answers short.",
        "model": "fixie-ai/ultravox",
        "voice": "Mark",
        "medium": {"serverWebSocket": {"inputSampleRate": 8000, "outputSampleRate": 8000}}
    }
    async with session.post(f"{ULX_API}/calls",
                            headers={"X-API-Key": ULX_KEY, "Content-Type":"application/json"},
                            json=payload) as r:
        text = await r.text()
        if r.status != 200:
            raise RuntimeError(f"Ultravox /calls failed {r.status}: {text}")
        data = json.loads(text)
        return data["joinUrl"]

async def handle_exotel_call(ws, path):
    print("Exotel connected:", path)
    if not ULX_KEY:
        await ws.close(code=1011, reason="ULTRAVOX_API_KEY missing")
        return
    try:
        async with aiohttp.ClientSession() as session:
            join_url = await start_ulx_call(session)
            async with websockets.connect(join_url) as ulx:
                print("Ultravox WS connected.")

                async def exotel_to_ulx():
                    async for raw in ws:
                        msg = json.loads(raw)
                        et = msg.get("event")
                        if et == "media":
                            pcm = base64.b64decode(msg["media"]["payload"])
                            await ulx.send(pcm)
                        elif et == "stop":
                            break

                async def ulx_to_exotel():
                    async for m in ulx:
                        if isinstance(m, (bytes, bytearray)):
                            b64 = base64.b64encode(m).decode("ascii")
                            await ws.send(json.dumps({"event":"media","media":{"payload": b64}}))

                await asyncio.gather(exotel_to_ulx(), ulx_to_exotel())
    except Exception as e:
        print("Bridge error:", repr(e))
        traceback.print_exc()

async def main():
    async with websockets.serve(handle_exotel_call, "0.0.0.0", 8080, max_size=None, ping_interval=20, ping_timeout=20):
        print("Bridge listening on :8080")
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
