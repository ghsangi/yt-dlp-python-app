import os
import re
import sqlite3
import time
import requests
from fastapi import FastAPI, Request, Form, HTTPException, Depends, status
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import yt_dlp

app = FastAPI()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
security = HTTPBasic()

ADMIN_USERNAME = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASS", "SecretAdmin2026!")
PROXY_URL = os.getenv("PROXY_URL", "") 

YOUTUBE_REGEX = r'^(https?://)?(www\.)?(youtube\.com|youtu\.be)/.+$'
DB_FILE = os.path.join(BASE_DIR, "analytics.db")

fetch_ratelimit_store = {}
download_ratelimit_store = {}

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS download_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            video_title TEXT,
            format_quality TEXT,
            extension TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS blocklist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id TEXT UNIQUE
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS site_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    cursor.execute("INSERT OR IGNORE INTO site_settings (key, value) VALUES ('theme_color', 'red')")
    cursor.execute("INSERT OR IGNORE INTO site_settings (key, value) VALUES ('logo_text', 'StreamDrop')")
    cursor.execute("INSERT OR IGNORE INTO site_settings (key, value) VALUES ('redirect_url', '')")
    conn.commit()
    conn.close()

init_db()

def authenticate_admin(credentials: HTTPBasicCredentials = Depends(security)):
    if credentials.username != ADMIN_USERNAME or credentials.password != ADMIN_PASSWORD:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect admin credentials signature profile",
            headers={"WWW-Authenticate": "Basic"},
        )
    return True

def get_settings():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT key, value FROM site_settings")
    settings = dict(cursor.fetchall())
    conn.close()
    return settings

def check_rate_limit(client_ip: str, store: dict, max_requests: int, window_seconds: int = 60):
    current_time = time.time()
    if client_ip not in store:
        store[client_ip] = []
    store[client_ip] = [t for t in store[client_ip] if current_time - t < window_seconds]
    if len(store[client_ip]) >= max_requests:
        return False
    store[client_ip].append(current_time)
    return True

def extract_video_id(url: str) -> str:
    pattern = r'(?:v=|\/v\/|youtu\.be\/|\/embed\/|\/shorts\/)([a-zA-Z0-9_-]{11})'
    match = re.search(pattern, url)
    return match.group(1) if match else ""

def is_blocked(video_id: str) -> bool:
    if not video_id:
        return False
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM blocklist WHERE video_id = ?", (video_id,))
    blocked = cursor.fetchone() is not None
    conn.close()
    return blocked

@app.get("/")
async def get_index(request: Request):
    settings = get_settings()
    if settings.get("redirect_url"):
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url=settings["redirect_url"])
    return templates.TemplateResponse("index.html", {"request": request, "settings": settings})

@app.get("/admin")
async def get_admin_dashboard(request: Request, authenticated: bool = Depends(authenticate_admin)):
    settings = get_settings()
    return templates.TemplateResponse("admin.html", {"request": request, "settings": settings, "proxy_url": PROXY_URL})

