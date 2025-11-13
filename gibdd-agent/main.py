import asyncio
import hashlib
import json
import os
from datetime import datetime
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import aiosqlite
import aiohttp
import aiosmtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv

load_dotenv()

import os
DB_PATH = os.getenv("RAILWAY_VOLUME_PATH", "vehicles.db")
API_URL = "https://shtrafy-gibdd.ru/api/v1/fines"  # Убраны лишние пробелы
EMAIL_LOGIN = os.getenv("EMAIL_LOGIN")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
CHECK_INTERVAL_HOURS = int(os.getenv("CHECK_INTERVAL_HOURS", 24))
REQUEST_DELAY_SEC = float(os.getenv("REQUEST_DELAY_SEC", 6.5))

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


def hash_fines(fines):
    sorted_fines = sorted(fines, key=lambda x: (x.get("date", ""), x.get("sum", 0)))
    return hashlib.md5(json.dumps(sorted_fines, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


async def send_email(to_email: str, subject: str, body: str):
    if not EMAIL_LOGIN or not EMAIL_PASSWORD:
        print("Email credentials not set")
        return
    try:
        msg = MIMEMultipart()
        msg["From"] = EMAIL_LOGIN
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))
        await aiosmtplib.send(
            message=msg,
            hostname=SMTP_HOST,
            port=SMTP_PORT,
            start_tls=True,
            username=EMAIL_LOGIN,
            password=EMAIL_PASSWORD,
        )
    except Exception as e:
        print(f"Email error: {e}")


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        # Таблица авто
        await db.execute("""
            CREATE TABLE IF NOT EXISTS vehicles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                car_number TEXT NOT NULL,
                sts_number TEXT NOT NULL,
                email TEXT NOT NULL,
                email2 TEXT,               -- ← добавлено
                description TEXT,
                last_fines_hash TEXT,
                last_check TEXT
            )
        """)

        # Таблица штрафов
        await db.execute("""
            CREATE TABLE IF NOT EXISTS fines_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vehicle_id INTEGER NOT NULL,
                fine_date TEXT NOT NULL,
                fine_sum INTEGER NOT NULL,
                description TEXT,
                photo_url TEXT,
                document_url TEXT,         -- ← для постановлений
                fine_hash TEXT UNIQUE NOT NULL,
                detected_at TEXT NOT NULL,
                sent BOOLEAN DEFAULT 0,
                is_paid BOOLEAN DEFAULT 0,
                paid_at TEXT,
                uin TEXT,
                kbk TEXT,
                oktmo TEXT,
                payment_name TEXT,
                payment_account TEXT,
                payment_bic TEXT
            )
        """)

        # === Безопасное добавление столбцов (если их нет) ===
        # Для vehicles
        try:
            await db.execute("ALTER TABLE vehicles ADD COLUMN email2 TEXT")
        except aiosqlite.OperationalError as e:
            if "duplicate column name" not in str(e):
                raise

        # Для fines_history
        try:
            await db.execute("ALTER TABLE fines_history ADD COLUMN document_url TEXT")
        except aiosqlite.OperationalError as e:
            if "duplicate column name" not in str(e):
                raise

        await db.commit()


