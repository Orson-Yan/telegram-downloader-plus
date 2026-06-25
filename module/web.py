"""web ui for media download"""

import datetime
import logging
import os
import threading

from flask import Flask, jsonify, render_template, request

import utils
from module.app import Application
from module.task_store import get_pending_tasks, remove_task
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
import asyncio

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
_app: Application = None


def init_web(app: Application):
    """
    Initialize and start the web server.

    Args:
        app: The Application instance.

    Returns:
        None.
    """
    global _app
    _app = app
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

    # Pagination support for completed downloads
    offset = request.args.get("offset", 0, type=int)
    limit = request.args.get("limit", 0, type=int)

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

    total = len(result)

    # Apply pagination slice (only for completed downloads)
    if already_down and limit > 0:
        result = result[offset:offset + limit]

    return jsonify(result) if not already_down else jsonify({
        "tasks": result,
        "total": total,
        "offset": offset,
        "limit": limit,
    })


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
            "from_user_id": f.get("from_user_id", "") or "",
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
    """Retry a failed download task - re-download the file"""
    task_id = request.args.get("task_id")
    if not task_id:
        return jsonify({"code": "0", "message": "task_id required"})

    # Find the failed entry to get chat_id + msg_id
    failed_list = get_failed_downloads()
    target = None
    for f in failed_list:
        if str(f.get("task_id", "")) == str(task_id):
            target = f
            break

    if not target:
        return jsonify({"code": "0", "message": "task not found in failed list"})

    chat_id = target.get("chat_id")
    msg_id = target.get("msg_id")
    from_user_id = target.get("from_user_id", "") or ""
    if not chat_id or not msg_id:
        return jsonify({"code": "0", "message": "incomplete task data (missing chat_id/msg_id)"})

    # Remove from failed list first
    remove_failed_download(task_id)

    # Submit async retry to the main event loop
    try:
        if _app and _app.loop:
            asyncio.run_coroutine_threadsafe(
                _async_retry_download(chat_id, msg_id, from_user_id),
                _app.loop,
            )
            return jsonify({"code": "1", "message": "已加入重试队列"})
        return jsonify({"code": "0", "message": "应用尚未初始化完成"})
    except Exception as e:
        return jsonify({"code": "0", "message": f"重试失败: {str(e)}"})


@_flask_app.route("/batch_retry", methods=["POST"])
def web_batch_retry():
    """Batch retry multiple failed downloads"""
    data = request.get_json(silent=True)
    if not data or "task_ids" not in data:
        return jsonify({"code": "0", "message": "task_ids required"})

    task_ids = data["task_ids"]
    if not isinstance(task_ids, list):
        return jsonify({"code": "0", "message": "task_ids must be a list"})

    failed_list = get_failed_downloads()
    queued = 0
    errors = []

    for task_id in task_ids:
        target = None
        for f in failed_list:
            if str(f.get("task_id", "")) == str(task_id):
                target = f
                break

        if not target:
            errors.append(f"{task_id}: not found")
            continue

        chat_id = target.get("chat_id")
        msg_id = target.get("msg_id")
        from_user_id = target.get("from_user_id", "") or ""
        if not chat_id or not msg_id:
            errors.append(f"{task_id}: incomplete data")
            continue

        # Remove from failed list first
        remove_failed_download(task_id)

        # Submit async retry
        try:
            if _app and _app.loop:
                asyncio.run_coroutine_threadsafe(
                    _async_retry_download(chat_id, msg_id, from_user_id),
                    _app.loop,
                )
                queued += 1
            else:
                errors.append(f"{task_id}: app not initialized")
        except Exception as e:
            errors.append(f"{task_id}: {str(e)}")

    return jsonify({
        "code": "1",
        "message": f"已加入重试队列 {queued} 个任务" + (f"，{len(errors)} 个失败" if errors else ""),
        "queued": queued,
        "errors": errors,
    })


