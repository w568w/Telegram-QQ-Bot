# Telegram-QQ-Bot

Telegram-QQ-Bot 是一个机器人，用于将 Telegram 群组和 QQ 群互通。

## 1. 功能特点

* Telegram 群组 <-> QQ 群消息双向转发
* 支持 Telegram 的文本消息、图片/动画和组合消息
* 支持 QQ 的聊天记录合并转发预览
* 支持双向 Reply
* 支持双向 @ 提及绑定（使用 `/bindqq <QQ 号>` 命令来绑定 Telegram 用户和 QQ 号）
* 妥善处理 `.webm`、`.tgs` 等媒体格式

## 2. 系统要求

* [uv](https://docs.astral.sh/uv/) 包管理工具
* 已正确配置 [NapCat](https://napneko.github.io/) 的正向 WebSocket 服务器连接
* 已安装 FFmpeg（用于处理媒体文件）
  * 如果你不方便安装 FFmpeg，可以从 [FFmpeg Static Builds](https://johnvansickle.com/ffmpeg/) 下载预编译的二进制文件、解压，并将 `ffmpeg` 的路径设置到环境变量 `FFMPEG_EXECUTABLE` 中。

## 3. 安装

### 3.1. 克隆项目仓库

```shell
git clone git@github.com:w568w/Telegram-QQ-Bot.git
```

### 3.2. 使用 uv 创建虚拟环境并安装依赖

```shell
uv venv --python 3.13
uv sync
```

## 4. 配置

在项目根目录创建 `.env` 文件，并设置环境变量。

```shell
cp .env.example .env
```

### 4.1. 获取 Telegram 群组 ID

如果你的 Telegram 客户端不支持显示群组 ID 的话，可以这样获取：

1. 将机器人添加到目标群组
2. 在群组中发送一条消息
3. 访问 `https://api.telegram.org/bot<BOT_TOKEN>/getUpdates` 查看 `update` 中的 `chat.id`

### 4.2. 运行

```shell
uv run bot.py
```

## 贡献

欢迎提交 Issues 或 Pull Requests 来改进本项目。

## 许可证

GPLv3, 但是不允许售卖或提供付费托管服务，除非取得所有者另行同意。
