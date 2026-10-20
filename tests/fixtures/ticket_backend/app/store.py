"""
简易 JSON 文件存储 — 账号 + 订单持久化
"""

import datetime as dt
import errno
import hashlib
import json
import os
import re
import secrets
import sqlite3
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows development environments
    fcntl = None

# Releases are immutable. Production data lives outside the versioned release
# tree; local development keeps using backend/data unless explicitly overridden.
_RELEASE_DATA_DIR = Path("/var/lib/ticket-system/backend-data")
DATA_DIR = Path(os.getenv("TICKET_DATA_DIR") or (_RELEASE_DATA_DIR if _RELEASE_DATA_DIR.exists() else Path(__file__).parent.parent / "data"))
DATA_DIR.mkdir(exist_ok=True)

ACCOUNTS_FILE = DATA_DIR / "accounts.json"
ORDERS_FILE = DATA_DIR / "orders.json"
ACTIVITIES_FILE = DATA_DIR / "spread_activities.json"
LOTTERY_ACTIVITIES_FILE = DATA_DIR / "lottery_activities.json"
WPLUS_USAGE_FILE = DATA_DIR / "wplus_usage.json"
REFUND_QUOTA_FILE = DATA_DIR / "refund_quota.json"
COUPON_NOTES_FILE = DATA_DIR / "coupon_notes.json"
CUSTOMER_COUPON_PAGES_FILE = DATA_DIR / "customer_coupon_pages.json"
COLA_AUTO_CLAIM_FILE = DATA_DIR / "cola_auto_claim.json"
# Shared-pool bookkeeping deliberately contains no Wanda token or raw coupon
# code. It lets a shared account pool remain auditable per customer/operator.
COUPON_LEDGER_FILE = DATA_DIR / "coupon_ledger.json"
ACCOUNT_LEASES_FILE = DATA_DIR / "account_leases.json"
OPERATORS_FILE = DATA_DIR / "operators.json"
CUSTOMERS_FILE = DATA_DIR / "customers.json"
ADMIN_ACCESS_FILE = DATA_DIR / "admin_access.json"
WPLUS_DAILY_LIMIT = 6
WPLUS_MONTHLY_REFUND_LIMIT = 3
_ORDER_PERSIST_LOCK = threading.RLock()
_ORDER_LOCK_WARN_SECONDS = 1.0


@contextmanager
def _order_persist_guard(
    timing: Optional[dict[str, float]] = None,
    operation: str = "order_write",
) -> Iterator[None]:
    """Own the process and POSIX file locks for exactly one write transaction.

    Callers must use the ``*_unlocked`` helpers while inside this context. In
    particular, a batch rebuild must not call ``_sync_order_index`` here because
    that public helper owns this same file lock.
    """
    wait_started = time.perf_counter()
    lock_handle = None
    with _ORDER_PERSIST_LOCK:
        try:
            if fcntl is not None:
                lock_path = ORDERS_FILE.with_name(f".{ORDERS_FILE.name}.lock")
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                lock_handle = lock_path.open("a+", encoding="utf-8")
                warning_logged = False
                while True:
                    try:
                        lock_flags = fcntl.LOCK_EX | getattr(fcntl, "LOCK_NB", 0)
                        fcntl.flock(lock_handle.fileno(), lock_flags)
                        break
                    except (BlockingIOError, OSError) as exc:
                        retryable_errno = getattr(exc, "errno", None) in (errno.EACCES, errno.EAGAIN)
                        if not isinstance(exc, BlockingIOError) and not retryable_errno:
                            raise
                        lock_wait_ms = (time.perf_counter() - wait_started) * 1000
                        if not warning_logged and lock_wait_ms >= _ORDER_LOCK_WARN_SECONDS * 1000:
                            print(f"[order-lock] warning operation={operation} waiting_ms={lock_wait_ms:.2f}")
                            warning_logged = True
                        time.sleep(0.01)
            lock_wait_ms = (time.perf_counter() - wait_started) * 1000
            if timing is not None:
                timing["order_lock_wait"] = lock_wait_ms
            if lock_wait_ms >= _ORDER_LOCK_WARN_SECONDS * 1000:
                print(f"[order-lock] warning operation={operation} wait_ms={lock_wait_ms:.2f}")
            hold_started = time.perf_counter()
            try:
                yield
            finally:
                hold_ms = (time.perf_counter() - hold_started) * 1000
                if timing is not None:
                    timing["order_lock_hold"] = hold_ms
                if hold_ms >= _ORDER_LOCK_WARN_SECONDS * 1000:
                    print(f"[order-lock] warning operation={operation} hold_ms={hold_ms:.2f}")
        finally:
            if lock_handle is not None:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                lock_handle.close()


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def _load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    # os.replace gives POSIX readers an old-or-new complete file. Windows can
    # briefly reject replace while a reader still has the old handle open, so
    # retry that platform-specific sharing race without taking a write lock.
    for attempt in range(5):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.002)
    return []  # pragma: no cover - loop either returns or raises


