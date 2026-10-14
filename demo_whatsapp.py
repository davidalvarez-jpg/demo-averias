"""Demo de Averías Hostelería en WhatsApp, gratis y sin IA.

Un guion que entiende qué máquina se ha roto y lleva al dueño del bar por todo el proceso
desde su móvil: parte abierto → técnico asignado con código → pieza pedida → reparación →
cierre con código → factura → valoración. El "técnico" es simulado y el reloj va acelerado.

Variables de entorno:
  WHATSAPP_TOKEN            token de Meta (permanente, de un usuario del sistema)
  WHATSAPP_PHONE_NUMBER_ID  "Phone number ID" del número de prueba
  WHATSAPP_VERIFY_TOKEN     contraseña que inventas y pones igual en Meta (webhook)
  WHATSAPP_APP_SECRET       opcional: comprueba que los mensajes vienen de Meta
  DEMO_RITMO                opcional: multiplica las esperas (1 = normal, 0.3 = rápido)
Sin WHATSAPP_TOKEN, los mensajes se imprimen en pantalla (para probar en el ordenador).
"""

import hashlib
import hmac
import json
import os
import random
import threading
import time
import urllib.error
import urllib.request
import unicodedata
from collections import deque
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

TOKEN = os.environ.get("WHATSAPP_TOKEN", "").strip()
PHONE_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "").strip()
VERIFY = os.environ.get("WHATSAPP_VERIFY_TOKEN", "").strip()
SECRETO = os.environ.get("WHATSAPP_APP_SECRET", "").strip()
WABA_ID = os.environ.get("WHATSAPP_WABA_ID", "1649530216617131").strip()  # cuenta de WhatsApp Business
RITMO = float(os.environ.get("DEMO_RITMO", "1"))
try:
    MADRID = ZoneInfo("Europe/Madrid")
except Exception:  # servidores sin base de husos horarios
    from datetime import timezone
    MADRID = timezone(timedelta(hours=2))

TARIFA = {"desplazamiento": 45, "hora": 50, "urgencia": 30, "margen_piezas": 0.20, "iva": 0.21}

# palabra clave → (nombre con artículo, pieza que se cambia, precio de la pieza, técnicos que la reparan)
MAQUINAS = [
    (("cafeter", "cafe", "molino", "espresso", "expres"), ("la cafetera", "Resistencia 2000 W", 38, "cafeteras")),
    (("lavavaj", "lavavas", "lavaplat", "lavaloz"), ("el lavavajillas", "Bomba de desagüe", 55, "lavado")),
    (("botellero",), ("el botellero", "Termostato", 35, "frío")),
    (("hielo", "cubito"), ("la máquina de hielo", "Sonda de temperatura", 28, "frío")),
    (("camara", "nevera", "frigo", "congelador", "mesa fria", "vitrina", "enfria"),
     ("la cámara", "Ventilador condensador", 42, "frío")),
    (("horno", "plancha", "freidora", "fogon", "campana", "fuego"), ("el horno", "Termostato", 35, "cocina")),
    (("grifo", "tirador", "cerveza", "barril"), ("el grifo de cerveza", "Válvula y juntas", 18, "cerveza")),
]
MAQUINA_GENERICA = ("la máquina", "Pieza de recambio", 40, "maquinaria")
PALABRAS_AVERIA = ("roto", "rota", "no funciona", "no va", "averi", "no enfria", "no calienta", "estropead",
                   "fuga", "pierde agua", "gotea", "no arranca", "se ha parado", "no hace")
SI = {"si", "ok", "vale", "funciona", "perfecto", "listo", "bien", "dale", "correcto", "hecho", "genial", "estupendo"}
NO = {"no", "sigue", "todavia", "aun", "nada", "igual"}

_estados = {}
_bloqueo = threading.Lock()
_vistos = deque(maxlen=2000)
DIAGNOSTICO = {"arranque": datetime.now(MADRID).isoformat(timespec="seconds"), "avisos_meta": 0,
               "ultimo_aviso_meta": None, "mensajes_recibidos": 0, "envios_ok": 0, "ultimo_error_envio": None,
               "suscripcion": None, "numero": None, "apps_suscritas": None}


