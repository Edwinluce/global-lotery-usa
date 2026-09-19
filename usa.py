from flask import Flask, render_template, request, jsonify, session, redirect
import sqlite3, hashlib, random, secrets, uuid, os
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = 'global-lottery-2025-segura'

MI_CUENTA_BCP = "19106864219053"
MI_CCI_BCP = "00219110686421905358"
MI_LINK_IZIPAY = "https://izipayya.page.link/TU_LINK_AQUI"
MI_NOMBRE_BCP = "Globallotery"

DATABASE_URL = os.environ.get('DATABASE_URL')

def is_postgres():
    return DATABASE_URL is not None and DATABASE_URL!= ""

def db():
    if is_postgres():
        import psycopg2
        return psycopg2.connect(DATABASE_URL)
    else:
        return sqlite3.connect('animalitos.db', check_same_thread=False)

def q(query):
    return query.replace('?', '%s') if is_postgres() else query

def hash_pass(p):
    return hashlib.sha256(p.encode()).hexdigest()

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
    else:
        c.execute("CREATE TABLE IF NOT EXISTS usuarios (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE, password TEXT, telefono TEXT, saldo REAL DEFAULT 0, fecha_registro TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS animales (id INTEGER PRIMARY KEY, nombre TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS sorteos (id INTEGER PRIMARY KEY AUTOINCREMENT, fecha_hora_cierre TEXT, animal_ganador INTEGER, estado TEXT, seed TEXT, hash_verificacion TEXT, recaudacion REAL, fondo_premios REAL, margen_plataforma REAL, jackpot REAL, tiempo_min INTEGER DEFAULT 60)")
        c.execute("CREATE TABLE IF NOT EXISTS apuestas (id TEXT PRIMARY KEY, sorteo_id INTEGER, usuario_id INTEGER, animal_id INTEGER, monto REAL, fecha TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS config (k TEXT PRIMARY KEY, v TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS retiros (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, monto REAL, banco_info TEXT, estado TEXT, fecha TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS recargas_bcp (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, monto INTEGER, operacion TEXT, estado TEXT, fecha TEXT, voucher TEXT)")
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

def sortear():
    pausado,_ = get_config()
    if pausado: return
    con = db(); c = con.cursor()
    c.execute(q("SELECT id FROM sorteos WHERE estado='ABIERTO' AND fecha_hora_cierre <=?"), (datetime.now().isoformat(),))
    row = c.fetchone()
    if not row: con.close(); return
    sid=row[0]
    c.execute(q("UPDATE sorteos SET estado='CERRADO' WHERE id=?"), (sid,)); con.commit()
    c.execute(q("SELECT COUNT(*), COALESCE(SUM(monto),0) FROM apuestas WHERE sorteo_id=?"), (sid,))
    tot, recaud = c.fetchone(); recaud=recaud or 0; fondo=int(recaud*0.75); margen=int(recaud*0.25)
    if tot and tot>=1:
        seed_raw=f"{sid}-{datetime.now().isoformat()}-{secrets.token_hex(16)}"; h=hashlib.sha256(seed_raw.encode()).hexdigest()
        random.seed(h); ganador=random.randint(1,25)
        c.execute(q("UPDATE sorteos SET animal_ganador=?, seed=?, hash_verificacion=?, recaudacion=?, fondo_premios=?, margen_plataforma=?, estado='RESULTADO_GENERADO' WHERE id=?"), (ganador, seed_raw, h, recaud, fondo, margen, sid))
        c.execute(q("SELECT usuario_id, SUM(monto) FROM apuestas WHERE sorteo_id=? AND animal_id=? GROUP BY usuario_id"), (sid, ganador))
        ganadores=c.fetchall()
        if ganadores:
            total_gan=sum([g[1] for g in ganadores]) or 1
            for uid, apostado in ganadores:
                premio=int(fondo*(apostado/total_gan)); c.execute(q("UPDATE usuarios SET saldo=saldo+? WHERE id=?"), (premio, uid))
            c.execute(q("UPDATE sorteos SET estado='PAGADO' WHERE id=?"), (sid,))
        else: c.execute(q("UPDATE sorteos SET jackpot=?, estado='FINALIZADO' WHERE id=?"), (fondo, sid))
    else: c.execute(q("UPDATE sorteos SET recaudacion=?, fondo_premios=?, margen_plataforma=?, jackpot=?, estado='FINALIZADO' WHERE id=?"), (recaud,fondo,margen,fondo,sid))
    con.commit(); proximo=get_proximo_cierre_global()
    _, tiempo = get_config()
    c.execute(q("INSERT INTO sorteos (fecha_hora_cierre, estado, tiempo_min) VALUES (?, 'ABIERTO',?)"), (proximo.isoformat(), tiempo))
    con.commit(); con.close()

