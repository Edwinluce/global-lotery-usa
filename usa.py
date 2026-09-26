import os, psycopg2, smtplib, threading, uuid
from flask import Flask, render_template, request, redirect, session, jsonify, url_for
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from werkzeug.utils import secure_filename
import hashlib

app=Flask(__name__)
app.secret_key=os.environ.get('SECRET_KEY','globallotery-secreto-2025')
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = False
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)
MI_CUENTA_BCP="191-12345678-0-12 - Yape: 999888777"
DATABASE_URL=os.environ.get('DATABASE_URL')

# EMAIL CONFIG - Render Environment
EMAIL_USER = os.environ.get('EMAIL_USER')
EMAIL_PASS = os.environ.get('EMAIL_PASS')

def enviar_correo_async(destinatario, asunto, mensaje_html):
    def enviar():
        try:
            if not EMAIL_USER or not EMAIL_PASS:
                print("Email no configurado en Render")
                return
            msg = MIMEMultipart()
            msg['From'] = f"Global Lotery <{EMAIL_USER}>"
            msg['To'] = destinatario
            msg['Subject'] = asunto
            msg.attach(MIMEText(mensaje_html, 'html'))
            server = smtplib.SMTP('smtp.gmail.com', 587)
            server.starttls()
            server.login(EMAIL_USER, EMAIL_PASS.replace(" ",""))
            server.send_message(msg)
            server.quit()
            print(f"Correo enviado a {destinatario}")
        except Exception as e:
            print(f"Error enviando correo a {destinatario}: {e}")
    threading.Thread(target=enviar, daemon=True).start()

def q(query):
    if DATABASE_URL and DATABASE_URL.startswith("postgres"):
        return query.replace("?", "%s")
    return query

def db():
    if DATABASE_URL:
        return psycopg2.connect(DATABASE_URL, sslmode='require')
    import sqlite3
    return sqlite3.connect('local.db')

def hash_pass(p): return hashlib.sha256(p.encode()).hexdigest()
def check_pass(p,h): return hash_pass(p)==h

def _get_col_retiros():
    con=db(); c=con.cursor()
    try:
        c.execute(q("SELECT column_name FROM information_schema.columns WHERE table_name='retiros'"))
        cols=[r[0] for r in c.fetchall()]
        con.close()
        if 'usuario_id' in cols: return 'usuario_id'
        if 'user_id' in cols: return 'user_id'
        return 'usuario_id'
    except:
        con.close()
        return 'usuario_id'

def get_config():
    con=db(); c=con.cursor()
    try:
        c.execute(q("SELECT clave, valor FROM configuracion"))
        d=dict(c.fetchall())
        pausado = d.get('pausado','0')=='1'
        tiempo = int(d.get('tiempo_min','60'))
    except:
        pausado=False; tiempo=60
    con.close()
    return pausado, tiempo

def set_config(clave, valor):
    con=db(); c=con.cursor()
    c.execute(q("INSERT INTO configuracion (clave, valor) VALUES (?,?) ON CONFLICT (clave) DO UPDATE SET valor=?") if DATABASE_URL else q("INSERT OR REPLACE INTO configuracion (clave, valor) VALUES (?,?)"), (clave, valor, valor) if DATABASE_URL else (clave, valor))
    con.commit(); con.close()

def get_proximo_cierre_global():
    _, tiempo_min = get_config()
    return datetime.now() + timedelta(minutes=tiempo_min)

