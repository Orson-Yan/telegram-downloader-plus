"""web ui for media download"""

import datetime
import logging
import os
import threading

from flask import Flask, jsonify, render_template, request

import utils
from module.app import Application
from module.download_stat import (
    DownloadState,
    batch_delete_failed,
    batch_delete_tasks,
    delete_task,
    get_chat_title,
    get_download_result,
    get_download_state,
    get_failed_downloads,
    get_total_download_speed,
    is_task_paused,
    pause_task,
    remove_failed_download,
    resume_task,
    set_download_state,
)
from utils.format import format_byte

log = logging.getLogger("werkzeug")
log.setLevel(logging.ERROR)

_flask_app = Flask(__name__)

_flask_app.secret_key = "tdl"


def get_flask_app() -> Flask:
    """get flask app instance"""
    return _flask_app


def run_web_server(app: Application):
    """
    Runs a web server using the Flask framework.
    """
    get_flask_app().run(
        app.web_host, app.web_port, debug=app.debug_web, use_reloader=False
    )


# pylint: disable = W0603
def init_web(app: Application):
    """
    Initialize and start the web server.

    Args:
        app: The Application instance.

    Returns:
        None.
    """
    if app.debug_web:
        threading.Thread(target=run_web_server, args=(app,)).start()
    else:
        threading.Thread(
            target=get_flask_app().run, daemon=True, args=(app.web_host, app.web_port)
        ).start()


@_flask_app.route("/")
def index():
    """Index page - no login required"""
    return render_template("index.html")


@_flask_app.route("/get_download_status")
def get_download_speed():
    """Get download speed"""
    return jsonify(
        download_speed=format_byte(get_total_download_speed()) + "/s",
        upload_speed="0.00 B/s"
    )


@_flask_app.route("/get_download_state")
def web_get_download_state():
    """Get current download state"""
    state = get_download_state()
    if state is DownloadState.Downloading:
        return "downloading"
    return "paused"


@_flask_app.route("/set_download_state", methods=["POST"])
def web_set_download_state():
    """Set download state"""
    state = request.args.get("state")

    if state == "continue" and get_download_state() is DownloadState.StopDownload:
        set_download_state(DownloadState.Downloading)
        return "pause"

    if state == "pause" and get_download_state() is DownloadState.Downloading:
        set_download_state(DownloadState.StopDownload)
        return "continue"

    return state


@_flask_app.route("/get_app_version")
def get_app_version():
    """Get telegram_media_downloader version"""
    return utils.__version__


@_flask_app.route("/get_download_list")
def get_download_list():
    """Get download list with task_id and status"""
    if request.args.get("already_down") is None:
        return "[]"

    already_down = request.args.get("already_down") == "true"

    download_result = get_download_result()
    result = []
    for chat_id, messages in download_result.items():
        for idx, value in messages.items():
            is_already_down = value["down_byte"] == value["total_size"]

            if already_down and not is_already_down:
                continue
            if not already_down:
                # 在活跃列表中，过滤掉还没真正开始下载的条目
                # (down_byte==0 且 start_time==end_time 表示 Pyrogram 回调还没触发)
                if value["down_byte"] == 0 and value.get("start_time", 0) == value.get("end_time", 0):
                    continue

            progress = round(value["down_byte"] / value["total_size"] * 100, 1) if value["total_size"] > 0 else 0
            download_speed = format_byte(value["download_speed"]) + "/s"

            # ETA calculation
            eta = ""
            speed = value["download_speed"]
            if speed > 0 and not is_already_down:
                remaining = value["total_size"] - value["down_byte"]
                eta_seconds = int(remaining / speed)
                if eta_seconds >= 3600:
                    eta = f"{eta_seconds // 3600}h{(eta_seconds % 3600) // 60:02d}m"
                elif eta_seconds >= 60:
                    eta = f"{eta_seconds // 60}m{eta_seconds % 60:02d}s"
                else:
                    eta = f"{eta_seconds}s"

            # Internal key for operations (stable across restarts)
            task_id = f"{chat_id}_{idx}"

            # Determine status: completed, paused, or active
            if is_already_down:
                status = "completed"
            elif is_task_paused(task_id) or is_task_paused(value.get("task_id", "")):
                status = "paused"
            else:
                status = "active"

            # Get chat title from cache
            source_title = value.get("source_chat_title", "")
            if not source_title and value.get("source_chat_id"):
                source_title = get_chat_title(value["source_chat_id"]) or ""
            chat_title = source_title or get_chat_title(chat_id)

            # Format times
            end_time = value.get("end_time", 0)
            start_time = value.get("start_time", 0)
            completed_time = ""
            created_at = ""
            if start_time:
                created_at = datetime.datetime.fromtimestamp(start_time).strftime("%m-%d %H:%M:%S")
            if is_already_down:
                ts = end_time if end_time else start_time
                if ts:
                    completed_time = datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M:%S")

            result.append({
                "task_id": str(task_id),
                "chat": str(chat_id),
                "chat_title": chat_title,
                "id": str(idx),
                "filename": os.path.basename(value["file_name"]),
                "total_size": format_byte(value["total_size"]),
                "total_size_bytes": value["total_size"],
                "download_progress": str(progress),
                "download_progress_raw": progress,
                "download_speed": download_speed,
                "eta": eta,
                "save_path": value["file_name"].replace("\\", "/"),
                "status": status,
                "start_time": start_time,
                "end_time": end_time,
                "created_at": created_at,
                "completed_time": completed_time,
                "task_id_display": value.get("task_id_display", "") or task_id,
            })

    # Sort by time: active by start_time (newest first), completed by end_time (newest first)
    if already_down:
        result.sort(key=lambda x: x.get("end_time", 0) or x.get("start_time", 0), reverse=True)
    else:
        result.sort(key=lambda x: x.get("start_time", 0), reverse=True)

    return jsonify(result)


