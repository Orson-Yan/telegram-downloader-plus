"""Pyrogram ext"""

import asyncio
import html
import os
import secrets
import struct
import time
from copy import deepcopy
from datetime import datetime
from functools import wraps
from io import BytesIO, StringIO
from mimetypes import MimeTypes
from typing import Callable, Iterable, List, Optional, Tuple, Union

import pyrogram
from loguru import logger
from pyrogram import enums, parser, types, utils
from pyrogram.client import Cache
from pyrogram.enums import MessageEntityType
from pyrogram.file_id import (
    FILE_REFERENCE_FLAG,
    PHOTO_TYPES,
    WEB_LOCATION_FLAG,
    FileType,
    b64_decode,
    rle_decode,
)
from pyrogram.mime_types import mime_types

from module.app import (
    Application,
    CloudDriveUploadStat,
    DownloadStatus,
    ForwardStatus,
    TaskNode,
    UploadProgressStat,
    UploadStatus,
)
from module.download_stat import get_download_result
from module.language import Language, _t
from module.send_media_group_v2 import cache_media, send_media_group_v2
from utils.format import (
    create_progress_bar,
    extract_info_from_link,
    format_byte,
    truncate_filename,
)
from utils.meta_data import MetaData

_mimetypes = MimeTypes()
_mimetypes.readfp(StringIO(mime_types))
_download_cache = Cache(1024 * 1024 * 1024)


def reset_download_cache():
    """Reset download cache"""
    _download_cache.store.clear()


def remove_download_cache(chat_id, message_id):
    """Remove a specific entry from download cache."""
    try:
        _download_cache.store.pop((chat_id, message_id), None)
    except Exception as e:
        logger.warning(f"Failed to remove download cache ({chat_id}, {message_id}): {e}")


def _guess_mime_type(filename: str) -> Optional[str]:
    """Guess mime type"""
    return _mimetypes.guess_type(filename)[0]


def _guess_extension(mime_type: str) -> Optional[str]:
    """Guess extension"""
    return _mimetypes.guess_extension(mime_type)


def get_utf16_length(text: str) -> int:
    """
    Returns the length of UTF-16 units for the string text.

    Notes:
      - Using 'utf-16-le' encoding (without BOM), dividing the number of bytes by 2 gives the number of UTF-16 units in the string.
      - This correctly counts both regular characters (1 unit) and emoji characters outside the BMP (2 units).
    """
    # After encoding to utf-16-le, every 2 bytes represent 1 UTF-16 unit
    return len(text.encode("utf-16-le")) // 2


def get_media_obj(
    message: pyrogram.types.Message,
    media: str = None,
    caption: str = None,
    caption_entities: List[pyrogram.types.MessageEntity] = None,
    parse_mode: Optional[enums.ParseMode] = None,
) -> Union[
    types.InputMediaPhoto,
    types.InputMediaVideo,
    types.InputMediaAudio,
    types.InputMediaDocument,
    types.InputMediaAnimation,
]:
    """Get media object"""
    media_type = message.media
    if media_type == pyrogram.enums.MessageMediaType.PHOTO:
        return types.InputMediaPhoto(
            media,
            caption=caption,
            caption_entities=caption_entities,
            parse_mode=parse_mode,
        )

    if media_type == pyrogram.enums.MessageMediaType.VIDEO:
        return types.InputMediaVideo(
            media,
            caption=caption,
            caption_entities=caption_entities,
            parse_mode=parse_mode,
        )

    if media_type in [
        pyrogram.enums.MessageMediaType.AUDIO,
        pyrogram.enums.MessageMediaType.VOICE,
    ]:
        return types.InputMediaAudio(
            media,
            caption=caption,
            caption_entities=caption_entities,
            parse_mode=parse_mode,
        )

    if media_type == pyrogram.enums.MessageMediaType.DOCUMENT:
        return types.InputMediaDocument(
            media,
            caption=caption,
            caption_entities=caption_entities,
            parse_mode=parse_mode,
        )

    if media_type == pyrogram.enums.MessageMediaType.ANIMATION:
        return types.InputMediaAnimation(
            media,
            caption=caption,
            caption_entities=caption_entities,
            parse_mode=parse_mode,
        )

    return None


