from sqlalchemy import Column, Integer, String, Boolean, Date, DateTime, ForeignKey, func, Text
from sqlalchemy.orm import relationship
from database import Base


class Parish(Base):
    __tablename__ = "parishes"

    id         = Column(Integer, primary_key=True, index=True)
    name       = Column(String, nullable=False)
    slug       = Column(String, unique=True, index=True, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    is_active      = Column(Boolean, default=True)
    plan           = Column(String, default="trial")   # trial | active | grace | suspended
    trial_ends_at  = Column(DateTime, nullable=True)
    grace_ends_at  = Column(DateTime, nullable=True)
    paid_until     = Column(DateTime, nullable=True)
    # Theme customization
    theme_bg       = Column(String, default="#080c18")   # display background
    theme_text     = Column(String, default="#f0ead6")   # display name text
    theme_accent   = Column(String, default="#c9b97a")   # gold accent
    theme_label    = Column(String, default="#c9b97a")   # category label
    # Dashboard theme
    dash_accent    = Column(String, default="#2d5a3d")   # dashboard accent

    users       = relationship("User",      back_populates="parish", cascade="all, delete")
    categories  = relationship("Category",  back_populates="parish", cascade="all, delete",
                               order_by="Category.display_order")
    intentions   = relationship("Intention",   back_populates="parish", cascade="all, delete")
    access_codes = relationship("AccessCode", secondary="code_redemptions",
                                back_populates="used_by")


class User(Base):
    __tablename__ = "users"

    id            = Column(Integer, primary_key=True, index=True)
    parish_id     = Column(Integer, ForeignKey("parishes.id"), nullable=False)
    email         = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    role          = Column(String, default="secretary")   # secretary | admin
    is_active     = Column(Boolean, default=True)
    tutorial_seen = Column(Boolean, default=False)
    created_at    = Column(DateTime, server_default=func.now())

    parish = relationship("Parish", back_populates="users")


class Category(Base):
    __tablename__ = "categories"

    id            = Column(Integer, primary_key=True, index=True)
    parish_id     = Column(Integer, ForeignKey("parishes.id"), nullable=False)
    label         = Column(String, nullable=False)   # e.g. "Thanksgiving", "Souls"
    display_order = Column(Integer, default=0)
    is_active     = Column(Boolean, default=True)
    tutorial_seen = Column(Boolean, default=False)

    parish     = relationship("Parish",    back_populates="categories")
    intentions = relationship("Intention", back_populates="category",
                              cascade="all, delete")


class Intention(Base):
    __tablename__ = "intentions"

    id          = Column(Integer, primary_key=True, index=True)
    parish_id   = Column(Integer, ForeignKey("parishes.id"), nullable=False)
    category_id = Column(Integer, ForeignKey("categories.id"), nullable=False)
    name        = Column(String, nullable=False)        # honoree name
    offered_by  = Column(String, nullable=False)        # family / donor name
    start_date  = Column(Date, nullable=False)
    end_date    = Column(Date, nullable=False)
    is_active   = Column(Boolean, default=True)
    created_at  = Column(DateTime, server_default=func.now())

    parish   = relationship("Parish",   back_populates="intentions")
    category = relationship("Category", back_populates="intentions")


class AccessCode(Base):
    __tablename__ = "access_codes"

    id          = Column(Integer, primary_key=True, index=True)
    code        = Column(String, unique=True, index=True, nullable=False)
    created_by  = Column(Integer, ForeignKey("users.id"), nullable=True)
    expires_at  = Column(DateTime, nullable=False)
    max_uses    = Column(Integer, default=1)
    times_used  = Column(Integer, default=0)
    note        = Column(Text, nullable=True)       # e.g. "For Sto. Nino Parish"
    created_at  = Column(DateTime, server_default=func.now())

    used_by = relationship("Parish", secondary="code_redemptions",
                           back_populates="access_codes")


class CodeRedemption(Base):
    __tablename__ = "code_redemptions"

    id         = Column(Integer, primary_key=True)
    code_id    = Column(Integer, ForeignKey("access_codes.id"), nullable=False)
    parish_id  = Column(Integer, ForeignKey("parishes.id"),     nullable=False)
    redeemed_at = Column(DateTime, server_default=func.now())


class SlugChangeLog(Base):
    __tablename__ = "slug_change_logs"

    id         = Column(Integer, primary_key=True)
    parish_id  = Column(Integer, ForeignKey("parishes.id"), nullable=False)
    old_slug   = Column(String, nullable=False)
    new_slug   = Column(String, nullable=False)
    changed_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    changed_at = Column(DateTime, server_default=func.now())


class RegistrationRequest(Base):
    __tablename__ = "registration_requests"

    id                = Column(Integer, primary_key=True, index=True)
    parish_name       = Column(String,  nullable=False)
    representative    = Column(String,  nullable=False)
    facebook_page     = Column(String,  nullable=False)
    parish_priest     = Column(String,  nullable=False)
    email             = Column(String,  nullable=False)
    contact_number    = Column(String,  nullable=False)
    status            = Column(String,  default="pending")  # pending | approved | rejected
    access_code_id    = Column(Integer, ForeignKey("access_codes.id"), nullable=True)
    reviewed_by       = Column(Integer, ForeignKey("users.id"),        nullable=True)
    reviewed_at       = Column(DateTime, nullable=True)
    created_at        = Column(DateTime, server_default=func.now())


class PaymentSubmission(Base):
    __tablename__ = "payment_submissions"

    id             = Column(Integer, primary_key=True, index=True)
    parish_id      = Column(Integer, ForeignKey("parishes.id"), nullable=False)
    plan           = Column(String,  nullable=False)   # monthly | annual
    amount         = Column(Integer, nullable=False)   # 200 or 2000
    reference_no   = Column(String,  nullable=False)
    submitted_by   = Column(Integer, ForeignKey("users.id"), nullable=False)
    status         = Column(String,  default="pending") # pending | confirmed | rejected
    confirmed_by   = Column(Integer, ForeignKey("users.id"), nullable=True)
    confirmed_at   = Column(DateTime, nullable=True)
    months_added   = Column(Integer, nullable=True)    # 1 or 12
    created_at     = Column(DateTime, server_default=func.now())


class Message(Base):
    __tablename__ = "messages"

    id          = Column(Integer, primary_key=True, index=True)
    parish_id   = Column(Integer, ForeignKey("parishes.id"), nullable=False)
    sent_by     = Column(Integer, ForeignKey("users.id"),    nullable=False)
    subject     = Column(String,  nullable=False)
    body        = Column(Text,    nullable=False)
    category    = Column(String,  default="general")  # general | suggestion | issue | assistance
    status      = Column(String,  default="unread")   # unread | read | replied
    reply       = Column(Text,    nullable=True)
    replied_by  = Column(Integer, ForeignKey("users.id"), nullable=True)
    replied_at  = Column(DateTime, nullable=True)
    created_at  = Column(DateTime, server_default=func.now())
