"""Downloads media from telegram."""
import asyncio
import logging
import os
import shutil
import time
from typing import List, Optional, Tuple, Union

import pyrogram
from loguru import logger
from pyrogram.types import Audio, Document, Photo, Video, VideoNote, Voice
from rich.logging import RichHandler

from module.app import Application, ChatDownloadConfig, DownloadStatus, TaskNode
from module.bot import start_download_bot, stop_download_bot
from module.download_stat import load_downloads, save_downloads, set_chat_title, update_download_status
from module.download_stat import add_failed_download as _add_failed_download
from module.task_store import update_task_progress, update_download_state
from module.get_chat_history_v2 import get_chat_history_v2
from module.language import _t
from module.pyrogram_extension import (
    HookClient,
    fetch_message,
    get_extension,
    record_download_status,
    report_bot_download_status,
    set_max_concurrent_transmissions,
    set_meta_data,
    update_cloud_upload_stat,
    upload_telegram_chat,
)
from module.web import init_web
from utils.format import truncate_filename, validate_title
from utils.log import LogFilter
from utils.meta import print_meta
from utils.meta_data import MetaData

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler()],
)

CONFIG_NAME = "config.yaml"
DATA_FILE_NAME = "data.yaml"
APPLICATION_NAME = "media_downloader"
app = Application(CONFIG_NAME, DATA_FILE_NAME, APPLICATION_NAME)

queue: asyncio.Queue = asyncio.Queue()
RETRY_TIME_OUT = 3

logging.getLogger("pyrogram.session.session").addFilter(LogFilter())
logging.getLogger("pyrogram.client").addFilter(LogFilter())

logging.getLogger("pyrogram").setLevel(logging.WARNING)


def _check_download_finish(media_size: int, download_path: str, ui_file_name: str):
    """Check download task if finish"""
    download_size = os.path.getsize(download_path)
    if media_size == download_size:
        logger.success(f"{_t('Successfully downloaded')} - {ui_file_name}")
    else:
        logger.warning(
            f"{_t('Media downloaded with wrong size')}: "
            f"{download_size}, {_t('actual')}: "
            f"{media_size}, {_t('file name')}: {ui_file_name}"
        )
        os.remove(download_path)
        raise pyrogram.errors.exceptions.bad_request_400.BadRequest()


def _move_to_download_path(temp_download_path: str, download_path: str):
    """Move file to download path"""
    directory, _ = os.path.split(download_path)
    os.makedirs(directory, exist_ok=True)
    shutil.move(temp_download_path, download_path)


def _check_timeout(retry: int, _: int):
    """Check if message download timeout"""
    if retry == 2:
        return True
    return False


def _can_download(_type: str, file_formats: dict, file_format: Optional[str]) -> bool:
    """Check if the given file format can be downloaded."""
    if _type in ["audio", "document", "video"]:
        allowed_formats: list = file_formats[_type]
        if not file_format in allowed_formats and allowed_formats[0] != "all":
            return False
    return True


def _is_exist(file_path: str) -> bool:
    """Check if a file exists and it is not a directory."""
    return not os.path.isdir(file_path) and os.path.exists(file_path)


def _cleanup_temp_file(temp_file_name: str):
    """Remove temp file if it exists."""
    if temp_file_name and os.path.exists(temp_file_name):
        try:
            os.remove(temp_file_name)
        except OSError:
            pass


