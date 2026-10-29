import os
import re
from io import BytesIO
from datetime import datetime, timezone, date, timedelta
from calendar import monthrange
from urllib.parse import quote_plus, urlsplit

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

# ---------------------------------------------------------------------------
# App & DB
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY") or os.urandom(32)
app.config["MAX_CONTENT_LENGTH"] = 21 * 1024 * 1024

MAX_COMMENT_IMAGE_SIZE = 5 * 1024 * 1024
MAX_COMMENT_IMAGES = 4
INITIAL_TEAM_MEMBERS = (
    ("Brigitt Gamboa", "sig@abcgotuplas.com"),
    ("Cristian Martinez", "calidad@abcgotuplas.com"),
)

# Build DATABASE_URL from parts so the password '@' doesn't break URL parsing
_db_user     = os.environ.get("DB_USER",     "postgres")
_db_password = os.environ.get("DB_PASSWORD", "Got0p1@s")
_db_host     = os.environ.get("DB_HOST",     "localhost")
_db_port     = os.environ.get("DB_PORT",     "5432")
_db_name     = os.environ.get("DB_NAME",     "kanban_db")

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    f"postgresql+pg8000://{_db_user}:{quote_plus(_db_password)}@{_db_host}:{_db_port}/{_db_name}",
)

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


@login_manager.unauthorized_handler
def unauthorized():
    if request.path.startswith("/api/"):
        return jsonify({"error": "Autenticación requerida."}), 401
    return redirect(url_for("login", next=request.path))


@app.before_request
def require_authentication():
    if request.endpoint not in {"login", "static"} and not current_user.is_authenticated:
        return login_manager.unauthorized()
    return None


@app.errorhandler(413)
def request_too_large(_error):
    if request.path.startswith("/api/"):
        return jsonify({"error": "La solicitud supera el tamaño máximo permitido."}), 413
    return "La solicitud supera el tamaño máximo permitido.", 413

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_STATUSES   = ["todo", "in_progress", "review", "done"]
VALID_PRIORITIES = ["baja", "media", "alta"]

# ---------------------------------------------------------------------------
# Association tables
# ---------------------------------------------------------------------------

task_tags = db.Table(
    "task_tags",
    db.Column("task_id", db.Integer, db.ForeignKey("tasks.id",    ondelete="CASCADE"), primary_key=True),
    db.Column("tag_id",  db.Integer, db.ForeignKey("tags.id",     ondelete="CASCADE"), primary_key=True),
)

board_members = db.Table(
    "board_members",
    db.Column("board_id",  db.Integer, db.ForeignKey("boards.id",   ondelete="CASCADE"), primary_key=True),
    db.Column("member_id", db.Integer, db.ForeignKey("members.id",  ondelete="CASCADE"), primary_key=True),
)

meeting_members = db.Table(
    "meeting_members",
    db.Column("meeting_id", db.Integer, db.ForeignKey("meetings.id", ondelete="CASCADE"), primary_key=True),
    db.Column("member_id", db.Integer, db.ForeignKey("members.id", ondelete="CASCADE"), primary_key=True),
)

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class User(UserMixin, db.Model):
    __tablename__ = "users"

    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(80), nullable=False, unique=True)
    full_name     = db.Column(db.String(100), nullable=False, default="")
    email         = db.Column(db.String(254), nullable=True, unique=True)
    password_hash = db.Column(db.String(255), nullable=True)
    theme         = db.Column(db.String(10), nullable=False, default="dark")
    is_admin      = db.Column(db.Boolean, nullable=False, default=False)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return bool(self.password_hash) and check_password_hash(self.password_hash, password)


def available_username(email):
    base = email.split("@", 1)[0][:70] or "user"
    username = base
    suffix = 1
    while User.query.filter_by(username=username).first():
        suffix_text = f"-{suffix}"
        username = f"{base[:80 - len(suffix_text)]}{suffix_text}"
        suffix += 1
    return username


def normalized_identity(value):
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


class Board(db.Model):
    __tablename__ = "boards"

    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(100), nullable=False)
    description = db.Column(db.String(300), nullable=True)
    color       = db.Column(db.String(7), nullable=False, default="#1d4ed8")
    created_at  = db.Column(db.DateTime(timezone=True), nullable=False,
                            default=lambda: datetime.now(timezone.utc))

    tasks   = db.relationship("Task",   backref="board", lazy="dynamic", cascade="all, delete-orphan")
    members = db.relationship("Member", secondary=board_members, backref="boards", lazy="joined")

    def to_dict(self):
        return {
            "id":          self.id,
            "name":        self.name,
            "description": self.description or "",
            "color":       self.color,
            "created_at":  self.created_at.strftime("%d/%m/%Y"),
            "member_count": len(self.members),
            "task_count":   self.tasks.count(),
        }


class Member(db.Model):
    __tablename__ = "members"

    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(100), nullable=False)
    email      = db.Column(db.String(150), nullable=True, unique=True)
    avatar_url = db.Column(db.String(300), nullable=True)   # optional external URL
    color      = db.Column(db.String(7),   nullable=False, default="#1d4ed8")  # fallback avatar color
    created_at = db.Column(db.DateTime(timezone=True), nullable=False,
                           default=lambda: datetime.now(timezone.utc))

    def initials(self):
        parts = self.name.strip().split()
        if len(parts) >= 2:
            return (parts[0][0] + parts[-1][0]).upper()
        return self.name[:2].upper()

    def to_dict(self):
        return {
            "id":         self.id,
            "name":       self.name,
            "email":      self.email or "",
            "avatar_url": self.avatar_url or "",
            "color":      self.color,
            "initials":   self.initials(),
        }


