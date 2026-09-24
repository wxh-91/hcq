import asyncio
import json
import uuid
import logging
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from dataclasses import dataclass, field
from typing import Optional
import uvicorn

from ai import get_ai_response, submit_tool_results, run_async_task_chain, conversation_history
from admin import execute_tool_directly
from memory import Memory

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ECHO")

app = FastAPI()
memory = Memory()

# ========== 会话持久化函数 ==========
def load_session_history(session_id: str) -> list:
    """从 memory 加载会话历史"""
    key = f"web_session_{session_id}"
    data = memory.get(key, [])
    if data:
        logger.info(f"加载会话历史: {session_id}, {len(data)} 条消息")
    return data

def save_session_history(session_id: str, history: list) -> None:
    """保存会话历史到 memory"""
    key = f"web_session_{session_id}"
    memory.set(key, history)
    logger.debug(f"保存会话历史: {session_id}, {len(history)} 条消息")

def delete_session_history(session_id: str) -> None:
    """删除会话历史（相当于清除记忆）"""
    key = f"web_session_{session_id}"
    memory.set(key, [])
    logger.info(f"已清除会话历史: {session_id}")

# ========== 任务队列系统 ==========
@dataclass(order=True)
class TaskItem:
    priority: int
    session_id: str
    user_input: str
    task_type: str = "single"
    websocket: Optional[WebSocket] = field(compare=False, default=None)
    seq: int = field(init=False, default=0)

    def __post_init__(self):
        if not hasattr(TaskItem, "_counter"):
            TaskItem._counter = 0
        TaskItem._counter += 1
        self.seq = TaskItem._counter

high_priority_queue = asyncio.Queue()
low_priority_queue = asyncio.Queue()
active_sessions = {}

consumer_task = None
consumer_running = True


