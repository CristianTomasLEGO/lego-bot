#!/usr/bin/env python3
"""Bot de ofertas LEGO v4: peina el catálogo LEGO de varias tiendas, guarda el histórico
de precios y te avisa por Telegram solo de descuentos REALES (el set vale de verdad su
precio normal), comparando con los 3-4 meses previos, los últimos 12 meses y las
Navidades pasadas.

Solo usa la librería estándar de Python (no hay que instalar nada).

Uso (en Windows escribe "py" en lugar de "python"):
    python bot.py                       # una pasada por todas las tiendas
    python bot.py --loop 900            # repite cada 900 s (15 min)
    python bot.py --probar "URL"        # comprueba si sabe leer una página de listado
    python bot.py --informe 75375       # historial y análisis de un set
    python bot.py --importar datos.csv  # carga historial de precios de otras fuentes
    python bot.py --telegram-test       # manda un mensaje de prueba
    python bot.py --test                # autotest sin internet
"""
import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from statistics import median

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
DB_PATH = os.path.join(BASE, "precios.db")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"

SET_RE = re.compile(r"(?<!\d)(\d{5})(?!\d)")
PRICE_RE = re.compile(
    r"(\d{1,3}(?:\.\d{3})*,\d{2}|\d+[.,]\d{2})\s*(?:€|&euro;|eur)|(?:€|&euro;)\s*(\d{1,3}(?:\.\d{3})*,\d{2}|\d+[.,]\d{2})",
    re.I)
