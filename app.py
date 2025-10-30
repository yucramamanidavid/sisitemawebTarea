# app.py
import os, io, csv, json, logging, threading, re, secrets
from datetime import datetime, date
from typing import Optional

from flask import (
    Flask, render_template, request, redirect, url_for, flash,
    jsonify, Response, send_from_directory, session, abort
)
import mysql.connector
from mysql.connector import pooling
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

try:
    import requests
except Exception:
    requests = None

# ==================== CONFIG ====================

SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_hex(16))
DEBUG = os.getenv("DEBUG", "false").lower() == "true"

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_NAME = os.getenv("DB_NAME", "gestion_tareas")
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")

API_KEY = os.getenv("API_KEY", "")

# n8n webhooks (opcionales)
N8N_WEBHOOK_TASK_MUTATION = os.getenv("N8N_WEBHOOK_TASK_MUTATION", "")
N8N_WEBHOOK_STATUS_CHANGE = os.getenv("N8N_WEBHOOK_STATUS_CHANGE", "")

# uploads
UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER", os.path.abspath("uploads"))
MAX_CONTENT_LENGTH = int(os.getenv("MAX_CONTENT_LENGTH", str(10 * 1024 * 1024)))  # 10MB
ALLOWED_EXT = set((os.getenv("ALLOWED_EXT", "png,jpg,jpeg,pdf,doc,docx,xls,xlsx,txt").split(",")))

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ==================== APP ====================

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["SECRET_KEY"] = SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

@app.context_processor
def inject_now():
    return {"now": datetime.now()}

# ==================== DB HELPER (crear BD si no existe) ====================

def ensure_database_exists():
    """Crea la base de datos si no existe (con charset/collation correctos)."""
    try:
        conn = mysql.connector.connect(
            host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD
        )
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            f"CREATE DATABASE IF NOT EXISTS `{DB_NAME}` "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
        cur.close()
        conn.close()
        app.logger.info(f"✅ Base verificada/creada: {DB_NAME}")
    except Exception as e:
        app.logger.error(f"❌ No se pudo crear/verificar la base {DB_NAME}: {e}")

# ==================== DB POOL ====================

def _build_pool():
    return pooling.MySQLConnectionPool(
        pool_name="tareas_pool",
        pool_size=10,
        pool_reset_session=True,
        host=DB_HOST, port=DB_PORT, database=DB_NAME,
        user=DB_USER, password=DB_PASSWORD,
        charset="utf8mb4", collation="utf8mb4_unicode_ci"
    )

# Asegura la BD antes de construir el pool
ensure_database_exists()

try:
    pool = _build_pool()
except Exception as e:
    app.logger.error(f"No se pudo crear el pool de conexiones: {e}")
    pool = None

def get_conn():
    """Obtiene una conexión del pool (reconstruye si es necesario)."""
    global pool
    if pool is None:
        try:
            pool = _build_pool()
        except Exception as e:
            app.logger.error(f"Pool no disponible: {e}")
            return None
    try:
        return pool.get_connection()
    except Exception as e:
        app.logger.error(f"No se pudo obtener conexión: {e}")
        return None

# ==================== ESQUEMA (creado al arranque) ====================

