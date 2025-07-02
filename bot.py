from pathlib import Path
from typing import Any, Literal, Optional, ClassVar
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
import logging
import os
import asyncio
from dotenv import load_dotenv
import sqlite3
from dataclasses import dataclass
import websockets
import json
import httpx

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
    
    def __init__(self, db_path: str):
        self.connection = sqlite3.connect(db_path)
        self.connection.row_factory = sqlite3.Row
        with self.connection:
            self.connection.execute(self.SavedMessageMapping.CREATE_TBL)

    def map_message(self, mapping: SavedMessageMapping):
        """将 QQ 消息和 TG 消息进行映射"""
        with self.connection:
            self.connection.execute(
                "INSERT INTO saved_qq_messages (qq_message_id, tg_message_id) VALUES (?, ?)",
                (
                    mapping.qq_message_id,
                    mapping.tg_message_id,
                ),
            )

    def get_by_id(self, id: int, type_: Literal["qq", "tg"]) -> Optional[SavedMessageMapping]:
        """根据 ID 获取映射的消息"""
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
        if row is None:
            return None
        return self.SavedMessageMapping(
            qq_message_id=row["qq_message_id"],
            tg_message_id=row["tg_message_id"],
        )

    def close(self):
        """关闭数据库连接"""
        self.connection.close()

db = DB(db_path)

