from datetime import datetime, date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, model_validator
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_contributor
from app.database import get_db
from app.models import Borrower, Loan, Book, User

router = APIRouter()


# ── Schemas ──────────────────────────────────────────────────────────────────

class BorrowerCreate(BaseModel):
    name: str
    contact: Optional[str] = None
    notes: Optional[str] = None


class LoanCreate(BaseModel):
    book_id: int
    borrower_id: Optional[int] = None   # emprunteur externe
    user_id: Optional[int] = None       # ou compte du site
    due_date: Optional[date] = None
    notes: Optional[str] = None

    @model_validator(mode="after")
    def check_borrower_or_user(self):
        if not self.borrower_id and not self.user_id:
            raise ValueError("borrower_id ou user_id est requis")
        return self


# ── Helpers ──────────────────────────────────────────────────────────────────

def _borrower_dict(b: Borrower) -> dict:
    return {"id": b.id, "name": b.name, "contact": b.contact, "notes": b.notes}


def _loan_borrower_name(loan: Loan) -> str | None:
    if loan.user:
        return loan.user.username
    if loan.borrower:
        return loan.borrower.name
    return None


def _loan_dict(loan: Loan) -> dict:
    book = loan.book
    return {
        "id": loan.id,
        "book_id": loan.book_id,
        "book_title": book.title if book else None,
        "book_cover": book.cover_url if book else None,
        "borrower_id": loan.borrower_id,
        "user_id": loan.user_id,
        "borrower_name": _loan_borrower_name(loan),
        "borrower_is_user": loan.user_id is not None,
        "loan_date": loan.loan_date.isoformat() if loan.loan_date else None,
        "due_date": loan.due_date.isoformat() if loan.due_date else None,
        "return_date": loan.return_date.isoformat() if loan.return_date else None,
        "overdue": (
            loan.due_date is not None
            and loan.return_date is None
            and loan.due_date.date() < date.today()
        ),
        "notes": loan.notes,
    }


# ── Borrowers ─────────────────────────────────────────────────────────────────

@router.get("/api/borrowers")
def list_borrowers(request: Request, db: Session = Depends(get_db)):
    get_current_user(request, db)
    borrowers = db.query(Borrower).order_by(Borrower.name).all()
    result = []
    for b in borrowers:
        d = _borrower_dict(b)
        d["active_loans"] = db.query(Loan).filter(
            Loan.borrower_id == b.id, Loan.return_date.is_(None)
        ).count()
        result.append(d)
    return result