def crear_esquema_completo():
    """
    Crea todas las tablas necesarias. Nombres/columnas alineados con el código:
    - usuarios
    - tareas (incluye creada_por, asignada_a, prioridad, etc.)
    - tags y tarea_tags (N:N)
    - comentarios (con fecha)
    - adjuntos
    - auditoria
    """
    conn = get_conn()
    if not conn:
        app.logger.error("Sin conexión para crear tablas.")
        return
    try:
        cur = conn.cursor()

        # usuarios
        cur.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
          id INT AUTO_INCREMENT PRIMARY KEY,
          nombre VARCHAR(120) NOT NULL,
          email VARCHAR(190) NOT NULL UNIQUE,
          password_hash VARCHAR(255) NOT NULL,
          rol ENUM('admin','miembro') NOT NULL DEFAULT 'miembro',
          activo TINYINT(1) NOT NULL DEFAULT 1,
          creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          INDEX idx_usuarios_email (email)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;""")

        # tareas
        cur.execute("""
        CREATE TABLE IF NOT EXISTS tareas (
          id INT AUTO_INCREMENT PRIMARY KEY,
          titulo VARCHAR(255) NOT NULL,
          descripcion TEXT,
          estado ENUM('pendiente','en_progreso','completada') NOT NULL DEFAULT 'pendiente',
          prioridad ENUM('baja','media','alta','critica') NOT NULL DEFAULT 'media',
          fecha_creacion TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
          fecha_vencimiento DATE NULL,
          creada_por INT NULL,
          asignada_a INT NULL,
          FOREIGN KEY (creada_por) REFERENCES usuarios(id) ON DELETE SET NULL,
          FOREIGN KEY (asignada_a) REFERENCES usuarios(id) ON DELETE SET NULL,
          INDEX idx_tareas_estado (estado),
          INDEX idx_tareas_prioridad (prioridad),
          INDEX idx_tareas_venc (fecha_vencimiento)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;""")

        # tags y relación N:N
        cur.execute("""
        CREATE TABLE IF NOT EXISTS tags (
          id INT AUTO_INCREMENT PRIMARY KEY,
          nombre VARCHAR(64) NOT NULL UNIQUE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;""")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS tarea_tags (
          tarea_id INT NOT NULL,
          tag_id INT NOT NULL,
          PRIMARY KEY (tarea_id, tag_id),
          FOREIGN KEY (tarea_id) REFERENCES tareas(id) ON DELETE CASCADE,
          FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;""")

        # comentarios
        cur.execute("""
        CREATE TABLE IF NOT EXISTS comentarios (
          id INT AUTO_INCREMENT PRIMARY KEY,
          tarea_id INT NOT NULL,
          usuario_id INT NULL,
          contenido TEXT NOT NULL,
          fecha TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY (tarea_id) REFERENCES tareas(id) ON DELETE CASCADE,
          FOREIGN KEY (usuario_id) REFERENCES usuarios(id) ON DELETE SET NULL,
          INDEX idx_comentarios_fecha (fecha)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;""")

        # adjuntos
        cur.execute("""
        CREATE TABLE IF NOT EXISTS adjuntos (
          id INT AUTO_INCREMENT PRIMARY KEY,
          tarea_id INT NOT NULL,
          nombre_archivo VARCHAR(255) NOT NULL,
          ruta VARCHAR(1024) NOT NULL,
          tamano BIGINT NOT NULL,
          fecha TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY (tarea_id) REFERENCES tareas(id) ON DELETE CASCADE,
          INDEX idx_adjuntos_fecha (fecha)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;""")

        # auditoria
        cur.execute("""
        CREATE TABLE IF NOT EXISTS auditoria (
          id INT AUTO_INCREMENT PRIMARY KEY,
          usuario_id INT NULL,
          accion VARCHAR(50) NOT NULL,
          entidad VARCHAR(50) NOT NULL,
          entidad_id INT NULL,
          detalle JSON NULL,
          fecha TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY (usuario_id) REFERENCES usuarios(id) ON DELETE SET NULL,
          INDEX idx_auditoria_fecha (fecha),
          INDEX idx_auditoria_entidad (entidad, entidad_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;""")

        conn.commit()
        app.logger.info("✅ Tablas creadas/verificadas correctamente")

        # Seed admin si no hay usuarios
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT COUNT(*) AS c FROM usuarios")
        if (cur.fetchone() or {}).get("c", 0) == 0:
            pwd = generate_password_hash("admin123")
            cur2 = conn.cursor()
            cur2.execute(
                "INSERT INTO usuarios (nombre,email,password_hash,rol) VALUES (%s,%s,%s,%s)",
                ("Admin", "admin@example.com", pwd, "admin")
            )
            conn.commit()
            cur2.close()
            app.logger.info("👤 Usuario admin creado: admin@example.com / admin123 (cambia la clave)")

        cur.close()
    except Exception as e:
        app.logger.exception(f"❌ Error creando esquema: {e}")
    finally:
        try:
            conn.close()
        except:
            pass

# Crear/validar esquema inmediatamente
crear_esquema_completo()

# ==================== UTILS ====================

def _post_async(url: str, payload: dict):
    if not url or not requests:
        return
    def _run():
        try:
            requests.post(url, json=payload, timeout=5)
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()

def notify_task_mutation(action: str, data: dict):
    if N8N_WEBHOOK_TASK_MUTATION:
        _post_async(N8N_WEBHOOK_TASK_MUTATION, {"action": action, "task": data})

def notify_status_change(task_id: int, new_status: str):
    if N8N_WEBHOOK_STATUS_CHANGE:
        _post_async(N8N_WEBHOOK_STATUS_CHANGE, {"id": task_id, "estado": new_status})

def api_auth_ok():
    return (not API_KEY) or (request.headers.get("X-API-Key") == API_KEY)

def require_api_key():
    if not api_auth_ok():
        return jsonify({"error": "unauthorized"}), 401

def clamp(n, a, b): return max(a, min(b, n))

def paginate_params():
    page = clamp(int(request.args.get("page", 1)), 1, 10_000)
    per_page = clamp(int(request.args.get("per_page", 12)), 5, 100)
    return page, per_page, (page - 1) * per_page

def allowed_file(filename: str) -> bool:
    if "." not in filename: return False
    ext = filename.rsplit(".", 1)[1].lower()
    return ext in ALLOWED_EXT

def current_user_id() -> Optional[int]:
    return session.get("user_id")

def current_user_role() -> str:
    return session.get("user_role", "miembro")

def login_required(func):
    from functools import wraps
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not current_user_id():
            flash("Debe iniciar sesión.", "error")
            return redirect(url_for("login", next=request.path))
        return func(*args, **kwargs)
    return wrapper

def admin_required(func):
    from functools import wraps
    @wraps(func)
    def wrapper(*args, **kwargs):
        if current_user_role() != "admin":
            flash("Requiere rol admin.", "error")
            return redirect(url_for("index"))
        return func(*args, **kwargs)
    return wrapper

def audit(accion: str, entidad: str, entidad_id: Optional[int], detalle: dict):
    conn = get_conn()
    if not conn: return
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO auditoria (usuario_id, accion, entidad, entidad_id, detalle) VALUES (%s,%s,%s,%s,%s)",
            (current_user_id(), accion, entidad, entidad_id, json.dumps(detalle or {}))
        )
        conn.commit()
    except Exception as e:
        app.logger.warning(f"Auditoría fallo: {e}")
    finally:
        try: cur.close(); conn.close()
        except: pass

