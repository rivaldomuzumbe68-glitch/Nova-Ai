import os
import uuid
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import wraps

import jwt
import requests
from flask import Flask, request, jsonify, send_from_directory, g
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash


# =========================================================
# NOVA AI — CONFIGURAÇÃO
# =========================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "nova.db")

SECRET_KEY = os.environ.get("SECRET_KEY", "change-this-secret")
JWT_SECRET = os.environ.get("JWT_SECRET", SECRET_KEY)

AI_API_KEY = (
    os.environ.get("OPENAI_API_KEY")
    or os.environ.get("GROQ_API_KEY")
    or ""
)

AI_BASE_URL = os.environ.get(
    "AI_BASE_URL",
    "https://api.openai.com/v1"
).rstrip("/")

AI_MODEL = os.environ.get(
    "AI_MODEL",
    "gpt-4o-mini"
)

PORT = int(os.environ.get("PORT", 5000))


# =========================================================
# APP
# =========================================================

app = Flask(
    __name__,
    static_folder=BASE_DIR,
    static_url_path=""
)

app.config["SECRET_KEY"] = SECRET_KEY

CORS(
    app,
    resources={r"/api/*": {"origins": "*"}},
    supports_credentials=True
)


# =========================================================
# DATABASE
# =========================================================

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def now():
    return datetime.now(timezone.utc).isoformat()


def init_db():
    db = sqlite3.connect(DB_PATH)

    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            objective TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            project_id TEXT,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS connections (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            status TEXT NOT NULL,
            token TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS approvals (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            action_type TEXT NOT NULL,
            payload TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT NOT NULL,
            decided_at TEXT
        );

        CREATE TABLE IF NOT EXISTS audit (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            action TEXT NOT NULL,
            detail TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_messages_user
        ON messages(user_id, created_at);

        CREATE INDEX IF NOT EXISTS idx_memories_user
        ON memories(user_id, created_at);

        CREATE INDEX IF NOT EXISTS idx_projects_user
        ON projects(user_id);

        CREATE INDEX IF NOT EXISTS idx_audit_user
        ON audit(user_id, created_at);
    """)

    db.commit()
    db.close()


init_db()


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)

    if db is not None:
        db.close()


# =========================================================
# UTILIDADES
# =========================================================

def error(message, status=400):
    return jsonify({
        "ok": False,
        "error": message
    }), status


def success(data=None, status=200):
    response = {
        "ok": True
    }

    if data:
        response.update(data)

    return jsonify(response), status


def audit_log(action, detail=""):
    try:
        db = get_db()

        db.execute(
            """
            INSERT INTO audit
            (id, user_id, action, detail, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                g.user["id"],
                action,
                detail,
                now()
            )
        )

        db.commit()

    except Exception as exc:
        print("Audit error:", exc)


# =========================================================
# JWT
# =========================================================

def create_token(user_id):
    payload = {
        "uid": user_id,
        "exp": datetime.now(timezone.utc) + timedelta(days=7)
    }

    return jwt.encode(
        payload,
        JWT_SECRET,
        algorithm="HS256"
    )


def get_token():
    auth = request.headers.get("Authorization", "")

    if auth.startswith("Bearer "):
        return auth[7:].strip()

    return request.headers.get("X-Token", "").strip()


def auth_required(function):
    @wraps(function)
    def wrapper(*args, **kwargs):

        token = get_token()

        if not token:
            return error("Não autenticado.", 401)

        try:
            payload = jwt.decode(
                token,
                JWT_SECRET,
                algorithms=["HS256"]
            )

            user_id = payload.get("uid")

            if not user_id:
                return error("Token inválido.", 401)

            user = get_db().execute(
                "SELECT * FROM users WHERE id = ?",
                (user_id,)
            ).fetchone()

            if not user:
                return error("Utilizador não encontrado.", 401)

            g.user = user

        except jwt.ExpiredSignatureError:
            return error("Sessão expirada.", 401)

        except jwt.InvalidTokenError:
            return error("Token inválido.", 401)

        return function(*args, **kwargs)

    return wrapper


# =========================================================
# IA — CÉREBRO DA NOVA
# =========================================================

SYSTEM_PROMPT = """
Você é NOVA AI, uma assistente pessoal inteligente.

Características:
- Responda em português.
- Seja clara, natural e útil.
- Explique assuntos difíceis de forma simples.
- Não invente informações.
- Quando não souber algo, diga claramente.
- Ajude o utilizador a estudar, programar, criar projetos,
  resolver problemas e aprender.
- Mantenha o contexto da conversa.
- Não diga que é o ChatGPT.
- Seu nome é NOVA AI.
"""


def call_ai(messages, web_context=""):
    if not AI_API_KEY:
        return (
            "A NOVA AI está funcionando, mas o cérebro de IA ainda "
            "não está conectado. Configure OPENAI_API_KEY ou "
            "GROQ_API_KEY nas variáveis de ambiente."
        )

    system = SYSTEM_PROMPT

    if web_context:
        system += (
            "\n\nInformações encontradas na pesquisa:\n"
            + web_context
        )

    payload_messages = [
        {
            "role": "system",
            "content": system
        }
    ]

    payload_messages.extend(messages)

    try:
        response = requests.post(
            f"{AI_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {AI_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": AI_MODEL,
                "messages": payload_messages,
                "temperature": 0.7,
                "max_tokens": 2000
            },
            timeout=60
        )

        response.raise_for_status()

        data = response.json()

        choices = data.get("choices", [])

        if not choices:
            raise RuntimeError("A IA não devolveu uma resposta.")

        message = choices[0].get("message", {})

        answer = message.get("content", "")

        if not answer:
            raise RuntimeError("Resposta vazia da IA.")

        return answer.strip()

    except requests.RequestException as exc:
        print("AI request error:", exc)

        return (
            "A NOVA AI teve um problema ao contactar o servidor "
            "de inteligência artificial. Verifique a API e tente "
            "novamente."
        )

    except Exception as exc:
        print("AI error:", exc)

        return (
            "A NOVA AI encontrou um erro ao processar a resposta."
        )