def sortear():
    con=db(); c=con.cursor()
    c.execute(q("SELECT id FROM sorteos WHERE estado='ABIERTO' ORDER BY id DESC LIMIT 1"))
    s=c.fetchone()
    if not s: con.close(); return
    sid=s[0]
    c.execute(q("SELECT fecha_hora_cierre FROM sorteos WHERE id=?"), (sid,))
    row=c.fetchone()
    if not row: con.close(); return
    try:
        cierre=datetime.fromisoformat(row[0])
    except:
        con.close(); return
    if datetime.now() < cierre:
        con.close(); return

    c.execute(q("SELECT COALESCE(SUM(monto),0) FROM apuestas WHERE sorteo_id=?"), (sid,))
    recaud=int(float(c.fetchone()[0] or 0))
    if recaud==0:
        proximo=get_proximo_cierre_global()
        _, tiempo = get_config()
        c.execute(q("UPDATE sorteos SET fecha_hora_cierre=?, tiempo_min=? WHERE id=?"), (proximo.isoformat(), tiempo, sid))
        con.commit(); con.close()
        return

    import random
    c.execute(q("SELECT id FROM animales"))
    animales=[r[0] for r in c.fetchall()]
    ganador=random.choice(animales) if animales else 1
    margen=int(recaud*0.25)
    fondo=int(recaud*0.75)
    c.execute(q("UPDATE sorteos SET animal_ganador=?, recaudacion=?, margen_plataforma=?, fondo_premios=?, estado='FINALIZADO' WHERE id=?"), (ganador, recaud, margen, fondo, sid))

    c.execute(q("SELECT nombre FROM animales WHERE id=?"), (ganador,))
    rg=c.fetchone()
    nombre_ganador = rg[0] if rg else f"Animal {ganador}"

    c.execute(q("SELECT usuario_id, SUM(monto) FROM apuestas WHERE sorteo_id=? AND animal_id=? GROUP BY usuario_id"), (sid, ganador))
    ganadores=c.fetchall()
    c.execute(q("SELECT DISTINCT a.usuario_id, u.email FROM apuestas a JOIN usuarios u ON u.id=a.usuario_id WHERE a.sorteo_id=?"), (sid,))
    todos=c.fetchall()

    if ganadores:
        total_gan=sum([g[1] for g in ganadores]) or 1
        for uid, apostado in ganadores:
            premio=int(fondo*(apostado/total_gan))
            c.execute(q("UPDATE usuarios SET saldo=saldo+? WHERE id=?"), (premio, uid))
            c.execute(q("SELECT email FROM usuarios WHERE id=?"), (uid,))
            em=c.fetchone()
            if em and em[0]:
                html=f"""<div style="font-family:Arial;background:#111;color:#fff;padding:20px;border-radius:15px"><h1 style="color:#1ee66d">🎉 ¡GANASTE! 🎉</h1><h2>Salió: 🦁 {nombre_ganador}</h2><p>Apostaste S/ {apostado} a {nombre_ganador}</p><p style="font-size:24px;background:#1ee66d;color:#000;padding:15px;border-radius:10px;text-align:center"><b>Premio: S/ {premio}</b></p><p>Tu saldo ya fue acreditado.</p></div>"""
                enviar_correo_async(em[0], f"🎉 GANASTE S/ {premio} con {nombre_ganador}! Sorteo #{sid}", html)
        ids_g=[g[0] for g in ganadores]
        for uid,email in todos:
            if uid not in ids_g and email:
                html=f"""<div style="font-family:Arial;background:#111;color:#fff;padding:20px;border-radius:15px"><h2 style="color:#ff2d2d">😢 Esta vez no ganaste</h2><h3>Salió: 🦁 {nombre_ganador}</h3><p>Sorteo #{sid} - ¡El próximo ya está abierto!</p></div>"""
                enviar_correo_async(email, f"Resultado Sorteo #{sid}: Salió {nombre_ganador}", html)
        c.execute(q("UPDATE sorteos SET estado='PAGADO' WHERE id=?"), (sid,))
    else:
        for uid,email in todos:
            if email:
                html=f"""<div style="font-family:Arial;background:#111;color:#fff;padding:20px;border-radius:15px"><h2>Resultado Sorteo #{sid}</h2><h3>🦁 Salió: {nombre_ganador}</h3><p>Nadie acertó. El fondo se va a Jackpot.</p></div>"""
                enviar_correo_async(email, f"Resultado Sorteo #{sid}: {nombre_ganador} - Nadie ganó", html)
        c.execute(q("UPDATE sorteos SET jackpot=?, estado='FINALIZADO' WHERE id=?"), (fondo, sid))

    _, tiempo = get_config()
    proximo=get_proximo_cierre_global()
    c.execute(q("INSERT INTO sorteos (fecha_hora_cierre, estado, tiempo_min) VALUES (?, 'ABIERTO',?)"), (proximo.isoformat(), tiempo))
    con.commit(); con.close()

