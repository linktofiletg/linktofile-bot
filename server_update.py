"""
Callback endpoint added to server.py.
Receives RELEASE_RESULT from runner bot and forwards to user.
"""
import os
import json
import logging
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from config import FILES_DIR
from database import Database

logger = logging.getLogger(__name__)
db = Database()
app = FastAPI(title="VideoLinkBot Server")


@app.post("/runner-callback")
async def runner_callback(request: Request):
    """Receive result from runner bot."""
    body = await request.json()
    text = body.get("text", "")
    logger.info(f"Runner callback received: {text}")
    # Format: RELEASE_RESULT::filename::size::url
    if text.startswith("RELEASE_RESULT::"):
        parts = text.split("::")
        if len(parts) >= 4:
            filename = parts[1]
            size = parts[2]
            url = parts[3]
            # Import bot client to send to user
            # For now, just log — bot will pick this up via polling
            result = {
                "status": "ok",
                "filename": filename,
                "size": int(size),
                "download_url": url
            }
            logger.info(f"Release result: {result}")
            return JSONResponse(result)
    return JSONResponse({"status": "received", "text": text})


@app.get("/dl/{file_id}")
async def download_file(file_id: str):
    record = db.get_file(file_id)
    if not record:
        raise HTTPException(status_code=404, detail="File not found or expired")
    file_path = os.path.join(FILES_DIR, record["stored_name"])
    if not os.path.exists(file_path):
        db.mark_deleted(record["id"])
        raise HTTPException(status_code=404, detail="File not found on disk")
    return FileResponse(
        path=file_path,
        filename=record["original_name"],
        media_type=record["mime_type"] or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{record["original_name"]}"'}
    )


@app.get("/del/{delete_token}")
async def delete_file(delete_token: str):
    record = db.get_file_by_delete_token(delete_token)
    if not record:
        return HTMLResponse(content=HTML_NOT_FOUND, status_code=404)
    file_path = os.path.join(FILES_DIR, record["stored_name"])
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
    except Exception:
        pass
    db.mark_deleted(record["id"])
    return HTMLResponse(content=HTML_DELETED.format(name=record["original_name"]), status_code=200)


@app.get("/")
async def index():
    return HTMLResponse(content=HTML_INDEX)


HTML_INDEX = '<html><body><h1>VideoLinkBot</h1></body></html>'
HTML_DELETED = '<html><body><h1>Deleted: {name}</h1></body></html>'
HTML_NOT_FOUND = '<html><body><h1>Not Found</h1></body></html>'