async def task_consumer():
    global consumer_running
    logger.info("Task Consumer started")
    while consumer_running:
        try:
            task = await asyncio.wait_for(high_priority_queue.get(), timeout=0.5)
        except asyncio.TimeoutError:
            try:
                task = await asyncio.wait_for(low_priority_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue

        logger.info(f"Executing task [priority {task.priority}] {task.session_id}: {task.user_input[:30]}...")
        await process_task(task)
        if task.priority == 0:
            high_priority_queue.task_done()
        else:
            low_priority_queue.task_done()


async def process_task(task: TaskItem):
    ws = task.websocket
    session_id = task.session_id

    # 确保会话历史已加载到 conversation_history
    if session_id not in conversation_history:
        history = load_session_history(session_id)
        conversation_history[session_id] = history
    else:
        # 如果已有内存历史，确保与存储同步（以防内存被清空）
        history = conversation_history[session_id]
        if not history:
            loaded = load_session_history(session_id)
            if loaded:
                conversation_history[session_id] = loaded

    try:
        # chain task
        if task.task_type == "chain":
            await ws.send_text(json.dumps({
                "type": "assistant",
                "content": "开始执行大型任务链..."
            }))
            summary = await run_async_task_chain(
                session_id,
                task.user_input,
                max_steps=10,
                chat_type="web",
                planning_mode=True
            )
            await ws.send_text(json.dumps({
                "type": "assistant",
                "content": summary
            }))
            # 保存历史
            save_session_history(session_id, conversation_history.get(session_id, []))
            return

        # single task
        ai_response = await get_ai_response(session_id, task.user_input, chat_type="web")
        content = ai_response.get("content", "")
        tool_calls = ai_response.get("tool_calls", [])

        if not tool_calls:
            await ws.send_text(json.dumps({
                "type": "assistant",
                "content": content if content else "任务已完成（无输出）"
            }))
            save_session_history(session_id, conversation_history.get(session_id, []))
            return

        tool_results = []
        for idx, tc in enumerate(tool_calls):
            tool_name = tc["function"]["name"]
            tool_args = tc["function"].get("arguments", {})
            tool_call_id = tc.get("id", f"call_{idx}")

            await ws.send_text(json.dumps({
                "type": "assistant",
                "content": f"正在执行：{tool_name}"
            }))

            try:
                exec_result = await execute_tool_directly(tool_name, tool_args, session_id)
                if exec_result is None:
                    exec_result = "执行成功（无返回）"
            except Exception as e:
                exec_result = f"执行失败: {str(e)}"
                await ws.send_text(json.dumps({
                    "type": "error",
                    "content": exec_result
                }))

            display_output = exec_result[:500] + "..." if len(exec_result) > 500 else exec_result
            await ws.send_text(json.dumps({
                "type": "assistant",
                "content": f"执行结果: {display_output}"
            }))

            tool_results.append({
                "tool_call_id": tool_call_id,
                "output": exec_result
            })

        await asyncio.sleep(0.8)
        summary = await submit_tool_results(session_id, tool_results)
        if summary.startswith(("错误", "请求失败", "请求超时")):
            await ws.send_text(json.dumps({
                "type": "error",
                "content": f"总结生成失败：{summary}"
            }))
        else:
            await ws.send_text(json.dumps({
                "type": "assistant",
                "content": summary
            }))

        # 保存历史
        save_session_history(session_id, conversation_history.get(session_id, []))

    except Exception as e:
        logger.error(f"Task processing error: {e}", exc_info=True)
        await ws.send_text(json.dumps({
            "type": "error",
            "content": f"任务执行失败: {str(e)}"
        }))


@app.on_event("startup")
async def startup_event():
    global consumer_task
    consumer_task = asyncio.create_task(task_consumer())


@app.on_event("shutdown")
async def shutdown_event():
    global consumer_running
    consumer_running = False
    if consumer_task:
        consumer_task.cancel()
        await consumer_task


@app.get("/")
async def get():
    with open("web_ui.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    # 从查询参数或首条消息获取 session_id
    # 前端可以传 ?session_id=xxx 或首条消息中带 session_id
    session_id = None
    try:
        # 尝试从查询参数获取
        query_params = dict(websocket.query_params)
        session_id = query_params.get("session_id")
    except:
        pass

    if not session_id:
        # 新会话
        session_id = str(uuid.uuid4())
    else:
        # 旧会话，检查是否有效
        history = load_session_history(session_id)
        if history:
            # 将历史加载到内存
            conversation_history[session_id] = history
            logger.info(f"恢复会话: {session_id}, {len(history)} 条消息")
        else:
            # session_id 无效，生成新的
            session_id = str(uuid.uuid4())
            logger.info(f"会话无效，生成新会话: {session_id}")

    active_sessions[session_id] = websocket
    logger.info(f"Session {session_id} connected")

    # 发送 session_id 给前端（用于刷新后重连）
    await websocket.send_text(json.dumps({
        "type": "system",
        "content": f"ECHO 终端已就绪 (会话: {session_id})"
    }))

    try:
        while True:
            data = await websocket.receive_text()
            try:
                payload = json.loads(data)
                user_input = payload.get("text", "").strip()
                task_type = payload.get("type", "single")
                # 前端可传 session_id 用于切换会话
                frontend_session = payload.get("session_id")
                if frontend_session and frontend_session != session_id:
                    # 切换到另一个会话（加载历史）
                    session_id = frontend_session
                    history = load_session_history(session_id)
                    conversation_history[session_id] = history
                    await websocket.send_text(json.dumps({
                        "type": "system",
                        "content": f"已切换到会话: {session_id}"
                    }))
                if not user_input:
                    continue
            except json.JSONDecodeError:
                continue

            if task_type == "chain" or len(user_input) > 100 or "分析" in user_input or "批量" in user_input:
                task = TaskItem(
                    priority=2,
                    session_id=session_id,
                    user_input=user_input,
                    task_type="chain",
                    websocket=websocket
                )
                await low_priority_queue.put(task)
                logger.info(f"Large task queued (low priority): {user_input[:30]}...")
            else:
                task = TaskItem(
                    priority=1,
                    session_id=session_id,
                    user_input=user_input,
                    task_type="single",
                    websocket=websocket
                )
                await low_priority_queue.put(task)

    except WebSocketDisconnect:
        logger.info(f"Session {session_id} disconnected")
    finally:
        # 保存当前会话历史
        if session_id in conversation_history:
            save_session_history(session_id, conversation_history[session_id])
        active_sessions.pop(session_id, None)


async def submit_qq_task(user_id: str, user_input: str) -> str:
    session_id = f"qq_{user_id}_{uuid.uuid4().hex[:8]}"
    task = TaskItem(
        priority=0,
        session_id=session_id,
        user_input=user_input,
        task_type="single",
        websocket=None
    )
    await high_priority_queue.put(task)
    return f"QQ task queued (high priority), session_id={session_id}"


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