@router.post("/api/borrowers", status_code=status.HTTP_201_CREATED)
def create_borrower(body: BorrowerCreate, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    require_contributor(user)
    b = Borrower(name=body.name, contact=body.contact, notes=body.notes)
    db.add(b)
    db.commit()
    db.refresh(b)
    return _borrower_dict(b)


@router.put("/api/borrowers/{borrower_id}")
def update_borrower(
    borrower_id: int,
    body: BorrowerCreate,
    request: Request,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    require_contributor(user)
    b = db.query(Borrower).filter(Borrower.id == borrower_id).first()
    if not b:
        raise HTTPException(status_code=404, detail="Emprunteur introuvable")
    b.name = body.name
    b.contact = body.contact
    b.notes = body.notes
    db.commit()
    db.refresh(b)
    return _borrower_dict(b)


@router.delete("/api/borrowers/{borrower_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_borrower(borrower_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    require_contributor(user)
    b = db.query(Borrower).filter(Borrower.id == borrower_id).first()
    if not b:
        raise HTTPException(status_code=404, detail="Emprunteur introuvable")
    if db.query(Loan).filter(Loan.borrower_id == borrower_id, Loan.return_date.is_(None)).count():
        raise HTTPException(status_code=400, detail="Cet emprunteur a des prêts en cours")
    db.delete(b)
    db.commit()


# ── Loans ─────────────────────────────────────────────────────────────────────

@router.get("/api/loans")
def list_loans(
    request: Request,
    active_only: bool = Query(False),
    book_id: Optional[int] = Query(None),
    borrower_id: Optional[int] = Query(None),
    user_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
):
    get_current_user(request, db)
    q = db.query(Loan)
    if active_only:
        q = q.filter(Loan.return_date.is_(None))
    if book_id is not None:
        q = q.filter(Loan.book_id == book_id)
    if borrower_id is not None:
        q = q.filter(Loan.borrower_id == borrower_id)
    if user_id is not None:
        q = q.filter(Loan.user_id == user_id)
    loans = q.order_by(Loan.loan_date.desc()).all()
    return [_loan_dict(l) for l in loans]


@router.post("/api/loans", status_code=status.HTTP_201_CREATED)
def create_loan(body: LoanCreate, request: Request, db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    require_contributor(current_user)

    book = db.query(Book).filter(Book.id == body.book_id).first()
    if not book:
        raise HTTPException(status_code=404, detail="Livre introuvable")

    active = db.query(Loan).filter(
        Loan.book_id == body.book_id, Loan.return_date.is_(None)
    ).first()
    if active:
        raise HTTPException(status_code=409, detail="Ce livre est déjà prêté")

    if body.borrower_id:
        if not db.query(Borrower).filter(Borrower.id == body.borrower_id).first():
            raise HTTPException(status_code=404, detail="Emprunteur introuvable")
    if body.user_id:
        if not db.query(User).filter(User.id == body.user_id).first():
            raise HTTPException(status_code=404, detail="Utilisateur introuvable")

    due_dt = datetime.combine(body.due_date, datetime.min.time()) if body.due_date else None
    loan = Loan(
        book_id=body.book_id,
        borrower_id=body.borrower_id,
        user_id=body.user_id,
        loan_date=datetime.utcnow(),
        due_date=due_dt,
        notes=body.notes,
    )
    db.add(loan)
    db.commit()
    db.refresh(loan)
    return _loan_dict(loan)


@router.post("/api/loans/{loan_id}/return")
def return_loan(loan_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    require_contributor(user)
    loan = db.query(Loan).filter(Loan.id == loan_id).first()
    if not loan:
        raise HTTPException(status_code=404, detail="Prêt introuvable")
    if loan.return_date:
        raise HTTPException(status_code=400, detail="Ce prêt est déjà clôturé")
    loan.return_date = datetime.utcnow()
    db.commit()
    db.refresh(loan)
    return _loan_dict(loan)


@router.get("/api/loans/overdue")
def overdue_loans(request: Request, db: Session = Depends(get_db)):
    get_current_user(request, db)
    loans = db.query(Loan).filter(Loan.return_date.is_(None)).all()
    today = date.today()
    return [_loan_dict(l) for l in loans if l.due_date and l.due_date.date() < today]


@router.post("/api/loans/send-overdue")
def send_overdue_mails(request: Request, db: Session = Depends(get_db)):
    """Envoie un mail de rappel pour chaque prêt en retard avec un destinataire connu."""
    from app.auth import require_admin
    user = get_current_user(request, db)
    require_admin(user)

    import app.settings as cfg
    from app.email import send_mail, mail_overdue, is_scenario_enabled
    from fastapi import HTTPException

    if not is_scenario_enabled(db, "overdue"):
        raise HTTPException(status_code=400, detail="Scénario avis de retard désactivé")

    loans = db.query(Loan).filter(Loan.return_date.is_(None)).all()
    today = date.today()
    overdue = [l for l in loans if l.due_date and l.due_date.date() < today]

    site_url = cfg.get(db, "site_url") or ""
    sent, skipped, errors = 0, 0, []

    for loan in overdue:
        # Déterminer l'email du destinataire
        if loan.borrower and loan.borrower.contact and "@" in loan.borrower.contact:
            to = loan.borrower.contact
            name = loan.borrower.name
        elif loan.user and loan.user.email:
            to = loan.user.email
            name = loan.user.username
        else:
            skipped += 1
            continue

        title = loan.book.title if loan.book else "Livre inconnu"
        subject, html = mail_overdue(name, title, loan.due_date.date())
        try:
            send_mail(db, to, subject, html)
            sent += 1
        except Exception as e:
            errors.append({"to": to, "error": str(e)})

    return {"sent": sent, "skipped": skipped, "errors": errors}