def init_db():
    con=db(); c=con.cursor()
    try:
        c.execute(q("CREATE TABLE IF NOT EXISTS usuarios (id TEXT PRIMARY KEY, email TEXT UNIQUE, password TEXT, telefono TEXT, saldo INTEGER DEFAULT 0)"))
        c.execute(q("CREATE TABLE IF NOT EXISTS animales (id INTEGER PRIMARY KEY, nombre TEXT)"))
        c.execute(q("CREATE TABLE IF NOT EXISTS sorteos (id SERIAL PRIMARY KEY, fecha_hora_cierre TEXT, estado TEXT, tiempo_min INTEGER DEFAULT 60, animal_ganador INTEGER, recaudacion INTEGER, margen_plataforma INTEGER, fondo_premios INTEGER, jackpot INTEGER)"))
        c.execute(q("CREATE TABLE IF NOT EXISTS apuestas (id TEXT PRIMARY KEY, sorteo_id INTEGER, usuario_id TEXT, animal_id INTEGER, monto INTEGER, fecha TEXT)"))
        c.execute(q("CREATE TABLE IF NOT EXISTS recargas_bcp (id SERIAL PRIMARY KEY, user_id TEXT, monto INTEGER, numero_operacion TEXT, fecha_operacion TEXT, titular TEXT, voucher_path TEXT, estado TEXT, fecha TEXT)"))
        c.execute(q("CREATE TABLE IF NOT EXISTS retiros (id SERIAL PRIMARY KEY, usuario_id TEXT, monto INTEGER, banco_info TEXT, estado TEXT, fecha TEXT)"))
        c.execute(q("CREATE TABLE IF NOT EXISTS configuracion (clave TEXT PRIMARY KEY, valor TEXT)"))
        c.execute(q("SELECT COUNT(*) FROM animales"));
        if c.fetchone()[0]==0:
            animales=['Perro','Gato','Tigre','León','Elefante','Mono','Cebra','Oso','Lobo','Águila','Tortuga','Serpiente','Delfín','Caballo','Vaca','Gallina','Cerdo','Conejo','Panda','Koala','Jirafa','Hipopótamo','Rinoceronte','Cocodrilo','Pingüino']
            for i,n in enumerate(animales,1):
                c.execute(q("INSERT INTO animales (id,nombre) VALUES (?,?) ON CONFLICT DO NOTHING") if DATABASE_URL else q("INSERT OR IGNORE INTO animales (id,nombre) VALUES (?,?)"), (i,n))
        c.execute(q("SELECT COUNT(*) FROM sorteos WHERE estado='ABIERTO'"))
        if c.fetchone()[0]==0:
            proximo=get_proximo_cierre_global()
            _, tiempo = get_config()
            c.execute(q("INSERT INTO sorteos (fecha_hora_cierre, estado, tiempo_min) VALUES (?, 'ABIERTO',?)"), (proximo.isoformat(), tiempo))
        con.commit()
    except Exception as e:
        print("init_db error", e)
    con.close()

init_db()

@app.route('/')
def index():
    pausado,_=get_config()
    con=db(); c=con.cursor()
    c.execute(q("SELECT id, fecha_hora_cierre, tiempo_min FROM sorteos WHERE estado='ABIERTO' ORDER BY id DESC LIMIT 1"))
    sorteo=c.fetchone()
    con.close()
    return render_template('index.html', sorteo=sorteo, pausado=pausado)

@app.route('/api/sorteo-actual')
def api_sorteo_actual():
    sortear()
    con=db(); c=con.cursor()
    c.execute(q("SELECT id, fecha_hora_cierre, estado FROM sorteos WHERE estado='ABIERTO' ORDER BY id DESC LIMIT 1"))
    s=c.fetchone()
    con.close()
    if not s: return jsonify({"id":0,"cierre":"","estado":"CERRADO"})
    return jsonify({"id":s[0],"cierre":s[1],"estado":s[2]})

@app.route('/api/animales')
def api_animales():
    con=db(); c=con.cursor()
    c.execute(q("SELECT id, nombre FROM animales ORDER BY id"))
    rows=c.fetchall(); con.close()
    return jsonify([{"id":r[0],"nombre":r[1]} for r in rows])

