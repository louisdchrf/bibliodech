"""Service d'envoi de mails via SMTP."""
import smtplib
import ssl
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import app.settings as cfg


# ── Config helpers ────────────────────────────────────────────────────────────

def get_smtp_config(db) -> dict:
    return cfg.get(db, "smtp") or {}


def is_scenario_enabled(db, key: str) -> bool:
    return bool(cfg.get(db, f"mail_{key}"))


# ── Send primitive ─────────────────────────────────────────────────────────────

def send_mail(db, to: str, subject: str, html: str) -> None:
    conf = get_smtp_config(db)
    host = conf.get("host", "").strip()
    if not host or not to:
        raise ValueError("Configuration SMTP incomplète ou destinataire manquant")

    port    = int(conf.get("port", 587))
    user    = conf.get("user", "").strip()
    pw      = conf.get("password", "")
    from_   = conf.get("from_", user) or user
    tls     = conf.get("tls", True)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = from_
    msg["To"]      = to
    msg.attach(MIMEText(html, "html", "utf-8"))

    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx) as s:
            if user:
                s.login(user, pw)
            s.sendmail(from_, [to], msg.as_string())
    else:
        with smtplib.SMTP(host, port) as s:
            if tls:
                s.starttls(context=ctx)
            if user:
                s.login(user, pw)
            s.sendmail(from_, [to], msg.as_string())


# ── Templates HTML ─────────────────────────────────────────────────────────────

def _wrap(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body{{margin:0;padding:0;background:#f7f8fc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#1a202c}}
  .wrap{{max-width:560px;margin:40px auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08)}}
  .head{{background:#1e3a5f;padding:28px 32px}}
  .head h1{{margin:0;font-size:1.2rem;color:#fff;font-weight:600}}
  .head p{{margin:4px 0 0;font-size:.8rem;color:rgba(255,255,255,.65)}}
  .body{{padding:32px}}
  .body p{{margin:0 0 1rem;line-height:1.6;font-size:.9rem}}
  .box{{background:#f0f4f8;border-radius:8px;padding:16px 20px;margin:1rem 0}}
  .box p{{margin:0;font-size:.85rem}}
  .box strong{{font-size:1.05rem;display:block;margin-top:4px;color:#1e3a5f}}
  .foot{{padding:16px 32px;font-size:.75rem;color:#94a3b8;border-top:1px solid #e2e8f0}}
</style>
</head>
<body>
<div class="wrap">
  <div class="head">
    <h1>📚 Bibliodech</h1>
    <p>{title}</p>
  </div>
  <div class="body">{body}</div>
  <div class="foot">Ce message a été envoyé automatiquement, merci de ne pas y répondre.</div>
</div>
</body>
</html>"""


def mail_overdue(borrower_name: str, book_title: str, due_date: date) -> tuple[str, str]:
    due_str = due_date.strftime("%-d %B %Y") if hasattr(due_date, "strftime") else str(due_date)
    subject = f"Rappel : « {book_title} » est en retard"
    body = f"""
<p>Bonjour <strong>{borrower_name}</strong>,</p>
<p>Nous vous contactons au sujet d'un livre que vous avez emprunté dans notre bibliothèque.</p>
<div class="box">
  <p>Livre emprunté</p>
  <strong>{book_title}</strong>
  <p style="margin-top:8px">Date de retour prévue : <strong>{due_str}</strong></p>
</div>
<p>Ce livre est désormais en retard. Merci de le retourner dès que possible.</p>
"""
    return subject, _wrap("Avis de retard", body)


def mail_new_account(username: str, password: str, site_url: str = "") -> tuple[str, str]:
    subject = "Votre compte Bibliodech a été créé"
    login_url = f"{site_url}/login" if site_url else "/login"
    body = f"""
<p>Bonjour,</p>
<p>Un compte a été créé pour vous sur <strong>Bibliodech</strong>. Voici vos identifiants :</p>
<div class="box">
  <p>Identifiant</p>
  <strong>{username}</strong>
  <p style="margin-top:12px">Mot de passe temporaire</p>
  <strong style="font-family:monospace;letter-spacing:.05em">{password}</strong>
</div>
<p>Connectez-vous à <a href="{login_url}" style="color:#1e3a5f">{login_url}</a> et changez votre mot de passe lors de votre première connexion.</p>
"""
    return subject, _wrap("Nouveau compte", body)


def mail_reset_password(username: str, temp_password: str, site_url: str = "") -> tuple[str, str]:
    subject = "Réinitialisation de votre mot de passe Bibliodech"
    login_url = f"{site_url}/login" if site_url else "/login"
    body = f"""
<p>Bonjour <strong>{username}</strong>,</p>
<p>Votre mot de passe a été réinitialisé par un administrateur. Voici votre mot de passe temporaire :</p>
<div class="box">
  <p>Mot de passe temporaire</p>
  <strong style="font-family:monospace;letter-spacing:.05em">{temp_password}</strong>
</div>
<p>Connectez-vous à <a href="{login_url}" style="color:#1e3a5f">{login_url}</a> et changez-le immédiatement.</p>
"""
    return subject, _wrap("Réinitialisation du mot de passe", body)