async def check_all_vehicles():
    print("🔍 Запуск проверки...")
    async with aiosqlite.connect(DB_PATH) as db_read:
        async with db_read.execute("SELECT id, car_number, sts_number, email, email2, description FROM vehicles") as cur:
            vehicles = await cur.fetchall()

    # Заголовки — внутри функции!
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    }

    # ClientSession — тоже внутри!
    async with aiohttp.ClientSession(headers=headers) as session:
        for vid, car, sts, email, email2, desc in vehicles:
            try:
                async with session.get(f"{API_URL}?number={car}&sts={sts}") as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        api_fines = data.get("fines", [])
                        new_fines = []

                        # Получаем все сохранённые штрафы для этого авто
                        async with aiosqlite.connect(DB_PATH) as db_write:
                            async with db_write.execute(
                                "SELECT id, fine_hash, is_paid FROM fines_history WHERE vehicle_id = ?",
                                (vid,)
                            ) as cur_hashes:
                                local_fines = {row[1]: (row[0], row[2]) for row in await cur_hashes.fetchall()}

                            # Словарь хэшей штрафов из API (для быстрого поиска)
                            api_fine_hashes = set()

                            # Обрабатываем штрафы из API
                            for f in api_fines:
                                fine_key = f"{f['date']}|{f['sum']}|{f.get('description', '')}"
                                fine_hash = hashlib.md5(fine_key.encode()).hexdigest()
                                api_fine_hashes.add(fine_hash)

                                # Если штраф новый — добавляем
                                if fine_hash not in local_fines:
                                    # Извлекаем реквизиты (если есть)
                                    uin = f.get("uin") or f.get("bill_id") or ""
                                    kbk = f.get("kbk", "")
                                    oktmo = f.get("oktmo", "")
                                    payment_name = f.get("recipient_name", "УФК по региону")
                                    payment_account = f.get("account", "40101810800000010111")
                                    payment_bic = f.get("bic", "044525000")

                                    await db_write.execute("""
                                        INSERT INTO fines_history 
                                        (vehicle_id, fine_date, fine_sum, description, photo_url, fine_hash, detected_at, is_paid,
                                         uin, kbk, oktmo, payment_name, payment_account, payment_bic)
                                        VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)
                                    """, (
                                        vid,
                                        f["date"],
                                        f["sum"],
                                        f.get("description", ""),
                                        f.get("photo_url", ""),
                                        fine_hash,
                                        datetime.now().isoformat(),
                                        uin, kbk, oktmo, payment_name, payment_account, payment_bic
                                    ))
                                    new_fines.append(f)

                            # 🔍 Проверка оплаты: если штраф был в БД, но отсутствует в API → оплачен
                            paid_fines = []
                            for fine_hash, (local_id, local_is_paid) in local_fines.items():
                                if fine_hash not in api_fine_hashes and not local_is_paid:
                                    # Штраф исчез из API → помечаем как оплаченный
                                    await db_write.execute(
                                        "UPDATE fines_history SET is_paid = 1, paid_at = ? WHERE id = ?",
                                        (datetime.now().isoformat(), local_id)
                                    )
                                    # Получаем данные штрафа для уведомления
                                    async with db_write.execute(
                                        "SELECT fine_date, fine_sum FROM fines_history WHERE id = ?",
                                        (local_id,)
                                    ) as cur_fine:
                                        fine_data = await cur_fine.fetchone()
                                        if fine_data:
                                            paid_fines.append(fine_data)

                            await db_write.commit()

                        # Отправка уведомлений
                        if new_fines:
                            body = f"Авто: {car}\nОписание: {desc or '—'}\n\n🚨 НОВЫЕ ШТРАФЫ:\n\n"
                            for f in new_fines:
                                body += f"📅 {f['date']}\n💰 {f['sum']} ₽\n📝 {f['description']}\n"
                                if f.get("photo_url"):
                                    body += f"📸 {f['photo_url']}\n"
                                body += "—\n\n"
                            await send_email(email, f"Штрафы ГИБДД — {car}", body)

                        if paid_fines:
                            body = f"Авто: {car}\nОписание: {desc or '—'}\n\n✅ ОПЛАЧЕННЫЕ ШТРАФЫ:\n\n"
                            for date, amount in paid_fines:
                                body += f"📅 {date}\n💰 {amount} ₽\n—\n\n"
                            await send_email(email, f"Оплата штрафов — {car}", body)

                        # Обновляем last_fines_hash
                        all_fines_hash = hash_fines(api_fines)
                        async with aiosqlite.connect(DB_PATH) as db_update:
                            await db_update.execute(
                                "UPDATE vehicles SET last_fines_hash = ?, last_check = ? WHERE id = ?",
                                (all_fines_hash, datetime.now().isoformat(), vid)
                            )
                            await db_update.commit()

            except Exception as e:
                print(f"Ошибка при проверке {car}: {e}")
            await asyncio.sleep(REQUEST_DELAY_SEC)


@app.on_event("startup")
async def startup():
    os.makedirs("/data", exist_ok=True)
    await init_db()
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        check_all_vehicles,
        trigger=IntervalTrigger(hours=CHECK_INTERVAL_HOURS),
        id="gibdd_check",
        replace_existing=True
    )
    scheduler.start()


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    async with aiosqlite.connect(DB_PATH) as db:
        # Автомобили (с email2)
        async with db.execute("SELECT id, car_number, sts_number, email, email2, description FROM vehicles ORDER BY description") as cur:
            vehicles = await cur.fetchall()

        # Все штрафы с привязкой к авто (для вкладки "История")
        async with db.execute("""
            SELECT f.fine_date, f.fine_sum, f.description, f.is_paid, f.detected_at, f.id, v.car_number
            FROM fines_history f
            JOIN vehicles v ON f.vehicle_id = v.id
            ORDER BY f.detected_at DESC
        """) as cur:
            all_fines = await cur.fetchall()

        # Неоплаченные штрафы — ВКЛЮЧАЯ document_url
        async with db.execute("""
            SELECT f.fine_date, f.fine_sum, f.description, f.id, f.document_url, v.car_number
            FROM fines_history f
            JOIN vehicles v ON f.vehicle_id = v.id
            WHERE f.is_paid = 0
            ORDER BY f.fine_date DESC
        """) as cur:
            fines_unpaid = await cur.fetchall()

        # Оплаченные штрафы
        async with db.execute("""
            SELECT f.fine_date, f.fine_sum, f.description, f.id, v.car_number
            FROM fines_history f
            JOIN vehicles v ON f.vehicle_id = v.id
            WHERE f.is_paid = 1
            ORDER BY f.fine_date DESC
        """) as cur:
            fines_paid = await cur.fetchall()

    return templates.TemplateResponse("index.html", {
        "request": request,
        "vehicles": vehicles,
        "all_fines": all_fines,
        "fines_unpaid": fines_unpaid,
        "fines_paid": fines_paid
    })