@app.route('/api/apostar-multiple', methods=['POST'])
def apostar_multiple():
    if 'user' not in session: return jsonify({"ok":False,"msg":"No logueado"})
    pausado,_ = get_config()
    if pausado: return jsonify({"ok":False,"msg":"Sala pausada por admin"})
    data=request.json['apuestas']; uid=session['user']
    con=db(); c=con.cursor()
    c.execute(q("SELECT saldo, email FROM usuarios WHERE id=?"), (uid,))
    row = c.fetchone()
    saldo = row[0] if row else 0
    email_user = row[1] if row and len(row)>1 else ""
    total=sum([int(v) for v in data.values()])
    if saldo < total: con.close(); return jsonify({"ok":False,"msg":f"Saldo insuficiente S/{saldo}"})
    c.execute(q("SELECT id FROM sorteos WHERE estado='ABIERTO' ORDER BY id DESC LIMIT 1"))
    s=c.fetchone()
    if not s: con.close(); return jsonify({"ok":False,"msg":"No hay sorteo abierto"})
    sid=s[0]
    nombres_apuestas = []
    for animal_id, monto in data.items():
        c.execute(q("SELECT nombre FROM animales WHERE id=?"), (int(animal_id),))
        nom = c.fetchone()
        nom = nom[0] if nom else f"Animal {animal_id}"
        nombres_apuestas.append(f"{nom} - S/{monto}")
        c.execute(q("INSERT INTO apuestas (id,sorteo_id,usuario_id,animal_id,monto,fecha) VALUES (?,?,?,?,?,?)"), (str(uuid.uuid4()), sid, uid, int(animal_id), int(monto), datetime.now().isoformat()))
    c.execute(q("UPDATE usuarios SET saldo=saldo-? WHERE id=?"), (total, uid))
    con.commit(); con.close()
    if email_user:
        lista_html = "<br>".join([f"🦁 {x}" for x in nombres_apuestas])
        html = f"""<div style="font-family:Arial;background:#111;color:#fff;padding:20px;border-radius:15px"><h2 style="color:#e9c97a">✅ Apuesta Confirmada - Sorteo #{sid}</h2><p>Has apostado en Global Lotery:</p><p style="background:#222;padding:15px;border-radius:10px">{lista_html}</p><p><b>Total:</b> S/ {total}</p><p>¡Suerte! Te avisaremos el resultado por correo.</p></div>"""
        enviar_correo_async(email_user, f"✅ Apuesta confirmada Sorteo #{sid} - S/ {total}", html)
    return jsonify({"ok":True})

@app.route('/api/login', methods=['POST'])
def api_login():
    d=request.json; email=d.get('email','').strip().lower(); password=d.get('password','')
    con=db(); c=con.cursor(); c.execute(q("SELECT id, password FROM usuarios WHERE email=?"), (email,)); row=c.fetchone(); con.close()
    if row and check_pass(password, row[1]): session.permanent = True 
    session['user']=row[0]; return jsonify({"ok":True})
    return jsonify({"ok":False,"msg":"Credenciales incorrectas"})

@app.route('/api/register', methods=['POST'])
def api_register():
    d=request.json; email=d.get('email','').strip().lower(); password=d.get('password',''); telefono=d.get('telefono','')
    if not email or not password: return jsonify({"ok":False,"msg":"Falta email/pass"})
    con=db(); c=con.cursor()
    try:
        uid=str(uuid.uuid4())
        c.execute(q("INSERT INTO usuarios (id,email,password,telefono,saldo) VALUES (?,?,?,?,0)"), (uid, email, hash_pass(password), telefono))
        con.commit();session.permanent = True
        session['user']=uid
        con.close()
        return jsonify({"ok":True})
    except Exception as e:
        con.close()
        return jsonify({"ok":False,"msg":"Email ya registrado"})

@app.route('/api/logout', methods=['POST'])
def api_logout(): session.pop('user',None); return jsonify({"ok":True})