@app.post("/fetch-info")
async def fetch_info(request: Request, url: str = Form(...)):
    client_ip = request.client.host or "unknown"
    if not check_rate_limit(client_ip, fetch_ratelimit_store, max_requests=5):
        raise HTTPException(status_code=429, detail="Too many search requests. Please wait a minute.")

    if not re.match(YOUTUBE_REGEX, url):
        raise HTTPException(status_code=400, detail="Invalid YouTube URL format.")
    
    video_id = extract_video_id(url)
    if is_blocked(video_id):
        raise HTTPException(status_code=403, detail="This content has been blocked by the network administrator.")
    
    ydl_opts = {'skip_download': True, 'quiet': True}
    if PROXY_URL:
        ydl_opts['proxy'] = PROXY_URL
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            raw_title = info.get('title') or info.get('playlist_title') or "video_download"
            safe_title = "".join([c for c in str(raw_title) if c.isalpha() or c.isdigit() or c==' ']).rstrip()
            if not safe_title:
                safe_title = "media_file"
            
            formats = []
            for f in info.get('formats', []):
                if f.get('ext') in ['mp4', 'm4a', 'mp3'] and f.get('url'):
                    if f.get('vcodec') != 'none' and f.get('acodec') != 'none':
                        quality = f"{f.get('height')}p (Video+Audio)"
                    elif f.get('vcodec') == 'none':
                        quality = f"{int(f.get('abr', 0))}kbps (Audio Only)"
                    else:
                        continue
                        
                    formats.append({
                        "format_id": f.get('format_id'),
                        "ext": f.get('ext'),
                        "quality": quality,
                        "stream_url": f.get('url') 
                    })
            
            if not formats:
                raise HTTPException(status_code=404, detail="No streaming formats found.")
                
            return JSONResponse({
                "title": str(raw_title),
                "safe_title": safe_title,
                "thumbnail": info.get('thumbnail', ''),
                "formats": formats
            })
    except Exception:
        raise HTTPException(status_code=500, detail="Error fetching details from structural stream layouts.")

@app.get("/download")
async def proxy_download(request: Request, url: str, filename: str, ext: str, quality: str):
    client_ip = request.client.host or "unknown"
    if not check_rate_limit(client_ip, download_ratelimit_store, max_requests=3):
        raise HTTPException(status_code=429, detail="Download limit exceeded. Please wait a minute.")

    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO download_logs (video_title, format_quality, extension) VALUES (?, ?, ?)",
            (filename, quality, ext)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Logging file error: {e}")

    proxies = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None

    try:
        response = requests.get(url, stream=True, timeout=45, proxies=proxies)
        response.raise_for_status()
        
        def stream_chunks():
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    yield chunk

        headers = {
            "Content-Disposition": f'attachment; filename="{filename}.{ext}"',
            "Content-Type": "application/octet-stream"
        }
        return StreamingResponse(stream_chunks(), headers=headers)
    except requests.exceptions.RequestException:
        raise HTTPException(status_code=500, detail="Streaming connection pipeline timed out.")

# --- ADMIN API INTERFACES ---

@app.get("/api/admin/stats")
async def get_admin_stats(authenticated: bool = Depends(authenticate_admin)):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM download_logs")
    total_downloads = cursor.fetchone()[0] # Unpack single value tuple safely
    
    cursor.execute("SELECT timestamp, video_title, format_quality, extension FROM download_logs ORDER BY id DESC LIMIT 15")
    rows = cursor.fetchall()
    
    cursor.execute("SELECT video_id FROM blocklist ORDER BY id DESC")
    blocked_rows = cursor.fetchall()
    blocklist = [r[0] for r in blocked_rows] # Unpack single value tuple safely
    
    logs = [{
        "timestamp": str(row[0]), 
        "title": str(row[1]), 
        "quality": str(row[2]), 
        "ext": str(row[3])
    } for row in rows]
    
    conn.close()
    return {"total_downloads": total_downloads, "recent_logs": logs, "blocklist": blocklist}

@app.post("/api/admin/update-settings")
async def update_settings(
    theme_color: str = Form(...), 
    logo_text: str = Form(...), 
    redirect_url: str = Form(""), 
    authenticated: bool = Depends(authenticate_admin)
):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE site_settings SET value = ? WHERE key = 'theme_color'", (theme_color,))
    cursor.execute("UPDATE site_settings SET value = ? WHERE key = 'logo_text'", (logo_text,))
    cursor.execute("UPDATE site_settings SET value = ? WHERE key = 'redirect_url'", (redirect_url.strip(),))
    conn.commit()
    conn.close()
    return {"status": "success"}

@app.post("/api/admin/clear-logs")
async def clear_logs(authenticated: bool = Depends(authenticate_admin)):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM download_logs")
    conn.commit()
    conn.close()
    return {"status": "success"}

@app.post("/api/admin/block")
async def block_video(url_or_id: str = Form(...), authenticated: bool = Depends(authenticate_admin)):