class Meeting(db.Model):
    __tablename__ = "meetings"

    id          = db.Column(db.Integer, primary_key=True)
    title       = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=False, default="")
    start_at    = db.Column(db.DateTime, nullable=False)
    end_at      = db.Column(db.DateTime, nullable=False)
    location    = db.Column(db.String(300), nullable=False, default="")
    meeting_url = db.Column(db.String(1000), nullable=False, default="")
    recurrence  = db.Column(db.String(10), nullable=False, default="none")
    attendees   = db.relationship("Member", secondary=meeting_members, lazy="joined")


class Tag(db.Model):
    __tablename__ = "tags"

    id    = db.Column(db.Integer, primary_key=True)
    name  = db.Column(db.String(50), nullable=False, unique=True)
    color = db.Column(db.String(7),  nullable=False, default="#1d4ed8")

    def to_dict(self):
        return {"id": self.id, "name": self.name, "color": self.color}


class Task(db.Model):
    __tablename__ = "tasks"

    id          = db.Column(db.Integer, primary_key=True)
    board_id    = db.Column(db.Integer, db.ForeignKey("boards.id", ondelete="CASCADE"), nullable=False)
    title       = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    status      = db.Column(db.String(20),  nullable=False, default="todo")
    priority    = db.Column(db.String(10),  nullable=False, default="media")
    position    = db.Column(db.Integer,     nullable=False, default=0)
    deadline    = db.Column(db.Date,        nullable=True)
    fecha_creacion = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    fecha_inicio   = db.Column(db.Date,     nullable=True)
    assignee_id = db.Column(db.Integer, db.ForeignKey("members.id", ondelete="SET NULL"), nullable=True)
    created_at  = db.Column(db.DateTime(timezone=True), nullable=False,
                            default=lambda: datetime.now(timezone.utc))

    tags     = db.relationship("Tag",     secondary=task_tags, backref="tasks", lazy="joined")
    assignee = db.relationship("Member",  foreign_keys=[assignee_id], lazy="joined")
    comments = db.relationship("Comment", backref="task", lazy="dynamic",
                               cascade="all, delete-orphan", order_by="Comment.created_at")

    def to_dict(self, include_comments=False):
        today = date.today()
        dl    = self.deadline

        if dl:
            delta     = (dl - today).days
            dl_str    = dl.strftime("%d/%m/%Y")
            dl_status = ("overdue" if delta < 0 else
                         "today"   if delta == 0 else
                         "soon"    if delta <= 2 else "ok")
        else:
            dl_str = dl_status = None

        d = {
            "id":           self.id,
            "board_id":     self.board_id,
            "title":        self.title,
            "description":  self.description or "",
            "status":       self.status,
            "priority":     self.priority,
            "position":     self.position,
            "deadline":     dl_str,
            "deadline_iso": self.deadline.isoformat() if self.deadline else None,
            "fecha_inicio": self.fecha_inicio.isoformat() if self.fecha_inicio else None,
            "fecha_creacion": self.fecha_creacion.isoformat() if self.fecha_creacion else None,
            "dl_status":    dl_status,
            "created_at":   self.created_at.strftime("%d/%m/%Y %H:%M"),
            "tags":         [t.to_dict() for t in self.tags],
            "assignee":     self.assignee.to_dict() if self.assignee else None,
            "comment_count": self.comments.count(),
        }
        if include_comments:
            d["comments"] = [c.to_dict() for c in self.comments]
        return d


