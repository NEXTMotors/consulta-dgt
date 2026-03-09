from flask import Flask, render_template, request, jsonify, Response, stream_with_context
import requests, zipfile, io, os, json
from collections import defaultdict
from datetime import datetime
import psycopg2, psycopg2.extras

app = Flask(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")

def get_db():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS matriculaciones (
                    id SERIAL PRIMARY KEY,
                    anio INTEGER, mes INTEGER,
                    marca TEXT, modelo TEXT, tipo TEXT,
                    propulsion TEXT, cilindrada INTEGER,
                    ciudad TEXT, provincia TEXT,
                    persona TEXT, renting TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_marca    ON matriculaciones(marca);
                CREATE INDEX IF NOT EXISTS idx_anio_mes ON matriculaciones(anio, mes);
            """)
            conn.commit()

def mes_ya_cargado(anio, mes):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM matriculaciones WHERE anio=%s AND mes=%s", (anio, mes))
            return cur.fetchone()[0] > 0

def guardar_registros(registros):
    if not registros:
        return
    with get_db() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(cur, """
                INSERT INTO matriculaciones
                (anio, mes, marca, modelo, tipo, propulsion, cilindrada, ciudad, provincia, persona, renting)
                VALUES %s
            """, [(r['anio_num'], r['mes_num'], r['marca'], r['modelo'], r['tipo'],
                   r['propulsion'], r['cilindrada'], r['ciudad'], r['provincia'],
                   r['persona'], r['renting']) for r in registros])
            conn.commit()

_cache_marcas = None
_cache_fecha  = None

def cargar_marcas_modelos():
    global _cache_marcas, _cache_fecha
    hoy = datetime.now().date()
    if _cache_marcas and _cache_fecha == hoy:
        return _cache_marcas
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT marca, modelo FROM matriculaciones ORDER BY marca, modelo")
                rows = cur.fetchall()
        result = defaultdict(list)
        for marca, modelo in rows:
            if modelo:
                result[marca].append(modelo)
        _cache_marcas = dict(result)
        _cache_fecha  = hoy
        return _cache_marcas
    except Exception:
        return {}

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

@app.route("/")
def index():
    try:
        init_db()
    except Exception:
        pass
    return render_template("index.html",
        provincias=sorted(COD_PROVINCIA.values()),
        tipos=sorted(set(COD_TIPO.values())),
        propulsiones=sorted(set(COD_PROP.values())),
        anio_actual=datetime.now().year
    )

@app.route("/marcas")
def marcas():
    return jsonify(cargar_marcas_modelos())

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
        # Descargar meses que no están en la BD
        for anio in range(anio_desde, anio_hasta + 1):
            mes_max = 12 if anio < anio_hoy else mes_hoy - 1
            m_ini = mes_desde if anio == anio_desde else 1
            m_fin = min(mes_hasta, mes_max) if anio == anio_hasta else mes_max
            for mes in range(m_ini, m_fin + 1):
                if mes_ya_cargado(anio, mes):
                    yield f"data: {json.dumps({'tipo':'progreso','texto':f'{MESES[mes]} {anio}: ya guardado ✓'})}\n\n"
                    continue
                yield f"data: {json.dumps({'tipo':'progreso','texto':f'Descargando {MESES[mes]} {anio}...'})}" + "\n\n"
                for intento in range(3):
                    try:
                        if intento > 0:
                            yield f"data: {json.dumps({'tipo':'progreso','texto':f'Reintentando {MESES[mes]} {anio} ({intento+1}/3)...'})}" + "\n\n"
                        r = requests.get(BASE_URL.format(a=anio, m=mes), timeout=120)
                        if r.status_code != 200:
                            yield f"data: {json.dumps({'tipo':'progreso','texto':f'{MESES[mes]} {anio}: no disponible'})}" + "\n\n"
                            break
                        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                            contenido = z.read(z.namelist()[0])
                        registros = procesar_zip(contenido, anio, mes)
                        guardar_registros(registros)
                        yield f"data: {json.dumps({'tipo':'progreso','texto':f'{MESES[mes]} {anio}: {len(registros):,} registros guardados'})}" + "\n\n"
                        break
                    except Exception:
                        if intento == 2:
                            yield f"data: {json.dumps({'tipo':'progreso','texto':f'{MESES[mes]} {anio}: omitido tras 3 intentos'})}" + "\n\n"

        # Consultar BD con filtros
        yield f"data: {json.dumps({'tipo':'progreso','texto':'Consultando base de datos...'})}\n\n"
        try:
            conditions = ["anio BETWEEN %s AND %s", "mes BETWEEN %s AND %s"]
            params     = [anio_desde, anio_hasta, mes_desde, mes_hasta]
            if marca:      conditions.append("UPPER(marca) LIKE %s");     params.append(f"%{marca}%")
            if modelo:     conditions.append("UPPER(modelo) LIKE %s");    params.append(f"%{modelo}%")
            if ciudad:     conditions.append("UPPER(ciudad) LIKE %s");    params.append(f"%{ciudad}%")
            if provincia:  conditions.append("provincia = %s");           params.append(provincia)
            if tipo:       conditions.append("tipo = %s");                params.append(tipo)
            if propulsion: conditions.append("propulsion = %s");          params.append(propulsion)
            if persona:    conditions.append("persona = %s");             params.append(persona)
            if renting:    conditions.append("renting = %s");             params.append(renting)

            where = " AND ".join(conditions)
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT COUNT(*) FROM matriculaciones WHERE {where}", params)
                    total = cur.fetchone()[0]
                    cur.execute(f"""
                        SELECT anio, mes, marca, modelo, tipo, propulsion,
                               cilindrada, ciudad, provincia, persona, renting
                        FROM matriculaciones WHERE {where}
                        ORDER BY anio, mes LIMIT 500
                    """, params)
                    rows = cur.fetchall()

            resultados = [{
                "anio": r[0], "mes": MESES[r[1]],
                "marca": r[2], "modelo": r[3], "tipo": r[4],
                "propulsion": r[5], "cilindrada": r[6] if r[6] else "-",
                "ciudad": r[7], "provincia": r[8],
                "persona": r[9], "renting": r[10],
            } for r in rows]

            resumen = defaultdict(lambda: defaultdict(int))
            for reg in resultados:
                resumen[f"{reg['marca']} {reg['modelo']}".strip()][reg['anio']] += 1
            anios = sorted(set(r['anio'] for r in resultados)) if resultados else []

            yield f"data: {json.dumps({'tipo':'resultado','total':total,'meses_procesados':len(rows),'anios':anios,'resumen':[{'modelo':k,'totales':dict(v),'total':sum(v.values())} for k,v in sorted(resumen.items(),key=lambda x:sum(x[1].values()),reverse=True)],'registros':resultados})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'tipo':'error','texto':str(e)})}\n\n"

    return Response(stream_with_context(generar()), mimetype="text/event-stream")

if __name__ == "__main__":
    app.run(debug=True)
