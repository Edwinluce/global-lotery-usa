from flask import Flask, render_template, request, jsonify, session, redirect
import sqlite3, hashlib, random, secrets, uuid, os, urllib.request, urllib.error, json
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash


app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('SESSION_COOKIE_SECURE', '0') == '1'

MI_CUENTA_BCP = "19106864219053"
MI_CCI_BCP = "00219110686421905358"
MI_LINK_IZIPAY = "https://izipayya.page.link/TU_LINK_AQUI"
MI_NOMBRE_BCP = "Globallotery"

# ========================= MULTIMONEDA =========================
# El saldo interno de la base se mantiene en USD. La interfaz y las
# apuestas se muestran/reciben en la moneda local del jugador.
# Las tasas son unidades de moneda local por 1 USD y deben actualizarse
# periódicamente (o mediante FX_RATES_JSON en Render).
COUNTRY_CONFIG = {
    "USA": {"name":"Estados Unidos","currency":"USD","symbol":"$","decimals":2,"units_per_usd":1.0,"bet_levels":[5,10,20,50],"bet_min":5,"deposit_min":20,"withdraw_min_usd":15},
    "Perú": {"name":"Perú","currency":"PEN","symbol":"S/","decimals":2,"units_per_usd":3.3983,"bet_levels":[1,2,5,10,20],"bet_min":1,"deposit_min":10,"withdraw_min_usd":15},
    "Chile": {"name":"Chile","currency":"CLP","symbol":"$","decimals":0,"units_per_usd":975.27,"bet_levels":[500,1000,2000,5000,10000,20000],"bet_min":500,"deposit_min":5000,"withdraw_min_usd":15},
    "Colombia": {"name":"Colombia","currency":"COP","symbol":"$","decimals":0,"units_per_usd":3349.63,"bet_levels":[2000,5000,10000,20000,50000],"bet_min":2000,"deposit_min":20000,"withdraw_min_usd":15},
    "México": {"name":"México","currency":"MXN","symbol":"$","decimals":2,"units_per_usd":18.5,"bet_levels":[10,20,50,100,200],"bet_min":10,"deposit_min":100,"withdraw_min_usd":15},
    "Ecuador": {"name":"Ecuador","currency":"USD","symbol":"$","decimals":2,"units_per_usd":1.0,"bet_levels":[1,2,5,10],"bet_min":1,"deposit_min":10,"withdraw_min_usd":15},
    "Argentina": {"name":"Argentina","currency":"ARS","symbol":"$","decimals":2,"units_per_usd":1450.0,"bet_levels":[500,1000,2500,5000],"bet_min":500,"deposit_min":10000,"withdraw_min_usd":15},
    "Bolivia": {"name":"Bolivia","currency":"BOB","symbol":"Bs","decimals":2,"units_per_usd":6.96,"bet_levels":[5,10,20,50],"bet_min":5,"deposit_min":35,"withdraw_min_usd":15}
}

try:
    _fx_env = json.loads(os.environ.get("FX_RATES_JSON", "{}") or "{}")
    for _country, _rate in _fx_env.items():
        if _country in COUNTRY_CONFIG and float(_rate) > 0:
            COUNTRY_CONFIG[_country]["units_per_usd"] = float(_rate)
except Exception:
    pass

PREMIO_MULTIPLICADOR = 25

# Países habilitados actualmente para operar la plataforma.
# El resto de COUNTRY_CONFIG queda preparado para futuras expansiones.
PAISES_ACTIVOS = ["Perú", "Chile", "USA"]

def get_country_config(country):
    return COUNTRY_CONFIG.get(country, COUNTRY_CONFIG["USA"])

def local_to_usd(amount_local, config):
    return float(amount_local) / float(config["units_per_usd"])

def usd_to_local(amount_usd, config):
    return float(amount_usd) * float(config["units_per_usd"])

def format_local(amount, config):
    decimals = int(config.get("decimals", 2))
    return f'{config["symbol"]}{amount:,.{decimals}f}'

def round_local_amount(amount, config):
    decimals = int(config.get("decimals", 2))
    if decimals == 0:
        return int(round(amount))
    return round(float(amount), decimals)


# ========================= CORREO =========================
# En Render Free usamos Brevo mediante HTTPS.
# NO usamos SMTP porque Render Free bloquea los puertos SMTP 25/465/587.

BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "")
BREVO_FROM_EMAIL = os.environ.get("BREVO_FROM_EMAIL", "")
BREVO_FROM_NAME = os.environ.get("BREVO_FROM_NAME", "Globallotery")


def enviar_correo(destinatario, asunto, cuerpo):
    if not destinatario or not BREVO_API_KEY or not BREVO_FROM_EMAIL:
        print("CORREO NO ENVIADO: configura BREVO_API_KEY y BREVO_FROM_EMAIL")
        return False

    try:
        datos = {
            "sender": {
                "name": BREVO_FROM_NAME,
                "email": BREVO_FROM_EMAIL
            },
            "to": [
                {
                    "email": destinatario
                }
            ],
            "subject": asunto,
            "textContent": cuerpo
        }

        payload = json.dumps(datos).encode("utf-8")

        req = urllib.request.Request(
            "https://api.brevo.com/v3/smtp/email",
            data=payload,
            method="POST",
            headers={
                "accept": "application/json",
                "api-key": BREVO_API_KEY,
                "content-type": "application/json"
            }
        )

        with urllib.request.urlopen(req, timeout=20) as response:
            resultado = response.read().decode("utf-8")
            print("CORREO ENVIADO OK:", resultado)
            return True

    except urllib.error.HTTPError as e:
        try:
            detalle = e.read().decode("utf-8")
        except Exception:
            detalle = str(e)

        print("ERROR BREVO:", e.code, detalle)
        return False

    except Exception as e:
        print("ERROR ENVIANDO CORREO:", e)
        return False


def enviar_correo_async(destinatario, asunto, cuerpo):
    import threading

    threading.Thread(
        target=enviar_correo,
        args=(destinatario, asunto, cuerpo),
        daemon=True
    ).start()
    
DATABASE_URL = os.environ.get('DATABASE_URL')

def is_postgres():
    return DATABASE_URL is not None and DATABASE_URL!= ""

def db():
    if is_postgres():
        import psycopg2
        return psycopg2.connect(DATABASE_URL)
    else:
        return sqlite3.connect('animalitos.db', check_same_thread=False)