def _save(path: Path, data: list[dict]):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _today_cn() -> str:
    tz = dt.timezone(dt.timedelta(hours=8))
    return dt.datetime.now(tz).strftime("%Y-%m-%d")


def _clean_phone(phone: object) -> str:
    return "".join(ch for ch in str(phone or "") if ch.isdigit())


def get_wplus_usage_events() -> list[dict]:
    return _load(WPLUS_USAGE_FILE)


def get_wplus_usage_summary(phone: str, date: Optional[str] = None, daily_limit: int = WPLUS_DAILY_LIMIT) -> dict:
    usage_date = date or _today_cn()
    clean_phone = _clean_phone(phone)
    events = [
        item for item in get_wplus_usage_events()
        if str(item.get("phone") or "") == clean_phone and str(item.get("date") or "") == usage_date
    ]
    net_used = sum(int(item.get("delta_used") or 0) for item in events)
    used = max(0, net_used)
    limit = max(0, int(daily_limit or WPLUS_DAILY_LIMIT))
    return {
        "date": usage_date,
        "daily_limit": limit,
        "used": used,
        "remaining": max(0, limit - used),
        "source": "local_fixed_daily",
    }


def record_wplus_usage(
    phone: str,
    order_id: str,
    quantity: int,
    action: str,
    date: Optional[str] = None,
    daily_limit: int = WPLUS_DAILY_LIMIT,
) -> dict:
    clean_phone = _clean_phone(phone)
    clean_order_id = str(order_id or "").strip()
    if not clean_phone or not clean_order_id:
        return {"recorded": False, "reason": "missing_phone_or_order", **get_wplus_usage_summary(clean_phone, date, daily_limit)}
    qty = max(1, int(quantity or 1))
    usage_date = date or _today_cn()
    if action not in {"use", "refund"}:
        raise ValueError("invalid wplus usage action")

    delta_used = qty if action == "use" else -qty
    key = f"{usage_date}:{clean_phone}:{clean_order_id}:{action}"
    events = get_wplus_usage_events()
    for item in events:
        if str(item.get("key") or "") == key:
            return {"recorded": False, "event": item, **get_wplus_usage_summary(clean_phone, usage_date, daily_limit)}

    event = {
        "key": key,
        "date": usage_date,
        "phone": clean_phone,
        "order_id": clean_order_id,
        "action": action,
        "quantity": qty,
        "delta_used": delta_used,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    events.append(event)
    _save(WPLUS_USAGE_FILE, events)
    return {"recorded": True, "event": event, **get_wplus_usage_summary(clean_phone, usage_date, daily_limit)}


def _month_cn() -> str:
    tz = dt.timezone(dt.timedelta(hours=8))
    return dt.datetime.now(tz).strftime("%Y-%m")


def get_refund_quota_summary(phone: str, month: Optional[str] = None, monthly_limit: int = WPLUS_MONTHLY_REFUND_LIMIT) -> dict:
    clean_phone = _clean_phone(phone)
    quota_month = month or _month_cn()
    entries = _load(REFUND_QUOTA_FILE)
    entry = next((item for item in entries if item.get("phone") == clean_phone and item.get("month") == quota_month), {})
    used = max(0, int(entry.get("used") or 0))
    limit = max(0, int(entry.get("total") or monthly_limit))
    calculated = max(0, limit - used)
    observed = entry.get("observed_remaining")
    remaining = max(0, int(observed)) if observed is not None else calculated
    return {"month": quota_month, "total": limit, "used": used, "remaining": remaining, "calculated_remaining": calculated, "source": "wanda_observed" if observed is not None else "local_monthly"}


def observe_refund_quota(phone: str, remaining: object, total: object = None, month: Optional[str] = None) -> dict:
    clean_phone = _clean_phone(phone)
    quota_month = month or _month_cn()
    entries = _load(REFUND_QUOTA_FILE)
    entry = next((item for item in entries if item.get("phone") == clean_phone and item.get("month") == quota_month), None)
    if entry is None:
        entry = {"phone": clean_phone, "month": quota_month, "used": 0, "total": WPLUS_MONTHLY_REFUND_LIMIT}
        entries.append(entry)
    if total is not None:
        try: entry["total"] = max(0, int(total))
        except (TypeError, ValueError): pass
    try:
        entry["observed_remaining"] = max(0, int(remaining))
        entry["observed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError): pass
    _save(REFUND_QUOTA_FILE, entries)
    return get_refund_quota_summary(clean_phone, quota_month)


def get_coupon_notes() -> dict[str, str]:
    try:
        value = json.loads(COUPON_NOTES_FILE.read_text(encoding="utf-8")) if COUPON_NOTES_FILE.exists() else {}
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def set_coupon_note(note_id: str, note: str) -> None:
    notes = get_coupon_notes()
    notes[str(note_id)] = str(note).strip()[:200]
    COUPON_NOTES_FILE.write_text(json.dumps(notes, ensure_ascii=False, indent=2), encoding="utf-8")


def get_customer_coupon_page(token: str) -> dict | None:
    try:
        pages = json.loads(CUSTOMER_COUPON_PAGES_FILE.read_text(encoding="utf-8")) if CUSTOMER_COUPON_PAGES_FILE.exists() else {}
        page = pages.get(str(token)) if isinstance(pages, dict) else None
        return page if isinstance(page, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def save_customer_coupon_page(token: str, page: dict) -> None:
    try:
        pages = json.loads(CUSTOMER_COUPON_PAGES_FILE.read_text(encoding="utf-8")) if CUSTOMER_COUPON_PAGES_FILE.exists() else {}
        if not isinstance(pages, dict):
            pages = {}
    except (OSError, json.JSONDecodeError):
        pages = {}
    pages[str(token)] = page
    CUSTOMER_COUPON_PAGES_FILE.write_text(json.dumps(pages, ensure_ascii=False, indent=2), encoding="utf-8")


# ── Shared workspace identities / ownership ledger ───────

def _password_hash(password: str, salt: str) -> str:
    return hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt), n=2**14, r=8, p=1).hex()


def _load_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _save_object(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, path)


def _password_record_matches(record: dict, password: str) -> bool:
    salt = str(record.get("password_salt") or "")
    expected = str(record.get("password_hash") or "")
    if not salt or not expected:
        return False
    try:
        actual = _password_hash(str(password or ""), salt)
    except (ValueError, TypeError):
        return False
    return secrets.compare_digest(actual, expected)


def _safe_operator(operator: dict) -> dict:
    return {key: value for key, value in operator.items() if key not in {"password_hash", "password_salt"}}


def get_operators() -> list[dict]:
    return [_safe_operator(item) for item in _load(OPERATORS_FILE) if item.get("active", True)]


def create_operator(username: str, password: str, display_name: str, customer_ids: list[str] | None = None) -> dict:
    normalized = str(username or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9_.-]{3,32}", normalized):
        raise ValueError("invalid username")
    if len(str(password or "")) < 12:
        raise ValueError("password must contain at least 12 characters")
    entries = _load(OPERATORS_FILE)
    if any(str(item.get("username") or "").lower() == normalized for item in entries):
        raise ValueError("username already exists")
    salt = secrets.token_hex(16)
    record = {
        "id": uuid.uuid4().hex,
        "username": normalized,
        "display_name": str(display_name or normalized).strip()[:80],
        "role": "operator",
        "customer_ids": sorted({str(item).strip() for item in (customer_ids or []) if str(item).strip()}),
        "password_salt": salt,
        "password_hash": _password_hash(str(password), salt),
        "active": True,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    entries.append(record)
    _save(OPERATORS_FILE, entries)
    return _safe_operator(record)


def verify_operator(username: str, password: str) -> Optional[dict]:
    normalized = str(username or "").strip().lower()
    for item in _load(OPERATORS_FILE):
        if not item.get("active", True) or str(item.get("username") or "").lower() != normalized:
            continue
        if _password_record_matches(item, password):
            return _safe_operator(item)
    return None


def change_operator_password(operator_id: str, current_password: str, new_password: str) -> bool:
    if len(str(new_password or "")) < 12:
        raise ValueError("password must contain at least 12 characters")
    entries = _load(OPERATORS_FILE)
    for item in entries:
        if str(item.get("id") or "") != str(operator_id) or not item.get("active", True):
            continue
        if not _password_record_matches(item, current_password):
            return False
        salt = secrets.token_hex(16)
        item.update({"password_salt": salt, "password_hash": _password_hash(str(new_password), salt), "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")})
        _save(OPERATORS_FILE, entries)
        return True
    return False


def update_operator_password(operator_id: str, password: str) -> bool:
    if len(str(password or "")) < 12:
        raise ValueError("password must contain at least 12 characters")
    entries = _load(OPERATORS_FILE)
    for item in entries:
        if str(item.get("id") or "") == str(operator_id) and item.get("active", True):
            salt = secrets.token_hex(16)
            item.update({"password_salt": salt, "password_hash": _password_hash(str(password), salt), "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")})
            _save(OPERATORS_FILE, entries)
            return True
    return False


