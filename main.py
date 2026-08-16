import os
from fastapi import FastAPI, Depends, HTTPException, status, Request, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session, joinedload, selectinload
from sqlalchemy import and_, or_, inspect, text
from datetime import date, datetime, timedelta
import re, secrets, asyncio, json, base64, binascii, hashlib
from collections import defaultdict
from typing import Optional
from jose import JWTError, jwt
import bcrypt
from pydantic import BaseModel
import models
from database import engine, get_db, Base

# ─────────────────────────────────────────────
# App setup
# ─────────────────────────────────────────────
Base.metadata.create_all(bind=engine)

def ensure_subscription_schema():
    """Small, idempotent migration for deployments without a migration runner."""
    additions = {
        "parishes": {
            "subscription_plan": "VARCHAR(20)",
        },
        "payment_submissions": {
            "payment_date": "TIMESTAMP",
            "coverage_start": "TIMESTAMP",
            "coverage_end": "TIMESTAMP",
            "payment_method": "VARCHAR(50)",
            "notes": "TEXT",
        },
    }
    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            for table_name, columns in additions.items():
                for column_name, column_type in columns.items():
                    connection.execute(text(
                        f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS "
                        f"{column_name} {column_type}"
                    ))
        else:
            schema = inspect(connection)
            for table_name, columns in additions.items():
                if table_name not in schema.get_table_names():
                    continue
                existing = {column["name"] for column in schema.get_columns(table_name)}
                for column_name, column_type in columns.items():
                    if column_name not in existing:
                        connection.execute(text(
                            f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"
                        ))
        connection.execute(text("""
            UPDATE parishes
            SET subscription_plan = (
                SELECT payment_submissions.plan
                FROM payment_submissions
                WHERE payment_submissions.parish_id = parishes.id
                  AND payment_submissions.status = 'confirmed'
                ORDER BY payment_submissions.confirmed_at DESC,
                         payment_submissions.id DESC
                LIMIT 1
            )
            WHERE subscription_plan IS NULL AND plan = 'active'
        """))

ensure_subscription_schema()

app = FastAPI(title="Panalangin", description="Mass Intentions Display for Filipino Catholic Parishes")


from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8000",
        "https://panalangin.markmuya.com",
        "https://markmuya.com",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")), name="static")

# ─────────────────────────────────────────────
# Auth config
# ─────────────────────────────────────────────
SECRET_KEY = os.environ.get("SECRET_KEY", "change-this-to-a-long-random-string-before-deploying")
SEED_DEMO = os.environ.get("SEED_DEMO", "").lower() in ("1", "true", "yes")
ALGORITHM  = "HS256"
TOKEN_EXPIRE_HOURS = 12


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))

def create_token(data: dict) -> str:
    payload = data.copy()
    payload["exp"] = datetime.utcnow() + timedelta(hours=TOKEN_EXPIRE_HOURS)
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

def get_current_user(request: Request, db: Session = Depends(get_db)):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = auth.split(" ", 1)[1]
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid token")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")
    user = db.query(models.User).filter(
        models.User.id == int(user_id),
        models.User.is_active == True
    ).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user

DEFAULT_CATEGORIES = [
    "Thanksgiving", "Wedding Anniversary", "Gift of Life",
    "Special Intentions", "Healing", "Fast Recovery",
    "Safe Travel", "Souls", "9th Day",
    "40th Day", "Death Anniversary", "Mass Card"
]

# ─────────────────────────────────────────────
# Seed helper — creates a parish + admin user
# on first run so you can log in immediately
# ─────────────────────────────────────────────
def seed_demo(db: Session):
    if db.query(models.Parish).count() > 0:
        return
    from datetime import datetime, timedelta
    now = datetime.utcnow()

    # Demo parish
    parish = models.Parish(
        name="St. Thomas Aquinas Parish",
        display_parish_name="St. Thomas Aquinas Parish",
        slug="demo",
        plan="trial",
        trial_ends_at=now + timedelta(days=90),
        grace_ends_at=now + timedelta(days=97),
    )
    db.add(parish)
    db.flush()

    for i, label in enumerate(DEFAULT_CATEGORIES):
        db.add(models.Category(parish_id=parish.id, label=label, display_order=i))

    # Superadmin account (platform owner)
    superadmin = models.User(
        parish_id=parish.id,
        email="superadmin@panalangin.markmuya.com",
        password_hash=hash_password("Panalangin@2024"),
        role="superadmin"
    )
    db.add(superadmin)

    # Parish admin account
    admin = models.User(
        parish_id=parish.id,
        email="admin@demo.com",
        password_hash=hash_password("Admin@1234"),
        role="admin"
    )
    db.add(admin)

    # Parish staff account
    staff_user = models.User(
        parish_id=parish.id,
        email="staff@demo.com",
        password_hash=hash_password("Staff@1234"),
        role="staff"
    )
    db.add(staff_user)
    db.flush()

    # Sample access code for testing registration
    sample_code = models.AccessCode(
        code       = "WELCOME2024",
        created_by = superadmin.id,
        expires_at = now + timedelta(days=90),
        max_uses   = 10,
        note       = "Sample registration code",
    )
    db.add(sample_code)
    db.commit()

# Recreate all tables (safe — skips existing ones)
Base.metadata.create_all(bind=engine)

def sync_categories(db: Session):
    """Add missing defaults and migrate labels without overwriting parish order."""
    parishes = db.query(models.Parish).all()
    for parish in parishes:
        existing = {
            c.label: c
            for c in db.query(models.Category)
                       .filter(models.Category.parish_id == parish.id)
                       .all()
        }

        # "Special Intention" → "Special Intentions"
        if "Special Intention" in existing:
            old = existing.pop("Special Intention")
            if "Special Intentions" in existing:
                # Both exist — move intentions to the surviving one then delete old
                db.query(models.Intention).filter(
                    models.Intention.category_id == old.id
                ).update({"category_id": existing["Special Intentions"].id})
                db.delete(old)
            else:
                old.label = "Special Intentions"
                existing["Special Intentions"] = old

        # "Birthday" → "Gift of Life"
        if "Birthday" in existing:
            old = existing.pop("Birthday")
            if "Gift of Life" in existing:
                # Both exist — retransfer Birthday intentions to Gift of Life
                db.query(models.Intention).filter(
                    models.Intention.category_id == old.id
                ).update({"category_id": existing["Gift of Life"].id})
                db.delete(old)
            else:
                old.label = "Gift of Life"
                existing["Gift of Life"] = old

        # "Speedy Recovery" → "Fast Recovery"
        if "Speedy Recovery" in existing:
            old = existing.pop("Speedy Recovery")
            if "Fast Recovery" in existing:
                db.query(models.Intention).filter(
                    models.Intention.category_id == old.id
                ).update({"category_id": existing["Fast Recovery"].id})
                db.delete(old)
            else:
                old.label = "Fast Recovery"
                existing["Fast Recovery"] = old

        next_order = max(
            (category.display_order for category in existing.values()),
            default=-1,
        ) + 1
        for label in DEFAULT_CATEGORIES:
            if label not in existing:
                db.add(models.Category(
                    parish_id=parish.id,
                    label=label,
                    display_order=next_order,
                ))
                next_order += 1
    db.commit()

with next(get_db()) as _db:
    if SEED_DEMO:
        seed_demo(_db)
    sync_categories(_db)

# ─────────────────────────────────────────────
# Pydantic schemas
# ─────────────────────────────────────────────
class IntentionCreate(BaseModel):
    name:        str
    offered_by:  str
    category_id: int
    start_date:  date
    end_date:    date

class IntentionRequestCreate(BaseModel):
    names:       list[str]
    offered_by:  str
    category_id: int
    start_date:  Optional[date] = None
    end_date:    Optional[date] = None
    birthday_dates: Optional[dict[str, date]] = None

class BatchIntentionItem(BaseModel):
    name: str
    category_id: int

class BatchIntentionRequestCreate(BaseModel):
    offered_by: str
    start_date: date
    end_date: date
    intentions: list[BatchIntentionItem]

class RequestEndDateUpdate(BaseModel):
    end_date: date

class IntentionUpdate(IntentionCreate):
    pass

class TokenResponse(BaseModel):
    access_token: str
    token_type:   str = "bearer"

# ─────────────────────────────────────────────
# AUTH ROUTES
# ─────────────────────────────────────────────
class LoginJSON(BaseModel):
    email:    str
    password: str

def do_login(email: str, password: str, db: Session):
    user = db.query(models.User).filter(
        models.User.email == email.strip(),
        models.User.is_active == True
    ).first()
    if not user or not verify_password(password, user.password_hash):
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    parish = db.query(models.Parish).filter(
        models.Parish.id == user.parish_id
    ).first()
    if not parish:
        raise HTTPException(status_code=400, detail="Parish not found")
    token = create_token({
        "sub":       str(user.id),
        "parish_id": parish.id,
        "slug":      parish.slug,
        "role":      user.role,
    })
    return {"access_token": token, "token_type": "bearer"}

