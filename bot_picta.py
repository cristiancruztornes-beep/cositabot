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
from telethon import TelegramClient

# ───────────────────────── CONFIG ─────────────────────────
# En local puedes escribir los valores aquí; en Render se leen de las
# variables de entorno (así no subes secretos a GitHub).
def env(nombre, defecto=""):
    return os.environ.get(nombre, defecto)


API_ID = int(env("API_ID", "0"))          # de https://my.telegram.org
API_HASH = env("API_HASH", "TU_API_HASH")
BOT_TOKEN = env("BOT_TOKEN", "TOKEN_DE_BOTFATHER")
CANAL = env("CANAL", "@tu_canal").strip()  # @usuario del canal; el bot debe ser admin
if CANAL.lstrip("-").isdigit():
    CANAL = int(CANAL)                     # si pones el id numérico

SECCION_URL = env("SECCION_URL", "https://www.picta.cu/")
FRAGMENTO = env("FRAGMENTO", "")          # parte común de la URL de un video
DIAGNOSTICO = env("DIAGNOSTICO", "true").lower() == "true"
MAX_POR_CICLO = int(env("MAX_POR_CICLO", "3"))
INTERVALO_MIN = int(env("INTERVALO_MIN", "30"))
MARCAR_INICIAL = env("MARCAR_INICIAL", "true").lower() == "true"
CARPETA = env("CARPETA", "descargas")
DB_PATH = env("DB_PATH", "vistos.db")     # en Render: /data/vistos.db (con disco)
REFERER = "https://www.picta.cu/"
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


def nuevo_driver():
    opts = webdriver.ChromeOptions()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--mute-audio")
    opts.add_argument("--window-size=1280,900")
    opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    if env("CHROME_BIN"):
        opts.binary_location = env("CHROME_BIN")
    if env("CHROMEDRIVER"):
        from selenium.webdriver.chrome.service import Service
        return webdriver.Chrome(service=Service(env("CHROMEDRIVER")), options=opts)
    return webdriver.Chrome(options=opts)


def enlaces_de_la_pagina(driver):
    driver.get(SECCION_URL)
    # Picta es Angular: espera hasta ~25 s a que aparezcan enlaces
    for _ in range(12):
        time.sleep(2)
        if len(driver.find_elements(By.TAG_NAME, "a")) > 3:
            break
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
    time.sleep(3)
    if DIAGNOSTICO:
        print("Título de la página:", driver.title)
        print("URL final:", driver.current_url)
        try:
            texto = driver.find_element(By.TAG_NAME, "body").text
            print("Texto visible:", texto[:300].replace("\n", " | "))
        except Exception:
            pass
        print("Total de <a>:", len(driver.find_elements(By.TAG_NAME, "a")))
        print("Elementos <video>:", len(driver.find_elements(By.TAG_NAME, "video")))
        rutas = [e.get_attribute("ng-reflect-router-link")
                 for e in driver.find_elements(By.CSS_SELECTOR, "[ng-reflect-router-link]")]
        print("Rutas Angular (routerLink):", [r for r in rutas if r][:15])
    vistos, salida = set(), []
    for a in driver.find_elements(By.TAG_NAME, "a"):
        href = a.get_attribute("href")
        if not href or href in vistos or "picta.cu" not in href:
            continue
        vistos.add(href)
        if not FRAGMENTO or FRAGMENTO in href:
            salida.append(href)
    return salida


def buscar_manifest(driver, url):
    """Abre la página del video y captura la URL del manifest.mpd del tráfico de red."""
    driver.get_log("performance")  # vacía el registro previo
    driver.get(url)
    for _ in range(10):
        time.sleep(2)
        try:
            driver.execute_script("document.querySelector('video') && document.querySelector('video').play()")
        except Exception:
            pass
        for entrada in driver.get_log("performance"):
            try:
                msg = json.loads(entrada["message"])["message"]
            except Exception:
                continue
            if msg.get("method") == "Network.requestWillBeSent":
                u = msg["params"]["request"]["url"]
                if "manifest.mpd" in u or ".m3u8" in u:
                    return u, driver.title
    return None, driver.title


def buscar_nuevos():
    """Devuelve lista de dicts {url, manifest, titulo} de videos aún no publicados."""
    driver = nuevo_driver()
    try:
        enlaces = enlaces_de_la_pagina(driver)
        print(f"Enlaces encontrados: {len(enlaces)}")

        if DIAGNOSTICO:
            for e in enlaces:
                print("  ", e)
            return []

        if MARCAR_INICIAL and db.execute("SELECT COUNT(*) FROM vistos").fetchone()[0] == 0:
            for e in enlaces:
                marcar(e)
            print("Primera ejecución: enlaces actuales marcados como vistos.")
            return []

        resultado = []
        for url in [e for e in enlaces if not ya_visto(e)][:MAX_POR_CICLO]:
            manifest, tit = buscar_manifest(driver, url)
            if manifest:
                resultado.append({"url": url, "manifest": manifest, "titulo": tit})
            else:
                print(f"[!] Sin manifest en {url} (¿no es una página de video?)")
                marcar(url)  # evita reintentar páginas que no son videos
        return resultado
    finally:
        driver.quit()


def correr(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def descargar(video):
    nombre = hashlib.md5(video["url"].encode()).hexdigest()
    plantilla = os.path.join(CARPETA, nombre + ".%(ext)s")
    intentos = [
        ["yt-dlp", "--referer", REFERER, "-f", "bv*+ba/b",
         "--merge-output-format", "mp4", "-o", plantilla, video["manifest"]],
        ["picta-dl", "-o", plantilla, video["url"]],
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


async def main():
    servidor_web()
    client = TelegramClient("sesion_bot", API_ID, API_HASH)
    await client.start(bot_token=BOT_TOKEN)
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