@_flask_app.route("/get_failed_downloads")
def web_get_failed_downloads():
    """Get list of failed downloads"""
    failed = get_failed_downloads()
    # Sort by failure timestamp, newest first
    failed = sorted(failed, key=lambda f: f.get("timestamp", 0), reverse=True)
    result = []
    for f in failed:
        chat_id = f.get("chat_id", "")
        chat_title = get_chat_title(chat_id)
        failed_time = ""
        ts = f.get("timestamp", 0)
        if ts:
            failed_time = datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M:%S")
        result.append({
            "task_id": str(f["task_id"]),
            "chat": str(chat_id),
            "chat_title": chat_title,
            "id": str(f.get("msg_id", "")),
            "filename": os.path.basename(f.get("file_name", "")),
            "error_message": f.get("error_message", "Unknown error"),
            "total_size": format_byte(f.get("total_size", 0)),
            "failed_time": failed_time,
            "source_link": f.get("source_link", ""),
        })
    return jsonify(result)


@_flask_app.route("/pause_task", methods=["POST"])
def web_pause_task():
    """Pause a specific download task"""
    task_id = request.args.get("task_id")
    if not task_id:
        return jsonify({"code": "0", "message": "task_id required"})
    pause_task(task_id)
    return jsonify({"code": "1", "message": "paused"})


@_flask_app.route("/resume_task", methods=["POST"])
def web_resume_task():
    """Resume a specific download task"""
    task_id = request.args.get("task_id")
    if not task_id:
        return jsonify({"code": "0", "message": "task_id required"})
    success = resume_task(task_id)
    if success:
        return jsonify({"code": "1", "message": "resumed"})
    return jsonify({"code": "0", "message": "task not paused"})


@_flask_app.route("/delete_task", methods=["POST"])
def web_delete_task():
    """Delete a specific download task"""
    from module.pyrogram_extension import remove_download_cache
    task_id = request.args.get("task_id")
    if not task_id:
        return jsonify({"code": "0", "message": "task_id required"})
    # Find and clear download cache before deleting
    download_result = get_download_result()
    for chat_id, messages in download_result.items():
        for msg_id, value in messages.items():
            composite_key = f"{chat_id}_{msg_id}"
            if composite_key == str(task_id) or str(value.get("task_id", "")) == str(task_id):
                remove_download_cache(chat_id, msg_id)
    # Try active downloads first, then failed
    success = delete_task(task_id)
    if not success:
        success = remove_failed_download(task_id)
    if success:
        return jsonify({"code": "1", "message": "deleted"})
    return jsonify({"code": "0", "message": "task not found"})


@_flask_app.route("/batch_delete", methods=["POST"])
def web_batch_delete():
    """Batch delete tasks by task_ids"""
    data = request.get_json(silent=True)
    if not data or "task_ids" not in data:
        return jsonify({"code": "0", "message": "task_ids required"})

    task_ids = data["task_ids"]
    if not isinstance(task_ids, list):
        return jsonify({"code": "0", "message": "task_ids must be a list"})

    deleted_active = batch_delete_tasks(task_ids)
    deleted_failed = batch_delete_failed(task_ids)
    total = deleted_active + deleted_failed

    return jsonify({
        "code": "1",
        "message": f"deleted {total} tasks",
        "deleted": total,
    })


@_flask_app.route("/retry_task", methods=["POST"])
def web_retry_task():
    """Retry a failed download task - removes from failed list"""
    task_id = request.args.get("task_id")
    if not task_id:
        return jsonify({"code": "0", "message": "task_id required"})
    success = remove_failed_download(task_id)
    if success:
        return jsonify({"code": "1", "message": "retry queued"})
    return jsonify({"code": "0", "message": "task not found"})