# ==================== AUTH ====================

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        conn = get_conn()
        if not conn:
            flash("Error de conexión", "error")
            return redirect(url_for("login"))
        try:
            cur = conn.cursor(dictionary=True)
            cur.execute("SELECT * FROM usuarios WHERE email=%s AND activo=1", (email,))
            u = cur.fetchone()
            if not u or not check_password_hash(u["password_hash"], password):
                flash("Credenciales inválidas", "error")
                return redirect(url_for("login"))
            session["user_id"] = u["id"]
            session["user_email"] = u["email"]
            session["user_name"] = u["nombre"]
            session["user_role"] = u["rol"]
            audit("login", "usuario", u["id"], {"email": email})
            nxt = request.args.get("next") or url_for("index")
            return redirect(nxt)
        finally:
            try: cur.close(); conn.close()
            except: pass
    return render_template("login.html")

@app.route("/logout")
def logout():
    uid = current_user_id()
    session.clear()
    if uid: audit("logout", "usuario", uid, {})
    flash("Sesión cerrada", "success")
    return redirect(url_for("login"))

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        nombre = request.form.get("nombre", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not (nombre and email and password):
            flash("Completa todos los campos", "error")
            return redirect(url_for("register"))
        conn = get_conn()
        if not conn:
            flash("Error de conexión", "error")
            return redirect(url_for("register"))
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM usuarios WHERE email=%s", (email,))
            if cur.fetchone():
                flash("Email ya registrado", "error")
                return redirect(url_for("register"))
            pwd = generate_password_hash(password)
            cur.execute("INSERT INTO usuarios (email, nombre, password_hash, rol) VALUES (%s,%s,%s,%s)",
                        (email, nombre, pwd, "miembro"))
            conn.commit()
            flash("Cuenta creada. Ahora inicia sesión.", "success")
            return redirect(url_for("login"))
        finally:
            try: cur.close(); conn.close()
            except: pass
    return render_template("register.html")

# ==================== DASHBOARD + LISTADO ====================

@app.route("/")
@login_required
def index():
    estado = request.args.get("estado", "").strip() or None
    q = request.args.get("q", "").strip()
    prioridad = request.args.get("prioridad", "").strip()
    asignada = request.args.get("asignada", "").strip()
    page, per_page, offset = paginate_params()

    conn = get_conn()
    if not conn:
        flash("Error de conexión", "error")
        return render_template("index.html", tareas=[], stats={}, filtros={})
    try:
        cur = conn.cursor(dictionary=True)

        # Stats (para dashboard)
        cur.execute("""
          SELECT 
            SUM(estado='pendiente') AS pend,
            SUM(estado='en_progreso') AS prog,
            SUM(estado='completada') AS comp,
            SUM(prioridad='critica') AS crit,
            SUM(prioridad='alta') AS alta
          FROM tareas
        """)
        stats = cur.fetchone() or {}

        # Filtros dinámicos
        where = ["1=1"]; params = []
        if estado: where.append("t.estado=%s"); params.append(estado)
        if prioridad: where.append("t.prioridad=%s"); params.append(prioridad)
        if asignada: where.append("t.asignada_a=%s"); params.append(asignada)
        if q:
            where.append("(t.titulo LIKE %s OR t.descripcion LIKE %s)")
            like = f"%{q}%"; params += [like, like]

        base = f" FROM tareas t LEFT JOIN usuarios u ON u.id=t.asignada_a WHERE {' AND '.join(where)}"
        cur.execute("SELECT COUNT(*) c " + base, params)
        total = cur.fetchone()["c"]
        cur.execute(f"""
          SELECT t.*, u.nombre AS asignada_a_nombre
          {base}
          ORDER BY t.fecha_creacion DESC
          LIMIT %s OFFSET %s
        """, params + [per_page, offset])
        tareas = cur.fetchall()

        # Para el selector de asignación
        cur.execute("SELECT id, nombre FROM usuarios WHERE activo=1 ORDER BY nombre")
        usuarios = cur.fetchall()

        # Formato de fechas
        for t in tareas:
            if t["fecha_creacion"] and not isinstance(t["fecha_creacion"], str):
                t["fecha_creacion"] = t["fecha_creacion"].strftime("%d/%m/%Y %H:%M")
            if t["fecha_vencimiento"] and not isinstance(t["fecha_vencimiento"], str):
                t["fecha_vencimiento"] = t["fecha_vencimiento"].strftime("%d/%m/%Y")

        filtros = {"estado": estado, "q": q, "prioridad": prioridad, "asignada": asignada}
        return render_template("index.html", tareas=tareas, stats=stats, usuarios=usuarios, filtros=filtros,
                               page=page, per_page=per_page, total=total)
    finally:
        try: cur.close(); conn.close()
        except: pass

# ==================== CRUD TAREAS ====================

@app.route("/tarea/nueva", methods=["POST"])
@login_required
def tarea_nueva():
    titulo = request.form.get("titulo", "").strip()
    descripcion = request.form.get("descripcion", "").strip() or None
    fecha_vencimiento = request.form.get("fecha_vencimiento") or None
    prioridad = request.form.get("prioridad", "media")
    asignada_a = request.form.get("asignada_a") or None

    if not titulo:
        flash("El título es obligatorio", "error")
        return redirect(url_for("index"))

    conn = get_conn()
    if not conn:
        flash("Error de conexión", "error")
        return redirect(url_for("index"))
    try:
        cur = conn.cursor()
        cur.execute("""
          INSERT INTO tareas (titulo, descripcion, fecha_vencimiento, prioridad, creada_por, asignada_a)
          VALUES (%s,%s,%s,%s,%s,%s)
        """, (titulo, descripcion, fecha_vencimiento, prioridad, current_user_id(), asignada_a))
        conn.commit()
        tarea_id = cur.lastrowid
        audit("create", "tarea", tarea_id, {"titulo": titulo})
        notify_task_mutation("create", {"id": tarea_id, "titulo": titulo})
        flash("✅ Tarea creada", "success")
    finally:
        try: cur.close(); conn.close()
        except: pass
    return redirect(url_for("index"))

@app.route("/tarea/<int:id>")
@login_required
def tarea_detalle(id: int):
    conn = get_conn()
    if not conn:
        flash("Error de conexión", "error"); return redirect(url_for("index"))
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("""
          SELECT t.*, cu.nombre AS creada_por_nombre, au.nombre AS asignada_a_nombre
          FROM tareas t
          LEFT JOIN usuarios cu ON cu.id=t.creada_por
          LEFT JOIN usuarios au ON au.id=t.asignada_a
          WHERE t.id=%s
        """, (id,))
        tarea = cur.fetchone()
        if not tarea:
            flash("Tarea no encontrada", "error"); return redirect(url_for("index"))

        # etiquetas
        cur.execute("""
          SELECT tg.id, tg.nombre FROM tarea_tags tt 
          JOIN tags tg ON tg.id=tt.tag_id WHERE tt.tarea_id=%s
        """, (id,))
        tags = cur.fetchall()

        # comentarios
        cur.execute("""
          SELECT c.*, u.nombre AS autor FROM comentarios c 
          LEFT JOIN usuarios u ON u.id=c.usuario_id 
          WHERE c.tarea_id=%s ORDER BY c.fecha DESC
        """, (id,))
        comentarios = cur.fetchall()

        # adjuntos
        cur.execute("SELECT * FROM adjuntos WHERE tarea_id=%s ORDER BY fecha DESC", (id,))
        adjuntos = cur.fetchall()

        # usuarios para re-asignar
        cur.execute("SELECT id, nombre FROM usuarios WHERE activo=1 ORDER BY nombre")
        usuarios = cur.fetchall()

        # todas las etiquetas disponibles
        cur.execute("SELECT id, nombre FROM tags ORDER BY nombre")
        all_tags = cur.fetchall()

        # formato fechas
        if tarea["fecha_creacion"] and not isinstance(tarea["fecha_creacion"], str):
            tarea["fecha_creacion"] = tarea["fecha_creacion"].strftime("%d/%m/%Y %H:%M")
        if tarea["fecha_vencimiento"] and not isinstance(tarea["fecha_vencimiento"], str):
            tarea["fecha_vencimiento"] = tarea["fecha_vencimiento"].strftime("%d/%m/%Y")

        return render_template("tarea_detalle.html", tarea=tarea, tags=tags,
                               comentarios=comentarios, adjuntos=adjuntos,
                               usuarios=usuarios, all_tags=all_tags)
    finally:
        try: cur.close(); conn.close()
        except: pass

@app.route("/tarea/<int:id>/editar", methods=["POST"])
@login_required
def tarea_editar(id: int):
    titulo = request.form.get("titulo", "").strip()
    descripcion = request.form.get("descripcion", "").strip() or None
    estado = request.form.get("estado", "pendiente")
    prioridad = request.form.get("prioridad", "media")
    fecha_vencimiento = request.form.get("fecha_vencimiento") or None
    asignada_a = request.form.get("asignada_a") or None

    if estado not in ("pendiente", "en_progreso", "completada"):
        flash("Estado inválido", "error"); return redirect(url_for("tarea_detalle", id=id))

    conn = get_conn()
    if not conn:
        flash("Error de conexión", "error"); return redirect(url_for("tarea_detalle", id=id))
    try:
        cur = conn.cursor()
        cur.execute("""
          UPDATE tareas SET titulo=%s, descripcion=%s, estado=%s, prioridad=%s, fecha_vencimiento=%s, asignada_a=%s
          WHERE id=%s
        """, (titulo, descripcion, estado, prioridad, fecha_vencimiento, asignada_a, id))
        conn.commit()
        audit("update", "tarea", id, {"estado": estado, "prioridad": prioridad})
        notify_task_mutation("update", {"id": id, "estado": estado, "prioridad": prioridad})
        flash("✅ Cambios guardados", "success")
    finally:
        try: cur.close(); conn.close()
        except: pass
    return redirect(url_for("tarea_detalle", id=id))

@app.route("/tarea/<int:id>/estado/<estado>")
@login_required
def tarea_cambiar_estado(id: int, estado: str):
    if estado not in ("pendiente", "en_progreso", "completada"):
        flash("Estado inválido", "error"); return redirect(url_for("tarea_detalle", id=id))
    conn = get_conn()
    if not conn:
        flash("Error de conexión", "error"); return redirect(url_for("tarea_detalle", id=id))
    try:
        cur = conn.cursor()
        cur.execute("UPDATE tareas SET estado=%s WHERE id=%s", (estado, id))
        conn.commit()
        audit("status", "tarea", id, {"estado": estado})
        notify_status_change(id, estado)
        flash("✅ Estado actualizado", "success")
    finally:
        try: cur.close(); conn.close()
        except: pass
    return redirect(url_for("tarea_detalle", id=id))

@app.route("/tarea/<int:id>/eliminar")
@login_required
def tarea_eliminar(id: int):
    conn = get_conn()
    if not conn:
        flash("Error de conexión", "error"); return redirect(url_for("index"))
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM tareas WHERE id=%s", (id,))
        conn.commit()
        audit("delete", "tarea", id, {})
        notify_task_mutation("delete", {"id": id})
        flash("✅ Tarea eliminada", "success")
    finally:
        try: cur.close(); conn.close()
        except: pass
    return redirect(url_for("index"))

# ==================== TAGS ====================

@app.route("/tarea/<int:id>/tag/agregar", methods=["POST"])
@login_required
def tarea_agregar_tag(id: int):
    nombre = request.form.get("tag", "").strip().lower()
    if not nombre: return redirect(url_for("tarea_detalle", id=id))
    conn = get_conn()
    if not conn: return redirect(url_for("tarea_detalle", id=id))
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM tags WHERE nombre=%s", (nombre,))
        r = cur.fetchone()
        if r:
            tag_id = r[0]
        else:
            cur.execute("INSERT INTO tags (nombre) VALUES (%s)", (nombre,))
            conn.commit()
            tag_id = cur.lastrowid
        cur.execute("INSERT IGNORE INTO tarea_tags (tarea_id, tag_id) VALUES (%s,%s)", (id, tag_id))
        conn.commit()
        audit("update", "tarea", id, {"tag_add": nombre})
        flash("✅ Tag agregado", "success")
    finally:
        try: cur.close(); conn.close()
        except: pass
    return redirect(url_for("tarea_detalle", id=id))

@app.route("/tarea/<int:id>/tag/<int:tag_id>/quitar")
@login_required
def tarea_quitar_tag(id: int, tag_id: int):
    conn = get_conn()
    if not conn: return redirect(url_for("tarea_detalle", id=id))
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM tarea_tags WHERE tarea_id=%s AND tag_id=%s", (id, tag_id))
        conn.commit()
        audit("update", "tarea", id, {"tag_remove": tag_id})
        flash("✅ Tag quitado", "success")
    finally:
        try: cur.close(); conn.close()
        except: pass
    return redirect(url_for("tarea_detalle", id=id))

# ==================== COMENTARIOS ====================

@app.route("/tarea/<int:id>/comentario", methods=["POST"])
@login_required
def tarea_comentar(id: int):
    contenido = request.form.get("contenido", "").strip()
    if not contenido:
        flash("Escribe un comentario", "error"); return redirect(url_for("tarea_detalle", id=id))
    conn = get_conn()
    if not conn:
        flash("Error de conexión", "error"); return redirect(url_for("tarea_detalle", id=id))
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO comentarios (tarea_id, usuario_id, contenido) VALUES (%s,%s,%s)",
                    (id, current_user_id(), contenido))
        conn.commit()
        audit("comment", "tarea", id, {"contenido": contenido[:120]})
        flash("💬 Comentario agregado", "success")
    finally:
        try: cur.close(); conn.close()
        except: pass
    return redirect(url_for("tarea_detalle", id=id))

