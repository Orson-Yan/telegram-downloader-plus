"""Bot task persistence store - save/restore tasks across restarts."""

import json
import os
import re
import threading
import time
from typing import Optional

from loguru import logger

_TASKS_FILE = os.path.join(os.path.abspath("."), "log", "bot_tasks.json")
_COUNTER_FILE = os.path.join(os.path.abspath("."), "log", "task_counter.json")
_lock = threading.Lock()


def _get_next_seq(date_str: str) -> int:
    """Get next sequential number for today, auto-increment.
    
    Date changes → seq resets to 1. Otherwise increments from last value.
    Thread-safe via _lock; caller must hold _lock.
    """
    try:
        if os.path.exists(_COUNTER_FILE):
            with open(_COUNTER_FILE, "r") as f:
                data = json.load(f)
            if data.get("date") == date_str:
                data["seq"] += 1
            else:
                data["date"] = date_str
                data["seq"] = 1
        else:
            data = {"date": date_str, "seq": 1}
        os.makedirs(os.path.dirname(_COUNTER_FILE), exist_ok=True)
        tmp = _COUNTER_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, _COUNTER_FILE)
        return data["seq"]
    except Exception as e:
        logger.warning(f"task_counter read/write error: {e}")
        return 1


def _set_seq(date_str: str, seq: int):
    """Set the counter to at least `seq` for today (only increases, never decreases).
    Thread-safe via _lock; caller must hold _lock.
    """
    try:
        os.makedirs(os.path.dirname(_COUNTER_FILE), exist_ok=True)
        if os.path.exists(_COUNTER_FILE):
            with open(_COUNTER_FILE, "r") as f:
                data = json.load(f)
            if data.get("date") == date_str and data.get("seq", 0) >= seq:
                return  # already larger, no overwrite
        data = {"date": date_str, "seq": seq}
        tmp = _COUNTER_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, _COUNTER_FILE)
    except Exception as e:
        logger.warning(f"task_counter write error: {e}")


def _parse_seq_from_display(task_id_display: str) -> int:
    """Extract the numeric part from 'MMDD-N' display id. Returns 0 if unparseable."""
    m = re.search(r"-(\d+)$", task_id_display)
    return int(m.group(1)) if m else 0


def _load_all() -> list:
    """Load all tasks from file."""
    try:
        if not os.path.exists(_TASKS_FILE):
            return []
        with open(_TASKS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load bot tasks: {e}")
        return []


def _save_all(tasks: list):
    """Save all tasks to file (atomic write via tmp + os.replace)."""
    try:
        os.makedirs(os.path.dirname(_TASKS_FILE), exist_ok=True)
        tmp = _TASKS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(tasks, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _TASKS_FILE)
    except Exception as e:
        logger.warning(f"Failed to save bot tasks: {e}")


def save_task(task_id, chat_id, url, start_offset_id, end_offset_id,
              limit, download_filter, from_user_id, task_type="download",
              extra_data=None):
    """Save a new bot task to the store."""
    with _lock:
        tasks = _load_all()
        # Remove existing task with same task_id (shouldn't happen but safe)
        tasks = [t for t in tasks if t.get("task_id") != task_id]
        tasks.append({
            "task_id": task_id,
            "chat_id": chat_id,
            "url": url,
            "start_offset_id": start_offset_id,
            "end_offset_id": end_offset_id,
            "limit": limit,
            "download_filter": download_filter,
            "from_user_id": from_user_id,
            "task_type": task_type,
            "extra_data": extra_data or {},
            "status": "running",
            "download_state": "pending",
            "last_message_id": start_offset_id,
            "created_at": time.time(),
        })
        _save_all(tasks)
        logger.info(f"Saved bot task {task_id} ({task_type}) to persistence store")


def update_task_progress(task_id, last_message_id):
    """Update the last processed message_id for a task."""
    with _lock:
        tasks = _load_all()
        for task in tasks:
            if task.get("task_id") == task_id:
                task["last_message_id"] = last_message_id
                task["updated_at"] = time.time()
                break
        _save_all(tasks)


def complete_task(task_id):
    """Mark a task as completed and remove it from the store."""
    with _lock:
        tasks = _load_all()
        tid = task_id if isinstance(task_id, int) else int(task_id) if str(task_id).isdigit() else task_id
        before = len(tasks)
        tasks = [t for t in tasks if t.get("task_id") != tid and str(t.get("task_id", "")) != str(task_id)]
        _save_all(tasks)
        if len(tasks) < before:
            logger.info(f"Removed completed bot task {task_id} from store")


def get_running_tasks() -> list:
    """Get all tasks with status='running' for recovery."""
    with _lock:
        tasks = _load_all()
        return [t for t in tasks if t.get("status") == "running"]


def get_pending_tasks() -> list:
    """Get all tasks with download_state='pending' (not yet consumed by pending consumer).
    
    'queued' means already in asyncio Queue — do NOT re-consume.
    """
    with _lock:
        tasks = _load_all()
        return [t for t in tasks if t.get("status") == "running"
                and t.get("download_state", "pending") == "pending"]


def get_downloading_tasks() -> list:
    """Get all tasks with download_state='downloading' (actively downloading)."""
    with _lock:
        tasks = _load_all()
        return [t for t in tasks if t.get("status") == "running"
                and t.get("download_state") == "downloading"]


def update_download_state(task_id, state: str):
    """Update the download_state of a task ('pending' or 'downloading')."""
    with _lock:
        tasks = _load_all()
        for task in tasks:
            if task.get("task_id") == task_id:
                task["download_state"] = state
                break
        _save_all(tasks)


def remove_task(task_id):
    """Remove a task from the store."""
    with _lock:
        tasks = _load_all()
        tasks = [t for t in tasks if t.get("task_id") != task_id]
        _save_all(tasks)