scheduler=BackgroundScheduler()
scheduler.add_job(sortear,'interval', seconds=60)
scheduler.start()

@app.route('/login')
def login_page(): return render_template('login.html')

@app.route('/api/register', methods=['POST'])
def api_register():
    try:
        d=request.json; email=d['email'].strip().lower(); pw=hash_pass(d['password']); tel=d.get('telefono','')
        con=db(); c=con.cursor()
        c.execute(q("INSERT INTO usuarios (email,password,telefono,saldo,fecha_registro) VALUES (?,?,?,?,?)"), (email,pw,tel,0,datetime.now().isoformat()))
        con.commit(); uid=c.lastrowid if not is_postgres() else c.fetchone() or 1
        if is_postgres():
            c.execute(q("SELECT id FROM usuarios WHERE email=?"),(email,)); uid=c.fetchone()[0]
        con.close()
        session['user']=uid; session['email']=email
        return jsonify({"ok":True})
    except Exception as e:
        print("ERROR REGISTER:", e)
        return jsonify({"ok":False,"msg": "Correo ya registrado" if "UNIQUE" in str(e) or "duplicate" in str(e).lower() else str(e)})

@app.route('/api/login', methods=['POST'])
def api_login():
    d=request.json; email=d['email'].strip().lower(); pw=hash_pass(d['password'])
    con=db(); c=con.cursor()
    c.execute(q("SELECT id,email,saldo FROM usuarios WHERE email=? AND password=?"), (email,pw))
    row=c.fetchone(); con.close()
    if row:
        session['user']=row[0]; session['email']=row[1]
        return jsonify({"ok":True, "saldo": row[2]})
    return jsonify({"ok":False,"msg":"Credenciales incorrectas"})

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
    if 'user' not in session: return jsonify({"ok":False,"msg":"No logueado"})
    pausado,_ = get_config()
    if pausado: return jsonify({"ok":False,"msg":"Sala pausada por admin"})
    data=request.json['apuestas']; uid=session['user']
    con=db(); c=con.cursor(); c.execute(q("SELECT saldo FROM usuarios WHERE id=?"), (uid,)); saldo=c.fetchone()[0]
    total=sum([int(v) for v in data.values()])
    if saldo < total: con.close(); return jsonify({"ok":False,"msg":f"Saldo insuficiente S/{saldo}"})
    c.execute(q("SELECT id FROM sorteos WHERE estado='ABIERTO' ORDER BY id DESC LIMIT 1")); sid=c.fetchone()[0]
    for animal_id, monto in data.items():
        c.execute(q("INSERT INTO apuestas (id,sorteo_id,usuario_id,animal_id,monto,fecha) VALUES (?,?,?,?,?,?)"), (str(uuid.uuid4()), sid, uid, int(animal_id), int(monto), datetime.now().isoformat()))
    c.execute(q("UPDATE usuarios SET saldo=saldo-? WHERE id=?"), (total, uid))
    con.commit(); con.close()
    return jsonify({"ok":True})