def _get_file_type(file_id: str):
    """Get file type"""
    decoded = rle_decode(b64_decode(file_id))

    # File id versioning. Major versions lower than 4 don't have a minor version
    major = decoded[-1]

    if major < 4:
        buffer = BytesIO(decoded[:-1])
    else:
        buffer = BytesIO(decoded[:-2])

    file_type, _ = struct.unpack("<ii", buffer.read(8))

    file_type &= ~WEB_LOCATION_FLAG
    file_type &= ~FILE_REFERENCE_FLAG

    try:
        file_type = FileType(file_type)
    except ValueError as exc:
        raise ValueError(f"Unknown file_type {file_type} of file_id {file_id}") from exc

    return file_type


def get_extension(file_id: str, mime_type: str, dot: bool = True) -> str:
    """Get extension"""

    if not file_id:
        if dot:
            return ".unknown"
        return "unknown"

    file_type = _get_file_type(file_id)

    guessed_extension = _guess_extension(mime_type)

    if file_type in PHOTO_TYPES:
        extension = "jpg"
    elif file_type == FileType.VOICE:
        extension = guessed_extension or "ogg"
    elif file_type in (FileType.VIDEO, FileType.ANIMATION, FileType.VIDEO_NOTE):
        extension = guessed_extension or "mp4"
    elif file_type == FileType.DOCUMENT:
        extension = guessed_extension or "zip"
    elif file_type == FileType.STICKER:
        extension = guessed_extension or "webp"
    elif file_type == FileType.AUDIO:
        extension = guessed_extension or "mp3"
    else:
        extension = "unknown"

    if dot:
        extension = "." + extension
    return extension


async def send_message_by_language(
    client: pyrogram.client.Client,
    language: Language,
    chat_id: Union[int, str],
    reply_to_message_id: int,
    language_str: List[str],
):
    """Record download status"""
    msg = language_str[language.value - 1]

    return await client.send_message(
        chat_id, msg, reply_to_message_id=reply_to_message_id
    )


async def download_thumbnail(
    client: pyrogram.Client,
    temp_path: str,
    message: pyrogram.types.Message,
):
    """Downloads the thumbnail of a video message to a temporary file.

    Args:
        client: A Pyrogram client instance.
        temp_path: The path to a temporary directory where the thumbnail file
                   will be stored.
        message: A Pyrogram Message object representing the video message.

    Returns:
        A string representing the path of the thumbnail file, or None if the
        download failed.

    Raises:
        ValueError: If the downloaded thumbnail file size doesn't match the
                    expected file size.
    """
    thumbnail_file = None
    if message.video.thumbs:
        message = await fetch_message(client, message)
        thumbnail = message.video.thumbs[0] if message.video.thumbs else None
        unique_name = os.path.join(
            temp_path,
            "thumbnail",
            f"thumb-{int(time.time())}-{secrets.token_hex(8)}.jpg",
        )

        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                thumbnail_file = await client.download_media(
                    thumbnail, file_name=unique_name
                )

                if os.path.getsize(thumbnail_file) == thumbnail.file_size:
                    break

                raise ValueError(
                    f"Thumbnail file size is {os.path.getsize(thumbnail_file)}"
                    f" bytes, actual {thumbnail.file_size}: {thumbnail_file}"
                )

            except Exception as e:
                if attempt == max_attempts:
                    logger.exception(
                        f"Failed to download thumbnail after {max_attempts}"
                        f" attempts: {e}"
                    )
                    thumbnail = None
                    thumbnail_file = None
                else:
                    message = await fetch_message(client, message)
                    thumbnail = message.video.thumbs[0] if message.video.thumbs else None
                    if not thumbnail:
                        break
                    logger.warning(
                        f"Attempt {attempt} to download thumbnail failed: {e}"
                    )
                    # Wait 2 seconds before retrying
                    await asyncio.sleep(2)
    return thumbnail_file