@app.post("/add")
async def add_vehicle(
    car_number: str = Form(...),
    sts_number: str = Form(...),
    email: str = Form(...),
    description: str = Form("")
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO vehicles (car_number, sts_number, email, description) VALUES (?, ?, ?, ?)",
            (car_number.upper(), sts_number, email, description)
        )
        await db.commit()
    return RedirectResponse(url="/", status_code=303)


@app.post("/delete/{vid}")
async def delete_vehicle(vid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM vehicles WHERE id = ?", (vid,))
        await db.commit()
    return RedirectResponse(url="/", status_code=303)


@app.post("/check-now")
async def manual_check():
    asyncio.create_task(check_all_vehicles())
    return RedirectResponse(url="/?manual=1", status_code=303)
import qrcode
from io import BytesIO
import base64

@app.get("/pay/{fine_id}", response_class=HTMLResponse)
async def pay_fine(request: Request, fine_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        # Получаем данные штрафа
        async with db.execute("""
            SELECT f.fine_sum, f.description, f.fine_date, v.car_number
            FROM fines_history f
            JOIN vehicles v ON f.vehicle_id = v.id
            WHERE f.id = ?
        """, (fine_id,)) as cur:
            row = await cur.fetchone()
        
        if not row:
            raise HTTPException(status_code=404, detail="Штраф не найден")
        
        amount, description, date, car_number = row

        # Формируем строку для СБП (стандарт ЦБ РФ)
        # Формат: https://qr.cbr.ru/...?...&sum=...&name=...&comment=...
        # Используем фиктивные, но валидные данные получателя (в реальности — ИФНС или ГИБДД)
        # Для демо можно использовать тестовый счёт (например, "ГИБДД РФ")
        recipient_name = "ГИБДД РФ"
        recipient_account = "40101810100000000225"  # Условный счёт (для демо)
        bank_bic = "044525225"  # БИК ЦБ РФ (для демо)

        # Сумма в формате X.XX (обязательно!)
        sum_str = f"{amount}.00"

        # Комментарий
        comment = f"Штраф ГИБДД {car_number} от {date}"

        # URL для QR СБП (официальный)
        qr_url = (
            f"https://qr.cbr.ru/transfer?"
            f"sum={sum_str}&"
            f"name={recipient_name}&"
            f"comment={comment}&"
            f"account={recipient_account}&"
            f"bic={bank_bic}"
        )

        # Генерация QR-кода
        qr = qrcode.QRCode(version=1, box_size=10, border=4)
        qr.add_data(qr_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")

        # Конвертация в base64 для встраивания в HTML
        buffer = BytesIO()
        img.save(buffer, format="PNG")
        img_str = base64.b64encode(buffer.getvalue()).decode()

        return templates.TemplateResponse("pay.html", {
            "request": request,
            "fine_id": fine_id,
            "car_number": car_number,
            "amount": amount,
            "date": date,
            "description": description,
            "qr_data": f"data:image/png;base64,{img_str}",
            "qr_url": qr_url
        })
@app.get("/edit/{vid}", response_class=HTMLResponse)
async def edit_vehicle_form(request: Request, vid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, car_number, sts_number, email, description FROM vehicles WHERE id = ?",
            (vid,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Авто не найдено")
        
        vehicle = {
            "id": row[0],
            "car_number": row[1],
            "sts_number": row[2],
            "email": row[3],
            "description": row[4]
        }
        return templates.TemplateResponse("edit.html", {"request": request, "vehicle": vehicle})


@app.post("/edit/{vid}")
async def edit_vehicle(
    vid: int,
    car_number: str = Form(...),
    sts_number: str = Form(...),
    email: str = Form(...),
    description: str = Form("")
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE vehicles SET car_number = ?, sts_number = ?, email = ?, description = ? WHERE id = ?",
            (car_number.upper(), sts_number, email, description, vid)
        )
        await db.commit()
    return RedirectResponse(url="/", status_code=303)
@app.get("/history/{vid}", response_class=HTMLResponse)
async def vehicle_history(request: Request, vid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        # Получаем описание авто
        async with db.execute("SELECT car_number, description FROM vehicles WHERE id = ?", (vid,)) as cur:
            vehicle = await cur.fetchone()
        if not vehicle:
            raise HTTPException(status_code=404, detail="Авто не найдено")
        
        # ВАЖНО: добавляем id (первым или последним — здесь последним)
        async with db.execute("""
            SELECT fine_date, fine_sum, description, photo_url, detected_at, id 
            FROM fines_history 
            WHERE vehicle_id = ? 
            ORDER BY detected_at DESC
        """, (vid,)) as cur:
            fines = await cur.fetchall()
    
    return templates.TemplateResponse("history.html", {
        "request": request,
        "vehicle": vehicle,
        "fines": fines

    })
