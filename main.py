from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import sqlite3
import hashlib
import asyncio
import json
import os
import uuid
from datetime import datetime
from PIL import Image
import io
from g4f.client import Client
import uvicorn

app = FastAPI()

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode('utf-8')).hexdigest()

def init_db():
    conn = sqlite3.connect("chat.db")
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password TEXT,
            display_name TEXT,
            avatar_url TEXT,
            status TEXT,
            bio TEXT
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel TEXT,
            username TEXT,
            display_name TEXT,
            avatar_url TEXT,
            text TEXT,
            image_url TEXT,
            created_at TEXT
        )
    """)
    
    for col, col_type in [("image_url", "TEXT"), ("avatar_url", "TEXT"), ("status", "TEXT"), ("bio", "TEXT")]:
        try:
            cursor.execute(f"ALTER TABLE users ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass
            
    for col in ["image_url TEXT", "avatar_url TEXT"]:
        try:
            cursor.execute(f"ALTER TABLE messages ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass

    conn.commit()
    conn.close()

init_db()

active_sessions = {}

def ask_bot(question: str) -> str:
    try:
        client = Client()
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": question}],
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"Не удалось получить ответ от ИИ: {str(e)}"

async def broadcast_user_list(channel_name: str):
    users_in_channel = []
    seen_usernames = set()

    for session_id, user_data in active_sessions.items():
        if user_data.get("channel") == channel_name:
            uname = user_data["username"]
            if uname not in seen_usernames:
                users_in_channel.append({
                    "username": uname,
                    "display_name": user_data["display_name"],
                    "avatar_url": user_data.get("avatar_url", ""),
                    "status": user_data.get("status", ""),
                    "bio": user_data.get("bio", "")
                })
                seen_usernames.add(uname)
    
    if channel_name == "general" and "cyber_bot" not in seen_usernames:
        users_in_channel.append({
            "username": "cyber_bot",
            "display_name": "🤖 Кибер-Помощник",
            "avatar_url": "",
            "status": "Всегда на связи",
            "bio": "ИИ-ассистент в этом чате"
        })

    users_json = json.dumps(users_in_channel, ensure_ascii=False)

    for session_id, user_data in list(active_sessions.items()):
        if user_data.get("channel") == channel_name:
            try:
                await user_data["websocket"].send_text(f"__USERS__:{users_json}")
            except Exception:
                pass

@app.get("/")
def get_chat_page():
    with open("index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    ext = file.filename.split(".")[-1].lower() if "." in file.filename else "bin"
    
    allowed_exts = [
        "jpg", "jpeg", "png", "gif", "webp", 
        "pdf", "txt", "zip", "rar", "7z", "docx", "doc", 
        "xlsx", "xls", "mp3", "ogg", "wav", "py", "json", "js", "html", "css"
    ]
    if ext not in allowed_exts:
        raise HTTPException(status_code=400, detail="Формат файла не поддерживается")
    
    filename = f"{uuid.uuid4().hex}.{ext}"
    filepath = os.path.join(UPLOAD_DIR, filename)
    file_bytes = await file.read()
    
    if ext in ["jpg", "jpeg", "png", "gif", "webp"]:
        try:
            image = Image.open(io.BytesIO(file_bytes))
            if image.mode in ('RGBA', 'LA') or (image.mode == 'P' and 'transparency' in image.info):
                image = image.convert('RGBA')
                background = Image.new('RGBA', image.size, (0, 0, 0, 255))
                alpha_composite = Image.alpha_composite(background, image)
                alpha_composite.convert('RGB').save(filepath, format="PNG")
            else:
                with open(filepath, "wb") as f:
                    f.write(file_bytes)
        except Exception:
            with open(filepath, "wb") as f:
                f.write(file_bytes)
    else:
        with open(filepath, "wb") as f:
            f.write(file_bytes)
        
    return {
        "url": f"/uploads/{filename}",
        "filename": file.filename,
        "is_image": ext in ["jpg", "jpeg", "png", "gif", "webp"]
    }

@app.post("/api/register")
async def register(data: dict):
    init_db()
    username = data.get("username", "").strip().lower()
    password = data.get("password", "")
    display_name = data.get("display_name", "").strip() or username

    if not username or not password:
        return {"success": False, "message": "Заполните логин и пароль!"}

    conn = sqlite3.connect("chat.db")
    cursor = conn.cursor()
    cursor.execute("SELECT username FROM users WHERE username = ?", (username,))
    if cursor.fetchone():
        conn.close()
        return {"success": False, "message": "Имя пользователя уже занято!"}

    hashed = hash_password(password)
    cursor.execute("INSERT INTO users (username, password, display_name, avatar_url, status, bio) VALUES (?, ?, ?, ?, ?, ?)",
                   (username, hashed, display_name, "", "", ""))
    conn.commit()
    conn.close()
    return {"success": True, "username": username, "display_name": display_name, "avatar_url": "", "status": "", "bio": ""}

@app.post("/api/login")
async def login(data: dict):
    init_db()
    username = data.get("username", "").strip().lower()
    password = data.get("password", "")

    conn = sqlite3.connect("chat.db")
    cursor = conn.cursor()
    hashed = hash_password(password)
    cursor.execute("SELECT username, display_name, avatar_url, status, bio FROM users WHERE username = ? AND password = ?", (username, hashed))
    user = cursor.fetchone()
    conn.close()

    if user:
        return {
            "success": True, 
            "username": user[0], 
            "display_name": user[1], 
            "avatar_url": user[2] or "",
            "status": user[3] or "",
            "bio": user[4] or ""
        }
    return {"success": False, "message": "Неверный логин или пароль!"}

@app.post("/api/update_profile")
async def update_profile(data: dict):
    init_db()
    username = data.get("username")
    display_name = data.get("display_name")
    avatar_url = data.get("avatar_url")
    status = data.get("status", "")
    bio = data.get("bio", "")

    conn = sqlite3.connect("chat.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET display_name = ?, avatar_url = ?, status = ?, bio = ? WHERE username = ?",
                   (display_name, avatar_url, status, bio, username))
    conn.commit()
    conn.close()
    
    for sid, user_data in active_sessions.items():
        if user_data["username"] == username:
            user_data["display_name"] = display_name
            user_data["avatar_url"] = avatar_url
            user_data["status"] = status
            user_data["bio"] = bio

    for channel_name in set(u["channel"] for u in active_sessions.values() if u["username"] == username):
        await broadcast_user_list(channel_name)

    return {"success": True, "status": status, "bio": bio}

@app.get("/history/{channel_name}")
def get_history(channel_name: str):
    init_db()
    conn = sqlite3.connect("chat.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT username, display_name, avatar_url, text, image_url, created_at 
        FROM messages WHERE channel = ? ORDER BY id ASC
    """, (channel_name,))
    rows = cursor.fetchall()
    conn.close()
    return [{
        "username": row[0],
        "display_name": row[1],
        "avatar_url": row[2] or "",
        "text": row[3] or "",
        "image_url": row[4] or "",
        "timestamp": row[5]
    } for row in rows]