async def upload_telegram_chat(
    client: pyrogram.Client,
    upload_user: pyrogram.Client,
    app: Application,
    node: TaskNode,
    message: pyrogram.types.Message,
    download_status: DownloadStatus,
    file_name: str = None,
):
    """Upload telegram chat"""
    # upload telegram
    if node.upload_telegram_chat_id:
        if download_status is DownloadStatus.SkipDownload and message.media:
            if message.media_group_id:
                await proc_cache_forward(client, node, message, True, app)
            return

        if download_status is DownloadStatus.SuccessDownload or (
            download_status is DownloadStatus.SkipDownload and not message.media
        ):
            try:
                await upload_telegram_chat_message(
                    client,
                    upload_user,
                    app,
                    node,
                    message,
                    file_name,
                )
            except Exception as e:
                logger.exception(f"Upload file {file_name} error: {e}")
            finally:
                if file_name and app.after_upload_telegram_delete:
                    os.remove(file_name)

            # forward text
            # FIXME: fix upload text
            # if (
            #     download_status is DownloadStatus.SkipDownload
            #     and message.text
            #     and bot
            # ):
            #     await upload_telegram_chat(
            #         client, app, node.upload_telegram_chat_id, message, file_name
            #     )


async def upload_telegram_chat_message(
    client: pyrogram.Client,
    upload_user: pyrogram.Client,
    app: Application,
    node: TaskNode,
    message: pyrogram.types.Message,
    file_name: str = None,
) -> ForwardStatus:
    """See upload telegram_chat"""
    forward_status = ForwardStatus.FailedForward
    max_attempts = 3
    for _ in range(1, max_attempts + 1):
        try:
            forward_status = await _upload_telegram_chat_message(
                client, upload_user, app, node, message, file_name
            )
            break
        except pyrogram.errors.exceptions.flood_420.FloodWait as wait_err:
            await asyncio.sleep(wait_err.value * 2)
            logger.warning(
                "Upload Message[{}]: FlowWait {}", message.id, wait_err.value
            )
        except Exception as e:
            logger.exception(f"Upload file {file_name} error: {e}")
            return ForwardStatus.FailedForward

    if forward_status != ForwardStatus.CacheForward:
        node.stat_forward(forward_status)
    return forward_status