# ==================== ADJUNTOS ====================

@app.route("/tarea/<int:id>/adjuntar", methods=["POST"])
@login_required
def tarea_adjuntar(id: int):
    file = request.files.get("archivo")
    if not file or not file.filename:
        flash("Selecciona un archivo", "error"); return redirect(url_for("tarea_detalle", id=id))
    filename = secure_filename(file.filename)
    if not allowed_file(filename):
        flash("Tipo de archivo no permitido", "error"); return redirect(url_for("tarea_detalle", id=id))
    ruta = os.path.join(UPLOAD_FOLDER, f"{id}_{filename}")
    file.save(ruta)
    tam = os.path.getsize(ruta)
    conn = get_conn()
    if not conn:
        flash("Error de conexión", "error"); return redirect(url_for("tarea_detalle", id=id))
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO adjuntos (tarea_id, nombre_archivo, ruta, tamano) VALUES (%s,%s,%s,%s)",
                    (id, filename, ruta, tam))
        conn.commit()
        audit("attach", "tarea", id, {"archivo": filename, "tamano": tam})
        flash("📎 Archivo adjuntado", "success")
    finally:
        try: cur.close(); conn.close()
        except: pass
    return redirect(url_for("tarea_detalle", id=id))

@app.route("/adjunto/<int:aid>/descargar")
@login_required
def adjunto_descargar(aid: int):
    conn = get_conn()
    if not conn: abort(404)
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM adjuntos WHERE id=%s", (aid,))
        a = cur.fetchone()
        if not a or not os.path.exists(a["ruta"]): abort(404)
        directory, fname = os.path.dirname(a["ruta"]), os.path.basename(a["ruta"])
        return send_from_directory(directory, fname, as_attachment=True, download_name=a["nombre_archivo"])
    finally:
        try: cur.close(); conn.close()
        except: pass