def crear_tablas_si_no_existen():
    try:
        con=db(); c=con.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS historial_resultados (
                id SERIAL PRIMARY KEY,
                sorteo_id INT,
                animal_id INT,
                animal_nombre TEXT,
                fecha TIMESTAMP DEFAULT NOW()
            );
        """)
        con.commit(); con.close()
        print("Tabla historial_resultados OK")
    except Exception as e:
        print("Error creando tabla historial:", e)

crear_tablas_si_no_existen()

def q(query):
    return query.replace('?', '%s') if is_postgres() else query

def hash_pass(p):
    return generate_password_hash(str(p), method='scrypt')

def verificar_password(ingresada, almacenada):
    try:
        if almacenada and (almacenada.startswith('scrypt:') or almacenada.startswith('pbkdf2:')):
            return check_password_hash(almacenada, ingresada)
    except Exception:
        pass
    # Compatibilidad con cuentas antiguas que usaban SHA-256.
    return hashlib.sha256(str(ingresada).encode()).hexdigest() == str(almacenada)

def registrar_correo_sorteo(user_id, sorteo_id, tipo):
    con = db(); c = con.cursor()
    try:
        if is_postgres():
            c.execute(q("""
                INSERT INTO correos_enviados (user_id,sorteo_id,tipo,fecha)
                VALUES (?,?,?,?)
                ON CONFLICT (user_id,sorteo_id,tipo) DO NOTHING
                RETURNING id
            """), (user_id,sorteo_id,tipo,datetime.now().isoformat()))
            row = c.fetchone()
        else:
            c.execute(q("""
                INSERT OR IGNORE INTO correos_enviados
                (user_id,sorteo_id,tipo,fecha) VALUES (?,?,?,?)
            """), (user_id,sorteo_id,tipo,datetime.now().isoformat()))
            row = (c.lastrowid,) if c.rowcount else None
        con.commit(); con.close()
        return bool(row)
    except Exception as e:
        try: con.rollback(); con.close()
        except Exception: pass
        print("ERROR REGISTRANDO CORREO:", e)
        return False


def get_proximo_cierre_global():
    ahora = datetime.now()
    proximo = ahora.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return proximo

def init_db():
    con = db(); c = con.cursor()
    if is_postgres():
        c.execute("CREATE TABLE IF NOT EXISTS usuarios (id SERIAL PRIMARY KEY, email TEXT UNIQUE, password TEXT, telefono TEXT, saldo FLOAT DEFAULT 0, fecha_registro TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS animales (id INTEGER PRIMARY KEY, nombre TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS sorteos (id SERIAL PRIMARY KEY, fecha_hora_cierre TEXT, animal_ganador INTEGER, estado TEXT, seed TEXT, hash_verificacion TEXT, recaudacion FLOAT, fondo_premios FLOAT, margen_plataforma FLOAT, jackpot FLOAT, tiempo_min INTEGER DEFAULT 60)")
        c.execute("CREATE TABLE IF NOT EXISTS apuestas (id TEXT PRIMARY KEY, sorteo_id INTEGER, usuario_id INTEGER, animal_id INTEGER, monto FLOAT, fecha TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS config (k TEXT PRIMARY KEY, v TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS config_paises (pais TEXT PRIMARY KEY, tasa_usd REAL NOT NULL, actualizado TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS metodos_pago (id SERIAL PRIMARY KEY, pais TEXT NOT NULL, nombre TEXT NOT NULL, tipo TEXT NOT NULL, destino TEXT, titular TEXT, banco TEXT, instrucciones TEXT, enlace TEXT, activo INTEGER DEFAULT 1, actualizado TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS retiros (id SERIAL PRIMARY KEY, user_id INTEGER, monto FLOAT, banco_info TEXT, estado TEXT, fecha TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS recargas_bcp (id SERIAL PRIMARY KEY, user_id INTEGER, monto INTEGER, operacion TEXT, estado TEXT, fecha TEXT, voucher TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS correos_enviados (id SERIAL PRIMARY KEY, user_id INTEGER, sorteo_id INTEGER, tipo TEXT, fecha TEXT, UNIQUE(user_id, sorteo_id, tipo))")
        c.execute("CREATE TABLE IF NOT EXISTS movimientos (id SERIAL PRIMARY KEY, user_id INTEGER, tipo TEXT, monto FLOAT, referencia TEXT, detalle TEXT, fecha TEXT)")
    else:
        c.execute("CREATE TABLE IF NOT EXISTS usuarios (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE, password TEXT, telefono TEXT, saldo REAL DEFAULT 0, fecha_registro TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS animales (id INTEGER PRIMARY KEY, nombre TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS sorteos (id INTEGER PRIMARY KEY AUTOINCREMENT, fecha_hora_cierre TEXT, animal_ganador INTEGER, estado TEXT, seed TEXT, hash_verificacion TEXT, recaudacion REAL, fondo_premios REAL, margen_plataforma REAL, jackpot REAL, tiempo_min INTEGER DEFAULT 60)")
        c.execute("CREATE TABLE IF NOT EXISTS apuestas (id TEXT PRIMARY KEY, sorteo_id INTEGER, usuario_id INTEGER, animal_id INTEGER, monto REAL, fecha TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS config (k TEXT PRIMARY KEY, v TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS config_paises (pais TEXT PRIMARY KEY, tasa_usd REAL NOT NULL, actualizado TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS metodos_pago (id INTEGER PRIMARY KEY AUTOINCREMENT, pais TEXT NOT NULL, nombre TEXT NOT NULL, tipo TEXT NOT NULL, destino TEXT, titular TEXT, banco TEXT, instrucciones TEXT, enlace TEXT, activo INTEGER DEFAULT 1, actualizado TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS retiros (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, monto REAL, banco_info TEXT, estado TEXT, fecha TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS recargas_bcp (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, monto INTEGER, operacion TEXT, estado TEXT, fecha TEXT, voucher TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS correos_enviados (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, sorteo_id INTEGER, tipo TEXT, fecha TEXT, UNIQUE(user_id, sorteo_id, tipo))")
        c.execute("CREATE TABLE IF NOT EXISTS movimientos (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, tipo TEXT, monto REAL, referencia TEXT, detalle TEXT, fecha TEXT)")

    # Migraciones para instalaciones existentes.
    migrations = [
        ("usuarios", "pais", "TEXT DEFAULT 'USA'"),
        ("usuarios", "moneda", "TEXT DEFAULT 'USD'"),
        ("usuarios", "terminos_aceptados", "INTEGER DEFAULT 0"),
        ("usuarios", "terminos_version", "TEXT DEFAULT '1.0'"),
        ("usuarios", "terminos_fecha", "TEXT"),
        ("apuestas", "monto_local", "REAL"),
        ("apuestas", "moneda", "TEXT"),
        ("apuestas", "fx_units_per_usd", "REAL"),
        ("recargas_bcp", "monto_local", "REAL"),
        ("recargas_bcp", "moneda", "TEXT"),
        ("recargas_bcp", "fx_units_per_usd", "REAL"),
        ("recargas_bcp", "metodo_pago_id", "INTEGER"),
        ("recargas_bcp", "metodo_pago_nombre", "TEXT"),
        ("recargas_bcp", "metodo_pago_destino", "TEXT"),
        ("recargas_bcp", "nombre_remitente", "TEXT"),
        ("retiros", "monto_local", "REAL"),
        ("retiros", "moneda", "TEXT"),
        ("retiros", "fx_units_per_usd", "REAL")
        ,("metodos_pago", "enlace", "TEXT")
    ]
    for table, col, definition in migrations:
        try:
            if is_postgres():
                c.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {definition}")
            else:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
        except Exception:
            pass

    c.execute("SELECT COUNT(*) FROM animales")
    if c.fetchone()[0]==0:
        noms=["Perro","Gato","Ratón","Conejo","Zorro","Tigre","León","Elefante","Mono","Gallina","Gallo","Cerdo","Vaca","Caballo","Alpaca","Vicuña","Cóndor","Oso","Puma","Gallito","Caimán","Serpiente","Rana","Delfin","Guacamayo"]
        for i,n in enumerate(noms):
            try: c.execute(q("INSERT INTO animales VALUES (?,?)"),(i+1,n))
            except: pass
    c.execute(q("SELECT id FROM sorteos WHERE estado='ABIERTO' LIMIT 1"))
    if not c.fetchone():
        proximo = get_proximo_cierre_global()
        c.execute(q("INSERT INTO sorteos (fecha_hora_cierre, estado, tiempo_min) VALUES (?, 'ABIERTO', 60)"),(proximo.isoformat(),))
    c.execute(q("INSERT INTO config (k,v) VALUES ('pausado','0') ON CONFLICT (k) DO NOTHING") if is_postgres() else "INSERT OR IGNORE INTO config (k,v) VALUES ('pausado','0')")
    c.execute(q("INSERT INTO config (k,v) VALUES ('tiempo_min','60') ON CONFLICT (k) DO NOTHING") if is_postgres() else "INSERT OR IGNORE INTO config (k,v) VALUES ('tiempo_min','60')")
    con.commit(); con.close()
    print("BASE CREADA OK - POSTGRES" if is_postgres() else "BASE CREADA OK - SQLITE")

init_db()

def cargar_tasas_paises():
    """Carga las tasas persistidas en DB y las mantiene en COUNTRY_CONFIG."""
    con=None
    try:
        con=db(); c=con.cursor()
        for pais, cfg in COUNTRY_CONFIG.items():
            tasa=float(cfg.get("units_per_usd", 1.0))
            if is_postgres():
                c.execute(q("INSERT INTO config_paises (pais,tasa_usd,actualizado) VALUES (?,?,?) ON CONFLICT (pais) DO NOTHING"), (pais,tasa,datetime.now().isoformat()))
            else:
                c.execute(q("INSERT OR IGNORE INTO config_paises (pais,tasa_usd,actualizado) VALUES (?,?,?)"), (pais,tasa,datetime.now().isoformat()))
        con.commit()
        c.execute(q("SELECT pais,tasa_usd FROM config_paises"))
        for pais,tasa in c.fetchall():
            if pais in COUNTRY_CONFIG and float(tasa)>0:
                COUNTRY_CONFIG[pais]["units_per_usd"]=float(tasa)
        con.close()
        print("TASAS POR PAIS CARGADAS OK")
    except Exception as e:
        if con:
            try: con.close()
            except Exception: pass
        print("ERROR CARGANDO TASAS POR PAIS:",e)

cargar_tasas_paises()

def cargar_metodos_pago():
    """Crea métodos manuales iniciales sin sobrescribir cambios hechos por admin."""
    con=None
    try:
        con=db(); c=con.cursor()
        # Mantiene la información que ya estaba configurada en el proyecto para Perú.
        defaults = [
            ("Perú", "Yape", "billetera", "", "", "", "Configura aquí el número Yape desde el panel admin."),
            ("Perú", "BCP", "banco", MI_CUENTA_BCP, MI_NOMBRE_BCP, "BCP", "Puedes indicar cuenta y/o CCI en las instrucciones."),
            ("Chile", "Transferencia bancaria", "banco", "", "", "", "Configura los datos de tu cuenta bancaria desde el panel admin."),
            ("USA", "Zelle", "zelle", "", "", "", "Configura aquí el correo o teléfono de Zelle desde el panel admin."),
        ]
        for pais,nombre,tipo,destino,titular,banco,instrucciones in defaults:
            c.execute(q("SELECT id FROM metodos_pago WHERE pais=? AND nombre=? LIMIT 1"),(pais,nombre))
            if not c.fetchone():
                ahora=datetime.now().isoformat()
                if is_postgres():
                    c.execute(q("INSERT INTO metodos_pago (pais,nombre,tipo,destino,titular,banco,instrucciones,activo,actualizado) VALUES (?,?,?,?,?,?,?,1,?)"),
                              (pais,nombre,tipo,destino,titular,banco,instrucciones,ahora))
                else:
                    c.execute(q("INSERT INTO metodos_pago (pais,nombre,tipo,destino,titular,banco,instrucciones,activo,actualizado) VALUES (?,?,?,?,?,?,?,1,?)"),
                              (pais,nombre,tipo,destino,titular,banco,instrucciones,ahora))
        con.commit(); con.close()
    except Exception as e:
        if con:
            try: con.close()
            except Exception: pass
        print("ERROR CARGANDO METODOS DE PAGO:",e)

cargar_metodos_pago()

def get_config():
    try:
        con=db(); c=con.cursor()
        c.execute(q("SELECT v FROM config WHERE k='pausado'")); r=c.fetchone()
        pausado = r[0]=='1' if r else False
        c.execute(q("SELECT v FROM config WHERE k='tiempo_min'")); r=c.fetchone()
        tiempo = int(r[0]) if r and str(r[0]).isdigit() else 60
        con.close(); return pausado, tiempo
    except: return False, 60

def set_config(k,v):
    con=db(); c=con.cursor()
    c.execute(q("INSERT INTO config (k,v) VALUES (?,?) ON CONFLICT (k) DO UPDATE SET v=EXCLUDED.v") if is_postgres() else "INSERT OR REPLACE INTO config (k,v) VALUES (?,?)",(k,str(v)))
    con.commit(); con.close()

def registrar_movimiento(c, user_id, tipo, monto, referencia='', detalle=''):
    c.execute(q("INSERT INTO movimientos (user_id,tipo,monto,referencia,detalle,fecha) VALUES (?,?,?,?,?,?)"),
              (user_id, tipo, float(monto), str(referencia), str(detalle), datetime.now().isoformat()))


def validar_monto(valor, maximo=100000):
    try:
        monto = int(float(valor))
    except (TypeError, ValueError):
        raise ValueError('Monto inválido')
    if monto <= 0 or monto > maximo:
        raise ValueError('Monto fuera de rango')
    return monto


def require_admin(request_obj):
    if not session.get('admin'):
        return False
    esperado = session.get('admin_csrf')
    recibido = request_obj.headers.get('X-CSRF-Token', '')
    return bool(esperado and recibido and secrets.compare_digest(esperado, recibido))


def sortear():
    pausado,_=get_config()
    if pausado: return
    con=db(); c=con.cursor()
    try:
        c.execute(q("SELECT id FROM sorteos WHERE estado='ABIERTO' AND fecha_hora_cierre <=? ORDER BY id ASC LIMIT 1"),
                  (datetime.now().isoformat(),))
        row=c.fetchone()
        if not row: con.close(); return
        sid=row[0]
        c.execute(q("UPDATE sorteos SET estado='CERRADO' WHERE id=?"),(sid,)); con.commit()

        c.execute(q("SELECT COUNT(*),COALESCE(SUM(monto),0) FROM apuestas WHERE sorteo_id=?"),(sid,))
        tot,recaud=c.fetchone(); recaud=float(recaud or 0)
        fondo=float(recaud*0.75); margen=float(recaud*0.25)

        if tot and tot>=1:
            seed_raw=f"{sid}-{datetime.now().isoformat()}-{secrets.token_hex(16)}"
            h=hashlib.sha256(seed_raw.encode()).hexdigest()
            random.seed(h); ganador=random.randint(1,25)

            c.execute(q("UPDATE sorteos SET animal_ganador=?,seed=?,hash_verificacion=?,recaudacion=?,fondo_premios=?,margen_plataforma=?,estado='RESULTADO_GENERADO' WHERE id=?"),
                      (ganador,seed_raw,h,recaud,fondo,margen,sid))
            c.execute(q("SELECT nombre FROM animales WHERE id=?"),(ganador,))
            animal_ganador=c.fetchone()[0]

            # Regla de la app: cada apuesta que acierta paga x25.
            c.execute(q("SELECT usuario_id,SUM(monto) FROM apuestas WHERE sorteo_id=? AND animal_id=? GROUP BY usuario_id"),(sid,ganador))
            ganadores=c.fetchall()
            premios=[]
            for uid,apostado_usd in ganadores:
                premio=float(apostado_usd or 0) * PREMIO_MULTIPLICADOR
                premios.append([uid,premio])
            for uid,premio in premios:
                c.execute(q("UPDATE usuarios SET saldo=saldo+? WHERE id=?"),(premio,uid))
                registrar_movimiento(c, uid, 'PREMIO', premio, f'sorteo:{sid}', f'Premio x{PREMIO_MULTIPLICADOR} por acertar {animal_ganador}')
            if ganadores:
                c.execute(q("UPDATE sorteos SET estado='PAGADO' WHERE id=?"),(sid,))
            else:
                c.execute(q("UPDATE sorteos SET jackpot=?,estado='FINALIZADO' WHERE id=?"),(fondo,sid))
            con.commit()

            c.execute(q("SELECT DISTINCT a.usuario_id,u.email,u.pais,u.moneda FROM apuestas a JOIN usuarios u ON u.id=a.usuario_id WHERE a.sorteo_id=?"),(sid,))
            participantes=c.fetchall()
            for uid,email,pais,moneda in participantes:
                cfg=get_country_config(pais)
                c.execute(q("""
                    SELECT an.nombre,SUM(a.monto),SUM(COALESCE(a.monto_local,0)) FROM apuestas a JOIN animales an ON an.id=a.animal_id
                    WHERE a.sorteo_id=? AND a.usuario_id=? GROUP BY an.id,an.nombre ORDER BY an.id
                """),(sid,uid))
                seleccion="\n".join(format_local(float(x[2] or usd_to_local(x[1],cfg)),cfg)+" - "+x[0] for x in c.fetchall())
                c.execute(q("SELECT COALESCE(SUM(monto),0) FROM apuestas WHERE sorteo_id=? AND usuario_id=? AND animal_id=?"),
                          (sid,uid,ganador))
                acierto_usd=float(c.fetchone()[0] or 0)

                if acierto_usd>0:
                    premio_usd=acierto_usd*PREMIO_MULTIPLICADOR
                    c.execute(q("SELECT saldo FROM usuarios WHERE id=?"),(uid,))
                    saldo_actual_usd=float(c.fetchone()[0] or 0)
                    premio_local=usd_to_local(premio_usd,cfg)
                    saldo_local=usd_to_local(saldo_actual_usd,cfg)
                    mensaje=f"¡Felicidades! Acertaste.\nPremio acreditado: {format_local(premio_local,cfg)}\nSaldo actual: {format_local(saldo_local,cfg)}"
                else:
                    mensaje="En este sorteo no acertaste el animal ganador."

                if registrar_correo_sorteo(uid,sid,"resultado"):
                    enviar_correo_async(
                        email,f"Resultado del sorteo #{sid}: {animal_ganador}",
                        f"Hola,\n\nEl sorteo #{sid} terminó.\n\n"
                        f"Tus animales elegidos ({cfg['currency']}):\n{seleccion}\n\n"
                        f"ANIMAL GANADOR: {animal_ganador}\n\n{mensaje}\n\n"
                        "Gracias por participar en Globallotery."
                    )
        else:
            c.execute(q("UPDATE sorteos SET recaudacion=?,fondo_premios=?,margen_plataforma=?,jackpot=?,estado='FINALIZADO' WHERE id=?"),
                      (recaud,fondo,margen,fondo,sid)); con.commit()

        proximo=get_proximo_cierre_global(); _,tiempo=get_config()
        c.execute(q("INSERT INTO sorteos (fecha_hora_cierre,estado,tiempo_min) VALUES (?,'ABIERTO',?)"),
                  (proximo.isoformat(),tiempo)); con.commit()
    except Exception as e:
        try: con.rollback()
        except Exception: pass
        print("ERROR EN SORTEO:",e)
    finally:
        try: con.close()
        except Exception: pass

scheduler=BackgroundScheduler()
scheduler.add_job(sortear,'interval', seconds=60)
scheduler.start()

@app.route('/login')
def login_page(): return render_template('login.html')

@app.route('/api/register', methods=['POST'])
def api_register():
    try:
        d=request.json or {}
        email=str(d.get('email','')).strip().lower()
        password=str(d.get('password',''))
        pais=str(d.get('pais','USA')).strip()
        if pais not in PAISES_ACTIVOS:
            return jsonify({"ok":False,"msg":"Por ahora solo están habilitados Perú, Chile y Estados Unidos."}),400
        cfg=get_country_config(pais)
        if '@' not in email or len(email)>254: return jsonify({"ok":False,"msg":"Correo inválido"}),400
        if len(password)<8 or len(password)>128: return jsonify({"ok":False,"msg":"La contraseña debe tener entre 8 y 128 caracteres"}),400
        if not bool(d.get('terminos_aceptados',False)):
            return jsonify({"ok":False,"msg":"Debes aceptar los Términos y Condiciones para registrarte."}),400
        pw=hash_pass(password); tel=str(d.get('telefono',''))[:30]
        con=db(); c=con.cursor(); ahora=datetime.now().isoformat()
        if is_postgres():
            c.execute(q("INSERT INTO usuarios (email,password,telefono,saldo,fecha_registro,pais,moneda,terminos_aceptados,terminos_version,terminos_fecha) VALUES (?,?,?,?,NOW(),?,?,?,?,?) RETURNING id"),
                      (email,pw,tel,0,pais,cfg['currency'],1,'1.0',ahora))
            uid=c.fetchone()[0]
        else:
            c.execute(q("INSERT INTO usuarios (email,password,telefono,saldo,fecha_registro,pais,moneda,terminos_aceptados,terminos_version,terminos_fecha) VALUES (?,?,?,?,?,?,?,?,?,?)"),
                      (email,pw,tel,0,ahora,pais,cfg['currency'],1,'1.0',ahora))
            uid=c.lastrowid
        con.commit(); con.close()
        session['user']=uid; session['email']=email
        return jsonify({"ok":True,"pais":pais,"moneda":cfg['currency']})
    except Exception as e:
        print("ERROR REGISTER:", e)
        return jsonify({"ok":False,"msg": "Correo ya registrado" if "UNIQUE" in str(e) or "duplicate" in str(e).lower() else "Error: "+str(e)})

@app.route('/api/login', methods=['POST'])
def api_login():
    d=request.json or {}; email=str(d.get('email','')).strip().lower(); password=str(d.get('password',''))
    con=db(); c=con.cursor()
    c.execute(q("SELECT id,email,saldo,password,pais,moneda FROM usuarios WHERE email=?"), (email,))
    row=c.fetchone()
    if not row:
        con.close(); return jsonify({"ok":False,"msg":"Credenciales incorrectas"})
    if not verificar_password(password, row[3]):
        con.close(); return jsonify({"ok":False,"msg":"Credenciales incorrectas"})
    if not str(row[3]).startswith(('scrypt:','pbkdf2:')):
        c.execute(q("UPDATE usuarios SET password=? WHERE id=?"),(hash_pass(password),row[0])); con.commit()
    pais=row[4] if row[4] in COUNTRY_CONFIG else 'USA'; cfg=get_country_config(pais)
    if not row[5]:
        c.execute(q("UPDATE usuarios SET pais=?,moneda=? WHERE id=?"),(pais,cfg['currency'],row[0])); con.commit()
    con.close(); session.clear(); session['user']=row[0]; session['email']=row[1]
    return jsonify({"ok":True, "saldo": row[2], "pais":pais, "moneda":cfg['currency']})

@app.route('/logout')
def logout(): session.clear(); return redirect('/login')

@app.route('/')
def player():
    if 'user' not in session: return redirect('/login')
    con=db(); c=con.cursor()
    c.execute(q("SELECT * FROM sorteos WHERE estado='ABIERTO' ORDER BY id DESC LIMIT 1")); s=c.fetchone()
    if not s:
        proximo = get_proximo_cierre_global()
        c.execute(q("INSERT INTO sorteos (fecha_hora_cierre, estado) VALUES (?, 'ABIERTO')"),(proximo.isoformat(),)); con.commit()
        c.execute(q("SELECT * FROM sorteos WHERE estado='ABIERTO' ORDER BY id DESC LIMIT 1")); s=c.fetchone()
    c.execute(q("SELECT saldo,email,pais,moneda FROM usuarios WHERE id=?"), (session['user'],)); u=c.fetchone()
    c.execute(q("SELECT * FROM animales")); anims=c.fetchall()
    c.execute(q("SELECT fecha_hora_cierre, animal_ganador FROM sorteos WHERE estado IN ('PAGADO','FINALIZADO') ORDER BY id DESC LIMIT 24")); historial=c.fetchall()
    con.close()
    pais=u[2] if u and u[2] in COUNTRY_CONFIG else 'USA'; cfg=get_country_config(pais)
    saldo_usd=float(u[0] or 0) if u else 0
    return render_template('player.html', sorteo=s, animales=anims, historial=historial, saldo=saldo_usd, saldo_local=usd_to_local(saldo_usd,cfg), email=u[1] if u else '', pais=pais, moneda=cfg['currency'], currency_config=cfg, premio_multiplicador=PREMIO_MULTIPLICADOR, bcp_cuenta=MI_CUENTA_BCP, bcp_cci=MI_CCI_BCP, bcp_link=MI_LINK_IZIPAY, bcp_nombre=MI_NOMBRE_BCP)

@app.route('/api/configuracion-juego')
def api_configuracion_juego():
    if 'user' not in session: return jsonify({"ok":False,"msg":"No logueado"}),401
    con=db(); c=con.cursor(); c.execute(q("SELECT pais FROM usuarios WHERE id=?"),(session['user'],)); r=c.fetchone(); con.close()
    pais=r[0] if r and r[0] in COUNTRY_CONFIG else 'USA'; cfg=get_country_config(pais)
    out=dict(cfg); out.update({"pais":pais,"premio_multiplicador":PREMIO_MULTIPLICADOR})
    return jsonify({"ok":True,"config":out})

@app.route('/api/metodos-pago')
def api_metodos_pago():
    if 'user' not in session:
        return jsonify({"ok":False,"msg":"No logueado"}),401
    con=None
    try:
        con=db(); c=con.cursor()
        c.execute(q("SELECT pais FROM usuarios WHERE id=?"),(session['user'],))
        r=c.fetchone()
        pais=r[0] if r and r[0] in COUNTRY_CONFIG else 'USA'
        c.execute(q("""
            SELECT id,nombre,tipo,destino,titular,banco,instrucciones,enlace
            FROM metodos_pago
            WHERE pais=? AND activo=1 AND (TRIM(COALESCE(destino,''))<>'' OR TRIM(COALESCE(enlace,''))<>'')
            ORDER BY id
        """),(pais,))
        rows=c.fetchall(); con.close()
        return jsonify({"ok":True,"pais":pais,"metodos":[
            {"id":x[0],"nombre":x[1],"tipo":x[2],"destino":x[3] or "","titular":x[4] or "","banco":x[5] or "","instrucciones":x[6] or "","enlace":x[7] or ""}
            for x in rows
        ]})
    except Exception as e:
        if con:
            try: con.close()
            except Exception: pass
        return jsonify({"ok":False,"msg":"No se pudieron cargar los métodos de pago"}),500

@app.route('/api/apostar-multiple', methods=['POST'])
def apostar_multiple():
    if 'user' not in session: return jsonify({"ok":False,"msg":"No logueado"}),401
    pausado,_ = get_config()
    if pausado: return jsonify({"ok":False,"msg":"Sala pausada por admin"}),400
    con=None
    try:
        data=(request.json or {}).get('apuestas',{})
        if not isinstance(data,dict) or not data: return jsonify({"ok":False,"msg":"Debes elegir al menos un animal"}),400
        con=db(); c=con.cursor()
        c.execute(q("SELECT saldo,email,pais FROM usuarios WHERE id=?"),(session['user'],)); u=c.fetchone()
        if not u: con.close(); return jsonify({"ok":False,"msg":"Usuario no encontrado"}),404
        pais=u[2] if u[2] in COUNTRY_CONFIG else 'USA'; cfg=get_country_config(pais)
        apuestas={}; total_usd=0; nombres=[]
        niveles={round(float(x),2):x for x in cfg['bet_levels']}
        for animal_id,monto in data.items():
            animal_id=int(animal_id); monto_local=float(monto)
            if animal_id<1 or animal_id>25: return jsonify({"ok":False,"msg":"Animal inválido"}),400
            if round(monto_local,2) not in niveles:
                return jsonify({"ok":False,"msg":f"Monto no permitido. Usa: {', '.join(format_local(x,cfg) for x in cfg['bet_levels'])}"}),400
            monto_usd=local_to_usd(monto_local,cfg); apuestas[animal_id]=(monto_local,monto_usd); total_usd+=monto_usd

        if float(u[0] or 0)<total_usd:
            con.close(); return jsonify({"ok":False,"msg":f"Saldo insuficiente. Disponible: {format_local(usd_to_local(float(u[0] or 0),cfg),cfg)}"}),400
        c.execute(q("SELECT id,fecha_hora_cierre FROM sorteos WHERE estado='ABIERTO' ORDER BY id DESC LIMIT 1")); row=c.fetchone()
        if not row: con.close(); return jsonify({"ok":False,"msg":"No hay un sorteo abierto"}),400
        sid,cierre=row
        try:
            if cierre and datetime.fromisoformat(str(cierre)) <= datetime.now(): con.close(); return jsonify({"ok":False,"msg":"El sorteo ya cerró"}),400
        except Exception: pass
        c.execute(q("UPDATE usuarios SET saldo=saldo-? WHERE id=? AND saldo>=?"),(total_usd,session['user'],total_usd))
        if c.rowcount != 1:
            con.rollback(); con.close(); return jsonify({"ok":False,"msg":"Saldo insuficiente"}),400
        for animal_id,(monto_local,monto_usd) in apuestas.items():
            c.execute(q("SELECT nombre FROM animales WHERE id=?"),(animal_id,)); animal=c.fetchone()
            if not animal: raise ValueError("Animal inválido")
            nombres.append(f"- {animal[0]}: {format_local(monto_local,cfg)}")
            c.execute(q("INSERT INTO apuestas (id,sorteo_id,usuario_id,animal_id,monto,fecha,monto_local,moneda,fx_units_per_usd) VALUES (?,?,?,?,?,?,?,?,?)"),
                      (str(uuid.uuid4()),sid,session['user'],animal_id,monto_usd,datetime.now().isoformat(),monto_local,cfg['currency'],cfg['units_per_usd']))
        registrar_movimiento(c, session['user'], 'APUESTA', -total_usd, f'sorteo:{sid}', f'Apuesta múltiple en {cfg["currency"]}')
        con.commit(); con.close()
        total_local=usd_to_local(total_usd,cfg)
        enviar_correo_async(u[1],f"Confirmación de tu apuesta - Sorteo #{sid}",
            f"Hola,\n\nTu apuesta fue registrada correctamente.\n\n"
            f"Moneda: {cfg['currency']}\nAnimales elegidos:\n"+"\n".join(nombres)+
            f"\n\nTotal apostado: {format_local(total_local,cfg)}\nPremio por acierto: x{PREMIO_MULTIPLICADOR}\n\nGloballotery")
        return jsonify({"ok":True,"msg":"Apuesta registrada y correo enviado","total_local":total_local,"total_usd":total_usd,"moneda":cfg['currency']})
    except Exception as e:
        if con:
            try: con.rollback(); con.close()
            except Exception: pass
        print("ERROR APOSTANDO:",e); return jsonify({"ok":False,"msg":"Error registrando la apuesta"}),500

@app.route('/api/recarga-bcp', methods=['POST'])
def recarga_bcp():
    if 'user' not in session: return jsonify({"ok":False,"msg":"No logueado"}),401
    try:
        monto_local=validar_monto(request.form.get('monto',0) or (request.json.get('monto',0) if request.is_json else 0), 100000000)
    except ValueError as e:
        return jsonify({"ok":False,"msg":str(e)}),400

    con=None
    try:
        con=db(); c=con.cursor()
        c.execute(q("SELECT pais,email FROM usuarios WHERE id=?"),(session['user'],))
        ur=c.fetchone()
        pais=ur[0] if ur and ur[0] in COUNTRY_CONFIG else 'USA'
        cfg=get_country_config(pais)
        if monto_local < cfg['deposit_min']:
            con.close(); return jsonify({"ok":False,"msg":f"La recarga mínima es {format_local(cfg['deposit_min'],cfg)}"}),400

        metodo_raw=request.form.get('metodo_pago_id','') or (request.json.get('metodo_pago_id','') if request.is_json else '')
        try: metodo_id=int(metodo_raw)
        except Exception: metodo_id=0
        c.execute(q("""
            SELECT id,nombre,tipo,destino,titular,banco,instrucciones
            FROM metodos_pago
            WHERE id=? AND pais=? AND activo=1
        """),(metodo_id,pais))
        metodo=c.fetchone()
        if not metodo:
            con.close(); return jsonify({"ok":False,"msg":"Selecciona un método de pago habilitado."}),400

        operacion=str(request.form.get('operacion','') or (request.json.get('operacion','') if request.is_json else ''))[:100]
        if not operacion:
            con.close(); return jsonify({"ok":False,"msg":"Falta el número de operación o referencia"}),400
        nombre_remitente=str(request.form.get('nombre','') or (request.json.get('nombre','') if request.is_json else ''))[:100]
        if not nombre_remitente:
            con.close(); return jsonify({"ok":False,"msg":"Indica el nombre de quien realizó el pago"}),400

        file=request.files.get('voucher'); voucher_path=""
        if file and file.filename:
            ext=os.path.splitext(file.filename)[1].lower()
            if ext not in {'.jpg','.jpeg','.png','.webp'}:
                con.close(); return jsonify({"ok":False,"msg":"El voucher debe ser JPG, PNG o WEBP"}),400
            os.makedirs("static/vouchers", exist_ok=True)
            fname=secure_filename(f"{session['user']}_{int(datetime.now().timestamp())}_{secrets.token_hex(4)}{ext}")
            voucher_path=os.path.join("static/vouchers", fname); file.save(voucher_path)

        monto_usd=local_to_usd(monto_local,cfg)
        c.execute(q("""
            INSERT INTO recargas_bcp
            (user_id,monto,operacion,estado,fecha,voucher,monto_local,moneda,fx_units_per_usd,
             metodo_pago_id,metodo_pago_nombre,metodo_pago_destino,nombre_remitente)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """),(
            session['user'],monto_usd,operacion,'pendiente',datetime.now().isoformat(),voucher_path,
            monto_local,cfg['currency'],cfg['units_per_usd'],metodo[0],metodo[1],metodo[3] or '',nombre_remitente
        ))
        con.commit(); con.close()
        return jsonify({"ok":True,"msg":f"Solicitud de recarga por {format_local(monto_local,cfg)} enviada"})
    except Exception as e:
        if con:
            try: con.rollback(); con.close()
            except Exception: pass
        print("ERROR RECARGA:",e)
        return jsonify({"ok":False,"msg":"No se pudo registrar la recarga"}),500

@app.route("/api/solicitar-retiro", methods=["POST"])
def solicitar_retiro():
    if 'user' not in session: return jsonify({"ok":False,"msg":"No logueado"}),401
    con=None
    try:
        data=request.get_json() or {}; monto_local=validar_monto(data.get("monto",0),100000000); destino=str(data.get("banco_info",data.get("yape",""))).strip()
        if len(destino)>150: return jsonify({"ok":False,"msg":"Dato de retiro demasiado largo"}),400
        if not destino: return jsonify({"ok":False,"msg":"Indica la cuenta o medio de retiro"}),400
        con=db(); c=con.cursor(); c.execute(q("SELECT saldo,email,pais FROM usuarios WHERE id=?"),(session['user'],)); u=c.fetchone()
        pais=u[2] if u and u[2] in COUNTRY_CONFIG else 'USA'; cfg=get_country_config(pais); min_local=round_local_amount(usd_to_local(cfg['withdraw_min_usd'],cfg),cfg)
        if monto_local < min_local: con.close(); return jsonify({"ok":False,"msg":f"El retiro mínimo es {format_local(min_local,cfg)}"}),400
        monto_usd=local_to_usd(monto_local,cfg)
        if not u or float(u[0] or 0)<monto_usd: con.close(); return jsonify({"ok":False,"msg":f"Saldo insuficiente: {format_local(usd_to_local(float(u[0] or 0),cfg),cfg)}"}),400
        c.execute(q("UPDATE usuarios SET saldo=saldo-? WHERE id=? AND saldo>=?"),(monto_usd,session['user'],monto_usd))
        if c.rowcount != 1: con.rollback(); con.close(); return jsonify({"ok":False,"msg":"Saldo insuficiente"}),400
        c.execute(q("INSERT INTO retiros (user_id,monto,banco_info,estado,fecha,monto_local,moneda,fx_units_per_usd) VALUES (?,?,?,?,?,?,?,?)"),
                  (session['user'],monto_usd,destino,"pendiente",datetime.now().isoformat(),monto_local,cfg['currency'],cfg['units_per_usd']))
        retiro_id=c.lastrowid if not is_postgres() else None
        registrar_movimiento(c,session['user'],'RETIRO_RESERVADO',-monto_usd,f'retiro:{retiro_id or "pendiente"}','Saldo reservado para retiro')
        con.commit(); con.close()
        enviar_correo_async(u[1],"Solicitud de retiro recibida - Globallotery",f"Hola,\n\nRecibimos tu solicitud de retiro por {format_local(monto_local,cfg)}.\nCuenta/medio: {destino}\n\nGloballotery")
        return jsonify({"ok":True,"msg":"Solicitud enviada. El saldo quedó reservado."})
    except Exception as e:
        if con:
            try: con.rollback(); con.close()
            except Exception: pass
        return jsonify({"ok":False,"msg":"No se pudo crear el retiro"}),500

@app.route("/api/aprobar-retiro", methods=["POST"])
@app.route("/api/admin/retiros/aprobar", methods=["POST"])
def aprobar_retiro():
    if not require_admin(request): return jsonify({"ok":False,"msg":"Sesión administrativa inválida"}),403
    if not session.get('admin'): return jsonify({"ok":False,"msg":"No admin"}),401
    con=None
    try:
        rid=int((request.get_json() or {}).get("id",0)); con=db(); c=con.cursor()
        c.execute(q("SELECT r.user_id,r.monto,r.estado,u.email,u.pais FROM retiros r JOIN usuarios u ON u.id=r.user_id WHERE r.id=?"),(rid,))
        r=c.fetchone()
        if not r: con.close(); return jsonify({"ok":False,"msg":"No existe"}),404
        if r[2]=='aprobado': con.close(); return jsonify({"ok":True,"msg":"Ya estaba aprobado"})
        if r[2]=='rechazado': con.close(); return jsonify({"ok":False,"msg":"El retiro ya fue rechazado"}),400
        c.execute(q("UPDATE retiros SET estado='aprobado' WHERE id=?"),(rid,))
        pais_retiro = r[4] if r[4] in COUNTRY_CONFIG else 'USA'
        cfg_retiro = get_country_config(pais_retiro)
        retiro_local = format_local(usd_to_local(float(r[1]), cfg_retiro), cfg_retiro)
        registrar_movimiento(c, r[0], 'RETIRO_APROBADO', 0, f'retiro:{rid}', f'Retiro aprobado por {retiro_local}')
        con.commit(); con.close()
        enviar_correo_async(r[3],"Retiro aprobado - Globallotery",
                            f"Hola,\n\nTu retiro de {retiro_local} fue APROBADO.\n"
                            "El pago puede ser procesado al medio registrado.\n\nGloballotery")
        return jsonify({"ok":True,"msg":"Retiro aprobado correctamente"})
    except Exception as e:
        if con:
            try: con.rollback(); con.close()
            except Exception: pass
        return jsonify({"ok":False,"msg":"No se pudo aprobar el retiro"}),500

@app.route("/api/rechazar-retiro", methods=["POST"])
@app.route("/api/admin/retiros/rechazar", methods=["POST"])
def rechazar_retiro():
    if not require_admin(request): return jsonify({"ok":False,"msg":"Sesión administrativa inválida"}),403
    if not session.get('admin'): return jsonify({"ok":False,"msg":"No admin"}),401
    con=None
    try:
        rid=int((request.get_json() or {}).get("id",0)); con=db(); c=con.cursor()
        c.execute(q("SELECT r.user_id,r.monto,r.estado,u.email,u.pais FROM retiros r JOIN usuarios u ON u.id=r.user_id WHERE r.id=?"),(rid,))
        r=c.fetchone()
        if not r: con.close(); return jsonify({"ok":False,"msg":"No existe"}),404
        if r[2]=='rechazado': con.close(); return jsonify({"ok":True,"msg":"Ya estaba rechazado"})
        if r[2]=='aprobado': con.close(); return jsonify({"ok":False,"msg":"No se puede rechazar un retiro aprobado"}),400
        c.execute(q("UPDATE usuarios SET saldo=saldo+? WHERE id=?"),(r[1],r[0]))
        registrar_movimiento(c, r[0], 'RETIRO_DEVUELTO', r[1], f'retiro:{rid}', 'Retiro rechazado; saldo devuelto')
        c.execute(q("UPDATE retiros SET estado='rechazado' WHERE id=?"),(rid,))
        con.commit(); con.close()
        enviar_correo_async(r[3],"Retiro rechazado - Globallotery",
                            f"Hola,\n\nTu retiro fue rechazado.\n"
                            f"El monto reservado fue devuelto a tu saldo.\n\nGloballotery")
        return jsonify({"ok":True,"msg":"Rechazado y saldo devuelto"})
    except Exception as e:
        if con:
            try: con.rollback(); con.close()
            except Exception: pass
        return jsonify({"ok":False,"msg":"No se pudo rechazar el retiro"}),500

@app.route('/api/movimientos')
def api_movimientos():
    if 'user' not in session: return jsonify([]),401
    con=db(); c=con.cursor()
    c.execute(q("SELECT tipo,monto,referencia,detalle,fecha FROM movimientos WHERE user_id=? ORDER BY id DESC LIMIT 100"),(session['user'],))
    rows=c.fetchall(); con.close()
    return jsonify([{"tipo":r[0],"monto":r[1],"referencia":r[2],"detalle":r[3],"fecha":r[4]} for r in rows])


@app.route('/api/saldo')
def api_saldo():
    if 'user' not in session: return jsonify({"saldo":0})
    con=db(); c=con.cursor(); c.execute(q("SELECT saldo,pais FROM usuarios WHERE id=?"),(session['user'],)); row=c.fetchone(); con.close()
    pais=row[1] if row and row[1] in COUNTRY_CONFIG else 'USA'; cfg=get_country_config(pais); saldo_usd=float(row[0] or 0) if row else 0
    return jsonify({"saldo":saldo_usd,"saldo_local":usd_to_local(saldo_usd,cfg),"moneda":cfg['currency'],"pais":pais})

@app.route('/api/mis-apuestas')
def api_mis_apuestas():
    if 'user' not in session: return jsonify([])
    con=db(); c=con.cursor()
    c.execute(q("""
        SELECT s.id, s.fecha_hora_cierre, an.nombre, a.monto, s.animal_ganador, s.estado
        FROM apuestas a
        JOIN sorteos s ON s.id=a.sorteo_id
        JOIN animales an ON an.id=a.animal_id
        WHERE a.usuario_id=? ORDER BY a.fecha DESC LIMIT 50
    """), (session['user'],))
    rows=c.fetchall()
    lista=[]
    for r in rows:
        gano="En juego"
        if r[5] in ('PAGADO','FINALIZADO') and r[4]:
            try:
                c2=db(); cc=c2.cursor()
                cc.execute(q("SELECT nombre FROM animales WHERE id=?"), (r[4],))
                nom=cc.fetchone()
                gano=nom[0] if nom else f"ID {r[4]}"
                c2.close()
            except: gano=f"ID {r[4]}"
        lista.append({"sorteo":r[0],"fecha":r[1][:16] if r[1] else "","mi_animal":r[2],"monto":r[3],"ganador":gano,"estado":r[5]})
    con.close()
    return jsonify(lista)

@app.route('/api/historial-global')
def api_historial_global():
    con=db(); c=con.cursor()
    c.execute(q("""
        SELECT s.id, s.fecha_hora_cierre, an.nombre, s.recaudacion
        FROM sorteos s LEFT JOIN animales an ON an.id=s.animal_ganador
        WHERE s.estado IN ('PAGADO','FINALIZADO') AND s.animal_ganador IS NOT NULL
        ORDER BY s.id DESC LIMIT 30
    """))
    rows=c.fetchall(); con.close()
    return jsonify([{"id":r[0],"fecha":r[1][:16] if r[1] else "","ganador":r[2],"recaudado":r[3]} for r in rows])

@app.route('/api/historial-sorteos')
def api_historial_sorteos():
    con=db(); c=con.cursor()
    c.execute(q("SELECT id,fecha_hora_cierre,animal_ganador FROM sorteos WHERE estado IN ('PAGADO','FINALIZADO') AND animal_ganador IS NOT NULL ORDER BY id DESC LIMIT 30"))
    rows=c.fetchall(); con.close()
    return jsonify([{"id":r[0],"fecha":r[1][:16] if r[1] else "","animal_ganador":r[2]} for r in rows])

@app.route('/api/ultimo-resultado')
def api_ultimo_resultado():
    con=db(); c=con.cursor()
    c.execute(q("SELECT id,animal_ganador,estado FROM sorteos WHERE estado IN ('PAGADO','FINALIZADO') AND animal_ganador IS NOT NULL ORDER BY id DESC LIMIT 1"))
    r=c.fetchone(); con.close()
    return jsonify({"id":r[0] if r else None,"ganador":r[1] if r else None,"estado":r[2] if r else None})

@app.route('/api/mis-apuestas-actuales')
def api_mis_apuestas_actuales():
    if 'user' not in session: return jsonify({"apuestas":{}}),401
    con=db(); c=con.cursor()
    c.execute(q("SELECT id FROM sorteos WHERE estado='ABIERTO' ORDER BY id DESC LIMIT 1")); s=c.fetchone()
    if not s: con.close(); return jsonify({"apuestas":{}})
    sid=s[0]
    c.execute(q("SELECT animal_id,monto_local,monto FROM apuestas WHERE sorteo_id=? AND usuario_id=?"),(sid,session['user']))
    rows=c.fetchall(); con.close()
    apuestas={}
    for animal_id,monto_local,monto_usd in rows:
        apuestas[str(animal_id)] = float(monto_local if monto_local is not None else monto_usd)
    return jsonify({"apuestas":apuestas})

@app.route('/api/mis-retiros')
def api_mis_retiros():
    if 'user' not in session: return jsonify([]),401
    con=db(); c=con.cursor()
    c.execute(q("SELECT id,monto,monto_local,moneda,estado,fecha,banco_info FROM retiros WHERE user_id=? ORDER BY id DESC LIMIT 30"),(session['user'],))
    rows=c.fetchall(); con.close()
    return jsonify([{"id":r[0],"monto":r[1],"monto_local":r[2],"moneda":r[3],"estado":r[4],"fecha":r[5],"banco":r[6]} for r in rows])

# ========== ADMIN ==========
ADMIN_USER=os.environ.get("ADMIN_USER", "Globallotery")
ADMIN_PASSWORD=os.environ.get("ADMIN_PASSWORD")
ADMIN_PASS_HASH=os.environ.get("ADMIN_PASS_HASH", hash_pass(ADMIN_PASSWORD) if ADMIN_PASSWORD else "")

@app.route('/admin/login')
def admin_login_page(): return render_template('admin_login.html')

@app.route('/api/admin/login', methods=['POST'])
def api_admin_login():
    d=request.json or {}
    user=str(d.get('user','')); password=str(d.get('pass',''))
    if user==ADMIN_USER and ADMIN_PASS_HASH and verificar_password(password, ADMIN_PASS_HASH):
        session.clear(); session['admin']=True; session['admin_csrf']=secrets.token_urlsafe(32)
        return jsonify({"ok":True})
    return jsonify({"ok":False,"msg":"Credenciales incorrectas"}),401

@app.route('/admin/logout')
def admin_logout(): session.pop('admin',None); return redirect('/admin/login')

@app.route('/admin')
def admin_panel():
    if not session.get('admin'): return redirect('/admin/login')
    pausado, tiempo_min = get_config()
    con=db(); c=con.cursor()
    c.execute(q("SELECT * FROM sorteos WHERE estado='ABIERTO' ORDER BY id DESC LIMIT 1")); sorteo_actual=c.fetchone()
    sid = sorteo_actual[0] if sorteo_actual else 0
    c.execute(q("SELECT COALESCE(SUM(monto),0) FROM apuestas WHERE sorteo_id=?"), (sid,))
    recaudado = int(float(c.fetchone()[0] or 0))
    c.execute(q("SELECT r.id,r.user_id,r.monto,r.operacion,r.estado,r.fecha,r.voucher,r.monto_local,r.moneda,r.metodo_pago_nombre,r.metodo_pago_destino,r.nombre_remitente,u.email FROM recargas_bcp r LEFT JOIN usuarios u ON u.id=r.user_id WHERE r.estado='pendiente' ORDER BY r.id DESC")); recargas=c.fetchall()
    c.execute(q("SELECT COUNT(*) FROM usuarios")); num_usuarios=c.fetchone()[0] or 0
    con.close()
    tu_25 = int(recaudado*0.25)
    pagado_75 = int(recaudado*0.75)
    estado = "PAUSADO" if pausado else "ABIERTO"
    return render_template('admin.html', sorteo_actual=sorteo_actual, recargas_pendientes=recargas, bcp_cuenta=MI_CUENTA_BCP, estado=estado, tiempo_min=tiempo_min, recaudado=recaudado, tu_25=tu_25, pagado_75=pagado_75, num_usuarios=num_usuarios, recargas_count=len(recargas), pausado=pausado, csrf_token=session.get('admin_csrf',''))

@app.route('/api/admin/aprobar-recarga', methods=['POST'])
def aprobar_recarga():
    if not require_admin(request): return jsonify({"ok":False,"msg":"Sesión administrativa inválida"}),403
    if not session.get('admin'): return jsonify({"ok":False,"msg":"No admin"}),401
    con=None
    try:
        rid=int((request.json or {}).get("id",0)); con=db(); c=con.cursor()
        c.execute(q("SELECT r.user_id,r.monto,r.estado,u.email,u.pais,r.monto_local,r.moneda FROM recargas_bcp r JOIN usuarios u ON u.id=r.user_id WHERE r.id=?"),(rid,))
        row=c.fetchone()
        if not row: con.close(); return jsonify({"ok":False,"msg":"Recarga no encontrada"}),404
        if row[2]=='aprobado': con.close(); return jsonify({"ok":True,"msg":"La recarga ya estaba aprobada"})
        if row[2]!='pendiente': con.close(); return jsonify({"ok":False,"msg":"La recarga ya fue procesada"}),400
        c.execute(q("UPDATE usuarios SET saldo=saldo+? WHERE id=?"),(row[1],row[0]))
        registrar_movimiento(c, row[0], 'RECARGA', row[1], f'recarga:{rid}', 'Recarga aprobada por administrador')
        c.execute(q("UPDATE recargas_bcp SET estado='aprobado' WHERE id=?"),(rid,))
        pais_recarga = row[4] if row[4] in COUNTRY_CONFIG else 'USA'
        cfg_recarga = get_country_config(pais_recarga)
        recarga_local = format_local(float(row[5] or usd_to_local(float(row[1]), cfg_recarga)), cfg_recarga)
        con.commit(); con.close()
        enviar_correo_async(row[3],"Recarga aprobada - Globallotery",
                            f"Hola,\n\nTu recarga de {recarga_local} fue aprobada correctamente.\n"
                            "El monto ya fue acreditado a tu saldo.\n\nGloballotery")
        return jsonify({"ok":True,"msg":f"Aprobado {recarga_local}"})
    except Exception as e:
        if con:
            try: con.rollback(); con.close()
            except Exception: pass
        return jsonify({"ok":False,"msg":"No se pudo aprobar la recarga"}),500

@app.route('/api/admin/rechazar-recarga', methods=['POST'])
def rechazar_recarga():
    if not require_admin(request): return jsonify({"ok":False,"msg":"Sesión administrativa inválida"}),403
    if not session.get('admin'): return jsonify({"ok":False,"msg":"No admin"}),401
    con=None
    try:
        rid=int((request.json or {}).get("id",0)); con=db(); c=con.cursor()
        c.execute(q("SELECT r.estado,r.monto,u.email,r.user_id,u.pais,r.monto_local FROM recargas_bcp r JOIN usuarios u ON u.id=r.user_id WHERE r.id=?"),(rid,))
        row=c.fetchone()
        if not row: con.close(); return jsonify({"ok":False,"msg":"Recarga no encontrada"}),404
        if row[0]!='pendiente': con.close(); return jsonify({"ok":False,"msg":"La recarga ya fue procesada"}),400
        c.execute(q("UPDATE recargas_bcp SET estado='rechazado' WHERE id=?"),(rid,))
        con.commit(); con.close()
        enviar_correo_async(row[2], 'Recarga rechazada - Globallotery', f'Hola,\n\nTu recarga fue rechazada.\n\nGloballotery')
        return jsonify({"ok":True,"msg":"Recarga rechazada"})
    except Exception as e:
        if con:
            try: con.rollback(); con.close()
            except Exception: pass
        return jsonify({"ok":False,"msg":"No se pudo rechazar la recarga"}),500

@app.route('/api/admin/control', methods=['POST'])
def api_admin_control():
    if not require_admin(request): return jsonify({"ok":False,"msg":"Sesión administrativa inválida"}),403
    if not session.get('admin'): return jsonify({"ok":False})
    d=request.json; accion=d.get('accion')
    if accion=='pausar': set_config('pausado','1')
    if accion=='activar': set_config('pausado','0')
    if accion=='tiempo':
        set_config('tiempo_min', str(int(d.get('min',60))))
        con=db(); c=con.cursor()
        c.execute(q("UPDATE sorteos SET tiempo_min=? WHERE estado='ABIERTO'"), (int(d.get('min',60)),))
        con.commit(); con.close()
    if accion=='reiniciar':
        con=db(); c=con.cursor()
        c.execute(q("UPDATE sorteos SET estado='CERRADO' WHERE estado='ABIERTO'"))
        proximo=get_proximo_cierre_global()
        _, tiempo = get_config()
        c.execute(q("INSERT INTO sorteos (fecha_hora_cierre, estado, tiempo_min) VALUES (?, 'ABIERTO',?)"), (proximo.isoformat(), tiempo))
        con.commit(); con.close()
    return jsonify({"ok":True})

@app.route('/api/admin/metodos-pago')
def api_admin_metodos_pago():
    if not session.get('admin'):
        return jsonify({"ok":False,"msg":"No admin"}),401
    con=None
    try:
        con=db(); c=con.cursor()
        c.execute(q("""
            SELECT id,pais,nombre,tipo,destino,titular,banco,instrucciones,enlace,activo,actualizado
            FROM metodos_pago ORDER BY pais,id
        """))
        rows=c.fetchall(); con.close()
        return jsonify({"ok":True,"metodos":[
            {"id":r[0],"pais":r[1],"nombre":r[2],"tipo":r[3],"destino":r[4] or "","titular":r[5] or "","banco":r[6] or "","instrucciones":r[7] or "","enlace":r[8] or "","activo":bool(r[9]),"actualizado":r[10] or ""}
            for r in rows
        ]})
    except Exception as e:
        if con:
            try: con.close()
            except Exception: pass
        return jsonify({"ok":False,"msg":"No se pudieron cargar los métodos de pago"}),500

@app.route('/api/admin/metodos-pago', methods=['POST'])
def api_admin_metodos_pago_guardar():
    if not require_admin(request):
        return jsonify({"ok":False,"msg":"Sesión administrativa inválida"}),403
    if not session.get('admin'):
        return jsonify({"ok":False,"msg":"No admin"}),401
    d=request.json or {}
    try:
        mid=int(d.get('id',0) or 0)
        pais=str(d.get('pais','')).strip()
        nombre=str(d.get('nombre','')).strip()[:80]
        tipo=str(d.get('tipo','')).strip()[:40]
        destino=str(d.get('destino','')).strip()[:160]
        titular=str(d.get('titular','')).strip()[:120]
        banco=str(d.get('banco','')).strip()[:100]
        instrucciones=str(d.get('instrucciones','')).strip()[:300]
        enlace=str(d.get('enlace','')).strip()[:500]
        activo=1 if bool(d.get('activo',True)) else 0
    except Exception:
        return jsonify({"ok":False,"msg":"Datos inválidos"}),400
    if pais not in COUNTRY_CONFIG:
        return jsonify({"ok":False,"msg":"País no válido"}),400
    if not nombre or not tipo:
        return jsonify({"ok":False,"msg":"Nombre y tipo son obligatorios"}),400
    ahora=datetime.now().isoformat(); con=None
    try:
        con=db(); c=con.cursor()
        if mid>0:
            c.execute(q("UPDATE metodos_pago SET pais=?,nombre=?,tipo=?,destino=?,titular=?,banco=?,instrucciones=?,enlace=?,activo=?,actualizado=? WHERE id=?"),
                      (pais,nombre,tipo,destino,titular,banco,instrucciones,enlace,activo,ahora,mid))
            if c.rowcount != 1:
                con.rollback(); con.close(); return jsonify({"ok":False,"msg":"Método no encontrado"}),404
        else:
            c.execute(q("INSERT INTO metodos_pago (pais,nombre,tipo,destino,titular,banco,instrucciones,enlace,activo,actualizado) VALUES (?,?,?,?,?,?,?,?,?,?)"),
                      (pais,nombre,tipo,destino,titular,banco,instrucciones,enlace,activo,ahora))
            mid=c.lastrowid if not is_postgres() else None
        con.commit(); con.close()
        return jsonify({"ok":True,"msg":"Método de pago guardado","id":mid})
    except Exception as e:
        if con:
            try: con.rollback(); con.close()
            except Exception: pass
        print("ERROR GUARDANDO METODO PAGO:",e)
        return jsonify({"ok":False,"msg":"No se pudo guardar el método"}),500

@app.route('/api/admin/metodos-pago/eliminar', methods=['POST'])
def api_admin_metodos_pago_eliminar():
    if not require_admin(request):
        return jsonify({"ok":False,"msg":"Sesión administrativa inválida"}),403
    if not session.get('admin'):
        return jsonify({"ok":False,"msg":"No admin"}),401
    try: mid=int((request.json or {}).get('id',0))
    except Exception: return jsonify({"ok":False,"msg":"ID inválido"}),400
    if mid<=0: return jsonify({"ok":False,"msg":"ID inválido"}),400
    con=None
    try:
        con=db(); c=con.cursor(); c.execute(q("DELETE FROM metodos_pago WHERE id=?"),(mid,))
        if c.rowcount != 1:
            con.rollback(); con.close(); return jsonify({"ok":False,"msg":"Método no encontrado"}),404
        con.commit(); con.close(); return jsonify({"ok":True,"msg":"Método eliminado"})
    except Exception:
        if con:
            try: con.rollback(); con.close()
            except Exception: pass
        return jsonify({"ok":False,"msg":"No se pudo eliminar"}),500

@app.route('/api/admin/tasas')
def api_admin_tasas():
    if not session.get('admin'): return jsonify({"ok":False,"msg":"No admin"}),401
    con=None
    try:
        con=db(); c=con.cursor(); c.execute(q("SELECT pais,tasa_usd,actualizado FROM config_paises ORDER BY pais")); rows=c.fetchall(); con.close()
        data=[]
        for pais,tasa,actualizado in rows:
            cfg=COUNTRY_CONFIG.get(pais)
            if not cfg: continue
            data.append({"pais":pais,"nombre":cfg["name"],"moneda":cfg["currency"],"simbolo":cfg["symbol"],"tasa_usd":float(tasa),"actualizado":actualizado or ""})
        return jsonify({"ok":True,"tasas":data})
    except Exception:
        if con:
            try: con.close()
            except Exception: pass
        return jsonify({"ok":False,"msg":"No se pudieron cargar las tasas"}),500

@app.route('/api/admin/tasas', methods=['POST'])
def api_admin_tasas_guardar():
    if not require_admin(request): return jsonify({"ok":False,"msg":"Sesión administrativa inválida"}),403
    if not session.get('admin'): return jsonify({"ok":False,"msg":"No admin"}),401
    d=request.json or {}; pais=str(d.get("pais","")).strip()
    try: tasa=float(d.get("tasa_usd"))
    except Exception: return jsonify({"ok":False,"msg":"La tasa debe ser un número válido"}),400
    if pais not in COUNTRY_CONFIG: return jsonify({"ok":False,"msg":"País no válido"}),400
    if tasa<=0 or tasa>100000000: return jsonify({"ok":False,"msg":"La tasa debe ser mayor que 0"}),400
    ahora=datetime.now().isoformat(); con=None
    try:
        con=db(); c=con.cursor()
        if is_postgres():
            c.execute(q("INSERT INTO config_paises (pais,tasa_usd,actualizado) VALUES (?,?,?) ON CONFLICT (pais) DO UPDATE SET tasa_usd=EXCLUDED.tasa_usd, actualizado=EXCLUDED.actualizado"),(pais,tasa,ahora))
        else:
            c.execute(q("INSERT INTO config_paises (pais,tasa_usd,actualizado) VALUES (?,?,?) ON CONFLICT(pais) DO UPDATE SET tasa_usd=excluded.tasa_usd, actualizado=excluded.actualizado"),(pais,tasa,ahora))
        con.commit(); con.close(); COUNTRY_CONFIG[pais]["units_per_usd"]=tasa
        cfg=COUNTRY_CONFIG[pais]
        return jsonify({"ok":True,"msg":f"Tasa actualizada: 1 USD = {format_local(tasa,cfg)}","pais":pais,"tasa_usd":tasa,"actualizado":ahora})
    except Exception as e:
        if con:
            try: con.rollback(); con.close()
            except Exception: pass
        print("ERROR GUARDANDO TASA:",e)
        return jsonify({"ok":False,"msg":"No se pudo guardar la tasa"}),500

@app.route('/api/admin/ganancias')
def api_admin_ganancias():
    if not session.get('admin'): return jsonify([])
    con=db(); c=con.cursor()
    c.execute(q("SELECT id, fecha_hora_cierre, recaudacion, margen_plataforma, fondo_premios, estado FROM sorteos WHERE recaudacion IS NOT NULL AND recaudacion>0 ORDER BY id DESC LIMIT 20"))
    rows=c.fetchall(); con.close()
    data=[{"id":r[0],"fecha":r[1][:16] if r[1] else "","recaudado":int(float(r[2] or 0)),"tu25":int(float(r[3] or 0)),"pago75":int(float(r[4] or 0)),"estado":r[5]} for r in rows]
    return jsonify(data)


@app.route('/api/apuestas-en-vivo')
def api_apuestas_en_vivo():
    if 'user' not in session:
        return jsonify({"ok":False,"msg":"No logueado"}),401
    con=None
    try:
        con=db(); c=con.cursor()
        c.execute(q("SELECT pais FROM usuarios WHERE id=?"),(session['user'],))
        user_row=c.fetchone()
        pais=user_row[0] if user_row and user_row[0] in COUNTRY_CONFIG else 'USA'
        cfg=get_country_config(pais)
        c.execute(q("SELECT id FROM sorteos WHERE estado='ABIERTO' ORDER BY id DESC LIMIT 1"))
        sorteo=c.fetchone()
        if not sorteo:
            con.close()
            return jsonify({"ok":True,"animales_jugados":0,"cantidad_apuestas":0,"total_local":0,"lista":[]})
        sid=sorteo[0]
        c.execute(q("SELECT COUNT(*),COUNT(DISTINCT animal_id),COALESCE(SUM(monto),0) FROM apuestas WHERE sorteo_id=?"),(sid,))
        stats=c.fetchone(); cantidad=int(stats[0] or 0); animales=int(stats[1] or 0); total_usd=float(stats[2] or 0)
        c.execute(q("SELECT a.fecha,u.email,an.nombre,a.monto FROM apuestas a LEFT JOIN usuarios u ON u.id=a.usuario_id LEFT JOIN animales an ON an.id=a.animal_id WHERE a.sorteo_id=? ORDER BY a.id DESC LIMIT 20"),(sid,))
        rows=c.fetchall(); con.close()
        lista=[]
        for fecha,email,animal,monto in rows:
            email=(email or 'jugador').strip()
            if '@' in email:
                local,domain=email.split('@',1)
                jugador=(local[:2]+'***' if local else '***')+'@'+domain
            else:
                jugador='Jugador'
            hora=fecha[11:19] if fecha and len(fecha)>18 else (fecha or '')
            lista.append({"jugador":jugador,"hora":hora,"animal":animal or 'Animal',"monto_local":round_local_amount(usd_to_local(float(monto or 0),cfg),cfg)})
        return jsonify({"ok":True,"sorteo_id":sid,"animales_jugados":animales,"cantidad_apuestas":cantidad,"total_local":round_local_amount(usd_to_local(total_usd,cfg),cfg),"lista":lista})
    except Exception as e:
        if con:
            try: con.close()
            except Exception: pass
        print("ERROR APUESTAS EN VIVO:",e)
        return jsonify({"ok":False,"msg":"No se pudieron cargar las apuestas"}),500

@app.route('/api/admin/apuestas-actual')
def api_admin_apuestas_actual():
    if not session.get('admin'): return jsonify({"lista":[],"por_animal":[],"total":0,"sorteo_id":0})
    con=db(); c=con.cursor()
    c.execute(q("SELECT id FROM sorteos WHERE estado='ABIERTO' ORDER BY id DESC LIMIT 1"))
    s=c.fetchone()
    if not s:
        con.close()
        return jsonify({"lista":[],"por_animal":[],"total":0,"sorteo_id":0})
    sid=s[0]
    c.execute(q("SELECT a.fecha, u.email, an.nombre, a.monto FROM apuestas a LEFT JOIN usuarios u ON u.id=a.usuario_id LEFT JOIN animales an ON an.id=a.animal_id WHERE a.sorteo_id=? ORDER BY a.monto DESC LIMIT 100"), (sid,))
    rows=c.fetchall()
    c.execute(q("SELECT COALESCE(SUM(monto),0) FROM apuestas WHERE sorteo_id=?"), (sid,))
    total_real=float(c.fetchone()[0] or 0)
    c.execute(q("SELECT an.nombre, COUNT(a.id), COALESCE(SUM(a.monto),0) FROM apuestas a JOIN animales an ON an.id=a.animal_id WHERE a.sorteo_id=? GROUP BY an.nombre ORDER BY SUM(a.monto) DESC"), (sid,))
    por_animal=c.fetchall()
    con.close()
    lista=[{"fecha":r[0][11:19] if r[0] and len(r[0])>10 else (r[0] or ""),"email":r[1] or "anon","animal":r[2] or "??","monto":int(float(r[3] or 0))} for r in rows]
    por_json=[{"animal":r[0],"cantidad":r[1],"total":int(float(r[2]))} for r in por_animal]
    total=int(total_real)
    return jsonify({"lista":lista,"por_animal":por_json,"total":total,"sorteo_id":sid})

@app.route('/api/admin/retiros')
def api_admin_retiros():
    if not session.get('admin'): return jsonify([])
    con=db(); c=con.cursor()
    c.execute(q("""
        SELECT r.id, r.user_id, u.email, r.monto, r.monto_local, r.moneda,
               r.banco_info, r.estado, r.fecha, u.pais
        FROM retiros r
        LEFT JOIN usuarios u ON u.id=r.user_id
        ORDER BY r.id DESC
        LIMIT 50
    """))
    rows=c.fetchall(); con.close()
    lista=[]
    for r in rows:
        pais = r[9] if r[9] in COUNTRY_CONFIG else 'USA'
        cfg = get_country_config(pais)
        monto_local = r[4]
        if monto_local is None:
            try: monto_local = usd_to_local(float(r[3] or 0), cfg)
            except Exception: monto_local = r[3] or 0
        moneda = r[5] or cfg['currency']
        lista.append({
            "id": r[0],
            "user_id": r[1],
            "user": r[1],
            "email": r[2] or "",
            "monto": float(r[3] or 0),
            "monto_local": float(monto_local or 0),
            "moneda": moneda,
            "banco": r[6] or "",
            "estado": (r[7] or "pendiente").upper(),
            "fecha": r[8] or "",
            "pais": pais
        })
    return jsonify(lista)

@app.route('/admin/usuarios')
def admin_usuarios_page():
    if not session.get('admin'): return redirect('/admin/login')
    con=db(); c=con.cursor()
    c.execute(q("SELECT id,email,telefono,saldo FROM usuarios ORDER BY id DESC"))
    usuarios=c.fetchall(); con.close()
    return render_template('admin_usuarios.html', usuarios=usuarios)


if __name__=='__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT',10000)))