"""
Bot local: Picta -> Telegram (videos completos, hasta 2 GB)

Instalación (en una terminal, con el entorno virtual activado):
    pip install selenium telethon hachoir yt-dlp picta-dl
Además necesitas Google Chrome y ffmpeg instalados.

Pasos:
 1. Rellena la sección CONFIG.
 2. Primera prueba: DIAGNOSTICO = True  -> solo muestra los enlaces que ve.
 3. Cuando los enlaces sean de videos, pon DIAGNOSTICO = False.
"""
import asyncio
import glob
import hashlib
import json
import os
import sqlite3
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession

# ───────────────────────── CONFIG ─────────────────────────
# En local puedes escribir los valores aquí; en Render se leen de las
# variables de entorno (así no subes secretos a GitHub).
def env(nombre, defecto=""):
    return os.environ.get(nombre, defecto)


API_ID = int(env("API_ID", "0"))          # de https://my.telegram.org
API_HASH = env("API_HASH", "TU_API_HASH")
BOT_TOKEN = env("BOT_TOKEN", "TOKEN_DE_BOTFATHER")
SESSION = env("SESSION")                   # sesión guardada de Telegram (se genera sola la 1ª vez)
CANAL = env("CANAL", "@tu_canal").strip()  # @usuario del canal; el bot debe ser admin
if CANAL.lstrip("-").isdigit():
    CANAL = int(CANAL)                     # si pones el id numérico

SECCION_URL = env("SECCION_URL", "https://www.picta.cu/")
FRAGMENTO = env("FRAGMENTO", "/medias/")          # parte común de la URL de un video
DIAGNOSTICO = env("DIAGNOSTICO", "true").lower() == "true"
MAX_POR_CICLO = int(env("MAX_POR_CICLO", "3"))
INTERVALO_MIN = int(env("INTERVALO_MIN", "30"))
MARCAR_INICIAL = env("MARCAR_INICIAL", "true").lower() == "true"
CARPETA = env("CARPETA", "descargas")
DB_PATH = env("DB_PATH", "vistos.db")     # en Render: /data/vistos.db (con disco)
SCROLL = env("SCROLL", "false").lower() == "true"
PICTA_USER = env("PICTA_USER")            # tu usuario/correo/teléfono de Picta
PICTA_PASS = env("PICTA_PASS")            # tu contraseña de Picta
REFERER = "https://www.picta.cu/"
CALIDAD = int(env("CALIDAD", "240"))      # altura máxima en px (240, 360, 480...); 0 = mejor calidad
LIMITE_MB = 1990                          # límite de subida de Telegram (~2 GB)
# ──────────────────────────────────────────────────────────

os.makedirs(CARPETA, exist_ok=True)
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("CREATE TABLE IF NOT EXISTS vistos (url TEXT PRIMARY KEY)")
db.commit()


def ya_visto(url):
    return db.execute("SELECT 1 FROM vistos WHERE url=?", (url,)).fetchone() is not None


def marcar(url):
    db.execute("INSERT OR IGNORE INTO vistos VALUES (?)", (url,))
    db.commit()


def nuevo_driver(con_red=False):
    opts = webdriver.ChromeOptions()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    # Ahorro de memoria (Render gratis tiene ~512 MB)
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-background-networking")
    opts.add_argument("--renderer-process-limit=1")
    opts.add_argument("--js-flags=--max-old-space-size=256")
    opts.add_experimental_option(
        "prefs", {"profile.managed_default_content_settings.images": 2})
    opts.add_argument("--mute-audio")
    opts.add_argument("--window-size=1280,900")
    if con_red:  # el registro de red solo se necesita para buscar el manifest
        opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    if env("CHROME_BIN"):
        opts.binary_location = env("CHROME_BIN")
    if env("CHROMEDRIVER"):
        from selenium.webdriver.chrome.service import Service
        return webdriver.Chrome(service=Service(env("CHROMEDRIVER")), options=opts)
    return webdriver.Chrome(options=opts)