# =========================================================
# PESQUISA WEB
# =========================================================

def web_search(query):
    if not query:
        return ""

    try:
        response = requests.get(
            "https://api.duckduckgo.com/",
            params={
                "q": query,
                "format": "json",
                "no_html": 1,
                "skip_disambig": 1
            },
            timeout=10
        )

        response.raise_for_status()

        data = response.json()

        parts = []

        abstract = data.get("AbstractText")

        if abstract:
            parts.append(abstract)

        for item in data.get("RelatedTopics", [])[:5]:
            text = item.get("Text")

            if text:
                parts.append(text)

        return "\n".join(parts)[:5000]

    except Exception as exc:
        print("Web search error:", exc)
        return ""


# =========================================================
# FRONTEND
# =========================================================

@app.route("/")
def index():
    return send_from_directory(
        BASE_DIR,
        "index.html"
    )


# =========================================================
# HEALTH
# =========================================================

@app.route("/api/health")
def health():

    return success({
        "service": "NOVA AI",
        "status": "online",
        "ai_configured": bool(AI_API_KEY),
        "model": AI_MODEL
    })


# =========================================================
# AUTH — REGISTRO
# =========================================================

@app.route("/api/auth/register", methods=["POST"])
def register():

    data = request.get_json(silent=True) or {}

    email = data.get("email", "").strip().lower()
    password = data.get("password", "")

    if not email:
        return error("Email obrigatório.")

    if len(password) < 8:
        return error(
            "A senha precisa ter pelo menos 8 caracteres."
        )

    db = get_db()

    existing = db.execute(
        "SELECT id FROM users WHERE email = ?",
        (email,)
    ).fetchone()

    if existing:
        return error(
            "Este email já está cadastrado.",
            409
        )

    user_id = str(uuid.uuid4())

    password_hash = generate_password_hash(password)

    db.execute(
        """
        INSERT INTO users
        (id, email, password_hash, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            user_id,
            email,
            password_hash,
            now()
        )
    )

    db.commit()

    token = create_token(user_id)

    return success({
        "token": token,
        "user": {
            "id": user_id,
            "email": email
        }
    }, 201)


# =========================================================
# AUTH — LOGIN
# =========================================================

@app.route("/api/auth/login", methods=["POST"])
def login():

    data = request.get_json(silent=True) or {}

    email = data.get("email", "").strip().lower()
    password = data.get("password", "")

    db = get_db()

    user = db.execute(
        "SELECT * FROM users WHERE email = ?",
        (email,)
    ).fetchone()

    if not user:
        return error(
            "Email ou senha incorretos.",
            401
        )

    if not check_password_hash(
        user["password_hash"],
        password
    ):
        return error(
            "Email ou senha incorretos.",
            401
        )

    token = create_token(user["id"])

    return success({
        "token": token,
        "user": {
            "id": user["id"],
            "email": user["email"]
        }
    })


# =========================================================
# LOGOUT
# =========================================================

@app.route("/api/auth/logout", methods=["POST"])
@auth_required
def logout():

    audit_log("logout")

    return success()


# =========================================================
# PERFIL
# =========================================================

@app.route("/api/me")
@auth_required
def me():

    return success({
        "user": {
            "id": g.user["id"],
            "email": g.user["email"],
            "created_at": g.user["created_at"]
        }
    })


# =========================================================
# CHAT
# =========================================================

@app.route("/api/chat", methods=["POST"])
@auth_required
def chat():

    data = request.get_json(silent=True) or {}

    message = data.get("message", "").strip()
    project_id = data.get("projectId")
    use_web = data.get("web") is True

    if not message:
        return error("Mensagem vazia.")

    db = get_db()

    # Guardar mensagem do utilizador
    db.execute(
        """
        INSERT INTO messages
        (id, user_id, project_id, role, content, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            g.user["id"],
            project_id,
            "user",
            message,
            now()
        )
    )

    # Memórias
    memory_rows = db.execute(
        """
        SELECT content
        FROM memories
        WHERE user_id = ?
        ORDER BY created_at DESC
        LIMIT 10
        """,
        (g.user["id"],)
    ).fetchall()

    memory_text = "\n".join(
        f"- {row['content']}"
        for row in memory_rows
    )

    # Histórico
    rows = db.execute(
        """
        SELECT role, content
        FROM messages
        WHERE user_id = ?
        ORDER BY created_at DESC
        LIMIT 20
        """,
        (g.user["id"],)
    ).fetchall()

    rows = list(reversed(rows))

    messages = [
        {
            "role": row["role"],
            "content": row["content"]
        }
        for row in rows
    ]

    if memory_text:
        messages.insert(
            0,
            {
                "role": "system",
                "content": (
                    "Memórias do utilizador:\n"
                    + memory_text
                )
            }
        )

    web_context = ""

    if use_web:
        web_context = web_search(message)

    answer = call_ai(
        messages,
        web_context
    )

    # Guardar resposta
    db.execute(
        """
        INSERT INTO messages
        (id, user_id, project_id, role, content, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            g.user["id"],
            project_id,
            "assistant",
            answer,
            now()
        )
    )

    db.commit()

    audit_log(
        "chat",
        f"project={project_id or 'default'}"
    )

    return success({
        "answer": answer,
        "model": AI_MODEL,
        "web_used": bool(web_context)
    })


# =========================================================
# HISTÓRICO DO CHAT
# =========================================================

@app.route("/api/messages")
@auth_required
def messages():

    project_id = request.args.get("projectId")

    db = get_db()

    if project_id:

        rows = db.execute(
            """
            SELECT *
            FROM messages
            WHERE user_id = ?
            AND project_id = ?
            ORDER BY created_at ASC
            """,
            (
                g.user["id"],
                project_id
            )
        ).fetchall()

    else:

        rows = db.execute(
            """
            SELECT *
            FROM messages
            WHERE user_id = ?
            ORDER BY created_at ASC
            LIMIT 100
            """,
            (g.user["id"],)
        ).fetchall()

    return success({
        "messages": [dict(row) for row in rows]
    })


# =========================================================
# PROJETOS
# =========================================================

@app.route("/api/projects", methods=["GET", "POST"])
@auth_required
def projects():

    db = get_db()

    if request.method == "GET":

        rows = db.execute(
            """
            SELECT *
            FROM projects
            WHERE user_id = ?
            ORDER BY created_at DESC
            """,
            (g.user["id"],)
        ).fetchall()

        return success({
            "projects": [dict(row) for row in rows]
        })

    data = request.get_json(silent=True) or {}

    name = data.get(
        "name",
        "Novo Projeto"
    ).strip()

    objective = data.get(
        "objective",
        ""
    ).strip()

    project_id = str(uuid.uuid4())

    db.execute(
        """
        INSERT INTO projects
        (id, user_id, name, objective, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            project_id,
            g.user["id"],
            name,
            objective,
            now()
        )
    )

    db.commit()

    return success({
        "project": {
            "id": project_id,
            "name": name,
            "objective": objective
        }
    }, 201)


