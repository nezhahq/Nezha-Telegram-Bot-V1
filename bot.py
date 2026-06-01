import asyncio
import logging
import math
import time
from datetime import datetime, timezone
from dateutil import parser
from dotenv import load_dotenv
import pytz
import os
import secrets

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

from nezha_api import NezhaAPI
from database import Database
from llm_assistant import (
    load_pending_execution_for_confirmation,
    redact_secret,
    run_assistant_message,
    serialize_pending_execution,
)
from server_utils import (
    attach_group_ids,
    compact_server_status,
    filter_servers,
    paginate_servers,
    sort_servers_for_display,
)

# 配置日志
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

load_dotenv()

# 定义常量和配置
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DATABASE_PATH = "db/users.db"
# 从环境变量读取流量告警阈值 (GB)，默认为 0 (不告警)
UPLOAD_ALERT_THRESHOLD_GB = float(os.getenv("UPLOAD_ALERT_THRESHOLD_GB", 0))
DOWNLOAD_ALERT_THRESHOLD_GB = float(os.getenv("DOWNLOAD_ALERT_THRESHOLD_GB", 0))

UPLOAD_ALERT_THRESHOLD_BYTES = UPLOAD_ALERT_THRESHOLD_GB * (1024**3)
DOWNLOAD_ALERT_THRESHOLD_BYTES = DOWNLOAD_ALERT_THRESHOLD_GB * (1024**3)


# 定义阶段
BIND_USERNAME, BIND_PASSWORD, BIND_DASHBOARD, BIND_ALIAS = range(4)
SEARCH_SERVER = range(1)

# 群组消息存活时间（秒）
GROUP_MESSAGE_LIFETIME = 180  # 3分钟

# 初始化数据库
db = Database(DATABASE_PATH)


def create_api(dashboard):
    return NezhaAPI(
        dashboard["dashboard_url"],
        dashboard.get("username"),
        dashboard.get("password"),
        auth_type=dashboard.get("auth_type", "password"),
        api_token=dashboard.get("api_token"),
    )


def dashboard_auth_text(dashboard):
    if dashboard.get("auth_type") == "token":
        return "API Token"
    if dashboard.get("api_token"):
        return "用户名/密码（API Token 已绑定）"
    return "用户名/密码"


# 添加获取当前时间函数
def get_localized_time_string():
    tz_str = os.environ.get("TZ")

    if tz_str:
        try:
            tz = pytz.timezone(tz_str)
            localized_time = datetime.now(tz)
            return localized_time.strftime("%Y-%m-%d %H:%M:%S %Z%z")
        except pytz.exceptions.UnknownTimeZoneError:
            return "Error: Invalid Time Zone in TZ environment variable."
    else:
        utc_time = datetime.utcnow()
        return utc_time.strftime("%Y-%m-%d %H:%M:%S UTC")


# 添加 format_bytes 函数
def format_bytes(size_in_bytes):
    if size_in_bytes == 0:
        return "0B"
    units = ["B", "KB", "MB", "GB", "TB"]
    power = int(math.floor(math.log(size_in_bytes, 1024)))
    power = min(power, len(units) - 1)  # 防止超过单位列表的范围
    size = size_in_bytes / (1024**power)
    formatted_size = f"{size:.2f}{units[power]}"
    return formatted_size


def is_online(server):
    """根据last_active判断服务器是否在线，如果最后活跃时间在10秒内则为在线。"""
    now_utc = datetime.now(timezone.utc)
    last_active_str = server.get("last_active")
    if not last_active_str:
        return False
    try:
        last_active_dt = parser.isoparse(last_active_str)
    except ValueError:
        return False
    last_active_utc = last_active_dt.astimezone(timezone.utc)
    diff = now_utc - last_active_utc
    is_on = diff.total_seconds() < 10
    logger.info(
        "Checking online: diff=%s now=%s last=%s is_online=%s",
        diff,
        now_utc,
        last_active_utc,
        is_on,
    )
    return is_on


# 添加 IP 地址掩码函数
def mask_ipv4(ipv4_address):
    if ipv4_address == "未知" or ipv4_address == "❌":
        return ipv4_address
    parts = ipv4_address.split(".")
    if len(parts) != 4:
        return ipv4_address  # 非法的 IPv4 地址，直接返回
    # 将后两部分替换为 'xx'
    masked_ip = f"{parts[0]}.{parts[1]}.xx.xx"
    return masked_ip


def mask_ipv6(ipv6_address):
    if ipv6_address == "未知" or ipv6_address == "❌":
        return ipv6_address
    parts = ipv6_address.split(":")
    if len(parts) < 3:
        return ipv6_address  # 非法的 IPv6 地址，直接返回
    # 只显示前两个部分，后面用 'xx' 替代
    masked_ip = ":".join(parts[:2]) + ":xx:xx:xx:xx"
    return masked_ip


async def delete_message_later(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int
):
    """
    延迟删除消息的任务
    """
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        logger.warning(f"删除消息失败: {e}")


