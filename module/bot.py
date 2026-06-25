"""Bot for media downloader"""

import asyncio
import os
import time
from datetime import datetime
from typing import Callable, List, Union

import pyrogram
from loguru import logger
from pyrogram import types
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from ruamel import yaml

import utils
from module.app import (
    Application,
    ChatDownloadConfig,
    ForwardStatus,
    QueryHandler,
    QueryHandlerStr,
    TaskNode,
    TaskType,
    UploadStatus,
)
from module.download_stat import (
    add_failed_download,
    delete_task as _delete_download_progress,
    set_chat_title,
)
from module.filter import Filter
from module.task_store import save_task, complete_task, get_running_tasks, update_task_progress, get_pending_tasks, update_download_state
from module.get_chat_history_v2 import get_chat_history_v2
from module.language import Language, _t
from module.pyrogram_extension import (
    check_user_permission,
    get_utf16_length,
    parse_link,
    proc_cache_forward,
    report_bot_forward_status,
    report_bot_status,
    retry,
    set_meta_data,
    upload_telegram_chat_message,
)
from utils.format import replace_date_time, validate_title
from utils.meta_data import MetaData

# pylint: disable = C0301, R0902


def _cleanup_stopped_task(node):
    """When a task is stopped by user: clear download progress and record as failed.
    
    This ensures the task:
    1. Shows up in webui's Failed tab with '手动终止' reason
    2. Does NOT show lingering progress in the active/completed list
    3. Does NOT get recovered on next restart
    
    Only incomplete entries (down_byte < total_size) are recorded as failed.
    Already-completed entries are left untouched.
    """
    try:
        from module.download_stat import get_download_result
        download_result = get_download_result()
        removed = 0
        recorded = 0
        task_id_display = getattr(node, "task_id_display", "") or str(node.task_id)
        for chat_id, messages in list(download_result.items()):
            for msg_id, value in list(messages.items()):
                tid = str(value.get("task_id", ""))
                if tid != str(node.task_id) and tid != task_id_display:
                    continue
                total = value.get("total_size", 0)
                down = value.get("down_byte", 0)
                is_complete = total > 0 and down >= total
                if not is_complete:
                    # Build source link
                    source_link = ""
                    source_chat_id = value.get("source_chat_id", 0) or getattr(node, 'source_chat_id', 0)
                    source_message_id = value.get("source_message_id", 0) or getattr(node, 'source_message_id', 0)
                    if source_chat_id and source_message_id:
                        if str(source_chat_id).startswith("-100"):
                            link_id = str(source_chat_id)[4:]
                        else:
                            link_id = str(source_chat_id)
                        source_link = f"https://t.me/c/{link_id}/{source_message_id}"
                    # Record in failed list before deleting
                    add_failed_download(
                        chat_id=chat_id,
                        msg_id=msg_id,
                        task_id=task_id_display,
                        file_name=value.get("file_name", ""),
                        error_message="手动终止",
                        total_size=total,
                        source_link=source_link,
                            from_user_id=getattr(node, "from_user_id", "") or "",
                    )
                    recorded += 1
                # Delete the download progress entry (both incomplete and complete)
                # to remove it from active/completed list in webui
                _delete_download_progress(f"{chat_id}_{msg_id}")
                removed += 1
        if removed > 0:
            logger.info(f"Cleaned up {removed} download entries for stopped task {task_id_display}")
        else:
            # Fallback: even if no download_result entry, record a generic failed entry
            add_failed_download(
                chat_id=node.chat_id,
                msg_id=0,
                task_id=task_id_display,
                file_name="",
                error_message="手动终止",
                total_size=0,
                    from_user_id=getattr(node, "from_user_id", "") or "",
            )
    except Exception as e:
        logger.warning(f"Failed to cleanup stopped task {getattr(node, 'task_id_display', node.task_id)}: {e}")


def _record_pending_failures(node):
    """Record failed download_status entries that weren't caught by download_task.
    
    The criteria: entry exists in _download_result, belongs to this task,
    is incomplete (down_byte < total_size), AND has NEVER been touched by
    update_download_status (start_time == end_time means Pyrogram callback 
    never ran → never reached download_media's actual download loop).
    Entries that were touched by download (start_time < end_time) have already
    been handled by download_task's add_failed_download call, so we skip them
    to avoid duplicates.
    """
    try:
        from module.download_stat import get_download_result, get_failed_downloads
        download_result = get_download_result()
        # Collect existing failed composite keys to avoid duplicates
        existing_keys = {f"{f.get('chat_id', '')}_{f.get('msg_id', '')}" for f in get_failed_downloads()}
        recorded = 0
        task_id_display = getattr(node, "task_id_display", "") or str(node.task_id)
        for chat_id, messages in list(download_result.items()):
            for msg_id, value in list(messages.items()):
                tid = str(value.get("task_id", ""))
                if not (tid == str(node.task_id) or tid == task_id_display):
                    continue
                # Skip entries already recorded by download_task
                composite_key = f"{chat_id}_{msg_id}"
                if composite_key in existing_keys:
                    continue
                # Only catch entries that never started downloading
                # (down_byte == 0 or down_byte == total_size with same timestamps
                #  means Pyrogram progress callback never ran)
                if value.get("down_byte", 0) < value.get("total_size", 1) and value.get("total_size", 0) > 0:
                    # Build source link
                    source_link = ""
                    source_chat_id = value.get("source_chat_id", 0) or getattr(node, 'source_chat_id', 0)
                    source_message_id = value.get("source_message_id", 0) or getattr(node, 'source_message_id', 0)
                    if source_chat_id and source_message_id:
                        if str(source_chat_id).startswith("-100"):
                            link_id = str(source_chat_id)[4:]
                        else:
                            link_id = str(source_chat_id)
                        source_link = f"https://t.me/c/{link_id}/{source_message_id}"
                    add_failed_download(
                        chat_id=chat_id,
                        msg_id=msg_id,
                        task_id=task_id_display,
                        file_name=value.get("file_name", ""),
                        error_message="下载未开始（下载队列中未进入实际下载流程）",
                        total_size=value.get("total_size", 0),
                        source_link=source_link,
                            from_user_id=getattr(node, "from_user_id", "") or "",
                    )
                    recorded += 1
        if recorded > 0:
            logger.warning(f"Recorded {recorded} pending failures for task {task_id_display}")
    except Exception as e:
        logger.warning(f"Failed to record pending failures for task {getattr(node, 'task_id_display', node.task_id)}: {e}")


