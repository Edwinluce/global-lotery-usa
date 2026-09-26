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
        c.execute("CREATE TABLE IF NOT EXISTS retiros (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, monto REAL, banco_info TEXT, estado TEXT, fecha TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS recargas_bcp (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, monto INTEGER, operacion TEXT, estado TEXT, fecha TEXT, voucher TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS correos_enviados (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, sorteo_id INTEGER, tipo TEXT, fecha TEXT, UNIQUE(user_id, sorteo_id, tipo))")
        c.execute("CREATE TABLE IF NOT EXISTS movimientos (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, tipo TEXT, monto REAL, referencia TEXT, detalle TEXT, fecha TEXT)")
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
        fondo=int(recaud*0.75); margen=int(recaud*0.25)

        if tot and tot>=1:
            seed_raw=f"{sid}-{datetime.now().isoformat()}-{secrets.token_hex(16)}"
            h=hashlib.sha256(seed_raw.encode()).hexdigest()
            random.seed(h); ganador=random.randint(1,25)

            c.execute(q("UPDATE sorteos SET animal_ganador=?,seed=?,hash_verificacion=?,recaudacion=?,fondo_premios=?,margen_plataforma=?,estado='RESULTADO_GENERADO' WHERE id=?"),
                      (ganador,seed_raw,h,recaud,fondo,margen,sid))
            c.execute(q("SELECT nombre FROM animales WHERE id=?"),(ganador,))
            animal_ganador=c.fetchone()[0]

            c.execute(q("SELECT usuario_id,SUM(monto) FROM apuestas WHERE sorteo_id=? AND animal_id=? GROUP BY usuario_id"),(sid,ganador))
            ganadores=c.fetchall()
            total_gan=sum(float(x[1]) for x in ganadores) or 1

            premios=[]; acumulado=0
            for uid,apostado in ganadores:
                premio=int(fondo*(float(apostado)/total_gan))
                premios.append([uid,premio]); acumulado += premio
            # El sobrante por redondeo se entrega al primer ganador para que el fondo se cuadre exactamente.
            if premios and acumulado < fondo:
                premios[0][1] += fondo-acumulado
            for uid,premio in premios:
                c.execute(q("UPDATE usuarios SET saldo=saldo+? WHERE id=?"),(premio,uid))
                registrar_movimiento(c, uid, 'PREMIO', premio, f'sorteo:{sid}', f'Premio por acertar {animal_ganador}')
            if ganadores:
                c.execute(q("UPDATE sorteos SET estado='PAGADO' WHERE id=?"),(sid,))
            else:
                c.execute(q("UPDATE sorteos SET jackpot=?,estado='FINALIZADO' WHERE id=?"),(fondo,sid))
            con.commit()

            c.execute(q("SELECT DISTINCT a.usuario_id,u.email FROM apuestas a JOIN usuarios u ON u.id=a.usuario_id WHERE a.sorteo_id=?"),(sid,))
            participantes=c.fetchall()
            for uid,email in participantes:
                c.execute(q("""
                    SELECT an.nombre,SUM(a.monto) FROM apuestas a JOIN animales an ON an.id=a.animal_id
                    WHERE a.sorteo_id=? AND a.usuario_id=? GROUP BY an.id,an.nombre ORDER BY an.id
                """),(sid,uid))
                seleccion="\n".join(f"- {x[0]}: S/{int(float(x[1]))}" for x in c.fetchall())
                c.execute(q("SELECT COALESCE(SUM(monto),0) FROM apuestas WHERE sorteo_id=? AND usuario_id=? AND animal_id=?"),
                          (sid,uid,ganador))
                acierto=float(c.fetchone()[0] or 0)

                if acierto>0:
                    premio=int(fondo*(acierto/total_gan))
                    c.execute(q("SELECT saldo FROM usuarios WHERE id=?"),(uid,))
                    saldo_actual=c.fetchone()[0]
                    mensaje=f"¡Felicidades! Acertaste.\nPremio acreditado: S/{premio}\nSaldo actual: S/{saldo_actual}"
                else:
                    mensaje="En este sorteo no acertaste el animal ganador."

                if registrar_correo_sorteo(uid,sid,"resultado"):
                    enviar_correo_async(
                        email,f"Resultado del sorteo #{sid}: {animal_ganador}",
                        f"Hola,\n\nEl sorteo #{sid} terminó.\n\n"
                        f"Tus animales elegidos:\n{seleccion}\n\n"
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
        d=request.json
        email=str(d.get('email','')).strip().lower()
        password=str(d.get('password',''))
        if '@' not in email or len(email)>254: return jsonify({"ok":False,"msg":"Correo inválido"}),400
        if len(password)<8 or len(password)>128: return jsonify({"ok":False,"msg":"La contraseña debe tener entre 8 y 128 caracteres"}),400
        pw=hash_pass(password)
        tel=str(d.get('telefono',''))[:30]
        con=db(); c=con.cursor()
        if is_postgres():
            c.execute(q("INSERT INTO usuarios (email,password,telefono,saldo,fecha_registro) VALUES (?,?,?,?,NOW()) RETURNING id"), (email,pw,tel,0))
            uid=c.fetchone()[0]
        else:
            c.execute(q("INSERT INTO usuarios (email,password,telefono,saldo,fecha_registro) VALUES (?,?,?,?,?)"), (email,pw,tel,0, datetime.now().isoformat()))
            uid=c.lastrowid
        con.commit()
        if is_postgres():
            c.execute(q("SELECT id FROM usuarios WHERE email=?"), (email,))
            uid=c.fetchone()[0]
        con.close()
        session['user']=uid; session['email']=email
        return jsonify({"ok":True})
    except Exception as e:
        print("ERROR REGISTER:", e)
        return jsonify({"ok":False,"msg": "Correo ya registrado" if "UNIQUE" in str(e) or "duplicate" in str(e).lower() else "Error: "+str(e)})

@app.route('/api/login', methods=['POST'])
def api_login():
    d=request.json or {}; email=str(d.get('email','')).strip().lower(); password=str(d.get('password',''))
    con=db(); c=con.cursor()
    c.execute(q("SELECT id,email,saldo,password FROM usuarios WHERE email=?"), (email,))
    row=c.fetchone()
    if not row:
        con.close(); return jsonify({"ok":False,"msg":"Credenciales incorrectas"})
    if not verificar_password(password, row[3]):
        con.close(); return jsonify({"ok":False,"msg":"Credenciales incorrectas"})
    # Migra silenciosamente contraseñas antiguas SHA-256 a scrypt.
    if not str(row[3]).startswith(('scrypt:','pbkdf2:')):
        c.execute(q("UPDATE usuarios SET password=? WHERE id=?"),(hash_pass(password),row[0])); con.commit()
    con.close(); session.clear(); session['user']=row[0]; session['email']=row[1]
    return jsonify({"ok":True, "saldo": row[2]})

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
    c.execute(q("SELECT saldo,email FROM usuarios WHERE id=?"), (session['user'],)); u=c.fetchone()
    c.execute(q("SELECT * FROM animales")); anims=c.fetchall()
    c.execute(q("SELECT fecha_hora_cierre, animal_ganador FROM sorteos WHERE estado IN ('PAGADO','FINALIZADO') ORDER BY id DESC LIMIT 24")); historial=c.fetchall()
    con.close()
    return render_template('player.html', sorteo=s, animales=anims, historial=historial, saldo=u[0] if u else 0, email=u[1] if u else '', bcp_cuenta=MI_CUENTA_BCP, bcp_cci=MI_CCI_BCP, bcp_link=MI_LINK_IZIPAY, bcp_nombre=MI_NOMBRE_BCP)

@app.route('/api/apostar-multiple', methods=['POST'])
def apostar_multiple():
    if 'user' not in session: return jsonify({"ok":False,"msg":"No logueado"}),401
    pausado,_ = get_config()
    if pausado: return jsonify({"ok":False,"msg":"Sala pausada por admin"}),400
    con=None
    try:
        data=(request.json or {}).get('apuestas',{})
        if not isinstance(data,dict) or not data:
            return jsonify({"ok":False,"msg":"Debes elegir al menos un animal"}),400
        apuestas={}
        for animal_id,monto in data.items():
            animal_id=int(animal_id); monto=validar_monto(monto, 10000)
            if animal_id<1 or animal_id>25:
                return jsonify({"ok":False,"msg":"Animal inválido"}),400
            apuestas[animal_id]=monto

        uid=session['user']; total=sum(apuestas.values())
        con=db(); c=con.cursor()
        c.execute(q("SELECT saldo,email FROM usuarios WHERE id=?"),(uid,))
        u=c.fetchone()
        if not u: con.close(); return jsonify({"ok":False,"msg":"Usuario no encontrado"}),404
        if float(u[0])<total:
            con.close(); return jsonify({"ok":False,"msg":f"Saldo insuficiente S/{u[0]}"}),400

        c.execute(q("SELECT id,fecha_hora_cierre FROM sorteos WHERE estado='ABIERTO' ORDER BY id DESC LIMIT 1"))
        row=c.fetchone()
        if not row: con.close(); return jsonify({"ok":False,"msg":"No hay un sorteo abierto"}),400
        sid, cierre=row
        try:
            if cierre and datetime.fromisoformat(str(cierre)) <= datetime.now():
                con.close(); return jsonify({"ok":False,"msg":"El sorteo ya cerró"}),400
        except Exception: pass
        # Reserva el saldo dentro de la misma transacción para evitar dobles gastos por solicitudes simultáneas.
        c.execute(q("UPDATE usuarios SET saldo=saldo-? WHERE id=? AND saldo>=?"),(total,uid,total))
        if c.rowcount != 1:
            con.rollback(); con.close(); return jsonify({"ok":False,"msg":"Saldo insuficiente"}),400
        nombres=[]

        for animal_id,monto in apuestas.items():
            c.execute(q("SELECT nombre FROM animales WHERE id=?"),(animal_id,))
            animal=c.fetchone()
            if not animal: raise ValueError("Animal inválido")
            nombres.append(f"- {animal[0]}: S/{monto}")
            c.execute(q("INSERT INTO apuestas (id,sorteo_id,usuario_id,animal_id,monto,fecha) VALUES (?,?,?,?,?,?)"),
                      (str(uuid.uuid4()),sid,uid,animal_id,monto,datetime.now().isoformat()))

        registrar_movimiento(c, uid, 'APUESTA', -total, f'sorteo:{sid}', 'Apuesta múltiple')
        con.commit(); con.close()

        enviar_correo_async(
            u[1],f"Confirmación de tu apuesta - Sorteo #{sid}",
            f"Hola,\n\nTu apuesta fue registrada correctamente.\n\n"
            f"Animales elegidos:\n" + "\n".join(nombres) +
            f"\n\nTotal apostado: S/{total}\n\n"
            "Cuando termine el sorteo recibirás un correo con el animal ganador.\n\nGloballotery"
        )
        return jsonify({"ok":True,"msg":"Apuesta registrada y correo enviado"})
    except Exception as e:
        if con:
            try: con.rollback(); con.close()
            except Exception: pass
        print("ERROR APOSTANDO:",e)
        return jsonify({"ok":False,"msg":"Error registrando la apuesta"}),500

@app.route('/api/recarga-bcp', methods=['POST'])
def recarga_bcp():
    uid = session.get('user')
    if not uid:
        email_fb = request.form.get('email_fallback','').strip().lower()
        if not email_fb and request.is_json:
            try: email_fb = (request.json.get('email_fallback','') or '').strip().lower()
            except: pass
        if email_fb:
            try:
                con_fb = db(); c_fb = con_fb.cursor()
                c_fb.execute(q("SELECT id FROM usuarios WHERE email=?"), (email_fb,))
                r = c_fb.fetchone()
                con_fb.close()
                if r:
                    uid = r[0]
                    session['user'] = uid
            except: pass
    if not uid:
        return jsonify({"ok":False,"msg":"No logueado"}),401

    try:
        monto=validar_monto(request.form.get('monto',0) or (request.json.get('monto',0) if request.is_json else 0), 100000)
    except ValueError as e:
        return jsonify({"ok":False,"msg":str(e)}),400
    operacion=str(request.form.get('operacion','') or (request.json.get('operacion','') if request.is_json else ''))[:100]
    if not operacion:
        return jsonify({"ok":False,"msg":"Falta el número de operación"}),400
    file=request.files.get('voucher'); voucher_path=""
    if file and file.filename:
        ext=os.path.splitext(file.filename)[1].lower()
        if ext not in {'.jpg','.jpeg','.png','.webp'}:
            return jsonify({"ok":False,"msg":"El voucher debe ser JPG, PNG o WEBP"}),400
        os.makedirs("static/vouchers", exist_ok=True)
        fname=secure_filename(f"{uid}_{int(datetime.now().timestamp())}_{secrets.token_hex(4)}{ext}")
        voucher_path=os.path.join("static/vouchers", fname); file.save(voucher_path)
    con=db(); c=con.cursor()
    c.execute(q("INSERT INTO recargas_bcp (user_id,monto,operacion,estado,fecha,voucher) VALUES (?,?,?,?,?,?)"),(uid, monto, operacion, 'pendiente', datetime.now().isoformat(), voucher_path))
    con.commit(); con.close()
    return jsonify({"ok":True,"msg":f"Voucher S/{monto} enviado"})

@app.route("/api/solicitar-retiro", methods=["POST"])
def solicitar_retiro():
    if 'user' not in session: return jsonify({"ok":False,"msg":"No logueado"}),401
    con=None
    try:
        data=request.get_json() or {}
        monto=validar_monto(data.get("monto",0), 100000); yape=str(data.get("yape","")).strip()
        if len(yape)>100: return jsonify({"ok":False,"msg":"Dato de retiro demasiado largo"}),400
        if not yape: return jsonify({"ok":False,"msg":"Indica el número/cuenta de retiro"}),400
        con=db(); c=con.cursor()
        c.execute(q("SELECT saldo,email FROM usuarios WHERE id=?"),(session['user'],))
        u=c.fetchone()
        if not u or float(u[0])<monto:
            con.close(); return jsonify({"ok":False,"msg":f"Saldo insuficiente S/{u[0] if u else 0}"}),400

        c.execute(q("UPDATE usuarios SET saldo=saldo-? WHERE id=? AND saldo>=?"),(monto,session['user'],monto))
        if c.rowcount != 1:
            con.rollback(); con.close(); return jsonify({"ok":False,"msg":"Saldo insuficiente"}),400
        c.execute(q("INSERT INTO retiros (user_id,monto,banco_info,estado,fecha) VALUES (?,?,?,?,?)"),
                  (session['user'],monto,yape,"pendiente",datetime.now().isoformat()))
        retiro_id=c.lastrowid if not is_postgres() else None
        registrar_movimiento(c, session['user'], 'RETIRO_RESERVADO', -monto, f'retiro:{retiro_id or "pendiente"}', 'Saldo reservado para retiro')
        con.commit(); con.close()
        enviar_correo_async(u[1],"Solicitud de retiro recibida - Globallotery",
                            f"Hola,\n\nRecibimos tu solicitud de retiro por S/{monto}.\n"
                            f"Cuenta/número: {yape}\n\nPendiente de aprobación por el administrador.\n\nGloballotery")
        return jsonify({"ok":True,"msg":"Solicitud enviada. El saldo quedó reservado."})
    except Exception as e:
        if con:
            try: con.rollback(); con.close()
            except Exception: pass
        return jsonify({"ok":False,"msg":"No se pudo crear el retiro"}),500

@app.route("/api/aprobar-retiro", methods=["POST"])
def aprobar_retiro():
    if not require_admin(request): return jsonify({"ok":False,"msg":"Sesión administrativa inválida"}),403
    if not session.get('admin'): return jsonify({"ok":False,"msg":"No admin"}),401
    con=None
    try:
        rid=int((request.get_json() or {}).get("id",0)); con=db(); c=con.cursor()
        c.execute(q("SELECT r.user_id,r.monto,r.estado,u.email FROM retiros r JOIN usuarios u ON u.id=r.user_id WHERE r.id=?"),(rid,))
        r=c.fetchone()
        if not r: con.close(); return jsonify({"ok":False,"msg":"No existe"}),404
        if r[2]=='aprobado': con.close(); return jsonify({"ok":True,"msg":"Ya estaba aprobado"})
        if r[2]=='rechazado': con.close(); return jsonify({"ok":False,"msg":"El retiro ya fue rechazado"}),400
        c.execute(q("UPDATE retiros SET estado='aprobado' WHERE id=?"),(rid,))
        registrar_movimiento(c, r[0], 'RETIRO_APROBADO', 0, f'retiro:{rid}', f'Retiro aprobado por S/{r[1]}')
        con.commit(); con.close()
        enviar_correo_async(r[3],"Retiro aprobado - Globallotery",
                            f"Hola,\n\nTu retiro de S/{r[1]} fue APROBADO.\n"
                            "El pago puede ser procesado al medio registrado.\n\nGloballotery")
        return jsonify({"ok":True,"msg":"Retiro aprobado correctamente"})
    except Exception as e:
        if con:
            try: con.rollback(); con.close()
            except Exception: pass
        return jsonify({"ok":False,"msg":"No se pudo aprobar el retiro"}),500

@app.route("/api/rechazar-retiro", methods=["POST"])
def rechazar_retiro():
    if not require_admin(request): return jsonify({"ok":False,"msg":"Sesión administrativa inválida"}),403
    if not session.get('admin'): return jsonify({"ok":False,"msg":"No admin"}),401
    con=None
    try:
        rid=int((request.get_json() or {}).get("id",0)); con=db(); c=con.cursor()
        c.execute(q("SELECT r.user_id,r.monto,r.estado,u.email FROM retiros r JOIN usuarios u ON u.id=r.user_id WHERE r.id=?"),(rid,))
        r=c.fetchone()
        if not r: con.close(); return jsonify({"ok":False,"msg":"No existe"}),404
        if r[2]=='rechazado': con.close(); return jsonify({"ok":True,"msg":"Ya estaba rechazado"})
        if r[2]=='aprobado': con.close(); return jsonify({"ok":False,"msg":"No se puede rechazar un retiro aprobado"}),400
        c.execute(q("UPDATE usuarios SET saldo=saldo+? WHERE id=?"),(r[1],r[0]))
        registrar_movimiento(c, r[0], 'RETIRO_DEVUELTO', r[1], f'retiro:{rid}', 'Retiro rechazado; saldo devuelto')
        c.execute(q("UPDATE retiros SET estado='rechazado' WHERE id=?"),(rid,))
        con.commit(); con.close()
        enviar_correo_async(r[3],"Retiro rechazado - Globallotery",
                            f"Hola,\n\nTu retiro de S/{r[1]} fue rechazado.\n"
                            f"El monto S/{r[1]} fue devuelto a tu saldo.\n\nGloballotery")
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
    con=db(); c=con.cursor(); c.execute(q("SELECT saldo FROM usuarios WHERE id=?"),(session['user'],)); row=c.fetchone(); con.close()
    return jsonify({"saldo":row[0] if row else 0})

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
    c.execute(q("SELECT r.*, u.email FROM recargas_bcp r LEFT JOIN usuarios u ON u.id=r.user_id WHERE r.estado='pendiente' ORDER BY r.id DESC")); recargas=c.fetchall()
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
        c.execute(q("SELECT r.user_id,r.monto,r.estado,u.email FROM recargas_bcp r JOIN usuarios u ON u.id=r.user_id WHERE r.id=?"),(rid,))
        row=c.fetchone()
        if not row: con.close(); return jsonify({"ok":False,"msg":"Recarga no encontrada"}),404
        if row[2]=='aprobado': con.close(); return jsonify({"ok":True,"msg":"La recarga ya estaba aprobada"})
        if row[2]!='pendiente': con.close(); return jsonify({"ok":False,"msg":"La recarga ya fue procesada"}),400
        c.execute(q("UPDATE usuarios SET saldo=saldo+? WHERE id=?"),(row[1],row[0]))
        registrar_movimiento(c, row[0], 'RECARGA', row[1], f'recarga:{rid}', 'Recarga aprobada por administrador')
        c.execute(q("UPDATE recargas_bcp SET estado='aprobado' WHERE id=?"),(rid,))
        con.commit(); con.close()
        enviar_correo_async(row[3],"Recarga aprobada - Globallotery",
                            f"Hola,\n\nTu recarga de S/{row[1]} fue aprobada correctamente.\n"
                            "El monto ya fue acreditado a tu saldo.\n\nGloballotery")
        return jsonify({"ok":True,"msg":f"Aprobado S/{row[1]}"})
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
        c.execute(q("SELECT r.estado,r.monto,u.email,r.user_id FROM recargas_bcp r JOIN usuarios u ON u.id=r.user_id WHERE r.id=?"),(rid,))
        row=c.fetchone()
        if not row: con.close(); return jsonify({"ok":False,"msg":"Recarga no encontrada"}),404
        if row[0]!='pendiente': con.close(); return jsonify({"ok":False,"msg":"La recarga ya fue procesada"}),400
        c.execute(q("UPDATE recargas_bcp SET estado='rechazado' WHERE id=?"),(rid,))
        con.commit(); con.close()
        enviar_correo_async(row[2], 'Recarga rechazada - Globallotery', f'Hola,\n\nTu recarga de S/{row[1]} fue rechazada.\n\nGloballotery')
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

@app.route('/api/admin/ganancias')
def api_admin_ganancias():
    if not session.get('admin'): return jsonify([])
    con=db(); c=con.cursor()
    c.execute(q("SELECT id, fecha_hora_cierre, recaudacion, margen_plataforma, fondo_premios, estado FROM sorteos WHERE recaudacion IS NOT NULL AND recaudacion>0 ORDER BY id DESC LIMIT 20"))
    rows=c.fetchall(); con.close()
    data=[{"id":r[0],"fecha":r[1][:16] if r[1] else "","recaudado":int(float(r[2] or 0)),"tu25":int(float(r[3] or 0)),"pago75":int(float(r[4] or 0)),"estado":r[5]} for r in rows]
    return jsonify(data)

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
    c.execute(q("SELECT r.id, u.email, r.monto, r.banco_info, r.estado, r.fecha FROM retiros r LEFT JOIN usuarios u ON u.id=r.user_id WHERE r.estado='pendiente' ORDER BY r.id DESC"))
    rows=c.fetchall(); con.close()
    return jsonify([{"id":r[0],"email":r[1],"monto":r[2],"banco":r[3],"estado":r[4],"fecha":r[5]} for r in rows])

@app.route('/admin/usuarios')
def admin_usuarios_page():
    if not session.get('admin'): return redirect('/admin/login')
    con=db(); c=con.cursor()
    c.execute(q("SELECT id,email,telefono,saldo FROM usuarios ORDER BY id DESC"))
    usuarios=c.fetchall(); con.close()
    return render_template('admin_usuarios.html', usuarios=usuarios)


if __name__=='__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT',10000)))