app = ApplicationBuilder().token(bot_token).build()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    assert update.message is not None
    await update.message.reply_text("Hello!")

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
    
    saved_reply_to = None
    reply_info_text = ""
    if message.reply_to_message is not None:
        saved_reply_to = db.get_by_id(message.reply_to_message.message_id, "tg")
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
    if message.text is not None:
        # 处理文本消息
        single_qq_msg.append(
            {
                "type": "text",
                "data": {
                    "text": message.text,
                }
            }
        )

    qq_message_id = await qq_send_msg_in_group(single_qq_msg)
    # 保存映射关系
    db.map_message(
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

from lottie.importers import importers
from lottie.exporters import exporters
from lottie.parsers.baseporter import Baseporter
tgs_importer: Baseporter = importers.get_from_extension("tgs")
gif_exporter: Baseporter = exporters.get_from_extension("gif")

async def get_converted_image_with_cache(file_obj: telegram.File, file_path: str, file_unique_id: str) -> bytes:
    """通过 unique_id 获取转码后的图片，如果缓存不存在则下载并转码"""
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

@dataclass
class ConstructedTelegramMessageFromQQ:
    # 元信息
    sender_name: str
    reply_tg_message_id: Optional[int] = None
    # 消息内容
    text_context: str = ""
    image_url: Optional[str] = None

    def is_empty(self) -> bool:
        """检查当前消息内容是否为空"""
        return len(self.text_context.strip()) == 0 and self.image_url is None
    
    def reset(self):
        """重置消息内容为空，但不重置回复 ID、发送者名称等元数据"""
        self.text_context = ""
        self.image_url = None

    async def _is_animated_image(self) -> bool:
        """检查当前消息是否包含动画图片"""
        if self.image_url is None: return False
        if self.image_url.lower().endswith((".gif", ".webm", ".tgs")): return True
        # 请求网络，通过 header Content-Type 判断是否为动画图片
        async with httpx.AsyncClient() as client:
            async with client.stream("GET", self.image_url, timeout=5) as response:
                content_type = response.headers.get("Content-Type", "").lower()
                return "image/gif" in content_type

    async def send_to_telegram(self, chat_id: int, app: Application) -> Optional[Message]:
        """将构造的消息发送到 Telegram"""
        if self.is_empty():
            logging.debug("Constructed message is empty, not sending to Telegram.")
            return None

        logging.info(f"Sending message to Telegram chat {chat_id}: text='{self.text_context}', image_url='{self.image_url}'")

        text_with_sender = f"{self.sender_name}: {self.text_context}" if self.text_context else f"{self.sender_name}:"
        reply_parameters = ReplyParameters(
            message_id=self.reply_tg_message_id
        ) if self.reply_tg_message_id is not None else None

        if self.image_url is not None:
            # 如果有图片，发送图片消息
            if await self._is_animated_image():
                # 如果是动画图片，使用 send_animation
                return await app.bot.send_animation(
                    chat_id=chat_id,
                    animation=self.image_url,
                    caption=text_with_sender,
                    reply_parameters=reply_parameters,
                )
            else:
                # 如果是静态图片，使用 send_photo
                return await app.bot.send_photo(
                    chat_id=chat_id,
                    photo=self.image_url,
                    caption=text_with_sender,
                    reply_parameters=reply_parameters,
                )
        else:
            # 如果没有图片，发送文本消息
            return await app.bot.send_message(
                chat_id=chat_id,
                text=text_with_sender,
                reply_parameters=reply_parameters,
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
                reply_text += text
            case "image":
                # 如果是图片，添加图片链接
                image_url = msg.get("data", {}).get("url", "")
                if image_url:
                    reply_text += f" [图片]({image_url}) "
            case "at":
                at_qq_id_or_all = msg.get("data", {}).get("qq", "")
                reply_text += f"@{at_qq_id_or_all} "
            case "json":
                reply_text += "[JSON 卡片]"
            case _:
                reply_text += f"[未知类型消息: {msg.get('type', 'unknown')}]"
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
    return f"[回复 {sender_name}: {reply_text.strip()}]\n"

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
        result_text += f"{sender_name}: "
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
    
    if isinstance(messages, str):
        # 如果消息是字符串，直接使用
        tg_msg.text_context = messages
    elif isinstance(messages, list):
        for msg in messages:
            match msg.get("type"):
                case "reply":
                    reply_message_id = msg.get("data", {}).get("id")
                    if reply_message_id is not None:
                        saved_reply = db.get_by_id(reply_message_id, "qq")
                        if saved_reply is not None:
                            tg_msg.reply_tg_message_id = saved_reply.tg_message_id
                        else:
                            # 找不到映射消息时，尝试获取原 QQ 消息内容
                            try:
                                qq_replied_to_msg = await qq_get_msg_info(reply_message_id)
                                reply_info_text = render_qq_message_to_reply_text(qq_replied_to_msg)
                            except Exception as e:
                                logging.error(f"Failed to get reply message info for QQ ID {reply_message_id}: {e}")
                                reply_info_text = "[无法获取回复消息内容]"

                case "at":
                    at_qq_id_or_all = msg.get("data", {}).get("qq", "")
                    tg_msg.text_context += f"@{at_qq_id_or_all} "
                case "text":
                    text = msg.get("data", {}).get("text", "")
                    if len(text) > 0:
                        tg_msg.text_context += text
                case "image":
                    if tg_msg.image_url is not None:
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
                        tg_msg.text_context += "[卡片分享]\n"
                        if len(title) > 0:
                            tg_msg.text_context += f"标题：{title}\n"
                        if len(desc) > 0:
                            tg_msg.text_context += f"描述：{desc}\n"
                        if len(tag) > 0:
                            tg_msg.text_context += f"标签：{tag}\n"
                        if len(url) > 0:
                            tg_msg.text_context += f"链接：{url}\n"
                    except json.JSONDecodeError:
                        tg_msg.text_context += f"[无法解析的 JSON 卡片]\n```json\n{json.dumps(json_obj, indent=2, ensure_ascii=False)}\n```"
                case "forward":
                    # 转发消息，通常是来自其他 QQ 群的消息
                    forward_list_id: Optional[str] = msg.get("data", {}).get("id")
                    if forward_list_id is None:
                        tg_msg.text_context += "[无法解析的转发消息 ID]"
                    else:
                        try:
                            forward_msg_data = await qq_get_forward_msg_info(forward_list_id)
                            # 只渲染前 5 条消息
                            tg_msg.text_context += render_qq_forward_message_to_texts(forward_msg_data.get("data", {}).get("messages", [])[:5])
                        except Exception as e:
                            logging.error(f"Failed to get forward message info for ID {forward_list_id}: {e}")
                            tg_msg.text_context += "[无法获取转发消息内容]"
                            continue
                    
                case "face":
                    face_id: str = msg.get("data", {}).get("id")
                    tg_msg.text_context += f" [表情 {face_id}] "
    
    # 将回复信息添加到消息开头
    if reply_info_text:
        tg_msg.text_context = reply_info_text + tg_msg.text_context
        
    tg_sent_msgs.append(await tg_msg.send_to_telegram(default_tg_chat_id, app))
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

    db.map_message(
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

app.add_handlers(
    [
        CommandHandler("start", start),
        MessageHandler(filters.ChatType.GROUPS & (~filters.StatusUpdate.ALL), group_message_handler, block=False),
    ]
)
loop = asyncio.get_event_loop()
loop.create_task(websocket_handler())
app.run_polling(allowed_updates=Update.ALL_TYPES)
db.close()