@app.route('/api/recarga-bcp', methods=['POST'])
def recarga_bcp():
    if 'user' not in session: return jsonify({"ok":False})
    monto=int(request.form.get('monto',0) or (request.json.get('monto',0) if request.is_json else 0))
    operacion=request.form.get('operacion','') or (request.json.get('operacion','') if request.is_json else '')
    file=request.files.get('voucher'); voucher_path=""
    if file:
        os.makedirs("static/vouchers", exist_ok=True)
        fname=secure_filename(f"{session['user']}_{operacion}_{int(datetime.now().timestamp())}.jpg")
        voucher_path=os.path.join("static/vouchers", fname); file.save(voucher_path)
    con=db(); c=con.cursor()
    c.execute(q("INSERT INTO recargas_bcp (user_id,monto,operacion,estado,fecha,voucher) VALUES (?,?,?,?,?,?)"),(session['user'], monto, operacion, 'pendiente', datetime.now().isoformat(), voucher_path))
    con.commit(); con.close()
    return jsonify({"ok":True,"msg":f"Voucher S/{monto} enviado"})

@app.route("/api/solicitar-retiro", methods=["POST"])
def solicitar_retiro():
    if 'user' not in session: return jsonify({"ok":False,"msg":"No logueado"}),401
    data=request.get_json(); monto=int(float(data.get("monto",0))); yape=data.get("yape","")
    con=db(); c=con.cursor(); c.execute(q("SELECT saldo FROM usuarios WHERE id=?"), (session['user'],)); u=c.fetchone()
    if not u or u[0] < monto: con.close(); return jsonify({"ok":False,"msg":f"Saldo insuficiente S/{u[0] if u else 0}"})
    c.execute(q("UPDATE usuarios SET saldo=saldo-? WHERE id=?"), (monto, session['user']))
    c.execute(q("INSERT INTO retiros (user_id, monto, banco_info, estado, fecha) VALUES (?,?,?,?,?)"), (session['user'], monto, yape, "pendiente", datetime.now().isoformat()))
    con.commit(); con.close()
    return jsonify({"ok":True})

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
ADMIN_USER="Globallotery"; ADMIN_PASS_HASH=hash_pass("Diosmeama.1")

@app.route('/admin/login')
def admin_login_page(): return render_template('admin_login.html')

@app.route('/api/admin/login', methods=['POST'])
def api_admin_login():
    d=request.json
    if d['user']==ADMIN_USER and hash_pass(d['pass'])==ADMIN_PASS_HASH:
        session['admin']=True; return jsonify({"ok":True})
    return jsonify({"ok":False})

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
    if recaudado==0 and len(recargas)==0: recaudado_demo=210
    else: recaudado_demo=recaudado
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

@app.route('/api/admin/test-apuesta')
def test_apuesta():
    if not session.get('admin'): return "no auth"
    con=db(); c=con.cursor()
    c.execute(q("SELECT id FROM sorteos WHERE estado='ABIERTO' ORDER BY id DESC LIMIT 1")); sid=c.fetchone()[0]
    c.execute(q("SELECT id FROM usuarios LIMIT 1")); u=c.fetchone()
    if not u:
        c.execute(q("INSERT INTO usuarios (email,password,telefono,saldo,fecha_registro) VALUES (?,?,?,?,?)"),('test@test.com','123','999',1000,datetime.now().isoformat()))
        con.commit()
        c.execute(q("SELECT id FROM usuarios LIMIT 1")); u=c.fetchone()
    uid=u[0]
    c.execute(q("INSERT INTO apuestas (id,sorteo_id,usuario_id,animal_id,monto,fecha) VALUES (?,?,?,?,?,?)"), (str(uuid.uuid4()), sid, uid, 7, 50, datetime.now().isoformat()))
    c.execute(q("INSERT INTO apuestas (id,sorteo_id,usuario_id,animal_id,monto,fecha) VALUES (?,?,?,?,?,?)"), (str(uuid.uuid4()), sid, uid, 1, 100, datetime.now().isoformat()))
    con.commit(); con.close()
    return "Apuestas de prueba creadas! Ahora anda a /admin y dale a Animales Apostados"

if __name__=='__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT',10000)))