def _cleanup_stale_temp_files():
    """Remove stale temp files on startup.

    Rules:
    - 0-byte .temp files: always delete (empty shells from failed downloads)
    - Non-zero .temp files: delete if corresponding target file exists in downloads/
    - Empty directories in temp/: delete
    """
    temp_dir = app.temp_save_path
    if not os.path.isdir(temp_dir):
        return

    removed = 0
    for root, dirs, files in os.walk(temp_dir, topdown=False):
        for f in files:
            if not f.endswith('.temp'):
                continue
            temp_path = os.path.join(root, f)
            try:
                file_size = os.path.getsize(temp_path)
            except OSError:
                continue

            if file_size == 0:
                # Empty temp file — always remove
                try:
                    os.remove(temp_path)
                    removed += 1
                except OSError:
                    pass
            else:
                # Non-zero: check if target already exists in downloads/
                # temp path: temp/chat_id/filename.ext.temp
                # target path: downloads/chat_id/filename.ext
                rel_path = os.path.relpath(temp_path, temp_dir)
                # Strip .temp suffix to get the target filename
                target_name = f[:-5] if f.endswith('.temp') else f
                target_path = os.path.join(
                    os.path.abspath("."), "downloads",
                    os.path.dirname(rel_path), target_name
                )
                if os.path.exists(target_path):
                    try:
                        target_size = os.path.getsize(target_path)
                        if target_size >= file_size:
                            os.remove(temp_path)
                            removed += 1
                    except OSError:
                        pass

        # Remove empty directories
        for d in dirs:
            dir_path = os.path.join(root, d)
            try:
                if not os.listdir(dir_path):
                    os.rmdir(dir_path)
            except OSError:
                pass

    if removed:
        logger.info(f"Startup cleanup: removed {removed} stale temp files")


async def _get_media_meta(
    chat_id: Union[int, str],
    message: pyrogram.types.Message,
    media_obj: Union[Audio, Document, Photo, Video, VideoNote, Voice],
    _type: str,
) -> Tuple[str, str, Optional[str]]:
    """Extract file name and file id from media object."""
    if _type in ["audio", "document", "video"]:
        file_format: Optional[str] = media_obj.mime_type.split("/")[-1]
    else:
        file_format = None

    file_name = None
    temp_file_name = None
    dirname = validate_title(f"{chat_id}")
    if message.chat and message.chat.title:
        dirname = validate_title(f"{message.chat.title}")

    if message.date:
        datetime_dir_name = message.date.strftime(app.date_format)
    else:
        datetime_dir_name = "0"

    if _type in ["voice", "video_note"]:
        file_format = media_obj.mime_type.split("/")[-1]
        file_save_path = app.get_file_save_path(_type, dirname, datetime_dir_name)
        file_name = "{} - {}_{}.{}".format(
            message.id, _type, media_obj.date.isoformat(), file_format,
        )
        file_name = validate_title(file_name)
        temp_file_name = os.path.join(app.temp_save_path, dirname, file_name)
        file_name = os.path.join(file_save_path, file_name)
    else:
        file_name = getattr(media_obj, "file_name", None)
        caption = getattr(message, "caption", None)

        file_name_suffix = ".unknown"
        if not file_name:
            file_name_suffix = get_extension(
                media_obj.file_id, getattr(media_obj, "mime_type", "")
            )
        else:
            _, file_name_without_suffix = os.path.split(os.path.normpath(file_name))
            file_name, file_name_suffix = os.path.splitext(file_name_without_suffix)
            if not file_name_suffix:
                file_name_suffix = get_extension(
                    media_obj.file_id, getattr(media_obj, "mime_type", "")
                )

        if caption:
            caption = validate_title(caption)
            app.set_caption_name(chat_id, message.media_group_id, caption)
            app.set_caption_entities(
                chat_id, message.media_group_id, message.caption_entities
            )
        else:
            caption = app.get_caption_name(chat_id, message.media_group_id)

        if not file_name and message.photo:
            file_name = f"{message.photo.file_unique_id}"

        gen_file_name = (
            app.get_file_name(message.id, file_name, caption) + file_name_suffix
        )
        file_save_path = app.get_file_save_path(_type, dirname, datetime_dir_name)
        temp_file_name = os.path.join(app.temp_save_path, dirname, gen_file_name)
        file_name = os.path.join(file_save_path, gen_file_name)

    return truncate_filename(file_name), truncate_filename(temp_file_name), file_format


