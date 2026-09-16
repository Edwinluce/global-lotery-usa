from flask import Flask, render_template, request, jsonify, session, redirect
import sqlite3, hashlib, random, secrets, uuid, os
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = 'global-lottery-2025'

MI_CUENTA_BCP = "19106864219053"
MI_CCI_BCP = "00219110686421905358"
MI_LINK_IZIPAY = "https://izipayya.page.link/TU_LINK_AQUI"
MI_NOMBRE_BCP = "Globallotery"

def db():
    return sqlite3.connect('animalitos.db', check_same_thread=False)

def hash_pass(p):
    return hashlib.sha256(p.encode()).hexdigest()

# === FIX GLOBAL - CIERRE A LA HORA EN PUNTO IGUAL PARA TODOS ===
def get_proximo_cierre_global():
    ahora = datetime.now()
    proximo = ahora.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return proximo

def init_db():
    con = db()
    c = con.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS usuarios (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE, password TEXT, telefono TEXT, saldo REAL DEFAULT 0, fecha_registro TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS animales (id INTEGER PRIMARY KEY, nombre TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS sorteos (id INTEGER PRIMARY KEY AUTOINCREMENT, fecha_hora_cierre TEXT, animal_ganador INTEGER, estado TEXT, seed TEXT, hash_verificacion TEXT, recaudacion REAL, fondo_premios REAL, margen_plataforma REAL, jackpot REAL)")
    c.execute("CREATE TABLE IF NOT EXISTS apuestas (id TEXT PRIMARY KEY, sorteo_id INTEGER, usuario_id TEXT, animal_id INTEGER, monto REAL, fecha TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS config (k TEXT PRIMARY KEY, v TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS retiros (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, monto REAL, banco_info TEXT, estado TEXT, fecha TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS recargas (id INTEGER PRIMARY KEY AUTOINCREMENT, usuario_id INTEGER, monto INTEGER, fecha TEXT, tarjeta TEXT, operacion TEXT, estado TEXT DEFAULT 'pendiente', voucher TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS recargas_bcp (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, monto INTEGER, operacion TEXT, estado TEXT, fecha TEXT, voucher TEXT)")
    c.execute("SELECT COUNT(*) FROM animales")
    if c.fetchone()[0]==0:
        noms=["Perro","Gato","Ratón","Conejo","Zorro","Tigre","León","Elefante","Mono","Gallina","Gallo","Cerdo","Vaca","Caballo","Alpaca","Vicuña","Cóndor","Oso","Puma","Gallito","Caimán","Serpiente","Rana","Delfin","Guacamayo"]
        for i,n in enumerate(noms):
            c.execute("INSERT INTO animales VALUES (?,?)",(i+1,n))
    c.execute("SELECT id FROM sorteos WHERE estado='ABIERTO' LIMIT 1")
    if not c.fetchone():
        proximo = get_proximo_cierre_global()
        c.execute("INSERT INTO sorteos (fecha_hora_cierre, estado) VALUES (?, 'ABIERTO')",(proximo.isoformat(),))
    con.commit()
    con.close()   
    

def get_config():
    try:
        con=db(); c=con.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS config (k TEXT PRIMARY KEY, v TEXT)")
        c.execute("SELECT v FROM config WHERE k='pausado'"); r=c.fetchone()
        pausado = r[0]=='1' if r else False
        c.execute("SELECT v FROM config WHERE k='tiempo_min'"); r=c.fetchone()
        tiempo = int(r[0]) if r and str(r[0]).isdigit() else 60
        con.close()
        return pausado, tiempo
    except:
        return False, 60

def set_config(k,v):
    con=db(); c=con.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS config (k TEXT PRIMARY KEY, v TEXT)")
    c.execute("INSERT OR REPLACE INTO config (k,v) VALUES (?,?)",(k,str(v)))
    con.commit(); con.close()

def sortear():
    pausado,_ = get_config()
    if pausado:
        return
    con = db(); c = con.cursor()
    c.execute("SELECT id FROM sorteos WHERE estado='ABIERTO' AND fecha_hora_cierre <=?", (datetime.now().isoformat(),))
    row = c.fetchone()
    if not row:
        con.close()
        return
    sid=row[0]
    c.execute("UPDATE sorteos SET estado='CERRADO' WHERE id=?", (sid,)); con.commit()
    c.execute("SELECT COUNT(*), COALESCE(SUM(monto),0) FROM apuestas WHERE sorteo_id=?", (sid,))
    tot, recaud = c.fetchone(); recaud=recaud or 0; fondo=int(recaud*0.75); margen=int(recaud*0.25)
    if tot and tot>=1:
        seed_raw=f"{sid}-{datetime.now().isoformat()}-{secrets.token_hex(16)}"; h=hashlib.sha256(seed_raw.encode()).hexdigest()
        random.seed(h); ganador=random.randint(1,25)
        c.execute("UPDATE sorteos SET animal_ganador=?, seed=?, hash_verificacion=?, recaudacion=?, fondo_premios=?, margen_plataforma=?, estado='RESULTADO_GENERADO' WHERE id=?", (ganador, seed_raw, h, recaud, fondo, margen, sid))
        c.execute("SELECT usuario_id, SUM(monto) FROM apuestas WHERE sorteo_id=? AND animal_id=? GROUP BY usuario_id", (sid, ganador))
        ganadores=c.fetchall()
        if ganadores:
            total_gan=sum([g[1] for g in ganadores]) or 1
            for uid, apostado in ganadores:
                premio=int(fondo*(apostado/total_gan)); c.execute("UPDATE usuarios SET saldo=saldo+? WHERE id=?", (premio, uid))
            c.execute("UPDATE sorteos SET estado='PAGADO' WHERE id=?", (sid,))
        else:
            c.execute("UPDATE sorteos SET jackpot=?, estado='FINALIZADO' WHERE id=?", (fondo, sid))
    else:
        c.execute("UPDATE sorteos SET recaudacion=?, fondo_premios=?, margen_plataforma=?, jackpot=?, estado='FINALIZADO' WHERE id=?", (recaud,fondo,margen,fondo,sid))
    con.commit()
    proximo=get_proximo_cierre_global()
    c.execute("INSERT INTO sorteos (fecha_hora_cierre, estado) VALUES (?, 'ABIERTO')", (proximo.isoformat(),))
    con.commit(); con.close()

scheduler=BackgroundScheduler()
scheduler.add_job(sortear,'interval', seconds=20)
scheduler.start()

@app.route('/login')
def login_page(): return render_template('login.html')

@app.route('/api/register', methods=['POST'])
def api_register():
    d=request.json; email=d['email']; pw=hash_pass(d['password']); tel=d.get('telefono','')
    con=db(); c=con.cursor()
    try:
        c.execute("INSERT INTO usuarios (email,password,telefono,saldo,fecha_registro) VALUES (?,?,?,?,?)", (email,pw,tel,0,datetime.now().isoformat()))
        con.commit(); uid=c.lastrowid; con.close()
        session['user']=uid; session['email']=email
        return jsonify({"ok":True})
    except:
        con.close(); return jsonify({"ok":False,"msg":"Correo ya registrado"})

@app.route('/api/login', methods=['POST'])
def api_login():
    d=request.json; email=d['email']; pw=hash_pass(d['password']) if 'password' in d else hash_pass(d.get('pass',''))
    con=db(); c=con.cursor()
    c.execute("SELECT id,email,saldo FROM usuarios WHERE email=? AND password=?", (email,pw))
    row=c.fetchone(); con.close()
    if row or 'google' in email:
        if not row:
            con=db(); c=con.cursor()
            c.execute("INSERT OR IGNORE INTO usuarios (email,password,saldo) VALUES (?,?,0)", (email, pw))
            con.commit(); c.execute("SELECT id FROM usuarios WHERE email=?", (email,)); uid=c.fetchone()[0]; con.close()
        else: uid=row[0]
        session['user']=uid; session['email']=email
        return jsonify({"ok":True, "saldo": row[2] if row else 0})
    return jsonify({"ok":False})

@app.route('/logout')
def logout(): session.clear(); return redirect('/login')

@app.route('/')
def player():
    if 'user' not in session:
        return redirect('/login')
    pausado, tiempo_min = get_config()
    con=db(); c=con.cursor()
    c.execute("SELECT * FROM sorteos WHERE estado='ABIERTO' ORDER BY id DESC LIMIT 1")
    s=c.fetchone()
    if not s:
        proximo = get_proximo_cierre_global()
        c.execute("INSERT INTO sorteos (fecha_hora_cierre, estado) VALUES (?, 'ABIERTO')",(proximo.isoformat(),))
        con.commit()
        c.execute("SELECT * FROM sorteos WHERE estado='ABIERTO' ORDER BY id DESC LIMIT 1")
        s=c.fetchone()
    c.execute("SELECT saldo,email FROM usuarios WHERE id=?", (session['user'],)); u=c.fetchone()
    c.execute("SELECT * FROM animales"); anims=c.fetchall()
    c.execute("SELECT fecha_hora_cierre, animal_ganador FROM sorteos WHERE estado IN ('PAGADO','FINALIZADO') ORDER BY id DESC LIMIT 24"); historial=c.fetchall()
    c.execute("SELECT fecha_hora_cierre, animal_ganador, fondo_premios FROM sorteos WHERE estado='PAGADO' ORDER BY id DESC LIMIT 20"); resultados=c.fetchall()
    con.close()
    return render_template('player.html', sorteo=s, animales=anims, historial=historial, resultados=resultados, saldo=u[0] if u else 0, email=u[1] if u else '', bcp_cuenta=MI_CUENTA_BCP, bcp_cci=MI_CCI_BCP, bcp_link=MI_LINK_IZIPAY, bcp_nombre=MI_NOMBRE_BCP)

@app.route('/api/apostar-multiple', methods=['POST'])
def apostar_multiple():
    if 'user' not in session: return jsonify({"ok":False})
    data=request.json['apuestas']; uid=session['user']
    pausado,_ = get_config()
    if pausado:
        return jsonify({"ok":False,"msg":"Sala pausada por admin"})
    con=db(); c=con.cursor(); c.execute("SELECT saldo FROM usuarios WHERE id=?", (uid,)); saldo=c.fetchone()[0]
    total=sum([int(v) for v in data.values()])
    if saldo < total: con.close(); return jsonify({"ok":False,"msg":f"Saldo insuficiente S/{saldo}. BCP {MI_CUENTA_BCP}"})
    c.execute("SELECT id, fecha_hora_cierre FROM sorteos WHERE estado='ABIERTO' ORDER BY id DESC LIMIT 1"); sid_row=c.fetchone()
    if not sid_row or datetime.fromisoformat(sid_row[1]) <= datetime.now():
        proximo=get_proximo_cierre_global()
        c.execute("INSERT INTO sorteos (fecha_hora_cierre, estado) VALUES (?, 'ABIERTO')", (proximo.isoformat(),))
        sid_row=(c.lastrowid, proximo.isoformat())
    sid=sid_row[0]
    for animal_id, monto in data.items():
        c.execute("INSERT INTO apuestas (id,sorteo_id,usuario_id,animal_id,monto,fecha) VALUES (?,?,?,?,?,?)", (str(uuid.uuid4()), sid, uid, int(animal_id), int(monto), datetime.now().isoformat()))
    c.execute("UPDATE usuarios SET saldo=saldo-? WHERE id=?", (total, uid))
    con.commit(); con.close()
    return jsonify({"ok":True})
@app.route('/api/recarga-bcp', methods=['POST'])
@app.route('/api/recarga-tarjeta', methods=['POST'])
def recarga_tarjeta():
    if 'user' not in session: return jsonify({"ok":False})
    if request.is_json:
        d=request.json; monto=int(d.get('monto',0)); operacion=d.get('operacion',''); nombre=d.get('nombre','')
    else:
        monto=int(request.form.get('monto',0)); operacion=request.form.get('operacion',''); nombre=request.form.get('nombre','')
    if monto < 10: return jsonify({"ok":False,"msg":"Minimo S/10"})
    file=request.files.get('voucher'); voucher_path=""
    if file:
        os.makedirs("static/vouchers", exist_ok=True)
        fname=secure_filename(f"{session['user']}_{operacion}_{int(datetime.now().timestamp())}.jpg")
        voucher_path=os.path.join("static/vouchers", fname); file.save(voucher_path)
    con=db(); c=con.cursor()
    c.execute("INSERT INTO recargas (usuario_id,monto,fecha,tarjeta,operacion,estado,voucher) VALUES (?,?,?,?,?,?,?)",(session['user'], monto, datetime.now().isoformat(), nombre[-4:] if nombre else 'BCP', operacion, 'pendiente', voucher_path))
    c.execute("INSERT INTO recargas_bcp (user_id,monto,operacion,estado,fecha,voucher) VALUES (?,?,?,?,?,?)",(session['user'], monto, operacion, 'pendiente', datetime.now().isoformat(), voucher_path))
    con.commit(); con.close()
    return jsonify({"ok":True,"msg":f"Voucher S/{monto} enviado BCP {MI_CUENTA_BCP}"})

@app.route("/api/solicitar-retiro", methods=["POST"])
def solicitar_retiro():
    try:
        print("SESSION ACTUAL:", dict(session)) # para ver en logs de Render
        # buscar el usuario logueado con cualquier nombre posible
        uid = session.get("user_id") or session.get("usuario_id") or session.get("id") or session.get("uid")
        email_sess = session.get("email") or session.get("usuario") or session.get("user")

        con=db(); c=con.cursor()

        # si no tenemos id pero tenemos email, buscamos el id por email
        if not uid and email_sess:
            c.execute("SELECT id FROM usuarios WHERE email=?", (email_sess,))
            r=c.fetchone()
            if r: uid=r[0]

        if not uid:
            return jsonify({"ok": False, "msg": f"No logueado - sesión: {dict(session)}"}), 401

        data = request.get_json()
        monto = int(float(data.get("monto",0)))
        yape = data.get("yape") or data.get("banco_info") or data.get("yape_plin") or data.get("numero") or ""

        if monto < 10:
            return jsonify({"ok":False,"msg":"Mínimo S/10"})

        c.execute("SELECT saldo FROM usuarios WHERE id=?", (uid,))
        u=c.fetchone()
        if not u or (u[0] or 0) < monto:
            return jsonify({"ok":False,"msg":f"Saldo insuficiente. Tienes S/{u[0] if u else 0}"})

        # descontar al solicitar
        c.execute("UPDATE usuarios SET saldo = saldo -? WHERE id=?", (monto, uid))

        c.execute("SELECT * FROM retiros LIMIT 1")
        cols = [d[0] for d in c.description] if c.description else ["id","user_id","monto","banco_info","estado","fecha"]

        if "user_id" in cols:
            c.execute("INSERT INTO retiros (user_id, monto, banco_info, estado, fecha) VALUES (?,?,?,?, datetime('now','localtime'))", (uid, monto, yape, "pendiente"))
        elif "usuario_id" in cols:
            c.execute("INSERT INTO retiros (usuario_id, monto, banco_info, estado, fecha) VALUES (?,?,?,?, datetime('now','localtime'))", (uid, monto, yape, "pendiente"))
        else:
            c.execute("INSERT INTO retiros (monto, banco_info, estado, fecha, user_id) VALUES (?,?,?,?,?)", (monto, yape, "pendiente", "now", uid))

        con.commit(); con.close()
        return jsonify({"ok":True,"msg":"¡Solicitud enviada!"})

    except Exception as e:
        return jsonify({"ok":False,"msg":f"Error: {str(e)}"}),500
        
@app.route('/api/mis-retiros')
def mis_retiros():
    if 'user' not in session: return jsonify([])
    con=db(); c=con.cursor()
    c.execute("SELECT monto,banco_info,estado,fecha FROM retiros WHERE user_id=? ORDER BY id DESC LIMIT 20",(session['user'],))
    rows=c.fetchall(); con.close()
    return jsonify([{"monto":r[0],"banco":r[1],"estado":r[2],"fecha":r[3][11:16] if r[3] else ""} for r in rows])

@app.route("/api/retiros")
def api_retiros():
    try:
        con=db(); c=con.cursor()
        c.execute("SELECT * FROM retiros LIMIT 1")
        cols = [d[0] for d in c.description] if c.description else []
        c.execute("SELECT * FROM retiros ORDER BY id DESC")
        rows=c.fetchall()
        data=[]
        for r in rows:
            rd=dict(zip(cols, r))
            uid = rd.get('user_id') or rd.get('usuario_id') or rd.get('user_id')
            email = str(uid)
            try:
                if uid:
                    c2=con.cursor()
                    c2.execute("SELECT email FROM usuarios WHERE id=?", (uid,))
                    u=c2.fetchone()
                    if u: email=u[0]
            except: pass
            data.append({
                "id": rd.get('id'),
                "fecha": rd.get('fecha') or '',
                "usuario": email,
                "monto": rd.get('monto',0),
                "banco_info": rd.get('banco_info') or rd.get('yape_plin') or rd.get('banco') or '',
                "yape_plin": rd.get('banco_info') or rd.get('yape_plin') or '',
                "estado": rd.get('estado','pendiente')
            })
        con.close()
        return jsonify(data)
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),500

@app.route("/api/retiro/<int:rid>/<acc>", methods=["POST"])
def accion_retiro(rid, acc):
    try:
        con=db(); c=con.cursor()
        c.execute("SELECT * FROM retiros WHERE id=?", (rid,))
        if not c.description: return jsonify({"ok":False,"msg":"No existe"})
        cols=[d[0] for d in c.description]
        row=c.fetchone()
        if not row: return jsonify({"ok":False,"msg":"No existe"})
        rd=dict(zip(cols,row))
        uid = rd.get('user_id') or rd.get('usuario_id')
        monto = rd.get('monto',0)

        if acc=='aprobar':
            c.execute("UPDATE retiros SET estado='aprobado' WHERE id=?", (rid,))
            msg="Aprobado ✅ - Recuerda que ya yapeaste"
        else:
            # al rechazar devolvemos saldo
            if uid:
                c.execute("UPDATE usuarios SET saldo = saldo +? WHERE id=?", (monto, uid))
            c.execute("UPDATE retiros SET estado='rechazado' WHERE id=?", (rid,))
            msg="Rechazado y saldo devuelto"

        con.commit(); con.close()
        return jsonify({"ok":True,"msg":msg})
    except Exception as e:
        return jsonify({"ok":False,"msg":str(e)}),500
    
@app.route('/api/retiro/<int:rid>/<string:accion>', methods=['POST'])
def api_accion_retiro(rid,accion):
    if not session.get('admin'): return jsonify({"ok":False})
    con=db(); c=con.cursor()
    c.execute("SELECT user_id,monto,estado FROM retiros WHERE id=?",(rid,)); row=c.fetchone()
    if not row: con.close(); return jsonify({"ok":False,"msg":"No existe"})
    uid,monto,estado=row
    if estado!='pendiente': con.close(); return jsonify({"ok":False,"msg":"Ya procesado"})
    if accion=='aprobar':
        c.execute("SELECT saldo FROM usuarios WHERE id=?",(uid,)); s=c.fetchone()
        if not s or s[0]<monto: con.close(); return jsonify({"ok":False,"msg":f"Usuario solo tiene S/{s[0] if s else 0}"})
        c.execute("UPDATE usuarios SET saldo=saldo-? WHERE id=?",(monto, uid))
        c.execute("UPDATE retiros SET estado='aprobado' WHERE id=?",(rid,))
    else:
        c.execute("UPDATE retiros SET estado='rechazado' WHERE id=?",(rid,))
    con.commit(); con.close()
    return jsonify({"ok":True})

@app.route('/api/saldo')
def api_saldo():
    if 'user' not in session: return jsonify({"saldo":0})
    con=db(); c=con.cursor(); c.execute("SELECT saldo FROM usuarios WHERE id=?",(session['user'],)); row=c.fetchone(); con.close()
    return jsonify({"saldo":row[0] if row else 0})

ADMIN_USER="Globallotery"; ADMIN_PASS_HASH=hash_pass("Diosmeama.1")
@app.route('/admin/login')
def admin_login_page(): return render_template('admin_login.html')
@app.route('/api/admin/login', methods=['POST'])
def api_admin_login():
    d=request.json
    if d['user']==ADMIN_USER and hash_pass(d['pass'])==ADMIN_PASS_HASH:
        session['admin']=True; return jsonify({"ok":True})
    return jsonify({"ok":False})

@app.route('/admin')
def admin_panel():
    if not session.get('admin'): return redirect('/admin/login')
    pausado, tiempo_min = get_config()
    con=db(); c=con.cursor()
    c.execute("SELECT SUM(recaudacion), SUM(margen_plataforma), SUM(fondo_premios) FROM sorteos WHERE estado IN ('PAGADO','FINALIZADO')")
    tot_rec, tot_margen, tot_fondo=c.fetchone(); tot_rec=tot_rec or 0; tot_margen=tot_margen or 0; tot_fondo=tot_fondo or 0
    c.execute("SELECT * FROM sorteos WHERE estado='ABIERTO' ORDER BY id DESC LIMIT 1"); sorteo_actual=c.fetchone(); sid_actual=sorteo_actual[0] if sorteo_actual else 0
    try:
        c.execute("SELECT a.fecha, u.email, u.telefono, an.nombre, a.monto FROM apuestas a JOIN usuarios u ON a.usuario_id=u.id JOIN animales an ON a.animal_id=an.id WHERE a.sorteo_id=? ORDER BY a.fecha DESC LIMIT 100",(sid_actual,))
        apuestas_actuales=c.fetchall()
    except: apuestas_actuales=[]
    c.execute("SELECT id, fecha_hora_cierre, animal_ganador, estado, recaudacion, margen_plataforma, fondo_premios, hash_verificacion FROM sorteos ORDER BY id DESC LIMIT 50"); historial_sorteos=c.fetchall()
    c.execute("SELECT COUNT(*), SUM(saldo) FROM usuarios"); total_users, saldo_sistema=c.fetchone()
    try:
        c.execute("SELECT r.*, u.email FROM recargas_bcp r LEFT JOIN usuarios u ON u.id=r.user_id WHERE r.estado='pendiente' ORDER BY r.id DESC"); recargas_pendientes=c.fetchall()
        c.execute("SELECT COUNT(*), SUM(monto) FROM recargas_bcp WHERE estado='pendiente'"); count_pend, sum_pend=c.fetchone()
    except: recargas_pendientes=[]; count_pend=0; sum_pend=0
    con.close()
    return render_template('admin.html', total_recaudado=tot_rec, ganancia_25=tot_margen, total_pagado=tot_fondo, sorteo_actual=sorteo_actual, apuestas=apuestas_actuales, historial=historial_sorteos, total_users=total_users or 0, saldo_sistema=saldo_sistema or 0, recargas_pendientes=recargas_pendientes, count_pend=count_pend or 0, sum_pend=sum_pend or 0, bcp_cuenta=MI_CUENTA_BCP, pausado=pausado, tiempo_min=tiempo_min)

@app.route('/api/admin/aprobar-recarga/<int:rid>', methods=['POST'])
def api_aprobar_recarga(rid):
    if not session.get('admin'): return jsonify({"ok":False})
    con=db(); c=con.cursor(); c.execute("SELECT user_id, monto FROM recargas_bcp WHERE id=?",(rid,)); rec=c.fetchone()
    if rec:
        c.execute("UPDATE usuarios SET saldo=saldo+? WHERE id=?",(rec[1], rec[0])); c.execute("UPDATE recargas_bcp SET estado='aprobado' WHERE id=?",(rid,)); con.commit(); con.close()
        return jsonify({"ok":True,"msg":f"Aprobado S/{rec[1]}"})
    con.close(); return jsonify({"ok":False})

@app.route('/api/admin/rechazar-recarga/<int:rid>', methods=['POST'])
def api_rechazar_recarga(rid):
    if not session.get('admin'): return jsonify({"ok":False})
    con=db(); c=con.cursor(); c.execute("UPDATE recargas_bcp SET estado='rechazado' WHERE id=?",(rid,)); con.commit(); con.close()
    return jsonify({"ok":True})

@app.route('/api/admin/pausar-sala', methods=['POST'])
def api_pausar_sala():
    if not session.get('admin'): return jsonify({"ok":False})
    set_config('pausado','1'); return jsonify({"ok":True})

@app.route('/api/admin/activar-sala', methods=['POST'])
def api_activar_sala():
    if not session.get('admin'): return jsonify({"ok":False})
    set_config('pausado','0')
    con=db(); c=con.cursor()
    c.execute("SELECT id FROM sorteos WHERE estado='ABIERTO' ORDER BY id DESC LIMIT 1"); s=c.fetchone()
    nuevo=get_proximo_cierre_global()
    if s: c.execute("UPDATE sorteos SET fecha_hora_cierre=? WHERE id=?",(nuevo.isoformat(), s[0]))
    else: c.execute("INSERT INTO sorteos (fecha_hora_cierre, estado) VALUES (?, 'ABIERTO')",(nuevo.isoformat(),))
    con.commit(); con.close()
    return jsonify({"ok":True})

@app.route('/api/admin/set-tiempo', methods=['POST'])
def api_set_tiempo():
    if not session.get('admin'): return jsonify({"ok":False})
    m=int((request.json or {}).get('minutos',60))
    if m not in [15,30,60]: m=60
    set_config('tiempo_min', str(m))
    con=db(); c=con.cursor()
    nuevo=get_proximo_cierre_global()
    c.execute("DELETE FROM sorteos WHERE estado='ABIERTO'")
    c.execute("INSERT INTO sorteos (fecha_hora_cierre, estado) VALUES (?, 'ABIERTO')",(nuevo.isoformat(),))
    con.commit(); con.close()
    return jsonify({"ok":True})

@app.route('/api/admin/reiniciar-ganancias', methods=['POST'])
def api_reiniciar_ganancias():
    if not session.get('admin'): return jsonify({"ok":False})
    con=db(); c=con.cursor()
    c.execute("DELETE FROM apuestas"); c.execute("DELETE FROM sorteos")
    nuevo=get_proximo_cierre_global()
    c.execute("INSERT INTO sorteos (fecha_hora_cierre, estado) VALUES (?, 'ABIERTO')",(nuevo.isoformat(),))
    con.commit(); con.close()
    return jsonify({"ok":True})

@app.route('/api/admin/saldo-usuario', methods=['POST'])
def api_editar_saldo():
    if not session.get('admin'): return jsonify({"ok":False})
    d=request.json or {}; user_id=d.get('user_id') or d.get('id'); nuevo_saldo=d.get('saldo')
    con=db(); c=con.cursor(); c.execute("UPDATE usuarios SET saldo=? WHERE id=?",(int(nuevo_saldo), int(user_id))); con.commit(); con.close()
    return jsonify({"ok":True})

@app.route('/api/admin/forzar-sorteo', methods=['POST'])
def admin_forzar_sorteo():
    if not session.get('admin'): return jsonify({"ok":False})
    con=db(); c=con.cursor()
    c.execute("SELECT id FROM sorteos WHERE estado='ABIERTO' ORDER BY id DESC LIMIT 1"); s=c.fetchone()
    ganador = random.randint(1,25)
    if s:
        c.execute("SELECT COALESCE(SUM(monto),0) FROM apuestas WHERE sorteo_id=?", (s[0],))
        rec=c.fetchone()[0] or 0
        c.execute("UPDATE sorteos SET animal_ganador=?, recaudacion=?, fondo_premios=?, margen_plataforma=?, estado='PAGADO', fecha_hora_cierre=? WHERE id=?", (ganador, rec, int(rec*0.75), int(rec*0.25), datetime.now().isoformat(), s[0]))
        c.execute("SELECT usuario_id, SUM(monto) FROM apuestas WHERE sorteo_id=? AND animal_id=? GROUP BY usuario_id", (s[0], ganador))
        rows=c.fetchall()
        if rows:
            total=sum([x[1] for x in rows]) or 1
            for uid, apostado in rows:
                premio=int((rec*0.75)*(apostado/total)) if total>0 else 0
                if premio>0: c.execute("UPDATE usuarios SET saldo=saldo+? WHERE id=?", (premio, uid))
    proximo=get_proximo_cierre_global()
    c.execute("INSERT INTO sorteos (fecha_hora_cierre, estado) VALUES (?, 'ABIERTO')", (proximo.isoformat(),))
    con.commit(); con.close()
    return jsonify({"ok":True,"msg":f"Forzado ganador {ganador}"})

@app.route('/admin/usuarios')
def admin_usuarios_page():
    if not session.get('admin'): return redirect('/admin/login')
    con=db(); c=con.cursor()
    c.execute("SELECT id,email,telefono,saldo,fecha_registro FROM usuarios ORDER BY id DESC")
    usuarios=c.fetchall(); con.close()
    return render_template('admin_usuarios.html', usuarios=usuarios, bcp_cuenta=MI_CUENTA_BCP)

@app.route('/api/ultimo-resultado')
def ultimo_resultado():
    con=db(); c=con.cursor()
    c.execute("SELECT id, animal_ganador, fecha_hora_cierre FROM sorteos WHERE estado IN ('PAGADO','FINALIZADO') ORDER BY id DESC LIMIT 1")
    row=c.fetchone(); con.close()
    if row: return jsonify({"id":row[0],"ganador":row[1],"fecha":row[2]})
    return jsonify({"ganador":None})

@app.route('/api/mis-apuestas-actuales')
def mis_apuestas_actuales():
    if 'user' not in session: return jsonify({"apuestas":{}})
    con=db(); c=con.cursor()
    c.execute("SELECT id, fecha_hora_cierre FROM sorteos WHERE estado='ABIERTO' ORDER BY id DESC LIMIT 1"); s=c.fetchone()
    if not s: con.close(); return jsonify({"apuestas":{}})
    try:
        if datetime.fromisoformat(s[1]) <= datetime.now():
            con.close()
            return jsonify({"apuestas":{}})
    except:
        pass
    sid=s[0]
    c.execute("SELECT animal_id, monto FROM apuestas WHERE sorteo_id=? AND usuario_id=?", (sid, session['user']))
    rows=c.fetchall(); con.close()
    return jsonify({"apuestas":{r[0]:r[1] for r in rows}, "sorteo_id":sid})

@app.route('/api/historial-sorteos')
def historial_sorteos():
    con=db(); c=con.cursor()
    c.execute("SELECT animal_ganador, fecha_hora_cierre, id FROM sorteos WHERE animal_ganador IS NOT NULL ORDER BY id DESC LIMIT 20")
    rows=c.fetchall(); con.close()
    return jsonify([{"animal_ganador":r[0], "fecha":r[1], "id":r[2]} for r in rows])

@app.route('/api/admin/detalle-sorteo/<int:sid>')
def detalle_sorteo(sid):
    con=db(); c=con.cursor()
    c.execute("SELECT animal_ganador, recaudacion, fondo_premios, margen_plataforma FROM sorteos WHERE id=?",(sid,)); s=c.fetchone()
    if not s: con.close(); return jsonify({"ganador":None,"ganadores":[]})
    ganador, rec, fondo, margen=s
    c.execute("SELECT u.email, SUM(a.monto) FROM apuestas a JOIN usuarios u ON u.id=a.usuario_id WHERE a.sorteo_id=? AND a.animal_id=? GROUP BY u.email",(sid, ganador))
    rows=c.fetchall(); con.close()
    total=sum([r[1] for r in rows]) or 1
    ganadores=[{"email":r[0],"apostado":r[1],"porcentaje":round(r[1]/total*100,1),"premio":int(fondo*(r[1]/total))} for r in rows]
    return jsonify({"ganador":ganador,"fondo":fondo,"margen":margen,"ganadores":ganadores})

@app.route('/borrar-todo-usa')
def borrar_todo_usa():
    import os, sqlite3, glob
    for f in glob.glob("*.db") + ["init_db", "loteria.db", "users.db", "database.db"]:
        try:
            if os.path.exists(f):
                os.remove(f)
        except:
            pass
    # vuelve a crear las tablas vacías
    try:
        conn = sqlite3.connect('loteria.db')
        conn.execute('DROP TABLE IF EXISTS users')
        conn.execute('CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, email TEXT UNIQUE, password TEXT)')
        conn.commit()
        conn.close()
        return "BASE DE DATOS DE USA BORRADA Y LIMPIA. Ya puedes registrarte. Ahora borra este codigo."
    except Exception as e:
        return f"Intento de borrado: {e} - Archivos borrados, intenta registrarte ahora"

if __name__=='__main__':
    init_db()
    app.run(host='0.0.0.0', port=5000, debug=True)