# Telegram-QQ-Bot

Telegram-QQ-Bot 是一个机器人，用于将 Telegram 群组和 QQ 群互通。

## 功能特点

* Telegram 群组 <-> QQ 群消息双向转发
* 支持文本消息、图片和组合消息
* 妥善处理 `.webm`、`.tgs` 等媒体格式

## 系统要求

* Python 3.13+
* uv 包管理工具
* 已正确配置 napcat 的正向 WebSocket 服务器连接
* 已安装 FFmpeg（用于处理媒体文件）
  * 如果你不方便安装 FFmpeg，可以从 [FFmpeg Static Builds](https://johnvansickle.com/ffmpeg/) 下载预编译的二进制文件、解压，并将 `ffmpeg` 的路径设置到环境变量 `FFMPEG_EXECUTABLE` 中。

## 安装

### 1. 克隆项目仓库

```shell
git clone git@github.com:w568w/Telegram-QQ-Bot.git
```

### 2. 使用 uv 创建虚拟环境并安装依赖

```shell
uv venv --python 3.13
uv sync
```

## 配置

在项目根目录创建 .env 文件，并设置环境变量。

```shell
cp .env.example .env
```

### 获取 Telegram 群组 ID

如果你的 telegram 客户端不支持显示 channel id 的话，可以这样获取：

1. 将机器人添加到目标群组
2. 在群组中发送一条消息
3. 访问 `https://api.telegram.org/bot<BOT_TOKEN>/getUpdates` 查看 update 中的 chat.id

### 运行

```shell
uv run bot.py
```

## 贡献

欢迎提交 Issues 或 Pull Requests 来改进本项目。

## 许可证

GPLv3, 但是不允许售卖或提供付费托管服务，除非取得所有者另行同意。
