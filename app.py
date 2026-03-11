from flask import Flask, render_template, request, jsonify, Response, stream_with_context
import requests, zipfile, io, os, json
from collections import defaultdict
from datetime import datetime

app = Flask(__name__)

# ── Supabase REST API ─────────────────────────────────────────────────────────
SUPABASE_URL    = os.environ.get("SUPABASE_URL")     # https://xxxx.supabase.co
SUPABASE_KEY    = os.environ.get("SUPABASE_KEY")     # anon public key

def sb_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal"
    }

def sb_get(table, params=None):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}",
                     headers=sb_headers(), params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def sb_post(table, data):
    r = requests.post(f"{SUPABASE_URL}/rest/v1/{table}",
                      headers=sb_headers(), json=data, timeout=60)
    r.raise_for_status()

def sb_rpc(func, params):
    r = requests.post(f"{SUPABASE_URL}/rest/v1/rpc/{func}",
                      headers={**sb_headers(), "Prefer": ""},
                      json=params, timeout=30)
    r.raise_for_status()
    return r.json()

def init_db():
    # Crear tabla via SQL usando la API de Supabase
    sql = """
    CREATE TABLE IF NOT EXISTS matriculaciones (
        id BIGSERIAL PRIMARY KEY,
        anio INTEGER, mes INTEGER,
        marca TEXT, modelo TEXT, tipo TEXT,
        propulsion TEXT, cilindrada INTEGER,
        ciudad TEXT, provincia TEXT,
        persona TEXT, renting TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_marca ON matriculaciones(marca);
    CREATE INDEX IF NOT EXISTS idx_anio_mes ON matriculaciones(anio, mes);
    """
    # Intentar via RPC exec_sql si existe, sino ignorar
    try:
        requests.post(f"{SUPABASE_URL}/rest/v1/rpc/exec_sql",
                      headers=sb_headers(), json={"sql": sql}, timeout=10)
    except Exception:
        pass

def mes_ya_cargado(anio, mes):
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/matriculaciones",
        headers={**sb_headers(), "Prefer": "count=exact"},
        params={"anio": f"eq.{anio}", "mes": f"eq.{mes}", "select": "id", "limit": "1"},
        timeout=15
    )
    count = int(r.headers.get("Content-Range", "0/0").split("/")[-1])
    return count > 0

def guardar_registros(registros):
    if not registros:
        return
    # Insertar en lotes de 500
    batch_size = 500
    for i in range(0, len(registros), batch_size):
        lote = registros[i:i+batch_size]
        payload = [{
            "anio": r["anio_num"], "mes": r["mes_num"],
            "marca": r["marca"], "modelo": r["modelo"],
            "tipo": r["tipo"], "propulsion": r["propulsion"],
            "cilindrada": r["cilindrada"], "ciudad": r["ciudad"],
            "provincia": r["provincia"], "persona": r["persona"],
            "renting": r["renting"]
        } for r in lote]
        requests.post(
            f"{SUPABASE_URL}/rest/v1/matriculaciones",
            headers={**sb_headers(), "Prefer": "resolution=ignore-duplicates"},
            json=payload, timeout=60
        )

def consultar_bd(conditions_dict, anio_desde, anio_hasta, mes_desde, mes_hasta):
    params = {
        "anio": f"gte.{anio_desde}",
        "mes":  f"gte.{mes_desde}",
        "select": "*",
        "order": "anio,mes",
        "limit": "500"
    }
    params["anio"] = f"gte.{anio_desde}"

    # Supabase REST no soporta BETWEEN directamente, usamos gte/lte
    base_params = [
        ("anio", f"gte.{anio_desde}"),
        ("anio", f"lte.{anio_hasta}"),
        ("mes",  f"gte.{mes_desde}"),
        ("mes",  f"lte.{mes_hasta}"),
        ("select", "*"),
        ("order", "anio,mes"),
        ("limit", "500"),
    ]

    for key, val in conditions_dict.items():
        base_params.append((key, val))

    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/matriculaciones",
        headers={**sb_headers(), "Prefer": "count=exact"},
        params=base_params,
        timeout=30
    )
    total = int(r.headers.get("Content-Range", "0/0").split("/")[-1])
    return r.json(), total