def normal(t):
    t = unicodedata.normalize("NFD", t.lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def detectar_maquina(texto):
    t = normal(texto)
    for claves, datos in MAQUINAS:
        if any(c in t for c in claves):
            return datos
    return None


def eur(x):
    return f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


# ---------- Envío ----------

def enviar(tel, texto):
    if not TOKEN:
        print(f"\n[→ {tel}]\n{texto}\n")
        return
    datos = json.dumps({"messaging_product": "whatsapp", "to": tel, "type": "text",
                        "text": {"body": texto, "preview_url": False}}).encode()
    peticion = urllib.request.Request(f"https://graph.facebook.com/v21.0/{PHONE_ID}/messages", data=datos,
                                      headers={"Authorization": f"Bearer {TOKEN}",
                                               "Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(peticion, timeout=20).read()
        DIAGNOSTICO["envios_ok"] += 1
    except urllib.error.HTTPError as e:
        error = e.read().decode(errors="ignore")
        DIAGNOSTICO["ultimo_error_envio"] = f"{e.code}: {error[:500]}"
        print(f"[whatsapp] Error {e.code}: {error}", flush=True)
    except urllib.error.URLError as e:
        DIAGNOSTICO["ultimo_error_envio"] = f"sin conexión: {e}"
        print(f"[whatsapp] Sin conexión: {e}", flush=True)


def graph(metodo, ruta):
    """Llamada a la API de Meta; devuelve la respuesta (o el error) como diccionario."""
    peticion = urllib.request.Request(f"https://graph.facebook.com/v21.0/{ruta}", method=metodo,
                                      data=b"" if metodo == "POST" else None,
                                      headers={"Authorization": f"Bearer {TOKEN}"})
    try:
        return json.loads(urllib.request.urlopen(peticion, timeout=20).read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return {"error_http": e.code, **json.loads(e.read() or b"{}")}
        except ValueError:
            return {"error_http": e.code}
    except Exception as e:
        return {"error": str(e)}


def preparar_whatsapp():
    """Al arrancar: suscribe la cuenta de WhatsApp a esta app (si no, Meta no avisa de los mensajes)."""
    if not (TOKEN and WABA_ID):
        return
    DIAGNOSTICO["suscripcion"] = graph("POST", f"{WABA_ID}/subscribed_apps")
    DIAGNOSTICO["apps_suscritas"] = graph("GET", f"{WABA_ID}/subscribed_apps")
    if PHONE_ID:
        DIAGNOSTICO["numero"] = graph("GET", f"{PHONE_ID}?fields=display_phone_number,verified_name,"
                                             "code_verification_status,quality_rating,platform_type")
    print(f"[demo] Suscripción a WhatsApp: {DIAGNOSTICO['suscripcion']}", flush=True)


def marcar_leido(mensaje_id):
    if TOKEN:
        try:
            datos = json.dumps({"messaging_product": "whatsapp", "status": "read", "message_id": mensaje_id}).encode()
            urllib.request.urlopen(urllib.request.Request(
                f"https://graph.facebook.com/v21.0/{PHONE_ID}/messages", data=datos, method="POST",
                headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}), timeout=10).read()
        except Exception:
            pass


def decir(tel, *textos, pausa=1.6):
    """Envía varios mensajes seguidos con una pausa corta, como escribiría una persona."""
    for i, t in enumerate(textos):
        if i:
            time.sleep(pausa)
        enviar(tel, t)


# ---------- Guion ----------

def nuevo_estado():
    return {"paso": "inicio", "maquina": None, "local": "", "urgente": True, "parte": random.randint(140, 199),
            "codigo": f"{random.randint(1000, 9999)}", "version": 0, "inicio": None}


def reiniciar(tel):
    with _bloqueo:
        e = nuevo_estado()
        e["version"] = _estados.get(tel, {}).get("version", 0) + 1  # anula los avisos programados
        _estados[tel] = e
    return e


def programar(tel, segundos, funcion):
    """Ejecuta funcion(tel) dentro de un rato, salvo que la demo se haya reiniciado mientras."""
    version = _estados[tel]["version"]

    def lanzar():
        time.sleep(segundos * RITMO)
        if _estados.get(tel, {}).get("version") == version:
            funcion(tel)
    threading.Thread(target=lanzar, daemon=True).start()


def hora_en(minutos, e):
    return (e["inicio"] + timedelta(minutes=minutos)).strftime("%H:%M")


def atender(tel, nombre, texto):
    t = normal(texto).strip()
    if t in ("reiniciar", "reset", "empezar", "demo", "inicio", "nueva"):
        reiniciar(tel)
        decir(tel, saludo(nombre))
        return

    e = _estados.get(tel) or reiniciar(tel)
    paso = e["paso"]

    if paso == "inicio":
        m = detectar_maquina(texto)
        if not m and not any(p in t for p in PALABRAS_AVERIA):
            decir(tel, saludo(nombre))
            e["paso"] = "esperando"
            return
        e["maquina"] = m or MAQUINA_GENERICA
        e["paso"] = "local"
        decir(tel, f"Vaya, lo siento 😕 Te busco técnico ahora mismo para {e['maquina'][0]}.",
              "¿Qué marca es y cómo se llama tu local? (con la calle, para mandarte al técnico más cercano)")
        return

    if paso == "esperando":
        m = detectar_maquina(texto)
        if not m and not any(p in t for p in PALABRAS_AVERIA):
            decir(tel, "Dime qué máquina se ha estropeado, por ejemplo: *la cafetera no calienta* "
                       "o *la cámara no enfría* 🙂")
            return
        e["paso"] = "inicio"
        atender(tel, nombre, texto)
        return

    if paso == "local":
        e["local"] = texto.strip()
        if e["maquina"] == MAQUINA_GENERICA:
            m = detectar_maquina(texto)
            if m:
                e["maquina"] = m
        e["paso"] = "urgencia"
        decir(tel, f"Apuntado. ¿Podéis seguir trabajando o {e['maquina'][0]} está parada del todo?"
              if e["maquina"][0].startswith("la") else
              f"Apuntado. ¿Podéis seguir trabajando o {e['maquina'][0]} está parado del todo?")
        return

    if paso == "urgencia":
        parada = any(p in t for p in ("parad", "no podemos", "no funciona nada", "urgente", "del todo"))
        e["urgente"] = parada or not any(p in t for p in ("podemos seguir", "seguimos", "no es urgente", "tranquil",
                                                          "aguanta", "a medias", "mas o menos"))
        e["paso"] = "en_curso"
        e["inicio"] = datetime.now(MADRID)
        decir(tel, f"Apuntado como *{'urgente' if e['urgente'] else 'normal'}*. "
                   f"Parte *#0{e['parte']}* abierto ✅\n\n"
                   f"Aviso ya a los técnicos verificados de {e['maquina'][3]} de tu zona. "
                   "El primero que lo acepta se lo queda. Te digo quién va en un momento.")
        programar(tel, 12, tecnico_asignado)
        return

    if paso == "en_curso":
        decir(tel, "Lo tengo en marcha 👍 Te escribo en cuanto haya novedades del técnico.")
        return

    if paso == "confirmar":
        palabras = set("".join(c if c.isalnum() else " " for c in t).split())
        if e["codigo"] in t or (palabras & SI and not palabras & NO):
            e["paso"] = "valorar"
            decir(tel, f"{'Código correcto' if e['codigo'] in t else 'Perfecto'} ✅ Parte *#0{e['parte']}* cerrado.", factura(e),
                  "¿Qué tal ha ido con Rubén? Puntúa del *1* al *5*.")
        elif palabras & NO:
            decir(tel, "Vale, se lo digo a Rubén para que lo revise otra vez. No le des el código hasta que funcione 👍",
                  pausa=0)
            programar(tel, 10, lambda x: enviar(x, f"Rubén lo ha vuelto a revisar y dice que ya está. "
                                                   f"¿Funciona? Responde *sí* o mándame el código *{e['codigo']}*."))
        else:
            decir(tel, f"Cuando {e['maquina'][0]} funcione, responde *sí* o mándame tu código *{e['codigo']}*.")
        return

    if paso == "valorar":
        nota = next((int(c) for c in t if c in "12345"), None)
        if nota is None:
            decir(tel, "Puntúa del *1* al *5*, por favor 🙂")
            return
        e["paso"] = "fin"
        gracias = ("¡Gracias! ⭐ Las valoraciones hacen que solo se queden los buenos técnicos."
                   if nota >= 4 else
                   "Gracias por decírnoslo. Lo revisa la oficina hoy mismo y te llamamos si hace falta.")
        decir(tel, gracias, "Para cualquier otra avería, escríbenos aquí mismo, a cualquier hora.",
              "— Fin de la demo —\n\nAsí funcionaría en tu local: sin llamadas, con precio cerrado y pagando solo "
              "si la máquina funciona.\n\nEscribe *reiniciar* para probarlo otra vez.")
        return

    if paso == "fin":
        decir(tel, "Escribe *reiniciar* para empezar otra demo 🙂")


def saludo(nombre):
    hola = f"¡Hola{', ' + nombre.split()[0] if nombre else ''}! 👋"
    return (f"{hola} Soy Nora, de *Averías Hostelería*.\n\n"
            "Si se os rompe una máquina (cafetera, cámara, lavavajillas, hielo, horno…), escríbeme qué le pasa "
            "y te mando un técnico verificado de tu zona. Sin llamadas.\n\n"
            "Pruébalo: cuéntame qué se ha roto.")


# ---------- Lo que hace el técnico simulado ----------

def tecnico_asignado(tel):
    e = _estados[tel]
    decir(tel, f"Ya está: va *Rubén Ortega* ★ 4,9 (132 reparaciones). Llega sobre las *{hora_en(30, e)}*.\n\n"
               f"🔑 Tu código de visita es *{e['codigo']}*. Dáselo solo cuando {e['maquina'][0]} funcione: "
               "sin código no se puede cerrar ni cobrar.",
          "⏩ _En esta demo adelantamos el reloj para que lo veas entero en un par de minutos._")
    programar(tel, 18, tecnico_llega)


def tecnico_llega(tel):
    e = _estados[tel]
    pieza = e["maquina"][1]
    decir(tel, f"🚐 {hora_en(30, e)} · Rubén ha llegado a tu local.")
    time.sleep(8 * RITMO)
    if _estados.get(tel) is not e:
        return
    decir(tel, f"🔧 {hora_en(41, e)} · Rubén ha encontrado la avería: hay que cambiar una pieza: *{pieza}*.\n"
               "La ha pedido al distribuidor por aquí mismo y se la traen en 25 minutos. No tienes que hacer nada.")
    programar(tel, 16, tecnico_termina)


def tecnico_termina(tel):
    e = _estados[tel]
    e["paso"] = "confirmar"
    decir(tel, f"✅ {hora_en(78, e)} · Rubén dice que ha terminado y que {e['maquina'][0]} ya funciona.\n\n"
               f"Compruébalo y, si está bien, dale tu código *{e['codigo']}* y responde *sí* aquí para cerrar el parte.")


def factura(e):
    _, pieza, coste, _ = e["maquina"]
    pieza_pvp = round(coste * (1 + TARIFA["margen_piezas"]), 2)
    lineas = [("Desplazamiento", TARIFA["desplazamiento"]), ("Mano de obra 1 h", TARIFA["hora"])]
    if e["urgente"]:
        lineas.append(("Recargo urgencia", TARIFA["urgencia"]))
    lineas.append((pieza, pieza_pvp))
    base = round(sum(v for _, v in lineas), 2)
    iva = round(base * TARIFA["iva"], 2)
    filas = "\n".join(f"{n[:22]:<22}{eur(v):>9}" for n, v in lineas)
    return (f"🧾 *Factura · Parte #0{e['parte']}*\n"
            f"Técnico: Rubén Ortega (autónomo)\n"
            + (f"Local: {e['local'][:60]}\n" if e["local"] else "") +
            "```\n"
            f"{filas}\n"
            f"{'-' * 31}\n"
            f"{'Base imponible':<22}{eur(base):>9}\n"
            f"{'IVA 21 %':<22}{eur(iva):>9}\n"
            f"{'TOTAL':<22}{eur(base + iva):>9} €\n"
            "```\n"
            "Precio cerrado con la tarifa que se te dio al abrir el parte.")


# ---------- Servidor ----------

CONTACTO = os.environ.get("DEMO_CONTACTO", "david.alvarez@alumni.mondragon.edu")
PRIVACIDAD = f"""<!doctype html><html lang="es"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Política de privacidad · Averías Hostelería (demo)</title>
<body style="font:17px/1.55 system-ui,sans-serif;max-width:40em;margin:0 auto;padding:24px;color:#17201d">
<h1>Política de privacidad</h1>
<p><b>Averías Hostelería</b> es una demostración de un servicio de reparación de maquinaria para hostelería
por WhatsApp. No presta servicios reales ni envía técnicos.</p>
<h2>Qué datos tratamos</h2>
<p>Solo el número de teléfono, el nombre de perfil de WhatsApp y los mensajes que nos envías durante la demo.</p>
<h2>Para qué</h2>
<p>Únicamente para responderte dentro de la demostración. No usamos tus datos para publicidad ni los cedemos
a terceros, salvo a Meta Platforms (WhatsApp), que transmite los mensajes.</p>
<h2>Cuánto tiempo</h2>
<p>La conversación se guarda solo en la memoria temporal del servidor y se borra al reiniciarse, como mucho
en unas horas. No se guarda en ninguna base de datos.</p>
<h2>Tus derechos</h2>
<p>Puedes pedir información o la eliminación de tus datos escribiendo a <b>{CONTACTO}</b>, o escribir
<b>reiniciar</b> en el chat para borrar la conversación de la demo.</p>
<p style="color:#5b6662">Última actualización: 14 de octubre de 2026.</p>
</body></html>"""

def procesar(carga):
    for entrada in carga.get("entry", []):
        for cambio in entrada.get("changes", []):
            valor = cambio.get("value", {})
            nombres = {c.get("wa_id"): c.get("profile", {}).get("name", "") for c in valor.get("contacts", [])}
            for m in valor.get("messages", []):
                if m.get("id") in _vistos:
                    continue
                _vistos.append(m.get("id"))
                tel = m["from"]
                DIAGNOSTICO["mensajes_recibidos"] += 1
                print(f"[demo] Mensaje recibido de …{tel[-4:]} ({m.get('type')})", flush=True)
                marcar_leido(m.get("id"))
                tipo = m.get("type")
                if tipo == "text":
                    texto = m["text"]["body"]
                elif tipo == "button":
                    texto = m["button"].get("text", "")
                elif tipo == "interactive":
                    i = m["interactive"]
                    texto = (i.get("button_reply") or i.get("list_reply") or {}).get("title", "")
                else:
                    enviar(tel, "En esta demo solo leo mensajes de texto 🙏 ¿Me lo escribes?")
                    continue
                try:
                    atender(tel, nombres.get(tel, ""), texto)
                except Exception as e:  # que un fallo no deje al bar sin respuesta
                    print(f"[demo] Error: {type(e).__name__}: {e}")
                    enviar(tel, "Uy, me he liado. Escribe *reiniciar* para empezar de nuevo.")


class Manejador(BaseHTTPRequestHandler):
    def _fin(self, codigo, cuerpo=b"", tipo="text/plain; charset=utf-8"):
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/webhook":
            if q.get("hub.mode") == "subscribe" and VERIFY and q.get("hub.verify_token") == VERIFY:
                return self._fin(200, q.get("hub.challenge", "").encode())
            return self._fin(403, b"Token de verificacion incorrecto")
        if url.path == "/diagnostico":
            if not VERIFY or q.get("clave") != VERIFY:
                return self._fin(403, b"Falta la clave")
            if q.get("resuscribir"):
                preparar_whatsapp()
            datos = dict(DIAGNOSTICO, token_configurado=bool(TOKEN), phone_id=PHONE_ID, waba_id=WABA_ID,
                         conversaciones=len(_estados))
            return self._fin(200, json.dumps(datos, ensure_ascii=False, indent=1).encode(),
                             "application/json; charset=utf-8")
        if url.path in ("/privacidad", "/privacy"):
            return self._fin(200, PRIVACIDAD.encode(), "text/html; charset=utf-8")
        estado = "conectado a WhatsApp" if TOKEN and PHONE_ID else "SIN configurar (faltan variables)"
        html = (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                f"<body style='font:18px system-ui;padding:24px;max-width:30em'>"
                f"<h1>✅ Demo de averías despierta</h1><p>WhatsApp: {estado}.</p>"
                f"<p>Ya puedes escribir al número de la demo.</p></body>").encode()
        self._fin(200, html, "text/html; charset=utf-8")

    def do_HEAD(self):
        self._fin(200)

    def do_POST(self):
        cuerpo = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        if urlparse(self.path).path != "/webhook":
            return self._fin(404)
        if SECRETO:
            firma = "sha256=" + hmac.new(SECRETO.encode(), cuerpo, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(firma, self.headers.get("X-Hub-Signature-256", "")):
                return self._fin(401)
        self._fin(200, b"ok")  # Meta exige respuesta rápida
        DIAGNOSTICO["avisos_meta"] += 1
        DIAGNOSTICO["ultimo_aviso_meta"] = datetime.now(MADRID).isoformat(timespec="seconds")
        print(f"[demo] Aviso de Meta recibido ({len(cuerpo)} bytes)", flush=True)
        try:
            carga = json.loads(cuerpo or b"{}")
        except ValueError:
            return
        threading.Thread(target=procesar, args=(carga,), daemon=True).start()

    def log_message(self, formato, *args):
        pass


def main():
    puerto = int(os.environ.get("PORT", "8020"))
    print(f"Demo de averías escuchando en el puerto {puerto} · WhatsApp {'conectado' if TOKEN else 'NO configurado (modo consola)'}", flush=True)
    threading.Thread(target=preparar_whatsapp, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", puerto), Manejador).serve_forever()


if __name__ == "__main__":
    main()