async def add_download_task(message: pyrogram.types.Message, node: TaskNode):
    """Add Download task"""
    if message.empty:
        return False
    node.download_status[message.id] = DownloadStatus.Downloading
    await queue.put((message, node))
    node.total_task += 1
    return True


async def save_msg_to_file(app, chat_id: Union[int, str], message: pyrogram.types.Message):
    """Write message text into file"""
    dirname = validate_title(
        message.chat.title if message.chat and message.chat.title else str(chat_id)
    )
    datetime_dir_name = message.date.strftime(app.date_format) if message.date else "0"
    file_save_path = app.get_file_save_path("msg", dirname, datetime_dir_name)
    file_name = os.path.join(
        app.temp_save_path, file_save_path,
        f"{app.get_file_name(message.id, None, None)}.txt",
    )
    os.makedirs(os.path.dirname(file_name), exist_ok=True)
    if _is_exist(file_name):
        return DownloadStatus.SkipDownload, None
    with open(file_name, "w", encoding="utf-8") as f:
        f.write(message.text or "")
    return DownloadStatus.SuccessDownload, file_name


async def download_task(client: pyrogram.Client, message: pyrogram.types.Message, node: TaskNode):
    """Download and Forward media"""
    download_status, file_name, error_message = await download_media(
        client, message, app.media_types, app.file_formats, node
    )
    # Backfill source_chat_title from cache (populated during download_media)
    if not node.source_chat_title and getattr(node, 'source_chat_id', 0):
        from module.download_stat import get_chat_title as _gct
        cached = _gct(node.source_chat_id)
        if cached:
            node.source_chat_title = cached
    if app.enable_download_txt and message.text and not message.media:
        download_status, file_name = await save_msg_to_file(app, node.chat_id, message)
    if not node.bot:
        app.set_download_id(node, message.id, download_status)
    node.download_status[message.id] = download_status
    file_size = os.path.getsize(file_name) if file_name else 0
    # Record failed downloads to the failed list for webui display
    if download_status is DownloadStatus.FailedDownload:
        # Get task_id_display (format: MMDD-N)
        task_id_display = getattr(node, "task_id_display", "") or str(node.task_id)
        # Build source link from node or message
        source_link = ""
        if getattr(node, 'source_chat_id', 0) and getattr(node, 'source_message_id', 0):
            # For forwarded messages, use source channel link
            source_id = node.source_chat_id
            if str(source_id).startswith("-100"):
                link_id = str(source_id)[4:]
            else:
                link_id = str(source_id)
            source_link = f"https://t.me/c/{link_id}/{node.source_message_id}"
        elif message and message.chat:
            # For direct messages, use current message link
            chat_id_for_link = message.chat.id
            if hasattr(message.chat, 'username') and message.chat.username:
                source_link = f"https://t.me/{message.chat.username}/{message.id}"
            else:
                if str(chat_id_for_link).startswith("-100"):
                    link_id = str(chat_id_for_link)[4:]
                else:
                    link_id = str(chat_id_for_link)
                source_link = f"https://t.me/c/{link_id}/{message.id}"
        _add_failed_download(
            chat_id=node.chat_id,
            msg_id=message.id,
            task_id=task_id_display,
            file_name=file_name or "",
            error_message=error_message or "下载失败",
            total_size=file_size,
            source_link=source_link,
        )
    await upload_telegram_chat(
        client, node.upload_user if node.upload_user else client,
        app, node, message, download_status, file_name,
    )
    if not node.upload_telegram_chat_id and download_status is DownloadStatus.SuccessDownload:
        ui_file_name = file_name
        if app.hide_file_name:
            ui_file_name = f"****{os.path.splitext(file_name)[-1]}"
        if await app.upload_file(file_name, update_cloud_upload_stat, (node, message.id, ui_file_name)):
            node.upload_success_count += 1
    await report_bot_download_status(node.bot, node, download_status, file_size)
    # Immediately mark task as complete if all files done (avoid relying on timer loop)
    if node.bot and node.is_finish() and not node.is_stop_transmission:
        try:
            from module.task_store import complete_task as _ct
            _ct(node.task_id)
        except Exception as e:
                    logger.warning(f"Failed to complete task {node.task_id}: {e}")