# ── Cache marcas ──────────────────────────────────────────────────────────────
_cache_marcas = None
_cache_fecha  = None

def cargar_marcas_modelos():
    global _cache_marcas, _cache_fecha
    hoy = datetime.now().date()
    if _cache_marcas and _cache_fecha == hoy:
        return _cache_marcas
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/rpc/get_marcas",
            headers={**sb_headers(), "Prefer": ""},
            json={}, timeout=30
        )
        r.raise_for_status()
        marcas = r.json()  # lista de marcas
        if not marcas:
            return {}
        # Devolver dict marca -> [] (modelos se cargan bajo demanda)
        _cache_marcas = {m: [] for m in sorted(marcas)}
        _cache_fecha  = hoy
        return _cache_marcas
    except Exception:
        return {}

# ── Tablas de códigos DGT ─────────────────────────────────────────────────────
COD_PROP = {
    "0":"Gasolina","1":"Diésel","2":"Eléctrico","3":"Otros",
    "4":"Butano","5":"Solar","6":"Gas Licuado (GLP)",
    "7":"Gas Natural Comprimido","8":"Gas Natural Licuado",
    "9":"Hidrógeno","A":"Biometano","B":"Etanol","C":"Biodiesel",
}
COD_TIPO = {
    "40":"Turismo","25":"Todo Terreno","20":"Furgoneta","21":"Furgoneta Mixta",
    "50":"Motocicleta","51":"Motocicleta con Sidecar",
    "90":"Ciclomotor 2 ruedas","91":"Ciclomotor 3 ruedas","92":"Cuatriciclo Ligero",
    "54":"Cuatriciclo Pesado","53":"Automóvil 3 ruedas",
    "30":"Autobús","31":"Autobús Articulado","00":"Camión",
    "24":"Camioneta","80":"Tractor","70":"Vehículo Especial",
}
COD_PROVINCIA = {
    "A":"Alicante","AB":"Albacete","AL":"Almería","AV":"Ávila",
    "B":"Barcelona","BA":"Badajoz","BI":"Bizkaia","BU":"Burgos",
    "C":"A Coruña","CA":"Cádiz","CC":"Cáceres","CE":"Ceuta",
    "CO":"Córdoba","CR":"Ciudad Real","CS":"Castellón","CU":"Cuenca",
    "GC":"Las Palmas","GI":"Girona","GR":"Granada","GU":"Guadalajara",
    "H":"Huelva","HU":"Huesca","IB":"Illes Balears","J":"Jaén",
    "L":"Lleida","LE":"León","LO":"La Rioja","LU":"Lugo",
    "M":"Madrid","MA":"Málaga","ML":"Melilla","MU":"Murcia",
    "NA":"Navarra","O":"Asturias","OU":"Ourense","P":"Palencia",
    "PO":"Pontevedra","S":"Cantabria","SA":"Salamanca","SE":"Sevilla",
    "SG":"Segovia","SO":"Soria","SS":"Gipuzkoa","T":"Tarragona",
    "TE":"Teruel","TF":"Santa Cruz de Tenerife","TO":"Toledo",
    "V":"Valencia","VA":"Valladolid","VI":"Álava","Z":"Zaragoza","ZA":"Zamora",
}
MESES = ["","Enero","Febrero","Marzo","Abril","Mayo","Junio",
         "Julio","Agosto","Septiembre","Octubre","Noviembre","Diciembre"]
BASE_URL = "https://www.dgt.es/microdatos/salida/{a}/{m}/vehiculos/matriculaciones/export_mensual_mat_{a}{m:02d}.zip"