# =========================================================
# MEMÓRIAS
# =========================================================

@app.route("/api/memories", methods=["GET", "POST", "DELETE"])
@auth_required
def memories():

    db = get_db()

    if request.method == "GET":

        rows = db.execute(
            """
            SELECT *
            FROM memories
            WHERE user_id = ?
            ORDER BY created_at DESC
            """,
            (g.user["id"],)
        ).fetchall()

        return success({
            "memories": [dict(row) for row in rows]
        })

    if request.method == "POST":

        data = request.get_json(silent=True) or {}

        content = data.get(
            "content",
            ""
        ).strip()

        if not content:
            return error("Memória vazia.")

        memory_id = str(uuid.uuid4())

        db.execute(
            """
            INSERT INTO memories
            (id, user_id, content, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                memory_id,
                g.user["id"],
                content,
                now()
            )
        )

        db.commit()

        audit_log(
            "memory_created",
            memory_id
        )

        return success({
            "memory": {
                "id": memory_id,
                "content": content
            }
        }, 201)

    memory_id = request.args.get("id")

    if not memory_id:
        return error("ID da memória necessário.")

    db.execute(
        """
        DELETE FROM memories
        WHERE id = ?
        AND user_id = ?
        """,
        (
            memory_id,
            g.user["id"]
        )
    )

    db.commit()

    return success()


# =========================================================
# CONEXÕES
# =========================================================

@app.route("/api/connectors")
@auth_required
def connectors():

    rows = get_db().exe
