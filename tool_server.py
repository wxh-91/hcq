import asyncio
import json
import logging
from admin import execute_tool_directly
from cl import DEFAULT_ADMIN

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ToolServer")

async def handle_client(reader, writer):
    """处理单个 Socket 客户端（Pi 扩展）"""
    try:
        data = await reader.read(4096)
        if not data:
            return
        request = json.loads(data.decode())
        tool_name = request.get("tool")
        args = request.get("args", {})
        sender_id = request.get("sender_id", DEFAULT_ADMIN)  # Pi 可传，默认主管理员

        logger.info(f"收到工具调用: {tool_name} {args}")

        # 执行工具
        try:
            result = await execute_tool_directly(tool_name, args, sender_id)
            response = {"success": True, "output": result}
        except Exception as e:
            logger.exception("工具执行失败")
            response = {"success": False, "error": str(e)}

        writer.write(json.dumps(response).encode())
        await writer.drain()
    except Exception as e:
        logger.exception("处理请求异常")
    finally:
        writer.close()
        await writer.wait_closed()

async def main():
    server = await asyncio.start_server(handle_client, "127.0.0.1", 9999)
    logger.info("Socket 服务已启动，监听 127.0.0.1:9999")
    async with server:
        await server.serve_forever()

if __name__ == "__main__":
    asyncio.run(main())