@record_download_status
async def download_media(
    client: pyrogram.client.Client,
    message: pyrogram.types.Message,
    media_types: List[str],
    file_formats: dict,
    node: TaskNode,
):
    """Download media from Telegram. Each file retried 3 times with 5s delay.
    Returns: (DownloadStatus, file_name, error_message)
    """
    file_name: str = ""
    ui_file_name: str = ""
    task_start_time: float = time.time()
    media_size = 0
    _media = None
    error_message = ""  # Track specific error reason
    message = await fetch_message(client, message)

    # Cache chat title from message object
    if message and message.chat:
        chat_title = getattr(message.chat, 'title', None) or getattr(message.chat, 'first_name', None)
        if chat_title:
            set_chat_title(message.chat.id, chat_title)
    try:
        for _type in media_types:
            _media = getattr(message, _type, None)
            if _media is None:
                continue
            file_name, temp_file_name, file_format = await _get_media_meta(
                node.chat_id, message, _media, _type
            )
            media_size = getattr(_media, "file_size", 0)
            ui_file_name = file_name
            if app.hide_file_name:
                ui_file_name = f"****{os.path.splitext(file_name)[-1]}"

            if _can_download(_type, file_formats, file_format):
                if _is_exist(file_name):
                    file_size = os.path.getsize(file_name)
                    if media_size > 0 and file_size >= media_size:
                        logger.info(
                            f"id={message.id} {ui_file_name} "
                            f"{_t('already download,download skipped')}."
                        )
                        return DownloadStatus.SkipDownload, None, ""
                    elif file_size > 0:
                        os.remove(file_name)
                        logger.info(
                            f"id={message.id} {ui_file_name} "
                            f"{_t('File exists but size mismatch')}: "
                            f"{file_size} != {media_size}, {_t('re-downloading')}."
                        )
            else:
                return DownloadStatus.SkipDownload, None, ""
            break
    except Exception as e:
        logger.error(
            f"Message[{message.id}]: "
            f"{_t('could not be downloaded due to following exception')}:\n[{e}].",
            exc_info=True,
        )
        return DownloadStatus.SkipDownload, None, ""
    if _media is None:
        return DownloadStatus.SkipDownload, None, ""
    # Build source link from message for failed downloads
    source_link = ""
    if message and message.chat:
        chat_id_for_link = message.chat.id
        # For private chats (user bot), use username if available
        if hasattr(message.chat, 'username') and message.chat.username:
            source_link = f"https://t.me/{message.chat.username}/{message.id}"
        else:
            # For channels/supergroups, use c/ prefix
            # Remove -100 prefix for channels
            if str(chat_id_for_link).startswith("-100"):
                link_id = str(chat_id_for_link)[4:]
            else:
                link_id = str(chat_id_for_link)
            source_link = f"https://t.me/c/{link_id}/{message.id}"

    message_id = message.id
    for retry in range(3):
        try:
            temp_download_path = await client.download_media(
                message, file_name=temp_file_name,
                progress=update_download_status,
                progress_args=(message_id, ui_file_name, task_start_time, node, client),
            )
            if temp_download_path and isinstance(temp_download_path, str):
                _check_download_finish(media_size, temp_download_path, ui_file_name)
                await asyncio.sleep(0.5)
                _move_to_download_path(temp_download_path, file_name)
                return DownloadStatus.SuccessDownload, file_name, ""
        except pyrogram.errors.exceptions.bad_request_400.BadRequest:
            _cleanup_temp_file(temp_file_name)
            logger.warning(
                f"Message[{message.id}]: {_t('file reference expired, refetching')}..."
            )
            error_message = "文件引用过期"
            await asyncio.sleep(RETRY_TIME_OUT)
            message = await fetch_message(client, message)
            if _check_timeout(retry, message.id):
                logger.error(
                    f"Message[{message.id}]: {_t('file reference expired for 3 retries, download skipped.')}"
                )
                error_message = "文件引用过期（重试3次后失败）"
        except pyrogram.errors.exceptions.flood_420.FloodWait as wait_err:
            _cleanup_temp_file(temp_file_name)
            await asyncio.sleep(wait_err.value)
            logger.info("Message[{}]: FlowWait {}s, waiting", message.id, wait_err.value)
            error_message = f"频率限制，等待{wait_err.value}秒"
            _check_timeout(retry, message.id)
        except TypeError:
            _cleanup_temp_file(temp_file_name)
            logger.warning(
                f"{_t('Timeout Error occurred when downloading Message')}[{message.id}], "
                f"{_t('retrying after')} {RETRY_TIME_OUT} {_t('seconds')}"
            )
            error_message = "下载超时"
            await asyncio.sleep(RETRY_TIME_OUT)
            if _check_timeout(retry, message.id):
                logger.error(
                    f"Message[{message.id}]: {_t('Timing out after 3 reties, download skipped.')}"
                )
                error_message = "下载超时（重试3次后失败）"
        except Exception as e:
            _cleanup_temp_file(temp_file_name)
            logger.error(
                f"Message[{message.id}]: "
                f"{_t('could not be downloaded due to following exception')}:\n[{e}].",
                exc_info=True,
            )
            error_message = f"下载异常: {str(e)[:100]}"
            break
    _cleanup_temp_file(temp_file_name)
    return DownloadStatus.FailedDownload, None, error_message or "下载失败"


