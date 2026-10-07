"""Bot Tickets: vigila secciones Ticketmaster y avisa por Telegram."""
import asyncio
import json
import os
import random
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv

import config

load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
STATE_FILE = Path(__file__).parent / "state_ticket.json"
SUBS_FILE = Path(__file__).parent / "subscribers.json"


def norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s.lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def extraer_secciones(html: str) -> dict:
    """{nombre_norm: {nombre, disponible, pocas}} uniendo duplicados (disponible si alguna dice true)."""
    out: dict = {}
    for m in re.finditer(
        r'\{"id":\d+,"name":"((?:[^"\\]|\\.)*)","uuid":"[^"]*","available":(true|false),"lowAvailability":(true|false)\}',
        html,
    ):
        try:
            nombre = json.loads('"' + m.group(1) + '"')
        except Exception:
            nombre = m.group(1)
        key = norm(nombre)
        disp = m.group(2) == "true"
        low = m.group(3) == "true"
        if key in out:
            out[key]["disponible"] = out[key]["disponible"] or disp
            out[key]["pocas"] = out[key]["pocas"] or low
        else:
            out[key] = {"nombre": nombre, "disponible": disp, "pocas": low}
    return out


async def fetch_event(url: str) -> tuple[dict | None, str]:
    def _get(imp: str):
        from curl_cffi import requests as creq
        r = creq.get(url, impersonate=imp, timeout=25,  # type: ignore
                     headers={"Accept-Language": "es-CL,es;q=0.9"})
        return r.status_code, r.text
    last = "?"
    for imp in ("chrome", "safari15_5", "chrome120"):
        try:
            status, text = await asyncio.to_thread(_get, imp)
        except Exception:
            last = f"err-{imp}"
            continue
        last = f"HTTP {status}"
        if status != 200:
            await asyncio.sleep(1)
            continue
        secs = extraer_secciones(text)
        if secs:
            return secs, f"HTTP 200 ({len(text)//1000}kb)"
        last = "HTTP 200 sin secciones"
    return None, last


def cargar_estado() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def guardar_estado(e: dict):
    STATE_FILE.write_text(json.dumps(e, indent=2, ensure_ascii=False), encoding="utf-8")


def cargar_subs() -> set:
    subs = set()
    if CHAT_ID:
        subs.add(str(CHAT_ID))
    if SUBS_FILE.exists():
        try:
            data = json.loads(SUBS_FILE.read_text(encoding="utf-8"))
            items = data if isinstance(data, list) else list(data.keys())
            for x in items:
                subs.add(str(x))
        except Exception:
            pass
    return subs


def agregar_sub(chat: str):
    try:
        subs = cargar_subs()
        if str(chat) not in subs:
            subs.add(str(chat))
            SUBS_FILE.write_text(json.dumps(sorted(subs), indent=2), encoding="utf-8")
    except Exception:
        pass


async def enviar_telegram(mensaje: str, chat_id: str | None = None):
    if not BOT_TOKEN:
        print(f"  [TG] {mensaje[:200]}", flush=True)
        return
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            await c.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id or CHAT_ID, "text": mensaje, "disable_web_page_preview": False},
            )
            print("  [TG] enviado OK", flush=True)
    except Exception as e:
        print(f"  [TG] error: {e}", flush=True)


async def enviar_a_todos(mensaje: str):
    for chat in sorted(cargar_subs()):
        await enviar_telegram(mensaje, chat_id=chat)


async def ronda():
    estado = cargar_estado()
    print(f"\n=== Ronda tickets {datetime.now().strftime('%H:%M:%S')} ===", flush=True)
    for ev in config.EVENTOS:
        secs, detalle = await fetch_event(ev["url"])
        if secs is None:
            print(f"? {ev['nombre']}: sin lectura ({detalle})", flush=True)
            await asyncio.sleep(2)
            continue
        prev = estado.get(ev["url"], {})
        primera = not prev
        for objetivo in ev["secciones"]:
            key = norm(objetivo)
            cur = secs.get(key)
            if cur is None:
                print(f"? seccion no encontrada: {objetivo}", flush=True)
                continue
            ant = prev.get(key, {})
            est = "DISPONIBLE" if cur["disponible"] else "agotada"
            print(f"[{'!' if cur['disponible'] else 'x'}] {cur['nombre']}: {est}" +
                  (" (pocas!)" if cur["pocas"] else ""), flush=True)
            if primera:
                await enviar_a_todos(
                    f"Tickets - {ev['nombre']}\n{cur['nombre']}: {est}\n{ev['url']}")
            else:
                if cur["disponible"] and not ant.get("disponible"):
                    await enviar_a_todos(
                        f"SE LIBERO - {ev['nombre']}\n{cur['nombre']} disponible!\n{ev['url']}")
                elif not cur["disponible"] and ant.get("disponible"):
                    await enviar_a_todos(
                        f"AGOTADA - {ev['nombre']}\n{cur['nombre']} se agoto.\n{ev['url']}")
                elif cur["pocas"] and not ant.get("pocas"):
                    await enviar_a_todos(
                        f"POCAS - {ev['nombre']}\n{cur['nombre']}: quedan pocas!\n{ev['url']}")
            prev[key] = cur
        estado[ev["url"]] = prev
        await asyncio.sleep(random.uniform(1, 2))
    guardar_estado(estado)


async def telegram_poll_loop():
    import os as _os
    instancia = _os.getenv("INSTANCIA", "nube" if _os.getenv("PORT") else "pc")
    if not BOT_TOKEN:
        return
    offset = 0
    while True:
        try:
            async with httpx.AsyncClient(timeout=40) as c:
                r = await c.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                                params={"offset": offset, "timeout": 25})
                data = r.json() if r.status_code == 200 else {}
            for up in data.get("result", []):
                offset = up["update_id"] + 1
                msg = up.get("message") or {}
                chat = str(msg.get("chat", {}).get("id", ""))
                if not chat:
                    continue
                agregar_sub(chat)
                texto = (msg.get("text") or "").strip().lower()
                if texto in ("test", "/test", "/start", "start", "estado", "/estado"):
                    estado = cargar_estado()
                    lineas = [f"Bot tickets OK ({instancia})."]
                    for ev in config.EVENTOS:
                        for s in ev["secciones"]:
                            cur = estado.get(ev["url"], {}).get(norm(s))
                            if cur:
                                lineas.append(f"- {cur['nombre']}: {'DISPONIBLE' if cur['disponible'] else 'agotada'}")
                            else:
                                lineas.append(f"- {s}: sin lectura aun")
                    await enviar_telegram("\n".join(lineas), chat_id=chat)
        except Exception as e:
            print(f"  [poll] error: {e}", flush=True)
            await asyncio.sleep(10)
        await asyncio.sleep(1)


async def main():
    print("Bot Tickets iniciado", flush=True)
    asyncio.create_task(telegram_poll_loop())
    while True:
        try:
            await ronda()
        except KeyboardInterrupt:
            sys.exit(0)
        except Exception as e:
            print(f"Error en ronda: {e}", flush=True)
        espera = config.INTERVALO_SEGUNDOS * random.uniform(0.8, 1.2)
        print(f"proxima revision en {int(espera)}s...", flush=True)
        await asyncio.sleep(espera)


if __name__ == "__main__":
    asyncio.run(main())