def iniciar_sesion(driver):
    """Picta redirige a /usuario/acceder si no hay sesión: entra con tu cuenta."""
    for _ in range(5):  # Angular puede tardar en redirigir a la pantalla de acceso
        if "acceder" in driver.current_url:
            break
        time.sleep(2)
    else:
        return
    if not (PICTA_USER and PICTA_PASS):
        print("[!] Picta pide iniciar sesión: define PICTA_USER y PICTA_PASS")
        return
    print("Picta pide iniciar sesión, entrando con tu cuenta...")
    usuario = clave = None
    for _ in range(15):  # espera hasta ~30 s a que se pinte el formulario
        campos = [e for e in driver.find_elements(By.CSS_SELECTOR, "input") if e.is_displayed()]
        usuario = next((e for e in campos if e.get_attribute("type") in ("text", "email", "tel")), None)
        clave = next((e for e in campos if e.get_attribute("type") == "password"), None)
        if usuario and clave:
            break
        time.sleep(2)
    if not usuario or not clave:
        print("[!] No encontré los campos de usuario/contraseña")
        return
    usuario.send_keys(PICTA_USER)
    clave.send_keys(PICTA_PASS)
    botones = driver.find_elements(
        By.XPATH,
        "//button[contains(translate(normalize-space(.),"
        "'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'INICIAR SES')]")
    botones = [b for b in botones if b.is_displayed()]
    if botones:
        botones[0].click()
    else:
        clave.send_keys(Keys.ENTER)
    for _ in range(15):
        time.sleep(2)
        if "acceder" not in driver.current_url:
            print("Sesión iniciada ✔")
            return
    print("[!] Sigo en la página de acceso: revisa usuario y contraseña")


def enlaces_de_la_pagina(driver):
    driver.get(SECCION_URL)
    time.sleep(5)
    iniciar_sesion(driver)
    print("Cargando la lista de la sección...", flush=True)
    # Picta es Angular: el menú sale enseguida pero las películas llegan después.
    # Con FRAGMENTO definido se espera (hasta ~40 s) a que aparezcan enlaces de video.
    hrefs = []
    for _ in range(20):
        time.sleep(2)
        hrefs = driver.execute_script(
            "return Array.from(document.querySelectorAll('a')).map(a => a.href)")
        if FRAGMENTO:
            if any(FRAGMENTO in h for h in hrefs):
                break
        elif len(hrefs) > 10:
            break
    print(f"Lista cargada: {len(hrefs)} enlaces <a>", flush=True)

    if SCROLL:  # el scroll carga más tarjetas pero gasta más memoria
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(3)
        hrefs = driver.execute_script(
            "return Array.from(document.querySelectorAll('a')).map(a => a.href)")

    if DIAGNOSTICO:
        print("Título de la página:", driver.title, flush=True)
        print("URL final:", driver.current_url, flush=True)
        rutas = driver.execute_script(
            "return Array.from(document.querySelectorAll('[ng-reflect-router-link]'))"
            ".map(e => e.getAttribute('ng-reflect-router-link')).slice(0, 15)")
        print("Rutas Angular (routerLink):", rutas, flush=True)
        # Estructura de las tarjetas: sube desde las primeras imágenes de la página
        muestra = driver.execute_script("""
            const imgs = Array.from(document.querySelectorAll('img'))
                .filter(i => i.src && !i.src.startsWith('data:'));
            const res = {total_img: imgs.length, tarjetas: []};
            imgs.slice(0, 3).forEach(i => {
                let el = i, cadena = [];
                for (let k = 0; k < 5 && el.parentElement; k++) {
                    el = el.parentElement;
                    const attrs = Array.from(el.attributes)
                        .filter(a => a.name !== 'style')
                        .map(a => a.name + '=' + a.value.slice(0, 50)).join(' ');
                    cadena.push(el.tagName.toLowerCase() + ' ' + attrs);
                }
                res.tarjetas.push({src: i.src.slice(-60), cadena: cadena});
            });
            return res;
        """)
        print("Imágenes en la página:", muestra["total_img"], flush=True)
        for t in muestra["tarjetas"]:
            print("  Tarjeta:", t["src"], flush=True)
            for nivel in t["cadena"]:
                print("     ^", nivel[:200], flush=True)

    vistos, salida = set(), []
    for href in hrefs:
        if not href or href in vistos or "picta.cu" not in href:
            continue
        vistos.add(href)
        if not FRAGMENTO or FRAGMENTO in href:
            salida.append(href)
    return salida


