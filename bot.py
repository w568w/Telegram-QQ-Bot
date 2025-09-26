from hashlib import sha3_256
from pathlib import Path
from typing import Any, Literal, Optional, ClassVar, overload
import uuid
from telegram import Message, Update, ReplyParameters
import telegram
from telegram.ext import (
    Application,
    ApplicationBuilder,
    MessageHandler,
    filters,
    CommandHandler,
    ContextTypes,
)
from telegram.helpers import mention_markdown
import logging
import os
import asyncio
from dotenv import load_dotenv
import sqlite3
from dataclasses import dataclass
import websockets
import json
import httpx
import traceback

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

# Bot 配置
group_ids = [
    int(group_id.strip())
    for group_id in os.getenv("GROUP_IDS", "-100_00000_00000").split(",")
]
assert len(group_ids) > 0, "At least one group ID is required"
bot_token = os.getenv("BOT_TOKEN")
assert bot_token is not None, "BOT_TOKEN environment variable is required"
DEVELOPER_ID = os.getenv("DEVELOPER_ID")

# Napcat 配置
NAPCAT_URL = os.getenv("NAPCAT_WS_URL")
QQ_GROUP_ID = os.getenv("QQ_GROUP_ID")
logging.info(f"\n\nQQ_GROUP_ID: {QQ_GROUP_ID}\n\n")
assert QQ_GROUP_ID is not None, "QQ_GROUP_ID environment variable is required"

# 载入消息数据库
db_path = os.getenv("DB_PATH", "messages.db")