async def send_message_with_auto_delete(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, **kwargs
):
    """
    发送消息并在群组中自动设置延迟删除
    """
    message = await update.message.reply_text(text, **kwargs)

    # 如果是群组消息，设置定时删除
    if update.effective_chat.type in ["group", "supergroup"]:
        # 延迟5秒删除原始命令消息
        context.job_queue.run_once(
            lambda ctx: delete_message_later(
                ctx, update.message.chat_id, update.message.message_id
            ),
            5,  # 5秒后删除原始命令
        )

        # 设置定时删除回复的消息
        context.job_queue.run_once(
            lambda ctx: delete_message_later(ctx, message.chat_id, message.message_id),
            GROUP_MESSAGE_LIFETIME,
        )

    return message


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_message_with_auto_delete(
        update,
        context,
        "欢迎使用 Nezha 监控机器人！\n请使用 /bind 命令绑定您的账号。\n请注意，使用公共机器人有安全风险，用户名密码将会被记录用以鉴权，解绑删除。",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_message_with_auto_delete(
        update,
        context,
        """可用命令：
/bind - 绑定账号
/unbind - 解绑账号
/dashboard - 管理面板
/overview - 查看服务器状态总览
/server - 查看单台服务器状态
/servers - 服务器搜索入口
/cron - 执行计划任务
/services - 查看服务状态总览
/chat - LLM 运维助手
/mcp - 面板设置 / MCP 快捷入口
/help - 获取帮助
        """,
    )


async def bind_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 检查当前对话类型
    if update.effective_chat.type != "private":
        await send_message_with_auto_delete(
            update, context, "请与机器人私聊进行绑定操作，\n避免机密信息泄露。"
        )
        return ConversationHandler.END

    await update.message.reply_text("请输入您的用户名，或直接发送 API Token（nzp_...）：")
    return BIND_USERNAME


async def bind_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = update.message.text.strip()
    if value.startswith("nzp_"):
        context.user_data["auth_type"] = "token"
        context.user_data["api_token"] = value
        context.user_data["username"] = ""
        context.user_data["password"] = ""
        await update.message.reply_text(
            "请输入您的 Dashboard 地址（例如：https://nezha.example.com）："
        )
        return BIND_DASHBOARD
    context.user_data["auth_type"] = "password"
    context.user_data["username"] = value
    await update.message.reply_text("请输入您的密码：")
    return BIND_PASSWORD


async def bind_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["password"] = update.message.text.strip()
    # 在私聊中直接使用 reply_text
    await update.message.reply_text(
        "请输入您的 Dashboard 地址（例如：https://nezha.example.com）："
    )
    return BIND_DASHBOARD


async def bind_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dashboard_url = update.message.text.strip()
    context.user_data["dashboard_url"] = dashboard_url
    # 在私聊中直接使用 reply_text
    await update.message.reply_text("请为这个面板设置一个别名（如：主面板、备用等）：")
    return BIND_ALIAS


async def bind_alias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    alias = update.message.text.strip()
    context.user_data["alias"] = alias
    telegram_id = update.effective_user.id
    username = context.user_data["username"]
    password = context.user_data["password"]
    dashboard_url = context.user_data["dashboard_url"]
    auth_type = context.user_data.get("auth_type", "password")
    api_token = context.user_data.get("api_token")

    # 测试连接
    try:
        api = NezhaAPI(
            dashboard_url,
            username,
            password,
            auth_type=auth_type,
            api_token=api_token,
        )
        await api.validate_auth()
        await api.close()
    except Exception as e:
        await update.message.reply_text(f"绑定失败：{e}\n请检查您的信息并重新绑定。")
        return ConversationHandler.END

    # 保存到数据库
    await db.add_user(
        telegram_id,
        username,
        password,
        dashboard_url,
        alias,
        auth_type=auth_type,
        api_token=api_token,
    )
    await update.message.reply_text("绑定成功！您现在可以使用机器人的功能了。")
    return ConversationHandler.END


async def unbind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dashboards = await db.get_all_dashboards(update.effective_user.id)
    if not dashboards:
        await send_message_with_auto_delete(update, context, "您尚未绑定任何面板。")
        return

    keyboard = []
    # 添加每个 dashboard 的解绑选项
    for dashboard in dashboards:
        default_mark = "（默认）" if dashboard["is_default"] else ""
        button_text = f"解绑 {dashboard['alias']}{default_mark}"
        keyboard.append(
            [
                InlineKeyboardButton(
                    button_text, callback_data=f"unbind_{dashboard['id']}"
                )
            ]
        )

    # 添加解绑所有的选项
    if len(dashboards) > 1:
        keyboard.append(
            [InlineKeyboardButton("解绑所有面板", callback_data="unbind_all")]
        )

    reply_markup = InlineKeyboardMarkup(keyboard)
    await send_message_with_auto_delete(
        update, context, "请选择要解绑的面板：", reply_markup=reply_markup
    )


async def overview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await db.get_user(update.effective_user.id)
    if not user:
        await send_message_with_auto_delete(
            update, context, "请先使用 /bind 命令绑定您的账号。"
        )
        return

    api = create_api(user)
    try:
        data = await api.get_overview()
    except Exception as e:
        await send_message_with_auto_delete(update, context, f"获取数据失败：{e}")
        await api.close()
        return

    if data and data.get("success"):
        servers = data["data"]
        online_servers = 0
        offline_servers_info = []
        traffic_alerts = []
        total_servers = len(servers)
        total_mem = 0
        used_mem = 0
        total_swap = 0
        used_swap = 0
        total_disk = 0
        used_disk = 0
        net_in_speed = 0
        net_out_speed = 0
        net_in_transfer = 0
        net_out_transfer = 0

        for s in servers:
            server_name = s.get("name", "未知")
            if is_online(s):
                online_servers += 1
            else:
                last_active_str = s.get("last_active")
                last_active_formatted = "未知时间"
                if last_active_str:
                    try:
                        # 解析时间并转换为本地时区（如果设置了TZ）
                        last_active_dt_utc = parser.isoparse(
                            last_active_str
                        ).astimezone(timezone.utc)
                        tz_str = os.environ.get("TZ")
                        if tz_str:
                            try:
                                target_tz = pytz.timezone(tz_str)
                                last_active_dt_local = last_active_dt_utc.astimezone(
                                    target_tz
                                )
                                last_active_formatted = last_active_dt_local.strftime(
                                    "%Y-%m-%d %H:%M:%S %Z%z"
                                )
                            except pytz.exceptions.UnknownTimeZoneError:
                                last_active_formatted = last_active_dt_utc.strftime(
                                    "%Y-%m-%d %H:%M:%S UTC"
                                )
                        else:
                            last_active_formatted = last_active_dt_utc.strftime(
                                "%Y-%m-%d %H:%M:%S UTC"
                            )
                    except ValueError:
                        last_active_formatted = "无效时间格式"
                offline_servers_info.append(
                    f"服务器 **{server_name}** 离线，最后在线: {last_active_formatted}"
                )

            # 累加统计信息
            if s.get("host"):
                total_mem += s["host"].get("mem_total", 0)
                total_swap += s["host"].get("swap_total", 0)
                total_disk += s["host"].get("disk_total", 0)
            if s.get("state"):
                used_mem += s["state"].get("mem_used", 0)
                used_swap += s["state"].get("swap_used", 0)
                used_disk += s["state"].get("disk_used", 0)
                net_in_speed += s["state"].get("net_in_speed", 0)
                net_out_speed += s["state"].get("net_out_speed", 0)
                current_net_in = s["state"].get("net_in_transfer", 0)
                current_net_out = s["state"].get("net_out_transfer", 0)
                net_in_transfer += current_net_in
                net_out_transfer += current_net_out

                # 检查流量阈值
                if (
                    UPLOAD_ALERT_THRESHOLD_BYTES > 0
                    and current_net_out > UPLOAD_ALERT_THRESHOLD_BYTES
                ):
                    traffic_alerts.append(
                        f"服务器 **{server_name}** 上行流量超限: {format_bytes(current_net_out)} / {format_bytes(UPLOAD_ALERT_THRESHOLD_BYTES)}"
                    )
                if (
                    DOWNLOAD_ALERT_THRESHOLD_BYTES > 0
                    and current_net_in > DOWNLOAD_ALERT_THRESHOLD_BYTES
                ):
                    traffic_alerts.append(
                        f"服务器 **{server_name}** 下行流量超限: {format_bytes(current_net_in)} / {format_bytes(DOWNLOAD_ALERT_THRESHOLD_BYTES)}"
                    )

        transfer_ratio = (
            (net_out_transfer / net_in_transfer * 100) if net_in_transfer else 0
        )

        response = f"""📊 **统计信息**
===========================
**服务器数量**： {total_servers}
**在线服务器**： {online_servers}
**内存**： {used_mem / total_mem * 100 if total_mem else 0:.1f}% [{format_bytes(used_mem)}/{format_bytes(total_mem)}]
**交换**： {used_swap / total_swap * 100 if total_swap else 0:.1f}% [{format_bytes(used_swap)}/{format_bytes(total_swap)}]
**磁盘**： {used_disk / total_disk * 100 if total_disk else 0:.1f}% [{format_bytes(used_disk)}/{format_bytes(total_disk)}]
**下行速度**： ↓{format_bytes(net_in_speed)}/s
**上行速度**： ↑{format_bytes(net_out_speed)}/s
**下行流量**： ↓{format_bytes(net_in_transfer)}
**上行流量**： ↑{format_bytes(net_out_transfer)}
**流量对等性**： {transfer_ratio:.1f}%
"""
        # 添加离线设备信息
        if offline_servers_info:
            response += "\n\n🔌 **离线设备**\n===========================\n"
            response += "\n".join(offline_servers_info)

        # 添加流量告警信息
        if traffic_alerts:
            response += "\n\n🚨 **流量告警**\n===========================\n"
            response += "\n".join(traffic_alerts)

        response += f"\n\n**更新于**： {get_localized_time_string()}"

        keyboard = [
            [InlineKeyboardButton("刷新", callback_data="refresh_overview")],
            [InlineKeyboardButton("切换面板", callback_data="dashboard_back")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await send_message_with_auto_delete(
            update, context, response, parse_mode="Markdown", reply_markup=reply_markup
        )
    else:
        await send_message_with_auto_delete(update, context, "获取服务器信息失败。")
    await api.close()


async def server_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await db.get_user(update.effective_user.id)
    if not user:
        await send_message_with_auto_delete(
            update, context, "请先使用 /bind 命令绑定您的账号。"
        )
        return

    if context.args:
        await send_server_search_results(
            update,
            context,
            " ".join(context.args),
            page=1,
        )
        return ConversationHandler.END

    await send_message_with_auto_delete(
        update, context, "请输入要查询的服务器名称（支持模糊搜索）："
    )
    return SEARCH_SERVER


async def search_server(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_text = update.message.text.strip()
    await send_server_search_results(update, context, query_text, page=1)
    return ConversationHandler.END


async def servers_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await db.get_user(update.effective_user.id)
    if not user:
        await send_message_with_auto_delete(
            update, context, "请先使用 /bind 命令绑定您的账号。"
        )
        return
    if context.args:
        await send_server_search_results(update, context, " ".join(context.args), page=1)
        return
    keyboard = [
        [InlineKeyboardButton("搜索", callback_data="servers_mode_search")],
        [
            InlineKeyboardButton("在线", callback_data="servers_filter_online"),
            InlineKeyboardButton("离线", callback_data="servers_filter_offline"),
        ],
        [
            InlineKeyboardButton("高负载", callback_data="servers_filter_load"),
            InlineKeyboardButton("高流量", callback_data="servers_filter_traffic"),
        ],
        [InlineKeyboardButton("返回主菜单", callback_data="dashboard_back")],
    ]
    await send_message_with_auto_delete(
        update,
        context,
        "请选择服务器查看方式：",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def send_server_search_results(update, context, query_text="", page=1, status="all", high_load=False, high_traffic=False):
    user = await db.get_user(update.effective_user.id)
    api = create_api(user)
    try:
        data = await api.get_servers()
        servers = data.get("data", []) if data and data.get("success") else []
        results = filter_servers(
            servers,
            query=query_text,
            status=status,
            high_load=high_load,
            high_traffic=high_traffic,
        )
    except Exception as e:
        await send_message_with_auto_delete(update, context, f"搜索失败：{e}")
        await api.close()
        return
    await api.close()

    if not results:
        await send_message_with_auto_delete(update, context, "未找到匹配的服务器。")
        return

    context.user_data["server_search"] = {
        "query": query_text,
        "status": status,
        "high_load": high_load,
        "high_traffic": high_traffic,
    }
    text, reply_markup = build_server_results_message(results, page)
    await send_message_with_auto_delete(update, context, text, reply_markup=reply_markup)


def build_server_results_message(results, page):
    page_data = paginate_servers(sort_servers_for_display(results), page=page, per_page=8)
    keyboard = []
    for server in page_data["items"]:
        label = f"{server.get('name', '未知')} | {compact_server_status(server)}"
        keyboard.append([InlineKeyboardButton(label[:60], callback_data=f"server_detail_{server['id']}")])
    nav = []
    if page_data["page"] > 1:
        nav.append(InlineKeyboardButton("上一页", callback_data=f"srvpg_{page_data['page'] - 1}"))
    if page_data["page"] < page_data["total_pages"]:
        nav.append(InlineKeyboardButton("下一页", callback_data=f"srvpg_{page_data['page'] + 1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("返回", callback_data="servers_entry")])
    text = f"请选择服务器（{page_data['page']}/{page_data['total_pages']}，共 {page_data['total']} 台）："
    return text, InlineKeyboardMarkup(keyboard)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    if data.startswith("unbind_"):
        if data == "unbind_all":
            await db.delete_user(query.from_user.id)
            await edit_message_with_auto_delete(
                query, "已解绑所有面板，您可以使用 /bind 重新绑定。"
            )
        else:
            dashboard_id = int(data.split("_")[-1])
            # 获取当前面板信息，用于判断是否是默认面板
            dashboards = await db.get_all_dashboards(query.from_user.id)
            current_dashboard = next(
                (d for d in dashboards if d["id"] == dashboard_id), None
            )
            was_default = current_dashboard and current_dashboard["is_default"]

            has_remaining = await db.delete_dashboard(query.from_user.id, dashboard_id)

            if not has_remaining:
                await edit_message_with_auto_delete(
                    query, "已解绑最后一个面板，您可以使用 /bind 重新绑定。"
                )
            else:
                # 新面板列表
                dashboards = await db.get_all_dashboards(query.from_user.id)
                keyboard = []

                # 如果解绑的是默认面板，显示新的默认面板提示
                if was_default:
                    new_default = next((d for d in dashboards if d["is_default"]), None)
                    message = f"已解绑面板，新的默认面板已设置为：{new_default['alias']}\n\n请选择要解绑的面板："
                else:
                    message = "请选择要解绑的面板："

                for dashboard in dashboards:
                    default_mark = "（默认）" if dashboard["is_default"] else ""
                    button_text = f"解绑 {dashboard['alias']}{default_mark}"
                    keyboard.append(
                        [
                            InlineKeyboardButton(
                                button_text, callback_data=f"unbind_{dashboard['id']}"
                            )
                        ]
                    )

                if len(dashboards) > 1:
                    keyboard.append(
                        [
                            InlineKeyboardButton(
                                "解绑所有面板", callback_data="unbind_all"
                            )
                        ]
                    )

                reply_markup = InlineKeyboardMarkup(keyboard)
                await edit_message_with_auto_delete(
                    query, message, reply_markup=reply_markup
                )
        return

    elif data.startswith("set_default_"):
        dashboard_id = int(data.split("_")[-1])
        dashboards = await db.get_all_dashboards(query.from_user.id)
        selected_dashboard = next(
            (d for d in dashboards if d["id"] == dashboard_id), None
        )

        if not selected_dashboard:
            await query.answer("未找到该面板", show_alert=True)
            return

        if selected_dashboard["is_default"]:
            await query.answer("这已经是默认面板了", show_alert=True)
            return

        # 直接切换默认面板
        await db.set_default_dashboard(query.from_user.id, dashboard_id)

        # 更新面板列表
        dashboards = await db.get_all_dashboards(query.from_user.id)
        keyboard = []
        for dashboard in dashboards:
            default_mark = "（当前默认）" if dashboard["is_default"] else ""
            button_text = f"{dashboard['alias']}{default_mark}"
            keyboard.append(
                [
                    InlineKeyboardButton(
                        button_text, callback_data=f"set_default_{dashboard['id']}"
                    ),
                    InlineKeyboardButton("设置", callback_data=f"settings_{dashboard['id']}")
                ]
            )

        reply_markup = InlineKeyboardMarkup(keyboard)
        await edit_message_with_auto_delete(
            query, "您的面板列表：", reply_markup=reply_markup
        )
        return

    elif data.startswith("settings_"):
        if query.message.chat.type != "private":
            await query.answer("请在私聊中设置 MCP/LLM。", show_alert=True)
            return
        await query.answer()
        parts = data.split("_")
        action = "view" if len(parts) == 2 else parts[1]
        dashboard_id = int(parts[-1])
        dashboard_row = await db.get_dashboard(query.from_user.id, dashboard_id)
        if not dashboard_row:
            await edit_message_with_auto_delete(query, "未找到该面板。")
            return
        if action == "view":
            llm_config = await db.get_llm_config(query.from_user.id, dashboard_id)
            auth_text = dashboard_auth_text(dashboard_row)
            token_text = redact_secret(dashboard_row.get("api_token"))
            llm_text = "已启用" if llm_config and llm_config.get("enabled") and llm_config.get("api_key") else "未配置"
            text = (
                f"面板设置：{dashboard_row.get('alias') or 'NEZHA'}\n"
                f"URL：{dashboard_row.get('dashboard_url')}\n"
                f"认证：{auth_text}\n"
                f"API Token：{token_text}\n"
                f"LLM/MCP 助手：{llm_text}\n\n"
                "提示：完整使用建议授予 nezha:inventory:read、nezha:service:read、nezha:cron:read、nezha:cron:exec、nezha:server:exec；需要 MCP 读取单机详情时再加 nezha:server:read，并限制服务器白名单。"
            )
            keyboard = [
                [InlineKeyboardButton("测试连接", callback_data=f"settings_test_{dashboard_id}")],
                [InlineKeyboardButton("绑定/更换 API Token", callback_data=f"settings_token_{dashboard_id}")],
                [InlineKeyboardButton("配置 LLM", callback_data=f"settings_llm_{dashboard_id}")],
                [InlineKeyboardButton("返回面板列表", callback_data="dashboard_back")],
            ]
            await edit_message_with_auto_delete(query, text, reply_markup=InlineKeyboardMarkup(keyboard))
            return
        if action == "test":
            api = create_api(dashboard_row)
            try:
                ok = await api.validate_auth()
            except Exception as e:
                await edit_message_with_auto_delete(query, f"连接失败：{e}")
                await api.close()
                return
            await api.close()
            await edit_message_with_auto_delete(query, "连接测试成功。" if ok else "连接测试失败。")
            return
        if action == "token":
            context.user_data["settings_waiting"] = {"type": "token", "dashboard_id": dashboard_id}
            await edit_message_with_auto_delete(query, "请发送新的 API Token（nzp_...）。")
            return
        if action == "llm":
            context.user_data["settings_waiting"] = {"type": "llm", "dashboard_id": dashboard_id}
            await edit_message_with_auto_delete(
                query,
                "请按三行发送 LLM 配置：\nbase_url\nmodel\napi_key\n\n例如：\nhttps://api.openai.com/v1\ngpt-4o-mini\nsk-...",
            )
            return
        await edit_message_with_auto_delete(query, "未知设置操作。")
        return

    elif data.startswith("exec_cancel_"):
        await query.answer()
        execution_id = data.split("exec_cancel_", 1)[1]
        row = await db.get_pending_execution(execution_id)
        if not row or row["telegram_id"] != query.from_user.id:
            await edit_message_with_auto_delete(query, "确认已过期或无权限。")
            return
        await db.delete_pending_execution(execution_id)
        await edit_message_with_auto_delete(query, "操作已取消。")
        return

    elif data.startswith("exec_confirm_"):
        if query.message.chat.type != "private":
            await query.answer("命令执行只允许在私聊中确认。", show_alert=True)
            return
        await query.answer()
        execution_id = data.split("exec_confirm_", 1)[1]
        loaded = await load_pending_execution_for_confirmation(
            db, execution_id, query.from_user.id
        )
        if loaded.get("error"):
            await edit_message_with_auto_delete(query, loaded["error"])
            return
        pending = loaded["pending"]
        dashboard_row = await db.get_dashboard(query.from_user.id, pending.dashboard_id)
        if not dashboard_row:
            await edit_message_with_auto_delete(query, "未找到面板绑定。")
            return
        if not dashboard_row.get("api_token"):
            await edit_message_with_auto_delete(query, "MCP 命令执行需要 API Token 认证，请先在设置页绑定 nzp_ Token。")
            return
        api = create_api(dashboard_row)
        lines = [f"开始执行：{pending.command}", ""]
        try:
            for server_id, server_name in zip(pending.server_ids, pending.server_names):
                try:
                    result = await api.execute_command_mcp(server_id, pending.command)
                    output = api.format_mcp_exec_output(result)
                    first_line = output.strip().splitlines()[0] if output.strip() else "已完成"
                    lines.append(f"✅ {server_name}: {first_line[:80]}")
                except Exception as e:
                    lines.append(f"❌ {server_name}: {str(e)[:120]}")
        finally:
            await api.close()
            await db.delete_pending_execution(execution_id)
        await edit_message_with_auto_delete(query, "\n".join(lines))
        return

    user = await db.get_user(query.from_user.id)
    if not user:
        await query.answer("请先使用 /bind 命令绑定您的账号。", show_alert=True)
        return

    # 实现刷新频率限制
    last_refresh_time = context.user_data.get("last_refresh_time", 0)
    current_time = time.time()
    if data.startswith("refresh_"):
        if current_time - last_refresh_time < 1:
            await query.answer("刷新太频繁，请稍后再试。", show_alert=True)
            return
        else:
            context.user_data["last_refresh_time"] = current_time

    await query.answer()

    api = create_api(user)

    if data == "servers_entry":
        keyboard = [
            [InlineKeyboardButton("搜索", callback_data="servers_mode_search")],
            [
                InlineKeyboardButton("在线", callback_data="servers_filter_online"),
                InlineKeyboardButton("离线", callback_data="servers_filter_offline"),
            ],
            [
                InlineKeyboardButton("高负载", callback_data="servers_filter_load"),
                InlineKeyboardButton("高流量", callback_data="servers_filter_traffic"),
            ],
            [InlineKeyboardButton("返回主菜单", callback_data="dashboard_back")],
        ]
        await api.close()
        await edit_message_with_auto_delete(
            query, "请选择服务器查看方式：", reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if data == "servers_mode_search":
        await api.close()
        await edit_message_with_auto_delete(
            query, "请直接发送 /server 关键词 或 /servers 关键词 搜索服务器。"
        )
        return

    if data.startswith("servers_filter_") or data.startswith("srvpg_"):
        params = context.user_data.get("server_search", {})
        if data.startswith("servers_filter_"):
            mode = data.split("_")[-1]
            params = {
                "query": "",
                "status": "online" if mode == "online" else "offline" if mode == "offline" else "all",
                "high_load": mode == "load",
                "high_traffic": mode == "traffic",
            }
            context.user_data["server_search"] = params
            page = 1
        else:
            page = int(data.split("_")[-1])
        try:
            server_data = await api.get_servers()
            servers = server_data.get("data", []) if server_data and server_data.get("success") else []
            results = filter_servers(servers, **params)
        except Exception as e:
            await api.close()
            await edit_message_with_auto_delete(query, f"搜索失败：{e}")
            return
        await api.close()
        if not results:
            await edit_message_with_auto_delete(query, "未找到匹配的服务器。")
            return
        text, reply_markup = build_server_results_message(results, page)
        await edit_message_with_auto_delete(query, text, reply_markup=reply_markup)
        return

    if data.startswith("server_detail_"):
        server_id = int(data.split("_")[-1])
        try:
            server = await api.get_server_detail(server_id)
        except Exception as e:
            await edit_message_with_auto_delete(query, f"获取服务器详情失败：{e}")
            await api.close()
            return

        await api.close()

        if not server:
            await edit_message_with_auto_delete(query, "未找到该服务器。")
            return

        name = server.get("name", "未知")
        online_status = is_online(server)
        status = "❇️在线" if online_status else "❌离线"
        ipv4 = server.get("geoip", {}).get("ip", {}).get("ipv4_addr", "未知")
        ipv6 = server.get("geoip", {}).get("ip", {}).get("ipv6_addr", "❌")

        # 对 IP 地址进行掩码处理
        ipv4 = mask_ipv4(ipv4)
        ipv6 = mask_ipv6(ipv6)

        platform = server.get("host", {}).get("platform", "未知")
        cpu_info = (
            ", ".join(server.get("host", {}).get("cpu", []))
            if server.get("host")
            else "未知"
        )
        uptime_seconds = server.get("state", {}).get("uptime", 0)
        uptime_days = uptime_seconds // 86400
        uptime_hours = (uptime_seconds % 86400) // 3600
        load_1 = server.get("state", {}).get("load_1", 0)
        load_5 = server.get("state", {}).get("load_5", 0)
        load_15 = server.get("state", {}).get("load_15", 0)
        cpu_usage = server.get("state", {}).get("cpu", 0)
        mem_used = server.get("state", {}).get("mem_used", 0)
        mem_total = server.get("host", {}).get("mem_total", 1)
        swap_used = server.get("state", {}).get("swap_used", 0)
        swap_total = server.get("host", {}).get("swap_total", 1)
        disk_used = server.get("state", {}).get("disk_used", 0)
        disk_total = server.get("host", {}).get("disk_total", 1)
        net_in_transfer = server.get("state", {}).get("net_in_transfer", 0)
        net_out_transfer = server.get("state", {}).get("net_out_transfer", 0)
        net_in_speed = server.get("state", {}).get("net_in_speed", 0)
        net_out_speed = server.get("state", {}).get("net_out_speed", 0)
        arch = server.get("host", {}).get("arch", "")

        response = f"""**{name}** {status}
==========================
**ID**: {server.get('id', '未知')}
**IPv4**: {ipv4}
**IPv6**: {ipv6}
**平台**： {platform}
**CPU 信息**： {cpu_info}
**运行时间**： {uptime_days} 天 {uptime_hours} 小时
**负载**： {load_1:.2f} {load_5:.2f} {load_15:.2f}
**CPU**： {cpu_usage:.2f}% [{arch}]
**内存**： {mem_used / mem_total * 100 if mem_total else 0:.1f}% [{format_bytes(mem_used)}/{format_bytes(mem_total)}]
**交换**： {swap_used / swap_total * 100 if swap_total else 0:.1f}% [{format_bytes(swap_used)}/{format_bytes(swap_total)}]
**磁盘**： {disk_used / disk_total * 100 if disk_total else 0:.1f}% [{format_bytes(disk_used)}/{format_bytes(disk_total)}]
**流量**： ↓{format_bytes(net_in_transfer)}     ↑{format_bytes(net_out_transfer)}
**网速**： ↓{format_bytes(net_in_speed)}/s     ↑{format_bytes(net_out_speed)}/s

**更新于**： {get_localized_time_string()}
"""
        # 添加刷新按钮
        keyboard = [
            [InlineKeyboardButton("刷新", callback_data=f"refresh_server_{server_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await edit_message_with_auto_delete(
            query, response, parse_mode="Markdown", reply_markup=reply_markup
        )

    elif data.startswith("refresh_server_"):
        server_id = int(data.split("_")[-1])
        # 重新获取服务器详情，与上面相同的代码
        try:
            server = await api.get_server_detail(server_id)
        except Exception as e:
            await edit_message_with_auto_delete(query, f"获取服务器详情失败：{e}")
            await api.close()
            return

        await api.close()

        if not server:
            await edit_message_with_auto_delete(query, "未找到该服务器。")
            return

        # 同上，构建响应和刷新按钮
        name = server.get("name", "未知")
        online_status = is_online(server)
        status = "❇️在线" if online_status else "❌离线"
        ipv4 = server.get("geoip", {}).get("ip", {}).get("ipv4_addr", "未知")
        ipv6 = server.get("geoip", {}).get("ip", {}).get("ipv6_addr", "❌")

        # 对 IP 地址进行掩码处理
        ipv4 = mask_ipv4(ipv4)
        ipv6 = mask_ipv6(ipv6)

        platform = server.get("host", {}).get("platform", "未知")
        cpu_info = (
            ", ".join(server.get("host", {}).get("cpu", []))
            if server.get("host")
            else "未知"
        )
        uptime_seconds = server.get("state", {}).get("uptime", 0)
        uptime_days = uptime_seconds // 86400
        uptime_hours = (uptime_seconds % 86400) // 3600
        load_1 = server.get("state", {}).get("load_1", 0)
        load_5 = server.get("state", {}).get("load_5", 0)
        load_15 = server.get("state", {}).get("load_15", 0)
        cpu_usage = server.get("state", {}).get("cpu", 0)
        mem_used = server.get("state", {}).get("mem_used", 0)
        mem_total = server.get("host", {}).get("mem_total", 1)
        swap_used = server.get("state", {}).get("swap_used", 0)
        swap_total = server.get("host", {}).get("swap_total", 1)
        disk_used = server.get("state", {}).get("disk_used", 0)
        disk_total = server.get("host", {}).get("disk_total", 1)
        net_in_transfer = server.get("state", {}).get("net_in_transfer", 0)
        net_out_transfer = server.get("state", {}).get("net_out_transfer", 0)
        net_in_speed = server.get("state", {}).get("net_in_speed", 0)
        net_out_speed = server.get("state", {}).get("net_out_speed", 0)
        arch = server.get("host", {}).get("arch", "")

        response = f"""**{name}** {status}
==========================
**ID**: {server.get('id', '未知')}
**IPv4**: {ipv4}
**IPv6**: {ipv6}
**平台**： {platform}
**CPU 信息**： {cpu_info}
**运行时间**： {uptime_days} 天 {uptime_hours} 小时
**负载**： {load_1:.2f} {load_5:.2f} {load_15:.2f}
**CPU**： {cpu_usage:.2f}% [{arch}]
**内存**： {mem_used / mem_total * 100 if mem_total else 0:.1f}% [{format_bytes(mem_used)}/{format_bytes(mem_total)}]
**交换**： {swap_used / swap_total * 100 if swap_total else 0:.1f}% [{format_bytes(swap_used)}/{format_bytes(swap_total)}]
**磁盘**： {disk_used / disk_total * 100 if disk_total else 0:.1f}% [{format_bytes(disk_used)}/{format_bytes(disk_total)}]
**流量**： ↓{format_bytes(net_in_transfer)}     ↑{format_bytes(net_out_transfer)}
**网速**： ↓{format_bytes(net_in_speed)}/s     ↑{format_bytes(net_out_speed)}/s

**更新于**： {get_localized_time_string()}
"""
        keyboard = [
            [InlineKeyboardButton("刷新", callback_data=f"refresh_server_{server_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await edit_message_with_auto_delete(
            query, response, parse_mode="Markdown", reply_markup=reply_markup
        )

    elif data == "refresh_overview":
        # 重新获取概览数据，与 overview 函数类似
        try:
            data = await api.get_overview()
        except Exception as e:
            await edit_message_with_auto_delete(query, f"获取数据失败：{e}")
            await api.close()
            return

        if data and data.get("success"):
            servers = data["data"]
            online_servers = 0
            offline_servers_info = []
            traffic_alerts = []
            total_servers = len(servers)
            total_mem = 0
            used_mem = 0
            total_swap = 0
            used_swap = 0
            total_disk = 0
            used_disk = 0
            net_in_speed = 0
            net_out_speed = 0
            net_in_transfer = 0
            net_out_transfer = 0

            for s in servers:
                server_name = s.get("name", "未知")
                if is_online(s):
                    online_servers += 1
                else:
                    last_active_str = s.get("last_active")
                    last_active_formatted = "未知时间"
                    if last_active_str:
                        try:
                            # 解析时间并转换为本地时区（如果设置了TZ）
                            last_active_dt_utc = parser.isoparse(
                                last_active_str
                            ).astimezone(timezone.utc)
                            tz_str = os.environ.get("TZ")
                            if tz_str:
                                try:
                                    target_tz = pytz.timezone(tz_str)
                                    last_active_dt_local = (
                                        last_active_dt_utc.astimezone(target_tz)
                                    )
                                    last_active_formatted = (
                                        last_active_dt_local.strftime(
                                            "%Y-%m-%d %H:%M:%S %Z%z"
                                        )
                                    )
                                except pytz.exceptions.UnknownTimeZoneError:
                                    last_active_formatted = last_active_dt_utc.strftime(
                                        "%Y-%m-%d %H:%M:%S UTC"
                                    )
                            else:
                                last_active_formatted = last_active_dt_utc.strftime(
                                    "%Y-%m-%d %H:%M:%S UTC"
                                )
                        except ValueError:
                            last_active_formatted = "无效时间格式"
                    offline_servers_info.append(
                        f"服务器 **{server_name}** 离线，最后在线: {last_active_formatted}"
                    )

                # 累加统计信息
                if s.get("host"):
                    total_mem += s["host"].get("mem_total", 0)
                    total_swap += s["host"].get("swap_total", 0)
                    total_disk += s["host"].get("disk_total", 0)
                if s.get("state"):
                    used_mem += s["state"].get("mem_used", 0)
                    used_swap += s["state"].get("swap_used", 0)
                    used_disk += s["state"].get("disk_used", 0)
                    net_in_speed += s["state"].get("net_in_speed", 0)
                    net_out_speed += s["state"].get("net_out_speed", 0)
                    current_net_in = s["state"].get("net_in_transfer", 0)
                    current_net_out = s["state"].get("net_out_transfer", 0)
                    net_in_transfer += current_net_in
                    net_out_transfer += current_net_out

                    # 检查流量阈值
                    if (
                        UPLOAD_ALERT_THRESHOLD_BYTES > 0
                        and current_net_out > UPLOAD_ALERT_THRESHOLD_BYTES
                    ):
                        traffic_alerts.append(
                            f"服务器 **{server_name}** 上行流量超限: {format_bytes(current_net_out)} / {format_bytes(UPLOAD_ALERT_THRESHOLD_BYTES)}"
                        )
                    if (
                        DOWNLOAD_ALERT_THRESHOLD_BYTES > 0
                        and current_net_in > DOWNLOAD_ALERT_THRESHOLD_BYTES
                    ):
                        traffic_alerts.append(
                            f"服务器 **{server_name}** 下行流量超限: {format_bytes(current_net_in)} / {format_bytes(DOWNLOAD_ALERT_THRESHOLD_BYTES)}"
                        )

            transfer_ratio = (
                (net_out_transfer / net_in_transfer * 100) if net_in_transfer else 0
            )

            response = f"""📊 **统计信息**
===========================
**服务器数量**： {total_servers}
**在线服务器**： {online_servers}
**内存**： {used_mem / total_mem * 100 if total_mem else 0:.1f}% [{format_bytes(used_mem)}/{format_bytes(total_mem)}]
**交换**： {used_swap / total_swap * 100 if total_swap else 0:.1f}% [{format_bytes(used_swap)}/{format_bytes(total_swap)}]
**磁盘**： {used_disk / total_disk * 100 if total_disk else 0:.1f}% [{format_bytes(used_disk)}/{format_bytes(total_disk)}]
**下行速度**： ↓{format_bytes(net_in_speed)}/s
**上行速度**： ↑{format_bytes(net_out_speed)}/s
**下行流量**： ↓{format_bytes(net_in_transfer)}
**上行流量**： ↑{format_bytes(net_out_transfer)}
**流量对等性**： {transfer_ratio:.1f}%
"""
            # 添加离线设备信息
            if offline_servers_info:
                response += "\n\n🔌 **离线设备**\n===========================\n"
                response += "\n".join(offline_servers_info)

            # 添加流量告警信息
            if traffic_alerts:
                response += "\n\n🚨 **流量告警**\n===========================\n"
                response += "\n".join(traffic_alerts)

            response += f"\n\n**更新于**： {get_localized_time_string()}"

            keyboard = [
                [InlineKeyboardButton("刷新", callback_data="refresh_overview")],
                [InlineKeyboardButton("切换面板", callback_data="dashboard_back")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            # 使用 edit_message_with_auto_delete 而不是 send_message_with_auto_delete
            await edit_message_with_auto_delete(
                query, response, parse_mode="Markdown", reply_markup=reply_markup
            )
        else:
            await edit_message_with_auto_delete(query, "获取服务器信息失败。")
        await api.close()

    elif data.startswith("cron_job_"):
        await api.close()
        cron_id = int(data.split("_")[-1])
        keyboard = [
            [InlineKeyboardButton("确认执行", callback_data=f"confirm_cron_{cron_id}")],
            [InlineKeyboardButton("取消", callback_data="cancel")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await edit_message_with_auto_delete(
            query, "您确定要执行此计划任务吗？", reply_markup=reply_markup
        )

    elif data.startswith("confirm_cron_"):
        cron_id = int(data.split("_")[-1])
        try:
            result = await api.run_cron_job(cron_id)
        except Exception as e:
            await edit_message_with_auto_delete(query, f"执行失败：{e}")
            await api.close()
            return

        await api.close()

        if result and result.get("success"):
            await edit_message_with_auto_delete(query, "计划任务已执行。")
        else:
            await edit_message_with_auto_delete(query, "执行失败。")

    elif data == "cancel":
        await api.close()
        await edit_message_with_auto_delete(query, "操作已取消。")

    elif data == "view_loop_traffic":
        await view_loop_traffic(query, context, api)

    elif data == "refresh_loop_traffic":
        await view_loop_traffic(query, context, api)

    elif data == "view_availability":
        await view_availability(query, context, api)

    elif data == "refresh_availability":
        await view_availability(query, context, api)

    elif data.startswith("set_default_"):
        dashboard_id = int(data.split("_")[-1])
        await api.close()
        await db.set_default_dashboard(query.from_user.id, dashboard_id)
        await edit_message_with_auto_delete(query, "已更新默认面板。")
        return

    elif data.startswith("dashboard_"):
        dashboard_id = int(data.split("_")[-1])
        dashboards = await db.get_all_dashboards(query.from_user.id)
        selected_dashboard = next(
            (d for d in dashboards if d["id"] == dashboard_id), None
        )

        if not selected_dashboard:
            await query.answer("未找到该面板", show_alert=True)
            return

        if selected_dashboard["is_default"]:
            await query.answer("这已经是默认面板了", show_alert=True)
            return

        # 直接切换默认面板
        await db.set_default_dashboard(query.from_user.id, dashboard_id)

        # 更新面板列表
        dashboards = await db.get_all_dashboards(query.from_user.id)
        keyboard = []
        for dashboard in dashboards:
            default_mark = "（当前默认）" if dashboard["is_default"] else ""
            button_text = f"{dashboard['alias']}{default_mark}"
            keyboard.append(
                [
                    InlineKeyboardButton(
                        button_text, callback_data=f"set_default_{dashboard['id']}"
                    ),
                    InlineKeyboardButton("设置", callback_data=f"settings_{dashboard['id']}")
                ]
            )

        reply_markup = InlineKeyboardMarkup(keyboard)
        await edit_message_with_auto_delete(
            query, "您的面板列表：", reply_markup=reply_markup
        )
        return

    elif data == "dashboard_back":
        await api.close()
        # 返回面板列表
        dashboards = await db.get_all_dashboards(query.from_user.id)
        keyboard = []
        for dashboard in dashboards:
            default_mark = "（当前默认）" if dashboard["is_default"] else ""
            button_text = f"{dashboard['alias']}{default_mark}"
            keyboard.append(
                [
                    InlineKeyboardButton(
                        button_text, callback_data=f"set_default_{dashboard['id']}"
                    ),
                    InlineKeyboardButton("设置", callback_data=f"settings_{dashboard['id']}")
                ]
            )

        reply_markup = InlineKeyboardMarkup(keyboard)
        await edit_message_with_auto_delete(
            query, "您的面板列表：", reply_markup=reply_markup
        )
        return


async def view_loop_traffic(query, context, api):
    # 获取服务状态
    try:
        services_data = await api.get_services_status()
    except Exception as e:
        await edit_message_with_auto_delete(query, f"获取服务信息失败：{e}")
        await api.close()
        return

    if services_data and services_data.get("success"):
        cycle_stats = services_data["data"].get("cycle_transfer_stats", {})
        if not cycle_stats:
            await edit_message_with_auto_delete(query, "暂无循环流量信息。")
            await api.close()
            return

        response = "**循环流量信息总览**\n==========================\n"
        for stat_name, stats in cycle_stats.items():
            rule_name = stats.get("name", "未知规则")
            server_names = stats.get("server_name", {})
            transfers = stats.get("transfer", {})
            max_transfer = stats.get("max", 1)  # 最大流量（字节）

            response += f"**规则：{rule_name}**\n"
            for server_id_str, transfer_value in transfers.items():
                server_id = str(server_id_str)
                server_name = server_names.get(server_id, f"服务器ID {server_id}")
                transfer_formatted = format_bytes(transfer_value)
                max_transfer_formatted = format_bytes(max_transfer)
                percentage = (
                    (transfer_value / max_transfer * 100) if max_transfer else 0
                )
                response += f"服务器 **{server_name}**：已使用 {transfer_formatted} / {max_transfer_formatted}，已使用 {percentage:.2f}%\n"
            response += "--------------------------\n"

        response += f"**更新于**： {get_localized_time_string()}"

        # 添加刷新按钮
        keyboard = [
            [InlineKeyboardButton("刷新", callback_data="refresh_loop_traffic")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await edit_message_with_auto_delete(
            query, response, parse_mode="Markdown", reply_markup=reply_markup
        )
    else:
        await edit_message_with_auto_delete(query, "获取循环流量信息失败。")
    await api.close()


async def view_availability(query, context, api):
    # 获取服务状态
    try:
        services_data = await api.get_services_status()
    except Exception as e:
        await edit_message_with_auto_delete(query, f"获取服务信息失败：{e}")
        await api.close()
        return
    # print("返回的服务数据:", services_data)

    if services_data and services_data.get("success"):
        services = services_data["data"].get("services", {})
        if not services:
            await edit_message_with_auto_delete(query, "暂无可用性监测信息。")
            await api.close()
            return

        response = "**可用性监测信息总览**\n==========================\n"
        for service_id, service_info in services.items():
            service = service_info.get("service", {})
            name = service_info.get("service_name", "未知")
            total_up = service_info.get("total_up", 0)
            total_down = service_info.get("total_down", 0)
            total = total_up + total_down
            availability = (total_up / total * 100) if total else 0
            status = "🟢 UP" if service_info.get("current_up", 0) else "🔴 DOWN"
            # 计算平均延迟
            delays = service_info.get("delay", [])
            if delays:
                avg_delay = sum(delays) / len(delays)
            else:
                avg_delay = None
            if avg_delay is not None:
                delay_text = f"，平均延迟 {avg_delay:.2f}ms"
            else:
                delay_text = ""
            response += f"**{name}**：可用率 {availability:.2f}%，状态 {status}{delay_text}\n------------------\n"
        response += f"\n**更新于**： {get_localized_time_string()}"

        # 添加刷新按钮
        keyboard = [
            [InlineKeyboardButton("刷新", callback_data="refresh_availability")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await edit_message_with_auto_delete(
            query, response, parse_mode="Markdown", reply_markup=reply_markup
        )
    else:
        await edit_message_with_auto_delete(query, "获取可用性监测信息失败。")
    await api.close()


async def cron_jobs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await db.get_user(update.effective_user.id)
    if not user:
        await send_message_with_auto_delete(
            update, context, "请先使用 /bind 命令绑定您的账号。"
        )
        return

    api = create_api(user)
    try:
        data = await api.get_cron_jobs()
    except Exception as e:
        await send_message_with_auto_delete(update, context, f"获取计划任务失败：{e}")
        await api.close()
        return

    if data and data.get("success"):
        cron_jobs = data["data"]
        if not cron_jobs:
            await send_message_with_auto_delete(update, context, "暂无计划任务。")
            await api.close()
            return

        keyboard = [
            [InlineKeyboardButton(job["name"], callback_data=f"cron_job_{job['id']}")]
            for job in cron_jobs
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await send_message_with_auto_delete(
            update, context, "请选择要执行的计划任务：", reply_markup=reply_markup
        )
    else:
        await send_message_with_auto_delete(update, context, "获取计划任务失败。")
    await api.close()


async def services_overview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await db.get_user(update.effective_user.id)
    if not user:
        await send_message_with_auto_delete(
            update, context, "请先使用 /bind 命令绑定您的账号。"
        )
        return

    keyboard = [
        [InlineKeyboardButton("查看循环流量信息", callback_data="view_loop_traffic")],
        [InlineKeyboardButton("查看可用性监测信息", callback_data="view_availability")],
        [InlineKeyboardButton("切换面板", callback_data="dashboard_back")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await send_message_with_auto_delete(
        update, context, "请选择要查看的服务信息：", reply_markup=reply_markup
    )


async def dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dashboards = await db.get_all_dashboards(update.effective_user.id)
    if not dashboards:
        await send_message_with_auto_delete(update, context, "您还没有绑定任何面板。")
        return

    keyboard = []
    for dashboard in dashboards:
        default_mark = "（当前默认）" if dashboard["is_default"] else ""
        button_text = f"{dashboard['alias']}{default_mark}"
        keyboard.append(
            [
                InlineKeyboardButton(
                    button_text, callback_data=f"set_default_{dashboard['id']}"
                ),
                InlineKeyboardButton(
                    "设置", callback_data=f"settings_{dashboard['id']}"
                )
            ]
        )

    reply_markup = InlineKeyboardMarkup(keyboard)
    await send_message_with_auto_delete(
        update, context, "您的面板列表：", reply_markup=reply_markup
    )


async def mcp_shortcut(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await send_message_with_auto_delete(update, context, "MCP/LLM 设置和执行命令请在私聊中使用。")
        return
    dashboards = await db.get_all_dashboards(update.effective_user.id)
    if not dashboards:
        await send_message_with_auto_delete(update, context, "请先使用 /bind 命令绑定您的账号。")
        return
    if len(dashboards) == 1:
        await send_dashboard_settings(update, context, dashboards[0]["id"])
        return
    keyboard = [
        [InlineKeyboardButton(d["alias"] or d["dashboard_url"], callback_data=f"settings_{d['id']}")]
        for d in dashboards
    ]
    await send_message_with_auto_delete(
        update, context, "请选择要设置的面板：", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def send_dashboard_settings(update, context, dashboard_id):
    dashboard_row = await db.get_dashboard(update.effective_user.id, int(dashboard_id))
    if not dashboard_row:
        await send_message_with_auto_delete(update, context, "未找到该面板。")
        return
    llm_config = await db.get_llm_config(update.effective_user.id, int(dashboard_id))
    auth_text = dashboard_auth_text(dashboard_row)
    token_text = redact_secret(dashboard_row.get("api_token"))
    llm_text = "已启用" if llm_config and llm_config.get("enabled") and llm_config.get("api_key") else "未配置"
    text = (
        f"面板设置：{dashboard_row.get('alias') or 'NEZHA'}\n"
        f"URL：{dashboard_row.get('dashboard_url')}\n"
        f"认证：{auth_text}\n"
        f"API Token：{token_text}\n"
        f"LLM/MCP 助手：{llm_text}\n\n"
        "提示：完整使用建议授予 nezha:inventory:read、nezha:service:read、nezha:cron:read、nezha:cron:exec、nezha:server:exec；需要 MCP 读取单机详情时再加 nezha:server:read，并限制服务器白名单。"
    )
    keyboard = [
        [InlineKeyboardButton("测试连接", callback_data=f"settings_test_{dashboard_id}")],
        [InlineKeyboardButton("绑定/更换 API Token", callback_data=f"settings_token_{dashboard_id}")],
        [InlineKeyboardButton("配置 LLM", callback_data=f"settings_llm_{dashboard_id}")],
        [InlineKeyboardButton("返回面板列表", callback_data="dashboard_back")],
    ]
    await send_message_with_auto_delete(update, context, text, reply_markup=InlineKeyboardMarkup(keyboard))


async def settings_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    waiting = context.user_data.get("settings_waiting")
    if not waiting:
        return
    if update.effective_chat.type != "private":
        return
    value = update.message.text.strip()
    dashboard_id = int(waiting["dashboard_id"])
    if waiting["type"] == "token":
        if not value.startswith("nzp_"):
            await update.message.reply_text("API Token 格式应以 nzp_ 开头，已取消。")
            context.user_data.pop("settings_waiting", None)
            return
        dashboard_row = await db.get_dashboard(update.effective_user.id, dashboard_id)
        api = NezhaAPI(dashboard_row["dashboard_url"], auth_type="token", api_token=value)
        try:
            await api.validate_api_token()
        except Exception as e:
            await update.message.reply_text(f"Token 验证失败：{e}")
            await api.close()
            context.user_data.pop("settings_waiting", None)
            return
        await api.close()
        await db.update_dashboard_token(update.effective_user.id, dashboard_id, value)
        await update.message.reply_text("API Token 已保存，可用于 LLM/MCP 命令执行。")
        context.user_data.pop("settings_waiting", None)
        return
    if waiting["type"] == "llm":
        parts = [p.strip() for p in value.splitlines() if p.strip()]
        if len(parts) < 3:
            await update.message.reply_text("LLM 配置需要三行：base_url、model、api_key。")
            return
        await db.upsert_llm_config(
            update.effective_user.id,
            dashboard_id,
            base_url=parts[0],
            model=parts[1],
            api_key=parts[2],
            enabled=True,
        )
        await update.message.reply_text("LLM 配置已保存。")
        context.user_data.pop("settings_waiting", None)


async def chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await send_message_with_auto_delete(update, context, "LLM 运维助手和命令执行请在私聊中使用。")
        return
    dashboard_row = await db.get_user(update.effective_user.id)
    if not dashboard_row:
        await update.message.reply_text("请先使用 /bind 命令绑定您的账号。")
        return
    user_text = " ".join(context.args).strip()
    message_thread_id = getattr(update.effective_message, "message_thread_id", None) or 0
    chat_id = update.effective_chat.id
    if user_text.lower() in {"reset", "重置", "清空", "clear"}:
        await db.reset_llm_session(
            update.effective_user.id, chat_id, message_thread_id, dashboard_row["id"]
        )
        await update.message.reply_text("已清空当前面板的 LLM 对话记忆和待确认命令。")
        return
    if not user_text:
        await update.message.reply_text(
            "请在 /chat 后面写你的请求，例如：/chat 在 hk 分组执行 `apt-get update`\n"
            "清空记忆：/chat reset"
        )
        return
    llm_config = await db.get_llm_config(update.effective_user.id, dashboard_row["id"])
    api = create_api(dashboard_row)
    try:
        result = await run_assistant_message(
            db,
            update.effective_user.id,
            dashboard_row,
            api,
            llm_config,
            user_text,
            chat_id=chat_id,
            message_thread_id=message_thread_id,
        )
    except Exception as e:
        await update.message.reply_text(f"助手处理失败：{e}")
        await api.close()
        return
    await api.close()
    pending = result.get("pending_execution")
    if pending:
        execution_id = secrets.token_urlsafe(8)
        await db.save_pending_execution(
            execution_id,
            update.effective_user.id,
            pending.dashboard_id,
            serialize_pending_execution(pending),
            pending.created_at,
        )
        preview_names = "\n".join(f"- {name}" for name in pending.server_names[:10])
        more = "" if len(pending.server_names) <= 10 else f"\n... 还有 {len(pending.server_names) - 10} 台"
        match_text = (
            f"匹配依据：{pending.match_summary}\n"
            if pending.match_summary
            else ""
        )
        text = (
            "即将批量执行命令，请确认：\n\n"
            f"面板：{pending.dashboard_alias}\n"
            f"目标：{pending.source}\n"
            f"{match_text}"
            f"数量：{len(pending.server_ids)} 台\n"
            f"服务器：\n{preview_names}{more}\n\n"
            f"命令：\n{pending.command}\n\n"
            "风险提示：该命令会在目标服务器上执行。确认前请检查目标和命令。"
        )
        keyboard = [
            [InlineKeyboardButton("确认执行", callback_data=f"exec_confirm_{execution_id}")],
            [InlineKeyboardButton("取消", callback_data=f"exec_cancel_{execution_id}")],
        ]
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        return
    await update.message.reply_text(result.get("text") or "没有可展示的回复。")


async def edit_message_with_auto_delete(query: CallbackQuery, text: str, **kwargs):
    """
    编辑消息并在群组中设置自动删除
    """
    await query.edit_message_text(text, **kwargs)

    # 如果是群组消息，设置定时删除
    if query.message.chat.type in ["group", "supergroup"]:
        context = query.get_bot()
        # 设置定时删除
        context.job_queue.run_once(
            lambda ctx: delete_message_later(
                ctx, query.message.chat_id, query.message.message_id
            ),
            GROUP_MESSAGE_LIFETIME,
        )


def main():
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # 初始化数据库
    loop = asyncio.get_event_loop()
    loop.run_until_complete(db.initialize())

    # 回调查询处理（放在最前面）
    application.add_handler(CallbackQueryHandler(button_handler))

    # 命令处理
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("unbind", unbind))
    application.add_handler(CommandHandler("overview", overview))
    application.add_handler(CommandHandler("cron", cron_jobs))
    application.add_handler(CommandHandler("services", services_overview))
    application.add_handler(CommandHandler("dashboard", dashboard))
    application.add_handler(CommandHandler("servers", servers_entry))
    application.add_handler(CommandHandler("mcp", mcp_shortcut))
    application.add_handler(CommandHandler("chat", chat_command))
    application.add_handler(CommandHandler("llm", chat_command))

    # 绑定命令的会话处理
    bind_handler = ConversationHandler(
        entry_points=[CommandHandler("bind", bind_start)],
        states={
            BIND_USERNAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, bind_username)
            ],
            BIND_PASSWORD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, bind_password)
            ],
            BIND_DASHBOARD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, bind_dashboard)
            ],
            BIND_ALIAS: [MessageHandler(filters.TEXT & ~filters.COMMAND, bind_alias)],
        },
        fallbacks=[],
    )
    application.add_handler(bind_handler)

    # 查看单台服务器状态的会话处理
    server_handler = ConversationHandler(
        entry_points=[CommandHandler("server", server_status)],
        states={
            SEARCH_SERVER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, search_server)
            ],
        },
        fallbacks=[],
    )
    application.add_handler(server_handler)

    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, settings_text_handler),
        group=1,
    )

    # 在 run_polling 中指定 allowed_updates
    application.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