def buscar_manifest(driver, url):
    """Abre la página del video y captura TODOS los manifest que pide el reproductor
    (si hay anuncio previo, puede haber uno para el anuncio y otro para la película)."""
    driver.get_log("performance")  # vacía el registro previo
    driver.get(url)
    encontrados, quietos = [], 0
    for _ in range(15):
        time.sleep(2)
        try:
            driver.execute_script("document.querySelector('video') && document.querySelector('video').play()")
        except Exception:
            pass
        nuevos = 0
        for entrada in driver.get_log("performance"):
            try:
                msg = json.loads(entrada["message"])["message"]
            except Exception:
                continue
            if msg.get("method") == "Network.requestWillBeSent":
                u = msg["params"]["request"]["url"]
                if ("manifest.mpd" in u or ".m3u8" in u) and u not in encontrados:
                    encontrados.append(u)
                    nuevos += 1
        if encontrados and nuevos == 0:
            quietos += 1
            if quietos >= 4:  # ~8 s sin manifest nuevo: ya pasó el anuncio
                break
        else:
            quietos = 0
    return encontrados, driver.title


def duracion(manifest):
    r = correr(["yt-dlp", "--referer", REFERER, "--skip-download", "--no-warnings",
                "--print", "duration", manifest])
    try:
        return float(r.stdout.strip().splitlines()[-1])
    except Exception:
        return 0.0


def elegir_manifest(candidatos):
    """Si hay varios (anuncio + película), se queda con el más largo."""
    if len(candidatos) == 1:
        return candidatos[0]
    puntuados = [(duracion(m), i, m) for i, m in enumerate(candidatos)]
    print("Manifests vistos (duración, url):", [(d, m[-60:]) for d, _, m in puntuados], flush=True)
    puntuados.sort(key=lambda x: (x[0], x[1]))  # empate: el último (la película va después del anuncio)
    return puntuados[-1][2]


def buscar_nuevos():
    """Devuelve lista de dicts {url, manifest, titulo} de videos aún no publicados."""
    # 1) Chrome ligero solo para leer la lista; se cierra para liberar memoria
    driver = nuevo_driver()
    try:
        enlaces = enlaces_de_la_pagina(driver)
    finally:
        driver.quit()
    print(f"Enlaces encontrados: {len(enlaces)}", flush=True)

    if DIAGNOSTICO:
        for e in enlaces:
            print("  ", e)
        return []

    if MARCAR_INICIAL and db.execute("SELECT COUNT(*) FROM vistos").fetchone()[0] == 0:
        for e in enlaces:
            marcar(e)
        print("Primera ejecución: enlaces actuales marcados como vistos.")
        return []

    # 2) Un Chrome nuevo por cada video (con registro de red), cerrado al terminar
    resultado = []
    for url in [e for e in enlaces if not ya_visto(e)][:MAX_POR_CICLO]:
        print("Buscando el video en:", url, flush=True)
        driver = nuevo_driver(con_red=True)
        try:
            candidatos, tit = buscar_manifest(driver, url)
        finally:
            driver.quit()
        if candidatos:
            manifest = elegir_manifest(candidatos)
            resultado.append({"url": url, "manifest": manifest, "titulo": tit})
        else:
            print(f"[!] Sin manifest en {url} (¿no es una página de video?)")
            marcar(url)  # evita reintentar páginas que no son videos
    return resultado


