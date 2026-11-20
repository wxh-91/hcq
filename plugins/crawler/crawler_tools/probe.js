// 引入隐身插件
const { chromium } = require('playwright-extra');
const stealth = require('puppeteer-extra-plugin-stealth')();

chromium.use(stealth);

const fs = require('fs');
const path = require('path');

// 命令行参数：url, timeout(ms)
const targetUrl = process.argv[2];
const timeout = parseInt(process.argv[3] || '15000', 10);

if (!targetUrl) {
    console.log(JSON.stringify({ error: "缺少 URL 参数" }));
    process.exit(1);
}

(async () => {
    const workspace = path.join(__dirname, 'workspace');
    // 清理旧产物 (可选，这里不清理，避免并发冲突，统一由外部清理)
    
    const browser = await chromium.launch({ headless: true, args: ['--no-sandbox', '--disable-setuid-sandbox'] });
    const context = await browser.newContext({
        userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    });
    const page = await context.newPage();
    
    const requests = [];

    // 监听请求
    page.on('request', request => {
        const type = request.resourceType();
        if (['xhr', 'fetch', 'document', 'media'].includes(type)) {
            requests.push({
                method: request.method(),
                url: request.url(),
                type: type,
                headers: request.headers(), // 包含 referer 等信息
                postData: request.postData() ? request.postData().substring(0, 500) : null // 截断防太长
            });
        }
    });

    // 监听响应，抓取 Content-Type 和部分内容
    page.on('response', async response => {
        const type = response.request().resourceType();
        if (['xhr', 'fetch', 'media'].includes(type)) {
            try {
                const headers = response.headers();
                const contentType = headers['content-type'] || '';
                // 如果是 m3u8 文本，直接抓取内容！
                if (contentType.includes('mpegurl') || response.url().includes('.m3u8')) {
                    const text = await response.text();
                    // 找到对应的 request 对象追加数据
                    const reqObj = requests.find(r => r.url === response.url());
                    if (reqObj) reqObj.response_preview = text.substring(0, 1000);
                }
            } catch (e) {
                // 忽略读取失败的响应
            }
        }
    });

    try {
        await page.goto(targetUrl, { waitUntil: 'networkidle', timeout: timeout });
    } catch (e) {
        // 超时也继续，把已经抓到的返回
    }

    // 停留额外时间，等待异步请求
    await page.waitForTimeout(3000);

    await browser.close();

    // 去重并输出
    const uniqueReqs = [];
    const seen = new Set();
    for (const r of requests) {
        if (!seen.has(r.url)) {
            seen.add(r.url);
            uniqueReqs.push(r);
        }
    }

    console.log(JSON.stringify({ success: true, requests: uniqueReqs }, null, 2));
})();
