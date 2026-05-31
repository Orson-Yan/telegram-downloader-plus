"""web ui for media download"""

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

            progress = round(value["down_byte"] / value["total_size"] * 100, 1)
            download_speed = format_byte(value["download_speed"]) + "/s"

            # Generate task_id as chat_id_message_id or from node's task_id
            task_id = value.get("task_id", "")
            if not task_id:
                task_id = f"{chat_id}_{idx}"

            # Determine status: completed, paused, or active
            if is_already_down:
                status = "completed"
            elif is_task_paused(task_id):
                status = "paused"
            else:
                status = "active"

            # Get chat title from cache
            chat_title = get_chat_title(chat_id)

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
                "save_path": value["file_name"].replace("\\", "/"),
                "status": status,
            })

    return jsonify(result)


@_flask_app.route("/get_failed_downloads")
def web_get_failed_downloads():
    """Get list of failed downloads"""
    failed = get_failed_downloads()
    result = []
    for f in failed:
        chat_id = f.get("chat_id", "")
        chat_title = get_chat_title(chat_id)
        result.append({
            "task_id": str(f["task_id"]),
            "chat": str(chat_id),
            "chat_title": chat_title,
            "id": str(f.get("msg_id", "")),
            "filename": os.path.basename(f.get("file_name", "")),
            "error_message": f.get("error_message", "Unknown error"),
            "total_size": format_byte(f.get("total_size", 0)),
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
    task_id = request.args.get("task_id")
    if not task_id:
        return jsonify({"code": "0", "message": "task_id required"})
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