@app.post("/auth/token", response_model=TokenResponse)
def login(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    return do_login(form.username, form.password, db)

@app.post("/auth/login", response_model=TokenResponse)
def login_json(payload: LoginJSON, db: Session = Depends(get_db)):
    return do_login(payload.email, payload.password, db)

# ─────────────────────────────────────────────
# DASHBOARD API (requires login)
# ─────────────────────────────────────────────

# Get all intentions for dashboard — optionally filtered by date range
@app.get("/api/dashboard/intentions")
def list_intentions(
    start: Optional[date] = None,
    end:   Optional[date] = None,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    q = db.query(models.Intention).filter(
        models.Intention.parish_id == current_user.parish_id,
        models.Intention.is_active == True
    )
    if start:
        q = q.filter(models.Intention.end_date >= start)
    if end:
        q = q.filter(models.Intention.start_date <= end)

    intentions = q.order_by(
        models.Intention.start_date,
        models.Intention.category_id
    ).all()

    return [
        {
            "id":          i.id,
            "name":        i.name,
            "offered_by":  i.offered_by,
            "category_id": i.category_id,
            "category":    i.category.label,
            "start_date":  i.start_date.isoformat(),
            "end_date":    i.end_date.isoformat(),
        }
        for i in intentions
    ]

# Get categories for the logged-in parish
class CategoryOrderUpdate(BaseModel):
    category_ids: list[int]

class CategorySetting(BaseModel):
    id: int
    is_active: bool

class CategorySettingsUpdate(BaseModel):
    categories: list[CategorySetting]

@app.get("/api/dashboard/categories")
def list_categories(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    cats = db.query(models.Category).filter(
        models.Category.parish_id == current_user.parish_id,
        models.Category.is_active == True
    ).order_by(models.Category.display_order, models.Category.id).all()
    return [
        {
            "id": c.id,
            "label": c.label,
            "display_order": c.display_order,
            "is_active": True,
        }
        for c in cats
    ]

@app.get("/api/dashboard/categories/settings")
def get_category_settings(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    categories = db.query(models.Category).filter(
        models.Category.parish_id == current_user.parish_id,
    ).order_by(models.Category.display_order, models.Category.id).all()
    return [
        {
            "id": category.id,
            "label": category.label,
            "display_order": category.display_order,
            "is_active": bool(category.is_active),
        }
        for category in categories
    ]

@app.put("/api/dashboard/categories/settings")
def update_category_settings(
    payload: CategorySettingsUpdate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    categories = db.query(models.Category).filter(
        models.Category.parish_id == current_user.parish_id,
    ).all()
    parish_ids = {category.id for category in categories}
    submitted_ids = [setting.id for setting in payload.categories]
    if len(submitted_ids) != len(set(submitted_ids)):
        raise HTTPException(status_code=422, detail="Category settings contain duplicates")
    if set(submitted_ids) != parish_ids:
        raise HTTPException(
            status_code=422,
            detail="Category settings must include every category in this parish",
        )
    if categories and not any(setting.is_active for setting in payload.categories):
        raise HTTPException(status_code=422, detail="At least one category must remain enabled")
    by_id = {category.id: category for category in categories}
    for display_order, setting in enumerate(payload.categories):
        category = by_id[setting.id]
        category.display_order = display_order
        category.is_active = setting.is_active
    db.commit()
    return {"message": "Category settings saved"}

@app.put("/api/dashboard/categories/order")
def update_category_order(
    payload: CategoryOrderUpdate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    categories = db.query(models.Category).filter(
        models.Category.parish_id == current_user.parish_id,
    ).all()
    parish_ids = {category.id for category in categories}
    submitted_ids = payload.category_ids
    if len(submitted_ids) != len(set(submitted_ids)):
        raise HTTPException(status_code=422, detail="Category order contains duplicates")
    if set(submitted_ids) != parish_ids:
        raise HTTPException(
            status_code=422,
            detail="Category order must include every category in this parish",
        )
    by_id = {category.id: category for category in categories}
    for display_order, category_id in enumerate(submitted_ids):
        by_id[category_id].display_order = display_order
    db.commit()
    return {"message": "Category order saved"}

@app.post("/api/dashboard/categories/order/reset")
def reset_category_order(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    categories = db.query(models.Category).filter(
        models.Category.parish_id == current_user.parish_id,
    ).all()
    default_positions = {label: index for index, label in enumerate(DEFAULT_CATEGORIES)}
    categories.sort(key=lambda category: (
        0 if category.label in default_positions else 1,
        default_positions.get(category.label, category.display_order),
        category.display_order,
        category.id,
    ))
    for display_order, category in enumerate(categories):
        category.display_order = display_order
    db.commit()
    return {"message": "Default category order restored"}

# ─────────────────────────────────────────────
# SWEEP — expired intention cleanup
# ─────────────────────────────────────────────
@app.delete("/api/dashboard/intentions/sweep")
def sweep_intentions(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    today = date.today()
    two_months_ago = today - timedelta(days=60)
    expired = db.query(models.Intention).filter(
        models.Intention.parish_id == current_user.parish_id,
        models.Intention.end_date < two_months_ago
    )
    affected_request_ids = [
        request_id
        for (request_id,) in expired.with_entities(
            models.Intention.offering_request_id
        ).distinct().all()
        if request_id is not None
    ]
    count = expired.delete(synchronize_session=False)

    deleted_requests = 0
    if affected_request_ids:
        deleted_requests = db.query(models.OfferingRequest).filter(
            models.OfferingRequest.parish_id == current_user.parish_id,
            models.OfferingRequest.id.in_(affected_request_ids),
            ~models.OfferingRequest.intentions.any(),
        ).delete(synchronize_session=False)

    db.commit()
    return {
        "deleted": count,
        "deleted_requests": deleted_requests,
        "cutoff": two_months_ago.isoformat(),
    }


def get_parish_category(db: Session, parish_id: int, category_id: int):
    category = db.query(models.Category).filter(
        models.Category.id == category_id,
        models.Category.parish_id == parish_id,
        models.Category.is_active == True,
    ).first()
    if not category:
        raise HTTPException(status_code=400, detail="Invalid category")
    return category


def parish_display_name(parish: models.Parish) -> str:
    """Use the registered name when no custom display name has been chosen."""
    custom_name = (parish.display_parish_name or "").strip()
    if not custom_name or custom_name == "Parish Name":
        return parish.name
    return custom_name


@app.post("/api/dashboard/intention-requests", status_code=201)
def create_intention_request(
    payload: IntentionRequestCreate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    names = [name.strip() for name in payload.names if name.strip()]
    if not names:
        raise HTTPException(status_code=400, detail="At least one name is required")
    if len(names) > 500:
        raise HTTPException(
            status_code=400,
            detail="A request cannot contain more than 500 intentions",
        )
    offered_by = payload.offered_by.strip()
    if not offered_by:
        raise HTTPException(status_code=400, detail="Offered by is required")
    category = get_parish_category(
        db, current_user.parish_id, payload.category_id
    )

    is_individual_date = category.label in {
        "Gift of Life",
        "Death Anniversary",
    }
    if is_individual_date:
        birthday_dates = payload.birthday_dates or {}
        missing_birthdays = [
            name for name in names if name not in birthday_dates
        ]
        if missing_birthdays:
            raise HTTPException(
                status_code=400,
                detail=f"An individual date is required for every {category.label} name",
            )
        request_start = min(birthday_dates[name] for name in names)
        request_end = max(birthday_dates[name] for name in names)
    else:
        if payload.start_date is None or payload.end_date is None:
            raise HTTPException(
                status_code=400,
                detail="Start date and end date are required",
            )
        if payload.end_date < payload.start_date:
            raise HTTPException(
                status_code=400,
                detail="End date must be on or after start date",
            )
        birthday_dates = {}
        request_start = payload.start_date
        request_end = payload.end_date

    offering_request = models.OfferingRequest(
        parish_id=current_user.parish_id,
        offered_by=offered_by,
        start_date=request_start,
        end_date=request_end,
    )
    db.add(offering_request)
    db.flush()

    intentions = [
        models.Intention(
            parish_id=current_user.parish_id,
            category_id=payload.category_id,
            name=name,
            offered_by=offered_by,
            start_date=(
                birthday_dates[name] if is_individual_date else request_start
            ),
            end_date=(
                birthday_dates[name] if is_individual_date else request_end
            ),
            offering_request_id=offering_request.id,
        )
        for name in names
    ]
    db.add_all(intentions)
    db.commit()
    return {
        "request_id": offering_request.id,
        "created": len(intentions),
        "message": "Intentions added",
    }


@app.post("/api/dashboard/intention-requests/batch", status_code=201)
def create_batch_intention_request(
    payload: BatchIntentionRequestCreate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    offered_by = payload.offered_by.strip()
    if not offered_by:
        raise HTTPException(status_code=400, detail="Offered by is required")
    if payload.end_date < payload.start_date:
        raise HTTPException(
            status_code=400,
            detail="End date must be on or after start date",
        )
    if not payload.intentions:
        raise HTTPException(
            status_code=400,
            detail="At least one intention is required",
        )
    if len(payload.intentions) > 500:
        raise HTTPException(
            status_code=400,
            detail="A request cannot contain more than 500 intentions",
        )

    category_ids = {item.category_id for item in payload.intentions}
    categories = db.query(models.Category).filter(
        models.Category.id.in_(category_ids),
        models.Category.parish_id == current_user.parish_id,
        models.Category.is_active == True,
    ).all()
    categories_by_id = {category.id: category for category in categories}
    if set(categories_by_id) != category_ids:
        raise HTTPException(status_code=400, detail="Invalid category")
    if any(
        categories_by_id[item.category_id].label in {
            "Gift of Life",
            "Death Anniversary",
        }
        for item in payload.intentions
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Gift of Life and Death Anniversary must use the existing "
                "form because each name requires its own date"
            ),
        )

    cleaned_items = []
    for item in payload.intentions:
        name = item.name.strip()
        if not name:
            raise HTTPException(
                status_code=400,
                detail="Every intention must have a name",
            )
        cleaned_items.append((name, item.category_id))

    offering_request = models.OfferingRequest(
        parish_id=current_user.parish_id,
        offered_by=offered_by,
        start_date=payload.start_date,
        end_date=payload.end_date,
    )
    db.add(offering_request)
    db.flush()
    db.add_all([
        models.Intention(
            parish_id=current_user.parish_id,
            category_id=category_id,
            name=name,
            offered_by=offered_by,
            start_date=payload.start_date,
            end_date=payload.end_date,
            offering_request_id=offering_request.id,
        )
        for name, category_id in cleaned_items
    ])
    db.commit()
    return {
        "request_id": offering_request.id,
        "created": len(cleaned_items),
        "message": "Intentions added",
    }


@app.get("/api/dashboard/intention-requests")
def list_intention_requests(
    q: str = "",
    start: Optional[date] = None,
    end: Optional[date] = None,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    search = " ".join(q.strip().split())
    if len(search) < 2:
        return []

    pattern = f"%{search}%"
    query = db.query(models.OfferingRequest).options(
        selectinload(models.OfferingRequest.intentions).joinedload(
            models.Intention.category
        )
    ).filter(
        models.OfferingRequest.parish_id == current_user.parish_id
    )
    query = query.filter(
        or_(
            models.OfferingRequest.offered_by.ilike(pattern),
            models.OfferingRequest.intentions.any(
                and_(
                    models.Intention.is_active == True,
                    models.Intention.name.ilike(pattern),
                )
            ),
        )
    )
    if start:
        query = query.filter(
            models.OfferingRequest.intentions.any(
                and_(
                    models.Intention.is_active == True,
                    models.Intention.end_date >= start,
                )
            )
        )
    if end:
        query = query.filter(
            models.OfferingRequest.intentions.any(
                and_(
                    models.Intention.is_active == True,
                    models.Intention.start_date <= end,
                )
            )
        )

    candidates = query.order_by(
        models.OfferingRequest.created_at.desc(),
        models.OfferingRequest.id.desc(),
    ).limit(100).all()

    search_lower = search.lower()

    def match_rank(request):
        offeror = (request.offered_by or "").strip().lower()
        intention_names = [
            item.name.strip().lower()
            for item in request.intentions
            if item.is_active
        ]
        if offeror == search_lower:
            return 0
        if offeror.startswith(search_lower):
            return 1
        if any(word.startswith(search_lower) for word in offeror.split()):
            return 2
        if search_lower in offeror:
            return 3
        if any(name == search_lower for name in intention_names):
            return 4
        if any(name.startswith(search_lower) for name in intention_names):
            return 5
        if any(
            word.startswith(search_lower)
            for name in intention_names
            for word in name.split()
        ):
            return 6
        return 7

    def match_distance(request):
        values = [(request.offered_by or "").strip().lower()]
        values.extend(
            item.name.strip().lower()
            for item in request.intentions
            if item.is_active
        )
        matching = [value for value in values if search_lower in value]
        return min(
            (len(value) - len(search_lower) for value in matching),
            default=10_000,
        )

    requests = sorted(
        candidates,
        key=lambda request: (
            match_rank(request),
            match_distance(request),
            -request.id,
        ),
    )[:20]

    result = []
    for request in requests:
        intentions = sorted(
            (item for item in request.intentions if item.is_active),
            key=lambda item: (item.start_date, item.name.lower()),
        )
        if not intentions:
            continue
        gift_count = sum(
            item.category.label == "Gift of Life"
            for item in intentions
        )
        range_intentions = [
            item
            for item in intentions
            if item.category.label != "Gift of Life"
        ]
        offeror_match = search_lower in (request.offered_by or "").lower()
        matched_intention_names = [
            item.name
            for item in intentions
            if search_lower in item.name.lower()
        ]
        result.append({
            "id": request.id,
            "offered_by": request.offered_by,
            "start_date": min(
                item.start_date for item in intentions
            ).isoformat(),
            "end_date": max(
                item.end_date for item in intentions
            ).isoformat(),
            "extendable": bool(range_intentions),
            "extendable_end_date": (
                max(item.end_date for item in range_intentions).isoformat()
                if range_intentions else None
            ),
            "gift_of_life_count": gift_count,
            "match_source": "offeror" if offeror_match else "intention",
            "matched_intention_names": matched_intention_names[:3],
            "intentions": [
                {
                    "id": item.id,
                    "name": item.name,
                    "category": item.category.label,
                    "start_date": item.start_date.isoformat(),
                    "end_date": item.end_date.isoformat(),
                }
                for item in intentions
            ],
        })
    return result


@app.put("/api/dashboard/intention-requests/{request_id}/end-date")
def extend_intention_request(
    request_id: int,
    payload: RequestEndDateUpdate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    offering_request = db.query(models.OfferingRequest).filter(
        models.OfferingRequest.id == request_id,
        models.OfferingRequest.parish_id == current_user.parish_id,
    ).first()
    if not offering_request:
        raise HTTPException(status_code=404, detail="Request not found")

    active_intentions = [
        item for item in offering_request.intentions if item.is_active
    ]
    extendable = [
        item
        for item in active_intentions
        if item.category.label != "Gift of Life"
    ]
    if not extendable:
        raise HTTPException(
            status_code=400,
            detail="Gift of Life intentions use individual dates and cannot be extended",
        )
    current_end = max(item.end_date for item in extendable)
    if payload.end_date <= current_end:
        raise HTTPException(
            status_code=400,
            detail="The new ending date must be later than the current ending date",
        )
    if any(payload.end_date < item.start_date for item in extendable):
        raise HTTPException(
            status_code=400,
            detail="The new ending date cannot be before an intention start date",
        )

    for item in extendable:
        item.end_date = payload.end_date
    offering_request.end_date = max(
        item.end_date for item in active_intentions
    )
    db.commit()
    return {
        "message": "Request ending date extended",
        "updated": len(extendable),
        "gift_of_life_unchanged": len(active_intentions) - len(extendable),
        "end_date": payload.end_date.isoformat(),
    }

# Add new intention
@app.post("/api/dashboard/intentions", status_code=201)
def create_intention(
    payload: IntentionCreate,
    background_tasks: BackgroundTasks,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if payload.end_date < payload.start_date:
        raise HTTPException(status_code=400, detail="End date must be on or after start date")
    get_parish_category(db, current_user.parish_id, payload.category_id)
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    offered_by = payload.offered_by.strip()
    if not offered_by:
        raise HTTPException(status_code=400, detail="Offered by is required")
    offering_request = models.OfferingRequest(
        parish_id=current_user.parish_id,
        offered_by=offered_by,
        start_date=payload.start_date,
        end_date=payload.end_date,
    )
    db.add(offering_request)
    db.flush()
    intention = models.Intention(
        parish_id   = current_user.parish_id,
        category_id = payload.category_id,
        name        = name,
        offered_by  = offered_by,
        start_date  = payload.start_date,
        end_date    = payload.end_date,
        offering_request_id = offering_request.id,
    )
    db.add(intention)
    db.commit()
    db.refresh(intention)
    return {"id": intention.id, "message": "Intention added"}

# Update intention
@app.put("/api/dashboard/intentions/{intention_id}")
def update_intention(
    intention_id: int,
    payload: IntentionUpdate,
    background_tasks: BackgroundTasks,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    intention = db.query(models.Intention).filter(
        models.Intention.id == intention_id,
        models.Intention.parish_id == current_user.parish_id,
        models.Intention.is_active == True
    ).first()
    if not intention:
        raise HTTPException(status_code=404, detail="Intention not found")
    if payload.end_date < payload.start_date:
        raise HTTPException(status_code=400, detail="End date must be on or after start date")
    get_parish_category(db, current_user.parish_id, payload.category_id)
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    offered_by = payload.offered_by.strip()
    if not offered_by:
        raise HTTPException(status_code=400, detail="Offered by is required")
    intention.name        = name
    intention.offered_by  = offered_by
    intention.category_id = payload.category_id
    intention.start_date  = payload.start_date
    intention.end_date    = payload.end_date
    db.commit()
    return {"message": "Intention updated"}

# Soft-delete intention
@app.delete("/api/dashboard/intentions/{intention_id}")
def delete_intention(
    intention_id: int,
    background_tasks: BackgroundTasks,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    intention = db.query(models.Intention).filter(
        models.Intention.id == intention_id,
        models.Intention.parish_id == current_user.parish_id,
        models.Intention.is_active == True
    ).first()
    if not intention:
        raise HTTPException(status_code=404, detail="Intention not found")
    intention.is_active = False
    db.commit()
    return {"message": "Intention deleted"}

# ─────────────────────────────────────────────
# PUBLIC DISPLAY API
# Returns only intentions active today, grouped
# by category — this is what display.html calls
# ─────────────────────────────────────────────
@app.get("/api/{slug}/intentions")
def get_display_intentions(
    slug: str,
    request: Request,
    db: Session = Depends(get_db),
):
    try:
        parish = db.query(models.Parish).filter(
            models.Parish.slug == slug,
            models.Parish.is_active == True
        ).first()
        if not parish:
            raise HTTPException(status_code=404, detail="Parish not found")

        today      = date.today()
        categories = db.query(models.Category).filter(
            models.Category.parish_id == parish.id,
            models.Category.is_active == True
        ).order_by(models.Category.display_order, models.Category.id).all()

        result = {
            "parish":       parish_display_name(parish),
            "theme_bg":     parish.theme_bg     or "#080c18",
            "theme_text":   parish.theme_text   or "#f0ead6",
            "theme_accent": parish.theme_accent or "#c9b97a",
            "theme_label":  parish.theme_label  or "#c9b97a",
            "display_interval_seconds": parish.display_interval_seconds or 5,
            "display_names_per_page": parish.display_names_per_page or 5,
            "display_columns": parish.display_columns or 1,
            "display_bg_fit": parish.display_bg_fit or "fill",
            "background_image_url": (
                f"/api/{slug}/background-image?v="
                f"{hashlib.sha256(parish.display_bg_image.encode()).hexdigest()[:12]}"
                if parish.display_bg_image else None
            ),
            "display_font_family": parish.display_font_family or "Georgia",
            "display_font_size": parish.display_font_size or 0,
            "display_font_bold": bool(parish.display_font_bold),
            "display_text_case": (
                "proper" if parish.display_text_case == "sentence"
                else parish.display_text_case or "original"
            ),
            "display_parish_color": parish.display_parish_color or "#8a7f6a",
            "display_parish_font_family": parish.display_parish_font_family or "Georgia",
            "display_parish_font_size": parish.display_parish_font_size or 20,
            "display_parish_font_bold": (
                True if parish.display_parish_font_bold is None
                else bool(parish.display_parish_font_bold)
            ),
            "display_parish_text_case": parish.display_parish_text_case or "upper",
            "display_title_font_family": parish.display_title_font_family or "Georgia",
            "display_title_font_size": parish.display_title_font_size or 30,
            "display_title_font_bold": (
                True if parish.display_title_font_bold is None
                else bool(parish.display_title_font_bold)
            ),
            "display_title_text_case": parish.display_title_text_case or "upper",
            "display_transition_color": parish.display_transition_color or "#c9b97a",
            "display_transition_font_family": parish.display_transition_font_family or "Georgia",
            "display_transition_font_size": parish.display_transition_font_size or 0,
            "display_transition_font_bold": bool(parish.display_transition_font_bold),
            "display_transition_text_case": parish.display_transition_text_case or "upper",
            "categories":   []
        }
        for cat in categories:
            intentions = db.query(models.Intention).filter(
                models.Intention.category_id == cat.id,
                models.Intention.is_active   == True,
                models.Intention.start_date  <= today,
                models.Intention.end_date    >= today,
            ).order_by(models.Intention.name).all()  # alphabetical

            if intentions:
                seen = set()
                unique_names = []
                for i in intentions:
                    key = i.name.strip().lower()
                    if key not in seen:
                        seen.add(key)
                        unique_names.append(i.name)
                result["categories"].append({
                    "label":         cat.label,
                    "display_order": cat.display_order,
                    "intentions": [
                        {"name": n}
                        for n in unique_names
                    ]
                })
        payload_json = json.dumps(
            result,
            sort_keys=True,
            separators=(",", ":"),
        )
        etag = f'"{hashlib.sha256(payload_json.encode()).hexdigest()}"'
        response_headers = {
            "ETag": etag,
            "Cache-Control": "private, no-cache",
        }
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=response_headers)
        return JSONResponse(content=result, headers=response_headers)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/{slug}/background-image")
def get_display_background(slug: str, db: Session = Depends(get_db)):
    parish = db.query(models.Parish).filter(
        models.Parish.slug == slug, models.Parish.is_active == True
    ).first()
    if not parish or not parish.display_bg_image:
        raise HTTPException(status_code=404, detail="Background image not found")
    match = re.fullmatch(
        r"data:(image/(?:jpeg|png|webp));base64,([A-Za-z0-9+/=\s]+)",
        parish.display_bg_image,
    )
    if not match:
        raise HTTPException(status_code=404, detail="Invalid background image")
    try:
        content = base64.b64decode(match.group(2), validate=True)
    except (ValueError, binascii.Error):
        raise HTTPException(status_code=404, detail="Invalid background image")
    return Response(
        content=content,
        media_type=match.group(1),
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def slugify(name: str) -> str:
    name = name.lower().strip()
    name = re.sub(r"[^a-z0-9\s-]", "", name)
    name = re.sub(r"[\s]+", "-", name)
    return re.sub(r"-+", "-", name).strip("-")

def add_calendar_months(value: datetime, months: int) -> datetime:
    """Add whole calendar months, clamping to the last valid day."""
    import calendar
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)

def parish_status(parish) -> dict:
    now = datetime.utcnow()
    days_left = None
    locked    = False
    warning   = False

    if parish.plan == "trial" and parish.trial_ends_at:
        delta = (parish.trial_ends_at - now).days
        days_left = max(0, delta)
        warning = days_left <= 7
        if days_left <= 0:
            if parish.grace_ends_at and now < parish.grace_ends_at:
                parish.plan = "grace"
            else:
                parish.plan = "suspended"
    elif parish.plan == "grace" and parish.grace_ends_at:
        if now > parish.grace_ends_at:
            parish.plan = "suspended"
    elif parish.plan == "active" and parish.paid_until:
        days_left = max(0, (parish.paid_until - now).days)
        if now > parish.paid_until:
            parish.plan = "grace"
            parish.grace_ends_at = now + timedelta(days=7)

    reminder_days = []
    if parish.plan == "trial":
        reminder_days = [30, 15]
    elif parish.plan == "active" and parish.subscription_plan == "monthly":
        reminder_days = [15, 7]
    elif parish.plan == "active" and parish.subscription_plan == "annual":
        reminder_days = [30, 15]

    reminder_due = (
        days_left is not None and days_left > 0 and
        any(days_left <= threshold for threshold in reminder_days)
    )

    return {
        "plan":      parish.plan,
        "days_left": days_left,
        "warning":   warning,
        "locked":    parish.plan == "suspended",
        "subscription_plan": parish.subscription_plan,
        "ends_at": (
            parish.paid_until.isoformat() if parish.plan in ("active", "grace") and parish.paid_until
            else parish.trial_ends_at.isoformat() if parish.trial_ends_at else None
        ),
        "reminder_due": reminder_due,
    }

# ─────────────────────────────────────────────
# SETUP — creates first superadmin (one-time)
# ─────────────────────────────────────────────
@app.post("/setup")
def setup(
    email:    str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db)
):
    existing = db.query(models.User).filter(models.User.role == "superadmin").first()
    if existing:
        raise HTTPException(status_code=403, detail="Setup already completed")
    user = models.User(
        parish_id     = 1,
        email         = email,
        password_hash = hash_password(password),
        role          = "superadmin"
    )
    db.add(user)
    db.commit()
    return {"message": "Superadmin created"}

# ─────────────────────────────────────────────
# REGISTRATION — parish self-registers
# ─────────────────────────────────────────────
class RegisterPayload(BaseModel):
    parish_name: str
    email:       str
    password:    str
    code:        str

@app.post("/api/register")
def register_parish(payload: RegisterPayload, db: Session = Depends(get_db)):
    # Validate access code
    now  = datetime.utcnow()
    code = db.query(models.AccessCode).filter(
        models.AccessCode.code       == payload.code.strip().upper(),
        models.AccessCode.expires_at  > now,
    ).first()
    if not code:
        raise HTTPException(status_code=400, detail="Invalid or expired access code")
    if code.times_used >= code.max_uses:
        raise HTTPException(status_code=400, detail="Access code has already been used")

    # Check email not already taken
    if db.query(models.User).filter(models.User.email == payload.email).first():
        raise HTTPException(status_code=400, detail="Email already registered")

    # Generate unique slug
    base_slug = slugify(payload.parish_name)
    slug = base_slug
    counter = 1
    while db.query(models.Parish).filter(models.Parish.slug == slug).first():
        slug = f"{base_slug}-{counter}"; counter += 1

    # Create parish
    parish = models.Parish(
        name          = payload.parish_name.strip(),
        display_parish_name = payload.parish_name.strip(),
        slug          = slug,
        plan          = "trial",
        trial_ends_at = now + timedelta(days=90),
        grace_ends_at = now + timedelta(days=97),
    )
    db.add(parish)
    db.flush()

    # Add default categories
    for i, label in enumerate(DEFAULT_CATEGORIES):
        db.add(models.Category(parish_id=parish.id, label=label, display_order=i))

    # Create admin user
    user = models.User(
        parish_id     = parish.id,
        email         = payload.email.strip(),
        password_hash = hash_password(payload.password),
        role          = "admin"
    )
    db.add(user)
    db.flush()

    # Mark code as used
    code.times_used += 1
    db.add(models.CodeRedemption(code_id=code.id, parish_id=parish.id))
    db.commit()

    return {"message": "Parish registered", "slug": slug}

# ─────────────────────────────────────────────
# PARISH STATUS — called by dashboard on login
# ─────────────────────────────────────────────
@app.get("/api/dashboard/status")
def get_status(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    parish = db.query(models.Parish).filter(
        models.Parish.id == current_user.parish_id
    ).first()
    status = parish_status(parish)
    db.commit()
    return {
        **status,
        "parish_name":    parish.name,
        "slug":           parish.slug,
        "role":           current_user.role,
        "email":          current_user.email,
        "tutorial_seen":  current_user.tutorial_seen,
        "dash_accent":    parish.dash_accent  or "#2d5a3d",
    }

# ─────────────────────────────────────────────
# SUPERADMIN — access codes
# ─────────────────────────────────────────────
def require_superadmin(current_user: models.User = Depends(get_current_user)):
    if current_user.role != "superadmin":
        raise HTTPException(status_code=403, detail="Superadmin only")
    return current_user

class CodeCreate(BaseModel):
    note:      Optional[str] = None
    max_uses:  int = 1
    days_valid: int = 90

@app.post("/api/superadmin/codes", status_code=201)
def create_code(
    payload: CodeCreate,
    current_user: models.User = Depends(require_superadmin),
    db: Session = Depends(get_db)
):
    code = models.AccessCode(
        code        = secrets.token_urlsafe(8).upper()[:10],
        created_by  = current_user.id,
        expires_at  = datetime.utcnow() + timedelta(days=payload.days_valid),
        max_uses    = payload.max_uses,
        note        = payload.note,
    )
    db.add(code)
    db.commit()
    db.refresh(code)
    return {
        "id":         code.id,
        "code":       code.code,
        "expires_at": code.expires_at.isoformat(),
        "max_uses":   code.max_uses,
        "note":       code.note,
    }

@app.get("/api/superadmin/codes")
def list_codes(
    current_user: models.User = Depends(require_superadmin),
    db: Session = Depends(get_db)
):
    codes = db.query(models.AccessCode).order_by(
        models.AccessCode.created_at.desc()
    ).all()
    return [
        {
            "id":         c.id,
            "code":       c.code,
            "note":       c.note,
            "expires_at": c.expires_at.isoformat(),
            "max_uses":   c.max_uses,
            "times_used": c.times_used,
            "available":  c.times_used < c.max_uses and c.expires_at > datetime.utcnow(),
        }
        for c in codes
    ]

@app.delete("/api/superadmin/codes/{code_id}")
def delete_code(
    code_id: int,
    current_user: models.User = Depends(require_superadmin),
    db: Session = Depends(get_db)
):
    code = db.query(models.AccessCode).filter(models.AccessCode.id == code_id).first()
    if not code:
        raise HTTPException(status_code=404, detail="Code not found")
    db.delete(code)
    db.commit()
    return {"message": "Code deleted"}

# ─────────────────────────────────────────────
# SUPERADMIN — parishes
# ─────────────────────────────────────────────
@app.get("/api/superadmin/parishes")
def list_parishes(
    current_user: models.User = Depends(require_superadmin),
    db: Session = Depends(get_db)
):
    parishes = db.query(models.Parish).order_by(models.Parish.created_at.desc()).all()
    return [
        {
            "id":            p.id,
            "name":          p.name,
            "slug":          p.slug,
            "plan":          p.plan,
            "is_active":     p.is_active,
            "trial_ends_at": p.trial_ends_at.isoformat() if p.trial_ends_at else None,
            "grace_ends_at": p.grace_ends_at.isoformat() if p.grace_ends_at else None,
            "paid_until":    p.paid_until.isoformat()    if p.paid_until    else None,
            "subscription_plan": p.subscription_plan,
            "created_at":    p.created_at.isoformat()    if p.created_at    else None,
        }
        for p in parishes
    ]

@app.put("/api/superadmin/parishes/{parish_id}")
def update_parish(
    parish_id: int,
    is_active: Optional[bool] = None,
    extend_trial_days: Optional[int] = None,
    trial_ends_at: Optional[datetime] = None,
    current_user: models.User = Depends(require_superadmin),
    db: Session = Depends(get_db)
):
    parish = db.query(models.Parish).filter(models.Parish.id == parish_id).first()
    if not parish:
        raise HTTPException(status_code=404, detail="Parish not found")
    if is_active is not None:
        parish.is_active = is_active
    if extend_trial_days:
        base = parish.trial_ends_at or datetime.utcnow()
        parish.trial_ends_at = base + timedelta(days=extend_trial_days)
        parish.grace_ends_at = parish.trial_ends_at + timedelta(days=7)
        parish.plan = "trial"
    if trial_ends_at is not None:
        parish.trial_ends_at = trial_ends_at
        parish.grace_ends_at = trial_ends_at + timedelta(days=7)
        if parish.plan != "active":
            parish.plan = "trial"
    db.commit()
    return {"message": "Parish updated"}

# ─────────────────────────────────────────────
# SUPERADMIN — manual parish creation
# ─────────────────────────────────────────────
class ParishCreate(BaseModel):
    parish_name: str
    admin_email: str
    admin_password: str
    trial_days: int = 90

@app.post("/api/superadmin/parishes", status_code=201)
def create_parish_manual(
    payload: ParishCreate,
    current_user: models.User = Depends(require_superadmin),
    db: Session = Depends(get_db)
):
    now = datetime.utcnow()

    if db.query(models.User).filter(models.User.email == payload.admin_email).first():
        raise HTTPException(status_code=400, detail="Email already registered")

    base_slug = slugify(payload.parish_name)
    slug = base_slug
    counter = 1
    while db.query(models.Parish).filter(models.Parish.slug == slug).first():
        slug = f"{base_slug}-{counter}"; counter += 1

    parish = models.Parish(
        name          = payload.parish_name.strip(),
        display_parish_name = payload.parish_name.strip(),
        slug          = slug,
        plan          = "trial",
        trial_ends_at = now + timedelta(days=payload.trial_days),
        grace_ends_at = now + timedelta(days=payload.trial_days + 7),
    )
    db.add(parish)
    db.flush()

    for i, label in enumerate(DEFAULT_CATEGORIES):
        db.add(models.Category(parish_id=parish.id, label=label, display_order=i))

    admin = models.User(
        parish_id     = parish.id,
        email         = payload.admin_email.strip(),
        password_hash = hash_password(payload.admin_password),
        role          = "admin"
    )
    db.add(admin)
    db.commit()
    db.refresh(parish)

    return {
        "message":      "Parish created",
        "slug":         parish.slug,
        "parish_name":  parish.name,
        "admin_email":  payload.admin_email,
        "display_url":  f"/{parish.slug}/display",
        "trial_days":   payload.trial_days,
    }

# ─────────────────────────────────────────────
# SETTINGS — slug check and update
# ─────────────────────────────────────────────
@app.get("/api/dashboard/slug/check")
def check_slug(
    slug: str,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    slug = slugify(slug)
    if not slug:
        return {"available": False, "reason": "Slug cannot be empty"}
    if len(slug) < 3:
        return {"available": False, "reason": "Slug must be at least 3 characters"}
    existing = db.query(models.Parish).filter(
        models.Parish.slug == slug,
        models.Parish.id   != current_user.parish_id
    ).first()
    if existing:
        return {"available": False, "reason": "This name is already taken by another parish"}
    return {"available": True, "slug": slug}

class SlugUpdate(BaseModel):
    slug: str

@app.put("/api/dashboard/slug")
def update_slug(
    payload: SlugUpdate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    new_slug = slugify(payload.slug)
    if not new_slug or len(new_slug) < 3:
        raise HTTPException(status_code=400, detail="Invalid slug")

    # Check uniqueness
    existing = db.query(models.Parish).filter(
        models.Parish.slug == new_slug,
        models.Parish.id   != current_user.parish_id
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="This name is already taken")

    parish = db.query(models.Parish).filter(
        models.Parish.id == current_user.parish_id
    ).first()

    old_slug = parish.slug

    # Log the change
    log = models.SlugChangeLog(
        parish_id  = parish.id,
        old_slug   = old_slug,
        new_slug   = new_slug,
        changed_by = current_user.id,
    )
    db.add(log)
    parish.slug = new_slug
    db.commit()

    return {
        "message":  "Display URL updated successfully",
        "old_slug": old_slug,
        "new_slug": new_slug,
    }

@app.get("/api/dashboard/settings")
def get_settings(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    parish = db.query(models.Parish).filter(
        models.Parish.id == current_user.parish_id
    ).first()
    logs = db.query(models.SlugChangeLog).filter(
        models.SlugChangeLog.parish_id == parish.id
    ).order_by(models.SlugChangeLog.changed_at.desc()).limit(10).all()
    return {
        "parish_name": parish.name,
        "slug":        parish.slug,
        "slug_history": [
            {
                "old_slug":   l.old_slug,
                "new_slug":   l.new_slug,
                "changed_at": l.changed_at.isoformat(),
            }
            for l in logs
        ]
    }

# ─────────────────────────────────────────────
# SUPERADMIN — slug change log across all parishes
# ─────────────────────────────────────────────
@app.get("/api/superadmin/slug-changes")
def all_slug_changes(
    current_user: models.User = Depends(require_superadmin),
    db: Session = Depends(get_db)
):
    logs = db.query(models.SlugChangeLog).order_by(
        models.SlugChangeLog.changed_at.desc()
    ).limit(50).all()
    return [
        {
            "parish_id":  l.parish_id,
            "old_slug":   l.old_slug,
            "new_slug":   l.new_slug,
            "changed_at": l.changed_at.isoformat(),
        }
        for l in logs
    ]

# ─────────────────────────────────────────────
# USER MANAGEMENT — parish admin manages staff
# ─────────────────────────────────────────────
def require_admin(current_user: models.User = Depends(get_current_user)):
    if current_user.role not in ("admin", "superadmin"):
        raise HTTPException(status_code=403, detail="Admin only")
    return current_user

class StaffCreate(BaseModel):
    email:    str
    password: str
    name:     Optional[str] = None

@app.get("/api/dashboard/users")
def list_users(
    current_user: models.User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    users = db.query(models.User).filter(
        models.User.parish_id == current_user.parish_id,
        models.User.role == "staff"
    ).order_by(models.User.id).all()
    return [
        {
            "id":           u.id,
            "email":        u.email,
            "role":         u.role,
            "is_active":    u.is_active,
            "created_at":   u.created_at.isoformat() if u.created_at else None,
        }
        for u in users
    ]

@app.post("/api/dashboard/users", status_code=201)
def create_staff(
    payload: StaffCreate,
    current_user: models.User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    if db.query(models.User).filter(
        models.User.email == payload.email.strip()
    ).first():
        raise HTTPException(status_code=400, detail="Email already registered")
    user = models.User(
        parish_id     = current_user.parish_id,
        email         = payload.email.strip(),
        password_hash = hash_password(payload.password),
        role          = "staff",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"id": user.id, "message": "Parish staff account created"}

@app.put("/api/dashboard/users/{user_id}/toggle")
def toggle_user(
    user_id: int,
    current_user: models.User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    user = db.query(models.User).filter(
        models.User.id        == user_id,
        models.User.parish_id == current_user.parish_id,
        models.User.role == "staff"
    ).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.is_active = not user.is_active
    db.commit()
    return {"message": "User updated", "is_active": user.is_active}

@app.delete("/api/dashboard/users/{user_id}")
def delete_user(
    user_id: int,
    current_user: models.User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    user = db.query(models.User).filter(
        models.User.id        == user_id,
        models.User.parish_id == current_user.parish_id,
        models.User.role == "staff"
    ).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    db.delete(user)
    db.commit()
    return {"message": "User deleted"}

# ─────────────────────────────────────────────
# TUTORIAL — mark as seen
# ─────────────────────────────────────────────
@app.post("/api/dashboard/tutorial/done")
def mark_tutorial_done(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    user = db.query(models.User).filter(
        models.User.id == current_user.id
    ).first()
    user.tutorial_seen = True
    db.commit()
    return {"message": "Tutorial marked complete"}

# ─────────────────────────────────────────────
# MESSAGES — parish to superadmin
# ─────────────────────────────────────────────
class MessageCreate(BaseModel):
    subject:  str
    body:     str
    category: str = "general"  # general | suggestion | issue | assistance

class MessageReply(BaseModel):
    reply: str

@app.post("/api/dashboard/messages", status_code=201)
def send_message(
    payload: MessageCreate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if not payload.subject.strip():
        raise HTTPException(status_code=400, detail="Subject is required")
    if not payload.body.strip():
        raise HTTPException(status_code=400, detail="Message is required")
    if payload.category not in ("general","suggestion","issue","assistance"):
        raise HTTPException(status_code=400, detail="Invalid category")

    msg = models.Message(
        parish_id = current_user.parish_id,
        sent_by   = current_user.id,
        subject   = payload.subject.strip(),
        body      = payload.body.strip(),
        category  = payload.category,
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return {"id": msg.id, "message": "Message sent successfully"}

@app.get("/api/dashboard/messages")
def list_my_messages(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    msgs = db.query(models.Message).filter(
        models.Message.parish_id == current_user.parish_id
    ).order_by(models.Message.created_at.desc()).all()
    return [
        {
            "id":         m.id,
            "subject":    m.subject,
            "body":       m.body,
            "category":   m.category,
            "status":     m.status,
            "reply":      m.reply,
            "replied_at": m.replied_at.isoformat() if m.replied_at else None,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        }
        for m in msgs
    ]

@app.get("/api/superadmin/messages")
def list_all_messages(
    status: Optional[str] = None,
    current_user: models.User = Depends(require_superadmin),
    db: Session = Depends(get_db)
):
    q = db.query(models.Message).order_by(
        models.Message.created_at.desc()
    )
    if status:
        q = q.filter(models.Message.status == status)
    msgs = q.all()
    result = []
    for m in msgs:
        parish = db.query(models.Parish).filter(
            models.Parish.id == m.parish_id).first()
        sender = db.query(models.User).filter(
            models.User.id == m.sent_by).first()
        result.append({
            "id":           m.id,
            "parish_name":  parish.name if parish else "Unknown",
            "sender_email": sender.email if sender else "Unknown",
            "subject":      m.subject,
            "body":         m.body,
            "category":     m.category,
            "status":       m.status,
            "reply":        m.reply,
            "replied_at":   m.replied_at.isoformat() if m.replied_at else None,
            "created_at":   m.created_at.isoformat() if m.created_at else None,
        })
    return result

@app.put("/api/superadmin/messages/{msg_id}/reply")
def reply_message(
    msg_id: int,
    payload: MessageReply,
    current_user: models.User = Depends(require_superadmin),
    db: Session = Depends(get_db)
):
    msg = db.query(models.Message).filter(
        models.Message.id == msg_id
    ).first()
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    msg.reply      = payload.reply.strip()
    msg.status     = "replied"
    msg.replied_by = current_user.id
    msg.replied_at = datetime.utcnow()
    db.commit()
    return {"message": "Reply sent"}

@app.put("/api/superadmin/messages/{msg_id}/read")
def mark_read(
    msg_id: int,
    current_user: models.User = Depends(require_superadmin),
    db: Session = Depends(get_db)
):
    msg = db.query(models.Message).filter(
        models.Message.id == msg_id
    ).first()
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    if msg.status == "unread":
        msg.status = "read"
        db.commit()
    return {"message": "Marked as read"}

# Get unread message count for superadmin badge
@app.get("/api/superadmin/messages/unread-count")
def unread_count(
    current_user: models.User = Depends(require_superadmin),
    db: Session = Depends(get_db)
):
    count = db.query(models.Message).filter(
        models.Message.status == "unread"
    ).count()
    return {"count": count}

# ─────────────────────────────────────────────
# PASSWORD CHANGE
# ─────────────────────────────────────────────
class PasswordChange(BaseModel):
    current_password: str
    new_password:     str

class PasswordReset(BaseModel):
    new_password: str

@app.put("/api/dashboard/password")
def change_own_password(
    payload: PasswordChange,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if not verify_password(payload.current_password, current_user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    if len(payload.new_password) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")
    user = db.query(models.User).filter(models.User.id == current_user.id).first()
    user.password_hash = hash_password(payload.new_password)
    db.commit()
    return {"message": "Password changed successfully"}

@app.put("/api/dashboard/users/{user_id}/password")
def reset_staff_password(
    user_id: int,
    payload: PasswordReset,
    current_user: models.User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    if len(payload.new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    user = db.query(models.User).filter(
        models.User.id        == user_id,
        models.User.parish_id == current_user.parish_id,
        models.User.role      == "staff"
    ).first()
    if not user:
        raise HTTPException(status_code=404, detail="Staff not found")
    user.password_hash = hash_password(payload.new_password)
    db.commit()
    return {"message": "Password reset successfully"}

@app.put("/api/superadmin/users/{user_id}/password")
def superadmin_reset_password(
    user_id: int,
    payload: PasswordReset,
    current_user: models.User = Depends(require_superadmin),
    db: Session = Depends(get_db)
):
    if len(payload.new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    user = db.query(models.User).filter(
        models.User.id == user_id
    ).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.password_hash = hash_password(payload.new_password)
    db.commit()
    return {"message": "Password reset for " + user.email}

# ─────────────────────────────────────────────
# PAYMENTS — GCash submission and confirmation
# ─────────────────────────────────────────────
class PaymentCreate(BaseModel):
    plan:         str   # monthly | annual
    reference_no: str

class ManualPaymentCreate(BaseModel):
    parish_id: int
    plan: str
    reference_no: str
    payment_date: Optional[datetime] = None
    coverage_start: Optional[datetime] = None
    payment_method: Optional[str] = "Other"
    notes: Optional[str] = None

@app.post("/api/dashboard/payment", status_code=201)
def submit_payment(
    payload: PaymentCreate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if payload.plan not in ("monthly", "annual"):
        raise HTTPException(status_code=400, detail="Invalid plan")
    if not payload.reference_no.strip():
        raise HTTPException(status_code=400, detail="Reference number is required")

    # Check for duplicate pending reference
    existing = db.query(models.PaymentSubmission).filter(
        models.PaymentSubmission.reference_no == payload.reference_no.strip(),
        models.PaymentSubmission.status       == "pending"
    ).first()
    if existing:
        raise HTTPException(
            status_code=400,
            detail="This reference number has already been submitted."
        )

    amount = 200 if payload.plan == "monthly" else 2000
    months = 1   if payload.plan == "monthly" else 12

    payment = models.PaymentSubmission(
        parish_id    = current_user.parish_id,
        plan         = payload.plan,
        amount       = amount,
        reference_no = payload.reference_no.strip(),
        submitted_by = current_user.id,
        months_added = months,
    )
    db.add(payment)
    db.commit()
    db.refresh(payment)
    return {"id": payment.id, "message": "Payment submitted for review"}

@app.get("/api/superadmin/payments")
def list_payments(
    status: Optional[str] = None,
    parish_id: Optional[int] = None,
    current_user: models.User = Depends(require_superadmin),
    db: Session = Depends(get_db)
):
    q = db.query(models.PaymentSubmission).order_by(
        models.PaymentSubmission.created_at.desc()
    )
    if status:
        q = q.filter(models.PaymentSubmission.status == status)
    if parish_id is not None:
        q = q.filter(models.PaymentSubmission.parish_id == parish_id)
    payments = q.all()
    result = []
    for p in payments:
        parish = db.query(models.Parish).filter(
            models.Parish.id == p.parish_id).first()
        result.append({
            "id":           p.id,
            "parish_name":  parish.name if parish else "Unknown",
            "parish_id":    p.parish_id,
            "plan":         p.plan,
            "amount":       p.amount,
            "reference_no": p.reference_no,
            "status":       p.status,
            "months_added": p.months_added,
            "created_at":   p.created_at.isoformat() if p.created_at else None,
            "confirmed_at": p.confirmed_at.isoformat() if p.confirmed_at else None,
            "payment_date": p.payment_date.isoformat() if p.payment_date else None,
            "coverage_start": p.coverage_start.isoformat() if p.coverage_start else None,
            "coverage_end": p.coverage_end.isoformat() if p.coverage_end else None,
            "payment_method": p.payment_method,
            "notes": p.notes,
        })
    return result

@app.post("/api/superadmin/payments/manual", status_code=201)
def record_manual_payment(
    payload: ManualPaymentCreate,
    current_user: models.User = Depends(require_superadmin),
    db: Session = Depends(get_db),
):
    if payload.plan not in ("monthly", "annual"):
        raise HTTPException(status_code=400, detail="Invalid plan")
    if not payload.reference_no.strip():
        raise HTTPException(status_code=400, detail="Reference number is required")
    parish = db.query(models.Parish).filter(models.Parish.id == payload.parish_id).first()
    if not parish:
        raise HTTPException(status_code=404, detail="Parish not found")
    duplicate = db.query(models.PaymentSubmission).filter(
        models.PaymentSubmission.reference_no == payload.reference_no.strip(),
        models.PaymentSubmission.status.in_(("pending", "confirmed")),
    ).first()
    if duplicate:
        raise HTTPException(status_code=400, detail="This reference number is already recorded")

    now = datetime.utcnow()
    start = payload.coverage_start or (
        parish.paid_until if parish.paid_until and parish.paid_until > now else now
    )
    months = 1 if payload.plan == "monthly" else 12
    end = add_calendar_months(start, months)
    payment = models.PaymentSubmission(
        parish_id=parish.id,
        plan=payload.plan,
        amount=200 if payload.plan == "monthly" else 2000,
        reference_no=payload.reference_no.strip(),
        submitted_by=current_user.id,
        status="confirmed",
        confirmed_by=current_user.id,
        confirmed_at=now,
        months_added=months,
        payment_date=payload.payment_date or now,
        coverage_start=start,
        coverage_end=end,
        payment_method=(payload.payment_method or "Other").strip(),
        notes=(payload.notes or "").strip() or None,
    )
    parish.paid_until = end
    parish.subscription_plan = payload.plan
    parish.plan = "active"
    parish.grace_ends_at = end + timedelta(days=7)
    db.add(payment)
    db.commit()
    db.refresh(payment)
    return {"id": payment.id, "message": "Payment recorded", "paid_until": end.isoformat()}

@app.post("/api/superadmin/payments/{payment_id}/confirm")
def confirm_payment(
    payment_id: int,
    current_user: models.User = Depends(require_superadmin),
    db: Session = Depends(get_db)
):
    payment = db.query(models.PaymentSubmission).filter(
        models.PaymentSubmission.id == payment_id
    ).first()
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    if payment.status != "pending":
        raise HTTPException(status_code=400, detail="Payment already reviewed")

    parish = db.query(models.Parish).filter(
        models.Parish.id == payment.parish_id
    ).first()
    if not parish:
        raise HTTPException(status_code=404, detail="Parish not found")

    # Extend subscription
    now   = datetime.utcnow()
    base  = parish.paid_until if parish.paid_until and parish.paid_until > now             else (parish.trial_ends_at if parish.trial_ends_at and parish.trial_ends_at > now             else now)
    parish.paid_until    = add_calendar_months(base, payment.months_added)
    parish.subscription_plan = payment.plan
    parish.plan          = "active"
    parish.grace_ends_at = parish.paid_until + timedelta(days=7)

    payment.status       = "confirmed"
    payment.confirmed_by = current_user.id
    payment.confirmed_at = now
    payment.payment_date = payment.payment_date or payment.created_at or now
    payment.coverage_start = base
    payment.coverage_end = parish.paid_until
    payment.payment_method = payment.payment_method or "GCash"
    db.commit()

    return {
        "message":    "Payment confirmed",
        "parish":     parish.name,
        "plan":       payment.plan,
        "paid_until": parish.paid_until.isoformat(),
    }

@app.post("/api/superadmin/payments/{payment_id}/reject")
def reject_payment(
    payment_id: int,
    current_user: models.User = Depends(require_superadmin),
    db: Session = Depends(get_db)
):
    payment = db.query(models.PaymentSubmission).filter(
        models.PaymentSubmission.id == payment_id
    ).first()
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    payment.status       = "rejected"
    payment.confirmed_by = current_user.id
    payment.confirmed_at = datetime.utcnow()
    db.commit()
    return {"message": "Payment rejected"}

# ─────────────────────────────────────────────
# REGISTRATION REQUESTS — public form + superadmin review
# ─────────────────────────────────────────────
class RegistrationRequestCreate(BaseModel):
    parish_name:    str
    representative: str
    facebook_page:  str
    parish_priest:  str
    email:          str
    contact_number: str

@app.post("/api/request-code", status_code=201)
def submit_registration_request(
    payload: RegistrationRequestCreate,
    db: Session = Depends(get_db)
):
    # Validate all fields filled
    for field, val in payload.dict().items():
        if not val or not val.strip():
            raise HTTPException(
                status_code=400,
                detail=f"{field.replace('_', ' ').title()} is required"
            )

    # Prevent duplicate pending requests from same email
    existing = db.query(models.RegistrationRequest).filter(
        models.RegistrationRequest.email  == payload.email.strip().lower(),
        models.RegistrationRequest.status == "pending"
    ).first()
    if existing:
        raise HTTPException(
            status_code=400,
            detail="A pending request already exists for this email address."
        )

    req = models.RegistrationRequest(
        parish_name    = payload.parish_name.strip(),
        representative = payload.representative.strip(),
        facebook_page  = payload.facebook_page.strip(),
        parish_priest  = payload.parish_priest.strip(),
        email          = payload.email.strip().lower(),
        contact_number = payload.contact_number.strip(),
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    return {"id": req.id, "message": "Request submitted successfully"}

@app.get("/api/superadmin/requests")
def list_requests(
    status: Optional[str] = None,
    current_user: models.User = Depends(require_superadmin),
    db: Session = Depends(get_db)
):
    q = db.query(models.RegistrationRequest).order_by(
        models.RegistrationRequest.created_at.desc()
    )
    if status:
        q = q.filter(models.RegistrationRequest.status == status)
    requests = q.all()
    return [
        {
            "id":             r.id,
            "parish_name":    r.parish_name,
            "representative": r.representative,
            "facebook_page":  r.facebook_page,
            "parish_priest":  r.parish_priest,
            "email":          r.email,
            "contact_number": r.contact_number,
            "status":         r.status,
            "created_at":     r.created_at.isoformat() if r.created_at else None,
            "reviewed_at":    r.reviewed_at.isoformat() if r.reviewed_at else None,
        }
        for r in requests
    ]

@app.post("/api/superadmin/requests/{request_id}/approve")
def approve_request(
    request_id: int,
    current_user: models.User = Depends(require_superadmin),
    db: Session = Depends(get_db)
):
    req = db.query(models.RegistrationRequest).filter(
        models.RegistrationRequest.id == request_id
    ).first()
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
    if req.status != "pending":
        raise HTTPException(status_code=400, detail="Request already reviewed")

    # Auto-generate access code
    code = models.AccessCode(
        code        = secrets.token_urlsafe(8).upper()[:10],
        created_by  = current_user.id,
        expires_at  = datetime.utcnow() + timedelta(days=90),
        max_uses    = 1,
        note        = f"For {req.parish_name}",
    )
    db.add(code)
    db.flush()

    req.status         = "approved"
    req.access_code_id = code.id
    req.reviewed_by    = current_user.id
    req.reviewed_at    = datetime.utcnow()
    db.commit()
    db.refresh(code)

    return {
        "message":    "Request approved",
        "code":       code.code,
        "expires_at": code.expires_at.isoformat(),
        "email":      req.email,
        "parish":     req.parish_name,
    }

@app.post("/api/superadmin/requests/{request_id}/reject")
def reject_request(
    request_id: int,
    current_user: models.User = Depends(require_superadmin),
    db: Session = Depends(get_db)
):
    req = db.query(models.RegistrationRequest).filter(
        models.RegistrationRequest.id == request_id
    ).first()
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
    req.status      = "rejected"
    req.reviewed_by = current_user.id
    req.reviewed_at = datetime.utcnow()
    db.commit()
    return {"message": "Request rejected"}

# ─────────────────────────────────────────────
# THEME — get and update parish theme
# ─────────────────────────────────────────────
class ThemeUpdate(BaseModel):
    theme_bg:    str
    theme_text:  str
    theme_accent:str
    theme_label: str
    dash_accent: str
    display_interval_seconds: int = 5
    display_names_per_page: int = 5
    display_columns: int = 1
    display_bg_fit: str = "fill"
    background_image: Optional[str] = None
    remove_background_image: bool = False
    display_font_family: str = "Georgia"
    display_font_size: int = 0
    display_font_bold: bool = False
    display_text_case: str = "original"
    display_parish_name: str = ""
    display_parish_color: str = "#8a7f6a"
    display_parish_font_family: str = "Georgia"
    display_parish_font_size: int = 20
    display_parish_font_bold: bool = True
    display_parish_text_case: str = "upper"
    display_title_font_family: str = "Georgia"
    display_title_font_size: int = 30
    display_title_font_bold: bool = True
    display_title_text_case: str = "upper"
    display_transition_color: str = "#c9b97a"
    display_transition_font_family: str = "Georgia"
    display_transition_font_size: int = 0
    display_transition_font_bold: bool = False
    display_transition_text_case: str = "upper"

DISPLAY_FONTS = {
    "Georgia", "Arial", "Verdana", "Tahoma", "Trebuchet MS",
    "Times New Roman", "Courier New", "Garamond", "Palatino Linotype",
    "Arial Black",
}

@app.get("/api/dashboard/theme")
def get_theme(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    parish = db.query(models.Parish).filter(
        models.Parish.id == current_user.parish_id
    ).first()
    return {
        "theme_bg":     parish.theme_bg     or "#080c18",
        "theme_text":   parish.theme_text   or "#f0ead6",
        "theme_accent": parish.theme_accent or "#c9b97a",
        "theme_label":  parish.theme_label  or "#c9b97a",
        "dash_accent":  parish.dash_accent  or "#2d5a3d",
        "display_interval_seconds": parish.display_interval_seconds or 5,
        "display_names_per_page": parish.display_names_per_page or 5,
        "display_columns": parish.display_columns or 1,
        "display_bg_fit": parish.display_bg_fit or "fill",
        "has_background_image": bool(parish.display_bg_image),
        "background_image_url": (
            f"/api/{parish.slug}/background-image?v="
            f"{hashlib.sha256(parish.display_bg_image.encode()).hexdigest()[:12]}"
            if parish.display_bg_image else None
        ),
        "display_font_family": parish.display_font_family or "Georgia",
        "display_font_size": parish.display_font_size or 0,
        "display_font_bold": bool(parish.display_font_bold),
        "display_text_case": (
            "proper" if parish.display_text_case == "sentence"
            else parish.display_text_case or "original"
        ),
        "display_parish_name": parish_display_name(parish),
        "display_parish_color": parish.display_parish_color or "#8a7f6a",
        "display_parish_font_family": parish.display_parish_font_family or "Georgia",
        "display_parish_font_size": parish.display_parish_font_size or 20,
        "display_parish_font_bold": (
            True if parish.display_parish_font_bold is None
            else bool(parish.display_parish_font_bold)
        ),
        "display_parish_text_case": parish.display_parish_text_case or "upper",
        "display_title_font_family": parish.display_title_font_family or "Georgia",
        "display_title_font_size": parish.display_title_font_size or 30,
        "display_title_font_bold": (
            True if parish.display_title_font_bold is None
            else bool(parish.display_title_font_bold)
        ),
        "display_title_text_case": parish.display_title_text_case or "upper",
        "display_transition_color": parish.display_transition_color or "#c9b97a",
        "display_transition_font_family": parish.display_transition_font_family or "Georgia",
        "display_transition_font_size": parish.display_transition_font_size or 0,
        "display_transition_font_bold": bool(parish.display_transition_font_bold),
        "display_transition_text_case": parish.display_transition_text_case or "upper",
    }

@app.put("/api/dashboard/theme")
def update_theme(
    payload: ThemeUpdate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    parish = db.query(models.Parish).filter(
        models.Parish.id == current_user.parish_id
    ).first()
    parish.theme_bg     = payload.theme_bg
    parish.theme_text   = payload.theme_text
    parish.theme_accent = payload.theme_accent
    parish.theme_label  = payload.theme_label
    parish.dash_accent  = payload.dash_accent
    if not 2 <= payload.display_interval_seconds <= 60:
        raise HTTPException(status_code=422, detail="Display timing must be 2 to 60 seconds")
    if not 4 <= payload.display_names_per_page <= 20:
        raise HTTPException(status_code=422, detail="Names per display must be 4 to 20")
    if payload.display_columns not in (1, 2):
        raise HTTPException(status_code=422, detail="Columns must be 1 or 2")
    if payload.display_bg_fit not in {"fill", "stretch", "tile", "center", "span"}:
        raise HTTPException(status_code=422, detail="Invalid background image fit")
    if payload.display_font_family not in DISPLAY_FONTS:
        raise HTTPException(status_code=422, detail="Invalid font")
    if payload.display_font_size != 0 and not 20 <= payload.display_font_size <= 120:
        raise HTTPException(status_code=422, detail="Font size must be Auto or 20 to 120")
    if payload.display_text_case not in {"original", "upper", "lower", "proper"}:
        raise HTTPException(status_code=422, detail="Invalid text case")
    display_parish_name = payload.display_parish_name.strip() or parish.name
    if len(display_parish_name) > 120:
        raise HTTPException(status_code=422, detail="Parish display name must be 120 characters or fewer")
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", payload.display_parish_color):
        raise HTTPException(status_code=422, detail="Invalid parish name color")
    if payload.display_parish_font_family not in DISPLAY_FONTS:
        raise HTTPException(status_code=422, detail="Invalid parish name font")
    if payload.display_title_font_family not in DISPLAY_FONTS:
        raise HTTPException(status_code=422, detail="Invalid intention title font")
    if payload.display_parish_font_size != 0 and not 8 <= payload.display_parish_font_size <= 72:
        raise HTTPException(status_code=422, detail="Parish name size must be Auto or 8 to 72")
    if payload.display_title_font_size != 0 and not 8 <= payload.display_title_font_size <= 72:
        raise HTTPException(status_code=422, detail="Intention title size must be Auto or 8 to 72")
    if payload.display_parish_text_case not in {"original", "upper", "lower", "proper"}:
        raise HTTPException(status_code=422, detail="Invalid parish name text case")
    if payload.display_title_text_case not in {"original", "upper", "lower", "proper"}:
        raise HTTPException(status_code=422, detail="Invalid intention title text case")
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", payload.display_transition_color):
        raise HTTPException(status_code=422, detail="Invalid transition text color")
    if payload.display_transition_font_family not in DISPLAY_FONTS:
        raise HTTPException(status_code=422, detail="Invalid transition text font")
    if payload.display_transition_font_size != 0 and not 8 <= payload.display_transition_font_size <= 120:
        raise HTTPException(status_code=422, detail="Transition text size must be Auto or 8 to 120")
    if payload.display_transition_text_case not in {"original", "upper", "lower", "proper"}:
        raise HTTPException(status_code=422, detail="Invalid transition text case")
    if payload.background_image:
        match = re.fullmatch(
            r"data:image/(?:jpeg|png|webp);base64,([A-Za-z0-9+/=\s]+)",
            payload.background_image,
        )
        if not match:
            raise HTTPException(status_code=422, detail="Use a JPEG, PNG, or WebP image")
        try:
            decoded = base64.b64decode(match.group(1), validate=True)
        except (ValueError, binascii.Error):
            raise HTTPException(status_code=422, detail="Invalid background image")
        if len(decoded) > 1_500_000:
            raise HTTPException(status_code=422, detail="Background image must be 1.5 MB or smaller")
        parish.display_bg_image = payload.background_image
    elif payload.remove_background_image:
        parish.display_bg_image = None
    parish.display_interval_seconds = payload.display_interval_seconds
    parish.display_names_per_page = payload.display_names_per_page
    parish.display_columns = payload.display_columns
    parish.display_bg_fit = payload.display_bg_fit
    parish.display_font_family = payload.display_font_family
    parish.display_font_size = payload.display_font_size
    parish.display_font_bold = payload.display_font_bold
    parish.display_text_case = payload.display_text_case
    parish.display_parish_name = display_parish_name
    parish.display_parish_color = payload.display_parish_color
    parish.display_parish_font_family = payload.display_parish_font_family
    parish.display_parish_font_size = payload.display_parish_font_size
    parish.display_parish_font_bold = payload.display_parish_font_bold
    parish.display_parish_text_case = payload.display_parish_text_case
    parish.display_title_font_family = payload.display_title_font_family
    parish.display_title_font_size = payload.display_title_font_size
    parish.display_title_font_bold = payload.display_title_font_bold
    parish.display_title_text_case = payload.display_title_text_case
    parish.display_transition_color = payload.display_transition_color
    parish.display_transition_font_family = payload.display_transition_font_family
    parish.display_transition_font_size = payload.display_transition_font_size
    parish.display_transition_font_bold = payload.display_transition_font_bold
    parish.display_transition_text_case = payload.display_transition_text_case
    db.commit()
    return {"message": "Theme updated"}

# ─────────────────────────────────────────────
# PUBLIC SEARCH — parishioner name lookup
# ─────────────────────────────────────────────
@app.get("/api/{slug}/search")
def search_intentions(
    slug: str,
    q:    str = "",
    db:   Session = Depends(get_db)
):
    parish = db.query(models.Parish).filter(
        models.Parish.slug      == slug,
        models.Parish.is_active == True
    ).first()
    if not parish:
        raise HTTPException(status_code=404, detail="Parish not found")

    if not q or len(q.strip()) < 1:
        return {"results": [], "total": 0}

    from datetime import date, timedelta
    today     = date.today()
    week_end  = today + timedelta(days=6)

    intentions = db.query(models.Intention).join(models.Category).filter(
        models.Intention.parish_id  == parish.id,
        models.Intention.is_active  == True,
        models.Intention.start_date <= week_end,
        models.Intention.end_date   >= today,
        models.Category.is_active   == True,
        models.Intention.name.ilike(f"%{q.strip()}%")
    ).order_by(models.Intention.name).all()

    return {
        "results": [
            {
                "name":       i.name,
                "category":   i.category.label,
                "start_date": i.start_date.isoformat(),
                "end_date":   i.end_date.isoformat(),
            }
            for i in intentions
        ],
        "total": len(intentions)
    }


# ─────────────────────────────────────────────
# SERVE PAGES
# ─────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def root():
    base_dir  = os.path.dirname(os.path.abspath(__file__))
    html_path = os.path.join(base_dir, "static", "index.html")
    try:
        with open(html_path, encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        return RedirectResponse(url="/static/dashboard.html")

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page():
    base_dir  = os.path.dirname(os.path.abspath(__file__))
    html_path = os.path.join(base_dir, "static", "dashboard.html")
    with open(html_path, encoding="utf-8") as f:
        html = f.read()
    return Response(
        content=html,
        media_type="text/html",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma":        "no-cache",
        }
    )

@app.get("/favicon.ico")
def favicon():
    svg = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
      <circle cx="50" cy="50" r="45" fill="#c9b97a"/>
      <text x="50" y="68" font-size="55" text-anchor="middle" fill="#1a1814">+</text>
    </svg>'''.encode("utf-8")
    return Response(content=svg, media_type="image/svg+xml")

@app.get("/register", response_class=HTMLResponse)
def register_page():
    return RedirectResponse(url="/static/register.html")

@app.get("/request-code", response_class=HTMLResponse)
def request_code_page():
    return RedirectResponse(url="/static/request-code.html")

@app.get("/{slug}/display", response_class=HTMLResponse)
def display_page(slug: str):
    base_dir  = os.path.dirname(os.path.abspath(__file__))
    html_path = os.path.join(base_dir, "static", "display.html")
    try:
        with open(html_path, encoding="utf-8") as f:
            html = f.read()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Display page not found")
    html = html.replace("const PARISH_SLUG = 'demo'",
                        f"const PARISH_SLUG = '{slug}'")
    return HTMLResponse(
        content=html,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )
