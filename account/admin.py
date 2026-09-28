import functools

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from . import mail
from .extensions import db
from .models import AdminEvent, Client, CloudConnection, User
from .provisioning import ERRORS, configured, request_retry
from .security import fresh_required, login_required, revoke_sessions

bp = Blueprint("admin", __name__, url_prefix="/admin")


def admin_required(view):
    @login_required
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not g.user.is_admin:
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def audit(action, target_id=None):
    db.session.add(AdminEvent(actor_id=g.user.id, target_id=target_id, action=action))


@bp.get("/")
@admin_required
def index():
    search = request.args.get("q", "").strip()[:254]
    page = max(1, min(request.args.get("page", 1, type=int), 100000))
    query = db.select(User, CloudConnection).outerjoin(CloudConnection)
    if search:
        query = query.where(
            User.username.contains(search, autoescape=True)
            | User.email.contains(search, autoescape=True)
        )
    rows = db.session.execute(
        query.order_by(User.created_at.desc(), User.id).offset((page - 1) * 30).limit(31)
    ).all()
    events = db.session.execute(
        db.select(AdminEvent, User.username)
        .outerjoin(User, User.id == AdminEvent.actor_id)
        .order_by(AdminEvent.id.desc())
        .limit(30)
    ).all()
    return render_template(
        "admin.html",
        users=rows[:30],
        more=len(rows) > 30,
        page=page,
        search=search,
        errors=ERRORS,
        events=events,
        provision_configured=configured(),
        clients=db.session.scalars(db.select(Client).order_by(Client.slug)).all(),
    )


@bp.post("/users/<user_id>")
@admin_required
@fresh_required
def update_user(user_id):
    # Recheck permission while taking SQLite's write lock. A concurrent admin
    # demotion must not allow the former administrator to continue writing.
    permitted = db.session.execute(
        db.update(User)
        .where(
            User.id == g.user.id,
            User.is_admin.is_(True),
            User.enabled.is_(True),
        )
        .values(is_admin=True)
    )
    if permitted.rowcount != 1:
        db.session.rollback()
        abort(403)
    target = db.session.get(User, user_id, populate_existing=True)
    if not target:
        abort(404)
    action = request.form.get("action")
    connection = db.session.get(CloudConnection, target.id)
    if action in ("disable", "remove-admin") and target.id == g.user.id:
        abort(409, "Deine eigene Verwaltungssitzung kannst du hier nicht sperren oder herabstufen.")
    if action == "disable":
        target.enabled = False
        revoke_sessions(target.id)
    elif action == "enable":
        target.enabled = True
    elif action == "make-admin":
        if not target.enabled or not target.email_verified:
            abort(409, "Adminrechte benötigen ein aktives Konto mit bestätigter E-Mail-Adresse.")
        target.is_admin = True
    elif action == "remove-admin":
        target.is_admin = False
    elif action == "revoke-sessions":
        revoke_sessions(target.id)
    elif action == "retry-cloud":
        if not connection or not connection.managed:
            abort(409, "Für dieses Konto gibt es keine automatische Cloud-Einrichtung.")
    elif action == "reset-password":
        if not target.enabled or not target.email_verified:
            abort(409, "Das Konto muss aktiv und die E-Mail bestätigt sein.")
        # Release the write lock before SMTP; record only successful dispatches.
        db.session.commit()
        try:
            mail.send_action(target, "reset")
        except (OSError, RuntimeError):
            abort(503, "Die E-Mail konnte nicht versendet werden.")
    else:
        abort(400)
    if connection and connection.managed and action in ("disable", "enable", "retry-cloud"):
        request_retry(connection)
    audit(action, target.id)
    db.session.commit()
    flash(
        "Änderung gespeichert. Cloud-Änderungen werden im Hintergrund ausgeführt."
        if action in ("disable", "enable", "retry-cloud") and connection and connection.managed
        else "Änderung gespeichert.",
        "success",
    )
    return redirect(url_for("admin.index"))