# pylint: disable=R0912
async def _upload_signal_message(
    client: pyrogram.Client,
    upload_user: pyrogram.Client,
    app: Application,
    node: TaskNode,
    upload_telegram_chat_id: Union[int, str, None],
    message: pyrogram.types.Message,
    file_name: Optional[str],
    caption: Optional[str] = None,
    text: Optional[str] = None,
):
    """
    Uploads a video or message to a Telegram chat.

    Parameters:
        client (pyrogram.Client): The pyrogram client.
        upload_telegram_chat_id (Union[int, str]): The ID of the chat to upload to.
        message (pyrogram.types.Message): The message to upload.
        file_name (str): The name of the file to upload.
    """
    ui_file_name = file_name
    if file_name:
        ui_file_name = (
            f"****{os.path.splitext(file_name)[-1]}"
            if app.hide_file_name
            else file_name
        )

    if message.video:
        # Download thumbnail
        thumbnail_file = await download_thumbnail(client, app.temp_save_path, message)
        try:
            # TODO(tangyoha): add more log when upload video more than 2000MB failed
            # Send video to the destination chat
            if node.reply_to_message:
                await node.reply_to_message.reply_video(
                    file_name,
                    caption=caption,
                    message_thread_id=node.topic_id,
                    thumb=thumbnail_file,
                    width=message.video.width,
                    height=message.video.height,
                    duration=message.video.duration,
                    parse_mode=pyrogram.enums.ParseMode.HTML,
                )
            else:
                await upload_user.send_video(
                    upload_telegram_chat_id,
                    file_name,
                    thumb=thumbnail_file,
                    width=message.video.width,
                    height=message.video.height,
                    duration=message.video.duration,
                    caption=caption,
                    parse_mode=pyrogram.enums.ParseMode.HTML,
                    progress=update_upload_stat,
                    progress_args=(
                        message.id,
                        ui_file_name,
                        time.time(),
                        node,
                        upload_user,
                    ),
                    message_thread_id=node.topic_id,
                )
        except Exception as e:
            raise e
        finally:
            if thumbnail_file:
                os.remove(str(thumbnail_file))

    elif message.photo:
        if node.reply_to_message:
            await node.reply_to_message.reply_photo(
                file_name,
                caption=caption,
                message_thread_id=node.topic_id,
            )
        else:
            await upload_user.send_photo(
                upload_telegram_chat_id,
                file_name,
                caption=caption,
                progress=update_upload_stat,
                progress_args=(
                    message.id,
                    ui_file_name,
                    time.time(),
                    node,
                    upload_user,
                ),
                message_thread_id=node.topic_id,
            )
    elif message.document:
        if node.reply_to_message:
            await node.reply_to_message.reply_document(
                file_name,
                caption=caption,
                message_thread_id=node.topic_id,
            )
        else:
            await upload_user.send_document(
                upload_telegram_chat_id,
                file_name,
                caption=caption,
                progress=update_upload_stat,
                progress_args=(
                    message.id,
                    ui_file_name,
                    time.time(),
                    node,
                    upload_user,
                ),
                message_thread_id=node.topic_id,
            )
    elif message.voice:
        if node.reply_to_message:
            await node.reply_to_message.reply_voice(
                file_name,
                caption=caption,
                message_thread_id=node.topic_id,
            )
        else:
            await upload_user.send_voice(
                upload_telegram_chat_id,
                file_name,
                caption=caption,
                progress=update_upload_stat,
                progress_args=(
                    message.id,
                    ui_file_name,
                    time.time(),
                    node,
                    upload_user,
                ),
                message_thread_id=node.topic_id,
            )

    elif message.audio:
        if node.reply_to_message:
            await node.reply_to_message.reply_audio(
                file_name,
                caption=caption,
                message_thread_id=node.topic_id,
            )
        else:
            await upload_user.send_audio(
                upload_telegram_chat_id,
                file_name,
                caption=caption,
                progress=update_upload_stat,
                progress_args=(
                    message.id,
                    ui_file_name,
                    time.time(),
                    node,
                    upload_user,
                ),
                message_thread_id=node.topic_id,
            )
    elif message.animation:
        if node.reply_to_message:
            await node.reply_to_message.reply_animation(
                file_name,
                caption=caption,
                message_thread_id=node.topic_id,
            )
        else:
            await upload_user.send_animation(
                upload_telegram_chat_id,
                file_name,
                caption=caption,
                progress=update_upload_stat,
                progress_args=(
                    message.id,
                    ui_file_name,
                    time.time(),
                    node,
                    upload_user,
                ),
                message_thread_id=node.topic_id,
            )
    elif message.text:
        new_caption = _t("Text Messages are not supported for forwarding")
        if node.reply_to_message:
            await node.reply_to_message.reply_text(new_caption + text)
        else:
            await upload_user.send_message(
                upload_telegram_chat_id, new_caption, message_thread_id=node.topic_id
            )


def update_upload_progress(
    current: int,
    total: int,
    message_id: int,
    file_name: str,
    start_time: int,
    node: TaskNode,
    client: pyrogram.Client,
):
    """Update upload progress"""
    node.upload_progress[message_id] = UploadProgressStat(
        current, total, file_name, start_time, time.time()
    )


