// 引入隐身插件
const { chromium } = require('playwright-extra');
const stealth = require('puppeteer-extra-plugin-stealth')();

chromium.use(stealth);

const targetUrl = process.argv[2];
const timeout = parseInt(process.argv[3] || '20000', 10);

if (!targetUrl) {
    console.log(JSON.stringify({ error: "缺少 URL 参数" }));
    process.exit(1);
}

(async () => {
    const browser = await chromium.launch({ 
        headless: true,
        args: ['--autoplay-policy=no-user-gesture-required', '--no-sandbox', '--disable-setuid-sandbox'] // 允许无交互自动播放
    });
    const context = await browser.newContext({
        userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    });
    const page = await context.newPage();

    let videoUrls = new Set();
    let m3u8Content = "";

    // 1. 监听网络中的媒体请求
    page.on('response', async response => {
        const url = response.url();
        const type = response.request().resourceType();
        const contentType = response.headers()['content-type'] || '';
        
        if (type === 'media' || url.includes('.m3u8') || url.includes('.mp4') || contentType.includes('mpegurl')) {
            videoUrls.add(url);
            // 如果抓到 m3u8 文本，顺手截取一段
            if ((url.includes('.m3u8') || contentType.includes('mpegurl')) && !m3u8Content) {
                try {
                    const text = await response.text();
                    m3u8Content = text.substring(0, 1000);
                } catch (e) {}
            }
        }
    });

    try {
        await page.goto(targetUrl, { waitUntil: 'domcontentloaded', timeout: 15000 });
    } catch (e) {
        // 忽略超时，继续后面的操作
    }

    // 2. 尝试提取已经存在的 <video> 标签
    const videoTags = await page.evaluate(() => {
        const videos = Array.from(document.querySelectorAll('video'));
        return videos.map(v => ({
            src: v.src || v.currentSrc,
            sources: Array.from(v.querySelectorAll('source')).map(s => s.src)
        }));
    });
    
    for (const v of videoTags) {
        if (v.src) videoUrls.add(v.src);
        for (const s of v.sources) {
            if (s) videoUrls.add(s);
        }
    }

    // 3. 如果没有 src，尝试寻找并点击播放按钮（暴力试探）
    if (videoUrls.size === 0) {
        try {
            // 寻找可能是播放按钮的元素（包含 play 类名、id 或常见文本）
            const clicked = await page.evaluate(() => {
                const selectors = [
                    '.play', '.vjs-big-play-button', '.xgplayer-start', 
                    '[class*="play"]', '[id*="play"]', 'button:has-text("播放")'
                ];
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el && el.offsetParent !== null) { // 可见
                        el.click();
                        return true;
                    }
                }
                return false;
            });
            
            if (clicked) {
                // 点击后等待 5 秒，让播放器加载
                await page.waitForTimeout(5000);
            }
        } catch (e) {}
    }

    // 4. 再次检查视频标签（点击后可能动态生成了 src）
    const finalTags = await page.evaluate(() => {
        const videos = Array.from(document.querySelectorAll('video'));
        return videos.map(v => v.src || v.currentSrc).filter(Boolean);
    });
    for (const s of finalTags) {
        videoUrls.add(s);
    }

    await browser.close();

    const result = {
        success: true,
        found_count: videoUrls.size,
        urls: Array.from(videoUrls),
    };

    if (m3u8Content) {
        result.m3u8_preview = m3u8Content;
    }

    console.log(JSON.stringify(result, null, 2));
})();