@_flask_app.route("/get_pending_list")
def web_get_pending_list():
    """Get list of pending tasks (received but not started downloading)"""
    import time as _time
    pending = get_pending_tasks()
    result = []
    for task in pending:
        task_id = task.get("task_id", "")
        created_at = task.get("created_at", 0)
        created_at_str = ""
        wait_time = ""
        if created_at:
            created_at_str = datetime.datetime.fromtimestamp(created_at).strftime("%m-%d %H:%M:%S")
            elapsed = int(_time.time() - created_at)
            h, m = divmod(elapsed // 60, 60)
            wait_time = f"{h}h{m:02d}m" if h > 0 else f"{m}m"

        # Try to get chat title from task data or URL
        chat_title = task.get("extra_data", {}).get("chat_title", "")
        url = task.get("url", "")

        # Determine source type
        source_type = task.get("task_type", "download")
        if source_type == "download":
            source_type = "频道消息"
        elif source_type == "forward":
            source_type = "转发消息"
        else:
            source_type = "消息"

        # Build filename from URL or extra_data
        filename = task.get("extra_data", {}).get("filename", "")
        if not filename and url:
            filename = url.split("/")[-1] if "/" in url else url
        if not filename:
            filename = f"task_{task_id}"

        total_size = task.get("extra_data", {}).get("total_size", "")
        if isinstance(total_size, (int, float)) and total_size > 0:
            total_size = format_byte(total_size)
        elif not total_size:
            total_size = "未知"

        task_id_display = task.get("extra_data", {}).get("task_id_display", str(task_id))

        result.append({
            "task_id": str(task_id),
            "task_id_display": task_id_display,
            "chat": str(task.get("chat_id", "")),
            "chat_title": chat_title,
            "filename": filename,
            "total_size": total_size,
            "url": url,
            "source_type": source_type,
            "created_at": created_at_str,
            "created_ts": created_at,
            "wait_time": wait_time,
        })

    # Sort by created_at ascending (earliest first)
    result.sort(key=lambda x: x.get("created_ts", 0))
    return jsonify(result)


@_flask_app.route("/remove_pending", methods=["POST"])
def web_remove_pending():
    """Remove a pending task from the queue"""
    task_id = request.args.get("task_id")
    if not task_id:
        return jsonify({"code": "0", "message": "task_id required"})
    # Try int conversion since task_store uses int task_ids
    try:
        task_id_int = int(task_id)
    except (ValueError, TypeError):
        task_id_int = task_id
    remove_task(task_id_int)
    return jsonify({"code": "1", "message": "removed"})


async def _async_retry_download(chat_id, msg_id, from_user_id=""):
    """Async helper: fetch the message and re-add to download queue"""
    logger = logging.getLogger("web.retry")
    try:
        try:
            cid = int(chat_id)
        except (ValueError, TypeError):
            cid = chat_id

        from module.bot import _bot

        client = _bot.client
        if not client:
            logger.error("Retry failed: _bot.client is not available")
            return

        msg = await client.get_messages(cid, int(msg_id))
        if not msg or msg.empty:
            logger.error(f"Retry failed: message {msg_id} not found in chat {cid}")
            return

        from module.app import TaskNode, TaskType

        node = TaskNode(
            chat_id=cid,
            from_user_id=cid,
            reply_message_id=0,
            replay_message="WebUI retry",
            limit=1,
            bot=None,
            task_id=_bot.gen_task_id(),
        )
        _bot.add_task_node(node)

        # Persist to bot_tasks.json so the task survives container restarts
        from module.task_store import save_task as _save_task
        _save_task(
            task_id=node.task_id,
            chat_id=cid,
            url="",
            start_offset_id=0,
            end_offset_id=0,
            limit=1,
            download_filter=None,
            from_user_id=cid,
            task_type="download",
            extra_data={"task_id_display": node.task_id_display},
        )

        from media_downloader import add_download_task as _add_download_task
        await _add_download_task(msg, node)
        node.is_running = True


        # Send retry notification to user via bot
        if from_user_id and _bot and _bot.bot:
            try:
                chat_name = getattr(msg.chat, "title", str(cid))
                msg_text = "🔄 重试任务已加入下载队列\n"
                msg_text += f"消息: {msg_id}\n"
                msg_text += f"任务: {node.task_id_display}\n"
                msg_text += f"群组: {chat_name}"
                await _bot.bot.send_message(int(from_user_id), msg_text)
            except Exception as e:
                logger.warning(f"Retry notification failed for user {from_user_id}: {e}")

        logger.info(f"Retry: queued message {msg_id} from chat {cid} as task {node.task_id_display}")
    except Exception as e:
        logger.error(f"Retry failed for chat={chat_id} msg={msg_id}: {e}", exc_info=True)