class Comment(db.Model):
    __tablename__ = "comments"

    id             = db.Column(db.Integer, primary_key=True)
    task_id        = db.Column(db.Integer, db.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    author_id      = db.Column(db.Integer, db.ForeignKey("members.id", ondelete="SET NULL"), nullable=True)
    author_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    body           = db.Column(db.Text, nullable=False)
    created_at     = db.Column(db.DateTime(timezone=True), nullable=False,
                               default=lambda: datetime.now(timezone.utc))

    author      = db.relationship("Member", foreign_keys=[author_id], lazy="joined")
    author_user = db.relationship("User", foreign_keys=[author_user_id], lazy="joined")
    attachments = db.relationship(
        "CommentAttachment",
        backref="comment",
        lazy="select",
        cascade="all, delete-orphan",
        order_by="CommentAttachment.id",
    )

    def to_dict(self):
        return {
            "id":         self.id,
            "task_id":    self.task_id,
            "body":       self.body,
            "created_at": self.created_at.strftime("%d/%m/%Y %H:%M"),
            "author": (
                {
                    "id": self.author_user.id,
                    "name": self.author_user.full_name,
                    "initials": self.author_user.full_name[:2].upper(),
                    "color": "#1d4ed8",
                    "avatar_url": "",
                    "email": "",
                }
                if self.author_user
                else self.author.to_dict() if self.author else None
            ),
            "attachments": [attachment.to_dict() for attachment in self.attachments],
        }


class CommentAttachment(db.Model):
    __tablename__ = "comment_attachments"

    id         = db.Column(db.Integer, primary_key=True)
    comment_id = db.Column(db.Integer, db.ForeignKey("comments.id", ondelete="CASCADE"), nullable=False)
    filename   = db.Column(db.String(255), nullable=False)
    mime_type  = db.Column(db.String(20), nullable=False)
    image_data = db.Column(db.LargeBinary, nullable=False)

    def to_dict(self):
        return {
            "id": self.id,
            "filename": self.filename,
            "url": url_for("get_comment_attachment", attachment_id=self.id),
        }


# ---------------------------------------------------------------------------
# Auto-create / migrate tables
# ---------------------------------------------------------------------------

with app.app_context():
    db.create_all()

    # Add board_id column to existing tasks table if it doesn't exist yet
    with db.engine.connect() as conn:
        from sqlalchemy import text

        cols = [r[0] for r in conn.execute(
            text("SELECT column_name FROM information_schema.columns WHERE table_name='tasks'")
        )]

        if "board_id" not in cols:
            # Create a default board for existing tasks
            result = conn.execute(
                text("INSERT INTO boards (name, description, color, created_at) "
                     "VALUES ('Mi Tablero', 'Tablero por defecto', '#1d4ed8', NOW()) RETURNING id")
            )
            default_board_id = result.fetchone()[0]
            conn.execute(text("ALTER TABLE tasks ADD COLUMN board_id INTEGER"))
            conn.execute(text(f"UPDATE tasks SET board_id = {default_board_id}"))
            conn.execute(text("ALTER TABLE tasks ALTER COLUMN board_id SET NOT NULL"))
            conn.execute(text(
                "ALTER TABLE tasks ADD CONSTRAINT tasks_board_id_fkey "
                "FOREIGN KEY (board_id) REFERENCES boards(id) ON DELETE CASCADE"
            ))
            conn.commit()

        if "assignee_id" not in cols:
            conn.execute(text("ALTER TABLE tasks ADD COLUMN assignee_id INTEGER REFERENCES members(id) ON DELETE SET NULL"))
            conn.commit()

        if "fecha_creacion" not in cols:
            conn.execute(text(
                "ALTER TABLE tasks ADD COLUMN fecha_creacion TIMESTAMP WITHOUT TIME ZONE "
                "DEFAULT timezone('utc', now())"
            ))
            conn.execute(text(
                "UPDATE tasks SET fecha_creacion = created_at AT TIME ZONE 'UTC' "
                "WHERE created_at IS NOT NULL"
            ))
            conn.execute(text("ALTER TABLE tasks ALTER COLUMN fecha_creacion SET NOT NULL"))
            conn.commit()

        if "fecha_inicio" not in cols:
            conn.execute(text("ALTER TABLE tasks ADD COLUMN fecha_inicio DATE"))
            conn.commit()

        user_cols = [r[0] for r in conn.execute(
            text("SELECT column_name FROM information_schema.columns WHERE table_name='users'")
        )]
        if "full_name" not in user_cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN full_name VARCHAR(100)"))
        if "email" not in user_cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN email VARCHAR(254)"))
        if "theme" not in user_cols:
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN theme VARCHAR(10) NOT NULL DEFAULT 'dark'"
            ))
        if "is_admin" not in user_cols:
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT FALSE"
            ))
        conn.execute(text("UPDATE users SET full_name = username WHERE full_name IS NULL OR full_name = ''"))
        conn.execute(text(
            "UPDATE users SET full_name = 'Administrador' "
            "WHERE username = 'admin' AND full_name = 'admin'"
        ))
        conn.execute(text("ALTER TABLE users ALTER COLUMN full_name SET NOT NULL"))
        conn.execute(text("ALTER TABLE users ALTER COLUMN password_hash DROP NOT NULL"))
        conn.execute(text("UPDATE users SET is_admin = TRUE WHERE username = 'admin'"))
        admin_email = (os.environ.get("DEFAULT_ADMIN_EMAIL") or "admin@local.test").strip().lower()
        conn.execute(
            text("UPDATE users SET email = :email WHERE username = 'admin' AND email IS NULL"),
            {"email": admin_email},
        )
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_email_lower "
            "ON users (LOWER(email)) WHERE email IS NOT NULL"
        ))
        comment_cols = [r[0] for r in conn.execute(
            text("SELECT column_name FROM information_schema.columns WHERE table_name='comments'")
        )]
        if "author_user_id" not in comment_cols:
            conn.execute(text(
                "ALTER TABLE comments ADD COLUMN author_user_id INTEGER "
                "REFERENCES users(id) ON DELETE SET NULL"
            ))
        conn.commit()

    if not User.query.filter_by(username="admin").first():
        admin_email = (os.environ.get("DEFAULT_ADMIN_EMAIL") or "admin@local.test").strip().lower()
        default_user = User(
            username="admin",
            full_name="Administrador",
            email=admin_email,
        )
        default_user.set_password(os.environ.get("DEFAULT_ADMIN_PASSWORD", "123"))
        default_user.is_admin = True
        db.session.add(default_user)
        db.session.commit()

    for full_name, email in INITIAL_TEAM_MEMBERS:
        normalized_email = email.lower()
        member = Member.query.filter(db.func.lower(Member.email) == normalized_email).first()
        if not member:
            db.session.add(Member(name=full_name, email=normalized_email))
    db.session.flush()

    for member in Member.query.filter(Member.email.isnot(None)).all():
        email = (member.email or "").strip().lower()
        if not email:
            continue
        account = User.query.filter(db.func.lower(User.email) == email).first()
        identity = normalized_identity(member.name)
        legacy_matches = [
            user for user in User.query.filter(User.email.is_(None)).all()
            if normalized_identity(user.username) == identity
        ]
        if account:
            if not account.password_hash and len(legacy_matches) == 1:
                legacy_account = legacy_matches[0]
                Comment.query.filter_by(author_user_id=account.id).update(
                    {"author_user_id": legacy_account.id}
                )
                db.session.delete(account)
                db.session.flush()
                legacy_account.email = email
                legacy_account.full_name = member.name
            elif not account.password_hash:
                account.full_name = member.name
            continue
        if len(legacy_matches) == 1:
            legacy_account = legacy_matches[0]
            legacy_account.email = email
            legacy_account.full_name = member.name
            continue
        db.session.add(User(
            username=available_username(email),
            full_name=member.name,
            email=email,
            password_hash=None,
        ))
    db.session.commit()

# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.route("/")
@login_required
def index():
    return redirect(url_for("dashboard"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        user = User.query.filter(db.func.lower(User.email) == email).first() if email else None
        if user and user.check_password(password):
            login_user(user)
            return redirect(url_for("dashboard"))
        flash("Correo electrónico o contraseña incorrectos.", "error")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/usuarios", methods=["GET", "POST"])
@login_required
def users():
    if not current_user.is_admin:
        abort(403)

    if request.method == "POST":
        full_name = (request.form.get("full_name") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""

        if not full_name or len(full_name) > 100:
            flash("El nombre es obligatorio y no puede superar 100 caracteres.", "error")
        elif len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            flash("Ingresa un correo electrónico válido.", "error")
        elif len(password) < 8:
            flash("La contraseña debe tener al menos 8 caracteres.", "error")
        else:
            user = User.query.filter(db.func.lower(User.email) == email).first()
            if user and user.password_hash:
                flash("Ya existe una cuenta activa con ese correo.", "error")
            else:
                member = Member.query.filter(db.func.lower(Member.email) == email).first()
                if user:
                    user.full_name = full_name
                    user.set_password(password)
                    if member:
                        member.name = full_name
                    else:
                        db.session.add(Member(name=full_name, email=email))
                else:
                    username = available_username(email)
                    user = User(username=username, full_name=full_name, email=email)
                    user.set_password(password)
                    db.session.add(user)
                    if member:
                        member.name = full_name
                    else:
                        db.session.add(Member(name=full_name, email=email))
                db.session.commit()
                flash(f"Cuenta para {full_name} creada correctamente.", "success")
                return redirect(url_for("users"))

    return render_template("users.html", users=User.query.order_by(User.full_name.asc()).all())


@app.route("/usuarios/<int:user_id>", methods=["POST"])
@login_required
def update_user(user_id):
    if not current_user.is_admin:
        abort(403)

    user = db.session.get(User, user_id)
    if not user:
        abort(404)

    full_name = (request.form.get("full_name") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    if not full_name or len(full_name) > 100:
        flash("El nombre es obligatorio y no puede superar 100 caracteres.", "error")
    elif len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        flash("Ingresa un correo electrónico válido.", "error")
    elif password and len(password) < 8:
        flash("La contraseña debe tener al menos 8 caracteres.", "error")
    elif User.query.filter(db.func.lower(User.email) == email, User.id != user.id).first():
        flash("Ya existe otra cuenta con ese correo.", "error")
    elif not user.password_hash and not password:
        flash("Define una contraseña de al menos 8 caracteres para activar esta cuenta.", "error")
    else:
        member = Member.query.filter(db.func.lower(Member.email) == (user.email or "").lower()).first()
        matching_member = Member.query.filter(db.func.lower(Member.email) == email).first()
        if member and matching_member and member.id != matching_member.id:
            flash("Ya existe una persona del equipo con ese correo.", "error")
        else:
            member = member or matching_member
            user.full_name = full_name
            user.email = email
            if password:
                user.set_password(password)
            if member:
                member.name = full_name
                member.email = email
            else:
                db.session.add(Member(name=full_name, email=email))
            db.session.commit()
            flash(f"Cuenta de {full_name} actualizada.", "success")
    return redirect(url_for("users"))


@app.route("/preferencias/tema", methods=["POST"])
@login_required
def set_theme():
    theme = request.form.get("theme")
    if theme not in {"dark", "light"}:
        abort(400)

    current_user.theme = theme
    db.session.commit()
    next_url = request.form.get("next", "")
    if next_url.startswith("/") and not next_url.startswith("//"):
        return redirect(next_url)
    return redirect(url_for("dashboard"))


@app.route("/dashboard")
@login_required
def dashboard():
    counts = {
        status: Task.query.filter_by(status=status).count()
        for status in VALID_STATUSES
    }
    tag_rows = (
        db.session.query(
            Tag.name,
            Tag.color,
            db.func.count(task_tags.c.task_id).label("task_count"),
        )
        .outerjoin(task_tags, Tag.id == task_tags.c.tag_id)
        .group_by(Tag.id, Tag.name, Tag.color)
        .order_by(Tag.name.asc())
        .all()
    )
    tag_data = [
        {"name": name, "color": color, "count": task_count}
        for name, color, task_count in tag_rows
    ]
    tagged_task_count = (
        db.session.query(db.func.count(db.func.distinct(task_tags.c.task_id))).scalar() or 0
    )
    upcoming_tasks = (
        Task.query
        .filter(Task.deadline.isnot(None), Task.status != "done")
        .order_by(Task.deadline.asc())
        .limit(5)
        .all()
    )
    return render_template(
        "dashboard.html",
        counts=counts,
        total=sum(counts.values()),
        tag_data=tag_data,
        tagged_task_count=tagged_task_count,
        upcoming_tasks=upcoming_tasks,
        today=date.today(),
    )


@app.route("/kanban")
@login_required
def kanban():
    return render_template("kanban.html", username=current_user.full_name)


@app.route("/calendario")
@login_required
def calendario():
    return render_template("calendar.html", username=current_user.full_name)


# ---------------------------------------------------------------------------
# Panel fragments (loaded via fetch into floating panels)
# ---------------------------------------------------------------------------

@app.route("/panel/kanban")
@login_required
def panel_kanban():
    return render_template("fragments/kanban.html", username=current_user.full_name)


@app.route("/panel/calendario")
@login_required
def panel_calendario():
    return render_template("fragments/calendario.html", username=current_user.full_name)


@app.route("/api/tareas")
@login_required
def calendar_tasks():
    tasks = Task.query.order_by(Task.fecha_inicio.asc(), Task.fecha_creacion.asc()).all()
    events = []
    for task in tasks:
        event_date = task.fecha_inicio or (
            task.fecha_creacion.date() if task.fecha_creacion else None
        )
        if not event_date:
            continue
        events.append({
            "id": task.id,
            "title": task.title,
            "start": event_date.isoformat(),
            "url": url_for("kanban"),
            "backgroundColor": {
                "todo": "#64748b",
                "in_progress": "#3b82f6",
                "review": "#f59e0b",
                "done": "#10b981",
            }.get(task.status, "#1d4ed8"),
            "extendedProps": {
                "status": task.status,
                "board": task.board.name,
                "description": task.description or "",
            },
        })

    range_start = _parse_calendar_datetime(request.args.get("start"))
    range_end = _parse_calendar_datetime(request.args.get("end"))
    if request.args.get("start") and range_start is None:
        return jsonify({"error": "Fecha de inicio del calendario inválida."}), 400
    if request.args.get("end") and range_end is None:
        return jsonify({"error": "Fecha de fin del calendario inválida."}), 400
    events.extend(_meeting_calendar_events(range_start, range_end))
    return jsonify(events)


def _parse_calendar_datetime(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=None)


def _meeting_calendar_events(range_start, range_end):
    meetings = Meeting.query.order_by(Meeting.start_at.asc()).all()
    events = []
    for meeting in meetings:
        duration = meeting.end_at - meeting.start_at
        if meeting.recurrence == "weekly":
            weeks = 0
            if range_start and meeting.start_at < range_start:
                weeks = max(0, (range_start.date() - meeting.start_at.date()).days // 7)
            occurrence = meeting.start_at + timedelta(weeks=weeks)
            if range_start and occurrence < range_start:
                occurrence += timedelta(weeks=1)
            while not range_end or occurrence < range_end:
                events.append(_meeting_event(meeting, occurrence, occurrence + duration))
                occurrence += timedelta(weeks=1)
                if not range_end:
                    break
        elif meeting.recurrence == "monthly":
            months = 0
            if range_start and meeting.start_at < range_start:
                months = max(
                    0,
                    (range_start.year - meeting.start_at.year) * 12
                    + range_start.month - meeting.start_at.month,
                )
            while True:
                month_index = meeting.start_at.month - 1 + months
                year = meeting.start_at.year + month_index // 12
                month = month_index % 12 + 1
                day = min(meeting.start_at.day, monthrange(year, month)[1])
                occurrence = meeting.start_at.replace(year=year, month=month, day=day)
                if range_start and occurrence < range_start:
                    months += 1
                    continue
                if range_end and occurrence >= range_end:
                    break
                events.append(_meeting_event(meeting, occurrence, occurrence + duration))
                months += 1
                if not range_end:
                    break
        elif (
            (range_start is None or meeting.end_at >= range_start)
            and (range_end is None or meeting.start_at < range_end)
        ):
            events.append(_meeting_event(meeting, meeting.start_at, meeting.end_at))
    return events


def _meeting_event(meeting, start_at, end_at):
    return {
        "id": f"meeting-{meeting.id}-{start_at.isoformat()}",
        "title": f"Reunión: {meeting.title}",
        "start": start_at.isoformat(),
        "end": end_at.isoformat(),
        "backgroundColor": "#1d4ed8",
        "borderColor": "#1d4ed8",
        "extendedProps": {
            "eventType": "meeting",
            "meetingId": meeting.id,
            "location": meeting.location,
            "meetingUrl": meeting.meeting_url,
            "recurrence": meeting.recurrence,
            "attendees": [member.name for member in meeting.attendees],
        },
    }


@app.route("/api/meetings", methods=["POST"])
@login_required
def create_meeting():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "El formato de la reunión no es válido."}), 400
    title = data.get("title", "")
    description = data.get("description", "")
    location = data.get("location", "")
    meeting_url = data.get("meeting_url", "")
    if not all(isinstance(value, str) for value in (title, description, location, meeting_url)):
        return jsonify({"error": "Los campos de texto de la reunión no son válidos."}), 400
    title = title.strip()
    start_at = _parse_calendar_datetime(data.get("start_at"))
    end_at = _parse_calendar_datetime(data.get("end_at"))
    recurrence = data.get("recurrence", "none")
    location = location.strip()
    meeting_url = meeting_url.strip()
    description = description.strip()
    attendee_ids = data.get("attendee_ids", [])

    if not title or len(title) > 200:
        return jsonify({"error": "El título es obligatorio y no debe superar 200 caracteres."}), 400
    if not start_at or not end_at or end_at <= start_at:
        return jsonify({"error": "La fecha y hora de fin debe ser posterior al inicio."}), 400
    if not isinstance(recurrence, str) or recurrence not in {"none", "weekly", "monthly"}:
        return jsonify({"error": "La repetición seleccionada no es válida."}), 400
    if len(location) > 300 or len(meeting_url) > 1000:
        return jsonify({"error": "El lugar o enlace supera la longitud permitida."}), 400
    if meeting_url:
        parsed_url = urlsplit(meeting_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            return jsonify({"error": "El enlace debe comenzar con http:// o https://."}), 400
    if not isinstance(attendee_ids, list) or any(
        not isinstance(member_id, int) or isinstance(member_id, bool)
        for member_id in attendee_ids
    ):
        return jsonify({"error": "La lista de asistentes no es válida."}), 400

    attendees = Member.query.filter(Member.id.in_(set(attendee_ids))).all() if attendee_ids else []
    if len(attendees) != len(set(attendee_ids)):
        return jsonify({"error": "Uno o más asistentes ya no están disponibles."}), 400

    meeting = Meeting(
        title=title,
        description=description,
        start_at=start_at,
        end_at=end_at,
        location=location,
        meeting_url=meeting_url,
        recurrence=recurrence,
        attendees=attendees,
    )
    db.session.add(meeting)
    db.session.commit()
    return jsonify({"id": meeting.id}), 201


@app.route("/api/meetings/<int:meeting_id>")
@login_required
def get_meeting(meeting_id):
    meeting = db.session.get(Meeting, meeting_id)
    if not meeting:
        return jsonify({"error": "Reunión no encontrada."}), 404
    return jsonify({
        "id": meeting.id,
        "title": meeting.title,
        "description": meeting.description,
        "start_at": meeting.start_at.isoformat(),
        "end_at": meeting.end_at.isoformat(),
        "location": meeting.location,
        "meeting_url": meeting.meeting_url,
        "recurrence": meeting.recurrence,
        "attendees": [member.to_dict() for member in meeting.attendees],
    })


@app.route("/api/meetings/<int:meeting_id>", methods=["DELETE"])
@login_required
def delete_meeting(meeting_id):
    meeting = db.session.get(Meeting, meeting_id)
    if not meeting:
        return jsonify({"error": "Reunión no encontrada."}), 404
    db.session.delete(meeting)
    db.session.commit()
    return jsonify({"message": "Reunión eliminada."})


# ---------------------------------------------------------------------------
# Boards API
# ---------------------------------------------------------------------------

@app.route("/api/boards", methods=["GET"])
def get_boards():
    boards = Board.query.order_by(Board.created_at.asc()).all()
    return jsonify([b.to_dict() for b in boards])


@app.route("/api/boards", methods=["POST"])
def create_board():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "El nombre es obligatorio."}), 400

    board = Board(
        name        = name,
        description = data.get("description", "").strip(),
        color       = data.get("color", "#1d4ed8"),
    )
    db.session.add(board)
    db.session.commit()
    return jsonify(board.to_dict()), 201


@app.route("/api/boards/<int:board_id>", methods=["PATCH"])
def update_board(board_id):
    board = db.session.get(Board, board_id)
    if not board:
        return jsonify({"error": "Tablero no encontrado."}), 404
    data = request.get_json(silent=True) or {}
    if "name"        in data: board.name        = data["name"].strip()
    if "description" in data: board.description = data["description"].strip()
    if "color"       in data: board.color       = data["color"]
    db.session.commit()
    return jsonify(board.to_dict())


@app.route("/api/boards/<int:board_id>", methods=["DELETE"])
def delete_board(board_id):
    board = db.session.get(Board, board_id)
    if not board:
        return jsonify({"error": "Tablero no encontrado."}), 404
    db.session.delete(board)
    db.session.commit()
    return jsonify({"message": "Tablero eliminado."})


@app.route("/api/boards/<int:board_id>/members", methods=["POST"])
def add_member_to_board(board_id):
    board = db.session.get(Board, board_id)
    if not board:
        return jsonify({"error": "Tablero no encontrado."}), 404
    data      = request.get_json(silent=True) or {}
    member_id = data.get("member_id")
    member    = db.session.get(Member, member_id)
    if not member:
        return jsonify({"error": "Persona no encontrada."}), 404
    if member not in board.members:
        board.members.append(member)
        db.session.commit()
    return jsonify(board.to_dict())


@app.route("/api/boards/<int:board_id>/members/<int:member_id>", methods=["DELETE"])
def remove_member_from_board(board_id, member_id):
    board  = db.session.get(Board, board_id)
    member = db.session.get(Member, member_id)
    if not board or not member:
        return jsonify({"error": "No encontrado."}), 404
    if member in board.members:
        board.members.remove(member)
        db.session.commit()
    return jsonify(board.to_dict())


# ---------------------------------------------------------------------------
# Members API
# ---------------------------------------------------------------------------

@app.route("/api/members", methods=["GET"])
def get_members():
    members = Member.query.order_by(Member.name).all()
    return jsonify([m.to_dict() for m in members])


@app.route("/api/members", methods=["POST"])
def create_member():
    data  = request.get_json(silent=True) or {}
    name  = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "El nombre es obligatorio."}), 400

    email = (data.get("email") or "").strip() or None
    if email:
        if not re.match(r"[^@]+@[^@]+\.[^@]+", email):
            return jsonify({"error": "Email inválido."}), 400
        if Member.query.filter_by(email=email).first():
            return jsonify({"error": "Ya existe una persona con ese email."}), 400

    member = Member(
        name       = name,
        email      = email,
        color      = data.get("color", "#1d4ed8"),
        avatar_url = data.get("avatar_url", "").strip() or None,
    )
    db.session.add(member)
    db.session.commit()
    return jsonify(member.to_dict()), 201