@app.route('/api/recarga-bcp', methods=['POST'])
def recarga_bcp():
    if 'user' not in session: return jsonify({"ok":False,"msg":"No logueado"})
    monto=request.form.get('monto'); oper=request.form.get('operacion'); titular=request.form.get('titular',''); fecha_op=request.form.get('fecha_op','')
    file=request.files.get('voucher')
    path=""
    if file:
        fname=secure_filename(file.filename)
        os.makedirs('static/vouchers', exist_ok=True)
        path=f"static/vouchers/{uuid.uuid4()}_{fname}"
        file.save(path)
    con=db(); c=con.cursor()
    c.execute(q("INSERT INTO recargas_bcp (user_id,monto,numero_operacion,fecha_operacion,titular,voucher_path,estado,fecha) VALUES (?,?,?,?,?,?,?,?)"), (session['user'], int(monto), oper, fecha_op, titular, path, 'pendiente', datetime.now().isoformat()))
    con.commit(); con.close()
    return jsonify({"ok":True,"msg":"Recarga enviada, espera aprobación"})
@app.route("/api/solicitar-retiro", methods=["POST"])
def solicitar_retiro():
    if 'user' not in session: return jsonify({"ok":False,"msg":"No logueado"}),401
    data=request.get_json(); monto=int(float(data.get("monto",0))); yape=data.get("yape","")
    con=db(); c=con.cursor()
    c.execute(q("SELECT saldo FROM usuarios WHERE id=?"), (session['user'],))
    u=c.fetchone()
    if not u or u[0] < monto:
        con.close()
        return jsonify({"ok":False,"msg":f"Saldo insuficiente S/ {u[0] if u else 0}"})
    col = _get_col_retiros()
    c.execute(q(f"INSERT INTO retiros ({col}, monto, banco_info, estado, fecha) VALUES (?,?,?,?,?)"), (session['user'], monto, yape, 'PENDIENTE', datetime.now().isoformat()))
    con.commit(); con.close()
    return jsonify({"ok":True, "msg": f"Solicitud de retiro S/ {monto} guardada"})

@app.route('/api/admin/retiros')
def api_admin_retiros():
    if not session.get('admin'): return jsonify([])
    con=db(); c=con.cursor()
    col = _get_col_retiros()
    try:
        c.execute(q(f"SELECT r.id, u.email, r.monto, r.banco_info, r.estado, r.fecha FROM retiros r LEFT JOIN usuarios u ON u.id=r.{col} ORDER BY r.id DESC"))
        rows=c.fetchall()
    except Exception as e:
        print("ERROR RETIROS:", e)
        rows=[]
    con.close()
    return jsonify([{"id":r[0],"user":r[1] or "sin email","email":r[1] or "sin email","monto":float(r[2] or 0),"banco":r[3],"estado":r[4],"fecha":str(r[5])[:19] if r[5] else ""} for r in rows])

@app.route('/api/saldo')
def api_saldo():
    if 'user' not in session: return jsonify({"saldo":0})
    con=db(); c=con.cursor(); c.execute(q("SELECT saldo FROM usuarios WHERE id=?"),(session['user'],)); row=c.fetchone(); con.close()
    return jsonify({"saldo":row[0] if row else 0})

@app.route('/api/mis-apuestas')
def api_mis_apuestas():
    if 'user' not in session: return jsonify([])
    con=db(); c=con.cursor()
    c.execute(q("SELECT s.id, s.fecha_hora_cierre, an.nombre, a.monto, s.animal_ganador, s.estado FROM apuestas a JOIN sorteos s ON s.id=a.sorteo_id JOIN animales an ON an.id=a.animal_id WHERE a.usuario_id=? ORDER BY a.fecha DESC LIMIT 50"), (session['user'],))
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
    c.execute(q("SELECT s.id, s.fecha_hora_cierre, an.nombre, s.recaudacion FROM sorteos s LEFT JOIN animales an ON an.id=s.animal_ganador WHERE s.estado IN ('PAGADO','FINALIZADO') AND s.animal_ganador IS NOT NULL ORDER BY s.id DESC LIMIT 30"))
    rows=c.fetchall(); con.close()
    return jsonify([{"id":r[0],"fecha":r[1][:16] if r[1] else "","ganador":r[2],"recaudado":r[3]} for r in rows])

ADMIN_USER="Globallotery"; ADMIN_PASS_HASH=hash_pass("Diosmeama.1")

@app.route('/admin/login')
def admin_login_page(): return render_template('admin_login.html')

