import re

# 常见提示词注入/越狱黑名单正则
INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(previous|prior)\s+instructions",
    r"忽略(之前|上述|所有)的?(指令|提示)",
    r"system\s*prompt",
    r"你现在是一个",
    r"you\s+are\s+now\s+a",
    r"执行(rm|sudo|sh|bash)",
    r"execute\s+(rm|sudo|sh|bash)",
    r"<script.*?>.*?</script>", # 防XSS混入
    r"os\.system\(",
    r"subprocess\.run\("
]

# 编译正则，提高匹配效率
COMPILED_PATTERNS = [re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS]

def sanitize_crawler_output(text: str) -> str:
    """
    清洗爬虫抓取回来的文本，防止提示词注入。
    """
    if not text:
        return text
    
    # 1. 截断过长内容（防止撑爆上下文）
    if len(text) > 8000:
        text = text[:8000] + "\n...[内容过长已截断]"
        
    # 2. 正则黑名单过滤
    for pattern in COMPILED_PATTERNS:
        if pattern.search(text):
            # 一旦命中，有两种处理方式：
            # A. 粗暴打断：return "[安全拦截] 抓取内容包含疑似恶意指令，已屏蔽。"
            # B. 局部替换：把匹配到的词替换为 ***
            text = pattern.sub("[已拦截的危险指令]", text)
            
    return text