def verify_admin_access_key(access_key: str, configured_key: str) -> bool:
    record = _load_object(ADMIN_ACCESS_FILE)
    if record and record.get("password_hash"):
        return _password_record_matches(record, access_key)
    return bool(configured_key) and secrets.compare_digest(str(access_key or ""), str(configured_key))


def change_admin_access_key(current_password: str, new_password: str, configured_key: str) -> bool:
    if len(str(new_password or "")) < 12:
        raise ValueError("password must contain at least 12 characters")
    if not verify_admin_access_key(current_password, configured_key):
        return False
    salt = secrets.token_hex(16)
    _save_object(ADMIN_ACCESS_FILE, {
        "password_salt": salt,
        "password_hash": _password_hash(str(new_password), salt),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    return True


def delete_operator(operator_id: str) -> bool:
    entries = _load(OPERATORS_FILE)
    kept = [item for item in entries if str(item.get("id") or "") != str(operator_id)]
    if len(kept) == len(entries):
        return False
    _save(OPERATORS_FILE, kept)
    return True


def operator_can_access_customer(operator: dict | None, customer_id: str) -> bool:
    if not operator:
        return False
    if operator.get("role") == "admin":
        return True
    return str(customer_id) in {str(item) for item in operator.get("customer_ids") or []}


def get_customers() -> list[dict]:
    return _load(CUSTOMERS_FILE)


def create_customer(name: str, contact_name: str = "", contact_phone: str = "") -> dict:
    clean_name = str(name or "").strip()
    if not clean_name or len(clean_name) > 80:
        raise ValueError("invalid customer name")
    entries = _load(CUSTOMERS_FILE)
    record = {
        "id": uuid.uuid4().hex,
        "name": clean_name,
        "contact_name": str(contact_name or "").strip()[:80],
        "contact_phone": _clean_phone(contact_phone)[:20],
        "active": True,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    entries.append(record)
    _save(CUSTOMERS_FILE, entries)
    return record


def _coupon_fingerprint(coupon_code: str) -> str:
    return hashlib.sha256(str(coupon_code or "").strip().encode("utf-8")).hexdigest()


def get_coupon_inventory() -> list[dict]:
    return _load(COUPON_LEDGER_FILE)


def record_coupon_inventory(
    coupon_code: str,
    account_phone: str,
    owner_customer_id: str,
    created_by: str,
    purchase_ref: str = "",
    created_by_name: str = "",
    coupon_name: str = "",
) -> dict:
    fingerprint = _coupon_fingerprint(coupon_code)
    if not fingerprint or not str(owner_customer_id or "").strip():
        raise ValueError("missing coupon owner")
    entries = get_coupon_inventory()
    existing = next((item for item in entries if item.get("fingerprint") == fingerprint), None)
    if existing:
        # Binding retries must remain idempotent; never replace its ownership.
        return existing
    record = {
        "id": uuid.uuid4().hex,
        "fingerprint": fingerprint,
        "account_phone": _clean_phone(account_phone),
        "owner_customer_id": str(owner_customer_id).strip(),
        "created_by": str(created_by).strip() or "admin",
        "created_by_name": str(created_by_name).strip()[:80] or ("管理员" if str(created_by).strip() in {"", "admin"} else str(created_by).strip()[:80]),
        "purchase_ref": str(purchase_ref).strip()[:100],
        "coupon_name": str(coupon_name).strip()[:120],
        "status": "available",
        "reserved_order_id": "",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    entries.append(record)
    _save(COUPON_LEDGER_FILE, entries)
    return record


def reserve_coupon_inventory(coupon_id: str, customer_id: str, order_id: str) -> dict:
    entries = get_coupon_inventory()
    record = next((item for item in entries if item.get("id") == str(coupon_id)), None)
    if not record:
        return {"reserved": False, "reason": "not_found"}
    if record.get("owner_customer_id") != str(customer_id):
        return {"reserved": False, "reason": "not_owner"}
    if record.get("status") == "reserved" and record.get("reserved_order_id") == str(order_id):
        return {"reserved": True, "coupon": record}
    if record.get("status") != "available":
        return {"reserved": False, "reason": "unavailable"}
    record.update({"status": "reserved", "reserved_order_id": str(order_id), "reserved_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    _save(COUPON_LEDGER_FILE, entries)
    return {"reserved": True, "coupon": record}


def release_coupon_inventory(coupon_id: str, order_id: str) -> dict:
    entries = get_coupon_inventory()
    record = next((item for item in entries if item.get("id") == str(coupon_id)), None)
    if not record or record.get("status") != "reserved" or record.get("reserved_order_id") != str(order_id):
        return {"released": False}
    record.update({"status": "available", "reserved_order_id": "", "released_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    _save(COUPON_LEDGER_FILE, entries)
    return {"released": True, "coupon": record}


def acquire_account_lease(phone: str, order_id: str, operator_id: str, ttl_seconds: int = 900) -> dict:
    now = int(time.time())
    entries = [item for item in _load(ACCOUNT_LEASES_FILE) if int(item.get("expires_at") or 0) > now]
    clean_phone = _clean_phone(phone)
    current = next((item for item in entries if item.get("phone") == clean_phone), None)
    if current and (current.get("order_id") != str(order_id) or current.get("operator_id") != str(operator_id)):
        return {"acquired": False, "reason": "account_busy", "lease": current}
    lease = current or {"id": uuid.uuid4().hex, "phone": clean_phone, "order_id": str(order_id), "operator_id": str(operator_id)}
    lease.update({"expires_at": now + max(60, min(int(ttl_seconds), 1800)), "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    if not current:
        entries.append(lease)
    _save(ACCOUNT_LEASES_FILE, entries)
    return {"acquired": True, "lease": lease}


def release_account_lease(phone: str, order_id: str) -> bool:
    clean_phone = _clean_phone(phone)
    entries = _load(ACCOUNT_LEASES_FILE)
    kept = [item for item in entries if not (item.get("phone") == clean_phone and item.get("order_id") == str(order_id))]
    if len(kept) == len(entries):
        return False
    _save(ACCOUNT_LEASES_FILE, kept)
    return True


def get_cola_auto_claim_state() -> dict:
    try:
        value = json.loads(COLA_AUTO_CLAIM_FILE.read_text(encoding="utf-8")) if COLA_AUTO_CLAIM_FILE.exists() else {}
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_cola_auto_claim_state(state: dict) -> None:
    COLA_AUTO_CLAIM_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 账号 ──────────────────────────────────────────────────

def _is_known_offline(account: dict) -> bool:
    msg = str(account.get("login_check_msg") or "").lower()
    offline_markers = (
        "未登录",
        "尚未登录",
        "missing token",
        "login invalid",
        "token invalid",
    )
    return not account.get("token") or any(marker in msg for marker in offline_markers)


def get_accounts() -> list[dict]:
    accounts = _load(ACCOUNTS_FILE)
    for account in accounts:
        if _is_known_offline(account):
            account["status"] = "offline"
    return accounts


def get_account_by_phone(phone: str) -> Optional[dict]:
    for a in get_accounts():
        if a["phone"] == phone:
            return a
    return None


def upsert_account(phone: str, token: str, user_info: Optional[dict] = None):
    accounts = get_accounts()
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for a in accounts:
        if a["phone"] == phone:
            a["token"] = token
            a["login_time"] = now
            a["status"] = "online"
            if user_info:
                a["user_info"] = user_info
            _save(ACCOUNTS_FILE, accounts)
            return a
    new_account = {
        "id": len(accounts) + 1,
        "phone": phone,
        "token": token,
        "status": "online",
        "login_time": now,
        "user_info": user_info or {},
    }
    accounts.append(new_account)
    _save(ACCOUNTS_FILE, accounts)
    return new_account


def update_account(phone: str, data: dict) -> bool:
    """更新账号任意字段 (备注 等)"""
    accounts = get_accounts()
    for a in accounts:
        if a["phone"] == phone:
            a.update(data)
            _save(ACCOUNTS_FILE, accounts)
            return True
    return False


def delete_account(phone: str) -> bool:
    accounts = get_accounts()
    before = len(accounts)
    accounts = [a for a in accounts if a["phone"] != phone]
    if len(accounts) < before:
        _save(ACCOUNTS_FILE, accounts)
        return True
    return False


def reorder_accounts(phones: list[str]) -> list[dict]:
    """按 phones 顺序重排账号列表"""
    accounts = get_accounts()
    by_phone = {a["phone"]: a for a in accounts}
    ordered = [by_phone[p] for p in phones if p in by_phone]
    remaining = [a for a in accounts if a["phone"] not in set(phones)]
    result = ordered + remaining
    _save(ACCOUNTS_FILE, result)
    return result


# ── 订单 ──────────────────────────────────────────────────


def _order_db_path() -> Path:
    return DATA_DIR / "orders.sqlite"


def _order_db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_order_db_path(), timeout=30, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL DEFAULT '',
            created_date TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '',
            phone TEXT NOT NULL DEFAULT '',
            data TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_orders_created_date ON orders(created_date);
        CREATE INDEX IF NOT EXISTS idx_orders_status_created ON orders(status, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_orders_phone_created ON orders(phone, created_at DESC);
    """)
    return conn


def _order_source_stamp() -> str:
    try:
        stat = ORDERS_FILE.stat()
        return f"{stat.st_mtime_ns}:{stat.st_size}"
    except OSError:
        return "missing"


def _sync_order_index(orders: list[dict]) -> None:
    """Rebuild the projection while owning the single transaction lock."""
    with _order_persist_guard(operation="index_rebuild"):
        _sync_order_index_unlocked(orders)


def _rebuild_order_index_from_json() -> None:
    """Read the authoritative JSON while owning the rebuild transaction lock."""
    with _order_persist_guard(operation="index_rebuild"):
        _sync_order_index_unlocked(_load(ORDERS_FILE))


def _sync_order_index_unlocked(orders: list[dict]) -> None:
    """Keep a queryable SQLite projection beside the legacy JSON source."""
    try:
        with _order_db_connect() as conn:
            conn.execute("DELETE FROM orders")
            rows = []
            for order in orders:
                order_id = str(order.get("id") or order.get("order_id") or "")
                if not order_id:
                    continue
                created_at = str(order.get("created_at") or "")
                rows.append((
                    order_id, created_at, created_at[:10], str(order.get("status") or ""),
                    str(order.get("phone") or ""), json.dumps(order, ensure_ascii=False, separators=(",", ":")),
                ))
            conn.executemany("INSERT OR REPLACE INTO orders(order_id, created_at, created_date, status, phone, data) VALUES(?, ?, ?, ?, ?, ?)", rows)
            conn.execute("CREATE TABLE IF NOT EXISTS order_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute("INSERT OR REPLACE INTO order_meta(key, value) VALUES('source_stamp', ?)", (_order_source_stamp(),))
    except (OSError, sqlite3.Error) as exc:
        print(f"[order-index] sync failed: {exc}")


def _upsert_order_index(
    order: dict,
    encoded_order: str,
    expected_source_stamp: str,
    new_source_stamp: str,
) -> bool:
    """Upsert one JSON-authoritative order into the SQLite projection."""
    try:
        order_id = str(order.get("id") or order.get("order_id") or "")
        if not order_id:
            return True
        created_at = str(order.get("created_at") or "")
        with _order_db_connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS order_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            row = conn.execute("SELECT value FROM order_meta WHERE key = 'source_stamp'").fetchone()
            projection_was_current = bool(row and str(row["value"]) == expected_source_stamp)
            conn.execute(
                """
                INSERT INTO orders(order_id, created_at, created_date, status, phone, data)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(order_id) DO UPDATE SET
                    created_at = excluded.created_at,
                    created_date = excluded.created_date,
                    status = excluded.status,
                    phone = excluded.phone,
                    data = excluded.data
                """,
                (
                    order_id,
                    created_at,
                    created_at[:10],
                    str(order.get("status") or ""),
                    str(order.get("phone") or ""),
                    encoded_order,
                ),
            )
            # The source stamp proves that the projection is complete only when
            # it was current before this atomic JSON mutation. If it was stale or
            # absent, leave it stale so query_orders will reconcile from JSON.
            if projection_was_current:
                conn.execute(
                    "INSERT OR REPLACE INTO order_meta(key, value) VALUES('source_stamp', ?)",
                    (new_source_stamp,),
                )
        return True
    except (OSError, sqlite3.Error) as exc:
        print(f"[order-index] incremental upsert failed: {exc}")
        return False


def _atomic_write_order_json(encoded_orders: str) -> None:
    """Write orders JSON via same-directory replace so readers never see partial JSON."""
    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=ORDERS_FILE.parent,
            prefix=f".{ORDERS_FILE.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = handle.name
            handle.write(encoded_orders)
            handle.flush()
            os.fsync(handle.fileno())
        # A Windows reader may briefly retain the old file handle. Retry only
        # that sharing race; never bypass the transaction lock or write in place.
        for attempt in range(20):
            try:
                os.replace(temp_path, ORDERS_FILE)
                temp_path = None
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.005)
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass


def _ensure_order_index() -> None:
    with _order_db_connect() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS order_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        row = conn.execute("SELECT value FROM order_meta WHERE key = 'source_stamp'").fetchone()
        current = _order_source_stamp()
        if row and str(row["value"]) == current:
            return
    _rebuild_order_index_from_json()


def _order_index_is_current() -> bool:
    try:
        with _order_db_connect() as conn:
            row = conn.execute("SELECT value FROM order_meta WHERE key = 'source_stamp'").fetchone()
            return bool(row and str(row["value"]) == _order_source_stamp())
    except (OSError, sqlite3.Error):
        return False


def _report_order_index_fallback(reason: str) -> None:
    print(f"[order-index] read fallback reason={reason}")


def _query_orders_sql(
    *, phone: str = "", status: str = "", order_date: str = "", limit: int = 0, offset: int = 0,
    exclude_invalid: bool = False,
) -> tuple[list[dict], int]:
    clauses: list[str] = []
    params: list[str] = []
    if phone:
        clauses.append("phone = ?"); params.append(str(phone))
    if status:
        clauses.append("status = ?"); params.append(str(status))
    if order_date:
        clauses.append("created_date = ?"); params.append(str(order_date))
    if exclude_invalid and not status:
        clauses.append("status NOT IN ('cancelled', 'refunded', 'failed')")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with _order_db_connect() as conn:
        total = int(conn.execute(f"SELECT COUNT(*) FROM orders{where}", params).fetchone()[0])
        sql = f"SELECT data FROM orders{where} ORDER BY created_at DESC, order_id DESC"
        if limit:
            sql += " LIMIT ? OFFSET ?"
            rows = conn.execute(sql, [*params, int(limit), max(0, int(offset))]).fetchall()
        else:
            rows = conn.execute(sql, params).fetchall()
    result = []
    for row in rows:
        try:
            value = json.loads(row["data"])
            if isinstance(value, dict):
                result.append(value)
        except (TypeError, json.JSONDecodeError):
            continue
    return result, total


def query_orders_current(
    *, phone: str = "", status: str = "", order_date: str = "", limit: int = 0, offset: int = 0,
    exclude_invalid: bool = False,
) -> Optional[tuple[list[dict], int]]:
    """Read the projection only when its source stamp is current; never rebuild on a request path."""
    if not _order_index_is_current():
        _report_order_index_fallback("stale_or_unavailable")
        return None
    try:
        return _query_orders_sql(
            phone=phone, status=status, order_date=order_date, limit=limit, offset=offset,
            exclude_invalid=exclude_invalid,
        )
    except (OSError, sqlite3.Error, TypeError, json.JSONDecodeError):
        _report_order_index_fallback("read_error")
        return None


def query_orders(
    *, phone: str = "", status: str = "", order_date: str = "", limit: int = 0, offset: int = 0,
    exclude_invalid: bool = False,
) -> tuple[list[dict], int]:
    """Query the indexed order projection; filters and pagination run in SQLite."""
    _ensure_order_index()
    return _query_orders_sql(
        phone=phone, status=status, order_date=order_date, limit=limit, offset=offset,
        exclude_invalid=exclude_invalid,
    )


def get_order_by_id(order_id: str) -> Optional[dict]:
    """Read one projected order without rebuilding a stale projection in the request thread."""
    clean_id = str(order_id or "").strip()
    if not clean_id:
        return None
    if not _order_index_is_current():
        _report_order_index_fallback("stale_or_unavailable")
        return None
    try:
        with _order_db_connect() as conn:
            row = conn.execute("SELECT data FROM orders WHERE order_id = ?", (clean_id,)).fetchone()
        if not row:
            return None
        value = json.loads(row["data"])
        return value if isinstance(value, dict) else None
    except (OSError, sqlite3.Error, TypeError, json.JSONDecodeError):
        _report_order_index_fallback("read_error")
        return None


def get_orders() -> list[dict]:
    orders = _load(ORDERS_FILE)
    orders.sort(key=lambda o: o.get("created_at") or "", reverse=True)
    return orders


ORIGINAL_GET_ORDERS = get_orders


def save_order(order: dict, timing: Optional[dict[str, float]] = None):
    """Persist one order and incrementally maintain its SQLite projection.

    orders.json remains authoritative. The projection is updated only after the
    JSON replace succeeds; if SQLite is unavailable, the JSON order remains
    durable and the stale source stamp makes the next read reconcile it.
    """
    total_started = time.perf_counter()
    with _order_persist_guard(timing=timing, operation="save_order"):
        load_started = time.perf_counter()
        orders = _load(ORDERS_FILE)
        if timing is not None:
            timing["persist_load_json"] = (time.perf_counter() - load_started) * 1000

        mutate_started = time.perf_counter()
        oid = str(order.get("id") or order.get("order_id") or "")
        saved_order = order
        if oid:
            for i, existing in enumerate(orders):
                if str(existing.get("id") or existing.get("order_id") or "") == oid:
                    existing.update(order)
                    saved_order = existing
                    break
            else:
                orders.insert(0, order)
        else:
            orders.insert(0, order)
        if timing is not None:
            timing["persist_mutate"] = (time.perf_counter() - mutate_started) * 1000

        encode_started = time.perf_counter()
        encoded_orders = json.dumps(orders, ensure_ascii=False, indent=2)
        if timing is not None:
            timing["persist_json_encode"] = (time.perf_counter() - encode_started) * 1000

        write_started = time.perf_counter()
        source_stamp_before = _order_source_stamp()
        _atomic_write_order_json(encoded_orders)
        source_stamp_after = _order_source_stamp()
        if timing is not None:
            timing["persist_write_file"] = (time.perf_counter() - write_started) * 1000

        sqlite_started = time.perf_counter()
        upsert_started = time.perf_counter()
        _upsert_order_index(saved_order, json.dumps(saved_order, ensure_ascii=False, separators=(",", ":")), source_stamp_before, source_stamp_after)
        if timing is not None:
            timing["persist_sqlite_upsert"] = (time.perf_counter() - upsert_started) * 1000
            timing["persist_sqlite"] = (time.perf_counter() - sqlite_started) * 1000

    # Measure after leaving the guard so total time includes lock teardown and
    # can never be reported below the lock-hold duration.
    if timing is not None:
        timing["persist_total"] = (time.perf_counter() - total_started) * 1000
    return saved_order


def save_orders_batch(new_orders: list[dict]):
    """批量追加订单并按时间倒序排列（去重），保留全量 rebuild 语义。"""
    with _order_persist_guard(operation="save_orders_batch"):
        orders = _load(ORDERS_FILE)
        existing_ids = {str(o.get("id") or o.get("order_id") or "") for o in orders}
        for no in new_orders:
            nid = str(no.get("id") or no.get("order_id") or "")
            if nid and nid in existing_ids:
                continue
            orders.append(no)
            existing_ids.add(nid)
        orders.sort(key=lambda o: o.get("created_at") or "", reverse=True)
        _atomic_write_order_json(json.dumps(orders, ensure_ascii=False, indent=2))
        _sync_order_index_unlocked(orders)


def update_orders_batch(updates: dict[str, dict]):
    """批量更新：updates = {order_id: data_dict}，保留全量 rebuild 语义。"""
    if not updates:
        return
    with _order_persist_guard(operation="update_orders_batch"):
        orders = _load(ORDERS_FILE)
        for existing in orders:
            oid = str(existing.get("id") or existing.get("order_id") or "")
            if oid in updates:
                existing.update(updates[oid])
        _atomic_write_order_json(json.dumps(orders, ensure_ascii=False, indent=2))
        _sync_order_index_unlocked(orders)


# ── 推广活动配置 ──────────────────────────────────────────

def get_spread_activities() -> list[dict]:
    return _load(ACTIVITIES_FILE)


def save_spread_activity(activity: dict) -> dict:
    """新增/更新一条活动配置；以 activity_code 为主键"""
    activities = _load(ACTIVITIES_FILE)
    code = activity.get("activity_code", "")
    for i, a in enumerate(activities):
        if a.get("activity_code") == code:
            activities[i] = {**a, **activity}
            _save(ACTIVITIES_FILE, activities)
            return activities[i]
    activity.setdefault("created_at", time.strftime("%Y-%m-%d %H:%M:%S"))
    activities.append(activity)
    _save(ACTIVITIES_FILE, activities)
    return activity


def delete_spread_activity(activity_code: str) -> bool:
    activities = _load(ACTIVITIES_FILE)
    before = len(activities)
    activities = [a for a in activities if a.get("activity_code") != activity_code]
    if len(activities) < before:
        _save(ACTIVITIES_FILE, activities)
        return True
    return False


def reorder_spread_activities(codes: list[str]) -> list[dict]:
    """按 codes 顺序重排活动列表"""
    activities = _load(ACTIVITIES_FILE)
    by_code = {a["activity_code"]: a for a in activities}
    ordered = [by_code[c] for c in codes if c in by_code]
    remaining = [a for a in activities if a["activity_code"] not in set(codes)]
    result = ordered + remaining
    _save(ACTIVITIES_FILE, result)
    return result


def update_order(order_id: str, data: dict) -> bool:
    """按 order_id 更新已有订单，并增量刷新对应 projection 行。"""
    with _order_persist_guard(operation="update_order"):
        orders = _load(ORDERS_FILE)
        for existing in orders:
            if str(existing.get("id")) == order_id or str(existing.get("order_id")) == order_id:
                existing.update(data)
                source_stamp_before = _order_source_stamp()
                _atomic_write_order_json(json.dumps(orders, ensure_ascii=False, indent=2))
                source_stamp_after = _order_source_stamp()
                _upsert_order_index(
                    existing,
                    json.dumps(existing, ensure_ascii=False, separators=(",", ":")),
                    source_stamp_before,
                    source_stamp_after,
                )
                return True
        return False


def get_lottery_activities() -> list[dict]:
    """获取所有抽奖活动配置"""
    return _load(LOTTERY_ACTIVITIES_FILE)


def save_lottery_activity(activity: dict) -> dict:
    """新增/更新抽奖活动配置；以 id 为主键"""
    activities = _load(LOTTERY_ACTIVITIES_FILE)
    activity_id = activity.get("id")
    for i, a in enumerate(activities):
        if a.get("id") == activity_id:
            activities[i] = {**a, **activity}
            _save(LOTTERY_ACTIVITIES_FILE, activities)
            return activities[i]
    activity.setdefault("created_at", time.strftime("%Y-%m-%d %H:%M:%S"))
    activities.append(activity)
    _save(LOTTERY_ACTIVITIES_FILE, activities)
    return activity


def delete_lottery_activity(activity_id: int) -> bool:
    """删除抽奖活动配置"""
    activities = _load(LOTTERY_ACTIVITIES_FILE)
    before = len(activities)
    activities = [a for a in activities if a.get("id") != activity_id]
    if len(activities) < before:
        _save(LOTTERY_ACTIVITIES_FILE, activities)
        return True
    return False
