from flask import Flask, render_template, request, jsonify
import requests, zipfile, io
from collections import defaultdict
from datetime import datetime

app = Flask(__name__)

# Cache para no descargar en cada petición
_cache_marcas_modelos = None
_cache_fecha = None

def cargar_marcas_modelos():
    global _cache_marcas_modelos, _cache_fecha
    # Refrescar solo una vez al día
    hoy = datetime.now().date()
    if _cache_marcas_modelos and _cache_fecha == hoy:
        return _cache_marcas_modelos

    # Buscar el último mes disponible (hasta 6 meses atrás)
    anio_hoy = datetime.now().year
    mes_hoy  = datetime.now().month
    contenido = None
    for i in range(6):
        mes  = (mes_hoy - 1 - i) % 12 + 1
        anio = anio_hoy if (mes_hoy - 1 - i) >= 0 else anio_hoy - 1
        url  = BASE_URL.format(a=anio, m=mes)
        try:
            r = requests.get(url, timeout=30)
            if r.status_code == 200:
                with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                    contenido = z.read(z.namelist()[0])
                break
        except Exception:
            continue

    if not contenido:
        return {}

    marcas_modelos = defaultdict(set)
    for linea in contenido.decode("latin1").split("\n"):
        if len(linea) < 70:
            continue
        marca  = linea[17:47].strip()
        modelo = linea[47:69].strip()
        if marca:
            marcas_modelos[marca].add(modelo)

    result = {m: sorted(modelos) for m, modelos in sorted(marcas_modelos.items())}
    _cache_marcas_modelos = result
    _cache_fecha = hoy
    return result

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

@app.route("/")
def index():
    return render_template("index.html",
        provincias=sorted(COD_PROVINCIA.values()),
        tipos=sorted(set(COD_TIPO.values())),
        propulsiones=sorted(set(COD_PROP.values())),
        anio_actual=__import__('datetime').datetime.now().year
    )

@app.route("/marcas")
def marcas():
    datos = cargar_marcas_modelos()
    return jsonify(datos)

@app.route("/consulta", methods=["POST"])
def consulta():
    data = request.json
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

    anio_hoy = datetime.now().year
    mes_hoy  = datetime.now().month

    resultados = []
    meses_procesados = 0

    for anio in range(anio_desde, anio_hasta + 1):
        mes_max = 12 if anio < anio_hoy else mes_hoy - 1
        m_ini = mes_desde if anio == anio_desde else 1
        m_fin = min(mes_hasta, mes_max) if anio == anio_hasta else mes_max

        for mes in range(m_ini, m_fin + 1):
            try:
                r = requests.get(BASE_URL.format(a=anio, m=mes), timeout=30)
                if r.status_code != 200:
                    continue
                with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                    contenido = z.read(z.namelist()[0])
                for linea in contenido.decode("latin1").split("\n"):
                    if len(linea) < 285:
                        continue
                    m_val  = leer_campo(linea, "marca")
                    if not m_val:
                        continue
                    mo_val = leer_campo(linea, "modelo")
                    ti_val = COD_TIPO.get(leer_campo(linea, "cod_tipo"), "Desconocido")
                    pr_val = COD_PROP.get(leer_campo(linea, "prop"), "Desconocido")
                    ci_raw = leer_campo(linea, "cil")
                    lo_val = leer_campo(linea, "localidad")
                    pv_val = COD_PROVINCIA.get(leer_campo(linea, "prov"), "Desconocido")
                    pe_raw = leer_campo(linea, "persona")
                    re_raw = leer_campo(linea, "renting")

                    pe_val = "Física" if pe_raw == "D" else "Jurídica" if pe_raw == "X" else ""
                    re_val = "Sí" if re_raw == "S" else "No"
                    cil    = int(ci_raw) if ci_raw.isdigit() else 0

                    if marca      and marca      not in m_val.upper():   continue
                    if modelo     and modelo     not in mo_val.upper():  continue
                    if ciudad     and ciudad     not in lo_val.upper():  continue
                    if provincia  and provincia  != pv_val:              continue
                    if tipo       and tipo       != ti_val:              continue
                    if propulsion and propulsion != pr_val:              continue
                    if persona    and persona    != pe_val:              continue
                    if renting    and renting    != re_val:              continue

                    resultados.append({
                        "anio": anio, "mes": MESES[mes],
                        "marca": m_val, "modelo": mo_val,
                        "tipo": ti_val, "propulsion": pr_val,
                        "cilindrada": cil if cil > 0 else "-",
                        "ciudad": lo_val, "provincia": pv_val,
                        "persona": pe_val, "renting": re_val,
                    })
                meses_procesados += 1
            except Exception:
                continue

    # Resumen por modelo y año
    resumen = defaultdict(lambda: defaultdict(int))
    for reg in resultados:
        resumen[f"{reg['marca']} {reg['modelo']}".strip()][reg['anio']] += 1

    anios = sorted(set(r['anio'] for r in resultados)) if resultados else []

    return jsonify({
        "total": len(resultados),
        "meses_procesados": meses_procesados,
        "anios": anios,
        "resumen": [
            {"modelo": k, "totales": dict(v), "total": sum(v.values())}
            for k, v in sorted(resumen.items(), key=lambda x: sum(x[1].values()), reverse=True)
        ],
        "registros": resultados[:500]  # máximo 500 filas en tabla
    })

if __name__ == "__main__":
    app.run(debug=True)