async def update_upload_stat(
    current: int,
    total: int,
    message_id: int,
    file_name: str,
    start_time: int,
    node: TaskNode,
    client: pyrogram.Client,
):
    """Update upload stat"""
    node.upload_progress[message_id] = UploadProgressStat(
        current, total, file_name, start_time, time.time()
    )


def update_cloud_upload_stat(
    current: int,
    total: int,
    message_id: int,
    file_name: str,
    start_time: int,
    node: TaskNode,
    client: pyrogram.Client,
):
    """Update cloud upload stat"""
    node.upload_progress[message_id] = UploadProgressStat(
        current, total, file_name, start_time, time.time()
    )


_record_lock = asyncio.Lock()
_prev_message_id = 0


async def proc_cache_forward(
    client: pyrogram.Client,
    node: TaskNode,
    message: pyrogram.types.Message,
    enable_forward: bool,
    app: Application,
):
    """Process cached forward"""
    if message.media_group_id:
        await cache_media(client, message)
        forward_msg = await send_media_group_v2(
            app, node, client, node.upload_telegram_chat_id, message.media_group_id
        )
        if forward_msg:
            for msg in forward_msg:
                await proc_cache_forward(client, node, msg, enable_forward, app)
    else:
        if enable_forward:
            forward_msg = await forward_to_chat_self(client, message)
            if forward_msg:
                await report_bot_forward_status(node, message, forward_msg)


async def forward_to_chat_self(
    client: pyrogram.Client,
    message: pyrogram.types.Message,
):
    """Forward message to chat self"""
    chat_self = await client.get_me()
    return await message.forward(chat_self.id)


async def report_bot_forward_status(
    node: TaskNode,
    message: pyrogram.types.Message,
    forward_msg: pyrogram.types.Message,
):
    """report bot forward status"""
    if node and node.forward_message_event and message:
        node.forward_message_event.set()


def record_download_status(func):
    """Record download status"""

    @wraps(func)
    async def decorator(
        client: pyrogram.client.Client,
        message: pyrogram.types.Message,
        media_types: List[str],
        file_formats: dict,
        node: TaskNode,
    ):
        if _download_cache[(node.chat_id, message.id)] is DownloadStatus.Downloading:
            return DownloadStatus.Downloading, None, ""

        _download_cache[(node.chat_id, message.id)] = DownloadStatus.Downloading

        result = await func(client, message, media_types, file_formats, node)

        return result

    return decorator


_report_lock = asyncio.Lock()


async def report_bot_status(
    client: pyrogram.Client,
    node: TaskNode,
    immediate_reply=False,
):
    """see _report_bot_status"""
    try:
        async with _report_lock:
            return await _report_bot_status(client, node, immediate_reply)
    except pyrogram.errors.exceptions.flood_420.FloodWait as e:
        wait_seconds = e.value
        wait_minutes = wait_seconds / 60
        logger.warning(
            "FLOOD_WAIT: Telegram rate limit hit, need to wait {:.1f} minutes ({} seconds) before next EditMessage",
            wait_minutes,
            wait_seconds,
        )
        # Non-blocking: just set cooldown, don't sleep
        node.flood_wait_until = time.time() + wait_seconds
    except Exception as e:
        logger.debug(f"{e}")


