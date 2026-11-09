#!/usr/bin/env python3
"""
Sincronizador SINAC -> JSON para la página "Calidad del agua" de Dinae Aqua.

Recorre el acceso ciudadano del SINAC (Ministerio de Sanidad):
  comunidad > provincia > municipios > redes de distribución > ficha de red > análisis completos
y genera un JSON normalizado por provincia en data/provincia-<cod>.json.

Fuente: SINAC, Ministerio de Sanidad (https://sinac.sanidad.gob.es/CiudadanoWeb/).
Reutilización autorizada por el aviso legal del Ministerio citando la fuente y la fecha
de actualización, sin desnaturalizar los datos.

Uso:
  python sync/sinac_sync.py --ccaa 1 --provincia 29
  python sync/sinac_sync.py --ccaa 1 --provincia 29 --municipios 29067,29069   (prueba)

Diseño:
- Peticiones lentas y espaciadas (el portal es lento y es un servicio público).
- Los boletines no cambian una vez publicados: se guardan en cache/boletines/<id>.json
  y no se vuelven a pedir.
- Se guarda un histórico de boletines vistos por red (cache/historial/<idRed>.json) para
  poder contar resultados de los últimos 12 meses, ya que el SINAC solo muestra los 10 últimos.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import re
import sys
import time
import unicodedata
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://sinac.sanidad.gob.es/CiudadanoWeb/ciudadano"
UA = "DinaeAqua-CalidadAgua/1.0 (+https://dinaeaqua.com/calidad-del-agua/)"
ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "cache"
DATA = ROOT / "data"

# La leyenda varía entre redes ("Análisis completo" / "Análisis de completo"): se detecta por palabra clave.
TIPOS_BOLETIN = {
    "control": r"^an[áa]lisis\b.*\bcontrol\b",
    "completo": r"^an[áa]lisis\b.*\bcompleto\b",
    "radiactividad": r"^an[áa]lisis\b.*\bradiactividad\b",
    "vigilancia": r"^an[áa]lisis\b.*\bvigilancia\b",
}


# ---------------------------------------------------------------- utilidades
def log(*a):
    print(dt.datetime.now().strftime("%H:%M:%S"), *a, flush=True)


def decode(resp: requests.Response) -> str:
    # El portal declara ISO-8859-1 pero sirve UTF-8; hay restos sueltos en Latin-1.
    raw = resp.content
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def txt(el) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip() if el else ""


def num_es(s: str):
    """'137.870' -> 137870 ; '202,39' -> 202.39 (formato español)."""
    s = (s or "").strip()
    if not s:
        return None
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def num_param(s: str):
    """Valor de parámetro del SINAC: decimal con punto. Devuelve (valor, menor_que)."""
    s = (s or "").strip().replace(",", ".")
    menor = s.startswith("<")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None, menor
    return float(m.group(0)), menor


def fecha_iso(s: str):
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", (s or "").strip())
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None


PARTICULAS = {"de", "del", "la", "las", "los", "el", "y", "en", "a"}


def nombre_bonito(s: str) -> str:
    """'BORGE (EL)' -> 'El Borge' ; 'VÉLEZ-MÁLAGA' -> 'Vélez-Málaga'."""
    s = s.strip()
    m = re.match(r"^(.*)\s+\((EL|LA|LOS|LAS)\)$", s, flags=re.I)
    if m:
        s = f"{m.group(2)} {m.group(1)}"
    palabras = s.lower().split(" ")
    out = []
    for i, p in enumerate(palabras):
        if i > 0 and p in PARTICULAS:
            out.append(p)
        else:
            out.append("-".join(x[:1].upper() + x[1:] for x in p.split("-")))
    return " ".join(out)


def slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s


# ---------------------------------------------------------------- cliente
class Sinac:
    def __init__(self, delay: float = 2.5, timeout: int = 180):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "es-ES,es;q=0.9"})
        self.delay = delay
        self.timeout = timeout
        self.n = 0

    def _pausa(self):
        time.sleep(self.delay + random.uniform(0, self.delay / 2))

    def req(self, method: str, path: str, data=None, intentos: int = 4) -> str:
        url = f"{BASE}/{path}"
        for i in range(1, intentos + 1):
            self._pausa()
            try:
                r = self.s.request(method, url, data=data, timeout=self.timeout)
                self.n += 1
                if r.status_code == 200 and r.content:
                    return decode(r)
                log(f"  aviso: HTTP {r.status_code} en {path} (intento {i})")
            except requests.RequestException as e:
                log(f"  aviso: {type(e).__name__} en {path} (intento {i})")
            time.sleep(min(60, 5 * i * i))
        raise RuntimeError(f"No se pudo obtener {path} tras {intentos} intentos")

    def iniciar(self):
        self.req("GET", "informacionAbastecimientoActionEntrada.do")

    def opciones(self, path: str, ident: str) -> list[tuple[str, str]]:
        html = self.req("POST", path, data={"id": ident})
        soup = BeautifulSoup(html, "html.parser")
        return [(o.get("value"), txt(o)) for o in soup.find_all("option") if o.get("value")]

    def form(self, ccaa, prov, muni, **extra):
        d = {
            "provinciaMapa": "",
            "codComunidad": ccaa,
            "codProvincia": prov,
            "codMunicipio": muni,
            "denComunidad": "",
            "denProvincia": "",
            "denMunicipio": "",
        }
        d.update(extra)
        return d

    def redes(self, ccaa, prov, muni) -> list[dict]:
        html = self.req("POST", "informacionRedes.do", data=self.form(ccaa, prov, muni, method="Buscar"))
        soup = BeautifulSoup(html, "html.parser")
        out = []
        for a in soup.find_all("a", href=re.compile(r"eleccionRedDistribucion\(\d+\)")):
            out.append({"id": int(re.search(r"\((\d+)\)", a["href"]).group(1)), "nombre": txt(a)})
        return out

    def detalle_red(self, ccaa, prov, muni, id_red) -> dict:
        html = self.req(
            "POST",
            "informacionAbastecimientoActionDetalleRed.do",
            data=self.form(ccaa, prov, muni, idRed=id_red),
        )
        return parse_red(html, id_red)

    def detalle_boletin(self, ccaa, prov, muni, id_red, id_bol) -> dict:
        html = self.req(
            "POST",
            "detalleBoletin.do",
            data=self.form(ccaa, prov, muni, idRed=id_red, idBoletin=id_bol),
        )
        return parse_boletin(html, id_bol)


# ---------------------------------------------------------------- parsers
def _fieldsets(soup):
    """Devuelve {legend: fieldset} de los bloques principales (bloqueFieldsetU)."""
    out = {}
    for fs in soup.select("fieldset.bloqueFieldsetU"):
        lg = fs.find("legend")
        if lg:
            out.setdefault(txt(lg), fs)
    return out


def _tabla(fs):
    tb = fs.find("table") if fs else None
    if not tb:
        return [], []
    hdr = [txt(th) for th in tb.select("thead th")]
    filas = []
    for tr in tb.select("tbody tr"):
        tds = tr.find_all("td")
        if tds:
            filas.append(tds)
    return hdr, filas


def parse_red(html: str, id_red: int) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    fss = _fieldsets(soup)
    red = {"id": id_red}

    den = soup.find("input", attrs={"name": "denRed"})
    red["nombre"] = (den.get("value") or "").strip() if den else ""

    # Red: gestor, responsable, localidades
    fs = fss.get("Red")
    if fs:
        for tr in fs.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) == 2:
                k, v = txt(tds[0]).lower(), txt(tds[1])
                if k.startswith("gestor"):
                    red["gestor"] = v
                elif k.startswith("responsable"):
                    red["responsable"] = v

    # Suministro
    fs = fss.get("Suministro")
    red["localidades"], red["poblacion"], red["dotacion_l_hab_dia"] = [], 0, None
    if fs:
        _, filas = _tabla(fs)
        for tds in filas:
            if len(tds) >= 3:
                pob = num_es(txt(tds[1])) or 0
                red["localidades"].append(
                    {"nombre": nombre_bonito(txt(tds[0])), "poblacion": int(pob), "pct": num_es(txt(tds[2]))}
                )
                red["poblacion"] += int(pob)
        m = re.search(r"DOTACION\s*([\d.,]+)", txt(fs), flags=re.I)
        if m:
            red["dotacion_l_hab_dia"] = num_es(m.group(1))

    # Origen del agua: "Río/Embalse/Canal (5) Lago - Laguna (1) Pozo Entubado (10)"
    fs = fss.get("Origen del Agua")
    red["origen"] = []
    if fs:
        cuerpo = txt(fs).replace("Origen del Agua", "", 1)
        for m in re.finditer(r"([^()]+?)\s*\((\d+)\)", cuerpo):
            red["origen"].append({"tipo": m.group(1).strip(" |"), "captaciones": int(m.group(2))})

    # Tratamiento de potabilización: procesos únicos
    fs = fss.get("Tratamiento de potabilización")
    procesos = []
    if fs:
        _, filas = _tabla(fs)
        for tds in filas:
            if len(tds) >= 2:
                for li in tds[1].find_all(["li", "span", "div"]) or []:
                    t = txt(li)
                    if t and t not in procesos:
                        procesos.append(t)
                if not tds[1].find_all(["li", "span", "div"]):
                    for t in re.split(r"\s{2,}|<br/?>", tds[1].get_text("\n", strip=True)):
                        for t2 in t.split("\n"):
                            t2 = t2.strip()
                            if t2 and t2 not in procesos:
                                procesos.append(t2)
    red["tratamientos"] = [p for p in procesos if p.lower() != "otros tratamientos"]

    # Factura
    fs = fss.get("Contenido de la factura del agua")
    red["factura"] = {}
    if fs:
        t = txt(fs)
        m = re.search(r"Euros/metro c[úu]bico:\s*([\d.,]+)", t)
        if m:
            red["factura"]["eur_m3"] = num_es(m.group(1))
        m = re.search(r"Emisor de la factura:\s*(.+?)\s*(?:Bonificaciones|Informaci[óo]n comparativa|$)", t)
        if m:
            red["factura"]["emisor"] = m.group(1).strip(" |")
        m = re.search(r"Otros conceptos\s*(.+?)\s*Tipo de factura", t)
        if m and m.group(1).strip():
            red["factura"]["otros_conceptos"] = m.group(1).strip(" |")

    # Boletines notificados (10 últimos de cada tipo)
    red["boletines"] = {}
    for clave, patron in TIPOS_BOLETIN.items():
        fs = next((f for k, f in fss.items() if re.search(patron, k, flags=re.I)), None)
        lista = []
        if fs:
            _, filas = _tabla(fs)
            for tds in filas:
                ver = fs and tds[2].find("a") if len(tds) > 2 else None
                if not ver:
                    continue
                idb = int(re.search(r"\((\d+)\)", ver.get("href", "")).group(1))
                lista.append({"id": idb, "fecha": fecha_iso(txt(tds[0])), "calificacion": calif(txt(tds[1]))})
        lista.sort(key=lambda b: (b["fecha"] or "", b["id"]), reverse=True)
        red["boletines"][clave] = lista

    # Último valor notificado de cada parámetro
    fs = next((f for k, f in fss.items() if k.lower().startswith("últimos valor")), None)
    params = []
    if fs:
        _, filas = _tabla(fs)
        for tds in filas:
            if len(tds) < 3:
                continue
            cod, nombre_p, valor_txt = txt(tds[0]), txt(tds[1]), txt(tds[2])
            if "sin datos" in valor_txt.lower():
                continue
            valor, menor = num_param(valor_txt)
            params.append(
                {
                    "cod": cod,
                    "nombre": nombre_p,
                    "valor": valor,
                    "menor_que": menor,
                    "valor_txt": valor_txt,
                    "unidad": txt(tds[3]) if len(tds) > 3 else "",
                    "fecha": fecha_iso(txt(tds[4])) if len(tds) > 4 else None,
                    "laboratorio": txt(tds[5]) if len(tds) > 5 else "",
                }
            )
    red["parametros"] = params
    return red


def calif(s: str) -> str:
    s = s.upper()
    if "NO APTA" in s:
        return "NO APTA"
    if "PENDIENTE" in s:
        return "APTA (PENDIENTE DE PERMISO)"
    if "APTA" in s:
        return "APTA"
    if "CONSEJER" in s or "COMUNIDAD" in s:
        return "GESTIONADA POR LA CONSEJERÍA"
    return "SIN CALIFICAR"


def parse_boletin(html: str, id_bol: int) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    plano = txt(soup)

    def campo(etq, sig):
        m = re.search(re.escape(etq) + r"\s*(.+?)\s*(?:" + "|".join(map(re.escape, sig)) + ")", plano)
        return m.group(1).strip() if m else ""

    b = {
        "id": id_bol,
        "red": campo("Red", ["Punto de muestreo"]),
        "punto_muestreo": campo("Punto de muestreo", ["Municipio"]),
        "zona": campo("Zona abastecimiento", ["Fecha de toma"]),
        "fecha": fecha_iso(campo("Fecha de toma", ["Tipo de Boletin"])),
        "tipo_boletin": campo("Tipo de Boletin", ["Tipo de analisis"]),
        "tipo_analisis": campo("Tipo de analisis", ["Laboratorio/s"]),
        "laboratorios": campo("Laboratorio/s", ["Calificacion de la muestra"]),
        "calificacion": calif(campo("Calificacion de la muestra", ["Parametros", "Parámetros"])),
        "parametros": [],
    }
    for tb in soup.find_all("table"):
        hdr = [txt(th) for th in tb.find_all("th")]
        if not any("Valor cuantificado" in h for h in hdr):
            continue
        # el grupo (obligatorios/opcionales) es el texto de la leyenda/caption más cercana
        prev = tb.find_previous(string=re.compile(r"Par[aá]metros (Obligatorios|opcionales)", re.I))
        grupo = "opcional" if prev and "opcional" in prev.lower() else "obligatorio"
        for tr in tb.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 2:
                continue
            nombre_p, valor_txt = txt(tds[0]), txt(tds[1])
            valor, menor = num_param(valor_txt)
            b["parametros"].append(
                {
                    "nombre": nombre_p,
                    "valor": valor,
                    "menor_que": menor,
                    "valor_txt": valor_txt,
                    "unidad": txt(tds[2]) if len(tds) > 2 else "",
                    "grupo": grupo,
                }
            )
    return b


# ---------------------------------------------------------------- caché
def cache_get(tipo: str, ident) -> dict | None:
    p = CACHE / tipo / f"{ident}.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return None


def cache_put(tipo: str, ident, obj):
    p = CACHE / tipo / f"{ident}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def actualizar_historial(red: dict) -> list[dict]:
    """Une los boletines vistos hoy con los de semanas anteriores (sin duplicados)."""
    previo = cache_get("historial", red["id"]) or {"boletines": []}
    vistos = {b["id"]: b for b in previo["boletines"]}
    for tipo, lista in red["boletines"].items():
        for b in lista:
            vistos[b["id"]] = {**b, "tipo": tipo}
    todos = sorted(vistos.values(), key=lambda b: (b.get("fecha") or "", b["id"]), reverse=True)
    cache_put("historial", red["id"], {"red": red["id"], "boletines": todos})
    return todos


# ---------------------------------------------------------------- proceso
def resumen_historial(hist: list[dict], hoy: dt.date) -> dict:
    hace12 = (hoy - dt.timedelta(days=365)).isoformat()
    ult = [b for b in hist if (b.get("fecha") or "") >= hace12]
    return {
        "boletines_12m": len(ult),
        "no_apta_12m": sum(1 for b in ult if b["calificacion"] == "NO APTA"),
        "desde": min((b["fecha"] for b in hist if b.get("fecha")), default=None),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ccaa", default="1")
    ap.add_argument("--provincia", default="29")
    ap.add_argument("--municipios", default="", help="códigos INE separados por comas (prueba)")
    ap.add_argument("--completos", type=int, default=3, help="análisis completos con detalle por red")
    ap.add_argument("--delay", type=float, default=2.5)
    ap.add_argument("--max-minutos", type=int, default=330, help="corte de seguridad")
    args = ap.parse_args()

    inicio = time.time()
    hoy = dt.date.today()
    cli = Sinac(delay=args.delay)
    cli.iniciar()

    provs = dict(cli.opciones("cargarComboProvinciasAction.do", args.ccaa))
    comunidades = {
        "1": "Andalucía", "2": "Aragón", "3": "Principado de Asturias", "4": "Illes Balears",
        "5": "Canarias", "6": "Cantabria", "7": "Castilla y León", "8": "Castilla-La Mancha",
        "9": "Cataluña", "10": "Comunitat Valenciana", "11": "Extremadura", "12": "Galicia",
        "13": "Comunidad de Madrid", "14": "Región de Murcia", "15": "Comunidad Foral de Navarra",
        "16": "País Vasco", "17": "La Rioja", "18": "Ceuta", "19": "Melilla",
    }
    nombre_prov = nombre_bonito(provs.get(args.provincia, args.provincia))
    munis = cli.opciones("cargarComboMunicipiosAction.do", args.provincia)
    if args.municipios:
        filtro = set(args.municipios.split(","))
        munis = [m for m in munis if m[0] in filtro]
    log(f"Provincia {nombre_prov}: {len(munis)} municipios")

    redes_out: dict[str, dict] = {}
    munis_out = []
    errores = []

    # Datos de la ejecución anterior: si un municipio falla o se agota el tiempo, se conservan.
    previo_path = DATA / f"provincia-{args.provincia}.json"
    previo = {"municipios": [], "redes": {}}
    if previo_path.exists() and not args.municipios:
        try:
            previo = json.loads(previo_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    previo_munis = {m["ine"]: m for m in previo.get("municipios", [])}
    conservados = []

    def conservar(ine_c: str):
        m_old = previo_munis.get(ine_c)
        if not m_old:
            return False
        for rid in m_old["redes"]:
            if str(rid) in previo["redes"] and str(rid) not in redes_out:
                redes_out[str(rid)] = previo["redes"][str(rid)]
        munis_out.append({**m_old, "conservado": True})
        conservados.append(m_old["nombre"])
        return True

    for i, (ine, nombre_raw) in enumerate(munis, 1):
        nombre = nombre_bonito(nombre_raw)
        if (time.time() - inicio) / 60 > args.max_minutos:
            if not conservar(ine):
                errores.append(f"{nombre}: sin procesar por tiempo máximo")
            continue
        log(f"[{i}/{len(munis)}] {nombre} ({ine})")
        try:
            lista = cli.redes(args.ccaa, args.provincia, ine)
        except Exception as e:  # noqa: BLE001
            errores.append(f"{nombre}: {e}")
            conservar(ine)
            continue

        ids = []
        for r in lista:
            key = str(r["id"])
            if key not in redes_out:
                try:
                    red = cli.detalle_red(args.ccaa, args.provincia, ine, r["id"])
                except Exception as e:  # noqa: BLE001
                    errores.append(f"{nombre} / {r['nombre']}: {e}")
                    continue
                red["nombre"] = red.get("nombre") or r["nombre"]
                hist = actualizar_historial(red)
                red["historial"] = resumen_historial(hist, hoy)
                red["historial"]["no_apta_recientes"] = [
                    b for b in hist if b["calificacion"] == "NO APTA"
                ][:10]
                # Detalle de los últimos análisis completos (caché permanente por boletín)
                det = []
                # Redes privadas o sin población censada (aeropuertos, industrias): solo el último.
                n_com = args.completos if red.get("poblacion", 0) > 0 else 1
                for b in red["boletines"].get("completo", [])[:n_com]:
                    bol = cache_get("boletines", b["id"])
                    if not bol:
                        try:
                            bol = cli.detalle_boletin(args.ccaa, args.provincia, ine, r["id"], b["id"])
                            cache_put("boletines", b["id"], bol)
                        except Exception as e:  # noqa: BLE001
                            errores.append(f"Boletín {b['id']}: {e}")
                            continue
                    det.append(bol)
                red["completos"] = det
                redes_out[key] = red
            ids.append(r["id"])

        # Red principal: la que abastece a más población (las redes privadas suelen tener 0)
        candidatas = [redes_out[str(x)] for x in ids if str(x) in redes_out]
        principal = max(candidatas, key=lambda r: (r.get("poblacion", 0), len(r["parametros"])), default=None)
        munis_out.append(
            {
                "ine": ine,
                "nombre": nombre,
                "slug": slugify(nombre),
                "redes": ids,
                "red_principal": principal["id"] if principal else None,
                "poblacion_abastecida": sum(r.get("poblacion", 0) for r in candidatas),
            }
        )

    salida = {
        "version": 1,
        "generado": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "fuente": {
            "nombre": "SINAC, Sistema de Información Nacional de Agua de Consumo (Ministerio de Sanidad)",
            "url": "https://sinac.sanidad.gob.es/CiudadanoWeb/ciudadano/informacionAbastecimientoActionEntrada.do",
            "nota": "Elaboración de Dinae Aqua a partir de los datos públicos del SINAC. No es un servicio oficial.",
        },
        "comunidad": {"codigo": args.ccaa, "nombre": comunidades.get(args.ccaa, "")},
        "provincia": {"codigo": args.provincia, "nombre": nombre_prov, "slug": slugify(nombre_prov)},
        "municipios": munis_out,
        "redes": redes_out,
        "peticiones": cli.n,
        "errores": errores,
        "conservados_de_la_semana_anterior": conservados,
    }
    DATA.mkdir(parents=True, exist_ok=True)
    destino = DATA / f"provincia-{args.provincia}.json"
    # Si la ejecución es una prueba con filtro, no se pisa el fichero completo
    if args.municipios:
        destino = DATA / f"prueba-{args.provincia}.json"
    destino.write_text(json.dumps(salida, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log(
        f"OK: {len(munis_out)} municipios ({len(conservados)} conservados), {len(redes_out)} redes, "
        f"{cli.n} peticiones, {len(errores)} errores -> {destino}"
    )
    if errores:
        for e in errores[:30]:
            log("  error:", e)
    # Falla el workflow solo si no se ha podido obtener casi nada
    if len(munis_out) < max(1, len(munis) // 2):
        sys.exit(1)


if __name__ == "__main__":
    main()