# ==================== API REST ====================

@app.route("/api/tareas", methods=["GET"])
def api_tareas():
    unauth = require_api_key()
    if unauth: return unauth
    q = request.args.get("q", "").strip()
    estado = request.args.get("estado", "").strip()
    prioridad = request.args.get("prioridad", "").strip()
    page, per_page, offset = paginate_params()
    conn = get_conn()
    if not conn: return jsonify([])
    try:
        cur = conn.cursor(dictionary=True)
        where, params = ["1=1"], []
        if q:
            like = f"%{q}%"
            where.append("(titulo LIKE %s OR descripcion LIKE %s)")
            params += [like, like]
        if estado: where.append("estado=%s"); params.append(estado)
        if prioridad: where.append("prioridad=%s"); params.append(prioridad)
        cur.execute(
            f"SELECT * FROM tareas WHERE {' AND '.join(where)} ORDER BY fecha_creacion DESC LIMIT %s OFFSET %s",
            params + [per_page, offset]
        )
        return jsonify(cur.fetchall())
    finally:
        try: cur.close(); conn.close()
        except: pass

@app.route("/api/tareas/<int:id>")
def api_tarea(id: int):
    unauth = require_api_key()
    if unauth: return unauth
    conn = get_conn()
    if not conn: return jsonify({"error": "db"}), 500
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM tareas WHERE id=%s", (id,))
        r = cur.fetchone()
        if not r: return jsonify({"error": "not_found"}), 404
        return jsonify(r)
    finally:
        try: cur.close(); conn.close()
        except: pass

