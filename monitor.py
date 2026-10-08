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


def _bracket(s: str, i: int):
    depth, j, instr, esc = 0, i, False, False
    while j < len(s):
        c = s[j]
        if instr:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                instr = False
        else:
            if c == '"':
                instr = True
            elif c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    return s[i:j + 1]
        j += 1
    return None


def extraer_secciones(html: str) -> dict:
    """Stock REAL por seccion: suma availability de las tarifas con available=true.
    {nombre_norm: {nombre, stock, desde}} — el flag sectionAvailability no se usa (miente)."""
    out: dict = {}
    for m in re.finditer(r'"rates":\[', html):
        rates_txt = _bracket(html, m.end() - 1)
        if rates_txt is None:
            continue
        tail = m.end() - 1 + len(rates_txt)
        if html[tail:tail + 13] != '],"sections":[':
            continue
        secs_txt = _bracket(html, tail + 13)
        if secs_txt is None:
            continue
        nombres = []
        for nm in re.finditer(r'"name":"((?:[^"\\]|\\.)*)"', secs_txt):
            try:
                nombres.append(json.loads('"' + nm.group(1) + '"'))
            except Exception:
                nombres.append(nm.group(1))
        for r in re.finditer(r'"price":(\d+)[^}]*?"availability":(\d+)[^}]*?"available":(true|false)', rates_txt):
            if r.group(3) != "true":
                continue
            cant = int(r.group(2))
            if cant <= 0:
                continue
            precio = int(r.group(1))
            for nombre in nombres:
                key = norm(nombre)
                e = out.get(key, {"nombre": nombre, "stock": 0, "desde": None})
                e["stock"] += cant
                if e["desde"] is None or precio < e["desde"]:
                    e["desde"] = precio
                out[key] = e
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
        if secs or '"rates":' in text:
            return secs, f"HTTP 200 ({len(text)//1000}kb)"
        last = "HTTP 200 sin datos"
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
            cur = secs.get(key, {"nombre": objetivo, "stock": 0, "desde": None})
            ant_stock = prev.get(key, {}).get("stock")
            nombre = cur.get("nombre", objetivo)
            if cur["stock"] > 0:
                print(f"[!] {nombre}: {cur['stock']} entradas desde ${cur['desde']:,}".replace(",", "."), flush=True)
            else:
                print(f"[x] {nombre}: sin stock", flush=True)
            if primera:
                if cur["stock"] > 0:
                    await enviar_a_todos(
                        f"Tickets - {ev['nombre']}\n{nombre}: {cur['stock']} entradas desde ${cur['desde']:,}\n{ev['url']}".replace(",", "."))
                else:
                    await enviar_a_todos(
                        f"Tickets - {ev['nombre']}\n{nombre}: sin stock ahora, avisando cambios.\n{ev['url']}")
            elif ant_stock is not None and cur["stock"] != ant_stock:
                if cur["stock"] > 0 and ant_stock == 0:
                    await enviar_a_todos(
                        f"SE LIBERO - {ev['nombre']}\n{nombre}: {cur['stock']} entradas desde ${cur['desde']:,}\n{ev['url']}".replace(",", "."))
                elif cur["stock"] == 0:
                    await enviar_a_todos(
                        f"AGOTADA - {ev['nombre']}\n{nombre} se agoto.\n{ev['url']}")
                elif cur["stock"] <= 10:
                    await enviar_a_todos(
                        f"POCAS - {ev['nombre']}\n{nombre}: quedan {cur['stock']}!\n{ev['url']}")
            prev[key] = {"nombre": nombre, "stock": cur["stock"], "desde": cur.get("desde")}
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
                            if cur and cur.get("stock", 0) > 0:
                                lineas.append(f"- {cur['nombre']}: {cur['stock']} entradas desde ${cur['desde']:,}".replace(",", "."))
                            elif cur:
                                lineas.append(f"- {cur['nombre']}: sin stock")
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