@app.websocket("/ws/{channel_name}/{session_id}/{username}")
async def websocket_endpoint(websocket: WebSocket, channel_name: str, session_id: str, username: str, status: str = ""):
    await websocket.accept()
    
    init_db()
    conn = sqlite3.connect("chat.db")
    cursor = conn.cursor()
    cursor.execute("SELECT display_name, avatar_url, status, bio FROM users WHERE username = ?", (username,))
    u_row = cursor.fetchone()
    conn.close()

    display_name = u_row[0] if u_row else username
    avatar_url = u_row[1] if u_row and u_row[1] else ""
    user_status = u_row[2] if u_row and u_row[2] else status
    user_bio = u_row[3] if u_row and u_row[3] else ""

    active_sessions[session_id] = {
        "username": username,
        "display_name": display_name,
        "avatar_url": avatar_url,
        "status": user_status,
        "bio": user_bio,
        "websocket": websocket,
        "channel": channel_name
    }
    
    await broadcast_user_list(channel_name)
    
    try:
        while True:
            raw_data = await websocket.receive_text()
            payload = json.loads(raw_data)
            
            msg_text = payload.get("text", "")
            img_url = payload.get("image_url", "")
            status_update = payload.get("status")
            
            if status_update is not None:
                active_sessions[session_id]["status"] = status_update
                await broadcast_user_list(channel_name)

            timestamp = datetime.now().isoformat()
            curr_disp = active_sessions[session_id]["display_name"]
            curr_avatar = active_sessions[session_id]["avatar_url"]

            if msg_text or img_url:
                conn = sqlite3.connect("chat.db")
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO messages (channel, username, display_name, avatar_url, text, image_url, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (channel_name, username, curr_disp, curr_avatar, msg_text, img_url, timestamp))
                conn.commit()
                conn.close()
                
                msg_out = json.dumps({
                    "username": username,
                    "display_name": curr_disp,
                    "avatar_url": curr_avatar,
                    "text": msg_text,
                    "image_url": img_url,
                    "timestamp": timestamp
                }, ensure_ascii=False)
                
                for sid, user_data in list(active_sessions.items()):
                    if user_data.get("channel") == channel_name:
                        try:
                            await user_data["websocket"].send_text(f"__MSG__:{msg_out}")
                        except Exception:
                            pass
                
                if msg_text.strip().startswith("/bot"):
                    question = msg_text.replace("/bot", "", 1).strip()
                    if question:
                        loop = asyncio.get_running_loop()
                        bot_response = await loop.run_in_executor(None, ask_bot, question)
                        bot_uname = "cyber_bot"
                        bot_disp = "🤖 Кибер-Помощник"
                        bot_timestamp = datetime.now().isoformat()
                        
                        conn = sqlite3.connect("chat.db")
                        cursor = conn.cursor()
                        cursor.execute("""
                            INSERT INTO messages (channel, username, display_name, avatar_url, text, image_url, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        """, (channel_name, bot_uname, bot_disp, "", bot_response, "", bot_timestamp))
                        conn.commit()
                        conn.close()
                        
                        bot_out = json.dumps({
                            "username": bot_uname,
                            "display_name": bot_disp,
                            "avatar_url": "",
                            "text": bot_response,
                            "image_url": "",
                            "timestamp": bot_timestamp
                        }, ensure_ascii=False)
                        
                        for sid, user_data in list(active_sessions.items()):
                            if user_data.get("channel") == channel_name:
                                try:
                                    await user_data["websocket"].send_text(f"__MSG__:{bot_out}")
                                except Exception:
                                    pass
                        
    except WebSocketDisconnect:
        if session_id in active_sessions:
            del active_sessions[session_id]
        await broadcast_user_list(channel_name)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)