# ==================== EXPORTS ====================

@app.route("/export/tareas.csv")
@login_required
def export_tareas():
    conn = get_conn()
    if not conn: return Response("", mimetype="text/csv")
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM tareas ORDER BY fecha_creacion DESC")
        rows = cur.fetchall()
        out = io.StringIO()
        if rows:
            writer = csv.DictWriter(out, fieldnames=rows[0].keys()); writer.writeheader(); writer.writerows(rows)
        return Response(out.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=tareas.csv"})
    finally:
        try: cur.close(); conn.close()
        except: pass

# ==================== HEALTH ====================

@app.route("/healthz")
def healthz(): return jsonify({"ok": True})

@app.route("/readinessz")
def readinessz():
    conn = get_conn()
    if not conn: return jsonify({"db": "fail", "error": "no connection"}), 503
    try:
        cur = conn.cursor(); cur.execute("SELECT 1"); cur.fetchone()
        return jsonify({"db": "ok"})
    except Exception as e:
        return jsonify({"db": "fail", "error": str(e)}), 503
    finally:
        try: cur.close(); conn.close()
        except: pass

# ==================== BOOT ====================

if __name__ == "__main__":
    app.logger.info("🚀 Tareas PRO iniciado en http://0.0.0.0:5000")
    app.run(debug=DEBUG, host="0.0.0.0", port=5000)
