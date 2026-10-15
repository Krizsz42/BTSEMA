"""Bot Tickets: vigila sectores Ticketmaster con navegador real y avisa por Telegram.

Ticketmaster bloquea con desafio de identidad si se le golpea muy seguido:
- intervalo lento (5 min), perfil persistente, backoff si sale el desafio.
- pensado para correr en PC (el headless de datacenter lo bloquean al tiro).
"""
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
PROFILE_DIR = Path(__file__).parent / "brave_profile"
BRAVE_EXE = r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"


def norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s.lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def leer_lista(fuente: str) -> dict:
    """Sectores comprables: {norm: {nombre, precio}} desde el TEXTO visible
    ('Cancha Pacifico Desde $ 250.000 + $ 43.750 Sin numerar'). Si recibe HTML, pela tags."""
    if "<" in fuente and ">" in fuente:
        fuente = re.sub(r"<script.*?</script>", " ", fuente, flags=re.S)
        fuente = re.sub(r"<style.*?</style>", " ", fuente, flags=re.S)
        fuente = re.sub(r"<[^>]+>", " ", fuente)
    texto = re.sub(r"\s+", " ", fuente)
    out: dict = {}
    for m in re.finditer(
        r'([A-Za-zÁÉÍÓÚÜÑáéíóúüñ][A-Za-zÁÉÍÓÚÜÑáéíóúüñ 0-9\-/]{2,60}?)\s*Desde\s*\$\s*(\d{1,3}(?:\.\d{3})+)',
        texto,
    ):
        nombre = m.group(1).strip()
        if len(nombre) < 3 or "sector" in norm(nombre):
            continue
        precio = int(m.group(2).replace(".", ""))
        if not 10000 <= precio <= 5000000:
            continue
        key = norm(nombre)
        if key not in out or precio < out[key]["precio"]:
            out[key] = {"nombre": nombre, "precio": precio}
    return out


async def fetch_navegador(url: str) -> tuple[dict | None, str]:
    """Retorna (sectores, detalle). Sectores {} = pagina cargo pero nada comprable."""
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        launch = {"headless": True, "args": ["--disable-blink-features=AutomationControlled"]}
        if Path(BRAVE_EXE).exists():
            launch["executable_path"] = BRAVE_EXE
        else:
            launch["channel"] = "chrome"
        browser = await p.chromium.launch_persistent_context(
            str(PROFILE_DIR), locale="es-CL", viewport={"width": 1366, "height": 900}, **launch)
        page = await browser.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            try:
                await page.get_by_text("Seleccionar sector").wait_for(timeout=40000)
            except Exception:
                title = await page.title()
                if "Identity Verified" in title or "verify" in title.lower():
                    return None, "desafio-identidad"
                # reintento una vez: si la pagina cargo pero no hay lista, es lista vacia real
                await page.wait_for_timeout(15000)
                try:
                    await page.get_by_text("Seleccionar sector").wait_for(timeout=20000)
                except Exception:
                    return {}, "lista-vacia"
            await page.wait_for_timeout(3000)
            texto = await page.evaluate("document.body ? document.body.innerText : ''")
            return leer_lista(texto or ""), "navegador (texto visible)"
        finally:
            await browser.close()


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
            for x in (data if isinstance(data, list) else list(data.keys())):
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
        try:
            lista, detalle = await fetch_navegador(ev["url"])
        except Exception as e:
            print(f"? {ev['nombre']}: error navegador {str(e)[:120]}", flush=True)
            await asyncio.sleep(5)
            continue
        if lista is None:  # desafio o sin lista: backoff, no tocar estado
            print(f"! {ev['nombre']}: {detalle} -> espero mas", flush=True)
            estado["_bloqueo"] = estado.get("_bloqueo", 0) + 1
            guardar_estado(estado)
            return
        estado["_bloqueo"] = 0
        print(f"  comprables: {len(lista)} ({detalle})", flush=True)
        prev = estado.get(ev["url"], {})
        primera = not prev
        vistos = set()
        for objetivo in ev["secciones"]:
            key = norm(objetivo)
            cur = lista.get(key)
            vistos.add(key)
            ant = prev.get(key)
            if cur is None:
                print(f"[x] {objetivo}: no comprable ahora", flush=True)
                if not primera and ant and ant.get("comprable"):
                    await enviar_a_todos(f"AGOTADA - {ev['nombre']}\n{objetivo} salio de la lista.\n{ev['url']}")
                prev[key] = {"nombre": objetivo, "comprable": False, "precio": None}
                continue
            print(f"[!] {cur['nombre']}: COMPRABLE desde ${cur['precio']:,}".replace(",", "."), flush=True)
            if primera or not (ant and ant.get("comprable")):
                await enviar_a_todos(
                    f"{'Tickets' if primera else 'SE LIBERO'} - {ev['nombre']}\n{cur['nombre']}: comprable desde ${cur['precio']:,}\n{ev['url']}".replace(",", "."))
            elif ant.get("precio") != cur["precio"]:
                await enviar_a_todos(
                    f"CAMBIO PRECIO - {ev['nombre']}\n{cur['nombre']}: ${cur['precio']:,}\n{ev['url']}".replace(",", "."))
            prev[key] = {"nombre": cur["nombre"], "comprable": True, "precio": cur["precio"]}
        estado[ev["url"]] = prev
        await asyncio.sleep(random.uniform(3, 6))
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
                            if cur and cur.get("comprable"):
                                lineas.append(f"- {cur['nombre']}: COMPRABLE ${cur['precio']:,}".replace(",", "."))
                            elif cur:
                                lineas.append(f"- {s}: no comprable ahora")
                            else:
                                lineas.append(f"- {s}: sin lectura aun")
                    await enviar_telegram("\n".join(lineas), chat_id=chat)
        except Exception as e:
            print(f"  [poll] error: {e}", flush=True)
            await asyncio.sleep(10)
        await asyncio.sleep(1)


async def main():
    print("Bot Tickets iniciado (modo navegador)", flush=True)
    asyncio.create_task(telegram_poll_loop())
    while True:
        try:
            await ronda()
        except KeyboardInterrupt:
            sys.exit(0)
        except Exception as e:
            print(f"Error en ronda: {e}", flush=True)
        bloq = 1
        try:
            bloq = cargar_estado().get("_bloqueo", 0)
        except Exception:
            pass
        base = config.INTERVALO_SEGUNDOS * (6 if bloq else 1)  # backoff x6 si hay desafio
        espera = base * random.uniform(0.8, 1.2)
        print(f"proxima revision en {int(espera)}s...", flush=True)
        await asyncio.sleep(espera)


if __name__ == "__main__":
    asyncio.run(main())