def _load_config():
    """Load config"""
    app.load_config()


def _check_config() -> bool:
    """Check config"""
    print_meta(logger)
    try:
        _load_config()

        logger.add(
            os.path.join(app.log_file_path, "tdl.log"),
            rotation="10 MB",
            retention="30 days",
            level=app.log_level,
        )

        logger.add(
            os.path.join(app.log_file_path, "download.log"),
            rotation="10 MB",
            retention="30 days",
            level=app.log_level,
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | {name}:{function}:{line} - {message}",
        )

        load_downloads()
    except Exception as e:
        logger.exception(f"load config error: {e}")
        return False
    return True


async def worker(client: pyrogram.client.Client):
    """Work for download task"""
    while app.is_running:
        try:
            item = await queue.get()
            message = item[0]
            node: TaskNode = item[1]
            # Mark task as actively downloading (no longer pending)
            if node.task_id:
                update_download_state(node.task_id, "downloading")
            if node.is_stop_transmission:
                continue
            if node.client:
                await download_task(node.client, message, node)
            else:
                await download_task(client, message, node)
        except Exception as e:
            logger.exception(f"{e}")


async def download_chat_task(client: pyrogram.Client, chat_download_config: ChatDownloadConfig, node: TaskNode):
    """Download all task"""
    messages_iter = get_chat_history_v2(
        client, node.chat_id, limit=node.limit,
        max_id=node.end_offset_id, offset_id=chat_download_config.last_read_message_id, reverse=True,
    )
    chat_download_config.node = node
    if chat_download_config.ids_to_retry:
        logger.info(f"{_t('Downloading files failed during last run')}...")
        skipped_messages: list = await client.get_messages(
            chat_id=node.chat_id, message_ids=chat_download_config.ids_to_retry
        )
        for message in skipped_messages:
            await add_download_task(message, node)
    async for message in messages_iter:
        # Cache chat title from message
        if message and message.chat:
            chat_title = getattr(message.chat, 'title', None) or getattr(message.chat, 'first_name', None)
            if chat_title:
                set_chat_title(message.chat.id, chat_title)
        meta_data = MetaData()
        caption = message.caption
        if caption:
            caption = validate_title(caption)
            app.set_caption_name(node.chat_id, message.media_group_id, caption)
            app.set_caption_entities(node.chat_id, message.media_group_id, message.caption_entities)
        else:
            caption = app.get_caption_name(node.chat_id, message.media_group_id)
        set_meta_data(meta_data, message, caption)
        if app.need_skip_message(chat_download_config, message.id):
            continue
        if app.exec_filter(chat_download_config, meta_data):
            await add_download_task(message, node)
        else:
            node.download_status[message.id] = DownloadStatus.SkipDownload
            if message.media_group_id:
                await upload_telegram_chat(client, node.upload_user, app, node, message, DownloadStatus.SkipDownload)
        # Update task progress for crash recovery
        update_task_progress(node.task_id, message.id)
    chat_download_config.need_check = True
    chat_download_config.total_task = node.total_task
    node.is_running = True


