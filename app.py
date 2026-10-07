"""Modo nube (Render free): servidor web anti-sleep + bot en segundo plano."""
import os
import threading
import asyncio
import traceback
from datetime import datetime
from flask import Flask, jsonify

app = Flask(__name__)
BOT_STATUS = {"started": datetime.now().isoformat(timespec="seconds"), "rondas": 0, "error": None}


@app.get("/")
def health():
    return "Bot Tickets activo", 200


@app.get("/status")
def status():
    try:
        from pathlib import Path
        import json
        st = {}
        p = Path(__file__).parent / "state_ticket.json"
        if p.exists():
            st = json.loads(p.read_text(encoding="utf-8"))
        return jsonify({"bot": BOT_STATUS, "eventos": len(st)})
    except Exception as e:
        return jsonify({"bot": BOT_STATUS, "error": str(e)})


@app.get("/test")
def test_msg():
    import asyncio
    from flask import request
    from monitor import enviar_a_todos

    msg = request.args.get("msg") or "Prueba bot tickets OK."
    asyncio.run(enviar_a_todos(msg[:500]))
    return "mensaje de prueba enviado a todos", 200


def run_bot():
    import monitor
    print("BOT-THREAD iniciando...", flush=True)
    orig_ronda = monitor.ronda

    async def ronda_wrap():
        await orig_ronda()
        BOT_STATUS["rondas"] += 1
        BOT_STATUS["ultima"] = datetime.now().isoformat(timespec="seconds")

    monitor.ronda = ronda_wrap
    try:
        asyncio.run(monitor.main())
    except Exception:
        BOT_STATUS["error"] = traceback.format_exc()[-2000:]
        print("BOT-THREAD murio:\n" + BOT_STATUS["error"], flush=True)


if __name__ == "__main__":
    print("APP iniciando, lanzo thread del bot...", flush=True)
    threading.Thread(target=run_bot, daemon=True).start()
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