@app.route("/api/members/<int:member_id>", methods=["DELETE"])
def delete_member(member_id):
    member = db.session.get(Member, member_id)
    if not member:
        return jsonify({"error": "Persona no encontrada."}), 404
    db.session.delete(member)
    db.session.commit()
    return jsonify({"message": "Persona eliminada."})


# ---------------------------------------------------------------------------
# Tags API
# ---------------------------------------------------------------------------

@app.route("/api/tags", methods=["GET"])
def get_tags():
    return jsonify([t.to_dict() for t in Tag.query.order_by(Tag.name).all()])


@app.route("/api/tags", methods=["POST"])
def create_tag():
    data  = request.get_json(silent=True) or {}
    name  = (data.get("name") or "").strip()
    color = (data.get("color") or "#1d4ed8").strip()
    if not name:
        return jsonify({"error": "El nombre es obligatorio."}), 400
    existing = Tag.query.filter_by(name=name).first()
    if existing:
        return jsonify(existing.to_dict()), 200
    tag = Tag(name=name, color=color)
    db.session.add(tag)
    db.session.commit()
    return jsonify(tag.to_dict()), 201


@app.route("/api/tags/<int:tag_id>", methods=["DELETE"])
def delete_tag(tag_id):
    tag = db.session.get(Tag, tag_id)
    if not tag:
        return jsonify({"error": "Etiqueta no encontrada."}), 404
    db.session.delete(tag)
    db.session.commit()
    return jsonify({"message": "Etiqueta eliminada."})