async def download_all_chat(client: pyrogram.Client):
    """Download All chat"""
    from module.task_store import save_task as _save_task
    for key, value in app.chat_download_config.items():
        value.node = TaskNode(chat_id=key)
        _save_task(
            task_id=value.node.task_id,
            chat_id=key,
            url="",
            start_offset_id=value.last_read_message_id,
            end_offset_id=0,
            limit=0,
            download_filter=value.download_filter,
            from_user_id=0,
            task_type="config",
        )
        try:
            await download_chat_task(client, value, value.node)
        except Exception as e:
            logger.warning(f"Download {key} error: {e}")
        finally:
                    value.need_check = True
                    from module.task_store import complete_task
                    complete_task(value.node.task_id)


async def run_until_all_task_finish():
    """Normal download"""
    while True:
        finish = all(value.need_check and value.total_task == value.finish_task for _, value in app.chat_download_config.items())
        if (not app.bot_token and finish) or app.restart_program:
            break
        await asyncio.sleep(1)


def _exec_loop():
    """Exec loop"""
    app.loop.run_until_complete(run_until_all_task_finish())


async def start_server(client: pyrogram.Client):
    """Start the server"""
    await client.start()


async def stop_server(client: pyrogram.Client):
    """Stop the server"""
    await client.stop()


def main():
    """Main function"""
    tasks = []
    client = HookClient(
        "media_downloader", api_id=app.api_id, api_hash=app.api_hash,
        proxy=app.proxy, workdir=app.session_file_path,
        start_timeout=app.start_timeout, no_updates=True,
    )
    try:
        app.pre_run()
        _cleanup_stale_temp_files()
        init_web(app)
        set_max_concurrent_transmissions(client, app.max_concurrent_transmissions)
        app.loop.run_until_complete(start_server(client))
        logger.success(_t("Successfully started (Press Ctrl+C to stop)"))
        app.loop.create_task(download_all_chat(client))
        for _ in range(app.max_download_task):
            tasks.append(app.loop.create_task(worker(client)))
        if app.bot_token:
            app.loop.run_until_complete(start_download_bot(app, client, add_download_task, download_chat_task))
        _exec_loop()
    except KeyboardInterrupt:
        logger.info(_t("KeyboardInterrupt"))
    except Exception as e:
        logger.exception("{}", e)
    finally:
        app.is_running = False
        save_downloads()
        if app.bot_token:
            app.loop.run_until_complete(stop_download_bot())
        app.loop.run_until_complete(stop_server(client))
        for task in tasks:
            task.cancel()
        logger.info(_t("Stopped!"))
        logger.info(f"{_t('update config')}......")
        app.update_config()
        logger.success(
            f"{_t('Updated last read message_id to config file')},"
            f"{_t('total download')} {app.total_download_task}, "
            f"{_t('total upload file')} {app.cloud_drive_config.total_upload_success_file_count}"
        )


if __name__ == "__main__":
    if _check_config():
        main()