POS = dict(
    marca=(17,30), modelo=(47,22), cod_tipo=(91,2), prop=(93,1),
    cil=(94,5), localidad=(128,24), prov=(152,2), persona=(198,1), renting=(284,1)
)

def leer_campo(linea, campo):
    p, l = POS[campo]
    return linea[p:p+l].strip()

def procesar_zip(contenido, anio, mes):
    registros = []
    for linea in contenido.decode("latin1").split("\n"):
        if len(linea) < 285:
            continue
        marca = leer_campo(linea, "marca")
        if not marca:
            continue
        ci_raw = leer_campo(linea, "cil")
        pe_raw = leer_campo(linea, "persona")
        re_raw = leer_campo(linea, "renting")
        registros.append({
            "anio_num": anio, "mes_num": mes,
            "marca":    marca,
            "modelo":   leer_campo(linea, "modelo"),
            "tipo":     COD_TIPO.get(leer_campo(linea, "cod_tipo"), "Desconocido"),
            "propulsion": COD_PROP.get(leer_campo(linea, "prop"), "Desconocido"),
            "cilindrada": int(ci_raw) if ci_raw.isdigit() else 0,
            "ciudad":   leer_campo(linea, "localidad"),
            "provincia": COD_PROVINCIA.get(leer_campo(linea, "prov"), "Desconocido"),
            "persona":  "Física" if pe_raw == "D" else "Jurídica" if pe_raw == "X" else "",
            "renting":  "Sí" if re_raw == "S" else "No",
        })
    return registros

# ── Rutas ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html",
        provincias=sorted(COD_PROVINCIA.values()),
        tipos=sorted(set(COD_TIPO.values())),
        propulsiones=sorted(set(COD_PROP.values())),
        anio_actual=datetime.now().year
    )

@app.route("/test")
def test():
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/matriculaciones",
                         headers=sb_headers(),
                         params={"select": "id", "limit": "1"}, timeout=10)
        return jsonify({"status": "ok", "supabase": r.status_code, "url": SUPABASE_URL})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)})

@app.route("/marcas")
def marcas():
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/rpc/get_marcas",
            headers={**sb_headers(), "Prefer": ""},
            json={}, timeout=30
        )
        raw = r.text[:500]  # primeros 500 chars
        data = r.json()
        tipo = type(data).__name__
        longitud = len(data) if data else 0
        return jsonify({"status": r.status_code, "tipo": tipo, "longitud": longitud, "muestra": raw})
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/modelos")
def modelos():
    marca = request.args.get("marca", "").strip()
    if not marca:
        return jsonify([])
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/rpc/get_modelos",
            headers={**sb_headers(), "Prefer": ""},
            json={"p_marca": marca}, timeout=15
        )
        r.raise_for_status()
        return jsonify(r.json() or [])
    except Exception:
        return jsonify([])

