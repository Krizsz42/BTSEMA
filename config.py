# Bot Tickets Chile (Ticketmaster)
# Vigila secciones de un evento y avisa a Telegram cuando se liberan.

# --- Cada cuántos segundos revisar (lento: Ticketmaster desafia si se golpea seguido) ---
INTERVALO_SEGUNDOS = 300

# --- Eventos y secciones a vigilar (nombres sin tilde sirven igual) ---
EVENTOS = [
    {
        "nombre": "BTS World Tour Arirang - SCL",
        "url": "https://www.ticketmaster.cl/event/bts-world-tour-arirang-live-2026-scl-venta-general-16-de-octubre",
        "secciones": ["Galeria Sur"],
    },
]