# ---------------------------------------------------------------------------
# Tasks API
# ---------------------------------------------------------------------------

@app.route("/api/boards/<int:board_id>/tasks", methods=["GET"])
def get_tasks(board_id):
    board = db.session.get(Board, board_id)
    if not board:
        return jsonify({"error": "Tablero no encontrado."}), 404
    tasks   = Task.query.filter_by(board_id=board_id).order_by(Task.status, Task.position.asc()).all()
    grouped = {s: [] for s in VALID_STATUSES}
    for task in tasks:
        if task.status in grouped:
            grouped[task.status].append(task.to_dict())
    return jsonify(grouped)


@app.route("/api/boards/<int:board_id>/tasks", methods=["POST"])
def create_task(board_id):
    board = db.session.get(Board, board_id)
    if not board:
        return jsonify({"error": "Tablero no encontrado."}), 404

    data     = request.get_json(silent=True) or {}
    title    = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "El título es obligatorio."}), 400

    status   = data.get("status", "todo")
    priority = data.get("priority", "media")
    if status   not in VALID_STATUSES:   return jsonify({"error": f"Estado inválido: {status}"}),    400
    if priority not in VALID_PRIORITIES: return jsonify({"error": f"Prioridad inválida: {priority}"}), 400

    max_pos = db.session.query(db.func.max(Task.position)).filter_by(board_id=board_id, status=status).scalar() or 0

    deadline = None
    if data.get("deadline"):
        try:    deadline = date.fromisoformat(data["deadline"])
        except: return jsonify({"error": "Fecha inválida."}), 400

    fecha_inicio = None
    if data.get("fecha_inicio"):
        try:
            fecha_inicio = date.fromisoformat(data["fecha_inicio"])
        except (TypeError, ValueError):
            return jsonify({"error": "Fecha de inicio inválida."}), 400

    assignee_id = data.get("assignee_id") or None

    task = Task(
        board_id    = board_id,
        title       = title,
        description = data.get("description", "").strip(),
        status      = status,
        priority    = priority,
        position    = max_pos + 1,
        deadline    = deadline,
        fecha_inicio = fecha_inicio,
        assignee_id = assignee_id,
    )
    tag_ids = data.get("tag_ids", [])
    if tag_ids:
        task.tags = Tag.query.filter(Tag.id.in_(tag_ids)).all()

    db.session.add(task)
    db.session.commit()
    return jsonify(task.to_dict()), 201