ANCHOR_RE = re.compile(r'<a\b[^>]*?href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.S | re.I)
TAG_RE = re.compile(r"<[^>]+>")


# ---------- Descarga y utilidades ----------
def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "es-ES,es;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def _to_float(v):
    if v is None:
        return None
    s = str(v).strip().replace("\xa0", "").replace("€", "").replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _offer_price(offers):
    if offers is None:
        return None
    for off in (offers if isinstance(offers, list) else [offers]):
        if not isinstance(off, dict):
            continue
        avail = str(off.get("availability", ""))
        if avail and "InStock" not in avail:
            continue  # solo lo que se puede comprar
        p = _to_float(off.get("price") or off.get("lowPrice"))
        if p:
            return p
    return None


def set_id_de(nombre, url):
    """Número de set LEGO (5 cifras) sacado del nombre o de la URL."""
    for texto in (nombre or "", url or ""):
        m = SET_RE.search(texto)
        if m:
            return m.group(1)
    return None


def fmt_fecha(f):
    return date.fromisoformat(f).strftime("%d/%m/%Y")


# ---------- Lectura de páginas de listado ----------
def _productos_jsonld(html, base_url):
    out = []
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        for node in _walk(data):
            t = node.get("@type")
            tipos = t if isinstance(t, list) else [t]
            if "Product" not in tipos:
                continue
            nombre, url = node.get("name"), node.get("url")
            precio = _offer_price(node.get("offers"))
            if nombre and url and precio:
                out.append({"nombre": str(nombre), "url": urllib.parse.urljoin(base_url, url), "precio": precio})
    return out


def _productos_heuristica(html, base_url):
    """Plan B: enlaces con nombre seguidos de un precio en euros. Menos fiable."""
    anchors = list(ANCHOR_RE.finditer(html))
    out = []
    for i, m in enumerate(anchors):
        nombre = re.sub(r"\s+", " ", TAG_RE.sub(" ", m.group(2))).strip()
        if len(nombre) < 8:
            continue
        fin = min(m.end() + 600, len(html))
        for nxt in anchors[i + 1:]:
            if nxt.start() >= fin:
                break
            n2 = re.sub(r"\s+", " ", TAG_RE.sub(" ", nxt.group(2))).strip()
            if len(n2) >= 8 and nxt.group(1) != m.group(1):
                fin = nxt.start()
                break
        precios = []
        for pm in PRICE_RE.finditer(html[m.start():fin]):
            p = _to_float(pm.group(1) or pm.group(2))
            if p:
                precios.append(p)
            if len(precios) == 2:
                break
        if not precios:
            continue
        url = urllib.parse.urljoin(base_url, m.group(1))
        if not set_id_de(nombre, url):
            continue  # sin número de set no nos fiamos
        # Si hay dos precios, el menor es el actual (el otro suele ser el "antes" tachado).
        out.append({"nombre": nombre, "url": url, "precio": min(precios)})
    return out


def parse_listado(html, base_url):
    prods = _productos_jsonld(html, base_url) or _productos_heuristica(html, base_url)
    vistos, unicos = set(), []
    for p in prods:
        if p["url"] not in vistos:
            vistos.add(p["url"])
            unicos.append(p)
    return unicos


def peinar_tienda(tienda, pausa):
    """Recorre todas las páginas de listado de una tienda y devuelve sus productos."""
    todos, vistos = [], set()
    for base in tienda["listados"]:
        if "PON_AQUI" in base:
            continue
        paginas = int(tienda.get("paginas_max", 10)) if "{n}" in base else 1
        for n in range(1, paginas + 1):
            url = base.replace("{n}", str(n))
            try:
                prods = parse_listado(fetch(url), url)
            except Exception as e:
                print(f"  [{tienda['nombre']}] error en {url}: {e}")
                break
            nuevos = [p for p in prods if p["url"] not in vistos]
            if not nuevos:
                break  # página vacía o repetida: se acabó el listado
            for p in nuevos:
                vistos.add(p["url"])
                p["tienda"] = tienda["nombre"]
                p["set_id"] = set_id_de(p["nombre"], p["url"]) or "x" + hashlib.md5(p["url"].encode()).hexdigest()[:8]
                todos.append(p)
            time.sleep(pausa)
    return todos


# ---------- Cálculos de margen ----------
def net_margin(buy_price, resale_price, fee_pct, shipping_cost):
    """Beneficio neto y % sobre la compra, tras comisión de venta y envío."""
    net_sale = resale_price * (1 - fee_pct / 100) - shipping_cost
    profit = net_sale - buy_price
    return profit, (profit / buy_price * 100) if buy_price else 0.0


def percentil75(valores):
    v = sorted(valores)
    return v[min(len(v) - 1, int(len(v) * 0.75))]


def evaluar(precio, reventa, cfg):
    """Devuelve (beneficio, %) si cumple tus reglas; si no, None."""
    c, r = cfg["compra"], cfg["reventa"]
    if not (c["precio_min_eur"] <= precio <= c["precio_max_eur"]):
        return None
    profit, pct = net_margin(precio, reventa, r["comision_pct"], r["envio_eur"])
    if profit < r["beneficio_minimo_eur"]:
        return None
    return profit, pct


# ---------- Base de datos ----------
def crear_tablas(con):
    con.execute("""CREATE TABLE IF NOT EXISTS precios (
        ts TEXT, tienda TEXT, set_id TEXT, nombre TEXT, url TEXT, precio REAL)""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_set ON precios(set_id, ts)")
    con.execute("""CREATE TABLE IF NOT EXISTS alertas (
        set_id TEXT, tienda TEXT, precio REAL, ts TEXT)""")


def db():
    con = sqlite3.connect(DB_PATH)
    crear_tablas(con)
    return con


def ya_avisado(con, set_id, tienda, precio):
    row = con.execute("SELECT precio FROM alertas WHERE set_id=? AND tienda=? ORDER BY ts DESC LIMIT 1",
                      (set_id, tienda)).fetchone()
    return row is not None and abs(row[0] - precio) < 0.01


# ---------- Análisis de histórico ----------
def _serie_diaria(con, set_id, desde_iso, hasta_iso):
    """Un precio por día y tienda (el más bajo de ese día)."""
    d = {}
    for ts, tienda, precio in con.execute(
            "SELECT ts, tienda, precio FROM precios WHERE set_id=? AND ts>=? AND ts<?",
            (set_id, desde_iso, hasta_iso)):
        k = (ts[:10], tienda)
        if k not in d or precio < d[k]:
            d[k] = precio
    return sorted((f, t, p) for (f, t), p in d.items())


def analizar(con, set_id, ahora, otras, run_ts, cfg):
    """Calcula el 'precio normal' de un set y datos de contexto (3-4 meses, 12 meses, Navidad)."""
    hoy = ahora.date()
    busqueda = int(cfg.get("dias_historico", 400))
    serie = _serie_diaria(con, set_id, (hoy - timedelta(days=busqueda)).isoformat(), run_ts)
    r = {"normal": None, "fuente": "", "confianza": "BAJA", "dias_hist": 0, "med3": None, "min3": None,
         "min12": None, "nav": None, "estable": None, "inflado": False, "n_muestras": 0}
    f2d = date.fromisoformat
    if serie:
        r["dias_hist"] = (hoy - f2d(serie[0][0])).days
    r["confianza"] = "ALTA" if r["dias_hist"] >= 270 else "MEDIA" if r["dias_hist"] >= 90 else "BAJA"

    s12 = [x for x in serie if f2d(x[0]) >= hoy - timedelta(days=365)]
    s34 = [x for x in serie if f2d(x[0]) >= hoy - timedelta(days=120)]       # últimos 3-4 meses
    base = [x for x in serie if hoy - timedelta(days=120) <= f2d(x[0]) <= hoy - timedelta(days=14)]
    dias_base = len({x[0] for x in base})
    dias_12 = len({x[0] for x in s12})

    med_3m = median([x[2] for x in base]) if dias_base >= 7 else None
    med_12m = median([x[2] for x in s12]) if dias_12 >= 14 else None
    if s34:
        r["med3"], r["min3"] = median([x[2] for x in s34]), min(x[2] for x in s34)
    if s12:
        m = min(s12, key=lambda x: x[2])
        r["min12"] = (m[2], m[0], m[1])

    # Navidad pasada: del 1 de noviembre al 10 de enero de la temporada anterior
    ini, fin = date(hoy.year - 1, 11, 1), min(date(hoy.year, 1, 10), hoy)
    nav = [x for x in serie if ini <= f2d(x[0]) <= fin]
    if nav:
        m = min(nav, key=lambda x: x[2])
        r["nav"] = (m[2], m[0], m[1])

    candidatos = [v for v in (med_3m, med_12m) if v]
    if r["confianza"] in ("ALTA", "MEDIA") and candidatos:
        r["normal"] = min(candidatos)   # criterio prudente: la referencia más baja de las dos
        r["fuente"] = "mediana de los 3-4 meses previos y de 12 meses; se usa la más baja"
        r["n_muestras"] = len(s12)
    else:
        muestras = [x[2] for x in serie] + list(otras)
        r["n_muestras"] = len(muestras)
        if len(muestras) >= int(cfg.get("min_muestras_referencia", 2)):
            r["normal"] = percentil75(muestras)
            r["fuente"] = "comparación con otras tiendas y pocos días de historial"

    if r["normal"]:
        if dias_base >= 10 and med_3m:
            dentro = sum(1 for x in base if abs(x[2] - med_3m) / med_3m <= 0.12)
            r["estable"] = dentro / len(base) >= 0.6
        ult30 = [x[2] for x in serie if f2d(x[0]) >= hoy - timedelta(days=30)]
        r["inflado"] = bool(ult30) and max(ult30) > r["normal"] * 1.15
    return r


def redactar(p, an, reventa, profit, pct, nivel, antes, manual):
    L = [f"OFERTA {nivel}: LEGO {p['set_id']}", p["nombre"], f"{p['tienda']}: {p['precio']:.2f} €{antes}"]
    if an["normal"]:
        desc = (an["normal"] - p["precio"]) / an["normal"] * 100
        L.append(f"Precio normal: {an['normal']:.2f} € -> estás {desc:.0f} % por debajo")
        L.append(f"({an['fuente']})")
    if an["med3"]:
        L.append(f"Últimos 3-4 meses: mediana {an['med3']:.2f} € / mínimo {an['min3']:.2f} €")
    if an["min12"]:
        v, f, t = an["min12"]
        extra = " <- es el mínimo de 12 meses" if p["precio"] <= v + 0.01 else ""
        L.append(f"12 meses: mínimo {v:.2f} € ({fmt_fecha(f)}, {t}){extra}")
    if an["nav"]:
        v, f, t = an["nav"]
        if v < p["precio"] - 0.5:
            L.append(f"AVISO: en Navidad pasada llegó a {v:.2f} € ({fmt_fecha(f)}, {t}). Puede bajar más.")
        else:
            L.append(f"Navidad pasada: mínimo {v:.2f} € ({fmt_fecha(f)}). Ahora está igual o mejor.")
    if an["estable"]:
        L.append("Precio normal estable: el descuento parece real.")
    if an["inflado"]:
        L.append("AVISO: el precio subió en el último mes antes de la rebaja. Cuidado con el 'antes'.")
    L.append(f"Reventa estimada: {reventa:.2f} €" + (" (tu precio manual)" if manual else ""))
    L.append(f"Beneficio neto: {profit:.2f} € ({pct:.0f} %)")
    L.append(f"Fiabilidad: {an['confianza']} ({an['dias_hist']} días de historial)")
    L.append(p["url"])
    return "\n".join(L)


# ---------- Telegram ----------
def notify(cfg, text):
    tg = cfg.get("telegram", {})
    token = os.environ.get("TELEGRAM_TOKEN") or tg.get("token")
    chat = os.environ.get("TELEGRAM_CHAT_ID") or tg.get("chat_id")
    if not token or not chat or "PON_AQUI" in str(token):
        print("[ALERTA - Telegram sin configurar]\n" + text + "\n")
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text, "disable_web_page_preview": "true"}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=15)
    except Exception as e:
        print("Error enviando a Telegram:", e)


# ---------- Pasada completa ----------
def run_once(cfg):
    con = db()
    r = cfg["reventa"]
    pausa = cfg.get("pausa_entre_peticiones_s", 3)
    ahora = datetime.now()
    run_ts = ahora.isoformat(timespec="seconds")

    obs = []
    for t in cfg["tiendas"]:
        print(f"Peinando {t['nombre']}...")
        prods = peinar_tienda(t, pausa)
        print(f"  {len(prods)} productos leídos")
        obs.extend(prods)
    if not obs:
        print("No se ha leído ningún producto. Usa --probar con la URL de un listado para ver qué pasa.")
        con.close()
        return

    manual = cfg.get("reventa_manual", {})
    factor = float(r.get("factor_reventa", 0.9))
    exigir_real = cfg.get("exigir_descuento_real", True)
    avisos = 0
    for p in obs:
        sid = p["set_id"]
        otras = [o["precio"] for o in obs if o["set_id"] == sid and o is not p]
        an = analizar(con, sid, ahora, otras, run_ts, cfg)
        if sid in manual:
            reventa = float(manual[sid])
        elif an["normal"]:
            reventa = an["normal"] * factor
        else:
            continue  # aún no hay con qué comparar: se guarda y listo
        if exigir_real and sid not in manual and an["estable"] is False:
            continue  # el precio normal de este set es muy variable: descuento dudoso
        res = evaluar(p["precio"], reventa, cfg)
        if not res or ya_avisado(con, sid, p["tienda"], p["precio"]):
            continue
        profit, pct = res
        nivel = "MUY BUENA" if profit >= r.get("beneficio_objetivo_eur", 15) else "BUENA"
        previo = con.execute("SELECT precio FROM precios WHERE set_id=? AND tienda=? AND ts<? ORDER BY ts DESC LIMIT 1",
                             (sid, p["tienda"], run_ts)).fetchone()
        antes = f"  (antes aquí: {previo[0]:.2f} €)" if previo and previo[0] > p["precio"] else ""
        notify(cfg, redactar(p, an, reventa, profit, pct, nivel, antes, sid in manual))
        con.execute("INSERT INTO alertas VALUES (?,?,?,?)", (sid, p["tienda"], p["precio"], run_ts))
        avisos += 1

    # Guardar solo si cambia el precio o es la primera lectura del día (así la base de datos no crece de más)
    ultimo = {(s, t): (pr, ts) for s, t, pr, ts in con.execute(
        "SELECT set_id, tienda, precio, ts FROM precios WHERE rowid IN (SELECT MAX(rowid) FROM precios GROUP BY set_id, tienda)")}
    filas = []
    for p in obs:
        u = ultimo.get((p["set_id"], p["tienda"]))
        if u is None or abs(u[0] - p["precio"]) > 0.001 or u[1][:10] != run_ts[:10]:
            filas.append((run_ts, p["tienda"], p["set_id"], p["nombre"], p["url"], p["precio"]))
    con.executemany("INSERT INTO precios VALUES (?,?,?,?,?,?)", filas)
    con.commit()
    con.close()
    print(f"Pasada terminada: {len(obs)} productos leídos, {avisos} avisos.")


# ---------- Herramientas ----------
def probar(url):
    prods = parse_listado(fetch(url), url)
    print(f"Productos leídos en esa página: {len(prods)}")
    for p in prods[:15]:
        print(f"  {p['precio']:>8.2f} €  set {set_id_de(p['nombre'], p['url']) or '?':<6} {p['nombre'][:70]}")
    if not prods:
        print("No he sabido leer productos. La web puede bloquear bots o cargar los productos con JavaScript.")


def informe(cfg, set_id):
    con = db()
    ahora = datetime.now()
    ult = con.execute("SELECT tienda, precio, nombre FROM precios WHERE set_id=? ORDER BY ts DESC LIMIT 1", (set_id,)).fetchone()
    if not ult:
        print("No hay datos de ese set todavía.")
        return
    an = analizar(con, set_id, ahora, [], (ahora + timedelta(days=1)).isoformat(timespec="seconds"), cfg)
    print(f"Set {set_id}: {ult[2]}\nÚltimo precio visto: {ult[1]:.2f} € ({ult[0]})")
    for k, v in an.items():
        print(f"  {k}: {v}")


def _fecha_csv(s):
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def importar_csv(path):
    """CSV con columnas: fecha, set_id, precio  (opcionales: tienda, nombre, url)."""
    con = db()
    n = 0
    with open(path, encoding="utf-8-sig", newline="") as f:
        muestra = f.read(4096)
        f.seek(0)
        dialecto = csv.Sniffer().sniff(muestra, delimiters=",;\t")
        for row in csv.DictReader(f, dialect=dialecto):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            fecha = _fecha_csv(row.get("fecha", ""))
            sid = row.get("set_id") or row.get("set")
            precio = _to_float(row.get("precio"))
            if not (fecha and sid and precio):
                continue
            con.execute("INSERT INTO precios VALUES (?,?,?,?,?,?)",
                        (fecha + "T12:00:00", row.get("tienda") or "importado", sid,
                         row.get("nombre", ""), row.get("url", ""), precio))
            n += 1
    con.commit()
    con.close()
    print(f"Importadas {n} filas de precio.")


def self_test():
    ld = ('<script type="application/ld+json">{"@type":"ItemList","itemListElement":['
          '{"@type":"ListItem","item":{"@type":"Product","name":"LEGO Star Wars 75375 Halcón","url":"/p/75375",'
          '"offers":{"price":"59,99","availability":"https://schema.org/InStock"}}},'
          '{"@type":"ListItem","item":{"@type":"Product","name":"LEGO City 60400","url":"/p/60400",'
          '"offers":{"price":"20","availability":"https://schema.org/OutOfStock"}}}]}</script>')
    prods = parse_listado(ld, "https://tienda.es/lego")
    assert len(prods) == 1 and prods[0]["precio"] == 59.99, prods

    html = ('<div><a href="/p/10316">LEGO Rivendell 10316 El Señor de los Anillos</a>'
            '<span class="old">499,99 €</span><span class="now">399,99 €</span></div>'
            '<div><a href="/p/42100">LEGO Technic 42100 Excavadora</a><span>449,00 €</span></div>')
    assert [p["precio"] for p in parse_listado(html, "https://tienda.es/lego")] == [399.99, 449.0]

    cfg = {"compra": {"precio_min_eur": 25, "precio_max_eur": 100},
           "reventa": {"comision_pct": 10, "envio_eur": 5, "beneficio_minimo_eur": 10},
           "dias_historico": 400}
    assert evaluar(30, 63, cfg) is not None and evaluar(40, 54, cfg) is None
    assert evaluar(20, 90, cfg) is None and evaluar(120, 300, cfg) is None

    # Histórico sintético de un año: 50 € normal, 52 € en ago-oct, 35 € en Navidad pasada
    con = sqlite3.connect(":memory:")
    crear_tablas(con)
    hoy = date(2026, 11, 9)
    d = hoy - timedelta(days=365)
    while d < hoy:
        precio = 50.0
        if date(2025, 12, 18) <= d <= date(2025, 12, 26):
            precio = 35.0
        elif date(2026, 8, 1) <= d <= date(2026, 10, 31):
            precio = 52.0
        con.execute("INSERT INTO precios VALUES (?,?,?,?,?,?)",
                    (d.isoformat() + "T12:00:00", "A", "11111", "LEGO test", "u", precio))
        d += timedelta(days=1)
    ahora = datetime(2026, 11, 9, 18, 0, 0)
    run_ts = ahora.isoformat(timespec="seconds")
    an = analizar(con, "11111", ahora, [], run_ts, cfg)
    assert an["confianza"] == "ALTA" and an["normal"] == 50.0, an
    assert an["nav"][0] == 35.0 and an["nav"][1] == "2025-12-18", an["nav"]
    assert an["min12"][0] == 35.0 and an["estable"] is True and an["inflado"] is False, an
    p = {"set_id": "11111", "nombre": "LEGO test", "tienda": "A", "precio": 38.0, "url": "http://x"}
    msg = redactar(p, an, an["normal"] * 0.9, 12.0, 31.0, "BUENA", "", False)
    assert "Navidad pasada llegó a 35.00" in msg and "Fiabilidad: ALTA" in msg, msg

    # Historial corto (BAJA): se apoya en otras tiendas
    con2 = sqlite3.connect(":memory:")
    crear_tablas(con2)
    an2 = analizar(con2, "22222", ahora, [60.0, 62.0], run_ts, cfg)
    assert an2["confianza"] == "BAJA" and an2["normal"] == 62.0, an2
    print("OK: lectura de listados, filtros, histórico de 12 meses, Navidad pasada y reporte")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=0, help="segundos entre pasadas (0 = una vez)")
    ap.add_argument("--probar", metavar="URL", help="prueba la lectura de una página de listado")
    ap.add_argument("--informe", metavar="SET", help="muestra el análisis de un set")
    ap.add_argument("--importar", metavar="CSV", help="importa historial de precios desde un CSV")
    ap.add_argument("--telegram-test", action="store_true")
    ap.add_argument("--test", action="store_true")
    a = ap.parse_args()
    if a.test:
        self_test()
        sys.exit(0)
    if a.probar:
        probar(a.probar)
        sys.exit(0)
    if a.importar:
        importar_csv(a.importar)
        sys.exit(0)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = json.load(f)
    if a.informe:
        informe(config, a.informe)
        sys.exit(0)
    if a.telegram_test:
        notify(config, "Prueba del bot de LEGO: si lees esto, Telegram está bien configurado.")
        sys.exit(0)
    while True:
        run_once(config)
        if not a.loop:
            break
        time.sleep(a.loop)