@app.route('/api/admin/login', methods=['POST'])
def api_admin_login():
    d=request.json
    if d['user']==ADMIN_USER and hash_pass(d['pass'])==ADMIN_PASS_HASH:
        session['admin']=True; return jsonify({"ok":True})
    return jsonify({"ok":False})

@app.route('/api/admin/retiros/aprobar', methods=['POST'])
def api_aprobar_retiro():
    if not session.get('admin'): return jsonify({"ok":False})
    rid = request.json.get('id')
    con=db(); c=con.cursor()
    col = _get_col_retiros()
    c.execute(q(f"SELECT {col}, monto FROM retiros WHERE id=?"), (rid,))
    ret = c.fetchone()
    if not ret: con.close(); return jsonify({"ok":False})
    uid, monto = ret
    c.execute(q("UPDATE usuarios SET saldo = saldo -? WHERE id=?"), (monto, uid))
    c.execute(q("UPDATE retiros SET estado='APROBADO' WHERE id=?"), (rid,))
    con.commit(); con.close()
    return jsonify({"ok":True, "msg":f"Retiro #{rid} APROBADO, se descontó {monto}"})

@app.route('/api/admin/retiros/rechazar', methods=['POST'])
def api_rechazar_retiro():
    if not session.get('admin'): return jsonify({"ok":False})
    rid = request.json.get('id')
    con=db(); c=con.cursor()
    c.execute(q("UPDATE retiros SET estado='RECHAZADO' WHERE id=?"), (rid,))
    con.commit(); con.close()
    return jsonify({"ok":True})

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
    recaudado_demo=210 if recaudado==0 and len(recargas)==0 else recaudado
    tu_25 = int(recaudado_demo*0.25)
    pagado_75 = int(recaudado_demo*0.75)
    estado = "PAUSADO" if pausado else "ABIERTO"
    return render_template('admin.html', sorteo_actual=sorteo_actual, recargas_pendientes=recargas, bcp_cuenta=MI_CUENTA_BCP, estado=estado, tiempo_min=tiempo_min, recaudado=recaudado_demo, tu_25=tu_25, pagado_75=pagado_75, num_usuarios=num_usuarios, recargas_count=len(recargas), pausado=pausado)

@app.route('/api/admin/aprobar-recarga', methods=['POST'])
def aprobar_recarga():
    if not session.get('admin'): return jsonify({"ok":False})
    d=request.json; rid=d['id']
    con=db(); c=con.cursor()
    c.execute(q("SELECT user_id, monto FROM recargas_bcp WHERE id=?"), (rid,))
    row=c.fetchone()
    if row:
        c.execute(q("UPDATE usuarios SET saldo=saldo+? WHERE id=?"), (row[1], row[0]))
        c.execute(q("UPDATE recargas_bcp SET estado='aprobado' WHERE id=?"), (rid,))
        con.commit()
    con.close()
    return jsonify({"ok":True,"msg":f"Aprobado S/{row[1]}" if row else "Error"})

@app.route('/api/admin/control', methods=['POST'])
def api_admin_control():
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
    c.execute(q("SELECT an.nombre, COUNT(a.id), COALESCE(SUM(a.monto),0) FROM apuestas a JOIN animales an ON an.id=a.animal_id WHERE a.sorteo_id=? GROUP BY an.nombre ORDER BY SUM(a.monto) DESC"), (sid,))
    por_animal=c.fetchall()
    con.close()
    lista=[{"fecha":r[0][11:19] if r[0] and len(r[0])>10 else (r[0] or ""),"email":r[1] or "anon","animal":r[2] or "??","monto":int(float(r[3] or 0))} for r in rows]
    por_json=[{"animal":r[0],"cantidad":r[1],"total":int(float(r[2]))} for r in por_animal]
    total=sum([x["monto"] for x in lista])
    return jsonify({"lista":lista,"por_animal":por_json,"total":total,"sorteo_id":sid})

@app.route('/admin/usuarios')
def admin_usuarios_page():
    if not session.get('admin'): return redirect('/admin/login')
    con=db(); c=con.cursor()
    c.execute(q("SELECT id,email,telefono,saldo FROM usuarios ORDER BY id DESC"))
    usuarios=c.fetchall(); con.close()
    return render_template('admin_usuarios.html', usuarios=usuarios)

if __name__=='__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT',10000)))