@app.route("/api/tasks/<int:task_id>", methods=["GET"])
def get_task(task_id):
    task = db.session.get(Task, task_id)
    if not task:
        return jsonify({"error": "Tarea no encontrada."}), 404
    return jsonify(task.to_dict(include_comments=True))


@app.route("/api/tasks/<int:task_id>", methods=["PATCH"])
def update_task(task_id):
    task = db.session.get(Task, task_id)
    if not task:
        return jsonify({"error": "Tarea no encontrada."}), 404

    data = request.get_json(silent=True) or {}

    if "status" in data:
        if data["status"] not in VALID_STATUSES: return jsonify({"error": "Estado inválido."}), 400
        task.status = data["status"]
    if "title" in data:
        t = data["title"].strip()
        if not t: return jsonify({"error": "El título no puede estar vacío."}), 400
        task.title = t
    if "description" in data: task.description  = data["description"].strip()
    if "priority"    in data:
        if data["priority"] not in VALID_PRIORITIES: return jsonify({"error": "Prioridad inválida."}), 400
        task.priority = data["priority"]
    if "position"    in data: task.position     = int(data["position"])
    if "deadline"    in data:
        if data["deadline"]:
            try:    task.deadline = date.fromisoformat(data["deadline"])
            except: return jsonify({"error": "Fecha inválida."}), 400
        else: task.deadline = None
    if "fecha_inicio" in data:
        if data["fecha_inicio"]:
            try:
                task.fecha_inicio = date.fromisoformat(data["fecha_inicio"])
            except (TypeError, ValueError):
                return jsonify({"error": "Fecha de inicio inválida."}), 400
        else:
            task.fecha_inicio = None
    if "assignee_id" in data: task.assignee_id  = data["assignee_id"] or None
    if "tag_ids"     in data: task.tags         = Tag.query.filter(Tag.id.in_(data["tag_ids"])).all()

    db.session.commit()
    return jsonify(task.to_dict())