@app.route("/consulta", methods=["POST"])
def consulta():
    data       = request.json
    marca      = data.get("marca","").strip().upper()
    modelo     = data.get("modelo","").strip().upper()
    ciudad     = data.get("ciudad","").strip().upper()
    provincia  = data.get("provincia","").strip()
    tipo       = data.get("tipo","").strip()
    propulsion = data.get("propulsion","").strip()
    persona    = data.get("persona","").strip()
    renting    = data.get("renting","").strip()
    anio_desde = int(data.get("anio_desde", 2024))
    anio_hasta = int(data.get("anio_hasta", 2024))
    mes_desde  = int(data.get("mes_desde", 1))
    mes_hasta  = int(data.get("mes_hasta", 12))
    anio_hoy   = datetime.now().year
    mes_hoy    = datetime.now().month

    def generar():
        # Descargar meses que faltan
        for anio in range(anio_desde, anio_hasta + 1):
            mes_max = 12 if anio < anio_hoy else mes_hoy - 1
            m_ini = mes_desde if anio == anio_desde else 1
            m_fin = min(mes_hasta, mes_max) if anio == anio_hasta else mes_max
            for mes in range(m_ini, m_fin + 1):
                try:
                    if mes_ya_cargado(anio, mes):
                        yield "data: " + json.dumps({"tipo":"progreso","texto":f"{MESES[mes]} {anio}: ya guardado ✓"}) + "\n\n"
                        continue
                except Exception:
                    pass

                yield "data: " + json.dumps({"tipo":"progreso","texto":f"Descargando {MESES[mes]} {anio}..."}) + "\n\n"
                for intento in range(3):
                    try:
                        if intento > 0:
                            yield "data: " + json.dumps({"tipo":"progreso","texto":f"Reintentando {MESES[mes]} {anio} ({intento+1}/3)..."}) + "\n\n"
                        r = requests.get(BASE_URL.format(a=anio, m=mes), timeout=120)
                        if r.status_code != 200:
                            yield "data: " + json.dumps({"tipo":"progreso","texto":f"{MESES[mes]} {anio}: no disponible"}) + "\n\n"
                            break
                        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                            contenido = z.read(z.namelist()[0])
                        registros = procesar_zip(contenido, anio, mes)
                        guardar_registros(registros)
                        yield "data: " + json.dumps({"tipo":"progreso","texto":f"{MESES[mes]} {anio}: {len(registros):,} registros guardados ✓"}) + "\n\n"
                        break
                    except Exception as e:
                        if intento == 2:
                            yield "data: " + json.dumps({"tipo":"progreso","texto":f"{MESES[mes]} {anio}: omitido ({str(e)[:50]})"}) + "\n\n"

        # Consultar Supabase
        yield "data: " + json.dumps({"tipo":"progreso","texto":"Consultando base de datos..."}) + "\n\n"
        try:
            conditions = []
            if marca:      conditions.append(("marca",      f"ilike.*{marca}*"))
            if modelo:     conditions.append(("modelo",     f"ilike.*{modelo}*"))
            if ciudad:     conditions.append(("ciudad",     f"ilike.*{ciudad}*"))
            if provincia:  conditions.append(("provincia",  f"eq.{provincia}"))
            if tipo:       conditions.append(("tipo",       f"eq.{tipo}"))
            if propulsion: conditions.append(("propulsion", f"eq.{propulsion}"))
            if persona:    conditions.append(("persona",    f"eq.{persona}"))
            if renting:    conditions.append(("renting",    f"eq.{renting}"))

            rows, total = consultar_bd(dict(conditions), anio_desde, anio_hasta, mes_desde, mes_hasta)

            resultados = [{
                "anio": r["anio"], "mes": MESES[r["mes"]],
                "marca": r["marca"], "modelo": r["modelo"], "tipo": r["tipo"],
                "propulsion": r["propulsion"],
                "cilindrada": r["cilindrada"] if r["cilindrada"] else "-",
                "ciudad": r["ciudad"], "provincia": r["provincia"],
                "persona": r["persona"], "renting": r["renting"],
            } for r in rows]

            resumen = defaultdict(lambda: defaultdict(int))
            for reg in resultados:
                resumen[f"{reg['marca']} {reg['modelo']}".strip()][reg['anio']] += 1
            anios = sorted(set(r['anio'] for r in resultados)) if resultados else []

            yield "data: " + json.dumps({
                "tipo": "resultado",
                "total": total,
                "meses_procesados": len(set((r['anio'], r['mes']) for r in resultados)),
                "anios": anios,
                "resumen": [{"modelo":k,"totales":dict(v),"total":sum(v.values())}
                            for k,v in sorted(resumen.items(), key=lambda x: sum(x[1].values()), reverse=True)],
                "registros": resultados
            }) + "\n\n"

        except Exception as e:
            yield "data: " + json.dumps({"tipo":"error","texto":f"Error en consulta: {str(e)}"}) + "\n\n"

    return Response(stream_with_context(generar()), mimetype="text/event-stream")

if __name__ == "__main__":
    app.run(debug=True)