class DB:
    @dataclass
    class SavedMessageMapping:
        CREATE_TBL: ClassVar[str] = """CREATE TABLE IF NOT EXISTS saved_qq_messages (
            qq_message_id INTEGER NOT NULL,
            tg_message_id INTEGER NOT NULL,
            PRIMARY KEY (qq_message_id, tg_message_id)
        );
        """
        qq_message_id: int
        tg_message_id: int
    
    @dataclass
    class SavedUserMapping:
        CREATE_TBL: ClassVar[str] = """CREATE TABLE IF NOT EXISTS saved_qq_mappings_v2 (
            qq_user_id INTEGER NOT NULL,
            tg_user_id INTEGER NOT NULL,
            tg_username TEXT,
            PRIMARY KEY (qq_user_id, tg_user_id)
        );
        """
        qq_user_id: int
        tg_user_id: int
        tg_username: Optional[str] = None

    def __init__(self, db_path: str):
        self.connection = sqlite3.connect(db_path)
        self.connection.row_factory = sqlite3.Row
        # Condition 用于在查询不到映射时等待 map_message 的 notify
        self._condition = asyncio.Condition()
        with self.connection:
            self.connection.execute(self.SavedMessageMapping.CREATE_TBL)
            self.connection.execute(self.SavedUserMapping.CREATE_TBL)

    async def map_message(self, mapping: SavedMessageMapping):
        """将 QQ 消息和 TG 消息进行映射"""
        with self.connection:
            self.connection.execute(
                "INSERT INTO saved_qq_messages (qq_message_id, tg_message_id) VALUES (?, ?)",
                (
                    mapping.qq_message_id,
                    mapping.tg_message_id,
                ),
            )
        # 通知所有等待 get_by_id 的协程
        async with self._condition:
            self._condition.notify_all()

    async def get_by_id(self, id: int, type_: Literal["qq", "tg"], wait_timeout: float = 10.0) -> Optional[SavedMessageMapping]:
        """根据 ID 获取映射的消息；如果未找到则等待 map_message 通知"""
        wait_start_time = asyncio.get_event_loop().time()
        async with self._condition:
            while True:
                cursor = self.connection.cursor()
                if type_ == "qq":
                    cursor.execute(
                        "SELECT * FROM saved_qq_messages WHERE qq_message_id = ?",
                        (id,),
                    )
                elif type_ == "tg":
                    cursor.execute(
                        "SELECT * FROM saved_qq_messages WHERE tg_message_id = ?",
                        (id,),
                    )
                else:
                    raise ValueError("type_ must be either 'qq' or 'tg'")

                row = cursor.fetchone()
                if row is not None:
                    return self.SavedMessageMapping(
                        qq_message_id=row["qq_message_id"],
                        tg_message_id=row["tg_message_id"],
                    )
                # 未找到，且已经超时
                if asyncio.get_event_loop().time() - wait_start_time > wait_timeout:
                    return None
                # 未找到，等待被 map_message 通知
                try:
                    await asyncio.wait_for(self._condition.wait(), timeout=wait_timeout)
                except asyncio.TimeoutError:
                    return None

    def bind_user(self, mapping: SavedUserMapping):
        """绑定 QQ 用户和 TG 用户"""
        with self.connection:
            # 先删除已有的绑定关系
            self.connection.execute(
                "DELETE FROM saved_qq_mappings_v2 WHERE tg_user_id = ?",
                (mapping.tg_user_id,),
            )
            # 插入新的绑定关系
            self.connection.execute(
                "INSERT INTO saved_qq_mappings_v2 (qq_user_id, tg_user_id, tg_username) VALUES (?, ?, ?)",
                (mapping.qq_user_id, mapping.tg_user_id, mapping.tg_username),
            )

    def get_user_by_id(self, id: int, type_: Literal["qq", "tg"]) -> Optional[SavedUserMapping]:
        """根据 ID 获取绑定的用户"""
        cursor = self.connection.cursor()
        if type_ == "qq":
            cursor.execute(
                "SELECT * FROM saved_qq_mappings_v2 WHERE qq_user_id = ?",
                (id,),
            )
        elif type_ == "tg":
            cursor.execute(
                "SELECT * FROM saved_qq_mappings_v2 WHERE tg_user_id = ?",
                (id,),
            )
        else:
            raise ValueError("type_ must be either 'qq' or 'tg'")

        row = cursor.fetchone()
        if row is None:
            return None
        return self.SavedUserMapping(
            qq_user_id=row["qq_user_id"],
            tg_user_id=row["tg_user_id"],
            tg_username=row["tg_username"],
        )

    def get_user_by_username(self, username: str) -> Optional[SavedUserMapping]:
        """根据 Telegram 用户名获取绑定的用户"""
        cursor = self.connection.cursor()
        cursor.execute(
            "SELECT * FROM saved_qq_mappings_v2 WHERE tg_username = ?",
            (username,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return self.SavedUserMapping(
            qq_user_id=row["qq_user_id"],
            tg_user_id=row["tg_user_id"],
            tg_username=row["tg_username"],
        )

    def close(self):
        """关闭数据库连接"""
        self.connection.close()

db = DB(db_path)

app = ApplicationBuilder().token(bot_token).read_timeout(30.).write_timeout(30.).build()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    assert update.message is not None
    await update.message.reply_text("Hello!")

async def bind_qq_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /bindqq 命令"""
    assert update.message is not None
    message = update.message
    
    # 检查是否在允许的群组中
    if message.chat.id not in group_ids:
        await message.reply_text("此命令只能在指定的群组中使用。")
        return
    
    # 检查参数
    if not context.args or len(context.args) != 1:
        await message.reply_text("使用方法: /bindqq <QQ 号>")
        return
    
    try:
        qq_id = int(context.args[0])
    except ValueError:
        await message.reply_text("QQ 号必须是数字。")
        return
    
    # 获取用户信息
    tg_user_id = message.from_user.id
    tg_username = message.from_user.username
    
    # 保存绑定关系
    db.bind_user(DB.SavedUserMapping(qq_user_id=qq_id, tg_user_id=tg_user_id, tg_username=tg_username))
    
    await message.reply_text(f"已成功绑定 QQ 号 {qq_id} 到您的 Telegram 账号。")

def escape_mdv2(text: str) -> str:
    """使用 Telegram 的 Markdown V2 语法转义文本"""
    from telegram.helpers import escape_markdown
    return escape_markdown(text, version=2)

async def group_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info(f"\n\nReceived update: {update}\n\n")
    """处理群组消息"""
    assert update.message is not None
    message = update.message

    if message.chat.id not in group_ids:
        logging.debug(
            f"Received message from group {message.chat.id}, but not in {group_ids}"
        )
        return

    logging.info(f"Received update: {message}")

    qq_message_id = None
    saved_reply_to = None
    reply_info_text = ""
    try:
        if message.reply_to_message is not None:
            saved_reply_to = await db.get_by_id(message.reply_to_message.message_id, "tg")
            if saved_reply_to is None:
                # 找不到映射消息时，获取原始回复消息内容
                reply_msg = message.reply_to_message
                reply_user = reply_msg.from_user.first_name if reply_msg.from_user else "[???]"
                if reply_msg.from_user and reply_msg.from_user.last_name:
                    reply_user += f" {reply_msg.from_user.last_name}"

                reply_content = ""
                if reply_msg.text:
                    reply_content = reply_msg.text[:50] + ("..." if len(reply_msg.text) > 50 else "")
                elif reply_msg.caption:
                    reply_content = reply_msg.caption[:50] + ("..." if len(reply_msg.caption) > 50 else "")
                elif reply_msg.sticker:
                    reply_content = "[表情]"
                elif reply_msg.animation:
                    reply_content = "[动画]"
                elif reply_msg.photo:
                    reply_content = "[图片]"
                else:
                    reply_content = "[不可解析消息]"

                reply_info_text = f"[回复 {reply_user}: {reply_content}]\n"

        single_qq_msg = []
        if saved_reply_to is not None:
            # 如果是回复消息，添加引用信息
            single_qq_msg.append(
                {
                    "type": "reply",
                    "data": {
                        "id": saved_reply_to.qq_message_id,
                    },
                }
            )

        single_qq_msg.append(
            {
                "type": "text",
                "data": {
                    "text": f"{reply_info_text}{message.from_user.first_name}{' ' + message.from_user.last_name if message.from_user.last_name is not None else ''}: ",
                }
            }
        )
        if message.sticker is not None:
            # 处理贴纸消息
            sticker_file = await message.sticker.get_file()
            img_data = await get_converted_image_with_cache(sticker_file, sticker_file.file_path, message.sticker.file_unique_id)
            single_qq_msg.append(
                {
                    "type": "image",
                    "data": {
                        "file": encode_bytearray_to_base64_uri(img_data),
                        "sub_type": 1,
                    }
                }
            )
        if message.animation is not None:
            # 处理动画消息（GIF/WebM）
            animation_file = await message.animation.get_file()
            img_data = await get_converted_image_with_cache(animation_file, animation_file.file_path, message.animation.file_unique_id)
            single_qq_msg.append(
                {
                    "type": "image",
                    "data": {
                        "file": encode_bytearray_to_base64_uri(img_data),
                        "sub_type": 1,
                    }
                }
            )
        if message.voice is not None:
            # 处理语音消息（OGG）
            voice_file = await message.voice.get_file()
            voice_data = await get_converted_voice_with_cache(voice_file, voice_file.file_unique_id, voice_file.file_path)
            single_qq_msg.append(
                {
                    "type": "record",
                    "data": {
                        "file": encode_bytearray_to_base64_uri(voice_data),
                    }
                }
            )

        if len(message.photo) > 0:
            # 处理图片消息
            photo = message.photo[-1]
            image_file = await photo.get_file()
            img_data = await get_converted_image_with_cache(image_file, image_file.file_path, photo.file_unique_id)
            single_qq_msg.append(
                {
                    "type": "image",
                    "data": {
                        "file": encode_bytearray_to_base64_uri(img_data),
                    }
                }
            )

        # 处理文本消息和其中实体
        if message.text is not None:
            # 按实体位置切割文本
            text_segments = []
            last_offset = 0

            # 按照实体的偏移量排序
            sorted_entities = sorted(message.entities or [], key=lambda e: e.offset)

            for entity in sorted_entities:
                # 添加实体前的文本
                if entity.offset > last_offset:
                    text_segments.append({
                        "type": "text",
                        "content": message.text[last_offset:entity.offset]
                    })

                # 处理实体
                if entity.type == "mention":
                    # 处理 @username 格式
                    mentioned_username = message.text[entity.offset:entity.offset + entity.length]
                    username = mentioned_username[1:]  # 去掉 @ 符号
                    user_mapping = db.get_user_by_username(username)
                    if user_mapping:
                        text_segments.append({
                            "type": "at",
                            "qq_id": str(user_mapping.qq_user_id)
                        })
                    else:
                        text_segments.append({
                            "type": "text",
                            "content": mentioned_username
                        })
                elif entity.type == "text_mention":
                    # 处理直接 mention 用户的情况
                    mentioned_user = entity.user
                    user_mapping = db.get_user_by_id(mentioned_user.id, "tg")
                    if user_mapping:
                        text_segments.append({
                            "type": "at",
                            "qq_id": str(user_mapping.qq_user_id)
                        })
                    else:
                        display_name = mentioned_user.first_name
                        if mentioned_user.last_name:
                            display_name += f" {mentioned_user.last_name}"
                        text_segments.append({
                            "type": "text",
                            "content": f"@{display_name}"
                        })
                else:
                    # 其他类型的实体，保持原文本
                    text_segments.append({
                        "type": "text",
                        "content": message.text[entity.offset:entity.offset + entity.length]
                    })

                last_offset = entity.offset + entity.length

            # 添加最后剩余的文本
            if last_offset < len(message.text):
                text_segments.append({
                    "type": "text",
                    "content": message.text[last_offset:]
                })

            # 如果没有实体，直接添加整个文本
            if not text_segments:
                text_segments.append({
                    "type": "text",
                    "content": message.text
                })

            # 将处理后的文本段转换为 QQ 消息格式
            for segment in text_segments:
                if segment["type"] == "text" and segment["content"]:
                    single_qq_msg.append({
                        "type": "text",
                        "data": {"text": segment["content"]}
                    })
                elif segment["type"] == "at":
                    single_qq_msg.append({
                        "type": "at",
                        "data": {"qq": segment["qq_id"]}
                    })

        if message.caption is not None:
            # 处理图片或视频的标题
            single_qq_msg.append(
                {
                    "type": "text",
                    "data": {
                        "text": message.caption,
                    }
                }
            )

        qq_message_id = await qq_send_msg_in_group(single_qq_msg)
    except Exception as e:
        # 捕获解析过程中的任何错误
        err_id = str(uuid.uuid4())
        logging.error("=" * 32)
        error_msg = "Error occurred while processing Telegram message -> QQ\n"
        error_msg += f"Error ID: {err_id}\n"
        error_msg += f"Error message: {escape_mdv2(str(e))}\n"
        error_msg += f"Error traceback: {escape_mdv2(traceback.format_exc())}\n"
        error_msg += f"Original message data: {escape_mdv2(json.dumps(message_data, indent=2, ensure_ascii=False))}"
        logging.error(error_msg)
        logging.error("=" * 32)
        # 尝试发送错误消息到 Telegram
        try:
            qq_message_id = await qq_send_msg_in_group(
                [
                    {
                        "type": "text",
                        "data": {
                            "text": error_msg,
                        },
                    }
                ]
            )
        except Exception as e:
            logging.error(f"Still failed to send error log to Telegram: {e}")
            traceback.print_exc()
    # 保存映射关系
    if qq_message_id is not None:
        await db.map_message(
            DB.SavedMessageMapping(
                qq_message_id=qq_message_id,
                tg_message_id=message.message_id,
            )
        )

from ffmpeg.asyncio import FFmpeg
FFMPEG_EXECUTABLE = os.getenv("FFMPEG_EXECUTABLE", "ffmpeg-7.0.2-amd64-static/ffmpeg")

# 添加缓存配置
CACHE_DIR = os.getenv("CACHE_DIR", "runtime")
os.makedirs(CACHE_DIR, exist_ok=True)
CONVERTED_IMAGE_CACHE_DIR = Path(CACHE_DIR) / "image_cache"
os.makedirs(CONVERTED_IMAGE_CACHE_DIR, exist_ok=True)
CONVERTED_VOICE_CACHE_DIR = Path(CACHE_DIR) / "voice_cache"
os.makedirs(CONVERTED_VOICE_CACHE_DIR, exist_ok=True)

from lottie.importers import importers
from lottie.exporters import exporters
from lottie.parsers.baseporter import Baseporter
tgs_importer: Baseporter = importers.get_from_extension("tgs")
gif_exporter: Baseporter = exporters.get_from_extension("gif")

async def get_converted_image_with_cache(file_obj: telegram.File, file_path: str, file_unique_id: str) -> bytes:
    """
    通过 unique_id 获取转码后的图片，如果缓存不存在则下载并转码

    仅处理从 tg 到 qq 的转码
    """
    # 直接用 unique_id 作为缓存文件名
    cache_path = os.path.join(CONVERTED_IMAGE_CACHE_DIR, file_unique_id)
    
    # 检查缓存
    if os.path.exists(cache_path):
        logging.info(f"Using cached file: {cache_path}")
        with open(cache_path, "rb") as f:
            return f.read()
    
    # 缓存不存在，下载并转码
    logging.info(f"Cache miss, downloading and converting: {file_unique_id}")
    img_data = await file_obj.download_as_bytearray()
    
    if file_path.lower().endswith(".webm"):
        # WebM 转 GIF
        ffmpeg = (
            FFmpeg(FFMPEG_EXECUTABLE)
            .input("pipe:0")
            .output("pipe:1", f="gif")
        )
        converted_data = await ffmpeg.execute(bytes(img_data))
    elif file_path.lower().endswith(".mp4"):
        # MP4 转 GIF
        # FFMpeg 不支持 MP4 的流式输入，因此需要先保存到临时文件
        # 见 https://github.com/fluent-ffmpeg/node-fluent-ffmpeg/issues/932#issuecomment-699675713
        from aiofiles.tempfile import NamedTemporaryFile
        async with NamedTemporaryFile(suffix=".mp4") as temp_file:
            await temp_file.write(img_data)
            temp_file_path = temp_file.name
            ffmpeg = (
                FFmpeg(FFMPEG_EXECUTABLE)
                .input(temp_file_path)
                .output("pipe:1", f="gif")
            )
            converted_data = await ffmpeg.execute()
    elif file_path.lower().endswith(".tgs"):
        # TGS 转 GIF
        def process_tgs_to_gif(data: bytes) -> bytes:
            from io import BytesIO
            from lottie.objects.animation import Animation
            animation: Animation = tgs_importer.process(BytesIO(data))
            output = BytesIO()
            gif_exporter.process(animation, output)
            return output.getvalue()
        
        converted_data = await asyncio.to_thread(process_tgs_to_gif, bytes(img_data))
    else:
        # 其他格式不转码
        converted_data = bytes(img_data)
    
    # 保存到缓存
    with open(cache_path, "wb") as f:
        f.write(converted_data)
    logging.info(f"Cached converted file: {cache_path}")
    
    return converted_data

@overload
async def get_converted_voice_with_cache(file_url: str, file_unique_id: str) -> bytes:
    ...
@overload
async def get_converted_voice_with_cache(file_url: telegram.File, file_unique_id: str, file_path: str) -> bytes:
    ...
async def get_converted_voice_with_cache(file_url: str | telegram.File, file_unique_id: str, file_path: Optional[str] = None) -> bytes:
    """
    通过 unique_id 获取转码后的语音，如果缓存不存在则下载并转码

    注意，与图片不同，语音在双向都需要转码（tg .ogg -> qq .amr, qq .amr -> tg .ogg）
    """
    cache_path = os.path.join(CONVERTED_VOICE_CACHE_DIR, file_unique_id)
    if os.path.exists(cache_path):
        logging.info(f"Using cached voice file: {cache_path}")
        with open(cache_path, "rb") as f:
            return f.read()

    # 缓存不存在，下载并转码
    logging.info(f"Cache miss, downloading and converting voice: {file_unique_id}")
    voice_data: bytes
    mime_type_or_ext = file_path.lower() if file_path else None
    if isinstance(file_url, telegram.File):
        # 如果是 File 对象，直接下载
        voice_data = bytes(await file_url.download_as_bytearray())
    else:
        # 如果是 URL，使用 httpx 下载
        async with httpx.AsyncClient() as client:
            response = await client.get(file_url, timeout=10)
        if response.status_code != 200:
            logging.error(f"Failed to download voice file from {file_url}, status code: {response.status_code}")
            raise RuntimeError(f"Failed to download voice file from {file_url}")
        voice_data = response.content
        mime_type_or_ext = response.headers.get("Content-Type", "").lower()
    
    assert mime_type_or_ext is not None and isinstance(mime_type_or_ext, str), "mime_type_or_ext must be a string"

    if mime_type_or_ext.endswith("ogg"):
        # OGG 转 AMR
        ffmpeg = (
            FFmpeg(FFMPEG_EXECUTABLE)
            .input("pipe:0")
            .output("pipe:1", f="amr_nb")
        )
        converted_data = await ffmpeg.execute(bytes(voice_data))
    elif mime_type_or_ext.endswith("amr"):
        # AMR 转 OGG
        ffmpeg = (
            FFmpeg(FFMPEG_EXECUTABLE)
            .input("pipe:0")
            .output("pipe:1", f="ogg")
        )
        converted_data = await ffmpeg.execute(bytes(voice_data))
    else:
        # 其他格式不转码，直接使用原始数据
        converted_data = voice_data
    
    # 保存到缓存
    with open(cache_path, "wb") as f:
        f.write(converted_data)
    logging.info(f"Cached converted voice file: {cache_path}")
    return converted_data


def encode_bytearray_to_base64_uri(data: bytes) -> str:
    """将字节数组编码为 Base64 字符串"""
    import base64
    return "base64://" + base64.b64encode(data).decode('utf-8')

ws_send_task_queue = asyncio.Queue()
async def qq_send_msg_in_group(single_qq_msg: list[dict[str, Any]]) -> int:
    """发送单条消息到 QQ 群组"""
    logging.info(f"\n\nSending msg: {single_qq_msg}\n\n")
    action = "send_group_msg"
    params = {
        "group_id": QQ_GROUP_ID,
        "message": single_qq_msg,
    }
    echo = str(uuid.uuid4())
    loop = asyncio.get_running_loop()
    completion = loop.create_future()
    # 将消息发送到 WebSocket 队列，在 websocket_handler 中处理
    await ws_send_task_queue.put((action, params, echo, completion))
    # 等待结果
    result = await completion
    return result["data"]["message_id"]

async def qq_get_msg_info(qq_msg_id: int) -> dict[str, Any]:
    """获取单条 QQ 消息的详细内容"""
    logging.info(f"\n\nGetting msg ID: {qq_msg_id}\n\n")
    action = "get_msg"
    params = {
        "message_id": qq_msg_id,
    }
    echo = str(uuid.uuid4())
    loop = asyncio.get_running_loop()
    completion = loop.create_future()
    # 将消息发送到 WebSocket 队列，在 websocket_handler 中处理
    await ws_send_task_queue.put((action, params, echo, completion))
    # 等待结果
    result = await completion
    return result

async def qq_get_forward_msg_info(forward_list_id: str) -> dict[str, Any]:
    """获取 QQ 转发消息的详细内容"""
    logging.info(f"\n\nGetting forward msg ID: {forward_list_id}\n\n")
    action = "get_forward_msg"
    params = {
        "message_id": forward_list_id,
    }
    echo = str(uuid.uuid4())
    loop = asyncio.get_running_loop()
    completion = loop.create_future()
    # 将消息发送到 WebSocket 队列，在 websocket_handler 中处理
    await ws_send_task_queue.put((action, params, echo, completion))
    # 等待结果
    result = await completion
    return result

async def debug_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """调试处理函数，打印接收到的更新"""
    logging.info(f"\n\nReceived update: {update}\n\n")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    import html
    """错误处理函数，打印错误信息"""
    logging.error("Exception while handling an update:", exc_info=context.error)

    # traceback.format_exception returns the usual python message about an exception, but as a
    # list of strings rather than a single string, so we have to join them together.
    tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
    tb_string = "".join(tb_list)

    # Build the message with some markup and additional information about what happened.
    # You might need to add some logic to deal with messages longer than the 4096 character limit.
    update_str = update.to_dict() if isinstance(update, Update) else str(update)
    message = (
        "An exception was raised while handling an update\n"
        f"<pre>update = {html.escape(json.dumps(update_str, indent=2, ensure_ascii=False))}"
        "</pre>\n\n"
        f"<pre>context.chat_data = {html.escape(str(context.chat_data))}</pre>\n\n"
        f"<pre>context.user_data = {html.escape(str(context.user_data))}</pre>\n\n"
        f"<pre>{html.escape(tb_string)}</pre>"
    )

    # Finally, send the message
    if DEVELOPER_ID is not None:
        await context.bot.send_message(
            chat_id=DEVELOPER_ID, text=message, parse_mode=telegram.constants.ParseMode.HTML
        )

@dataclass
class ConstructedTelegramMessageFromQQ:
    # 元信息
    sender_name: str
    reply_tg_message_id: Optional[int] = None
    # 消息内容
    text_context: str = ""
    image_url: Optional[str] = None
    voice_data: Optional[bytes] = None
    
    def is_empty(self) -> bool:
        """检查当前消息内容是否为空"""
        return len(self.text_context.strip()) == 0 and self.image_url is None and self.voice_data is None

    def reset(self):
        """重置消息内容为空，但不重置回复 ID、发送者名称等元数据"""
        self.text_context = ""
        self.image_url = None
        self.voice_data = None

    async def _is_animated_image(self) -> bool:
        """检查当前消息是否包含动画图片"""
        if self.image_url is None: return False
        if self.image_url.lower().endswith((".gif", ".webm", ".tgs")): return True
        # 请求网络，通过 header Content-Type 判断是否为动画图片
        async with httpx.AsyncClient() as client:
            async with client.stream("GET", self.image_url, timeout=5) as response:
                content_type = response.headers.get("Content-Type", "").lower()
                return "image/gif" in content_type

    @property
    def has_media(self) -> bool:
        """检查当前消息是否包含媒体内容（图片或语音），因此不能再插入新的媒体"""
        return self.image_url is not None or self.voice_data is not None

    async def send_to_telegram(self, chat_id: int, app: Application) -> Optional[Message]:
        """将构造的消息发送到 Telegram"""
        if self.is_empty():
            logging.debug("Constructed message is empty, not sending to Telegram.")
            return None

        logging.info(f"Sending message to Telegram chat {chat_id}: text='{self.text_context}', image_url='{self.image_url}'")

        escaped_sender_name = escape_mdv2(self.sender_name)
        text_with_sender = f"{escaped_sender_name}: {self.text_context}" if self.text_context else f"{escaped_sender_name}:"
        reply_parameters = ReplyParameters(
            message_id=self.reply_tg_message_id
        ) if self.reply_tg_message_id is not None else None

        if self.image_url is not None:
            # 如果有图片，发送图片消息
            if await self._is_animated_image():
                # 如果是动画图片，使用 send_animation
                return await retry_on_network_error(app.bot.send_animation,
                    chat_id=chat_id,
                    animation=self.image_url,
                    caption=text_with_sender,
                    reply_parameters=reply_parameters,
                    parse_mode=telegram.constants.ParseMode.MARKDOWN_V2,
                )
            else:
                # 如果是静态图片，使用 send_photo
                return await retry_on_network_error(app.bot.send_photo,
                    chat_id=chat_id,
                    photo=self.image_url,
                    caption=text_with_sender,
                    reply_parameters=reply_parameters,
                    parse_mode=telegram.constants.ParseMode.MARKDOWN_V2,
                )
        elif self.voice_data is not None:
            # 如果有语音，发送语音消息
            return await retry_on_network_error(app.bot.send_voice,
                chat_id=chat_id,
                voice=self.voice_data,
                caption=text_with_sender,
                reply_parameters=reply_parameters,
                parse_mode=telegram.constants.ParseMode.MARKDOWN_V2,
            )
        else:
            # 如果没有媒体，发送文本消息
            return await retry_on_network_error(app.bot.send_message,
                chat_id=chat_id,
                text=text_with_sender,
                reply_parameters=reply_parameters,
                parse_mode=telegram.constants.ParseMode.MARKDOWN_V2,
            )
        
def render_qq_message_to_plain_markdown(
    messages: str | list[dict[str, Any]]
) -> str:
    """将单条 QQ 消息中的段转换为纯文本 Markdown 格式"""
    if isinstance(messages, str):
        return messages
    reply_text = ""
    for msg in messages:
        match msg.get("type"):
            case "text":
                text = msg.get("data", {}).get("text", "")
                reply_text += escape_mdv2(text)
            case "image":
                # 如果是图片，添加图片链接
                image_url = msg.get("data", {}).get("url", "")
                if image_url:
                    reply_text += f" [图片]({image_url}) "
            case "at":
                at_qq_id_or_all = msg.get("data", {}).get("qq", "")
                if at_qq_id_or_all == "all":
                        reply_text += escape_mdv2("@所有人 ")
                else:
                    try:
                        qq_id = int(at_qq_id_or_all)
                        logging.info(f"Processing QQ ID: {qq_id}")
                        user_mapping = db.get_user_by_id(qq_id, "qq")
                        if user_mapping is not None:
                            # 如果找到绑定的 TG 用户，转换为 TG 的 mention
                            at_name = user_mapping.tg_username or qq_id
                            reply_text += mention_markdown(user_mapping.tg_user_id, at_name, version=2) + " "
                        else:
                            logging.info(f"QQ ID {qq_id} is not bound to any TG user.")
                            # 如果没有绑定，显示 QQ 号
                            reply_text += escape_mdv2(f"@{at_qq_id_or_all} ")
                    except ValueError:
                        logging.error(f"Invalid QQ ID format: {at_qq_id_or_all}, treating as mention")
                        reply_text += escape_mdv2(f"@{at_qq_id_or_all} ")
            case "json":
                reply_text += escape_mdv2("[JSON 卡片]")
            case _:
                reply_text += escape_mdv2(f"[未知类型消息: {msg.get('type', 'unknown')}]")
    return reply_text

def render_qq_message_to_reply_text(
    getmsg_raw_response: dict[str, Any]
) -> str:
    """将 QQ 消息的原始响应转换为回复文本"""
    sender = getmsg_raw_response.get("sender", {})
    sender_name = sender.get("card", "")
    if len(sender_name) == 0:
        sender_name = sender.get("nickname", "[???]")
    messages = getmsg_raw_response["message"]
    reply_text = render_qq_message_to_plain_markdown(messages)
    result = escape_mdv2(f"[回复 {sender_name}: ")
    result += reply_text.strip()
    result += escape_mdv2("]\n")
    return result

def render_qq_forward_message_to_texts(
    messages: list[dict[str, Any]]
):
    """
    将 QQ 转发消息转换为文本

    :param messages: 转发消息列表，类型为 OB11Message[]
    """
    result_text = "[转发消息，完整内容请前往 QQ 查看]\n"
    for msg in messages:
        sender = msg.get("sender", {})
        sender_name: str = sender.get("card", "")
        if len(sender_name) == 0:
            sender_name = sender.get("nickname", "[???]")
        message_content = msg.get("message", [])
        result_text += f"{escape_mdv2(sender_name)}: "
        result_text += render_qq_message_to_plain_markdown(message_content)
        result_text += "\n"
    return result_text

async def qq_message_handler(message: websockets.Data):
    """处理从 QQ 接收到的消息"""
    import json
    message_data = json.loads(message)

    if message_data.get("post_type") != "message" or message_data.get("message_type") != "group":
        return
    group_id = message_data.get("group_id")
    if str(group_id) != QQ_GROUP_ID:
        logging.debug(f"Received message from group {group_id}, but not in {QQ_GROUP_ID}")
        return

    default_tg_chat_id = group_ids[0]
    messages = message_data.get("message", [])
    sender = message_data.get("sender", {})
    sender_name = sender.get("card", "")
    if len(sender_name) == 0:
        sender_name = sender.get("nickname", "[???]")
    # 解析消息
    tg_msg = ConstructedTelegramMessageFromQQ(sender_name=sender_name)
    tg_sent_msgs: list[Optional[Message]] = []
    reply_info_text = ""

    try:
        if isinstance(messages, str):
            # 如果消息是字符串，直接使用
            tg_msg.text_context = escape_mdv2(messages)
        elif isinstance(messages, list):
            for msg in messages:
                match msg.get("type"):
                    case "reply":
                        reply_message_id = msg.get("data", {}).get("id")
                        if reply_message_id is not None:
                            saved_reply = await db.get_by_id(reply_message_id, "qq")
                            if saved_reply is not None:
                                tg_msg.reply_tg_message_id = saved_reply.tg_message_id
                            else:
                                # 找不到映射消息时，尝试获取原 QQ 消息内容
                                try:
                                    qq_replied_to_msg = await qq_get_msg_info(
                                        reply_message_id
                                    )
                                    reply_info_text = render_qq_message_to_reply_text(
                                        qq_replied_to_msg
                                    )
                                except Exception as e:
                                    logging.error(
                                        f"Failed to get reply message info for QQ ID {reply_message_id}: {e}"
                                    )
                                    reply_info_text = escape_mdv2(
                                        "[无法获取回复消息内容]"
                                    )

                    case "at":
                        at_qq_id_or_all = msg.get("data", {}).get("qq", "")
                        if at_qq_id_or_all == "all":
                            tg_msg.text_context += escape_mdv2("@所有人 ")
                        else:
                            try:
                                qq_id = int(at_qq_id_or_all)
                                logging.info(f"Processing QQ ID: {qq_id}")
                                user_mapping = db.get_user_by_id(qq_id, "qq")
                                if user_mapping is not None:
                                    # 如果找到绑定的 TG 用户，转换为 TG 的 mention
                                    at_name = user_mapping.tg_username or qq_id
                                    tg_msg.text_context += mention_markdown(user_mapping.tg_user_id, at_name, version=2) + " "
                                else:
                                    logging.info(f"QQ ID {qq_id} is not bound to any TG user.")
                                    # 如果没有绑定，显示 QQ 号
                                    tg_msg.text_context += escape_mdv2(f"@{at_qq_id_or_all} ")
                            except ValueError:
                                logging.error(f"Invalid QQ ID format: {at_qq_id_or_all}, treating as mention")
                                tg_msg.text_context += escape_mdv2(f"@{at_qq_id_or_all} ")
                    case "text":
                        text = msg.get("data", {}).get("text", "")
                        if len(text) > 0:
                            tg_msg.text_context += escape_mdv2(text)
                    case "image":
                        if tg_msg.has_media:
                            # 如果已经有媒体内容，发送当前消息并重置
                            tg_sent_msgs.append(await tg_msg.send_to_telegram(default_tg_chat_id, app))
                            tg_msg.reset()
                        tg_msg.image_url = msg.get("data", {}).get("url")
                    case "json":
                        json_str = msg.get('data', {}).get("data", "")
                        try:
                            # 解析以进行格式化
                            json_obj = json.loads(json_str)
                            # 处理 JSON 卡片消息，尝试解析其中的信息
                            meta = json_obj["meta"]
                            info_data = meta.get(list(meta.keys())[0], {})
                            title = info_data.get("title", "")
                            URL_KEYS = ["jumpUrl", "qqdocurl", "url"]
                            url = ""
                            for key in URL_KEYS:
                                if key in info_data:
                                    url = info_data[key]
                                    break
                            if len(url) > 0:
                                url = await parse_b23_url_if_any(url)
                            desc = info_data.get("desc", "")
                            tag = info_data.get("tag", "")
                            tg_msg.text_context += escape_mdv2("[卡片分享]\n")
                            if len(title) > 0:
                                tg_msg.text_context += escape_mdv2(f"标题：{title}\n")
                            if len(desc) > 0:
                                tg_msg.text_context += escape_mdv2(f"描述：{desc}\n")
                            if len(tag) > 0:
                                tg_msg.text_context += escape_mdv2(f"标签：{tag}\n")
                            if len(url) > 0:
                                tg_msg.text_context += escape_mdv2(f"链接：{url}\n")
                        except json.JSONDecodeError:
                            tg_msg.text_context += escape_mdv2("[无法解析的 JSON 卡片消息]\n")
                            tg_msg.text_context += f"```json\n{json.dumps(json_obj, indent=2, ensure_ascii=False)}\n```"
                    case "forward":
                        # 转发消息，通常是来自其他 QQ 群的消息
                        forward_list_id: Optional[str] = msg.get("data", {}).get("id")
                        if forward_list_id is None:
                            tg_msg.text_context += escape_mdv2("[无法解析的转发消息 ID]")
                        else:
                            try:
                                forward_msg_data = await qq_get_forward_msg_info(forward_list_id)
                                # 只渲染前 5 条消息
                                tg_msg.text_context += render_qq_forward_message_to_texts(forward_msg_data.get("data", {}).get("messages", [])[:5])
                            except Exception as e:
                                logging.error(f"Failed to get forward message info for ID {forward_list_id}: {e}")
                                tg_msg.text_context += escape_mdv2("[无法获取转发消息内容]")
                                continue

                    case "face":
                        face_id: str = msg.get("data", {}).get("id")
                        tg_msg.text_context += escape_mdv2(f" [表情 {face_id}] ")
                    case "record":
                        if tg_msg.has_media:
                            # 如果已经有媒体内容，发送当前消息并重置
                            tg_sent_msgs.append(await tg_msg.send_to_telegram(default_tg_chat_id, app))
                            tg_msg.reset()
                        voice_url = msg.get("data", {}).get("url")
                        tg_msg.voice_data = await get_converted_voice_with_cache(voice_url, sha3_256(voice_url.encode()).hexdigest())
                    case "video":
                        video_url: str = msg.get("data", {}).get("url")
                        tg_msg.text_context += f" [视频]({video_url}) "
                    case "file":
                        tg_msg.text_context += escape_mdv2(" [文件] ")
                    case _:
                        tg_msg.text_context += escape_mdv2(f"[未知类型消息: {msg.get('type', 'unknown')}] ")

        # 将回复信息添加到消息开头
        if reply_info_text:
            tg_msg.text_context = reply_info_text + tg_msg.text_context

        tg_sent_msgs.append(await tg_msg.send_to_telegram(default_tg_chat_id, app))
    except Exception as e:
        # 捕获解析过程中的任何错误
        err_id = str(uuid.uuid4())
        logging.error("=" * 32)
        error_msg = "Error occurred while processing QQ message -> Telegram\n"
        error_msg += f"Error ID: {err_id}\n"
        error_msg += f"Error message: {escape_mdv2(str(e))}\n"
        error_msg += f"Error traceback: {escape_mdv2(traceback.format_exc())}\n"
        error_msg += f"Original message data: {escape_mdv2(json.dumps(message_data, indent=2, ensure_ascii=False))}"
        logging.error(error_msg)
        logging.error("=" * 32)
        # 尝试发送错误消息到 Telegram
        try:
            tg_sent_msgs.append(
                await app.bot.send_message(
                    chat_id=DEVELOPER_ID,
                    text=error_msg,
                    parse_mode=telegram.constants.ParseMode.MARKDOWN_V2,
                )
            )
        except Exception as e:
            logging.error(f"Still failed to send error log to Telegram: {e}")
            traceback.print_exc()

    # 保存映射关系
    qq_message_id = message_data.get("message_id")
    if qq_message_id is None:
        logging.warning(f"Received message without a valid QQ message ID, skipping mapping: {message_data}")
        return

    tg_message_id = None
    for tg_msg in reversed(tg_sent_msgs):
        if tg_msg is not None:
            tg_message_id = tg_msg.message_id
            break

    if tg_message_id is None:
        logging.warning(f"Failed to send message to Telegram, skipping mapping for QQ message ID {qq_message_id}")
        return

    await db.map_message(
        DB.SavedMessageMapping(
            qq_message_id=int(qq_message_id),
            tg_message_id=tg_message_id,
        )
    )

async def websocket_handler():
    """WebSocket 处理函数，监听 QQ 方面的消息"""

    logging.info(f"Connecting to WebSocket at {NAPCAT_URL}")

    async with websockets.connect(NAPCAT_URL) as websocket:
        websocket_recv = asyncio.create_task(websocket.recv())
        ws_send_task_queue_recv = asyncio.create_task(ws_send_task_queue.get())
        cur_waiting_tasks = {}
        def handle_server_response(maybe_data: websockets.Data):
            """处理服务器响应"""
            try:
                data = json.loads(maybe_data)
                if "echo" in data:
                    echo = data["echo"]
                    if echo in cur_waiting_tasks:
                        completion = cur_waiting_tasks.pop(echo)
                        try:
                            completion.set_result(data)
                        except asyncio.InvalidStateError:
                            logging.warning(f"Completion for echo {echo} already set or cancelled")
                        ws_send_task_queue.task_done()
                    else:
                        logging.warning(f"Received echo {echo} but no task found")
            except json.JSONDecodeError:
                logging.error(f"Failed to decode JSON from WebSocket: {maybe_data}")
                return

        while True:
            done, pending = await asyncio.wait(
                [websocket_recv, ws_send_task_queue_recv],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                if task is websocket_recv:
                    message = task.result()
                    logging.info(f"Received message from WebSocket: {message}")
                    handle_server_response(message)
                    asyncio.create_task(qq_message_handler(message))
                    websocket_recv = asyncio.create_task(websocket.recv()) # 重新创建接收任务
                elif task is ws_send_task_queue_recv:
                    action, params, echo, completion = task.result()
                    logging.info(f"Sending message to WebSocket: {action}, {params}")
                    await websocket.send(
                        json.dumps({
                            "action": action,
                            "params": params,
                            "echo": echo,
                        })
                    )
                    cur_waiting_tasks[echo] = completion
                    ws_send_task_queue_recv = asyncio.create_task(ws_send_task_queue.get()) # 重新创建发送任务

async def parse_b23_url_if_any(url: str) -> str:
    """
    纯工具函数，解析 Bilibili 视频 URL，返回视频的真实地址
    如果 URL 不是 Bilibili 视频链接，则直接返回原始 URL，因此是安全的
    """
    if "b23.tv" not in url: return url
    # 设置 httpx 客户端，不要走代理
    async with httpx.AsyncClient(trust_env=False) as client:
        USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3"
        REFERER = "https://www.bilibili.com/"
        response = await client.head(url, follow_redirects=True, headers={
            "User-Agent": USER_AGENT,
            "Referer": REFERER
        })
        if response.status_code == 200:
            # 返回最终的重定向 URL
            resolved_url = response.url
            # 清除查询参数
            return str(resolved_url.copy_with(params={}))
        else:
            logging.warning(f"Failed to resolve Bilibili URL {url}, status code: {response.status_code}")
            return url

async def retry_on_network_error(func, wait_sec=3, try_count=3, *args, **kwargs):
    """
    纯工具函数，用于在 tg 发送消息时遇到网络错误时进行重试
    """
    for attempt in range(try_count):
        try:
            return await func(*args, **kwargs)
        except telegram.error.NetworkError as e:
            logging.exception(f"Network error on attempt {attempt + 1}: {e}")
            await asyncio.sleep(wait_sec)

app.add_handlers(
    [
        CommandHandler("start", start),
        CommandHandler("bindqq", bind_qq_command),
        MessageHandler(filters.ChatType.GROUPS & (~filters.StatusUpdate.ALL), group_message_handler, block=False),
    ]
)
app.add_error_handler(error_handler)
loop = asyncio.get_event_loop()
loop.create_task(websocket_handler())
app.run_polling(allowed_updates=Update.ALL_TYPES)
db.close()
