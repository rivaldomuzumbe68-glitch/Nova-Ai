import os, sqlite3, uuid, json
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, request, jsonify, send_from_directory, g
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
import jwt

SECRET_KEY = os.environ.get("SECRET_KEY", "nova-secret-2026")
JWT_SECRET = os.environ.get("JWT_SECRET", SECRET_KEY)
AI_API_KEY = os.environ.get("OPENAI_API_KEY") or os.environ.get("GROQ_API_KEY") or ""
AI_BASE = os.environ.get("AI_BASE_URL", "https://api.openai.com/v1")
AI_MODEL = os.environ.get("AI_MODEL", "gpt-4o-mini")
DB_PATH = "nova.db"

app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, email TEXT UNIQUE, password_hash TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, user_id TEXT, name TEXT, objective TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, user_id TEXT, project_id TEXT, role TEXT, content TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS memories (id TEXT PRIMARY KEY, user_id TEXT, content TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS connections (id TEXT PRIMARY KEY, user_id TEXT, provider TEXT, status TEXT, token TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS approvals (id TEXT PRIMARY KEY, user_id TEXT, action_type TEXT, payload TEXT, status TEXT DEFAULT 'pending', created_at TEXT, decided_at TEXT);
    CREATE TABLE IF NOT EXISTS audit (id TEXT PRIMARY KEY, user_id TEXT, action TEXT, detail TEXT, created_at TEXT);
    """)
    db.commit(); db.close()

init_db()

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db: db.close()

def create_token(uid):
    exp = datetime.utcnow() + timedelta(days=7)
    return jwt.encode({"uid": uid, "exp": exp}, JWT_SECRET, algorithm="HS256")

def auth_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get("Authorization","")
        if token.startswith("Bearer "): token = token[7:]
        if not token: token = request.headers.get("X-Token")
        if not token: return jsonify({"error":"Nao autenticado"}), 401
        try:
            data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
            user = get_db().execute("SELECT * FROM users WHERE id=?", (data['uid'],)).fetchone()
            if not user: return jsonify({"error":"Usuario nao encontrado"}), 401
            g.user = user
        except: return jsonify({"error":"Token invalido"}), 401
        return f(*args, **kwargs)
    return decorated

def call_ai(messages, web_ctx=""):
    sys_prompt = "Voce e NOVA AI, assistente pessoal. Fale portugues de Mocambique, direta e util."
    full = [{"role":"system","content": sys_prompt}]
    if web_ctx: full.append({"role":"system","content": f"Contexto web:\n{web_ctx}"})
    full.extend(messages)
    if AI_API_KEY:
        try:
            import requests
            r = requests.post(f"{AI_BASE}/chat/completions",
                headers={"Authorization": f"Bearer {AI_API_KEY}", "Content-Type":"application/json"},
                json={"model": AI_MODEL, "messages": full, "temperature":0.7}, timeout=30)
            r.raise_for_status()
            return r.json()['choices'][0]['message']['content']
        except Exception as e:
            print(e)
    last = messages[-1]['content'] if messages else "Ola"
    return f"[NOVA offline - coloque OPENAI_API_KEY para ativar cerebro] Voce disse: '{last}'."

def web_search(q):
    try:
        import requests
        r = requests.get("https://api.duckduckgo.com/", params={"q":q,"format":"json","no_html":1}, timeout=10)
        return r.json().get("AbstractText","")[:3000]
    except: return ""

@app.route("/")
def idx(): return send_from_directory(".", "index.html")
@app.route("/api/health")
def health(): return jsonify({"status":"ok","service":"NOVA AI","ai_configured":bool(AI_API_KEY)})

@app.route("/api/auth/register", methods=["POST"])
def register():
    d = request.get_json() or {}
    email = d.get("email","").lower().strip()
    pw = d.get("password","")
    if not email or len(pw)<8: return jsonify({"error":"Email e senha 8+ caracteres"}), 400
    db = get_db()
    if db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone():
        return jsonify({"error":"Email ja cadastrado"}), 400
    uid = str(uuid.uuid4())
    db.execute("INSERT INTO users VALUES (?,?,?,?)", (uid,email,generate_password_hash(pw),datetime.utcnow().isoformat()))
    db.commit()
    token = create_token(uid)
    return jsonify({"token":token,"user":{"id":uid,"email":email}})

@app.route("/api/auth/login", methods=["POST"])
def login():
    d = request.get_json() or {}
    email = d.get("email","").lower().strip()
    pw = d.get("password","")
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not u or not check_password_hash(u["password_hash"], pw):
        return jsonify({"error":"Email ou senha incorretos"}), 401
    token = create_token(u["id"])
    return jsonify({"token":token,"user":{"id":u["id"],"email":email}})

@app.route("/api/auth/logout", methods=["POST"])
@auth_required
def logout(): return jsonify({"ok":True})

@app.route("/api/me")
@auth_required
def me(): return jsonify({"id":g.user["id"],"email":g.user["email"]})

@app.route("/api/chat", methods=["POST"])
@auth_required
def chat():
    d = request.get_json() or {}
    msg = d.get("message","").strip()
    pid = d.get("projectId")
    use_web = d.get("web") is True
    if not msg: return jsonify({"error":"Mensagem vazia"}), 400
    db = get_db()
    db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?)", (str(uuid.uuid4()), g.user["id"], pid, "user", msg, datetime.utcnow().isoformat()))
    mems = db.execute("SELECT content FROM memories WHERE user_id=? ORDER BY created_at DESC LIMIT 10", (g.user["id"],)).fetchall()
    mem_ctx = "\n".join([f"- {m['content']}" for m in mems])
    hist_rows = db.execute("SELECT role,content FROM messages WHERE user_id=? ORDER BY created_at ASC LIMIT 20", (g.user["id"],)).fetchall()
    hist = [{"role":r["role"],"content":r["content"]} for r in hist_rows]
    if mem_ctx: hist.insert(0, {"role":"system","content":f"Memorias:\n{mem_ctx}"})
    web_ctx = web_search(msg) if use_web else ""
    answer = call_ai(hist, web_ctx)
    db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?)", (str(uuid.uuid4()), g.user["id"], pid, "assistant", answer, datetime.utcnow().isoformat()))
    db.commit()
    return jsonify({"answer":answer})

@app.route("/api/projects", methods=["GET","POST"])
@auth_required
def projects():
    db=get_db()
    if request.method=="GET":
        rows=db.execute("SELECT * FROM projects WHERE user_id=? ORDER BY created_at DESC",(g.user["id"],)).fetchall()
        return jsonify([dict(r) for r in rows])
    d=request.get_json() or {}
    pid=str(uuid.uuid4())
    db.execute("INSERT INTO projects VALUES (?,?,?,?,?)",(pid,g.user["id"],d.get("name","Novo Projeto"),d.get("objective",""),datetime.utcnow().isoformat()))
    db.commit()
    return jsonify({"id":pid,"name":d.get("name","Novo Projeto")})

@app.route("/api/memories", methods=["GET","POST","DELETE"])
@auth_required
def memories():
    db=get_db()
    if request.method=="GET":
        rows=db.execute("SELECT * FROM memories WHERE user_id=? ORDER BY created_at DESC",(g.user["id"],)).fetchall()
        return jsonify([dict(r) for r in rows])
    if request.method=="POST":
        c=(request.get_json() or {}).get("content","").strip()
        if not c: return jsonify({"error":"Vazia"}), 400
        mid=str(uuid.uuid4())
        db.execute("INSERT INTO memories VALUES (?,?,?,?)",(mid,g.user["id"],c,datetime.utcnow().isoformat())); db.commit()
        return jsonify({"id":mid,"content":c})
    mid=request.args.get("id")
    if mid: db.execute("DELETE FROM memories WHERE id=? AND user_id=?",(mid,g.user["id"])); db.commit(); return jsonify({"ok":True})
    return jsonify({"error":"ID necessario"}), 400

@app.route("/api/connectors", methods=["GET"])
@auth_required
def list_conn():
    rows=get_db().execute("SELECT id,provider,status,created_at FROM connections WHERE user_id=?",(g.user["id"],)).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/connect/<provider>", methods=["POST"])
@auth_required
def conn_prov(provider):
    db=get_db(); cid=str(uuid.uuid4())
    db.execute("INSERT INTO connections VALUES (?,?,?,?,?,?)",(cid,g.user["id"],provider,"connected","oauth",datetime.utcnow().isoformat())); db.commit()
    return jsonify({"ok":True,"provider":provider})

@app.route("/api/approvals", methods=["GET","POST"])
@auth_required
def approvals():
    db=get_db()
    if request.method=="GET":
        rows=db.execute("SELECT * FROM approvals WHERE user_id=? ORDER BY created_at DESC",(g.user["id"],)).fetchall()
        return jsonify([dict(r) for r in rows])
    d=request.get_json() or {}; aid=d.get("id"); act=d.get("action")
    ns="approved" if act=="approve" else "rejected"
    db.execute("UPDATE approvals SET status=?, decided_at=? WHERE id=? AND user_id=?",(ns,datetime.utcnow().isoformat(),aid,g.user["id"])); db.commit()
    return jsonify({"ok":True,"status":ns})

@app.route("/api/audit", methods=["GET"])
@auth_required
def audit():
    rows=get_db().execute("SELECT action,detail,created_at FROM audit WHERE user_id=? ORDER BY created_at DESC LIMIT 100",(g.user["id"],)).fetchall()
    return jsonify([dict(r) for r in rows])

if __name__=="__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",5000)))