async def _report_bot_status(
    client: pyrogram.Client,
    node: TaskNode,
    immediate_reply=False,
):
    """
    Sends a message with the current status of the download bot.

    Parameters:
        client (pyrogram.Client): The client instance.
        node (TaskNode): The download task node.
        immediate_reply(bool): Immediate reply

    Returns:
        None
    """
    if not node.reply_message_id or not node.bot:
        return

    if immediate_reply or node.can_reply():
        if node.upload_telegram_chat_id:
            node.forward_msg_detail_str = (
                f"\n🔄 {_t('Forward')}\n"
                f"├─ 📁 {_t('Total')}: {node.total_forward_task}\n"
                f"├─ ✅ {_t('Success')}: {node.success_forward_task}\n"
                f"├─ ❌ {_t('Failed')}: {node.failed_forward_task}\n"
                f"└─ ⏩ {_t('Skipped')}: {node.skip_forward_task}\n"
            )

        upload_msg_detail_str: str = ""

        if node.upload_success_count:
            upload_msg_detail_str = (
                f"\n☁️ {_t('Upload')}\n"
                f"└─ ✅ {_t('Success')}: {node.upload_success_count}\n"
            )

        upload_result_str = ""
        for key, value in node.upload_progress.items():
            if value.total > 0 and value.current > 0:
                upload_result_str += (
                    f" ├─ 🆔 {_t('Message ID')}: {key}\n"
                    f" │   ├─ 📁 : {temp_file_name}\n"
                    f" │   ├─ 📏 : {value.total}\n"
                    f" │   ├─ ⏫ : {value.speed}\n"
                    f" │   └─ 📊 : ["
                    f'{create_progress_bar(int(value.percentage.split("%")[0]))}]'
                    f" ({value.percentage})%\n"
                )

        download_result_str = ""
        download_result = get_download_result()
        if node.chat_id in download_result:
            messages = download_result[node.chat_id]
            for idx, value in messages.items():
                task_id = value["task_id"]
                if task_id != node.task_id or value["down_byte"] == value["total_size"]:
                    continue

                temp_file_name = os.path.basename(value["file_name"])
                progress = int(value["down_byte"] / value["total_size"] * 100)
                download_result_str += (
                    f" ├─ 🆔 {_t('Message ID')}: {idx}\n"
                    f" │   ├─ 📁 : {temp_file_name}\n"
                    f" │   ├─ 📏 : {format_byte(value['total_size'])}\n"
                    f" │   ├─ ⏬ : {format_byte(value['download_speed'])}/s\n"
                    f" │   └─ 📊 : [{create_progress_bar(progress)}]"
                    f" ({progress}%)\n"
                )

            if download_result_str:
                download_result_str = (
                    f"\n📥 {_t('Downloading')}:\n" + download_result_str
                )

        # Build completed files list
        completed_files_str = ""
        failed_files_str = ""
        download_result = get_download_result()
        if node.chat_id in download_result:
            for idx, value in download_result[node.chat_id].items():
                task_id = value.get("task_id", "")
                if str(task_id) == str(node.task_id) and value["down_byte"] == value["total_size"]:
                    fname = os.path.basename(value["file_name"])
                    fsize = format_byte(value["total_size"])
                    completed_files_str += f"  • {fname} ({fsize})\n"
        # Build failed files list with error reasons (from _failed_downloads)
        if node.failed_download_task > 0:
            try:
                from module.download_stat import get_failed_downloads
                for f in get_failed_downloads():
                    f_task_id = str(f.get("task_id", ""))
                    if f_task_id == str(node.task_id) or f_task_id == str(node.task_id_display):
                        fname = os.path.basename(f.get("file_name", ""))
                        err = f.get("error_message", "未知错误")
                        failed_files_str += f"  • {fname} — {err}\n"
            except Exception as e:
                logger.warning(f"Failed to read failed download data for status report: {e}")
            if failed_files_str:
                failed_files_str = f"\n❌ {_t('Failed')}:\n" + failed_files_str
        if completed_files_str:
            completed_files_str = f"\n📄 {_t('Files')}:\n" + completed_files_str

        # Re-count from _download_result for accuracy (avoids counter race conditions)
        actual_total = 0
        actual_success = 0
        actual_failed = 0
        if node.chat_id in download_result:
            for mid, val in download_result[node.chat_id].items():
                if str(val.get("task_id", "")) == str(node.task_id):
                    actual_total += 1
                    if val["down_byte"] == val["total_size"] and val["total_size"] > 0:
                        actual_success += 1
        if node.failed_download_task > 0:
            try:
                for f in get_failed_downloads():
                    f_tid = str(f.get("task_id", ""))
                    if f_tid == str(node.task_id) or f_tid == str(node.task_id_display):
                        actual_failed += 1
            except Exception as e:
                logger.warning(f"Failed to get failed downloads for re-count: {e}")
        if immediate_reply and (actual_total > 0 or actual_failed > 0):
            display_total = actual_total + actual_failed
            display_success = actual_success
            display_failed = actual_failed
            display_skipped = node.skip_download_task
        else:
            display_total = node.total_download_task
            display_success = node.success_download_task
            display_failed = node.failed_download_task
            display_skipped = node.skip_download_task

        new_msg_str = (
            f"`\n"
            f"🆔 task: {node.task_id_display}\n"
        )
        if immediate_reply:
            new_msg_str += (
                f"📥 {_t('Downloading')}\n"
                f"├─ 📁 {_t('Total')}: {display_total}\n"
                f"├─ ✅ {_t('Success')}: {display_success}\n"
                f"├─ ❌ {_t('Failed')}: {display_failed}\n"
                f"└─ ⏩ {_t('Skipped')}: {display_skipped}\n"
            )
        new_msg_str += (
            f"{node.forward_msg_detail_str}"
            f"{upload_msg_detail_str}"
            f"{upload_result_str}"
            f"{download_result_str}"
            f"{completed_files_str}"
            f"{failed_files_str}\n`"
        )
        if new_msg_str != node.last_edit_msg:
            # Compute current progress percentage (for update_download_status's
            # own 20% throttle in download_stat.py — we don't apply bucket
            # throttle here because update_reply_message's 3s polling is
            # already our rate limiter)
            current_pct = 0
            if not immediate_reply:
                total = 0
                weighted = 0
                download_result = get_download_result()
                if node.chat_id in download_result:
                    for idx, value in download_result[node.chat_id].items():
                        tid = str(value.get("task_id", ""))
                        if tid == str(node.task_id) or tid == str(node.task_id_display):
                            ts = value.get("total_size", 0)
                            if ts > 0:
                                total += ts
                                weighted += value.get("down_byte", 0)
                if total > 0:
                    current_pct = int(weighted / total * 100)
            try:
                await client.edit_message_text(
                    node.from_user_id,
                    node.reply_message_id,
                    new_msg_str,
                    parse_mode=pyrogram.enums.ParseMode.MARKDOWN,
                )
                node.last_edit_msg = new_msg_str
                node.last_progress_pct = current_pct  # Track last edited percentage for throttle
            except pyrogram.errors.exceptions.flood_420.FloodWait as e:
                wait_seconds = e.value
                wait_hours = wait_seconds / 3600
                wait_minutes = (wait_seconds % 3600) / 60
                node.flood_wait_until = time.time() + wait_seconds
                logger.warning(
                    "FLOOD_WAIT in edit_message: need to wait {:.0f}h {:.0f}m ({} seconds)",
                    wait_hours, wait_minutes, wait_seconds,
                )
            except pyrogram.errors.exceptions.bad_request_400.MessageNotModified:
                pass
            except Exception as e:
                logger.debug(f"edit_message_text failed: {e}")


def set_max_concurrent_transmissions(
    client: pyrogram.Client, max_concurrent_transmissions: int
):
    """Set maximum concurrent transmissions"""
    if getattr(client, "max_concurrent_transmissions", None):
        client.max_concurrent_transmissions = max_concurrent_transmissions
        client.save_file_semaphore = asyncio.Semaphore(
            client.max_concurrent_transmissions
        )
        client.get_file_semaphore = asyncio.Semaphore(
            client.max_concurrent_transmissions
        )