@app.route("/api/tasks/reorder", methods=["POST"])
def reorder_tasks():
    for item in (request.get_json(silent=True) or []):
        task = db.session.get(Task, item["id"])
        if task:
            task.status   = item.get("status",   task.status)
            task.position = item.get("position", task.position)
    db.session.commit()
    return jsonify({"message": "Orden actualizado."})


@app.route("/api/tasks/<int:task_id>", methods=["DELETE"])
def delete_task(task_id):
    task = db.session.get(Task, task_id)
    if not task:
        return jsonify({"error": "Tarea no encontrada."}), 404
    db.session.delete(task)
    db.session.commit()
    return jsonify({"message": "Tarea eliminada."})


# ---------------------------------------------------------------------------
# Comments API
# ---------------------------------------------------------------------------

@app.route("/api/tasks/<int:task_id>/comments", methods=["GET"])
def get_comments(task_id):
    task = db.session.get(Task, task_id)
    if not task:
        return jsonify({"error": "Tarea no encontrada."}), 404
    return jsonify([c.to_dict() for c in task.comments])


@app.route("/api/tasks/<int:task_id>/comments", methods=["POST"])
def create_comment(task_id):
    task = db.session.get(Task, task_id)
    if not task:
        return jsonify({"error": "Tarea no encontrada."}), 404

    body = ((request.form.get("body") if request.mimetype == "multipart/form-data"
             else (request.get_json(silent=True) or {}).get("body", "")) or "").strip()
    files = [file for file in request.files.getlist("images") if file.filename]
    if len(files) > MAX_COMMENT_IMAGES:
        return jsonify({"error": f"Puedes adjuntar hasta {MAX_COMMENT_IMAGES} imágenes por comentario."}), 400
    if len(body) > 1000:
        return jsonify({"error": "El comentario no puede superar los 1000 caracteres."}), 400
    if not body and not files:
        return jsonify({"error": "Escribe un comentario o adjunta una imagen."}), 400

    attachments = []
    for uploaded_file in files:
        image_data = uploaded_file.stream.read(MAX_COMMENT_IMAGE_SIZE + 1)
        if len(image_data) > MAX_COMMENT_IMAGE_SIZE:
            return jsonify({"error": f"Cada imagen debe pesar como máximo {MAX_COMMENT_IMAGE_SIZE // (1024 * 1024)} MB."}), 400

        mime_type = detect_comment_image_type(image_data)
        if not mime_type:
            return jsonify({"error": "Solo se permiten imágenes PNG, JPEG, GIF o WebP válidas."}), 400

        attachments.append((
            secure_filename(uploaded_file.filename)[:255] or "imagen",
            mime_type,
            image_data,
        ))

    comment = Comment(
        task_id        = task_id,
        author_user_id = current_user.id,
        body           = body,
    )
    comment.attachments = [
        CommentAttachment(filename=filename, mime_type=mime_type, image_data=image_data)
        for filename, mime_type, image_data in attachments
    ]
    db.session.add(comment)
    db.session.commit()
    return jsonify(comment.to_dict()), 201


def detect_comment_image_type(image_data):
    if image_data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(image_data) >= 12 and image_data[:4] == b"RIFF" and image_data[8:12] == b"WEBP":
        return "image/webp"
    return None


@app.route("/api/comment-attachments/<int:attachment_id>")
@login_required
def get_comment_attachment(attachment_id):
    attachment = db.session.get(CommentAttachment, attachment_id)
    if not attachment:
        return jsonify({"error": "Imagen no encontrada."}), 404

    response = send_file(
        BytesIO(attachment.image_data),
        mimetype=attachment.mime_type,
        download_name=attachment.filename,
        max_age=0,
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "private, no-store"
    return response


@app.route("/api/comments/<int:comment_id>", methods=["DELETE"])
def delete_comment(comment_id):
    return jsonify({"error": "Los comentarios no se pueden eliminar."}), 405


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(
        debug=os.environ.get("FLASK_DEBUG") == "1",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "5000")),
    )
