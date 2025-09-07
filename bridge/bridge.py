#!/usr/bin/env python3
import asyncio, json, base64, os, websockets, aiohttp

# ---- Ultravox config (from Step 1) ----
ULTRAVOX_WS_URL = os.environ.get("ULTRAVOX_WS_URL")  # e.g., from Ultravox docs/dashboard
ULTRAVOX_API_KEY = os.environ.get("ULTRAVOX_API_KEY")  # X-API-Key value

# Exotel sends/expects 16-bit PCM, mono, 8 kHz frames in base64 (bidirectional stream).
# We'll maintain one Ultravox WS per call, relaying media both ways.

async def ulx_connect():
    headers = [("X-API-Key", ULTRAVOX_API_KEY)]
    # Ultravox WS URL is provided in their Realtime/WebSocket docs/console
    # (You can also start sessions via REST and upgrade to WS.)
    return await websockets.connect(ULTRAVOX_WS_URL, extra_headers=headers)

async def handle_exotel_call(ws, path):
    # path is expected to be "/exotel"
    print("Exotel connected:", path)
    # 1) Open Ultravox WS session
    ulx = await ulx_connect()
    print("Ultravox WS connected.")

    async def pump_ulx_to_exotel():
        # Read audio chunks from Ultravox → send to Exotel as 'media' events
        async for msg in ulx:
            # Ultravox WS will send data messages including audio frames;
            # exact schema is in Ultravox WebSocket docs.
            # Expect something like { "type": "audio_out", "pcm16": <raw bytes> } or similar.
            try:
                if isinstance(msg, (bytes, bytearray)):
                    # If Ultravox sends raw PCM frames, base64-encode for Exotel
                    b64 = base64.b64encode(msg).decode("ascii")
                    await ws.send(json.dumps({"event": "media", "media": {"payload": b64}}))
                else:
                    data = json.loads(msg)
                    if data.get("type") == "audio_out" and "pcm16" in data:
                        b64 = base64.b64encode(bytes(data["pcm16"])).decode("ascii")
                        await ws.send(json.dumps({"event": "media", "media": {"payload": b64}}))
                    # Ignore non-audio messages (transcripts, etc.) for now
            except Exception as e:
                print("ulx->exotel error:", e)
                break

    async def pump_exotel_to_ulx():
        # Read Exotel frames → forward audio to Ultravox
        async for raw in ws:
            try:
                msg = json.loads(raw)
                et = msg.get("event")
                if et == "start":
                    print("Call start:", msg.get("start", {}))
                    # Optionally tell Ultravox sample rate/channels via a config message if required by your Ultravox WS
                    # await ulx.send(json.dumps({"type":"config","sample_rate":8000,"num_channels":1}))
                elif et == "media":
                    payload_b64 = msg["media"]["payload"]
                    pcm = base64.b64decode(payload_b64)
                    # Send to Ultravox as audio-in frame; check Ultravox WS “data messages” schema
                    await ulx.send(pcm)  # if Ultravox accepts raw PCM frames
                    # Or: await ulx.send(json.dumps({"type":"audio_in","pcm16": list(pcm)}))
                elif et == "stop":
                    print("Call stop")
                    break
            except Exception as e:
                print("exotel->ulx error:", e)
                break

    # Run pumps concurrently
    await asyncio.gather(pump_exotel_to_ulx(), pump_ulx_to_exotel())

    try:
        await ulx.close()
    except:
        pass

async def main():
    # WS server on 0.0.0.0:8080 (Caddy will terminate TLS and proxy WSS here)
    async with websockets.serve(handle_exotel_call, "0.0.0.0", 8080, max_size=None):
        print("Bridge listening on :8080")
        await asyncio.Future()  # run forever

if __name__ == "__main__":
    asyncio.run(main())