def correr(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def descargar(video):
    nombre = hashlib.md5(video["url"].encode()).hexdigest()
    plantilla = os.path.join(CARPETA, nombre + ".%(ext)s")
    if CALIDAD > 0:
        # Mejor calidad que no pase de CALIDAD px; si no existe, la más baja disponible
        fmt = (f"bv*[height<={CALIDAD}]+ba/b[height<={CALIDAD}]/wv*+ba/w")
        fmt_picta = f"best[height<={CALIDAD}]/worst"
    else:
        fmt, fmt_picta = "bv*+ba/b", "best"
    intentos = [
        ["yt-dlp", "--referer", REFERER, "-f", fmt,
         "--merge-output-format", "mp4", "-o", plantilla, video["manifest"]],
        ["picta-dl", "-f", fmt_picta, "-o", plantilla, video["url"]],
    ]
    for cmd in intentos:
        r = correr(cmd)
        archivos = [f for f in glob.glob(os.path.join(CARPETA, nombre + ".*"))
                    if not f.endswith((".part", ".ytdl"))]
        if r.returncode == 0 and archivos:
            return archivos[0]
        print(f"[!] {cmd[0]} falló: {r.stderr[-300:]}")
    return None


async def ciclo(client):
    nuevos = await asyncio.to_thread(buscar_nuevos)
    for v in nuevos:
        print("Descargando:", v["titulo"], "|", v["manifest"])
        ruta = await asyncio.to_thread(descargar, v)
        if not ruta:
            continue  # no se marca: se reintenta en el próximo ciclo
        try:
            if os.path.getsize(ruta) > LIMITE_MB * 1024 * 1024:
                print("[!] Video demasiado grande para Telegram, se omite.")
                marcar(v["url"])
                continue
            pie = f"{v['titulo']}\n{v['url']}".strip()[:1000]
            await client.send_file(CANAL, ruta, caption=pie, supports_streaming=True)
            marcar(v["url"])
            print("Publicado ✔")
        except Exception as e:
            print("[!] Error subiendo:", e)
        finally:
            if os.path.exists(ruta):
                os.remove(ruta)


class Ping(BaseHTTPRequestHandler):
    """Servidor mínimo: Render (Web Service) exige que la app escuche en un puerto."""
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    do_HEAD = do_GET

    def log_message(self, *args):
        pass


def servidor_web():
    puerto = int(env("PORT", "0"))
    if puerto:
        hilo = threading.Thread(
            target=lambda: HTTPServer(("0.0.0.0", puerto), Ping).serve_forever(),
            daemon=True)
        hilo.start()
        print(f"Servidor de salud escuchando en el puerto {puerto}")


async def iniciar_telegram():
    """Inicia sesión del bot; si Telegram pide esperar (FloodWait), espera y reintenta."""
    while True:
        client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
        try:
            await client.start(bot_token=BOT_TOKEN)
            print("Telegram: bot conectado ✔", flush=True)
            if not SESSION:
                # Cópiala y guárdala en Render como variable SESSION
                print("SESSION=" + client.session.save(), flush=True)
            return client
        except FloodWaitError as e:
            print(f"[!] Telegram pide esperar {e.seconds} s para iniciar el bot. "
                  "Espero y reintento (no reinicies el servicio).", flush=True)
            await client.disconnect()
            await asyncio.sleep(e.seconds + 10)


async def main():
    servidor_web()
    if DIAGNOSTICO:
        client = None  # en diagnóstico no se usa Telegram (evita límites por reinicios)
        print("Modo diagnóstico: Telegram desactivado.", flush=True)
    else:
        client = await iniciar_telegram()
    while True:
        try:
            await ciclo(client)
        except Exception as e:
            print("[!] Error en el ciclo:", e)
        if DIAGNOSTICO and not env("PORT"):
            break
        await asyncio.sleep(INTERVALO_MIN * 60)


if __name__ == "__main__":
    asyncio.run(main())