class DownloadBot:
    """Download bot"""

    def __init__(self):
        self.bot = None
        self.client = None
        self.add_download_task: Callable = None
        self.download_chat_task: Callable = None
        self.app = None
        self.listen_forward_chat: dict = {}
        self.config: dict = {}
        self._yaml = yaml.YAML()
        self.config_path = os.path.join(os.path.abspath("."), "bot.yaml")
        self.download_command: dict = {}
        self.filter = Filter()
        self.bot_info = None
        self.task_node: dict = {}
        self.is_running = True
        self.allowed_user_ids: List[Union[int, str]] = []
        self.monitor_task = None

        meta = MetaData(datetime(2022, 8, 5, 14, 35, 12), 0, "", 0, 0, 0, "", 0)
        self.filter.set_meta_data(meta)

        self.download_filter: List[str] = []
        self.task_id: int = 0
        self.reply_task = None

    def gen_task_id(self) -> int:
        """Gen task id"""
        self.task_id += 1
        return self.task_id

    def add_task_node(self, node: TaskNode):
        """Add task node"""
        self.task_node[node.task_id] = node

    def remove_task_node(self, task_id: int):
        """Remove task node"""
        self.task_node.pop(task_id)

    def stop_task(self, task_id: str):
        """Stop task"""
        if task_id == "all":
            for value in self.task_node.values():
                value.stop_transmission()
        else:
            try:
                task = self.task_node.get(int(task_id))
                if task:
                    task.stop_transmission()
            except Exception:
                return

    async def update_reply_message(self):
        """Update reply message"""
        while self.is_running:
            for key, value in self.task_node.copy().items():
                if value.is_running:
                    await report_bot_status(self.bot, value)

            for key, value in self.task_node.copy().items():
                if value.is_running and value.is_finish():
                    # Send final status update before completing
                    await report_bot_status(self.bot, value, immediate_reply=True)
                    # If stopped by user, move download progress to failed list and clear data
                    if value.is_stop_transmission:
                        _cleanup_stopped_task(value)
                    # Record any failed download_status entries that weren't caught by
                    # download_task (e.g. messages that were queued but never processed)
                    if not value.is_stop_transmission and value.failed_download_task > 0:
                        _record_pending_failures(value)
                    # Safety net: if media_downloader's immediate complete_task failed,
                    # ensure task is marked complete before removal
                    from module.task_store import complete_task
                    complete_task(value.task_id)
                    self.remove_task_node(key)
            await asyncio.sleep(3)

    async def recover_tasks(self):
        """Recover incomplete bot tasks from previous run.
        
        Two types of tasks need recovery:
        1. 'pending' - created but never started downloading → re-queue fresh
        2. 'downloading' - was actively downloading → re-execute (resume for forward, re-download for direct)
        """
        pending_tasks = []
        downloading_tasks = []
        try:
            await asyncio.sleep(5)  # Wait for bot to fully start
            running_tasks = get_running_tasks()
            if not running_tasks:
                return

            # Split by download_state
            pending_tasks = [t for t in running_tasks if t.get("download_state", "pending") == "pending"]
            downloading_tasks = [t for t in running_tasks if t.get("download_state") == "downloading"]

            if not pending_tasks and not downloading_tasks:
                return

            logger.info(f"Found {len(downloading_tasks)} interrupted + {len(pending_tasks)} pending tasks, recovering...")

            # Phase 1: Recover actively downloading tasks first (were interrupted mid-download)
            for task_data in downloading_tasks:
                await self._recover_single_task(task_data)

            # Phase 2: Re-queue pending tasks (created but never started)
            for task_data in pending_tasks:
                await self._recover_single_task(task_data)

        except Exception as e:
            logger.warning(f"Task recovery failed: {e}")

        # After recovery, update persistent counter to max of all recovered task display IDs
        try:
            from module.task_store import _set_seq, _parse_seq_from_display, _lock
            max_seq = 0
            import time
            today = time.strftime('%m%d')
            for task_data in (downloading_tasks + pending_tasks):
                extra = task_data.get("extra_data", {}) or {}
                display = extra.get("task_id_display", "")
                if display:
                    seq = _parse_seq_from_display(display)
                    if seq > max_seq:
                        max_seq = seq
            if max_seq > 0:
                with _lock:
                    _set_seq(today, max_seq)
                logger.info(f"Updated persistent task counter to {max_seq} (after recovering {len(downloading_tasks) + len(pending_tasks)} tasks)")
        except Exception as e:
            logger.warning(f"Failed to update task counter after recovery: {e}")

    async def _recover_single_task(self, task_data):
        """Recover a single task."""
        try:
            task_id = task_data.get("task_id")
            chat_id = task_data.get("chat_id")
            start_offset_id = task_data.get("last_message_id", task_data.get("start_offset_id", 0))
            end_offset_id = task_data.get("end_offset_id", 0)
            limit = task_data.get("limit", 0)
            download_filter = task_data.get("download_filter")
            from_user_id = task_data.get("from_user_id")
            task_type = task_data.get("task_type", "download")
            extra_data = task_data.get("extra_data", {})
            download_state = task_data.get("download_state", "pending")

            task_id_display = extra_data.get("task_id_display", "") if extra_data else ""

            # Recalculate limit from the new start
            if end_offset_id and start_offset_id:
                limit = max(0, end_offset_id - start_offset_id + 1)

            chat_download_config = ChatDownloadConfig()
            chat_download_config.last_read_message_id = start_offset_id
            chat_download_config.download_filter = download_filter

            # For forward tasks, set upload_telegram_chat_id
            dst_chat_id = extra_data.get("dst_chat_id", 0) if extra_data else 0

            node = TaskNode(
                chat_id=chat_id,
                from_user_id=from_user_id,
                reply_message_id=0,
                limit=limit,
                start_offset_id=start_offset_id,
                end_offset_id=end_offset_id,
                bot=self.bot,
                task_id=task_id,
                upload_telegram_chat_id=dst_chat_id,
                task_id_display=task_id_display,
            )
            self.add_task_node(node)

            # Cache source channel title for web display
            source_chat_id_extra = extra_data.get("source_chat_id", 0) if extra_data else 0
            source_chat_title_extra = extra_data.get("source_chat_title", "") if extra_data else ""
            if not source_chat_title_extra and source_chat_id_extra:
                # Old task without source_chat_title — try cache
                from module.download_stat import get_chat_title as _gct
                source_chat_title_extra = _gct(source_chat_id_extra) or ""
            if source_chat_id_extra and source_chat_title_extra:
                set_chat_title(source_chat_id_extra, source_chat_title_extra)
            node.source_chat_title = source_chat_title_extra
            node.source_chat_id = source_chat_id_extra

            state_label = "中断" if download_state == "downloading" else "待执行"
            # Don't send notification - let update_reply_message handle it
            if from_user_id and self.bot:
                try:
                    from module.download_stat import get_chat_title
                    chat_title = get_chat_title(chat_id) or str(chat_id)
                    type_label = "转发" if task_type == "forward" else "下载"
                    # Send initial message in progress format
                    initial_msg = (
                        f"`\n"
                        f"🆔 task: {node.task_id_display}\n"
                        f"🔄 恢复{state_label}任务 ({type_label})\n"
                        f"群组: {chat_title}\n`"
                    )
                    recovery_msg = await self.bot.send_message(
                        from_user_id,
                        initial_msg,
                        parse_mode=pyrogram.enums.ParseMode.MARKDOWN,
                    )
                    node.reply_message_id = recovery_msg.id
                    node.last_edit_msg = initial_msg
                    node.last_progress_pct = -1  # First 20% bucket edit always triggers
                    node.is_running = True
                except Exception as e:
                    logger.warning(f"Failed to send recovery notification: {e}")

            if task_type == "forward" and dst_chat_id:
                self.app.loop.create_task(
                    self._recover_forward_task(task_data, node, start_offset_id)
                )
            elif task_type == "direct":
                self.app.loop.create_task(
                    self._recover_direct_task(task_data, node)
                )
            else:
                self.app.loop.create_task(
                    self.download_chat_task(self.client, chat_download_config, node)
                )
            logger.info(f"Recovered {state_label} {task_type} task {task_id} for chat {chat_id}")
        except Exception as e:
            logger.warning(f"Failed to recover task {task_data.get('task_id')}: {e}")

    async def _recover_forward_task(self, task_data, node, offset_id):
        """Recover a forward task from last checkpoint."""
        from module.pyrogram_extension import report_bot_status
        try:
            async for item in get_chat_history_v2(
                self.client, node.chat_id,
                limit=node.limit, max_id=node.end_offset_id,
                offset_id=offset_id, reverse=True,
            ):
                if not node.has_protected_content:
                    await forward_normal_content(self.client, node, item)
                else:
                    await self.add_download_task(item, node)
                update_task_progress(node.task_id, item.id)
                if node.is_stop_transmission:
                    break
        except Exception as e:
            logger.warning(f"Forward recovery failed for task {node.task_id}: {e}")
        finally:
            await report_bot_status(self.bot, node, immediate_reply=True)
            node.stop_transmission()
            complete_task(node.task_id)

    async def _recover_direct_task(self, task_data, node):
        """Recover a direct download task (single message)."""
        from module.pyrogram_extension import report_bot_status
        extra_data = task_data.get("extra_data", {})
        message_id = extra_data.get("message_id", 0)
        source_chat_id = extra_data.get("source_chat_id", 0)
        source_message_id = extra_data.get("source_message_id", 0)

        if not message_id and not source_message_id:
            logger.warning(f"Direct recovery failed: no message_id for task {node.task_id}")
            complete_task(node.task_id)
            return

        success = False
        msg = None
        try:
            # Try source channel first (more reliable for forwarded messages)
            if source_chat_id and source_message_id:
                msg = await self.client.get_messages(source_chat_id, source_message_id)
                if msg and msg.media:
                    logger.info(f"Recovery: re-downloading from source {source_chat_id}/{source_message_id} for task {node.task_id}")
                else:
                    logger.info(f"Recovery: source message has no media, trying original chat")
                    msg = None

            # Fallback: try original chat_id + message_id
            if not msg and message_id:
                msg = await self.client.get_messages(node.chat_id, message_id)

            if msg and msg.media:
                # Check if file already exists in download history
                from module.download_stat import get_download_result
                _dlr = get_download_result()
                _exists = False
                if node.chat_id in _dlr:
                    for _mid, _val in _dlr[node.chat_id].items():
                        if str(_val.get("task_id")) == str(node.task_id) and _val.get("down_byte", 0) > 0 and _val.get("down_byte") == _val.get("total_size"):
                            _exists = True
                            break
                if not _exists:
                    logger.info(f"Recovery: re-downloading message {message_id} for task {node.task_id}")
                    await self.add_download_task(msg, node)
                    node.is_running = True
                    # Wait for download to finish (no timeout — large files may take hours)
                    while node.total_task == 0 or node.total_download_task < node.total_task:
                        await asyncio.sleep(3)
                        if node.is_stop_transmission:
                            break
                else:
                    logger.info(f"Recovery: file already exists for task {node.task_id}, marking complete")
                    node.total_task = 1
                    node.total_download_task = 1
                    node.success_download_task = 1
                    success = True
                # Check if download actually succeeded
                if node.success_download_task > 0:
                    success = True
                else:
                    logger.warning(f"Recovery: task {node.task_id} completed but 0 successful downloads (total={node.total_task}, success={node.success_download_task})")
            else:
                logger.warning(f"Direct recovery: message not found or has no media (msg={msg is not None}, media={getattr(msg, 'media', None) if msg else None})")
        except Exception as e:
            logger.warning(f"Direct recovery failed for task {node.task_id}: {e}")
        finally:
            await report_bot_status(self.bot, node, immediate_reply=True)
            node.stop_transmission()
            if success:
                complete_task(node.task_id)
            else:
                # Cleanup temp files from failed recovery
                _cleanup_task_temp_files(node.chat_id)
                logger.info(f"Recovery task {node.task_id} not completed, will retry next restart")

    def assign_config(self, _config: dict):
        """assign config from str.

        Parameters
        ----------
        _config: dict
            application config dict

        Returns
        -------
        bool
        """

        self.download_filter = _config.get("download_filter", self.download_filter)

        return True

    def update_config(self):
        """Update config from str."""
        self.config["download_filter"] = self.download_filter

        with open(self.config_path, "w", encoding="utf-8") as yaml_file:
            self._yaml.dump(self.config, yaml_file)

    async def start(
        self,
        app: Application,
        client: pyrogram.Client,
        add_download_task: Callable,
        download_chat_task: Callable,
    ):
        """Start bot"""
        self.bot = pyrogram.Client(
            app.application_name + "_bot",
            api_hash=app.api_hash,
            api_id=app.api_id,
            bot_token=app.bot_token,
            workdir=app.session_file_path,
            proxy=app.proxy,
        )

        # Command list
        commands = [
            types.BotCommand("help", _t("Help")),
            types.BotCommand(
                "get_info", _t("Get group and user info from message link")
            ),
            types.BotCommand(
                "download",
                _t(
                    "To download the video, use the method to directly enter /download to view"
                ),
            ),
            types.BotCommand(
                "forward",
                _t("Forward video, use the method to directly enter /forward to view"),
            ),
            types.BotCommand(
                "listen_forward",
                _t(
                    "Listen forward, use the method to directly enter /listen_forward to view"
                ),
            ),
            types.BotCommand(
                "add_filter",
                _t(
                    "Add download filter, use the method to directly enter /add_filter to view"
                ),
            ),
            types.BotCommand("set_language", _t("Set language")),
            types.BotCommand("stop", _t("Stop bot download or forward")),
        ]

        self.app = app
        self.client = client
        self.add_download_task = add_download_task
        self.download_chat_task = download_chat_task

        # load config
        if os.path.exists(self.config_path):
            with open(self.config_path, encoding="utf-8") as f:
                config = self._yaml.load(f.read())
                if config:
                    self.config = config
                    self.assign_config(self.config)

        await self.bot.start()

        self.bot_info = await self.bot.get_me()

        for allowed_user_id in self.app.allowed_user_ids:
            try:
                chat = await self.client.get_chat(allowed_user_id)
                self.allowed_user_ids.append(chat.id)
            except Exception as e:
                logger.warning(f"set allowed_user_ids error: {e}")

        admin = await self.client.get_me()
        self.allowed_user_ids.append(admin.id)

        await self.bot.set_bot_commands(commands)

        self.bot.add_handler(
            MessageHandler(
                download_from_bot,
                filters=pyrogram.filters.command(["download"])
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )
        self.bot.add_handler(
            MessageHandler(
                forward_messages,
                filters=pyrogram.filters.command(["forward"])
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )
        self.bot.add_handler(
            MessageHandler(
                download_forward_media,
                filters=pyrogram.filters.media
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )
        self.bot.add_handler(
            MessageHandler(
                download_from_link,
                filters=pyrogram.filters.regex(r"^https://t.me.*")
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )
        self.bot.add_handler(
            MessageHandler(
                set_listen_forward_msg,
                filters=pyrogram.filters.command(["listen_forward"])
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )
        self.bot.add_handler(
            MessageHandler(
                help_command,
                filters=pyrogram.filters.command(["help"])
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )
        self.bot.add_handler(
            MessageHandler(
                get_info,
                filters=pyrogram.filters.command(["get_info"])
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )
        self.bot.add_handler(
            MessageHandler(
                help_command,
                filters=pyrogram.filters.command(["start"])
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )
        self.bot.add_handler(
            MessageHandler(
                set_language,
                filters=pyrogram.filters.command(["set_language"])
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )
        self.bot.add_handler(
            MessageHandler(
                add_filter,
                filters=pyrogram.filters.command(["add_filter"])
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )

        self.bot.add_handler(
            MessageHandler(
                stop,
                filters=pyrogram.filters.command(["stop"])
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )

        self.bot.add_handler(
            CallbackQueryHandler(
                on_query_handler, filters=pyrogram.filters.user(self.allowed_user_ids)
            )
        )

        try:
            await send_help_str(self.bot, admin.id)
        except Exception as e:
            logger.warning(f"Failed to send help message: {e}")

        self.reply_task = _bot.app.loop.create_task(_bot.update_reply_message())

        # Recover incomplete tasks from previous run
        _bot.app.loop.create_task(_bot.recover_tasks())

        self.bot.add_handler(
            MessageHandler(
                forward_to_comments,
                filters=pyrogram.filters.command(["forward_to_comments"])
                & pyrogram.filters.user(self.allowed_user_ids),
            )
        )


_bot = DownloadBot()


def _cleanup_task_temp_files(chat_id: int):
    """Remove temp files for a specific chat from temp directory."""
    temp_dir = os.path.join(os.path.abspath("."), "temp")
    chat_dir = os.path.join(temp_dir, str(chat_id))
    if not os.path.isdir(chat_dir):
        return
    removed = 0
    for f in os.listdir(chat_dir):
        if f.endswith('.temp'):
            try:
                os.remove(os.path.join(chat_dir, f))
                removed += 1
            except OSError:
                pass
    # Remove directory if empty
    try:
        if not os.listdir(chat_dir):
            os.rmdir(chat_dir)
    except OSError:
        pass
    if removed:
        logger.info(f"Cleanup: removed {removed} stale temp files for chat {chat_id}")


async def start_download_bot(
    app: Application,
    client: pyrogram.Client,
    add_download_task: Callable,
    download_chat_task: Callable,
):
    """Start download bot"""
    await _bot.start(app, client, add_download_task, download_chat_task)


async def stop_download_bot():
    """Stop download bot"""
    _bot.update_config()
    _bot.is_running = False
    if _bot.reply_task:
        _bot.reply_task.cancel()
    _bot.stop_task("all")
    if _bot.bot:
        await _bot.bot.stop()
    if _bot.monitor_task:
        _bot.monitor_task.cancel()
        _bot.monitor_task = None


async def send_help_str(client: pyrogram.Client, chat_id):
    """
    Sends a help string to the specified chat ID using the provided client.

    Parameters:
        client (pyrogram.Client): The Pyrogram client used to send the message.
        chat_id: The ID of the chat to which the message will be sent.

    Returns:
        str: The help string that was sent.

    Note:
        The help string includes information about the Telegram Media Downloader bot,
        its version, and the available commands.
    """

    update_keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Github",
                    url="https://github.com/tangyoha/telegram_media_downloader/releases",
                ),
                InlineKeyboardButton(
                    "Join us", url="https://t.me/TeegramMediaDownload"
                ),
            ]
        ]
    )
    latest_release_str = ""
    # try:
    #     latest_release = get_latest_release(_bot.app.proxy)

    #     latest_release_str = (
    #         f"{_t('New Version')}: [{latest_release['name']}]({latest_release['html_url']})\an"
    #         if latest_release
    #         else ""
    #     )
    # except Exception:
    #     latest_release_str = ""

    msg = (
            f"`\n🤖 {_t('Telegram Media Downloader')}\n"
            f"🌐 {_t('Version')}: {utils.__version__}`\n"
            f"{latest_release_str}\n"
            f"{_t('Available commands:')}\n"
            f"/help - {_t('显示帮助信息')}\n"
            f"/start - {_t('显示帮助信息')}\n"
            f"/get_info <link> - {_t('从消息链接获取群组/频道信息')}\n"
            f"/download <link> <start_id> <end_id> [filter] - {_t('批量下载消息')}\n"
            f"/forward <src_link> <dst_link> <start_id> <end_id> [filter] - {_t('转发消息到目标群组')}\n"
            f"/forward_to_comments <src_link> <dst_link> <start_id> <end_id> - {_t('转发媒体到评论区')}\n"
            f"/listen_forward <src_link> <dst_link> [filter] - {_t('实时监听并自动转发新消息')}\n"
            f"/stop - {_t('停止下载/转发/监听转发（交互式按钮选择）')}\n"
            f"/set_language en|ru|zh|ua - {_t('设置语言')}\n"
            f"/add_filter <filter> - {_t('设置下载过滤器')}\n\n"
            f"{_t('**快捷操作（无需命令）：**')}\n"
            f"• {_t('发送 Telegram 消息链接')} - {_t('下载单条消息')}\n"
            f"• {_t('转发一条媒体消息给机器人')} - {_t('下载或重新上传该媒体')}\n\n"
            f"{_t('**说明**')}\n"
            f"• `[option]` {_t('表示可选参数，非必填')}\n"
            f"• {_t('start_id=1 表示从头开始，end_id=0 表示直到末尾')}\n"
        )

    await client.send_message(chat_id, msg, reply_markup=update_keyboard)


async def help_command(client: pyrogram.Client, message: pyrogram.types.Message):
    """
    Sends a message with the available commands and their usage.

    Parameters:
        client (pyrogram.Client): The client instance.
        message (pyrogram.types.Message): The message object.

    Returns:
        None
    """

    await send_help_str(client, message.chat.id)


async def set_language(client: pyrogram.Client, message: pyrogram.types.Message):
    """
    Set the language of the bot.

    Parameters:
        client (pyrogram.Client): The pyrogram client.
        message (pyrogram.types.Message): The message containing the command.

    Returns:
        None
    """

    if len(message.text.split()) != 2:
        await client.send_message(
            message.from_user.id,
            _t("Invalid command format. Please use /set_language en/ru/zh/ua"),
        )
        return

    language = message.text.split()[1]

    try:
        language = Language[language.upper()]
        _bot.app.set_language(language)
        await client.send_message(
            message.from_user.id, f"{_t('Language set to')} {language.name}"
        )
    except KeyError:
        await client.send_message(
            message.from_user.id,
            _t("Invalid command format. Please use /set_language en/ru/zh/ua"),
        )


async def get_info(client: pyrogram.Client, message: pyrogram.types.Message):
    """
    Async function that retrieves information from a group message link.
    """

    msg = _t("Invalid command format. Please use /get_info group_message_link")

    args = message.text.split()
    if len(args) != 2:
        await client.send_message(
            message.from_user.id,
            msg,
        )
        return

    chat_id, message_id, _ = await parse_link(_bot.client, args[1])

    entity = None
    if chat_id:
        entity = await _bot.client.get_chat(chat_id)

    if entity:
        if message_id:
            _message = await retry(_bot.client.get_messages, args=(chat_id, message_id))
            if _message:
                meta_data = MetaData()
                set_meta_data(meta_data, _message)
                msg = (
                    f"`\n"
                    f"{_t('Group/Channel')}\n"
                    f"├─ {_t('id')}: {entity.id}\n"
                    f"├─ {_t('first name')}: {entity.first_name}\n"
                    f"├─ {_t('last name')}: {entity.last_name}\n"
                    f"└─ {_t('name')}: {entity.username}\n"
                    f"{_t('Message')}\n"
                )

                for key, value in meta_data.data().items():
                    if key == "send_name":
                        msg += f"└─ {key}: {value or None}\n"
                    else:
                        msg += f"├─ {key}: {value or None}\n"

                msg += "`"
    await client.send_message(
        message.from_user.id,
        msg,
    )


async def add_filter(client: pyrogram.Client, message: pyrogram.types.Message):
    """
    Set the download filter of the bot.

    Parameters:
        client (pyrogram.Client): The pyrogram client.
        message (pyrogram.types.Message): The message containing the command.

    Returns:
        None
    """

    args = message.text.split(maxsplit=1)
    if len(args) != 2:
        await client.send_message(
            message.from_user.id,
            _t("Invalid command format. Please use /add_filter your filter"),
        )
        return

    filter_str = replace_date_time(args[1])
    res, err = _bot.filter.check_filter(filter_str)
    if res:
        _bot.app.down = args[1]
        await client.send_message(
            message.from_user.id, f"{_t('Add download filter')} : {args[1]}"
        )
    else:
        await client.send_message(
            message.from_user.id, f"{err}\n{_t('Check error, please add again!')}"
        )
    return


async def add_filter_advertisement_filter(
    client: pyrogram.Client, message: pyrogram.types.Message
):
    """
    Set the download filter of the bot.

    Parameters:
        client (pyrogram.Client): The pyrogram client.
        message (pyrogram.types.Message): The message containing the command.

    Returns:
        None
    """

    args = message.text.split(maxsplit=1)
    if len(args) != 2:
        await client.send_message(
            message.from_user.id,
            _t("Invalid command format. Please use /add_ad filter"),
        )
        return

    filter_str = args[1]

    _bot.app.filter_advertisement_list.append(filter_str)
    await client.send_message(message.from_user.id, f"{_t('Add filter')} : {args[1]}")
    _bot.app.update_config(True)


async def remove_filter_advertisement_filter(
    client: pyrogram.Client, message: pyrogram.types.Message
):
    """
    Add or remove advertisement filter
    """

    args = message.text.split(maxsplit=1)
    if len(args) != 2:
        await client.send_message(
            message.from_user.id,
            _t("Invalid command format. Please use /remove_ad filter"),
        )
        return

    filter_str = args[1]
    if filter_str in _bot.app.filter_advertisement_list:
        _bot.app.filter_advertisement_list.remove(filter_str)
        await client.send_message(
            message.from_user.id, f"{_t('Remove filter')} : {args[1]}"
        )

        _bot.app.update_config(True)
    else:
        await client.send_message(
            message.from_user.id, f"{_t('Filter')} : {args[1]} {_t('not exist')}"
        )


async def set_add_advertisement(
    client: pyrogram.Client, message: pyrogram.types.Message
):
    """
    Add or remove advertisement filter
    """

    args = message.text.split(maxsplit=2)
    if len(args) < 2:
        await client.send_message(
            message.from_user.id,
            _t("Invalid command format. Please use /set_ad mesage_link advertisement"),
        )
        return

    mesage_link = args[1]
    advertisement_str = None if len(args) < 3 else args[2]

    try:
        chat_id, _, _ = await parse_link(_bot.client, mesage_link)
        _bot.app.group_add_advertisement[chat_id] = advertisement_str
        _bot.app.update_config(True)
        await client.send_message(
            message.from_user.id, f"{_t('Set advertisement')} : {advertisement_str}"
        )
    except Exception as e:
        await client.send_message(
            message.from_user.id, f"{_t('Parse link error')}: {e}"
        )
        return


class MessageProcessor:
    """Helper class for processing message captions and entities."""

    def __init__(self, raw_message, filter_str):
        self.raw_message = raw_message
        self.raw_caption = raw_message.caption
        self.filter_str = filter_str
        self.raw_filter_str = pyrogram.parser.utils.add_surrogates(filter_str)
        self.raw_caption_str = pyrogram.parser.utils.add_surrogates(raw_message.caption)
        self.idx = self.raw_caption_str.find(self.raw_filter_str)
        self.start_offset = self.idx
        self.end_offset = self.idx + get_utf16_length(filter_str)
        self.filtered_entities = []

    # pylint: disable = R0916
    def process_entities(self):
        """Process and filter message entities."""
        for entity in self.raw_message.caption_entities:
            cur_start_offset = entity.offset
            cur_end_offset = entity.offset + entity.length

            # Check if entity should be included
            if (
                (
                    cur_start_offset >= self.start_offset
                    and cur_end_offset <= self.end_offset
                )
                or (
                    cur_start_offset < self.start_offset
                    and cur_end_offset > self.start_offset
                )
                or (
                    cur_start_offset < self.end_offset
                    and cur_end_offset > self.end_offset
                )
            ):
                self.filtered_entities.append(entity)

        self.filtered_entities.sort(key=lambda x: x.offset)

    def get_total_span(self):
        """Calculate the total span for text extraction."""
        if self.filtered_entities:
            first_entity = self.filtered_entities[0]
            last_entity = self.filtered_entities[-1]
            return (
                min(self.start_offset, first_entity.offset),
                max(self.end_offset, last_entity.offset + last_entity.length),
            )
        return (self.start_offset, self.end_offset)

    def extract_text(self, total_span):
        """Extract and process text with adjusted entity offsets."""
        text = self.raw_caption[total_span[0] : total_span[1]]
        for entity in self.filtered_entities:
            entity.offset -= total_span[0]
        return pyrogram.parser.Parser.unparse(text, self.filtered_entities, True)


async def proc_replace_advertisement(mesage_link: str, filter_str: str):
    """
    Process and replace advertisement content in a message.

    This function takes a message link and a filter string, retrieves the message,
    and processes its caption by handling entities and filtering advertisement content.
    It preserves the formatting and entities while replacing the specified filter text.

    Args:
        mesage_link (str): The link to the Telegram message
        filter_str (str): The string to filter/replace in the message caption

    Returns:
        str: The processed caption with preserved formatting and entities

    Raises:
        Exception: If there are issues parsing the message link or accessing the message
    """
    chat_id, message_id, _ = await parse_link(_bot.client, mesage_link)
    raw_message = await retry(_bot.client.get_messages, args=(chat_id, message_id))

    processor = MessageProcessor(raw_message, filter_str)
    processor.process_entities()
    total_span = processor.get_total_span()
    return processor.extract_text(total_span)


async def add_replace_advertisement_filter(
    client: pyrogram.Client, message: pyrogram.types.Message
):
    """
    Set the download filter of the bot.

    Parameters:
        client (pyrogram.Client): The pyrogram client.
        message (pyrogram.types.Message): The message containing the command.

    Returns:
        None
    """

    args = message.text.split(maxsplit=2)
    if len(args) != 3:
        await client.send_message(
            message.from_user.id,
            _t("Invalid command format. Please use /add_replace_ad your filter"),
        )
        return

    mesage_link = args[1]
    filter_str = args[2]

    try:
        filter_str = await proc_replace_advertisement(mesage_link, filter_str)
        _bot.app.replace_advertisement_list.append(filter_str)
        _bot.app.update_config(True)
        await client.send_message(
            message.from_user.id, f"{_t('Add filter')} : {filter_str}"
        )
    except Exception as e:
        await client.send_message(
            message.from_user.id, f"{_t('Add filter')} : {filter_str}\n{e}"
        )
        return


async def remove_replace_advertisement_filter(
    client: pyrogram.Client, message: pyrogram.types.Message
):
    """
    Set the download filter of the bot.

    Parameters:
        client (pyrogram.Client): The pyrogram client.
        message (pyrogram.types.Message): The message containing the command.

    Returns:
        None
    """

    args = message.text.split(maxsplit=2)
    if len(args) != 3:
        await client.send_message(
            message.from_user.id,
            _t(
                "Invalid command format. Please use /remove_replace_ad mesage_link advertisement_filter"
            ),
        )
        return

    mesage_link = args[1]
    filter_str = args[2]

    try:
        filter_str = await proc_replace_advertisement(mesage_link, filter_str)

        if filter_str in _bot.app.replace_advertisement_list:
            _bot.app.replace_advertisement_list.remove(filter_str)
            await client.send_message(
                message.from_user.id, f"{_t('Remove filter')} : {filter_str}"
            )
        else:
            await client.send_message(
                message.from_user.id,
                f"{_t('Filter not found')}: {filter_str}",
            )
        _bot.app.update_config(True)
    except Exception as e:
        await client.send_message(
            message.from_user.id, f"{_t('Add filter')} : {filter_str}\n{e}"
        )
        return


async def direct_download(
    download_bot: DownloadBot,
    chat_id: Union[str, int],
    message: pyrogram.types.Message,
    download_message: pyrogram.types.Message,
    client: pyrogram.Client = None,
    source_chat_id: int = 0,
    source_message_id: int = 0,
    source_chat_title: str = "",
):
    """Direct Download

    Args:
        source_chat_id: Original source channel ID (for forwarded messages recovery)
        source_message_id: Original source message ID (for forwarded messages recovery)
        source_chat_title: Source channel title (for web display)
    """

    replay_message = "Direct download..."
    last_reply_message = await download_bot.bot.send_message(
        message.from_user.id, replay_message, reply_to_message_id=message.id
    )

    node = TaskNode(
        chat_id=chat_id,
        from_user_id=message.from_user.id,
        reply_message_id=last_reply_message.id,
        replay_message=replay_message,
        limit=1,
        bot=download_bot.bot,
        task_id=_bot.gen_task_id(),
    )

    # node.client = client
    # ↑ 注释掉以统一使用 HookClient（设置了 max_concurrent_transmissions=25 的 client），
    #   让 worker 走 media_downloader.py:466 的 download_task(client, ...) 分支。
    #   否则 bot handler 的 client 默认 max_concurrent_transmissions=1，导致单线程下载。
    #   如需恢复旧行为，取消注释即可。
    node.source_chat_title = source_chat_title
    node.source_chat_id = source_chat_id
    node.source_message_id = source_message_id

    _bot.add_task_node(node)

    extra_data = {
        "message_id": download_message.id,
        "task_id_display": node.task_id_display,
    }
    if source_chat_id and source_message_id:
        extra_data["source_chat_id"] = source_chat_id
        extra_data["source_message_id"] = source_message_id
    if source_chat_title:
        extra_data["source_chat_title"] = source_chat_title

    save_task(
        task_id=node.task_id,
        chat_id=chat_id,
        url="",
        start_offset_id=0,
        end_offset_id=0,
        limit=1,
        download_filter=None,
        from_user_id=message.from_user.id,
        task_type="direct",
        extra_data=extra_data,
    )

    await _bot.add_download_task(
        download_message,
        node,
    )

    node.is_running = True


async def download_forward_media(
    client: pyrogram.Client, message: pyrogram.types.Message
):
    """
    Downloads the media from a forwarded message.

    Parameters:
        client (pyrogram.Client): The client instance.
        message (pyrogram.types.Message): The message object.

    Returns:
        None
    """

    if message.media and getattr(message, message.media.value):
        # If forwarded from a channel/group, download from source
        if message.forward_from_chat:
            source_chat_id = message.forward_from_chat.id
            source_message_id = message.forward_from_message_id or 0
            source_chat_title = message.forward_from_chat.title or ""

            if source_message_id:
                source_msg = await retry(
                    _bot.client.get_messages,
                    args=(source_chat_id, source_message_id),
                )
                if source_msg and source_msg.media:
                    await direct_download(
                        _bot, source_chat_id, message, source_msg, client,
                        source_chat_id=source_chat_id,
                        source_message_id=source_message_id,
                        source_chat_title=source_chat_title,
                    )
                    return
                # Source message deleted — fall through to direct download

        # Direct upload or forward from user (no source channel info)
        await direct_download(_bot, message.from_user.id, message, message, client)
        return

    await client.send_message(
        message.from_user.id,
        f"1. {_t('Direct download, directly forward the message to your robot')}\n\n",
        parse_mode=pyrogram.enums.ParseMode.HTML,
    )


async def download_from_link(client: pyrogram.Client, message: pyrogram.types.Message):
    """
    Downloads a single message from a Telegram link.

    Parameters:
        client (pyrogram.Client): The pyrogram client.
        message (pyrogram.types.Message): The message containing the Telegram link.

    Returns:
        None
    """

    if not message.text or not message.text.startswith("https://t.me"):
        return

    msg = (
        f"1. {_t('Directly download a single message')}\n"
        "<i>https://t.me/12000000/1</i>\n\n"
    )

    text = message.text.split()
    if len(text) != 1:
        await client.send_message(
            message.from_user.id, msg, parse_mode=pyrogram.enums.ParseMode.HTML
        )

    chat_id, message_id, _ = await parse_link(_bot.client, text[0])

    entity = None
    if chat_id:
        entity = await _bot.client.get_chat(chat_id)
    if entity:
        title = entity.title or entity.first_name or str(entity.id)
        set_chat_title(entity.id, title)
        if message_id:
            download_message = await retry(
                _bot.client.get_messages, args=(chat_id, message_id)
            )
            if download_message:
                await direct_download(_bot, entity.id, message, download_message)
            else:
                await client.send_message(
                    message.from_user.id,
                    f"{_t('From')} {entity.title} {_t('download')} {message_id} {_t('error')}!",
                    reply_to_message_id=message.id,
                )
        return

    await client.send_message(
        message.from_user.id, msg, parse_mode=pyrogram.enums.ParseMode.HTML
    )


# pylint: disable = R0912, R0915,R0914


async def download_from_bot(client: pyrogram.Client, message: pyrogram.types.Message):
    """Download from bot"""

    msg = (
        f"{_t('Parameter error, please enter according to the reference format')}:\n\n"
        f"1. {_t('Download all messages of common group')}\n"
        "<i>/download https://t.me/fkdhlg 1 0</i>\n\n"
        f"{_t('The private group (channel) link is a random group message link')}\n\n"
        f"2. {_t('The download starts from the N message to the end of the M message')}. "
        f"{_t('When M is 0, it means the last message. The filter is optional')}\n"
        f"<i>/download https://t.me/12000000 N M [filter]</i>\n\n"
    )

    args = message.text.split(maxsplit=4)
    if not message.text or len(args) < 4:
        await client.send_message(
            message.from_user.id, msg, parse_mode=pyrogram.enums.ParseMode.HTML
        )
        return

    url = args[1]
    try:
        start_offset_id = int(args[2])
        end_offset_id = int(args[3])
    except Exception:
        await client.send_message(
            message.from_user.id, msg, parse_mode=pyrogram.enums.ParseMode.HTML
        )
        return

    limit = 0
    if end_offset_id:
        if end_offset_id < start_offset_id:
            raise ValueError(
                f"end_offset_id < start_offset_id, {end_offset_id} < {start_offset_id}"
            )

        limit = end_offset_id - start_offset_id + 1

    download_filter = args[4] if len(args) > 4 else None

    if download_filter:
        download_filter = replace_date_time(download_filter)
        res, err = _bot.filter.check_filter(download_filter)
        if not res:
            await client.send_message(
                message.from_user.id, err, reply_to_message_id=message.id
            )
            return
    entity = None
    try:
        chat_id, _, _ = await parse_link(_bot.client, url)
        if chat_id:
            entity = await _bot.client.get_chat(chat_id)
        if entity:
            chat_title = entity.title or entity.first_name or str(entity.id)
            set_chat_title(entity.id, chat_title)
            reply_message = f"from {chat_title} "
            chat_download_config = ChatDownloadConfig()
            chat_download_config.last_read_message_id = start_offset_id
            chat_download_config.download_filter = download_filter
            reply_message += (
                f"download message id = {start_offset_id} - {end_offset_id} !"
            )
            last_reply_message = await client.send_message(
                message.from_user.id, reply_message, reply_to_message_id=message.id
            )
            node = TaskNode(
                chat_id=entity.id,
                from_user_id=message.from_user.id,
                reply_message_id=last_reply_message.id,
                replay_message=reply_message,
                limit=limit,
                start_offset_id=start_offset_id,
                end_offset_id=end_offset_id,
                bot=_bot.bot,
                task_id=_bot.gen_task_id(),
            )
            _bot.add_task_node(node)
            save_task(
                task_id=node.task_id,
                chat_id=entity.id,
                url=url,
                start_offset_id=start_offset_id,
                end_offset_id=end_offset_id,
                limit=limit,
                download_filter=download_filter,
                from_user_id=message.from_user.id,
                task_type="download",
                extra_data={"task_id_display": node.task_id_display},
            )
            _bot.app.loop.create_task(
                _bot.download_chat_task(_bot.client, chat_download_config, node)
            )
    except Exception as e:
        await client.send_message(
            message.from_user.id,
            f"{_t('chat input error, please enter the channel or group link')}\n\n"
            f"{_t('Error type')}: {e.__class__}"
            f"{_t('Exception message')}: {e}",
        )
        return


async def get_forward_task_node(
    client: pyrogram.Client,
    message: pyrogram.types.Message,
    task_type: TaskType,
    src_chat_link: str,
    dst_chat_link: str,
    offset_id: int = 0,
    end_offset_id: int = 0,
    download_filter: str = None,
    reply_comment: bool = False,
):
    """Get task node"""
    limit: int = 0

    if end_offset_id:
        if end_offset_id < offset_id:
            await client.send_message(
                message.from_user.id,
                f" end_offset_id({end_offset_id}) < start_offset_id({offset_id}),"
                f" end_offset_id{_t('must be greater than')} offset_id",
            )
            return None

        limit = end_offset_id - offset_id + 1

    src_chat_id, _, _ = await parse_link(_bot.client, src_chat_link)
    dst_chat_id, target_msg_id, topic_id = await parse_link(_bot.client, dst_chat_link)

    if not src_chat_id or not dst_chat_id:
        logger.info(f"{src_chat_id} {dst_chat_id}")
        await client.send_message(
            message.from_user.id,
            _t("Invalid chat link") + f"{src_chat_id} {dst_chat_id}",
            reply_to_message_id=message.id,
        )
        return None

    try:
        src_chat = await _bot.client.get_chat(src_chat_id)
        dst_chat = await _bot.client.get_chat(dst_chat_id)
    except Exception as e:
        await client.send_message(
            message.from_user.id,
            f"{_t('Invalid chat link')} {e}",
            reply_to_message_id=message.id,
        )
        logger.exception(f"get chat error: {e}")
        return None

    me = await client.get_me()
    if dst_chat.id == me.id:
        # TODO: when bot receive message judge if download
        await client.send_message(
            message.from_user.id,
            _t("Cannot be forwarded to this bot, will cause an infinite loop"),
            reply_to_message_id=message.id,
        )
        return None

    if download_filter:
        download_filter = replace_date_time(download_filter)
        res, err = _bot.filter.check_filter(download_filter)
        if not res:
            await client.send_message(
                message.from_user.id, err, reply_to_message_id=message.id
            )

    last_reply_message = await client.send_message(
        message.from_user.id,
        "Forwarding message, please wait...",
        reply_to_message_id=message.id,
    )

    node = TaskNode(
        chat_id=src_chat.id,
        from_user_id=message.from_user.id,
        upload_telegram_chat_id=dst_chat_id,
        reply_message_id=last_reply_message.id,
        replay_message=last_reply_message.text,
        has_protected_content=src_chat.has_protected_content,
        download_filter=download_filter,
        limit=limit,
        start_offset_id=offset_id,
        end_offset_id=end_offset_id,
        bot=_bot.bot,
        task_id=_bot.gen_task_id(),
        task_type=task_type,
        topic_id=topic_id,
    )

    if target_msg_id and reply_comment:
        node.reply_to_message = await _bot.client.get_discussion_message(
            dst_chat_id, target_msg_id
        )

    _bot.add_task_node(node)

    node.upload_user = _bot.client
    if not dst_chat.type is pyrogram.enums.ChatType.BOT:
        has_permission = await check_user_permission(_bot.client, me.id, dst_chat.id)
        if has_permission:
            node.upload_user = _bot.bot

    if node.upload_user is _bot.client:
        await client.edit_message_text(
            message.from_user.id,
            last_reply_message.id,
            "Note that the robot may not be in the target group,"
            " use the user account to forward",
        )

    return node


# pylint: disable = R0914
async def forward_message_impl(
    client: pyrogram.Client, message: pyrogram.types.Message, reply_comment: bool
):
    """
    Forward message
    """

    async def report_error(client: pyrogram.Client, message: pyrogram.types.Message):
        """Report error"""

        await client.send_message(
            message.from_user.id,
            f"{_t('Invalid command format')}."
            f"{_t('Please use')} "
            "/forward https://t.me/c/src_chat https://t.me/c/dst_chat "
            f"1 400 `[`{_t('Filter')}`]`\n",
        )

    args = message.text.split(maxsplit=5)
    if len(args) < 5:
        await report_error(client, message)
        return

    src_chat_link = args[1]
    dst_chat_link = args[2]

    try:
        offset_id = int(args[3])
        end_offset_id = int(args[4])
    except Exception:
        await report_error(client, message)
        return

    download_filter = args[5] if len(args) > 5 else None

    node = await get_forward_task_node(
        client,
        message,
        TaskType.Forward,
        src_chat_link,
        dst_chat_link,
        offset_id,
        end_offset_id,
        download_filter,
        reply_comment,
    )

    if not node:
        return

    # Persist forward task for crash recovery
    save_task(
        task_id=node.task_id,
        chat_id=node.chat_id,
        url=src_chat_link,
        start_offset_id=offset_id,
        end_offset_id=end_offset_id,
        limit=node.limit,
        download_filter=download_filter,
        from_user_id=message.from_user.id,
        task_type="forward",
        extra_data={"dst_chat_id": node.upload_telegram_chat_id, "dst_chat_link": dst_chat_link, "task_id_display": node.task_id_display},
    )

    if not node.has_protected_content:
        try:
            async for item in get_chat_history_v2(  # type: ignore
                _bot.client,
                node.chat_id,
                limit=node.limit,
                max_id=node.end_offset_id,
                offset_id=offset_id,
                reverse=True,
            ):
                await forward_normal_content(client, node, item)
                # Update progress for crash recovery
                update_task_progress(node.task_id, item.id)
                if node.is_stop_transmission:
                    await client.edit_message_text(
                        message.from_user.id,
                        node.reply_message_id,
                        f"{_t('Stop Forward')}",
                    )
                    break
        except Exception as e:
            await client.edit_message_text(
                message.from_user.id,
                node.reply_message_id,
                f"{_t('Error forwarding message')} {e}",
            )
        finally:
            await report_bot_status(client, node, immediate_reply=True)
            node.stop_transmission()
            complete_task(node.task_id)
    else:
        await forward_msg(node, offset_id)
        complete_task(node.task_id)


async def forward_messages(client: pyrogram.Client, message: pyrogram.types.Message):
    """
    Forwards messages from one chat to another.

    Parameters:
        client (pyrogram.Client): The pyrogram client.
        message (pyrogram.types.Message): The message containing the command.

    Returns:
        None
    """
    return await forward_message_impl(client, message, False)


async def forward_normal_content(
    client: pyrogram.Client, node: TaskNode, message: pyrogram.types.Message
):
    """Forward normal content"""
    forward_ret = ForwardStatus.FailedForward
    caption = message.caption
    if caption:
        caption = validate_title(caption)
        _bot.app.set_caption_name(node.chat_id, message.media_group_id, caption)
    else:
        caption = _bot.app.get_caption_name(node.chat_id, message.media_group_id)

    if caption and _bot.app.is_match_advertisement(caption):
        forward_ret = ForwardStatus.SkipForward
        if message.media_group_id:
            # TODO
            node.upload_status[message.id] = UploadStatus.SkipUpload
        return

    if node.download_filter:
        meta_data = MetaData()
        set_meta_data(meta_data, message, caption)
        _bot.filter.set_meta_data(meta_data)
        if not _bot.filter.exec(node.download_filter):
            forward_ret = ForwardStatus.SkipForward
            if message.media_group_id:
                node.upload_status[message.id] = UploadStatus.SkipUpload
                await proc_cache_forward(_bot.client, node, message, False, _bot.app)
            await report_bot_forward_status(client, node, forward_ret)
            return

    await upload_telegram_chat_message(
        _bot.client, node.upload_user, _bot.app, node, message
    )


async def forward_msg(node: TaskNode, message_id: int):
    """Forward normal message"""

    chat_download_config = ChatDownloadConfig()
    chat_download_config.last_read_message_id = message_id
    chat_download_config.download_filter = node.download_filter  # type: ignore

    await _bot.download_chat_task(_bot.client, chat_download_config, node)


async def check_new_messages(
    client: pyrogram.Client, chat_id: int, node: TaskNode, last_message_id: int = 0
):
    """
    Checks for new messages in the chat and forwards them.

    Parameters:
        client (pyrogram.Client): The pyrogram client
        chat_id (int): The chat ID to monitor
        node (TaskNode): The task node containing forwarding configuration
        last_message_id (int): The ID of the last processed message
    """
    try:
        # Only get the most recent message if last_message_id is 0
        if last_message_id == 0:
            async for message in get_chat_history_v2(  # type: ignore
                client, chat_id, limit=1  # Get only the latest message
            ):
                last_message_id = message.id
                return last_message_id

        # Otherwise check for new messages after last_message_id
        async for message in get_chat_history_v2(  # type: ignore
            client, chat_id, limit=100, offset_id=last_message_id, reverse=True
        ):
            if message.id > last_message_id:
                if not node.has_protected_content:
                    await forward_normal_content(client, node, message)
                    await report_bot_status(client, node, immediate_reply=True)
                else:
                    await _bot.add_download_task(message, node)
                last_message_id = message.id
    except Exception as e:
        logger.exception(f"Error checking new messages in chat {chat_id}: {e}")

    return last_message_id


async def start_message_monitor():
    """
    Starts monitoring all chats that need to be forwarded.
    Runs every 60 seconds to check for new messages.
    """
    last_message_ids = {}  # 存储每个聊天的最后处理的消息ID

    while _bot.is_running:
        try:
            for chat_id, node in _bot.listen_forward_chat.items():
                if not node.is_running:
                    continue

                last_id = last_message_ids.get(chat_id, 0)
                new_last_id = await check_new_messages(
                    _bot.client, chat_id, node, last_id
                )
                last_message_ids[chat_id] = new_last_id

        except Exception as e:
            logger.exception(f"Error in message monitor: {e}")

        await asyncio.sleep(60)  # 每60秒检查一次


async def set_listen_forward_msg(
    client: pyrogram.Client, message: pyrogram.types.Message
):
    """
    Set the chat to listen for forwarded messages.
    """
    args = message.text.split(maxsplit=3)

    if len(args) < 3:
        await client.send_message(
            message.from_user.id,
            f"{_t('Invalid command format')}. {_t('Please use')} /listen_forward "
            f"https://t.me/c/src_chat https://t.me/c/dst_chat [{_t('Filter')}]\n",
        )
        return

    src_chat_link = args[1]
    dst_chat_link = args[2]
    download_filter = args[3] if len(args) > 3 else None

    node = await get_forward_task_node(
        client,
        message,
        TaskType.ListenForward,
        src_chat_link,
        dst_chat_link,
        download_filter=download_filter,
    )

    if not node:
        return

    if node.chat_id in _bot.listen_forward_chat:
        _bot.remove_task_node(_bot.listen_forward_chat[node.chat_id].task_id)

    node.is_running = True
    _bot.listen_forward_chat[node.chat_id] = node

    if not hasattr(_bot, "monitor_task") or _bot.monitor_task is None:
        _bot.monitor_task = _bot.app.loop.create_task(start_message_monitor())


async def stop(client: pyrogram.Client, message: pyrogram.types.Message):
    """Stops listening for forwarded messages."""

    await client.send_message(
        message.chat.id,
        _t("Please select:"),
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        _t("Stop Download"), callback_data="stop_download"
                    ),
                    InlineKeyboardButton(
                        _t("Stop Forward"), callback_data="stop_forward"
                    ),
                ],
                [  # Second row
                    InlineKeyboardButton(
                        _t("Stop Listen Forward"), callback_data="stop_listen_forward"
                    )
                ],
            ]
        ),
    )


async def stop_task(
    client: pyrogram.Client,
    query: pyrogram.types.CallbackQuery,
    queryHandler: str,
    task_type: TaskType,
):
    """Stop task"""
    if query.data == queryHandler:
        buttons: List[InlineKeyboardButton] = []
        temp_buttons: List[InlineKeyboardButton] = []
        for key, value in _bot.task_node.copy().items():
            if not value.is_finish() and value.task_type is task_type:
                if len(temp_buttons) == 3:
                    buttons.append(temp_buttons)
                    temp_buttons = []
                temp_buttons.append(
                    InlineKeyboardButton(
                        f"{key}", callback_data=f"{queryHandler} task {key}"
                    )
                )
        if temp_buttons:
            buttons.append(temp_buttons)

        if buttons:
            buttons.insert(
                0,
                [
                    InlineKeyboardButton(
                        _t("all"), callback_data=f"{queryHandler} task all"
                    )
                ],
            )
            await client.edit_message_text(
                query.message.from_user.id,
                query.message.id,
                f"{_t('Stop')} {_t(task_type.name)}...",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
        else:
            await client.edit_message_text(
                query.message.from_user.id,
                query.message.id,
                f"{_t('No Task')}",
            )
    else:
        task_id = query.data.split(" ")[2]
        await client.edit_message_text(
            query.message.from_user.id,
            query.message.id,
            f"{_t('Stop')} {_t(task_type.name)}...",
        )
        _bot.stop_task(task_id)


async def on_query_handler(
    client: pyrogram.Client, query: pyrogram.types.CallbackQuery
):
    """
    Asynchronous function that handles query callbacks.

    Parameters:
        client (pyrogram.Client): The Pyrogram client object.
        query (pyrogram.types.CallbackQuery): The callback query object.

    Returns:
        None
    """

    for it in QueryHandler:
        queryHandler = QueryHandlerStr.get_str(it.value)
        if queryHandler in query.data:
            await stop_task(client, query, queryHandler, TaskType(it.value))


async def forward_to_comments(client: pyrogram.Client, message: pyrogram.types.Message):
    """
    Forwards specified media to a designated comment section.

    Usage: /forward_to_comments <source_chat_link> <destination_chat_link> <msg_start_id> <msg_end_id>

    Parameters:
        client (pyrogram.Client): The pyrogram client.
        message (pyrogram.types.Message): The message containing the command.
    """
    return await forward_message_impl(client, message, True)
